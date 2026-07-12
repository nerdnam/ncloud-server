#!/bin/bash

docker stop ncloud-server


# ▼▼▼ [핵심 수정] ▼▼▼
# 도커 빌드를 실행하기 전, 로컬의 모든 .pyc 캐시 파일을 강제로 삭제합니다.
echo "Cleaning up local __pycache__ directories..."
find . -type d -name "__pycache__" -exec rm -r {} +
find . -type f -name "*.pyc" -delete
# ▲▲▲ [수정 완료] ▲▲▲

# 모든 사용하지 않는 도커 리소스(이미지, 컨테이너, 네트워크, 볼륨)를 삭제합니다.
echo "Pruning Docker system..."
docker system prune -a -f

# 캐시를 사용하지 않고 새 도커 이미지를 빌드합니다.
echo "Building new Docker image..."
docker build -t nerdnam/ncloud-server:0.0.1 --no-cache .

echo "Script finished."
