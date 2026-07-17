# ☁️ genDISK

**셀프 호스팅 개인 클라우드 스토리지** ([gendisk.cloud](https://gendisk.cloud)) — Nextcloud 스타일의 가벼운 파일 서버입니다.
FastAPI 백엔드 + 순수 JS 웹 UI 단일 컨테이너로 구성되어, 도커만 있으면 어떤 서버·NAS에서든 1분 안에 띄울 수 있습니다.

[![Build and release](https://github.com/nerdnam/gendisk.cloud/actions/workflows/docker.yml/badge.svg)](https://github.com/nerdnam/gendisk.cloud/actions/workflows/docker.yml)

```
ghcr.io/nerdnam/gendisk.cloud  (linux/amd64, linux/arm64)
```

---

## 목차

- [주요 기능](#주요-기능)
- [빠른 시작](#빠른-시작)
- [compose.yaml 설정](#composeyaml-설정)
- [외부 저장소 (도커 볼륨 마운트)](#외부-저장소-도커-볼륨-마운트)
- [계정 관리](#계정-관리)
- [외부 공유 (링크로 공유)](#외부-공유-링크로-공유)
- [Homepage 대시보드 위젯](#homepage-대시보드-위젯)
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

### 외부 공유
- **링크로 공유** — 파일이나 폴더를 골라 `/s/<토큰>` 공개 링크를 만들면 로그인 없이 **보기·다운로드**할 수 있습니다 (읽기 전용) ([아래 상세](#외부-공유-링크로-공유))
- **선택적 비밀번호·만료** — 링크마다 비밀번호와 만료(7·30·90일·무기한)를 걸 수 있고, 토큰은 항상 추측 불가한 무작위 값
- **내 공유 관리** — 헤더의 **🔗 내 공유**에서 만든 링크를 복사·열기·해제
- 공개 페이지는 폴더 탐색·이미지/동영상 미리보기·다크 모드를 지원하고, 원본 대신 썸네일로 목록을 그려 대역폭을 아낍니다

### 계정
- 첫 실행 시 관리자 계정 생성, 이후 로그인 (PBKDF2-SHA256 30만 회 + httponly 세션 쿠키)
- 관리자: 사용자 추가/삭제/비밀번호 재설정/관리자 지정·해제, 사용자별 저장 공간 사용량 확인
- **계정별 용량 제한** — 관리자가 사용자마다 개인 저장소 용량 상한을 지정 (0 = 무제한). 초과 업로드는 차단
- **계정별 외부 저장소 권한** — 관리자가 사용자마다 접근 가능한 외부 마운트를 지정. 관리자는 항상 전체 접근, 일반 사용자는 부여받은 것만
- 모든 사용자: 자기 비밀번호 변경 (변경 시 다른 기기 세션 자동 로그아웃)
- **QR 로그인** — 웹에서 QR을 띄우고 모바일 앱으로 스캔하면 서버 주소 입력 없이 바로 페어링 ([아래](#qr-로그인-모바일-앱-연동))

---

## 빠른 시작

### 1. compose.yaml 준비

```yaml
services:
  gendisk:
    image: ghcr.io/nerdnam/gendisk.cloud:latest
    container_name: gendisk
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

> 소스에서 직접 빌드하려면: `git clone https://github.com/nerdnam/gendisk.cloud.git` 후 `docker compose up -d --build`

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
| 외부 저장소 | 부여받은 것만 | ✅ 전체 |
| 자기 비밀번호 변경 | ✅ | ✅ |
| 사용자 추가/삭제/재설정/권한 변경 | ❌ | ✅ |

- **첫 실행 때 만드는 계정이 관리자**가 됩니다. 헤더에 `아이디 (관리자)`로 표시됩니다
- 관리자는 헤더의 **👥 사용자 관리** 버튼으로 사용자 목록(사용량/제한 포함)을, **💾 외부 저장소** 버튼으로 저장소별 접근 권한을 관리합니다
- 회원가입 기능은 의도적으로 없습니다 — 계정은 관리자만 만들 수 있습니다 (셀프 호스팅 서버가 외부에 노출돼도 임의 가입 불가)

### 용량 제한과 저장소 권한

- **용량 제한** — 사용자 관리에서 **용량 제한** 버튼으로 GB 단위 상한 지정 (0 = 무제한). **개인 저장소에만** 적용되며, 외부 마운트는 공유 저장소라 제한에 포함되지 않습니다. 상한을 넘기는 업로드는 저장되지 않고 거부됩니다 (동시 업로드로도 우회 불가)
- **외부 저장소 권한** — 헤더의 **💾 외부 저장소** 버튼을 열면 마운트된 저장소별로 접근할 사용자를 체크박스로 지정합니다 (체크 즉시 저장). 부여되지 않은 저장소는 사용자 목록에 아예 보이지 않고 API로 직접 접근해도 차단됩니다. 관리자는 항상 모든 마운트에 접근합니다

### 안전 장치

- **관리자는 항상 1명 이상 유지** — 자기 자신 삭제/권한 해제 불가, 동시 요청 경쟁까지 원자적 SQL로 차단
- **비밀번호 재설정 시 해당 사용자의 모든 세션 즉시 만료** (강제 재로그인)
- **계정 삭제 시 파일 처리 선택** — 함께 삭제하거나 보존. 보존한 경우 같은 아이디를 재생성해도 이전 사용자의 파일은 `@archived-` 폴더로 자동 격리되어 새 계정에 노출되지 않습니다
- 구버전 DB는 시작 시 자동 마이그레이션됩니다 (첫 사용자가 관리자로 승격)

---

## 외부 공유 (링크로 공유)

파일이나 폴더를 **로그인 없이 열람·다운로드할 수 있는 공개 링크**로 공유합니다. iCloud/Dropbox의 "링크 공유"와 같은 방식이며, 읽기 전용입니다.

**사용법**

1. 파일/폴더의 **🔗** 버튼(또는 항목 하나를 선택한 뒤 선택 바의 **🔗 공유**) 클릭
2. (선택) 비밀번호·만료일 지정 → **링크 만들기**
3. 생성된 `https://서버/s/<토큰>` 링크를 복사해 전달
4. 받은 사람은 로그인 없이 열람 — 폴더 공유는 하위 탐색·다운로드, 파일 공유는 미리보기+다운로드

헤더의 **🔗 내 공유**에서 만든 링크를 복사·열기·해제할 수 있습니다.

**보안 설계**

- 링크 토큰은 추측 불가한 무작위 값(`secrets.token_urlsafe`)이며, 검색엔진 색인도 막습니다(`noindex`)
- **읽기 전용** — 방문자는 업로드·삭제·이동을 할 수 없습니다
- **비밀번호(선택)** — PBKDF2로 저장되고, 잠금이 풀리기 전에는 파일 이름조차 노출되지 않습니다. 언락 성공 시 해당 링크 경로로만 스코프된 httponly 쿠키가 발급되며, 무차별 대입·CPU 소진을 막기 위해 언락에 레이트리밋이 걸려 있습니다
- **만료(선택)** — 지난 링크는 즉시 무효화됩니다(410)
- **소유자 신원으로 재확인** — 공개 열람 시에도 소유자 기준으로 저장소 접근권을 다시 검사하므로, 소유자가 삭제되거나 외부 저장소 권한이 회수되면 링크도 함께 죽습니다. 공유한 대상이 삭제·이동·이름변경되면 그 링크는 자동으로 제거됩니다
- **경로 격리·XSS 방지** — 공유 루트 밖 접근을 차단하고(심볼릭 링크 포함), `.html`·`.svg` 등 실행 가능한 파일은 브라우저에서 렌더되지 않도록 항상 다운로드로 강제(`X-Content-Type-Options: nosniff` + attachment)해 같은 오리진 스크립트 실행을 막습니다

> ⚠️ 링크가 실제로 외부에서 열리려면 서버가 인터넷에서 접근 가능해야 하며, HTTPS 뒤에 두는 것을 권장합니다.

---

## 네트워크 드라이브로 마운트 (WebDAV)

`/dav` 에서 WebDAV를 제공하므로, 별도 클라이언트 없이 OS 기본 기능으로 genDISK를 **일반 디스크처럼** 연결할 수 있습니다.

- **Windows** — 탐색기 → 네트워크 드라이브 연결 → `\\서버@SSL@443\dav` (HTTPS). 또는 [win_x64 클라이언트](win_x64/README.md)의 "드라이브 연결" 버튼.
- **macOS** — Finder → 서버에 연결 → `https://서버/dav`
- **Linux / rclone / Cyberduck** — WebDAV로 `https://서버/dav`

인증은 **아이디·비밀번호(HTTP Basic)**. 경로는 `/dav/<저장소>/...` 형식으로, `home`(개인) 또는 접근 권한이 있는 외부 저장소가 최상위 폴더로 보입니다. 저장소 접근 권한·읽기 전용·용량 제한이 그대로 적용됩니다.

> ⚠️ WebDAV는 Basic 인증을 쓰므로 **HTTPS(리버스 프록시) 뒤에서 사용**하세요. Windows는 평문 HTTP에서 Basic 인증을 기본 차단합니다.

지원 메서드: OPTIONS, PROPFIND, GET, HEAD, PUT, DELETE, MKCOL, MOVE, COPY, LOCK, UNLOCK, PROPPATCH.

## 데스크톱 동기화 (Mac 스토리지 프로바이더)

macOS의 File Provider 앱처럼, genDISK를 Finder에서 다른 클라우드 서비스(Dropbox 등)와 동일하게 탐색·동기화할 수 있도록 백엔드가 동기화 API를 제공합니다. (앱은 아직 개발 전이며, 서버 측 규격이 준비되어 있습니다.)

**동기화 API** (`/api/sync`, 쿠키 또는 `Authorization: Bearer` 인증)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/info` | 서버 기능·버전 |
| GET | `/enumerate?space=&path=` | 공간 전체를 재귀 나열 (각 항목에 콘텐츠 etag) + 현재 커서 |
| GET | `/delta?space=&cursor=` | 커서 이후 생성·수정된 항목만 |
| POST | `/put?space=&path=` | 정확한 경로에 파일 생성/덮어쓰기 (본문 = 파일 내용) |

읽기는 기존 `GET /api/files/download`, 폴더 생성·삭제는 `/api/files/mkdir`·`delete`, 이동(폴더 간)은 `/api/files/move`를 사용합니다. 앱이 여유 공간을 표시할 때는 `GET /api/files/usage`로 개인 저장소 사용량·용량을 조회합니다.

**동작 방식**

- **커서** — 열거를 시작한 시각(나노초). `delta`는 `커서 - 2초` 이후 변경분을 돌려주므로, 열거 도중·직후의 수정도 **절대 누락되지 않습니다** (최악의 경우 한 번 중복 전달되며 클라이언트가 etag로 걸러냄).
- **etag** — 8MB 이하 파일은 콘텐츠 해시라, 같은 크기로 덮어써도 변경이 감지됩니다. 그보다 큰 파일은 mtime+size를 씁니다.
- **삭제** — `delta`는 삭제를 담지 않습니다. 클라이언트가 주기적으로 `enumerate`로 재조정합니다.
- 모든 동기화 작업은 웹 UI와 동일한 **저장소 접근 권한·읽기 전용·용량 제한** 규칙을 따릅니다. 동시 쓰기도 안전합니다(요청별 임시 파일 + 원자적 교체).

---

## QR 로그인 (모바일 앱 연동)

향후 iOS/Android 앱에서 **서버 주소를 직접 입력하지 않고** QR 스캔만으로 로그인할 수 있도록 준비된 기능입니다. (앱은 아직 개발 전이지만 서버 측 연동 규격은 완성되어 있습니다.)

**흐름**

1. 웹 UI 로그인 후 헤더의 **📱 QR 로그인** 클릭 → 5분짜리 일회용 QR 표시
2. 앱이 QR을 스캔 → `gendisk://login?server=<서버주소>&token=<일회용토큰>` 획득 (서버 주소 + 토큰 동시 전달)
3. 앱이 `POST /api/auth/qr/redeem`으로 토큰 교환 → 30일짜리 세션 토큰 수령
4. 이후 앱은 모든 요청에 `Authorization: Bearer <세션토큰>` 헤더를 붙여 인증 (웹은 쿠키, 앱은 Bearer 둘 다 지원)

**앱 연동 예시**

```http
POST /api/auth/qr/redeem
Content-Type: application/json

{ "token": "<QR에서 얻은 토큰>" }
```

```json
{ "ok": true, "username": "hong", "session_token": "…", "token_type": "Bearer", "expires_days": 30 }
```

```http
GET /api/files/list?path=
Authorization: Bearer <session_token>
```

**보안 설계**

- 교환용 토큰은 **QR 이미지 안에만** 존재하고, 폴링·이미지 요청 URL에는 노출되지 않는 별도의 `handle`만 사용합니다 (토큰이 프록시/서버 로그에 남지 않음)
- 토큰은 **일회용** — 한 번 교환되면 즉시 무효, 5분 후 만료
- 비밀번호 변경/재설정 시 미사용 QR 토큰도 함께 무효화됩니다
- HTTPS 뒤에서 사용하는 것을 전제로 합니다 (앱은 `https://` 서버 주소로만 연결)

---

## Homepage 대시보드 위젯

[Homepage](https://gethomepage.dev)의 **Nextcloud 위젯**과 호환되는 `serverinfo` API를 제공하므로, Homepage 대시보드에 GenDisk를 그대로 띄울 수 있습니다 (CPU 부하·메모리·여유 공간·파일 수·공유 수·활성 사용자).

**1. 토큰 생성** — 웹 UI에서 **관리자 → 👥 사용자 관리 → "Homepage 위젯 토큰" → 생성**을 눌러 토큰을 만들고 복사합니다. (DB에 저장됩니다)

> 도커 환경변수 `GENDISK_SERVERINFO_TOKEN` 으로 지정해도 됩니다. 웹 UI에서 만든 토큰이 있으면 그쪽이 우선합니다.

**2. Homepage 위젯 등록** — `services.yaml` 에 `nextcloud` 위젯으로 추가:

```yaml
    - GenDisk:
        href: https://gendisk.example.com
        widget:
          type: nextcloud
          url: https://gendisk.example.com
          key: 원하는_랜덤_문자열      # 위 토큰과 동일
          # fields: ["cpuload", "memoryusage", "freespace", "numfiles", "numshares", "activeusers"]
```

- 토큰(`key`) 대신 **관리자 아이디/비밀번호**(`username`/`password`)를 써도 됩니다 (토큰 미설정 시).
- 내부적으로 `GET /ocs/v2.php/apps/serverinfo/api/v1/info` 를 Nextcloud OCS 형식으로 응답합니다. 인증(토큰 또는 관리자)이 없으면 정보를 전혀 노출하지 않습니다(401).
- CPU 부하·메모리는 Linux(도커) 기준으로 채워집니다.

---

## 업데이트 및 버전 관리

이미지는 GitHub Actions가 자동으로 빌드해 ghcr.io에 올리며, **버전은 main에 push할 때마다 자동으로 올라갑니다** (0.0.1 → 0.0.2 → 0.0.3 …):

| 태그 | 생성 시점 | 용도 |
|------|----------|------|
| `0.0.N` (자동 증가) | main push마다 패치 버전 +1, git 태그(`v0.0.N`)도 자동 생성 | 버전 고정 배포 |
| `latest` | main push마다 | 항상 최신 버전 추적 |
| `sha-XXXXXXX` | 커밋마다 | 특정 커밋 고정 |

```bash
# 서버 업데이트 (latest 사용 시)
docker compose pull && docker compose up -d

# 특정 버전 고정: compose.yaml에서
image: ghcr.io/nerdnam/gendisk.cloud:0.0.2
```

마이너/메이저 버전을 올리고 싶을 때만 수동 태그를 쓰면 됩니다:

```bash
git tag v0.1.0 && git push --tags   # → 0.1.0 빌드, 이후 main push는 0.1.1부터 증가
```

로컬에서 수동 빌드가 필요하면 [deploy.sh](deploy.sh)를 사용합니다 (`./deploy.sh` 빌드만, `./deploy.sh --push` 빌드 후 ghcr 푸시 — 사전에 `docker login ghcr.io` 필요).

---

## 프로젝트 구조

```
gendisk.cloud/
├── app/                      # FastAPI 백엔드
│   ├── main.py               #   앱 진입점, 라우터 등록, 정적 파일 서빙
│   ├── auth.py               #   계정 생성/로그인/세션/비밀번호 변경
│   ├── admin.py              #   관리자 전용 사용자 관리 API
│   ├── files.py              #   파일 목록/업로드/다운로드/썸네일/스트리밍/외부 저장소
│   ├── shares.py             #   외부 공유 링크 (생성·관리 + 공개 열람 엔드포인트)
│   └── database.py           #   SQLite 초기화 및 스키마 마이그레이션
├── static/                   # 웹 UI (프레임워크 없는 순수 HTML/CSS/JS)
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   ├── share.html            #   공개 공유 페이지 (/s/<토큰>)
│   └── share.js
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

의존성은 5개뿐입니다: `fastapi`, `uvicorn`, `python-multipart`, `pillow`, `qrcode`.

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
| POST | `/qr/create` | QR 로그인용 일회용 토큰 발급 (handle 반환) |
| GET | `/qr/image?handle=` | QR 이미지 PNG (교환 토큰은 이미지 안에만) |
| GET | `/qr/status?handle=` | QR 스캔·연결 상태 (pending/used/expired) |
| POST | `/qr/redeem` | (앱) QR 토큰을 세션으로 교환 — 인증 불필요, 일회용 |

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
| POST | `/move` | 파일·폴더를 다른 위치로 이동 (폴더 간 이동) |
| GET | `/usage` | 로그인 사용자의 개인 저장소 사용량·용량 제한 |

### 공유 `/api/shares` (로그인) · `/api/public/share` (공개)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| POST | `/api/shares/create` | 파일·폴더 공유 링크 생성 (`password`·`expires_days` 선택) |
| GET | `/api/shares/list` | 내가 만든 공유 목록 |
| POST | `/api/shares/revoke` | 공유 해제 (본인 것만) |
| GET | `/api/public/share/{token}` | (공개) 공유 메타 — 보호 여부·만료·이름 |
| POST | `/api/public/share/{token}/unlock` | (공개) 비밀번호 입력 → 접근 쿠키 발급 |
| GET | `/api/public/share/{token}/list?path=` | (공개) 폴더 공유 내용 |
| GET | `/api/public/share/{token}/download?path=` | (공개) 파일 다운로드 |
| GET | `/api/public/share/{token}/raw?path=` | (공개) 인라인 미리보기 (미디어만) |
| GET | `/api/public/share/{token}/thumb?path=` | (공개) 이미지 썸네일 |

### 관리자 `/api/admin` (관리자 세션 필요)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/users` | 사용자 목록 + 저장 공간 사용량 |
| POST | `/users` | 사용자 생성 (`is_admin` 선택) |
| POST | `/users/delete` | 사용자 삭제 (`delete_files`로 파일 처리 선택) |
| POST | `/users/reset-password` | 비밀번호 재설정 (세션 전체 만료) |
| POST | `/users/set-admin` | 관리자 지정/해제 |
| POST | `/users/quota` | 개인 저장소 용량 제한 설정 (bytes, 0=무제한) |
| GET | `/mounts` | 외부 저장소 목록 + 저장소별 접근 사용자 |
| POST | `/mounts/grant` | 한 저장소에 접근 가능한 사용자 목록 설정 |

### 대시보드 위젯 (Nextcloud 호환)

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/ocs/v2.php/apps/serverinfo/api/v1/info` | Nextcloud `serverinfo` 호환 — Homepage 위젯용 시스템/저장소/활성 사용자 정보. 인증: `NC-Token` 헤더(`GENDISK_SERVERINFO_TOKEN`) 또는 관리자 Basic ([위 상세](#homepage-대시보드-위젯)) |

전체 스키마는 서버 실행 후 `http://서버주소:8000/docs` (Swagger UI)에서 확인할 수 있습니다.

---

## 보안

- **비밀번호** — PBKDF2-HMAC-SHA256 300,000회 + 사용자별 랜덤 salt. 존재하지 않는 아이디도 동일한 시간이 걸리게 처리해 타이밍으로 계정 존재를 유추할 수 없습니다
- **세션** — 랜덤 토큰을 httponly + SameSite=Lax 쿠키(웹) 또는 `Authorization: Bearer`(앱)로 인증, 서버 측 DB에서 관리(만료 30일)
- **QR 페어링** — 교환 토큰은 QR 이미지 안에만 담겨 URL·로그에 노출되지 않으며, 일회용이고 비밀번호 변경 시 무효화됩니다
- **경로 격리** — 모든 파일 접근은 저장소 루트 기준으로 해석·검증됩니다. `../` 순회, 백슬래시, 심볼릭 링크를 통한 루트 밖 접근이 차단됩니다
- **권한 경계** — 관리자 API는 서버 측 세션 검증(`require_admin`), 읽기 전용 마운트는 API 차원에서 쓰기 403
- **외부 공유** — 링크 토큰은 추측 불가한 무작위 값이며 읽기 전용입니다. 선택적 비밀번호는 PBKDF2로 저장·레이트리밋되고, 공개 열람도 소유자 접근권으로 재검증됩니다. 실행 가능한 파일(html/svg 등)은 인라인 렌더 없이 다운로드로 강제(`nosniff`)해 같은 오리진 스크립트 실행을 막습니다 ([상세](#외부-공유-링크로-공유))

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
