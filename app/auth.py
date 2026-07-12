"""Password hashing (PBKDF2) and cookie-session auth."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from .database import FILES_DIR, get_db

SESSION_COOKIE = "ncloud_session"
SESSION_DAYS = 30
PBKDF2_ITERATIONS = 300_000
# 존재하지 않는 아이디도 같은 시간이 걸리게 해 타이밍으로 계정 존재를 유추하지 못하게 한다
_DUMMY_SALT = secrets.token_hex(16)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS
    ).hex()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=4, max_length=256)

    @field_validator("username")
    @classmethod
    def no_dot_only_names(cls, v: str) -> str:
        # 점으로만 된 이름(".", "..")은 디렉토리 경로로 해석되므로 금지
        if v.strip(".") == "":
            raise ValueError("사용할 수 없는 아이디입니다")
        return v


def current_user(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "로그인이 필요합니다")
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT u.id, u.username, u.is_admin, s.expires_at FROM sessions s
               JOIN users u ON u.id = s.user_id WHERE s.token = ?""",
            (token,),
        ).fetchone()
        if row is None:
            raise HTTPException(401, "세션이 유효하지 않습니다")
        expires = datetime.fromisoformat(row["expires_at"])
        if expires < _utcnow():
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            raise HTTPException(401, "세션이 만료되었습니다")
        return {
            "id": row["id"],
            "username": row["username"],
            "is_admin": bool(row["is_admin"]),
        }
    finally:
        conn.close()


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "관리자 권한이 필요합니다")
    return user


def _create_session(conn, user_id: int, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    expires = _utcnow() + timedelta(days=SESSION_DAYS)
    conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires.isoformat()),
    )
    conn.commit()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
    )


@router.get("/status")
def status(request: Request):
    conn = get_db()
    try:
        has_users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] > 0
    finally:
        conn.close()
    user = None
    try:
        user = current_user(request)
    except HTTPException:
        pass
    return {"setup_needed": not has_users, "user": user}


@router.post("/setup")
def setup(creds: Credentials, response: Response):
    """Create the first (admin) account. Only allowed when no users exist."""
    conn = get_db()
    try:
        salt = secrets.token_hex(16)
        # WHERE NOT EXISTS로 검사와 삽입을 한 문장으로 묶어, 동시에 들어온
        # 두 요청이 모두 관리자 계정을 만드는 경쟁을 차단한다
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, salt, is_admin) "
            "SELECT ?, ?, ?, 1 WHERE NOT EXISTS (SELECT 1 FROM users)",
            (creds.username, hash_password(creds.password, salt), salt),
        )
        if cur.rowcount == 0:
            raise HTTPException(403, "이미 계정이 존재합니다")
        (FILES_DIR / creds.username).mkdir(parents=True, exist_ok=True)
        _create_session(conn, cur.lastrowid, response)
        return {"ok": True, "username": creds.username}
    finally:
        conn.close()


@router.post("/login")
def login(creds: Credentials, response: Response):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, password_hash, salt FROM users WHERE username = ?",
            (creds.username,),
        ).fetchone()
        salt = row["salt"] if row else _DUMMY_SALT
        attempt = hash_password(creds.password, salt)
        if row is None or not secrets.compare_digest(row["password_hash"], attempt):
            raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다")
        _create_session(conn, row["id"], response)
        return {"ok": True, "username": creds.username}
    finally:
        conn.close()


class ChangePassword(BaseModel):
    current_password: str
    new_password: str = Field(min_length=4, max_length=256)


@router.post("/change-password")
def change_password(body: ChangePassword, request: Request):
    user = current_user(request)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT password_hash, salt FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        if not secrets.compare_digest(
            row["password_hash"], hash_password(body.current_password, row["salt"])
        ):
            raise HTTPException(403, "현재 비밀번호가 올바르지 않습니다")
        salt = secrets.token_hex(16)
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
            (hash_password(body.new_password, salt), salt, user["id"]),
        )
        # 현재 세션만 남기고 다른 기기의 세션은 모두 로그아웃
        conn.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token != ?",
            (user["id"], request.cookies.get(SESSION_COOKIE)),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        conn = get_db()
        try:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}
