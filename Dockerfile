# Dockerfile (이 내용으로 덮어쓰세요)

# 1. 파이썬 3.10 버전을 기반으로 시작합니다.
FROM python:3.10-slim

# 2. Whisper(STT)에 필요한 FFmpeg를 설치합니다.
RUN apt-get update && apt-get install -y ffmpeg

# 3. 컨테이너 내부의 작업 폴더를 만듭니다.
WORKDIR /app

# 4. '쇼핑 리스트'를 먼저 복사합니다.
COPY requirements.txt .

# 5. (★수정됨) PyTorch/Torchvision/Torchaudio를 먼저 설치합니다.
# 이것이 transformers(Whisper)에 필요합니다.
RUN pip install --no-cache-dir torch torchvision torchaudio

# 6. (★수정됨) '쇼핑 리스트'에 있는 나머지 패키지들을 설치합니다.
RUN pip install --no-cache-dir -r requirements.txt

# 7. 나머지 모든 코드(main.py, auth.py 등)를 컨테이너에 복사합니다.
COPY . .


# 8. 서버를 0.0.0.0 호스트로 실행하도록 기본 명령어를 설정합니다.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]