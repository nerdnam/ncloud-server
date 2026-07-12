"""Password hashing (PBKDF2) and cookie-session auth."""
import hashlib
import io
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

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


def _session_token(request: Request) -> str | None:
    """쿠키(웹) 또는 Authorization: Bearer(앱) 어느 쪽으로든 세션 토큰을 받는다."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def current_user(request: Request) -> dict:
    token = _session_token(request)
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


def verify_basic_auth(header: str | None) -> dict | None:
    """WebDAV 클라이언트가 보내는 HTTP Basic 인증을 검증한다 (아이디/비밀번호).
    성공 시 {id, username, is_admin} 반환, 실패 시 None."""
    if not header or not header.startswith("Basic "):
        return None
    import base64

    try:
        raw = base64.b64decode(header[6:]).decode("utf-8")
        username, _, password = raw.partition(":")
    except Exception:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, username, is_admin, password_hash, salt FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    salt = row["salt"] if row else _DUMMY_SALT
    attempt = hash_password(password, salt)
    if row is None or not secrets.compare_digest(row["password_hash"], attempt):
        return None
    return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"])}


def _create_session(conn, user_id: int, response: Response) -> str:
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
    return token


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
            (user["id"], _session_token(request)),
        )
        # 미사용 QR 페어링 토큰도 무효화 (비밀번호 변경으로 접근을 끊는 의미를 지키기 위해)
        conn.execute("DELETE FROM qr_tokens WHERE user_id = ?", (user["id"],))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.post("/logout")
def logout(request: Request, response: Response):
    token = _session_token(request)
    if token:
        conn = get_db()
        try:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


# ---------- QR 로그인 (모바일 앱 페어링) ----------
# 흐름: 웹(로그인 상태)에서 일회용 토큰 발급 → QR로 표시 → 앱이 스캔 후
# /qr/redeem으로 교환 → 해당 계정의 세션 토큰 획득 (Authorization: Bearer로 사용)

QR_TOKEN_MINUTES = 5


def _qr_content(server: str, token: str) -> str:
    return f"gendisk://login?server={quote(server, safe='')}&token={token}"


class QrCreate(BaseModel):
    server: str = Field(min_length=1, max_length=500)


@router.post("/qr/create")
def qr_create(body: QrCreate, request: Request):
    user = current_user(request)
    conn = get_db()
    try:
        now = _utcnow()
        conn.execute(
            "DELETE FROM qr_tokens WHERE expires_at < ?", (now.isoformat(),)
        )
        handle = secrets.token_urlsafe(24)  # URL/폴링용 (교환 불가)
        token = secrets.token_urlsafe(32)   # QR 안에만 들어가는 교환 비밀
        expires = now + timedelta(minutes=QR_TOKEN_MINUTES)
        conn.execute(
            "INSERT INTO qr_tokens (handle, token, user_id, server, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (handle, token, user["id"], body.server, expires.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    # 교환 토큰(token)은 응답에 넣지 않는다 — QR 이미지 안에만 존재한다
    return {"handle": handle, "expires_in": QR_TOKEN_MINUTES * 60}


@router.get("/qr/image")
def qr_image(handle: str, request: Request):
    user = current_user(request)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT server, token FROM qr_tokens "
            "WHERE handle = ? AND user_id = ? AND used = 0 AND expires_at >= ?",
            (handle, user["id"], _utcnow().isoformat()),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, "QR 토큰을 찾을 수 없습니다")
    import qrcode

    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(_qr_content(row["server"], row["token"]))
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, "PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/qr/status")
def qr_status(handle: str, request: Request):
    user = current_user(request)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT used, expires_at FROM qr_tokens WHERE handle = ? AND user_id = ?",
            (handle, user["id"]),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"status": "expired"}
    if row["used"]:
        return {"status": "used"}
    if datetime.fromisoformat(row["expires_at"]) < _utcnow():
        return {"status": "expired"}
    return {"status": "pending"}


class QrRedeem(BaseModel):
    token: str = Field(min_length=16, max_length=200)


@router.post("/qr/redeem")
def qr_redeem(body: QrRedeem, response: Response):
    """앱이 QR에서 얻은 일회용 토큰을 세션 토큰으로 교환한다 (인증 불필요, 1회용)."""
    conn = get_db()
    try:
        # 미사용·미만료 토큰만 원자적으로 사용 처리 (동시 요청 시 한쪽만 성공)
        cur = conn.execute(
            "UPDATE qr_tokens SET used = 1 WHERE token = ? AND used = 0 AND expires_at >= ?",
            (body.token, _utcnow().isoformat()),
        )
        if cur.rowcount == 0:
            conn.commit()
            raise HTTPException(401, "유효하지 않거나 만료된 QR 토큰입니다")
        row = conn.execute(
            """SELECT u.id, u.username FROM qr_tokens q
               JOIN users u ON u.id = q.user_id WHERE q.token = ?""",
            (body.token,),
        ).fetchone()
        if row is None:
            conn.commit()
            raise HTTPException(401, "유효하지 않거나 만료된 QR 토큰입니다")
        session_token = _create_session(conn, row["id"], response)
        return {
            "ok": True,
            "username": row["username"],
            "session_token": session_token,
            "token_type": "Bearer",
            "expires_days": SESSION_DAYS,
        }
    finally:
        conn.close()
