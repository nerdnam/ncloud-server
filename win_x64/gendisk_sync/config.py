"""설정 저장/불러오기 (%APPDATA%\\gendisk-sync\\config.json)."""
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass

from . import secret

_save_lock = threading.Lock()


def config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "gendisk-sync")
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


@dataclass
class Config:
    server_url: str = ""
    username: str = ""
    token: str = ""                 # 세션 토큰
    password_enc: str = ""          # DPAPI로 암호화된 비밀번호 (평문 저장 안 함)
    space: str = "home"
    local_folder: str = ""
    interval_sec: int = 30
    enabled: bool = False           # 자동 동기화 활성 여부
    # 시작 동작
    save_credentials: bool = False  # 로그인 정보(비밀번호) 저장
    auto_start: bool = False        # Windows 시작 시 자동 실행
    auto_login: bool = False        # 프로그램 시작 시 자동 로그인
    auto_connect_drive: bool = False  # 자동 로그인 후 드라이브 자동 연결
    drive_letter: str = "N:"

    @classmethod
    def load(cls) -> "Config":
        try:
            with open(config_path(), encoding="utf-8") as f:
                data = json.load(f)
            known = {k: data[k] for k in asdict(cls()) if k in data}
            return cls(**known)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return cls()

    def save(self):
        # 원자적 쓰기(임시파일 + os.replace) + 잠금 — 동시 저장·중단 시 설정 손상 방지
        with _save_lock:
            d = config_dir()
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".cfg-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(asdict(self), f, ensure_ascii=False, indent=2)
                os.replace(tmp, config_path())
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def is_ready(self) -> bool:
        return bool(self.server_url and self.token and self.local_folder)

    # ---------- 비밀번호 (DPAPI) ----------
    def set_password(self, password: str):
        enc = secret.encrypt(password) if password else None
        self.password_enc = enc or ""

    def get_password(self) -> str:
        return secret.decrypt(self.password_enc) or "" if self.password_enc else ""

    def clear_password(self):
        self.password_enc = ""
