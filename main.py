import os
import shutil
import uuid
import json
import asyncio
from typing import List, Annotated
import datetime as dt
from datetime import datetime
import cv2

import numpy as np
import soundfile as sf

from fastapi import (
    FastAPI, UploadFile, File, Form, HTTPException, Depends, status,
    WebSocket, WebSocketDisconnect
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from dotenv import load_dotenv
import torch
from transformers import pipeline
import google.generativeai as genai
from gtts import gTTS
from sqlalchemy.orm import Session
from google.cloud import storage

import firebase_admin
from firebase_admin import credentials, messaging

from sqlalchemy.orm import Session


# 로컬 모듈 import
import models
import schemas
import auth
from database import SessionLocal, engine, get_db


import wave
import os

AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 1

# --- 1. 초기 설정 ---
load_dotenv()


tags_metadata = [
    {"name": "System", "description": "시스템 상태 확인"},
    {"name": "Auth", "description": "인증 (회원가입, 로그인)"},
    {"name": "User", "description": "사용자 정보 및 상태 관리"},
    {"name": "Device", "description": "기기 등록 및 관리"},
    {"name": "Visit", "description": "방문 기록 관리"},
    {"name": "Appointment", "description": "일정 관리"},
    {"name": "Upload", "description": "파일 업로드"},
]

app = FastAPI(
    title="ALENTO Smart Doorbell Server",
    version="1.0.0",
    openapi_tags=tags_metadata
)

# 전역 변수
storage_client = None
bucket = None
stt_pipe = None
llm_model = None
system_instruction = ""
# --- VAD globals ---
vad_model = None
vad_utils = None
active_conversations_ws: dict[str, WebSocket] = {}   # 실제 소켓 저장용
active_conversation_devices: set[str] = set()       # 중복 방지용(필요하면)


origins = [
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "https://alento-liart.vercel.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# [헬퍼] 한국 시간(KST) 구하기
def get_kst_now():
    return dt.datetime.utcnow() + dt.timedelta(hours=9)

# --- 2. 헬퍼 함수 및 클래스 ---

def notify_user(user_id: int, title: str, body: str, db):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user or not user.push_token:
        return

    try:
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            token=user.push_token,
        )
        messaging.send(message)
        print(f"🔔 FCM 전송: {title}")

    except Exception as e:
        print(f"⚠️ FCM 전송 실패: {e}")

        # ✅ 토큰이 죽은 경우 DB에서 제거
        if "Requested entity was not found" in str(e) or "unregistered" in str(e).lower():
            user.push_token = None
            db.commit()
            print("🧹 죽은 push_token 제거 완료")

class VideoConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, List[WebSocket]] = {}

    async def connect(self, device_id: int, websocket: WebSocket):
        await websocket.accept()
        if device_id not in self.active_connections:
            self.active_connections[device_id] = []
        self.active_connections[device_id].append(websocket)

    def disconnect(self, device_id: int, websocket: WebSocket):
        if device_id in self.active_connections:
            try:
                self.active_connections[device_id].remove(websocket)
            except ValueError: pass

    async def broadcast_to_device_viewers(self, device_id: int, data: bytes):
        if device_id not in self.active_connections:
            return
        
        connections = list(self.active_connections[device_id])
        if not connections:
            return

        remove_list = []

        for ws in connections:
            try:
                await ws.send_bytes(data)
            except Exception:
                remove_list.append(ws)

        # 끊긴 소켓 정리
        for ws in remove_list:
            try:
                self.active_connections[device_id].remove(ws)
            except:
                pass

video_manager = VideoConnectionManager()


# --- 3. Startup ---
@app.on_event("startup")
def startup_event():
    global storage_client, bucket, stt_pipe, llm_model, system_instruction
    global vad_model, vad_utils

    try:
        models.Base.metadata.create_all(bind=engine)
        print("DB tables ensured.")
    except Exception as e:
        print("DB create_all failed:", e)
    
    # Firebase
    try:
        cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "/app/firebase_admin_key.json")
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        print("Firebase Init OK")
    except Exception as e:
        print("Firebase Init Failed:", e)

    # GCS
    try:
        storage_client = storage.Client()
        GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
        if GCS_BUCKET_NAME:
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            print("GCS Init OK")
        else:
            print("⚠️ GCS Bucket Not Set")
    except Exception as e:
        print(f"GCS Init Failed: {e}")

    print("Loading AI Models...")

    # ✅ STT (Whisper)
    device = -1
    stt_pipe = pipeline(
        "automatic-speech-recognition",
        model="openai/whisper-small",
        device=device
    )

    # ✅ LLM
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    genai.configure(api_key=GOOGLE_API_KEY)
    llm_model = genai.GenerativeModel("gemini-2.5-flash")

    system_instruction = """
    당신은 스마트 초인종 AI 비서입니다. 
    방문객을 친절하게 응대하고, 전달받은 집주인의 정보를 바탕으로 적절히 대답하세요.
    """

    # ✅ VAD (Silero) 정상 로드
    try:
        vad_model, vad_utils = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            trust_repo=True
        )
        print("VAD Loaded.")
    except Exception as e:
        vad_model, vad_utils = None, None
        print("⚠️ VAD Load Failed:", e)

    print("AI Models Loaded.")



# --- 4. 유틸리티 ---

def upload_to_gcs(file_path: str, destination_blob_name: str) -> str:
    if not bucket: return None
    try:
        blob = bucket.blob(destination_blob_name)
        blob.upload_from_filename(file_path)
        return blob.public_url
    except Exception as e:
        print(f"Upload Error: {e}")
        return None

def text_to_speech(text: str, filename: str) -> str:
    tts = gTTS(text=text, lang='ko')
    tts.save(filename)
    return filename

def get_llm_response(current_user: models.User, full_transcript: str, db: Session, device: models.Device = None) -> str:
    global llm_model, system_instruction
    
    user_info = {
        "name": current_user.full_name,
        "is_home": current_user.is_home,
        "memo": current_user.memo
    }
    
    appointments = db.query(models.Appointment).filter(
        models.Appointment.user_id == current_user.id
    ).order_by(models.Appointment.start_time.asc()).all()

    appt_list = [f"{a.title} ({a.start_time})" for a in appointments]
    
    full_prompt = f"""
    {system_instruction}
    [집주인 정보]: {user_info}
    [일정]: {appt_list}
    [대화 내용]:
    {full_transcript}
    
    AI 응답 (한 문장으로 간결하게):
    """
    try:
        response = llm_model.generate_content(full_prompt)
        return response.text
    except:
        return "잠시만 기다려 주세요."

def get_ai_post_processing(transcript_text: str) -> dict:
    global llm_model
    prompt = f"""
    대화 요약 및 일정 추출 (JSON):
    keys: "summary", "appointment" (null or {{title, start_time, end_time}})
    [대화]: {transcript_text}
    """
    try:
        res = llm_model.generate_content(prompt)
        json_text = res.text.strip().replace("```json", "").replace("```", "")
        return json.loads(json_text)
    except:
        return {"summary": "요약 실패", "appointment": None}


def _resample_np(audio_int16: np.ndarray, src_sr: int, tgt_sr: int) -> np.ndarray:
    """간단 선형 리샘플(외부 라이브러리 없이)"""
    if src_sr == tgt_sr:
        return audio_int16.astype(np.float32) / 32768.0

    x_old = np.linspace(0, 1, len(audio_int16))
    x_new = np.linspace(0, 1, int(len(audio_int16) * tgt_sr / src_sr))
    audio_float = np.interp(x_new, x_old, audio_int16).astype(np.float32)
    return audio_float / 32768.0


def vad_split_segments(
    pcm_chunks: List[bytes],
    src_sr: int = 48000,
    tgt_sr: int = 16000,
) -> List[np.ndarray]:
    """
    Pi에서 continuous PCM으로 오는 chunk들을
    VAD로 '말 구간' 단위로 잘라서 반환
    """
    global vad_model, vad_utils
    if vad_model is None or vad_utils is None:
        # VAD 없으면 통으로 하나 반환
        full_bytes = b"".join(pcm_chunks)
        audio_int16 = np.frombuffer(full_bytes, dtype=np.int16)
        return [audio_int16]

    get_speech_timestamps = vad_utils[0]

    full_bytes = b"".join(pcm_chunks)
    audio_int16 = np.frombuffer(full_bytes, dtype=np.int16)

    audio_16k = _resample_np(audio_int16, src_sr, tgt_sr)
    audio_16k_torch = torch.from_numpy(audio_16k)

    # ✅ silero 공식 util로 말 구간 추출 → list 반환 (여기서 item() 쓰면 안됨)
    speech_ts = get_speech_timestamps(
    audio_16k_torch,
    vad_model,
    sampling_rate=tgt_sr,
    threshold=0.5,                 # 기본(0.5)보다 낮으면 더 민감. 너무 잘게 쪼개지면 0.55~0.6도 시도
    min_speech_duration_ms=400,    # ✅ 0.4초 이하는 말로 안봄
    min_silence_duration_ms=250,   # ✅ 0.25초 이하는 끊김으로 안봄(붙여줌)
    speech_pad_ms=150              # ✅ 앞뒤 150ms 패딩
)
    merged = []
    gap_thresh = int(0.3 * tgt_sr)  # 300ms
    for ts in speech_ts:
        if not merged:
            merged.append(ts)
            continue
        prev = merged[-1]
        if ts["start"] - prev["end"] <= gap_thresh:
            prev["end"] = ts["end"]
        else:
            merged.append(ts)

    segments = []
    for ts in merged:   # ✅ 여기서 merged 사용
        start, end = ts["start"], ts["end"]
        seg_float = audio_16k[start:end].copy()
        seg_int16 = (seg_float * 32768.0).astype(np.int16)
        segments.append(seg_int16)

    return segments if segments else []

# --- 5. API 엔드포인트 ---

@app.get("/", tags=["System"], summary="서버 상태 확인")
def read_root():
    return {"status": "ALENTO Server Running", "time": get_kst_now()}

# --- [User Auth] ---
@app.post("/users/signup", response_model=schemas.User, tags=["Auth"])
def create_user(user: schemas.UserCreate, db: Session = Depends(get_db)):
    if auth.get_user(db, user.email): raise HTTPException(400, "Email exists")
    hashed = auth.get_password_hash(user.password)
    db_user = models.User(email=user.email, hashed_password=hashed, full_name=user.full_name, created_at=get_kst_now())
    db.add(db_user); db.commit(); db.refresh(db_user)
    return db_user

@app.post("/token", response_model=schemas.Token, tags=["Auth"])
async def login(form_data: Annotated[OAuth2PasswordRequestForm, Depends()], db: Session = Depends(get_db)):
    user = auth.get_user(db, form_data.username)
    if not user or not auth.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(401, "Login failed")
    return {"access_token": auth.create_access_token(data={"sub": user.email}), "token_type": "bearer"}

@app.get("/users/me", response_model=schemas.User, tags=["User"])
async def read_me(current_user: Annotated[models.User, Depends(auth.get_current_user)]):
    return current_user

@app.patch("/users/me/status", response_model=schemas.User, tags=["User"])
def update_user_status(status_update: schemas.UserStatusUpdate, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    update_data = status_update.model_dump(exclude_unset=True)
    for key, value in update_data.items(): setattr(current_user, key, value)
    db.add(current_user); db.commit(); db.refresh(current_user)
    return current_user

@app.patch("/users/me", response_model=schemas.User, tags=["User"])
def update_user_info(user_update: schemas.UserUpdate, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    update_data = user_update.model_dump(exclude_unset=True)
    for key, value in update_data.items(): setattr(current_user, key, value)
    db.add(current_user); db.commit(); db.refresh(current_user)
    return current_user

@app.post("/users/me/push-token", tags=["User"])
def save_push(
    body: dict,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db),
):
    token = body.get("token")
    if not token:
        raise HTTPException(400, "token field required")

    current_user.push_token = token
    db.commit()
    return {"detail": "OK", "token_saved": True}

# --- [Device] ---
@app.post("/devices/register", response_model=schemas.DeviceRegisterResponse, tags=["Device"])
def register_device(data: schemas.DeviceCreate, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    if db.query(models.Device).filter(models.Device.device_uid == data.device_uid).first():
        raise HTTPException(400, "Device exists")
    new_dev = models.Device(device_uid=data.device_uid, name=data.name, api_key=auth.create_api_key(), user_id=current_user.id, created_at=get_kst_now())
    db.add(new_dev); db.commit(); db.refresh(new_dev)
    return new_dev

@app.post("/devices/verify", tags=["Device"])
def verify_device(body: dict, db: Session = Depends(get_db)):
    dev = db.query(models.Device).filter(models.Device.device_uid == body.get("device_uid"), models.Device.api_key == body.get("api_key")).first()
    if not dev: raise HTTPException(401, "Invalid Device")
    return {"detail": "OK", "device_id": dev.id}

@app.get("/devices/me", response_model=List[schemas.Device], tags=["Device"])
def my_devices(current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    return db.query(models.Device).filter(models.Device.user_id == current_user.id).all()

@app.get("/devices/{device_uid}", response_model=schemas.Device, tags=["Device"])
def get_device_detail(device_uid: str, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device or device.user_id != current_user.id: raise HTTPException(403, "권한 없음")
    return device

@app.patch("/devices/{device_uid}/memo", response_model=schemas.Device, tags=["Device"])
def update_device_memo(device_uid: str, memo_data: dict, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device or device.user_id != current_user.id: raise HTTPException(403, "권한 없음")
    device.memo = memo_data.get("memo")
    db.commit(); db.refresh(device)
    return device

@app.patch("/devices/{device_uid}/name", response_model=schemas.Device, tags=["Device"])
def update_device_name(device_uid: str, body: dict, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device or device.user_id != current_user.id: raise HTTPException(403, "권한 없음")
    device.name = body.get("name")
    db.commit(); db.refresh(device)
    return device

@app.delete("/devices/{device_uid}", tags=["Device"])
def delete_device(device_uid: str, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device or device.user_id != current_user.id: raise HTTPException(403, "권한 없음")
    db.delete(device); db.commit()
    return {"detail": "삭제됨"}

# --- [Visits] ---
@app.get("/visits/", response_model=List[schemas.VisitSchema], tags=["Visit"])
def get_visits(current_user: Annotated[models.User, Depends(auth.get_current_user)], skip: int=0, limit: int=10, db: Session = Depends(get_db)):
    return db.query(models.Visit).join(models.Device).filter(models.Device.user_id == current_user.id).order_by(models.Visit.id.desc()).offset(skip).limit(limit).all()

@app.get("/visits/{visit_id}", response_model=schemas.VisitSchema, tags=["Visit"])
def visit_detail(visit_id: int, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    v = db.query(models.Visit).filter(models.Visit.id == visit_id).first()
    if not v or v.device.user_id != current_user.id: raise HTTPException(403, "No access")
    return v

@app.get("/visits/{visit_id}/transcript", response_model=schemas.VisitTranscriptResponse, tags=["Visit"])
def get_visit_transcript(visit_id: int, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    visit = db.query(models.Visit).filter(models.Visit.id == visit_id).first()
    if not visit or visit.device.user_id != current_user.id: raise HTTPException(403, "권한 없음")
    transcripts = db.query(models.Transcript).filter(models.Transcript.visit_id == visit_id).order_by(models.Transcript.created_at.asc()).all()
    return {"visit_id": visit.id, "summary": visit.summary, "created_at": visit.created_at, "transcripts": transcripts}

@app.delete("/visits/{visit_id}", tags=["Visit"])
def delete_visit(visit_id: int, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    visit = db.query(models.Visit).filter(models.Visit.id == visit_id).first()
    if not visit or visit.device.user_id != current_user.id: raise HTTPException(403, "권한 없음")
    db.delete(visit); db.commit()
    return {"detail": "삭제됨"}

# --- [Appointments] ---
@app.post("/appointments/", response_model=schemas.AppointmentSchema, tags=["Appointment"])
def create_appointment(data: schemas.AppointmentCreate, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    new_appt = models.Appointment(title=data.title, start_time=data.start_time, end_time=data.end_time, user_id=current_user.id, visit_id=None)
    db.add(new_appt); db.commit(); db.refresh(new_appt)
    return new_appt

@app.get("/appointments/", response_model=List[schemas.AppointmentSchema], tags=["Appointment"])
def get_appointments(current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    return db.query(models.Appointment).filter(models.Appointment.user_id == current_user.id).order_by(models.Appointment.start_time.desc()).all()

@app.get("/appointments/{appointment_id}", response_model=schemas.AppointmentSchema, tags=["Appointment"])
def get_appointment_detail(appointment_id: int, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    appt = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appt or appt.user_id != current_user.id: raise HTTPException(403, "권한 없음")
    return appt

@app.patch("/appointments/{appointment_id}", response_model=schemas.AppointmentSchema, tags=["Appointment"])
def update_appointment(appointment_id: int, body: dict, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    appt = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appt or appt.user_id != current_user.id: raise HTTPException(403, "권한 없음")
    for key, value in body.items(): setattr(appt, key, value)
    db.commit(); db.refresh(appt)
    return appt

@app.delete("/appointments/{appointment_id}", tags=["Appointment"])
def delete_appointment(appointment_id: int, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    appt = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appt or appt.user_id != current_user.id: raise HTTPException(403, "권한 없음")
    db.delete(appt); db.commit()
    return {"detail": "삭제됨"}

# --- [Upload] ---
@app.post("/upload", tags=["Upload"])
async def upload_file(
    file: UploadFile = File(...), 
    device_uid: str = Form(...), 
    db: Session = Depends(get_db)
):
    filename = f"{uuid.uuid4()}_{file.filename}"
    file_path = filename
    
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        folder = "videos" if filename.endswith(('.mp4', '.avi')) else "snapshots"
        
        loop = asyncio.get_event_loop()
        gcs_url = await loop.run_in_executor(
            None, upload_to_gcs, file_path, f"{folder}/{filename}"
        )
        
        if not gcs_url: return {"error": "Upload failed"}

        # DB Update
        device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
        if device:
            last_visit = db.query(models.Visit).filter(
                models.Visit.device_id == device.id
            ).order_by(models.Visit.id.desc()).first()
            
            if last_visit:
                last_visit.visitor_video_url = gcs_url
                db.commit()
                print(f"✅ Video Linked: Visit {last_visit.id}")

        return {"url": gcs_url}

    except Exception as e:
        print(f"Upload Error: {e}")
        return {"error": str(e)}
    
    finally:
        if os.path.exists(file_path): os.remove(file_path)


# --- 6. WebSocket ---
def save_frames_to_mp4(device_uid, frames):
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    filename = f"./temp/visit_{device_uid}_{timestamp()}.mp4"
    os.makedirs("./temp", exist_ok=True)

    out = None

    for jpg_bytes in frames:
        arr = np.frombuffer(jpg_bytes, np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            continue

        if out is None:
            h, w = frame.shape[:2]
            out = cv2.VideoWriter(filename, fourcc, 15, (w, h))

        out.write(frame)

    if out:
        out.release()
    return filename


def timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def save_pcm_to_wav(visit_id, chunk_list):
    filename = f"visit_{visit_id}.wav"
    filepath = f"./temp/{filename}"

    os.makedirs("./temp", exist_ok=True)

    with wave.open(filepath, "wb") as wf:
        wf.setnchannels(AUDIO_CHANNELS)
        wf.setsampwidth(2)  # int16
        wf.setframerate(AUDIO_SAMPLE_RATE)
        for c in chunk_list:
            wf.writeframes(c)

    return filepath

@app.websocket("/ws/stream/{device_uid}")
async def ws_stream(websocket: WebSocket, device_uid: str, db: Session = Depends(get_db)):
    dev = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not dev:
        await websocket.close(1008)
        return

    await video_manager.connect(dev.id, websocket)
    print(f"👀 Viewer connected: device {device_uid}")

    try:
        while True:
            # 클라이언트로부터 아무 메시지도 오지 않아도 유지됨
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        print(f"👋 Viewer disconnected: device {device_uid}")
        video_manager.disconnect(dev.id, websocket)

active_video_streams = {}

@app.websocket("/ws/broadcast/{device_uid}")
async def broadcast_socket(websocket: WebSocket, device_uid: str):
    await websocket.accept()

    if device_uid in active_video_streams:
        try:
            await active_video_streams[device_uid].close()
        except:
            pass

    active_video_streams[device_uid] = websocket
    frames = []

    print(f"🎥 Broadcast connected: {device_uid}")

    try:
        while True:
            msg = await websocket.receive()

            # ✅ disconnect 타입이면 바로 탈출
            if msg["type"] == "websocket.disconnect":
                print("🎥 Video WS disconnect msg received")
                break

            jpg = msg.get("bytes")
            if jpg:
                frames.append(jpg)

                # 실시간 뷰어 broadcast
                db = SessionLocal()
                try:
                    dev = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
                    if dev:
                        await video_manager.broadcast_to_device_viewers(dev.id, jpg)
                finally:
                    db.close()

    except WebSocketDisconnect:
        print("🎥 Video WS disconnected (exception)")

    finally:
        active_video_streams.pop(device_uid, None)
        if frames:
            mp4_path = save_frames_to_mp4(device_uid, frames)
            url = upload_to_gcs(mp4_path, f"videos/{device_uid}_{timestamp()}.mp4")
            print("📦 Video uploaded:", url)


@app.websocket("/ws/conversation/{device_uid}")
async def ws_conversation(websocket: WebSocket, device_uid: str):
    global active_conversations_ws, active_conversation_devices

    # =========================
    # 0) 중복 연결 정리
    # =========================
    if device_uid in active_conversation_devices:
        try:
            old_ws = active_conversations_ws.get(device_uid)
            if old_ws:
                await old_ws.close()
        except:
            pass

    active_conversation_devices.add(device_uid)
    active_conversations_ws[device_uid] = websocket

    await websocket.accept()
    loop = asyncio.get_event_loop()

    # =========================
    # 1) Visit 생성
    # =========================
    db = SessionLocal()
    try:
        dev = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
        if not dev:
            await websocket.close(1008)
            return

        user_id, dev_id, dev_name = dev.user_id, dev.id, dev.name

        visit = models.Visit(device_id=dev_id, summary="대화 중...", created_at=get_kst_now())
        db.add(visit); db.commit(); db.refresh(visit)
        visit_id = visit.id

        notify_user(user_id, "방문객 감지", f"{dev_name}에서 대화가 시작되었습니다.", db)
    finally:
        db.close()

    print(f"📞 Visit {visit_id} Start")

    transcript = ""

    # 서버로 들어오는 PCM chunk 모음
    pcm_buffer: List[bytes] = []

    # ✅ VAD로 잘라낸 “말 구간”만 저장할 리스트
    visitor_audio_segments_for_save: List[np.ndarray] = []

    # ✅ 원본 전체 PCM 저장 fallback 용
    all_pcm_for_save: List[bytes] = []

    # ====== VAD 윈도우/오버랩 설정 ======
    WINDOW_CHUNKS = 64        # (중요) 32는 너무 짧음. 48k/1024 기준 1.36초
    OVERLAP_CHUNKS = 8

    try:
        # =========================
        # 2) AI 첫 인사
        # =========================
        greeting = "방문객: (벨소리)"
        transcript += greeting + "\n"

        db = SessionLocal()
        try:
            u = db.query(models.User).filter(models.User.id == user_id).first()
            d = db.query(models.Device).filter(models.Device.id == dev_id).first()

            ai_text = await loop.run_in_executor(
                None, lambda: get_llm_response(u, greeting, db=db, device=d)
            )

            try:
                db.add(models.Transcript(
                    visit_id=visit_id,
                    speaker="ai",
                    message=ai_text,
                    created_at=get_kst_now()
                ))
                db.commit()
            except:
                db.rollback()
        finally:
            db.close()

        transcript += f"AI: {ai_text}\n"

        tmp = f"tmp_{uuid.uuid4()}.mp3"
        await loop.run_in_executor(None, text_to_speech, ai_text, tmp)
        try:
            with open(tmp, "rb") as f:
                await websocket.send_bytes(f.read())
        except WebSocketDisconnect:
            print("⚠️ Pi disconnected before first AI audio send")
            return
        except RuntimeError:
            print("⚠️ websocket already closed")
            return
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

        # =========================
        # 3) 대화 루프
        # =========================
        while True:
            try:
                msg = await websocket.receive()
            except WebSocketDisconnect:
                print("🔌 disconnect (exception)")
                break

            # starlette가 disconnect message를 직접 주는 경우 방어
            if msg.get("type") == "websocket.disconnect":
                print("🔌 disconnect msg received")
                break

            # -------------------------
            # A) 텍스트(User->Pi)
            # -------------------------
            if "text" in msg and msg["text"] is not None:
                text = msg["text"]

                if "handshake" in text or text.strip().startswith("{"):
                    print(f"🤝 설정 메시지 무시: {text}")
                    continue
                if text == "end":
                    break

                transcript += f"User: {text}\n"

                db = SessionLocal()
                try:
                    db.add(models.Transcript(
                        visit_id=visit_id,
                        speaker="user",
                        message=text,
                        created_at=get_kst_now()
                    ))
                    db.commit()
                except:
                    db.rollback()
                finally:
                    db.close()

            # -------------------------
            # B) 음성(Pi->Server) continuous PCM
            # -------------------------
            if "bytes" in msg and msg["bytes"]:
                chunk = msg["bytes"]
                pcm_buffer.append(chunk)

                # ✅ 원본 전체 저장용에도 누적
                all_pcm_for_save.append(chunk)

                # 윈도우가 안 찼으면 계속 누적
                if len(pcm_buffer) < WINDOW_CHUNKS:
                    continue

                # 윈도우가 찼을 때만 VAD/STT
                segments = vad_split_segments(pcm_buffer, src_sr=48000, tgt_sr=16000)

                # tail(overlap)만 남기기
                pcm_buffer = pcm_buffer[-OVERLAP_CHUNKS:]

                for seg_int16 in segments:
                    # 너무 짧은 말은 skip
                    if len(seg_int16) < 1600:
                        continue

                    visitor_audio_segments_for_save.append(seg_int16)

                    tmp_in = f"in_{uuid.uuid4()}.wav"
                    sf.write(tmp_in, seg_int16, 16000)

                    stt_text = await loop.run_in_executor(
                        None, lambda: stt_pipe(tmp_in)["text"]
                    )
                    if os.path.exists(tmp_in):
                        os.remove(tmp_in)

                    if not stt_text.strip():
                        continue

                    print(f"🗣️ STT: {stt_text}")
                    transcript += f"Visitor: {stt_text}\n"

                    db = SessionLocal()
                    try:
                        u = db.query(models.User).filter(models.User.id == user_id).first()
                        d = db.query(models.Device).filter(models.Device.id == dev_id).first()

                        db.add(models.Transcript(
                            visit_id=visit_id,
                            speaker="visitor",
                            message=stt_text,
                            created_at=get_kst_now()
                        ))
                        db.commit()

                        ai_text = await loop.run_in_executor(
                            None, lambda: get_llm_response(u, transcript, db=db, device=d)
                        )

                        db.add(models.Transcript(
                            visit_id=visit_id,
                            speaker="ai",
                            message=ai_text,
                            created_at=get_kst_now()
                        ))
                        db.commit()
                    except:
                        db.rollback()
                    finally:
                        db.close()

                    transcript += f"AI: {ai_text}\n"

                    tmp = f"tmp_{uuid.uuid4()}.mp3"
                    await loop.run_in_executor(None, text_to_speech, ai_text, tmp)
                    try:
                        with open(tmp, "rb") as f:
                            await websocket.send_bytes(f.read())
                    except WebSocketDisconnect:
                        print("⚠️ Pi disconnected before AI audio send")
                        break
                    except RuntimeError:
                        print("⚠️ websocket already closed")
                        break
                    finally:
                        if os.path.exists(tmp):
                            os.remove(tmp)

    except Exception as e:
        print(f"Conversation Error: {e}")

    finally:
        print(f"💾 Visit {visit_id} End. Saving Audio...")

        gcs_url_vad = None
        gcs_url_raw = None

        # =========================
        # 4) VAD 기반 저장(말 구간만)
        # =========================
        if visitor_audio_segments_for_save:
            final_vad_wav = f"visit_{visit_id}_vad.wav"
            try:
                full_audio = np.concatenate(visitor_audio_segments_for_save)
                sf.write(final_vad_wav, full_audio, 16000)

                gcs_url_vad = await loop.run_in_executor(
                    None, upload_to_gcs, final_vad_wav, f"audio/visit_{visit_id}_vad.wav"
                )
                print("✅ VAD wav uploaded:", gcs_url_vad)
            except Exception as e:
                print("VAD Audio Save Error:", e)
            finally:
                if os.path.exists(final_vad_wav):
                    os.remove(final_vad_wav)

        # =========================
        # 5) ✅ 원본 전체 PCM 저장(fallback)
        # =========================
        if all_pcm_for_save:
            final_raw_wav = f"visit_{visit_id}_raw48k.wav"
            try:
                raw_pcm = b"".join(all_pcm_for_save)
                audio_int16 = np.frombuffer(raw_pcm, dtype=np.int16)
                sf.write(final_raw_wav, audio_int16, 48000)

                gcs_url_raw = await loop.run_in_executor(
                    None, upload_to_gcs, final_raw_wav, f"audio/visit_{visit_id}_raw48k.wav"
                )
                print("✅ RAW wav uploaded:", gcs_url_raw)
            except Exception as e:
                print("RAW Audio Save Error:", e)
            finally:
                if os.path.exists(final_raw_wav):
                    os.remove(final_raw_wav)

        # =========================
        # 6) Visit 요약/URL 저장
        # =========================
        post_data = get_ai_post_processing(transcript)

        db = SessionLocal()
        try:
            v = db.query(models.Visit).filter(models.Visit.id == visit_id).first()
            if v:
                v.summary = post_data.get("summary", "요약 실패")

                # 우선순위: VAD > RAW
                v.visitor_audio_url = gcs_url_vad or gcs_url_raw
                db.commit()

                notify_user(user_id, "대화 종료", f"요약: {v.summary}", db)
        finally:
            db.close()

        active_conversation_devices.discard(device_uid)
        active_conversations_ws.pop(device_uid, None)

        print("✅ Done.")



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)