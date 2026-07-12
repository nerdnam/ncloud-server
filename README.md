# ☁️ ncloud

**셀프 호스팅 개인 클라우드 스토리지** — Nextcloud 스타일의 가벼운 파일 서버입니다.
FastAPI 백엔드 + 순수 JS 웹 UI 단일 컨테이너로 구성되어, 도커만 있으면 어떤 서버·NAS에서든 1분 안에 띄울 수 있습니다.

[![Build and push Docker image](https://github.com/nerdnam/ncloud-server/actions/workflows/docker.yml/badge.svg)](https://github.com/nerdnam/ncloud-server/actions/workflows/docker.yml)

```
ghcr.io/nerdnam/ncloud-server  (linux/amd64, linux/arm64)
```

---

## 목차

- [주요 기능](#주요-기능)
- [빠른 시작](#빠른-시작)
- [compose.yaml 설정](#composeyaml-설정)
- [외부 저장소 (도커 볼륨 마운트)](#외부-저장소-도커-볼륨-마운트)
- [계정 관리](#계정-관리)
- [업데이트 및 버전 관리](#업데이트-및-버전-관리)
- [프로젝트 구조](#프로젝트-구조)
- [API](#api)
- [보안](#보안)
- [개발 환경](#개발-환경)

---

## 주요 기능

### 파일 관리
- **업로드** — 버튼 또는 **드래그 앤 드롭**으로 여러 파일 동시 업로드. 같은 이름이 있으면 `이름 (1).ext` 형식으로 자동 회피
- **폴더 탐색** — 새 폴더 생성, 이름 변경, 삭제, 경로 표시줄(브레드크럼) 탐색
- **다운로드** — 개별 파일 다운로드 (한글 파일명 지원)
- **보기 방식 3종** — 그리드(▦) / 촘촘히(▩) / 목록(☰), 선택은 브라우저에 저장되어 유지
- **브라우저 히스토리 연동** — 뒤로가기 = 이전 폴더, 새로고침해도 보던 위치 유지, `#/저장소/경로` 딥링크 공유 가능

### 미리보기
- **사진** — 목록에서 WebP 썸네일 자동 생성(캐시), 클릭하면 원본 크게 보기 (EXIF 회전 반영)
- **동영상** — 브라우저 내장 플레이어로 즉시 재생, HTTP Range 지원으로 **구간 이동(탐색) 가능**
- **오디오** — mp3, flac 등 스트리밍 재생
- 미리보기 중 뒤로가기를 누르면 페이지 이동 대신 미리보기가 닫힘

### 저장소
- **개인 저장소** — 사용자마다 격리된 홈 디렉토리 (`data/files/<아이디>/`)
- **외부 저장소** — 호스트의 아무 폴더나 도커 볼륨으로 마운트하면 웹 UI에 자동 표시 ([아래 상세](#외부-저장소-도커-볼륨-마운트))

### 계정
- 첫 실행 시 관리자 계정 생성, 이후 로그인 (PBKDF2-SHA256 30만 회 + httponly 세션 쿠키)
- 관리자: 사용자 추가/삭제/비밀번호 재설정/관리자 지정·해제, 사용자별 저장 공간 사용량 확인
- 모든 사용자: 자기 비밀번호 변경 (변경 시 다른 기기 세션 자동 로그아웃)

---

## 빠른 시작

### 1. compose.yaml 준비

```yaml
services:
  ncloud:
    image: ghcr.io/nerdnam/ncloud-server:latest
    container_name: ncloud-server
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data          # 계정 DB·업로드 파일·썸네일 (필수)
    restart: unless-stopped
```

### 2. 실행

```bash
docker compose pull
docker compose up -d
```

### 3. 접속

브라우저에서 `http://서버주소:8000` 접속 → 첫 화면에서 **관리자 계정을 만들면 바로 사용 시작**됩니다.

> 소스에서 직접 빌드하려면: `git clone https://github.com/nerdnam/ncloud-server.git` 후 `docker compose up -d --build`

---

## compose.yaml 설정

이 저장소의 [compose.yaml](compose.yaml)이 전체 예시입니다. 주요 항목:

| 항목 | 설명 |
|------|------|
| `ports` | `"8000:8000"` — 왼쪽 숫자를 바꾸면 외부 포트 변경 (예: `"9000:8000"`) |
| `volumes` → `/app/data` | **필수.** 계정 DB(`ncloud.db`), 업로드 파일, 썸네일 캐시가 모두 저장됩니다. 컨테이너를 지우거나 이미지를 갈아엎어도 이 폴더만 있으면 데이터가 유지됩니다 |
| `volumes` → `/app/mounts/<이름>` | 선택. 외부 저장소 연결 ([아래](#외부-저장소-도커-볼륨-마운트)) |
| `restart: unless-stopped` | 서버 재부팅 시 자동 시작 |
| `healthcheck` | 30초마다 API 응답 확인 (`docker ps`에서 healthy 표시) |

자주 쓰는 명령:

```bash
docker compose logs -f     # 로그 실시간 확인
docker compose down        # 중지 (데이터는 유지됨)
docker compose pull && docker compose up -d   # 최신 버전으로 업데이트
```

---

## 외부 저장소 (도커 볼륨 마운트)

호스트의 폴더를 `/app/mounts/<이름>`으로 마운트하면, 웹 UI 왼쪽 사이드바에 `<이름>` 저장소가 자동으로 나타납니다. 별도 설정이나 재시작 없이 마운트만 하면 됩니다.

```yaml
    volumes:
      - ./data:/app/data
      - /mnt/Data0/Media:/app/mounts/media        # "media" 저장소 (읽기/쓰기)
      - /mnt/Data0/Backup:/app/mounts/backup:ro   # "backup" 저장소 (읽기 전용)
```

- **읽기/쓰기 마운트** — 탐색, 업로드, 폴더 생성, 이름 변경, 삭제, 미리보기 모두 가능
- **`:ro` 읽기 전용 마운트** — 서버가 자동 감지해 UI에 🔒 표시, 업로드·삭제 버튼이 숨겨지고 API 차원에서도 쓰기가 403으로 차단됩니다
- 외부 저장소는 **로그인한 모든 사용자에게 공유**됩니다 (개인 저장소만 사용자별 격리)
- 마운트 안의 심볼릭 링크도 지원합니다
- 이미 존재하는 파일 수만 개가 있는 폴더를 연결해도 색인 과정 없이 즉시 탐색 가능합니다

compose.yaml 수정 후 `docker compose up -d`로 다시 올리면 반영됩니다.

---

## 계정 관리

### 역할

| | 일반 사용자 | 관리자 |
|---|---|---|
| 개인 저장소 | ✅ | ✅ |
| 외부 저장소 | ✅ | ✅ |
| 자기 비밀번호 변경 | ✅ | ✅ |
| 사용자 추가/삭제/재설정/권한 변경 | ❌ | ✅ |

- **첫 실행 때 만드는 계정이 관리자**가 됩니다. 헤더에 `아이디 (관리자)`로 표시됩니다
- 관리자는 헤더의 **👥 사용자 관리** 버튼으로 사용자 목록(사용량 포함)을 보고 관리합니다
- 회원가입 기능은 의도적으로 없습니다 — 계정은 관리자만 만들 수 있습니다 (셀프 호스팅 서버가 외부에 노출돼도 임의 가입 불가)

### 안전 장치

- **관리자는 항상 1명 이상 유지** — 자기 자신 삭제/권한 해제 불가, 동시 요청 경쟁까지 원자적 SQL로 차단
- **비밀번호 재설정 시 해당 사용자의 모든 세션 즉시 만료** (강제 재로그인)
- **계정 삭제 시 파일 처리 선택** — 함께 삭제하거나 보존. 보존한 경우 같은 아이디를 재생성해도 이전 사용자의 파일은 `@archived-` 폴더로 자동 격리되어 새 계정에 노출되지 않습니다
- 구버전 DB는 시작 시 자동 마이그레이션됩니다 (첫 사용자가 관리자로 승격)

---

## 업데이트 및 버전 관리

이미지는 GitHub Actions가 자동으로 빌드해 ghcr.io에 올립니다:

| 태그 | 생성 시점 | 용도 |
|------|----------|------|
| `latest` | main 브랜치에 push될 때마다 | 항상 최신 버전 추적 |
| `sha-XXXXXXX` | 커밋마다 | 특정 커밋 고정 |
| `0.0.1` 등 semver | `v*` git 태그 push 시 | 릴리스 버전 고정 |

```bash
# 서버 업데이트 (latest 사용 시)
docker compose pull && docker compose up -d

# 특정 버전 고정: compose.yaml에서
image: ghcr.io/nerdnam/ncloud-server:0.0.1
```

새 버전 릴리스는 저장소에서:

```bash
git tag v0.0.2 && git push --tags   # → 0.0.2 이미지 자동 빌드
```

로컬에서 수동 빌드가 필요하면 [deploy.sh](deploy.sh)를 사용합니다 (`./deploy.sh` 빌드만, `./deploy.sh --push` 빌드 후 ghcr 푸시 — 사전에 `docker login ghcr.io` 필요).

---

## 프로젝트 구조

```
ncloud-server/
├── app/                      # FastAPI 백엔드
│   ├── main.py               #   앱 진입점, 라우터 등록, 정적 파일 서빙
│   ├── auth.py               #   계정 생성/로그인/세션/비밀번호 변경
│   ├── admin.py              #   관리자 전용 사용자 관리 API
│   ├── files.py              #   파일 목록/업로드/다운로드/썸네일/스트리밍/외부 저장소
│   └── database.py           #   SQLite 초기화 및 스키마 마이그레이션
├── static/                   # 웹 UI (프레임워크 없는 순수 HTML/CSS/JS)
│   ├── index.html
│   ├── app.js
│   └── style.css
├── Dockerfile                # python:3.13-slim 기반 단일 이미지
├── compose.yaml              # 배포 구성 (볼륨/포트/헬스체크)
├── deploy.sh                 # 로컬 수동 빌드·푸시 스크립트
└── .github/workflows/
    └── docker.yml            # ghcr.io 자동 빌드 (amd64/arm64)

# 런타임 데이터 (컨테이너 볼륨, git에는 없음)
data/
├── ncloud.db                 # 계정·세션 (SQLite)
├── files/<아이디>/            # 사용자별 개인 저장소
└── thumbs/                   # 썸네일 캐시 (지워도 자동 재생성)
```

의존성은 4개뿐입니다: `fastapi`, `uvicorn`, `python-multipart`, `pillow`.

---

## API

모든 파일 API는 `space` 파라미터를 받습니다 (`home` = 개인 저장소, 그 외 = 외부 저장소 이름).

### 인증 `/api/auth`

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/status` | 로그인 상태·초기 설정 필요 여부 |
| POST | `/setup` | 첫 관리자 계정 생성 (계정이 하나도 없을 때만) |
| POST | `/login` | 로그인 (세션 쿠키 발급, 30일) |
| POST | `/logout` | 로그아웃 |
| POST | `/change-password` | 자기 비밀번호 변경 |

### 파일 `/api/files`

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/spaces` | 저장소 목록 (개인 + 외부 마운트, 읽기 전용 여부 포함) |
| GET | `/list?space=&path=` | 폴더 내용 |
| GET | `/download?space=&path=` | 파일 다운로드 (attachment) |
| GET | `/raw?space=&path=` | 인라인 서빙 — 미리보기·스트리밍용, HTTP Range 지원 |
| GET | `/thumb?space=&path=` | 이미지 썸네일 (WebP 256px, 캐시) |
| POST | `/upload?space=&path=` | 파일 업로드 (multipart, 다중) |
| POST | `/mkdir` `/rename` `/delete` | 폴더 생성 / 이름 변경 / 삭제 |

### 관리자 `/api/admin` (관리자 세션 필요)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/users` | 사용자 목록 + 저장 공간 사용량 |
| POST | `/users` | 사용자 생성 (`is_admin` 선택) |
| POST | `/users/delete` | 사용자 삭제 (`delete_files`로 파일 처리 선택) |
| POST | `/users/reset-password` | 비밀번호 재설정 (세션 전체 만료) |
| POST | `/users/set-admin` | 관리자 지정/해제 |

전체 스키마는 서버 실행 후 `http://서버주소:8000/docs` (Swagger UI)에서 확인할 수 있습니다.

---

## 보안

- **비밀번호** — PBKDF2-HMAC-SHA256 300,000회 + 사용자별 랜덤 salt. 존재하지 않는 아이디도 동일한 시간이 걸리게 처리해 타이밍으로 계정 존재를 유추할 수 없습니다
- **세션** — 랜덤 토큰을 httponly + SameSite=Lax 쿠키로 발급, 서버 측 DB에서 관리(만료 30일)
- **경로 격리** — 모든 파일 접근은 저장소 루트 기준으로 해석·검증됩니다. `../` 순회, 백슬래시, 심볼릭 링크를 통한 루트 밖 접근이 차단됩니다
- **권한 경계** — 관리자 API는 서버 측 세션 검증(`require_admin`), 읽기 전용 마운트는 API 차원에서 쓰기 403

> ⚠️ **외부(인터넷)에 공개할 경우** 반드시 HTTPS 뒤에 두세요. Caddy, nginx, Traefik 등 리버스 프록시로 TLS를 붙이는 것을 권장합니다. 세션 쿠키가 평문 HTTP에서는 노출될 수 있습니다.

---

## 개발 환경

도커 없이 로컬에서 바로 실행:

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

- 데이터는 프로젝트 폴더의 `data/`에 저장됩니다
- 외부 저장소 테스트: `mounts/<이름>` 폴더를 만들면 됩니다 (`NCLOUD_MOUNTS_DIR` 환경변수로 위치 변경 가능)
- 프론트엔드는 빌드 과정이 없습니다 — `static/` 파일을 수정하면 새로고침으로 바로 반영
