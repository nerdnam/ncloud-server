"""Admin-only user management."""
import secrets
import shutil
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import Credentials, hash_password, require_admin
from .database import FILES_DIR, get_db
from .files import dir_size, list_mounts

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/users")
def list_users(admin: dict = Depends(require_admin)):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, username, is_admin, quota_bytes, created_at FROM users ORDER BY id"
        ).fetchall()
        grant_rows = conn.execute("SELECT user_id, mount_name FROM mount_grants").fetchall()
    finally:
        conn.close()
    grants: dict[int, list[str]] = {}
    for g in grant_rows:
        grants.setdefault(g["user_id"], []).append(g["mount_name"])
    users = []
    for row in rows:
        home = FILES_DIR / row["username"]
        users.append(
            {
                "id": row["id"],
                "username": row["username"],
                "is_admin": bool(row["is_admin"]),
                "created_at": row["created_at"],
                "usage_bytes": dir_size(home) if home.is_dir() else 0,
                "quota_bytes": row["quota_bytes"],
                "mounts": sorted(grants.get(row["id"], [])),
            }
        )
    # 저장소 권한 UI가 체크박스로 보여줄 전체 마운트 목록
    return {"users": users, "available_mounts": [m.name for m in list_mounts()]}


class NewUser(Credentials):
    is_admin: bool = False


@router.post("/users")
def create_user(body: NewUser, admin: dict = Depends(require_admin)):
    # 같은 아이디가 재사용될 때 이전 사용자의 파일이 새 계정에 노출되지 않도록,
    # 남아 있는 홈 디렉토리는 계정 생성 전에 보관 폴더로 옮겨 둔다.
    # ('@'는 아이디에 쓸 수 없는 문자라 실제 사용자 홈과 충돌하지 않는다)
    home = FILES_DIR / body.username
    try:
        if home.is_dir() and any(home.iterdir()):
            home.rename(FILES_DIR / f"{body.username}@archived-{time.time_ns()}")
    except OSError:
        raise HTTPException(500, "이전 사용자의 파일을 보관하지 못해 계정을 만들 수 없습니다")
    conn = get_db()
    try:
        salt = secrets.token_hex(16)
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, salt, is_admin) VALUES (?, ?, ?, ?)",
                (body.username, hash_password(body.password, salt), salt, int(body.is_admin)),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "이미 존재하는 아이디입니다")
        conn.commit()
    finally:
        conn.close()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # 실패해도 첫 파일 작업 때 user_root()가 다시 생성한다
    return {"ok": True}


def _get_target(conn, user_id: int):
    row = conn.execute(
        "SELECT id, username, is_admin FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "사용자를 찾을 수 없습니다")
    return row


class DeleteUser(BaseModel):
    user_id: int
    delete_files: bool = True


@router.post("/users/delete")
def delete_user(body: DeleteUser, admin: dict = Depends(require_admin)):
    if body.user_id == admin["id"]:
        raise HTTPException(400, "자기 자신은 삭제할 수 없습니다")
    conn = get_db()
    try:
        target = _get_target(conn, body.user_id)
        # 삭제 후에도 관리자가 1명 이상 남는 경우에만 삭제한다 (동시 요청 경쟁 방지:
        # 조건을 DELETE 문 안에 넣어 검사와 삭제가 원자적으로 수행되게 한다)
        cur = conn.execute(
            """DELETE FROM users WHERE id = ?
               AND (is_admin = 0
                    OR (SELECT COUNT(*) FROM users u2
                        WHERE u2.is_admin = 1 AND u2.id != ?) >= 1)""",
            (body.user_id, body.user_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            still = conn.execute(
                "SELECT 1 FROM users WHERE id = ?", (body.user_id,)
            ).fetchone()
            if still:
                raise HTTPException(400, "마지막 관리자는 삭제할 수 없습니다")
            raise HTTPException(404, "사용자를 찾을 수 없습니다")
    finally:
        conn.close()
    if body.delete_files:
        home = FILES_DIR / target["username"]
        if home.is_dir():
            shutil.rmtree(home, ignore_errors=True)
    return {"ok": True}


class ResetPassword(BaseModel):
    user_id: int
    new_password: str = Field(min_length=4, max_length=256)


@router.post("/users/reset-password")
def reset_password(body: ResetPassword, admin: dict = Depends(require_admin)):
    conn = get_db()
    try:
        target = _get_target(conn, body.user_id)
        salt = secrets.token_hex(16)
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
            (hash_password(body.new_password, salt), salt, body.user_id),
        )
        # 강제 재로그인 (본인 비밀번호를 재설정한 경우 현재 세션은 change-password를 쓰므로 전부 삭제해도 무방)
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (body.user_id,))
        # 미사용 QR 페어링 토큰도 무효화
        conn.execute("DELETE FROM qr_tokens WHERE user_id = ?", (body.user_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "username": target["username"]}


class SetAdmin(BaseModel):
    user_id: int
    is_admin: bool


@router.post("/users/set-admin")
def set_admin(body: SetAdmin, admin: dict = Depends(require_admin)):
    if body.user_id == admin["id"]:
        raise HTTPException(400, "자기 자신의 관리자 권한은 바꿀 수 없습니다")
    conn = get_db()
    try:
        _get_target(conn, body.user_id)
        if body.is_admin:
            conn.execute(
                "UPDATE users SET is_admin = 1 WHERE id = ?", (body.user_id,)
            )
            conn.commit()
        else:
            # 해제 후에도 관리자가 1명 이상 남는 경우에만 해제 (원자적 검사)
            cur = conn.execute(
                """UPDATE users SET is_admin = 0 WHERE id = ?
                   AND (SELECT COUNT(*) FROM users u2
                        WHERE u2.is_admin = 1 AND u2.id != ?) >= 1""",
                (body.user_id, body.user_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(400, "마지막 관리자의 권한은 해제할 수 없습니다")
    finally:
        conn.close()
    return {"ok": True}


class SetQuota(BaseModel):
    user_id: int
    quota_bytes: int = Field(ge=0)  # 0 = 무제한


@router.post("/users/quota")
def set_quota(body: SetQuota, admin: dict = Depends(require_admin)):
    conn = get_db()
    try:
        _get_target(conn, body.user_id)
        conn.execute(
            "UPDATE users SET quota_bytes = ? WHERE id = ?",
            (body.quota_bytes, body.user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


class SetMounts(BaseModel):
    user_id: int
    mounts: list[str]


@router.post("/users/mounts")
def set_mounts(body: SetMounts, admin: dict = Depends(require_admin)):
    """사용자가 접근 가능한 외부 마운트 목록을 통째로 교체한다."""
    valid = {m.name for m in list_mounts()}
    requested = {m for m in body.mounts if m in valid}  # 존재하는 마운트만 반영
    conn = get_db()
    try:
        _get_target(conn, body.user_id)
        conn.execute("DELETE FROM mount_grants WHERE user_id = ?", (body.user_id,))
        conn.executemany(
            "INSERT INTO mount_grants (user_id, mount_name) VALUES (?, ?)",
            [(body.user_id, name) for name in sorted(requested)],
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "mounts": sorted(requested)}
