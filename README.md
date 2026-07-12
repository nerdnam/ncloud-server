# ☁️ ncloud

셀프 호스팅 개인 클라우드 스토리지 (Nextcloud 스타일).

## 기능

- **사용자 인증** — 첫 실행 시 관리자 계정 생성, 이후 로그인 (PBKDF2 해시 + 세션 쿠키)
- **계정 관리** — 관리자는 사용자 추가/삭제(파일 보존 선택)/비밀번호 재설정/관리자 지정·해제, 모든 사용자는 자기 비밀번호 변경. 관리자는 항상 1명 이상 유지됨
- **파일/폴더 관리** — 업로드(드래그 앤 드롭 지원), 다운로드, 새 폴더, 이름 변경, 삭제
- **미리보기** — 사진 썸네일 그리드, 클릭하면 사진·동영상·오디오 재생 (동영상 탐색은 HTTP Range 지원)
- **보기 방식 전환** — 그리드(▦) / 촘촘히(▩) / 목록(☰) 3가지, 선택은 브라우저에 저장되어 유지
- **외부 저장소 (도커 볼륨 마운트)** — 호스트 폴더를 `/app/mounts/<이름>`에 마운트하면 웹 UI 사이드바에 자동으로 나타나 탐색·업로드·미리보기 가능. `:ro`로 마운트하면 읽기 전용(🔒)

## 실행 (Docker Compose)

```bash
docker compose up -d --build
```

브라우저에서 http://localhost:8000 접속 → 첫 실행이면 관리자 계정을 만들고 시작합니다.

- 데이터(계정 DB, 업로드 파일, 썸네일)는 `compose.yaml`의 `volumes`에 지정한 호스트 폴더에 저장되므로 컨테이너를 지우거나 재빌드해도 유지됩니다.
- 포트를 바꾸려면 `compose.yaml`의 `ports`를 `"원하는포트:8000"`으로 수정하세요.
- 중지: `docker compose down` / 로그 확인: `docker compose logs -f`

### 외부 저장소 연결

`compose.yaml`의 `volumes`에 호스트 폴더를 `/app/mounts/<이름>`으로 추가하면 웹 UI에 그 이름의 저장소가 나타납니다:

```yaml
    volumes:
      - /mnt/Data0/Ncloud/data:/app/data
      - /mnt/Data0/Media:/app/mounts/media        # "media" 저장소 (읽기/쓰기)
      - /mnt/Data0/Backup:/app/mounts/backup:ro   # "backup" 저장소 (읽기 전용 🔒)
```

수정 후 `docker compose up -d`로 다시 올리면 반영됩니다. 외부 저장소는 로그인한 모든 사용자가 볼 수 있습니다.

### 개발용 네이티브 실행 (선택)

```powershell
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000
```

## 구조

```
app/
  main.py       # FastAPI 앱, 정적 파일 서빙
  auth.py       # 계정 생성/로그인/세션
  files.py      # 파일 목록/업로드/다운로드/썸네일/스트리밍
  database.py   # SQLite (data/ncloud.db)
static/         # 웹 UI (HTML/CSS/JS)
data/
  files/<사용자>/  # 업로드된 파일 (사용자별 격리)
  thumbs/          # 썸네일 캐시
  ncloud.db        # 계정·세션 DB
```

## API

인증: `POST /api/auth/setup` `login` `logout` `change-password`, `GET /api/auth/status`
파일: `GET /api/files/spaces` `list` `download` `raw` `thumb`, `POST /api/files/upload` `mkdir` `rename` `delete`
관리: `GET /api/admin/users`, `POST /api/admin/users` `users/delete` `users/reset-password` `users/set-admin`

전체 문서는 http://localhost:8000/docs (Swagger UI).
