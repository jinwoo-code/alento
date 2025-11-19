import os
import shutil
import uuid
import json
import datetime
import asyncio  # ★ 추가: 비동기 처리를 위해 필요
from typing import List, Annotated
from fastapi import (
    FastAPI, UploadFile, File, HTTPException, Depends, status,
    WebSocket, WebSocketDisconnect
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from fastapi.security import OAuth2PasswordRequestForm
from dotenv import load_dotenv
import torch
from transformers import pipeline
import google.generativeai as genai
from gtts import gTTS
from sqlalchemy.orm import Session
from google.cloud import storage

from typing import Annotated

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

# ★★★ 실시간 영상 WebSocket 연결 관리자 ★★★
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
            except ValueError:
                pass

    # ★★★ 여기가 핵심 수정 부분입니다 ★★★
    async def broadcast_to_device_viewers(self, device_id: int, data: bytes):
        """
        순차 전송(for loop) 대신 병렬 전송(asyncio.gather)을 사용하여
        한 클라이언트의 지연이 다른 클라이언트에게 영향을 주지 않도록 합니다.
        """
        if device_id not in self.active_connections:
            return

        connections = self.active_connections[device_id]
        if not connections:
            return

        # 모든 연결에 대해 전송 작업을 동시에 생성
        tasks = [connection.send_bytes(data) for connection in connections]
        
        # 동시에 실행하고 결과 대기 (return_exceptions=True로 에러가 나도 멈추지 않게 함)
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 전송 실패한 연결(연결 끊김 등) 정리
        dead_connections = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                dead_connections.append(connections[i])
        
        for dead in dead_connections:
            self.disconnect(device_id, dead)

video_manager = VideoConnectionManager()


# --- 2. FastAPI 시작 이벤트 ---
@app.on_event("startup")
def startup_event():
    """서버가 시작될 때 무거운 모델들을 로드합니다."""
    global storage_client, bucket, stt_pipe, llm_model, system_instruction

    # 주의: 실제 배포 시에는 json 파일을 코드에 포함하지 말고 환경변수나 보안 저장소를 사용하세요.
    cred = credentials.Certificate(os.getenv("FIREBASE_ADMIN_KEY", "firebase_admin_key.json"))
    firebase_admin.initialize_app(cred)
    print("Firebase Admin SDK 초기화 완료")

    # GCS 클라이언트 설정
    print("Google Cloud Storage 클라이언트를 초기화합니다...")
    try:
        storage_client = storage.Client()
        GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
        if not GCS_BUCKET_NAME:
            # 로컬 테스트 등을 위해 예외를 띄우지 않고 경고만 출력할 수도 있음
            print("주의: GCS_BUCKET_NAME이 설정되지 않았습니다.")
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

    # AI 역할(시스템 프롬프트) 정의
    system_instruction = """
    당신은 스마트 초인종의 AI 비서입니다. 
    당신의 임무는 부재중인 집주인을 대신하여 방문객을 응대하는 것입니다.
    항상 침착하고 친절한 말투를 유지하세요. 
    방문객의 용무를 명확히 파악하고, 프롬프트로 전달되는 '집주인 현재 정보'와 '이전 대화 내용'을 참고하여 상황에 맞는 최적의 안내를 제공해야 합니다.
    """


# --- 3. 헬퍼 함수 ---

def upload_to_gcs(file_path: str, destination_blob_name: str) -> str:
    """로컬 파일을 GCS에 업로드하고 공개 URL을 반환합니다."""
    if not bucket: raise Exception("GCS Bucket이 초기화되지 않았습니다.")
    blob = bucket.blob(destination_blob_name)
    blob.upload_from_filename(file_path)
    return blob.public_url

def text_to_speech(text: str, filename: str) -> str:
    """텍스트를 음성 파일로 변환하고 파일 경로를 반환합니다."""
    tts = gTTS(text=text, lang='ko')
    tts.save(filename)
    return filename

# ★ 수정: db 세션을 인자로 받도록 변경 (불필요한 세션 생성 방지)
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

    # 3) 🔥 사용자 일정 불러오기 (전달받은 db 세션 사용)
    appointments = db.query(models.Appointment).filter(
        models.Appointment.user_id == current_user.id
    ).order_by(models.Appointment.start_time.asc()).all()

    appointment_list = [
        {
            "title": a.title,
            "start_time": a.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": a.end_time.strftime("%Y-%m-%d %H:%M:%S") if a.end_time else None
        }
        for a in appointments
    ]
    
    # 4) 🔥 일정 정보 포함한 전체 프롬프트 구성
    full_prompt = f"""
    {system_instruction}

    # 집주인 현재 정보:
    {user_status_from_db}

    # 집주인의 예정된 일정 목록 (AI가 참고해야 함):
    {appointment_list}

    # 현재 초인종 기기 정보:
    {device_info}

    # 지금까지의 전체 대화 내용:
    {full_transcript}

    # 방문객에게 할 AI의 응답 (간결하고 상황에 맞게):
    """

    response = llm_model.generate_content(full_prompt)
    return response.text


def get_ai_post_processing(transcript_text: str) -> dict:
    """대화 내용을 바탕으로 요약 및 일정 추출을 요청합니다."""
    global llm_model
    post_processing_prompt = f"""
    아래는 스마트 초인종 AI와 방문객 간의 대화 내용 전문입니다.
    이 대화 내용을 바탕으로, 다음 두 가지 작업을 수행하고, 결과를 반드시 JSON 형식으로 반환해주세요.

    1. "summary": 대화 내용을 한 문장으로 간결하게 요약합니다.
    2. "appointment": 대화에서 구체적인 날짜와 시간이 포함된 약속이 잡혔는지 분석합니다.
        - 만약 약속이 잡혔다면: 'title', 'start_time' (YYYY-MM-DD HH:MM:SS 형식)을 포함한 객체를 생성합니다.
        - 만약 'A시부터 B시 사이'라고 했다면, 'start_time'과 'end_time'을 모두 생성합니다.
        - 만약 약속이 잡히지 않았다면: 이 값은 null 이어야 합니다.

    [대화 내용]
    {transcript_text}

    [JSON 출력]
    """
    try:
        response = llm_model.generate_content(post_processing_prompt)
        json_text = response.text.strip().replace("```json", "").replace("```", "")
        data = json.loads(json_text)
        return data
    except Exception as e:
        print(f"AI 후처리 실패: {e}")
        return {"summary": "대화 요약 생성에 실패했습니다.", "appointment": None}


# --- 4. HTTP API 엔드포인트 ---

@app.get("/", summary="서버 상태 확인")
def read_root():
    return {"status": "띵동 AI 서버가 정상 작동 중입니다."}

# --- 4a. 사용자 인증 API ---
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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="이메일 또는 비밀번호가 잘못되었습니다.")
    access_token = auth.create_access_token(data={"sub": user.email})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/users/me", response_model=schemas.User, summary="내 정보 조회 (인증 필요)")
async def read_users_me(
    current_user: Annotated[models.User, Depends(auth.get_current_user)]
):
    return current_user

@app.patch("/users/me/status", response_model=schemas.User, summary="내 상태 업데이트 (인증 필요)")
def update_user_status(
    status_update: schemas.UserStatusUpdate,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    update_data = status_update.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="업데이트할 내용이 없습니다.")
    for key, value in update_data.items():
        setattr(current_user, key, value)
    db.add(current_user); db.commit(); db.refresh(current_user)
    return current_user

@app.patch("/users/me", response_model=schemas.User, summary="내 기본 정보 수정 (인증 필요)")
def update_user_info(
    user_update: schemas.UserUpdate,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    update_data = user_update.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="업데이트할 내용이 없습니다.")
    for key, value in update_data.items():
        setattr(current_user, key, value)
    db.add(current_user); db.commit(); db.refresh(current_user)
    return current_user


# --- 4b. 기기 관리 API ---
@app.post("/devices/register", response_model=schemas.DeviceRegisterResponse, summary="새 기기 등록 (인증 필요)")
def register_device(
    device_data: schemas.DeviceCreate,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    existing_device = db.query(models.Device).filter(models.Device.device_uid == device_data.device_uid).first()
    if existing_device:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="이 기기는 이미 다른 계정에 등록되었습니다.")
    new_api_key = auth.create_api_key()
    db_device = models.Device(
        device_uid=device_data.device_uid,
        name=device_data.name,
        api_key=new_api_key,
        user_id=current_user.id
    )
    db.add(db_device); db.commit(); db.refresh(db_device)
    return db_device

@app.post("/devices/verify", summary="기기 API Key 인증")
def verify_device(body: dict, db: Session = Depends(get_db)):
    device_uid = body.get("device_uid")
    api_key = body.get("api_key")
    if not device_uid or not api_key:
        raise HTTPException(status_code=400, detail="device_uid와 api_key가 필요합니다.")
    device = db.query(models.Device).filter(
        models.Device.device_uid == device_uid,
        models.Device.api_key == api_key
    ).first()
    if not device:
        raise HTTPException(status_code=401, detail="기기 인증 실패")
    return {"detail": "기기 인증 성공", "device_id": device.id}

@app.get("/devices/{device_uid}/visits", response_model=List[schemas.VisitSchema])
def get_visits_by_device(
    device_uid: str,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device:
        raise HTTPException(404, "해당 기기를 찾을 수 없습니다.")
    if device.user_id != current_user.id:
        raise HTTPException(403, "이 기기 방문 기록을 볼 권한이 없습니다.")
    visits = db.query(models.Visit).filter(
        models.Visit.device_id == device.id
    ).order_by(models.Visit.id.desc()).all()
    return visits

@app.get("/devices/me", response_model=List[schemas.Device], summary="내가 등록한 모든 기기 조회")
def get_my_devices(
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    devices = db.query(models.Device).filter(
        models.Device.user_id == current_user.id
    ).all()
    return devices

@app.patch("/devices/{device_uid}/memo", response_model=schemas.Device, summary="기기 메모 수정")
def update_device_memo(
    device_uid: str,
    memo_data: dict,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="해당 기기를 찾을 수 없습니다.")
    if device.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="이 기기를 수정할 권한이 없습니다.")
    new_memo = memo_data.get("memo")
    device.memo = new_memo
    db.commit(); db.refresh(device)
    return device

@app.get("/devices/{device_uid}", response_model=schemas.Device, summary="특정 기기 상세 조회")
def get_device_detail(
    device_uid: str,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device:
        raise HTTPException(status_code=404, detail="해당 기기를 찾을 수 없습니다.")
    if device.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="이 기기를 조회할 권한이 없습니다.")
    return device

@app.patch("/devices/{device_uid}/name", response_model=schemas.Device, summary="기기 이름 수정")
def update_device_name(
    device_uid: str,
    body: dict,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    new_name = body.get("name")
    if not new_name:
        raise HTTPException(status_code=400, detail="name 값이 필요합니다.")
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device:
        raise HTTPException(status_code=404, detail="해당 기기를 찾을 수 없습니다.")
    if device.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="이 기기를 수정할 권한이 없습니다.")
    device.name = new_name
    db.commit(); db.refresh(device)
    return device

@app.delete("/devices/{device_uid}", summary="기기 삭제")
def delete_device(
    device_uid: str,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device:
        raise HTTPException(status_code=404, detail="해당 기기를 찾을 수 없습니다.")
    if device.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="이 기기를 삭제할 권한이 없습니다.")
    db.delete(device); db.commit()
    return {"detail": f"기기({device_uid})가 성공적으로 삭제되었습니다."}


# --- 4c. 데이터 조회 API ---
@app.get("/visits/", response_model=List[schemas.VisitSchema], summary="저장된 방문 기록 조회 (인증 필요)")
def get_visits(
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    skip: int = 0, limit: int = 10, db: Session = Depends(get_db)
):
    visits = db.query(models.Visit).join(models.Device).filter(
        models.Device.user_id == current_user.id
    ).order_by(models.Visit.id.desc()).offset(skip).limit(limit).all()
    return visits

@app.get("/appointments/", response_model=List[schemas.AppointmentSchema], summary="내 약속/일정 조회 (인증 필요)")
def get_appointments(
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    appointments = db.query(models.Appointment).filter(
        models.Appointment.user_id == current_user.id
    ).order_by(models.Appointment.start_time.desc()).all()
    return appointments

@app.get("/visits/{visit_id}", response_model=schemas.VisitSchema)
def get_visit_detail(
    visit_id: int,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    visit = db.query(models.Visit).filter(models.Visit.id == visit_id).first()
    if not visit:
        raise HTTPException(404, "해당 방문 기록을 찾을 수 없습니다.")
    if visit.device.user_id != current_user.id:
        raise HTTPException(403, "이 방문 기록을 조회할 권한이 없습니다.")
    return visit

@app.get("/visits/{visit_id}/transcript", response_model=schemas.VisitTranscriptResponse)
def get_visit_transcript(
    visit_id: int,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    visit = db.query(models.Visit).filter(models.Visit.id == visit_id).first()
    if not visit:
        raise HTTPException(404, "해당 방문 기록이 없습니다.")
    if visit.device.user_id != current_user.id:
        raise HTTPException(403, "열람 권한이 없습니다.")
    transcripts = (
        db.query(models.Transcript)
        .filter(models.Transcript.visit_id == visit_id)
        .order_by(models.Transcript.created_at.asc())
        .all()
    )
    return {
        "visit_id": visit.id,
        "summary": visit.summary,
        "created_at": visit.created_at,
        "transcripts": transcripts,
    }

@app.delete("/visits/{visit_id}")
def delete_visit(
    visit_id: int,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    visit = db.query(models.Visit).filter(models.Visit.id == visit_id).first()
    if not visit:
        raise HTTPException(404, "해당 방문 기록이 없습니다.")
    if visit.device.user_id != current_user.id:
        raise HTTPException(403, "삭제 권한이 없습니다.")
    db.delete(visit); db.commit()
    return {"detail": "삭제 완료"}

@app.post("/appointments/", response_model=schemas.AppointmentSchema, summary="일정 추가 (인증 필요)")
def create_appointment(
    appointment_data: schemas.AppointmentCreate,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    new_appointment = models.Appointment(
        title=appointment_data.title,
        start_time=appointment_data.start_time,
        end_time=appointment_data.end_time,
        user_id=current_user.id,
        visit_id=None
    )
    db.add(new_appointment); db.commit(); db.refresh(new_appointment)
    return new_appointment

@app.get("/appointments/{appointment_id}", response_model=schemas.AppointmentSchema)
def get_appointment_detail(
    appointment_id: int,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    appointment = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appointment:
        raise HTTPException(404, "일정을 찾을 수 없습니다.")
    if appointment.user_id != current_user.id:
        raise HTTPException(403, "이 일정을 볼 권한이 없습니다.")
    return appointment

@app.patch("/appointments/{appointment_id}", response_model=schemas.AppointmentSchema)
def update_appointment(
    appointment_id: int,
    body: dict,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    appointment = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appointment:
        raise HTTPException(404, "일정을 찾을 수 없습니다.")
    if appointment.user_id != current_user.id:
        raise HTTPException(403, "수정 권한이 없습니다.")
    for key, value in body.items():
        setattr(appointment, key, value)
    db.commit(); db.refresh(appointment)
    return appointment

@app.delete("/appointments/{appointment_id}")
def delete_appointment(
    appointment_id: int,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    appointment = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appointment:
        raise HTTPException(404, "일정을 찾을 수 없습니다.")
    if appointment.user_id != current_user.id:
        raise HTTPException(403, "삭제 권한이 없습니다.")
    db.delete(appointment); db.commit()
    return {"detail": "일정 삭제 완료"}


# --- 5. WebSocket API 엔드포인트 ---

# 5a. 실시간 영상
@app.websocket("/ws/stream/{device_uid}") # 1. 입력값을 device_id(int)에서 device_uid(str)로 변경
async def websocket_stream(websocket: WebSocket, device_uid: str, db: Session = Depends(get_db)):
    """
    앱에서 device_uid(예: "1234")로 접속하면, 
    서버가 내부적으로 device_id(예: 1)를 찾아 연결해 줍니다.
    """
    # 2. DB에서 UID로 실제 기기 정보를 조회
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    
    if not device:
        # 기기가 없으면 연결 거부
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid device UID")
        return

    # 3. 실제 DB ID(pk)를 사용하여 매니저에 연결
    real_device_id = device.id
    await video_manager.connect(real_device_id, websocket)
    
    print(f"📱 앱(시청자) 연결됨: UID={device_uid} -> DB_ID={real_device_id}")
    
    try:
        while True:
            # 연결 유지를 위한 대기 (앱에서 데이터를 보낼 일은 없으므로)
            await websocket.receive_text() 
    except WebSocketDisconnect:
        video_manager.disconnect(real_device_id, websocket)
        print(f"👋 앱(시청자) 연결 해제: UID={device_uid}")

@app.websocket("/ws/broadcast/{device_uid}")
async def websocket_broadcast(websocket: WebSocket, device_uid: str, db: Session = Depends(get_db)):
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device:
        await websocket.accept()
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid device UID")
        return
    
    await websocket.accept()
    print(f"기기(ID: {device.id})가 영상 송출을 시작했습니다.")
    try:
        while True:
            video_data = await websocket.receive_bytes()
            await video_manager.broadcast_to_device_viewers(device.id, video_data)
    except WebSocketDisconnect:
        print(f"기기(ID: {device.id})의 영상 송출이 중단되었습니다.")


# 5b. ★★★ 실시간 대화 (수정됨: 비동기 처리 강화) ★★★
@app.websocket("/ws/conversation/{device_uid}")
async def websocket_conversation(websocket: WebSocket, device_uid: str):
    """
    라즈베리파이 ↔ 서버 ↔ 사용자(웹, 앱)를 연결하는 실시간 대화 WebSocket.
    Blocking I/O(Whisper, LLM, TTS)를 스레드 풀에서 실행하여 이벤트 루프 차단을 방지합니다.
    """
    await websocket.accept()
    db = SessionLocal()  # DB 세션 생성
    loop = asyncio.get_event_loop() # 비동기 루프 가져오기

    # 1️⃣ Device 인증
    device = db.query(models.Device).filter(models.Device.device_uid == device_uid).first()
    if not device:
        await websocket.close(code=1008, reason="Invalid device UID")
        db.close()
        return

    user = device.owner
    print(f"📡 {user.full_name}님의 기기로부터 대화 연결 중... (device_id: {device.id})")

    # 2️⃣ Visit 생성
    visit = models.Visit(device_id=device.id, summary="대화 중...")
    db.add(visit); db.commit(); db.refresh(visit)
    print(f"📝 방문 기록 생성됨 (visit_id: {visit.id})")

    # 🔔 푸시 알림
    notify_user(
        user_id=device.user_id,
        title="방문자 감지",
        body=f"{device.name}에서 방문자가 대화를 시작했습니다.",
        db=db,
    )

    transcript_log = ""

    try:
        # 3️⃣ AI의 첫 응답
        greeting = "방문객: (초인종 소리)"
        transcript_log += greeting + "\n"

        # ★ Blocking 방지: AI 응답 생성
        ai_reply = await loop.run_in_executor(
            None, 
            lambda: get_llm_response(user, greeting, db=db, device=device)
        )
        transcript_log += f"AI: {ai_reply}\n"

        # DB 저장
        db.add(models.Transcript(visit_id=visit.id, speaker="ai", message=ai_reply))
        db.commit()

        # ★ Blocking 방지 & 파일 관리: TTS 생성 및 전송
        temp_audio = f"ai_greeting_{uuid.uuid4()}.mp3"
        try:
            await loop.run_in_executor(None, text_to_speech, ai_reply, temp_audio)
            with open(temp_audio, "rb") as f:
                await websocket.send_bytes(f.read())
            print("🗣️ AI 첫 인사 전송 완료")
        finally:
            if os.path.exists(temp_audio):
                os.remove(temp_audio)

        # 4️⃣ 대화 Loop
        while True:
            try:
                incoming = await websocket.receive()
            except Exception as e:
                print(f"⚠️ WebSocket Receive Error: {e}")
                break
            
            # 🟡 사용자(앱)의 텍스트 메시지를 받았을 때
            if "text" in incoming:
                user_text = incoming["text"]
                if user_text == "end":
                    print("⛔️ 대화 종료 요청 수신")
                    break

                print(f"💬 [사용자] '{user_text}'")

                transcript_log += f"User: {user_text}\n"
                db.add(models.Transcript(visit_id=visit.id, speaker="user", message=user_text))
                db.commit()

                # ★ TTS 전송 (Blocking 방지 + 파일 정리)
                tmp_user_audio = f"user_input_{uuid.uuid4()}.mp3"
                try:
                    await loop.run_in_executor(None, text_to_speech, user_text, tmp_user_audio)
                    with open(tmp_user_audio, "rb") as f:
                        await websocket.send_bytes(f.read())
                finally:
                    if os.path.exists(tmp_user_audio):
                        os.remove(tmp_user_audio)
                continue

            # 🔵 라즈베리파이에서 음성 데이터가 들어왔을 때
            if "bytes" in incoming:
                visitor_audio = incoming["bytes"]
                tmp_voice = f"raw_voice_{uuid.uuid4()}.mp3"
                
                try:
                    with open(tmp_voice, "wb") as f:
                        f.write(visitor_audio)

                    # ★ Blocking 방지: STT 변환
                    visitor_text = await loop.run_in_executor(
                        None, 
                        lambda: stt_pipe(tmp_voice)["text"]
                    )
                finally:
                    if os.path.exists(tmp_voice):
                        os.remove(tmp_voice)

                print(f"🗣️ [방문자] '{visitor_text}'")

                transcript_log += f"Visitor: {visitor_text}\n"
                db.add(models.Transcript(visit_id=visit.id, speaker="visitor", message=visitor_text))
                db.commit()

                # ★ Blocking 방지: LLM 응답 생성
                ai_reply = await loop.run_in_executor(
                    None, 
                    lambda: get_llm_response(user, transcript_log, db=db, device=device)
                )
                transcript_log += f"AI: {ai_reply}\n"
                print(f"🤖 [AI 응답] '{ai_reply}'")

                db.add(models.Transcript(visit_id=visit.id, speaker="ai", message=ai_reply))
                db.commit()

                # ★ TTS 전송 (Blocking 방지 + 파일 정리)
                tmp_ai_audio = f"ai_reply_{uuid.uuid4()}.mp3"
                try:
                    await loop.run_in_executor(None, text_to_speech, ai_reply, tmp_ai_audio)
                    with open(tmp_ai_audio, "rb") as f:
                        await websocket.send_bytes(f.read())
                finally:
                    if os.path.exists(tmp_ai_audio):
                        os.remove(tmp_ai_audio)

    except WebSocketDisconnect:
        print("⚠️ 기기 연결 끊김")
    except Exception as e:
        print(f"❗ Websocket Error: {e}")
    finally:
        print("📦 대화 종료 — 요약/일정 저장 중...")

        # 5️⃣ 후처리: 방문 요약 및 일정 추출
        post_data = get_ai_post_processing(transcript_log)
        visit.summary = post_data.get("summary", "요약 생성 실패")
        db.add(visit)

        appointment = post_data.get("appointment")
        if appointment is not None:
            try:
                db_appt = models.Appointment(
                    title=appointment["title"],
                    start_time=datetime.datetime.fromisoformat(appointment["start_time"]),
                    end_time=datetime.datetime.fromisoformat(appointment["end_time"])
                    if appointment.get("end_time")
                    else None,
                    user_id=user.id,
                    visit_id=visit.id,
                )
                db.add(db_appt)
            except Exception as ae:
                print("⚠️ 일정 저장 실패:", ae)

        db.commit()
        print(f"📌 방문 요약 저장 완료: {visit.summary}")
        
        notify_user(
            user_id=user.id,
            title="대화 종료",
            body=f"방문 요약이 생성되었습니다: {visit.summary}",
            db=db,
        )

        db.close()

@app.post("/users/me/push-token")
def save_push_token(
    body: dict,
    current_user: Annotated[models.User, Depends(auth.get_current_user)],
    db: Session = Depends(get_db)
):
    token = body.get("token")
    if not token:
        raise HTTPException(400, "token 필드가 필요합니다.")
    current_user.push_token = token
    db.commit()
    return {"detail": "토큰 저장 완료"}

@app.post("/notify")
def send_push(body: dict):
    token = body.get("token")
    title = body.get("title", "새 방문자")
    message = body.get("message", "초인종이 눌렸습니다.")
    if not token:
        raise HTTPException(400, "token이 필요합니다.")
    message_obj = messaging.Message(
        notification=messaging.Notification(
            title=title,
            body=message
        ),
        token=token,
    )
    response = messaging.send(message_obj)
    return {"detail": "푸시 전송 성공", "response": response}


# --- 6. 서버 실행 ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)