FROM python:3.13-slim

# ghcr.io 패키지 페이지가 저장소와 연결되도록 하는 OCI 라벨
LABEL org.opencontainers.image.source="https://github.com/nerdnam/gendisk.cloud" \
      org.opencontainers.image.description="GenDisk — self-hosted personal cloud storage (FastAPI)"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
# Windows 클라이언트 .exe (CI가 downloads/에 넣어 이미지에 포함 → 웹 UI에서 다운로드).
# 로컬 빌드 시엔 .gitkeep만 있어 비어 있음 (다운로드 버튼은 파일이 있을 때만 표시).
COPY downloads ./downloads

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
