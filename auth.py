import os
import secrets
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv  # ★★★ 이 줄이 추가되었습니다! ★★★
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, APIKeyHeader
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

# 로컬 모듈 import
import models
import schemas
from database import get_db

# --- 환경 변수 로드 ---
load_dotenv()  # ★★★ 이제 이 함수가 정상적으로 작동합니다 ★★★
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM")
ACCESS_TOKEN_EXPIRE_MINUTES = 30 # 토큰 유효 기간 (30분)

# --- 비밀번호 암호화 설정 ---
# 사용할 암호화 스킴을 bcrypt로 지정
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- JWT 토큰 관련 설정 ---
# /token 엔드포인트에서 토큰을 가져오도록 설정
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# --- 헬퍼 함수 ---

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """입력된 평문 비밀번호와 DB의 해시된 비밀번호를 비교합니다."""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """비밀번호를 bcrypt 해시값으로 변환합니다."""
    return pwd_context.hash(password)

def create_access_token(data: dict) -> str:
    """JWT 액세스 토큰을 생성합니다."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def create_api_key() -> str:
    """32바이트(64글자) 길이의 안전한 16진수 API 키를 생성합니다."""
    return secrets.token_hex(32)

def get_user(db: Session, email: str) -> models.User | None:
    """이메일로 사용자를 조회합니다."""
    return db.query(models.User).filter(models.User.email == email).first()

# --- API 의존성 함수 ---

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> models.User:
    """
    (사용자 인증용)
    요청 헤더의 'Authorization: Bearer <토큰>'을 검증하고,
    DB에서 해당 사용자 정보를 찾아 반환합니다.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    user = get_user(db, email=email)
    if user is None:
        raise credentials_exception
    return user

# "X-API-Key" 라는 이름의 헤더에서 키를 찾는 객체
api_key_header_scheme = APIKeyHeader(name="X-API-Key")

def get_current_device(
    api_key: str = Depends(api_key_header_scheme), 
    db: Session = Depends(get_db)
) -> models.Device:
    """
    (기기 인증용)
    요청 헤더의 'X-API-Key'를 검증하고,
    DB에서 해당 기기 정보를 찾아 반환합니다.
    """
    device = db.query(models.Device).filter(models.Device.api_key == api_key).first()
    if not device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
        )
    return device