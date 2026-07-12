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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL
            );
            """
        )
        # 구버전 DB 마이그레이션: is_admin 컬럼이 없으면 추가하고 첫 사용자를 관리자로 승격
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "is_admin" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
            )
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
