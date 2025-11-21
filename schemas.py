from pydantic import BaseModel
from typing import List, Optional
import datetime

# ============================
# 🔐 인증 관련 스키마
# ============================
class UserCreate(BaseModel):
    email: str
    password: str
    full_name: str


class User(BaseModel):
    id: int
    email: str
    full_name: str
    is_home: bool
    return_time: str | None
    memo: str | None

    model_config = {
        "from_attributes": True
    }


class Token(BaseModel):
    access_token: str
    token_type: str


# ----------------------------
# 🛠 사용자 정보 업데이트
# ----------------------------
class UserStatusUpdate(BaseModel):
    is_home: bool | None = None
    return_time: str | None = None
    memo: str | None = None
    

class UserUpdate(BaseModel):
    full_name: str | None = None
    email: str | None = None
    memo: str | None = None


# ============================
# 🔔 기기 관련 스키마
# ============================
class DeviceBase(BaseModel):
    name: str | None = "My Doorbell"
    device_uid: str
    memo: str | None = None


class DeviceCreate(DeviceBase):
    pass


class DeviceRegisterResponse(DeviceBase):
    id: int
    user_id: int
    api_key: str

    model_config = {
        "from_attributes": True
    }


class Device(DeviceBase):
    id: int
    user_id: int
    api_key: str

    model_config = {
        "from_attributes": True
    }


# ============================
# 📝 방문 기록 & 대화 로그
# ============================
class TranscriptSchema(BaseModel):
    id: int
    speaker: str
    message: str
    created_at: datetime.datetime

    model_config = {
        "from_attributes": True
    }


class VisitSchema(BaseModel):
    id: int
    summary: str | None = None
    device_id: int
    visitor_photo_url: str | None = None
    visitor_audio_url: str | None = None
    ai_response_audio_url: str | None = None
    
    # [추가] 프론트엔드에 보낼 필드
    visitor_video_url: str | None = None 
    
    created_at: datetime.datetime
    transcripts: List[TranscriptSchema] = []

    model_config = {
        "from_attributes": True
    }


class VisitTranscriptResponse(BaseModel):
    visit_id: int
    summary: str | None
    created_at: datetime.datetime
    transcripts: List[TranscriptSchema]

    model_config = {
        "from_attributes": True
    }


# ============================
# 📅 일정 관리
# ============================
class AppointmentSchema(BaseModel):
    id: int
    title: str
    start_time: datetime.datetime
    end_time: datetime.datetime | None
    status: str
    user_id: int
    visit_id: int | None

    model_config = {
        "from_attributes": True
    }


class AppointmentCreate(BaseModel):
    title: str
    start_time: datetime.datetime
    end_time: datetime.datetime | None = None
