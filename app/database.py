"""SQLite helpers: users and session tokens."""
import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "ncloud.db"
FILES_DIR = DATA_DIR / "files"
THUMBS_DIR = DATA_DIR / "thumbs"
# 도커에서 -v /호스트/경로:/app/mounts/<이름> 으로 연결한 외부 저장소가 노출되는 위치
MOUNTS_DIR = Path(os.environ.get("NCLOUD_MOUNTS_DIR", BASE_DIR / "mounts"))


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 락 대기(공개 공유의 잦은 읽기+접근시각 기록으로 쓰기 경합이 생김)와 동시성 개선.
    # WAL 은 로컬 볼륨(data/)에서만 유효하며, 한 번 설정되면 DB 파일에 지속된다.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    FILES_DIR.mkdir(exist_ok=True)
    THUMBS_DIR.mkdir(exist_ok=True)
    try:
        MOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # 읽기 전용 파일시스템 등 — 마운트 기능만 비활성화되면 됨
    conn = get_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                quota_bytes INTEGER NOT NULL DEFAULT 0,  -- 0 = 무제한, 개인 저장소에만 적용
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS mount_grants (
                -- 일반 사용자가 접근 가능한 외부 마운트 (관리자는 항상 전체 접근)
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                mount_name TEXT NOT NULL,
                PRIMARY KEY (user_id, mount_name)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL,
                last_seen TEXT                    -- 마지막 활동 시각 (활성 사용자 집계용)
            );
            CREATE TABLE IF NOT EXISTS qr_tokens (
                -- handle: 폴링/이미지 URL에 노출되는 비-교환용 식별자
                -- token: QR 이미지 안에만 들어가는 실제 교환 비밀 (URL에 절대 노출 안 됨)
                handle TEXT PRIMARY KEY,
                token TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                server TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shares (
                -- 외부 공유 링크 (읽기 전용). token 은 공개 URL(/s/<token>)에 들어가는
                -- 추측 불가한 무작위 값. space/path 는 "생성 시점 소유자 기준" 위치.
                token TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                space TEXT NOT NULL,
                path TEXT NOT NULL,               -- space 루트 기준 상대경로 (파일 또는 폴더)
                is_dir INTEGER NOT NULL,
                password_hash TEXT,               -- NULL = 비밀번호 없음
                salt TEXT,                        -- password_hash 있을 때만
                expires_at TEXT,                  -- NULL = 무기한 (ISO8601 UTC)
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_access_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_shares_owner ON shares(owner_id);
            CREATE TABLE IF NOT EXISTS share_unlocks (
                -- 비밀번호 공유를 푼 뒤 발급되는 단기 접근 토큰 (httponly 쿠키에 저장).
                -- 공유가 지워지면 함께 삭제된다.
                access_token TEXT PRIMARY KEY,
                share_token TEXT NOT NULL REFERENCES shares(token) ON DELETE CASCADE,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS share_items (
                -- 컬렉션 공유(여러 항목을 담은 링크 하나)의 구성 항목.
                -- 단일 항목 공유는 shares.path 를 그대로 쓰고 이 표를 쓰지 않는다.
                -- (컬렉션 공유는 shares.path 가 빈 문자열이고 여기에 항목들이 들어간다.)
                share_token TEXT NOT NULL REFERENCES shares(token) ON DELETE CASCADE,
                path TEXT NOT NULL,               -- space 루트 기준 상대경로
                is_dir INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_share_items_token ON share_items(share_token);
            CREATE TABLE IF NOT EXISTS settings (
                -- 관리자 설정 키-값 (예: Homepage 위젯 serverinfo 토큰)
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # 구버전 DB 마이그레이션: 누락된 컬럼 추가, 첫 사용자를 관리자로 승격
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "is_admin" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
            )
        if "quota_bytes" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN quota_bytes INTEGER NOT NULL DEFAULT 0"
            )
        scols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "last_seen" not in scols:
            conn.execute("ALTER TABLE sessions ADD COLUMN last_seen TEXT")
        no_admin = (
            conn.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin = 1").fetchone()["n"] == 0
        )
        if no_admin:
            conn.execute(
                "UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users)"
            )
        conn.commit()
    finally:
        conn.close()


# ---------- 설정 키-값 (관리자 설정) ----------

def get_setting(key: str) -> str | None:
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def delete_setting(key: str) -> None:
    conn = get_db()
    try:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()
