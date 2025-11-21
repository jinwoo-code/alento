# models.py (api_key가 추가된 최종 코드)

from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey, func
from sqlalchemy.orm import relationship
from database import Base

now = func.now()

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(100))
    is_home = Column(Boolean, default=False, nullable=False)
    return_time = Column(String(100))
    memo = Column(Text)
    created_at = Column(DateTime, nullable=False, default=now)
    updated_at = Column(DateTime, nullable=False, default=now, onupdate=now)
    
    devices = relationship("Device", back_populates="owner", cascade="all, delete-orphan")
    appointments = relationship("Appointment", back_populates="user", cascade="all, delete-orphan")
    
    push_token = Column(String(512), nullable=True)
    

class Device(Base):
    __tablename__ = "devices"
    
    id = Column(Integer, primary_key=True, index=True)

    # 기기 고유 식별자(라즈베리파이에 박힌 고유 UID 등)
    device_uid = Column(String(255), unique=True, index=True, nullable=False)

    # 기기 인증 키 (반드시 유지)
    api_key = Column(String(255), unique=True, index=True, nullable=False)

    name = Column(String(100), nullable=False, default="My Doorbell")

    # 🔥 새로 추가되는 기기별 메모
    memo = Column(Text, nullable=True)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    created_at = Column(DateTime, nullable=False, default=now)
    updated_at = Column(DateTime, nullable=False, default=now, onupdate=now)

    owner = relationship("User", back_populates="devices")
    visits = relationship("Visit", back_populates="device", cascade="all, delete-orphan")

class Visit(Base):
    __tablename__ = "visits"
    
    id = Column(Integer, primary_key=True, index=True)
    summary = Column(Text)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False)
    visitor_photo_url = Column(String(1024), nullable=True)
    visitor_audio_url = Column(String(1024), nullable=True)
    ai_response_audio_url = Column(String(1024), nullable=True)
    
    visitor_video_url = Column(String(1024), nullable=True)
    
    created_at = Column(DateTime, nullable=False, default=now)
    updated_at = Column(DateTime, nullable=False, default=now, onupdate=now)
    
    device = relationship("Device", back_populates="visits")
    transcripts = relationship("Transcript", back_populates="visit", cascade="all, delete-orphan")
    appointments = relationship("Appointment", back_populates="visit", cascade="all, delete-orphan")

class Transcript(Base):
    __tablename__ = "transcripts"
    
    id = Column(Integer, primary_key=True, index=True)
    speaker = Column(String(50), nullable=False)
    message = Column(Text, nullable=False)
    visit_id = Column(Integer, ForeignKey("visits.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=now)
    
    visit = relationship("Visit", back_populates="transcripts")

class Appointment(Base):
    __tablename__ = "appointments"
    
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=True)
    status = Column(String(50), nullable=False, default="SCHEDULED")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    visit_id = Column(Integer, ForeignKey("visits.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=now)
    updated_at = Column(DateTime, nullable=False, default=now, onupdate=now)
    
    user = relationship("User", back_populates="appointments")
    visit = relationship("Visit", back_populates="appointments")