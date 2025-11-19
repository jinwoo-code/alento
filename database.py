# database.py (수정된 전체 코드)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_engine(DATABASE_URL)
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