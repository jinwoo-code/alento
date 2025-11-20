# database.py (수정된 전체 코드)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# engine = create_engine(DATABASE_URL)

# # DB 터지면
engine = create_engine(
    DATABASE_URL, 
    pool_size=20,       # 기본 연결 수 늘리기 (기본 5 -> 20)
    max_overflow=40,    # 돌발 시 추가 허용 수 (기본 10 -> 40)
    pool_recycle=1800   # 연결 갱신 주기
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# get_db 함수를 main.py에서 이곳으로 이동시킵니다.
def get_db():
    """API 요청마다 데이터베이스 세션을 생성하고, 요청이 끝나면 닫습니다."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()