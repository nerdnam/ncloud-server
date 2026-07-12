FROM python:3.13-slim

# ghcr.io 패키지 페이지가 저장소와 연결되도록 하는 OCI 라벨
LABEL org.opencontainers.image.source="https://github.com/nerdnam/ncloud-server" \
      org.opencontainers.image.description="Self-hosted personal cloud storage (FastAPI)"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
