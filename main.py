import os
import shutil
import uuid
import json
import datetime
import asyncio
from typing import List, Annotated

from fastapi import (
    FastAPI, UploadFile, File, Form, HTTPException, Depends, status,
    WebSocket, WebSocketDisconnect
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordRequestForm
from starlette.background import BackgroundTask
from dotenv import load_dotenv
import torch
from transformers import pipeline
import google.generativeai as genai
from gtts import gTTS
from sqlalchemy.orm import Session
from google.cloud import storage

import firebase_admin
from firebase_admin import credentials, messaging

# 로컬 모듈 import
import models
import schemas
import auth
from database import SessionLocal, engine, get_db

# --- 1. 초기 설정: DB 테이블 생성, 환경 변수 및 앱 생성 ---
models.Base.metadata.create_all(bind=engine)
load_dotenv()
app = FastAPI(title="Smart Doorbell AI Server")

# 전역 변수 (서버 시작 시 한 번만 로드)
storage_client = None
bucket = None
stt_pipe = None
llm_model = None
system_instruction = ""

origins = [
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- 2. 헬퍼 함수 및 클래스 ---

def notify_user(user_id: int, title: str, body: str, db):
    user = db.query(models.User).filter(models.User.id == user_id).first()

    if not user or not user.push_token:
        print("푸시 알림 스킵 — 토큰 없음")
        return

    message = messaging.Message(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        token=user.push_token,
    )

    try:
        messaging.send(message)
        print("FCM 푸시 전송 완료")
    except Exception as e:
        print("FCM 전송 실패:", e)


# ★★★ 실시간 영상 WebSocket 연결 관리자 (수정됨) ★★★
class VideoConnectionManager:
    def __init__(self):
        # {device_id: [연결된_앱_WebSocket, ...]}
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
            except ValueError:
                pass

    async def broadcast_to_device_viewers(self, device_id: int, data: bytes):
        """
        [최적화] IndexError 방지 및 병렬 전송 적용
        """
        if device_id not in self.active_connections:
            return

        # [중요] 리스트가 전송 도중 변경되지 않도록 복사본(snapshot) 사용
        connections = list(self.active_connections[device_id])
        if not connections:
            return

        # [중요] asyncio.gather를 사용하여 모든 앱에게 동시에 전송 (딜레이 최소화)
        tasks = [connection.send_bytes(data) for connection in connections]
        
        # 에러가 나도 다른 클라이언트는 영향받지 않도록 return_exceptions=True
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 전송 실패한 연결 정리
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                dead_socket = connections[i]
                self.disconnect(device_id, dead_socket)

video_manager = VideoConnectionManager()


# --- 3. FastAPI 시작 이벤트 ---
@app.on_event("startup")
def startup_event():
    """서버가 시작될 때 무거운 모델들을 로드합니다."""
    global storage_client, bucket, stt_pipe, llm_model, system_instruction

    # Firebase 초기화
    try:
        cred = credentials.Certificate(os.getenv("FIREBASE_ADMIN_KEY", "firebase_admin_key.json"))
        firebase_admin.initialize_app(cred)
        print("Firebase Admin SDK 초기화 완료")
    except Exception as e:
        print(f"Firebase 초기화 경고 (이미 초기화됨?): {e}")

    # GCS 클라이언트 설정
    print("Google Cloud Storage 클라이언트를 초기화합니다...")
    try:
        storage_client = storage.Client()
        GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
        if not GCS_BUCKET_NAME:
            print("주의: GCS_BUCKET_NAME이 설정되지 않았습니다. 파일 업로드가 불가능합니다.")
        else:
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            print("GCS 클라이언트 초기화 완료.")
    except Exception as e:
        print(f"GCS 클라이언트 초기화 실패: {e}")

    # STT (Whisper) 모델 로드
    print("Whisper 모델을 로드합니다...")
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    stt_pipe = pipeline("automatic-speech-recognition", model="openai/whisper-small", device=device)
    print("Whisper 모델 로드 완료.")

    # LLM (Gemini) 모델 설정
    print("Gemini 모델을 설정합니다...")
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    genai.configure(api_key=GOOGLE_API_KEY)
    llm_model = genai.GenerativeModel('gemini-2.5-flash')
    print("Gemini 모델 설정 완료.")

    # AI 역할 정의
    system_instruction = """
    당신은 스마트 초인종의 AI 비서입니다. 
    당신의 임무는 부재중인 집주인을 대신하여 방문객을 응대하는 것입니다.
    항상 침착하고 친절한 말투를 유지하세요. 
    """


# --- 4. 유틸리티 함수 ---

def upload_to_gcs(file_path: str, destination_blob_name: str) -> str:
    """로컬 파일을 GCS에 업로드하고 공개 URL을 반환합니다."""
    if not bucket:
        print("❌ GCS Bucket 미설정: 업로드 건너뜀")
        return None
    try:
        blob = bucket.blob(destination_blob_name)
        blob.upload_from_filename(file_path)
        # blob.make_public() # 버킷 설정에 따라 필요시 주석 해제
        print(f"GCS 업로드 성공: {destination_blob_name}")
        return blob.public_url
    except Exception as e:
        print(f"❌ GCS 업로드 실패: {e}")
        return None

def text_to_speech(text: str, filename: str) -> str:
    """텍스트를 음성 파일로 변환하고 파일 경로를 반환합니다."""
    tts = gTTS(text=text, lang='ko')
    tts.save(filename)
    return filename

def get_llm_response(current_user: models.User, full_transcript: str, db: Session, device: models.Device = None) -> str:
    global llm_model, system_instruction

    # 1) 유저 상태
    user_status_from_db = {
        "name": current_user.full_name,
        "is_home": current_user.is_home,
        "return_time": current_user.return_time,
        "memo": current_user.memo
    }

    # 2) 기기 정보
    device_info = None
    if device is not None:
        device_info = {
            "device_name": device.name,
            "device_memo": device.memo
        }

    # 3) 일정 정보
    appointments = db.query(models.Appointment).filter(
        models.Appointment.user_id == current_user.id
    ).order_by(models.Appointment.start_time.asc()).all()

    appointment_list = [
        f"{a.title} ({a.start_time.strftime('%Y-%m-%d %H:%M')})"
        for a in appointments
    ]

    full_prompt = f"""
    {system_instruction}

    # 집주인 정보: {user_status_from_db}
    # 일정 목록: {appointment_list}
    # 기기 정보: {device_info}
    # 대화 내용:
    {full_transcript}

    # AI 응답:
    """
    try:
        response = llm_model.generate_content(full_prompt)
        return response.text
    except Exception as e:
        print(f"LLM Error: {e}")
        return "죄송합니다. 잠시 문제가 발생했습니다."

def get_ai_post_processing(transcript_text: str) -> dict:
    global llm_model
    post_processing_prompt = f"""
    아래 대화를 요약하고, 약속(일정)이 잡혔는지 JSON으로 반환하세요.
    Keys: "summary", "appointment" (null 또는 {{"title", "start_time", "end_time"}})

    [대화 내용]
    {transcript_text}
    """
    try:
        response = llm_model.generate_content(post_processing_prompt)
        json_text = response.text.strip().replace("```json", "").replace("```", "")
        data = json.loads(json_text)
        return data
    except Exception as e:
        print(f"AI 후처리 실패: {e}")
        return {"summary": "요약 실패", "appointment": None}


# --- 5. HTTP API 엔드포인트 ---

@app.get("/", summary="서버 상태 확인")
def read_root():
    return {"status": "띵동 AI 서버가 정상 작동 중입니다."}

# --- 사용자 인증 ---
@app.post("/users/signup", response_model=schemas.User, summary="회원가입")
def create_user(user: schemas.UserCreate, db: Session = Depends(get_db)):
    db_user = auth.get_user(db, email=user.email)
    if db_user:
        raise HTTPException(status_code=400, detail="이미 등록된 이메일입니다.")
    hashed_password = auth.get_password_hash(user.password)
    db_user = models.User(email=user.email, hashed_password=hashed_password, full_name=user.full_name)
    db.add(db_user); db.commit(); db.refresh(db_user)
    return db_user

@app.post("/token", response_model=schemas.Token, summary="로그인")
async def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()], db: Session = Depends(get_db)
):
    user = auth.get_user(db, email=form_data.username)
    if not user or not auth.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="로그인 실패")
    access_token = auth.create_access_token(data={"sub": user.email})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/users/me", response_model=schemas.User, summary="내 정보 조회")
async def read_users_me(current_user: Annotated[models.User, Depends(auth.get_current_user)]):
    return current_user

@app.patch("/users/me/status", response_model=schemas.User, summary="내 상태 업데이트")
def update_user_status(
    status_update: schemas.UserStatusUpdate,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    update_data = status_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(current_user, key, value)
    db.add(current_user); db.commit(); db.refresh(current_user)
    return current_user

@app.patch("/users/me", response_model=schemas.User, summary="내 정보 수정")
def update_user_info(
    user_update: schemas.UserUpdate,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    update_data = user_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(current_user, key, value)
    db.add(current_user); db.commit(); db.refresh(current_user)
    return current_user

@app.post("/users/me/push-token")
def save_push_token(
    body: dict,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    token = body.get("token")
    if not token:
        raise HTTPException(400, "token 필드 필요")
    current_user.push_token = token
    db.commit()
    return {"detail": "토큰 저장 완료"}


# --- 기기 관리 ---
@app.post("/devices/register", response_model=schemas.DeviceRegisterResponse)
def register_device(
    device_data: schemas.DeviceCreate,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    if db.query(models.Device).filter(models.Device.device_uid == device_data.device_uid).first():
        raise HTTPException(400, "이미 등록된 기기")
    
    new_api_key = auth.create_api_key()
    db_device = models.Device(
        device_uid=device_data.device_uid,
        name=device_data.name,
        api_key=new_api_key,
        user_id=current_user.id
    )
    db.add(db_device); db.commit(); db.refresh(db_device)
    return db_device

@app.post("/devices/verify")
def verify_device(body: dict, db: Session = Depends(get_db)):
    device = db.query(models.Device).filter(
        models.Device.device_uid == body.get("device_uid"),
        models.Device.api_key == body.get("api_key")
    ).first()
    if not device:
        raise HTTPException(401, "기기 인증 실패")
    return {"detail": "성공", "device_id": device.id}

@app.get("/devices/me", response_model=List[schemas.Device])
def get_my_devices(current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    return db.query(models.Device).filter(models.Device.user_id == current_user.id).all()

@app.get("/devices/{device_uid}", response_model=schemas.Device)
def get_device_detail(device_uid: str, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device or device.user_id != current_user.id:
        raise HTTPException(403, "권한 없음")
    return device

@app.patch("/devices/{device_uid}/memo", response_model=schemas.Device)
def update_device_memo(device_uid: str, memo_data: dict, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device or device.user_id != current_user.id:
        raise HTTPException(403, "권한 없음")
    device.memo = memo_data.get("memo")
    db.commit(); db.refresh(device)
    return device

@app.patch("/devices/{device_uid}/name", response_model=schemas.Device)
def update_device_name(device_uid: str, body: dict, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device or device.user_id != current_user.id:
        raise HTTPException(403, "권한 없음")
    device.name = body.get("name")
    db.commit(); db.refresh(device)
    return device

@app.delete("/devices/{device_uid}")
def delete_device(device_uid: str, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device or device.user_id != current_user.id:
        raise HTTPException(403, "권한 없음")
    db.delete(device); db.commit()
    return {"detail": "삭제됨"}


# --- 방문 기록 및 일정 ---

@app.get("/visits/", response_model=List[schemas.VisitSchema])
def get_visits(
    current_user: Annotated[models.User, Depends(auth.get_current_user)], 
    skip: int = 0, limit: int = 10, db: Session = Depends(get_db)
):
    return db.query(models.Visit).join(models.Device).filter(
        models.Device.user_id == current_user.id
    ).order_by(models.Visit.id.desc()).offset(skip).limit(limit).all()

@app.get("/visits/{visit_id}", response_model=schemas.VisitSchema)
def get_visit_detail(visit_id: int, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    visit = db.query(models.Visit).filter(models.Visit.id == visit_id).first()
    if not visit or visit.device.user_id != current_user.id:
        raise HTTPException(403, "권한 없음")
    return visit

@app.get("/visits/{visit_id}/transcript", response_model=schemas.VisitTranscriptResponse)
def get_visit_transcript(visit_id: int, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    visit = db.query(models.Visit).filter(models.Visit.id == visit_id).first()
    if not visit or visit.device.user_id != current_user.id:
        raise HTTPException(403, "권한 없음")
    
    transcripts = db.query(models.Transcript).filter(models.Transcript.visit_id == visit_id).order_by(models.Transcript.created_at.asc()).all()
    
    return {
        "visit_id": visit.id,
        "summary": visit.summary,
        "created_at": visit.created_at,
        "transcripts": transcripts,
    }

@app.delete("/visits/{visit_id}")
def delete_visit(visit_id: int, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    visit = db.query(models.Visit).filter(models.Visit.id == visit_id).first()
    if not visit or visit.device.user_id != current_user.id:
        raise HTTPException(403, "권한 없음")
    db.delete(visit); db.commit()
    return {"detail": "삭제됨"}

@app.post("/appointments/", response_model=schemas.AppointmentSchema)
def create_appointment(data: schemas.AppointmentCreate, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    new_appt = models.Appointment(
        title=data.title, start_time=data.start_time, end_time=data.end_time, 
        user_id=current_user.id, visit_id=None
    )
    db.add(new_appt); db.commit(); db.refresh(new_appt)
    return new_appt

@app.get("/appointments/", response_model=List[schemas.AppointmentSchema])
def get_appointments(current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    return db.query(models.Appointment).filter(models.Appointment.user_id == current_user.id).order_by(models.Appointment.start_time.desc()).all()

@app.get("/appointments/{appointment_id}", response_model=schemas.AppointmentSchema)
def get_appointment_detail(appointment_id: int, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    appt = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appt or appt.user_id != current_user.id:
        raise HTTPException(403, "권한 없음")
    return appt

@app.patch("/appointments/{appointment_id}", response_model=schemas.AppointmentSchema)
def update_appointment(appointment_id: int, body: dict, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    appt = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appt or appt.user_id != current_user.id:
        raise HTTPException(403, "권한 없음")
    for key, value in body.items():
        setattr(appt, key, value)
    db.commit(); db.refresh(appt)
    return appt

@app.delete("/appointments/{appointment_id}")
def delete_appointment(appointment_id: int, current_user: Annotated[models.User, Depends(auth.get_current_user)], db: Session = Depends(get_db)):
    appt = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appt or appt.user_id != current_user.id:
        raise HTTPException(403, "권한 없음")
    db.delete(appt); db.commit()
    return {"detail": "삭제됨"}


# --- 6. WebSocket API (수정된 핵심 기능 포함) ---

# 6a. 실시간 영상 스트리밍 (시청자용)
@app.websocket("/ws/stream/{device_uid}")
async def websocket_stream(websocket: WebSocket, device_uid: str, db: Session = Depends(get_db)):
    """
    [수정됨] 앱이 UID로 접속해도 서버가 내부 ID를 찾아 연결해 줍니다.
    """
    # 1. UID로 기기 검색
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    
    if not device:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid device UID")
        return

    # 2. 내부 ID로 매니저 연결
    await video_manager.connect(device.id, websocket)
    print(f"👀 시청자 접속: {device_uid} (ID: {device.id})")
    
    try:
        while True:
            await websocket.receive_text() # 연결 유지용 대기
    except WebSocketDisconnect:
        video_manager.disconnect(device.id, websocket)
        print(f"👋 시청자 퇴장: {device_uid}")


# 6b. 실시간 영상 브로드캐스트 (라즈베리파이용)
@app.websocket("/ws/broadcast/{device_uid}")
async def websocket_broadcast(websocket: WebSocket, device_uid: str, db: Session = Depends(get_db)):
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    
    await websocket.accept()
    print(f"📷 기기 영상 송출 시작: {device.name} (ID: {device.id})")
    
    try:
        while True:
            video_data = await websocket.receive_bytes()
            # [수정됨] 병렬 전송(asyncio.gather) 사용
            await video_manager.broadcast_to_device_viewers(device.id, video_data)
    except WebSocketDisconnect:
        print(f"📷 기기 영상 송출 중단: {device_uid}")


# 6c. 실시간 대화 및 녹음 (핵심 기능)
# main.py 의 websocket_conversation 함수 전체 교체

@app.websocket("/ws/conversation/{device_uid}")
async def websocket_conversation(websocket: WebSocket, device_uid: str):
    await websocket.accept()
    loop = asyncio.get_event_loop()
    
    # [1] 초기화: DB를 열고 -> 기기 찾고 -> 바로 닫음 (Session 유지 X)
    db = SessionLocal()
    try:
        device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
        if not device:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        
        # 나중에 쓸 데이터만 변수에 백업
        device_id = device.id
        user_id = device.user_id
        device_name = device.name
        
        visit = models.Visit(device_id=device_id, summary="대화 중...")
        db.add(visit); db.commit(); db.refresh(visit)
        visit_id = visit.id
        
        notify_user(user_id, "방문자 감지", f"{device_name} 대화 시작", db)
    except Exception as e:
        print(f"❌ 초기화 에러: {e}")
        await websocket.close()
        return
    finally:
        db.close() # ★ 중요: 여기서 DB 연결 반납!

    print(f"📞 대화 시작 (Visit ID: {visit_id})")
    
    # 파일 등 로컬 자원 준비
    conversation_audio_filename = f"visit_{visit_id}_audio.mp3"
    conversation_audio_file = open(conversation_audio_filename, "wb")
    transcript_log = ""

    try:
        # [2] AI 첫 인사
        greeting = "방문객: (벨소리)"
        transcript_log += greeting + "\n"
        
        # DB가 필요한 작업(LLM)을 위해 '잠깐' 열기
        db = SessionLocal()
        try:
            current_user = db.query(models.User).filter(models.User.id == user_id).first()
            current_device = db.query(models.Device).filter(models.Device.id == device_id).first()
            
            ai_reply = await loop.run_in_executor(
                None, lambda: get_llm_response(current_user, greeting, db=db, device=current_device)
            )
            
            db.add(models.Transcript(visit_id=visit_id, speaker="ai", message=ai_reply))
            db.commit()
        finally:
            db.close() # ★ 사용 직후 바로 반납

        transcript_log += f"AI: {ai_reply}\n"
        
        # TTS (DB 필요 없음)
        temp_audio = f"temp_{uuid.uuid4()}.mp3"
        await loop.run_in_executor(None, text_to_speech, ai_reply, temp_audio)
        with open(temp_audio, "rb") as f:
            b = f.read()
            await websocket.send_bytes(b)
            conversation_audio_file.write(b)
        if os.path.exists(temp_audio): os.remove(temp_audio)

        # [3] 대화 루프
        while True:
            # ★ 대기 중에는 DB 연결이 없어야 함 (Connection 0개)
            incoming = await websocket.receive()

            # A. 앱 텍스트
            if "text" in incoming:
                user_text = incoming["text"]
                if user_text == "end": break
                
                print(f"💬 User: {user_text}")
                transcript_log += f"User: {user_text}\n"
                
                # DB 저장 (잠깐 열고 닫기)
                db = SessionLocal()
                try:
                    db.add(models.Transcript(visit_id=visit_id, speaker="user", message=user_text))
                    db.commit()
                finally:
                    db.close()

                # TTS (No DB)
                tmp_user = f"tmp_{uuid.uuid4()}.mp3"
                await loop.run_in_executor(None, text_to_speech, user_text, tmp_user)
                with open(tmp_user, "rb") as f:
                    b = f.read()
                    await websocket.send_bytes(b)
                    conversation_audio_file.write(b)
                if os.path.exists(tmp_user): os.remove(tmp_user)

            # B. 라즈베리파이 음성
            if "bytes" in incoming:
                visitor_audio = incoming["bytes"]
                print(f"🎤 방문자: {len(visitor_audio)} bytes")
                conversation_audio_file.write(visitor_audio)

                tmp_voice = f"raw_{uuid.uuid4()}.mp3"
                with open(tmp_voice, "wb") as f: f.write(visitor_audio)
                visitor_text = await loop.run_in_executor(None, lambda: stt_pipe(tmp_voice)["text"])
                if os.path.exists(tmp_voice): os.remove(tmp_voice)
                
                print(f"🗣️ 인식: {visitor_text}")
                transcript_log += f"Visitor: {visitor_text}\n"
                
                # AI 응답 (DB 필요 - 잠깐 열기)
                db = SessionLocal()
                try:
                    current_user = db.query(models.User).filter(models.User.id == user_id).first()
                    current_device = db.query(models.Device).filter(models.Device.id == device_id).first()
                    
                    db.add(models.Transcript(visit_id=visit_id, speaker="visitor", message=visitor_text))
                    
                    ai_reply = await loop.run_in_executor(
                        None, lambda: get_llm_response(current_user, transcript_log, db=db, device=current_device)
                    )
                    
                    db.add(models.Transcript(visit_id=visit_id, speaker="ai", message=ai_reply))
                    db.commit()
                finally:
                    db.close() # ★ 바로 반납
                
                transcript_log += f"AI: {ai_reply}\n"
                print(f"🤖 AI: {ai_reply}")
                
                # TTS (No DB)
                tmp_ai = f"ai_{uuid.uuid4()}.mp3"
                await loop.run_in_executor(None, text_to_speech, ai_reply, tmp_ai)
                with open(tmp_ai, "rb") as f:
                    b = f.read()
                    await websocket.send_bytes(b)
                    conversation_audio_file.write(b)
                if os.path.exists(tmp_ai): os.remove(tmp_ai)

    except Exception as e:
        print(f"⚠️ 대화 중 에러: {e}")
    
    finally:
        print("💾 대화 종료 처리 중...")
        conversation_audio_file.close()
        
        # GCS 업로드 (DB 없이 수행)
        gcs_url = await loop.run_in_executor(
            None, upload_to_gcs, conversation_audio_filename, f"audio/visit_{visit_id}.mp3"
        )
        if os.path.exists(conversation_audio_filename): os.remove(conversation_audio_filename)

        # 마지막 DB 업데이트 (잠깐 열고 닫기)
        post_data = get_ai_post_processing(transcript_log)
        db = SessionLocal()
        try:
            visit = db.query(models.Visit).filter(models.Visit.id == visit_id).first()
            if visit:
                visit.summary = post_data.get("summary", "요약 실패")
                visit.visitor_audio_url = gcs_url
                
                appt = post_data.get("appointment")
                if appt:
                    try:
                        new_appt = models.Appointment(
                            title=appt["title"],
                            start_time=datetime.datetime.fromisoformat(appt["start_time"]),
                            end_time=datetime.datetime.fromisoformat(appt["end_time"]) if appt.get("end_time") else None,
                            user_id=user_id, visit_id=visit_id
                        )
                        db.add(new_appt)
                    except: pass
                db.commit()
                notify_user(user_id, "대화 종료", f"요약: {visit.summary}", db)
        finally:
            db.close()
            
        print("✅ 종료 완료")

# main.py 에 추가 필수
# main.py 의 upload_file 함수 교체

@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...), 
    device_uid: str = Form(...),  # [추가] 라즈베리파이가 UID를 같이 보내줘야 함
    db: Session = Depends(get_db) # [추가] DB 연결
):
    """
    영상을 GCS에 업로드하고, 해당 기기의 '가장 최근 방문 기록'에 URL을 저장합니다.
    """
    try:
        # 1. GCS 업로드 (기존 로직)
        file_ext = file.filename.split(".")[-1] if "." in file.filename else "bin"
        folder = "videos" if file_ext in ["mp4", "avi"] else "snapshots"
        filename = f"{uuid.uuid4()}.{file_ext}"
        
        with open(filename, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        loop = asyncio.get_event_loop()
        gcs_url = await loop.run_in_executor(
            None, upload_to_gcs, filename, f"{folder}/{filename}"
        )
        
        if os.path.exists(filename): os.remove(filename)
        if not gcs_url: return {"error": "GCS upload failed", "url": None}

        # 2. [핵심 추가] DB에 URL 업데이트
        # 해당 UID를 가진 기기를 찾음
        device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
        if device:
            # 그 기기의 '가장 최근' 방문 기록을 찾음
            last_visit = db.query(models.Visit).filter(
                models.Visit.device_id == device.id
            ).order_by(models.Visit.id.desc()).first()
            
            if last_visit:
                last_visit.visitor_video_url = gcs_url
                db.commit()
                print(f"✅ DB 업데이트 완료 (Visit ID: {last_visit.id}) -> {gcs_url}")
            else:
                print("⚠️ 방문 기록이 없어서 영상 URL을 DB에 넣지 못했습니다.")
        else:
            print(f"⚠️ 알 수 없는 기기 UID: {device_uid}")

        return {"url": gcs_url}

    except Exception as e:
        print(f"❌ 업로드 에러: {e}")
        raise HTTPException(status_code=500, detail=str(e))
# --- 서버 실행 ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)