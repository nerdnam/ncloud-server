# gendisk-sync (Windows 클라이언트)

GenDisk 서버에 연결하는 Windows용 프로그램. 두 가지 방식으로 씁니다.

1. **일반 디스크처럼 사용** — WebDAV 네트워크 드라이브로 연결하면 탐색기에서 드라이브 문자(예: `N:`)로 나타나 바로 열고 저장합니다 (온디맨드, 전체 복사 없음).
2. **폴더 동기화** — 지정한 로컬 폴더를 서버 저장소와 양방향 자동 동기화합니다 (원할 때만 켜는 선택 기능).

## 받기 / 실행

- **바로 받기**: GenDisk 웹 UI의 "⬇ Windows 앱" 버튼, 또는 [GitHub Releases](https://github.com/nerdnam/gendisk.cloud/releases)의 `gendisk-sync-<버전>.exe` (파이썬 불필요, 더블클릭 실행).
- **소스로 실행** (파이썬 설치 시):

```
python main.py            # GUI
python main.py --startup  # 자동 시작(최소화) — 자동 로그인/드라이브 연결 수행
python main.py --once     # 저장된 설정으로 한 번만 동기화 (자동화용)
```

## 시작 옵션 (자동화)

- **로그인 정보 저장** — 비밀번호를 Windows DPAPI로 암호화해 저장합니다 (현재 Windows 사용자만 복호화 가능, 평문 저장 안 함).
- **Windows 시작 시 자동 실행** — 로그인 시 자동으로 프로그램을 띄웁니다 (레지스트리 Run 키, 최소화 상태).
- **프로그램 시작 시 자동 로그인** — 저장된 정보로 자동 로그인합니다.
- **자동 로그인 후 드라이브 자동 연결** — 로그인 후 WebDAV 드라이브를 자동 연결합니다.

이 옵션들을 켜고 "설정 저장"하면, 이후 PC를 켤 때마다 **자동 실행 → 자동 로그인 → 드라이브 연결 → 자동 동기화**까지 설정한 대로 동작합니다. 세션이 만료돼도 저장된 정보로 조용히 재로그인합니다.

## 동기화 동작

- **양방향** — 로컬/원격 어느 쪽에서 만들고·수정·삭제해도 반영됩니다.
- **충돌 안전** — 같은 파일을 양쪽에서 서로 다르게 고치면, 로컬본을 `이름 (conflict 시각).확장자` 로 보존하고 원격본을 받아옵니다. 데이터를 잃지 않습니다.
- **상태 추적** — 로컬 폴더의 `.gendisk\state.json` 에 마지막 동기화 상태를 저장해 신규/수정/삭제를 구분합니다.
- 서버의 저장소 접근 권한·용량 제한을 그대로 따릅니다.

## .exe 빌드

```
build.bat
```

PyInstaller로 `dist\gendisk-sync.exe` (약 11MB, 단독 실행) 를 만듭니다.

## 구조

```
win_x64/
  main.py                    진입점 (GUI / --startup / --once)
  gendisk_sync/
    client.py                서버 HTTP 클라이언트 (표준 라이브러리만)
    engine.py                양방향 동기화 엔진
    config.py                설정 (%APPDATA%\gendisk-sync\config.json)
    secret.py                비밀번호 DPAPI 암호화 저장
    autostart.py             Windows 시작 시 자동 실행 등록 (레지스트리)
    app.py                   tkinter GUI + 백그라운드 동기화 루프
    webdav_mount.py          WNetAddConnection2W 로 WebDAV 드라이브 연결/해제
  gendisk-sync.spec          PyInstaller 스펙
  build.bat                  빌드 스크립트
```

## 참고

- 런타임 의존성 없음 (urllib·tkinter 등 표준 라이브러리만). PyInstaller는 빌드에만 필요합니다.
- WebDAV 드라이브 연결은 **HTTPS 서버**를 권장합니다. 평문 HTTP면 Windows WebClient가 기본적으로 Basic 인증을 막습니다.
- 드라이브 연결 시 **Windows 'WebClient' 서비스**가 실행 중이어야 합니다 (프로그램이 자동 시작을 시도하지만, 서비스가 사용 안 함으로 설정돼 있으면 서비스 관리자에서 수동/자동으로 바꿔야 합니다).
