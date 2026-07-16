"""외부 공유 링크 (읽기 전용).

소유자는 로그인 상태에서 파일/폴더에 대한 공유 링크(/s/<token>)를 만들고, 링크만
아는 외부 사용자는 로그인 없이 그 안을 열람·다운로드할 수 있다. 링크에는 선택적으로
비밀번호와 만료일을 걸 수 있다.

보안 요점:
  * token 은 secrets.token_urlsafe(32) — 추측 불가.
  * 공개 열람 시에도 소유자의 신원으로 space 를 다시 확인하므로, 마운트 접근이
    회수되거나 소유자가 삭제되면 링크도 죽는다(404).
  * 경로는 항상 공유 루트 하위로 가둔다(디렉토리 트래버설 차단). 심볼릭 링크는
    목록/제공 모두에서 배제해 공유 밖을 가리키지 못하게 한다.
  * 공개 제공(raw/download)은 실행 가능한 타입(html/svg 등)을 절대 인라인하지 않고
    첨부(attachment)+octet-stream 으로 강제하며 X-Content-Type-Options: nosniff 를 붙인다
    (같은 오리진 XSS 방지).
  * 비밀번호 언락은 레이트리밋으로 PBKDF2 CPU 소진/온라인 무차별 대입을 막는다.
"""
import hashlib
import io
import mimetypes
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import files as files_mod
from .auth import current_user, hash_password
from .database import THUMBS_DIR, get_db

router = APIRouter(prefix="/api/shares", tags=["shares"])
public_router = APIRouter(prefix="/api/public/share", tags=["public-share"])

SHARE_COOKIE = "gd_share"
UNLOCK_HOURS = 12
MIN_SHARE_PASSWORD = 4
MAX_LIST_ENTRIES = 20000          # 공개 목록이 무한정 커지지 않도록 상한
THUMB_SIZE = 256

# 인라인 렌더를 허용할 미디어 접두사. 그 외(html/svg/pdf/txt…)는 첨부로 강제한다.
_SAFE_INLINE = ("image/", "video/", "audio/")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _cookie_path(token: str) -> str:
    return f"/api/public/share/{token}"


def _base_name(rel_path: str, space: str) -> str:
    name = rel_path.rstrip("/").split("/")[-1]
    return name or space


# ---------- 언락 레이트리밋 (인메모리) ----------
# 비밀번호 언락은 요청마다 PBKDF2(30만 회)를 돌리므로, 인증 없는 공개 엔드포인트에서
# 무제한 호출되면 CPU/스레드풀을 소진시키거나 약한 비밀번호를 무차별 대입할 수 있다.
_rl_lock = threading.Lock()
_rl: dict[str, list[float]] = {}


def _rl_hit(key: str, maxn: int, window: float) -> bool:
    """key 버킷에 이번 시도를 기록하고, window 초 내 maxn 이하이면 True(허용)."""
    now = time.monotonic()
    with _rl_lock:
        times = [t for t in _rl.get(key, ()) if now - t < window]
        allowed = len(times) < maxn
        if allowed:
            times.append(now)
        _rl[key] = times
        return allowed


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------- 공유 조회/검증 헬퍼 ----------

def _get_share(conn, token: str):
    return conn.execute("SELECT * FROM shares WHERE token = ?", (token,)).fetchone()


def _expired(share) -> bool:
    return bool(share["expires_at"]) and datetime.fromisoformat(share["expires_at"]) < _utcnow()


def _load_owner(conn, owner_id: int):
    row = conn.execute(
        "SELECT id, username, is_admin FROM users WHERE id = ?", (owner_id,)
    ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"])}


def _owner_base(share) -> Path:
    """소유자 신원으로 해석한 space 루트. 소유자가 없거나 마운트 접근권이 회수됐으면 404."""
    conn = get_db()
    try:
        owner = _load_owner(conn, share["owner_id"])
    finally:
        conn.close()
    if owner is None:
        raise HTTPException(404, "공유를 찾을 수 없습니다")
    # space_root 가 마운트 존재/소유자 접근권을 강제한다 (없거나 회수됐으면 404)
    return files_mod.space_root(owner, share["space"])


def _is_collection(share) -> bool:
    """여러 항목을 담은 컬렉션 공유인지 (path 가 비어 있으면 컬렉션)."""
    return not (share["path"] or "")


def _share_root_path(share) -> Path:
    """단일 공유 대상(파일 또는 폴더)의 실제 경로. 소유자 신원으로 space 를 다시 확인한다."""
    base = _owner_base(share)
    root = (base / (share["path"] or "").strip("/").replace("\\", "/")).resolve()
    # 재확인: 공유 루트는 반드시 space 루트 하위여야 한다
    if root != base and base not in root.parents:
        raise HTTPException(404, "공유를 찾을 수 없습니다")
    if not root.exists() or root.is_symlink():
        raise HTTPException(404, "공유 대상이 더 이상 존재하지 않습니다")
    return root


def _resolve_within(root: Path, sub: str) -> Path:
    """공유 루트 하위로 가둔 경로 해석 (트래버설 차단)."""
    target = (root / sub.strip("/").replace("\\", "/")).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(400, "잘못된 경로입니다")
    return target


def _collection_items(share) -> list[dict]:
    """컬렉션 공유의 항목들을 소유자 space 기준으로 해석. 존재하고 심링크 아닌 것만."""
    base = _owner_base(share)  # 소유자/마운트 접근권 재확인 (없으면 404)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT path FROM share_items WHERE share_token = ?", (share["token"],)
        ).fetchall()
    finally:
        conn.close()
    items = []
    for r in rows:
        rel = (r["path"] or "").strip("/").replace("\\", "/")
        if not rel:
            continue
        p = (base / rel).resolve()
        if p != base and base not in p.parents:
            continue  # 방어: 공유 루트 밖이면 제외
        if p.is_symlink() or not p.exists():
            continue
        items.append({"name": p.name, "real": p})
    return items


def _resolve_collection(share, path: str):
    """컬렉션 공개 path 를 (항목 실제 루트, 하위경로) 로 해석.
    path='' 면 (None, '') = 컬렉션 루트 자체."""
    p = (path or "").strip("/").replace("\\", "/")
    if not p:
        return None, ""
    first, _, rest = p.partition("/")
    for it in _collection_items(share):
        if it["name"] == first:
            return it["real"], rest
    raise HTTPException(404, "항목을 찾을 수 없습니다")


def _list_entries(target: Path, prefix: str):
    """target 폴더를 나열. 각 항목 path 는 공개 상대경로(prefix + 이름)."""
    entries = []
    truncated = False
    try:
        with os.scandir(target) as it:
            for de in it:
                if de.is_symlink():
                    continue  # 공유 밖을 가리킬 수 있는 심링크는 노출하지 않는다
                if len(entries) >= MAX_LIST_ENTRIES:
                    truncated = True
                    break
                child = Path(de.path)
                try:
                    info = files_mod.entry_info(child, child.parent)
                except OSError:
                    continue  # stat 불가 항목은 조용히 건너뜀
                info["path"] = f"{prefix}/{info['name']}" if prefix else info["name"]
                entries.append(info)
    except OSError as exc:
        raise files_mod._fs_error(exc)
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return entries, truncated


def _resolve_public_file(share, path: str) -> Path:
    """다운로드/미리보기 대상 파일 해석 (단일·컬렉션 공통, 트래버설 차단)."""
    if _is_collection(share):
        item_root, sub = _resolve_collection(share, path)
        if item_root is None:
            raise HTTPException(404, "파일을 찾을 수 없습니다")
        target = _resolve_within(item_root, sub)
    else:
        root = _share_root_path(share)
        target = _resolve_within(root, path) if bool(share["is_dir"]) else root
    if target.is_symlink() or not target.is_file():
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    return target


def _is_unlocked(conn, request: Request, token: str, share) -> bool:
    if not share["password_hash"]:
        return True
    access = request.cookies.get(SHARE_COOKIE)
    if not access:
        return False
    row = conn.execute(
        "SELECT expires_at FROM share_unlocks WHERE access_token = ? AND share_token = ?",
        (access, token),
    ).fetchone()
    if row is None:
        return False
    return datetime.fromisoformat(row["expires_at"]) >= _utcnow()


def _require_access(conn, request: Request, token: str):
    """유효한 공유인지(존재·미만료·필요시 언락) 확인하고 share 반환.
    (컬렉션은 단일 루트가 없으므로 루트 해석은 각 엔드포인트가 직접 한다.)"""
    share = _get_share(conn, token)
    if share is None:
        raise HTTPException(404, "공유를 찾을 수 없습니다")
    if _expired(share):
        raise HTTPException(410, "만료된 공유입니다")
    if share["password_hash"] and not _is_unlocked(conn, request, token, share):
        raise HTTPException(401, "비밀번호가 필요합니다")
    return share


def _touch(token: str) -> None:
    """마지막 접근 시각 기록(부가 정보). 실패(락 등)해도 요청을 깨뜨리지 않는다."""
    try:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE shares SET last_access_at = ? WHERE token = ?",
                (_utcnow().isoformat(), token),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # 접근 기록은 실패해도 무시 (다운로드/목록 응답을 500으로 만들지 않기 위해)


def _public_serve(target: Path, download: bool) -> FileResponse:
    """공개 제공용 파일 응답. 실행 가능한 타입은 인라인하지 않는다 (같은 오리진 XSS 방지)."""
    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    # 인라인은 실제 미디어(래스터 이미지/영상/오디오)만. svg 는 스크립트를 품을 수 있어 제외.
    inline_ok = (not download) and media.startswith(_SAFE_INLINE) and media != "image/svg+xml"
    disposition = "inline" if inline_ok else "attachment"
    serve_media = media if inline_ok else "application/octet-stream"
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(target.name)}",
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(target, media_type=serve_media, headers=headers)


def drop_shares_for(owner_id: int, space: str, path: str) -> None:
    """대상 파일/폴더가 삭제·이름변경·이동될 때 그 경로(및 하위)의 공유를 제거한다.

    경로 문자열에 묶인 공유가 삭제 후 같은 경로에 생긴 다른 파일을 가리키는 것을 막는다.
    (home 저장소는 사용자별이라 owner_id 로 스코프해야 다른 사용자의 동일 상대경로 공유를
    잘못 지우지 않는다.)
    """
    rel = (path or "").strip("/").replace("\\", "/")
    if not rel:
        return
    try:
        conn = get_db()
        try:
            # 1) 단일 공유 제거
            conn.execute(
                "DELETE FROM shares WHERE owner_id = ? AND space = ? AND (path = ? OR path LIKE ?)",
                (owner_id, space, rel, rel + "/%"),
            )
            # 2) 컬렉션에서 해당 경로(및 하위) 항목 제거
            conn.execute(
                "DELETE FROM share_items WHERE (path = ? OR path LIKE ?) AND share_token IN "
                "(SELECT token FROM shares WHERE owner_id = ? AND space = ?)",
                (rel, rel + "/%", owner_id, space),
            )
            # 3) 항목이 모두 사라진 컬렉션 공유 제거
            conn.execute(
                "DELETE FROM shares WHERE owner_id = ? AND space = ? AND path = '' "
                "AND token NOT IN (SELECT DISTINCT share_token FROM share_items)",
                (owner_id, space),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # 정리는 최선노력 — 실패해도 삭제/이동 자체는 성공시킨다


# ---------- 소유자용 (로그인 필요) ----------

class ShareCreate(BaseModel):
    space: str = files_mod.HOME_SPACE
    path: str | None = Field(default=None, min_length=1)      # 단일 (구버전 호환)
    paths: list[str] | None = None                            # 다중 (컬렉션)
    password: str | None = Field(default=None, max_length=256)
    expires_days: int | None = Field(default=None, ge=1, le=3650)


@router.post("/create")
def create_share(body: ShareCreate, user: dict = Depends(current_user)):
    # 단일(path) 또는 다중(paths) 어느 쪽이든 받는다. paths 가 있으면 우선.
    raw = list(body.paths) if body.paths else ([body.path] if body.path else [])
    if not raw:
        raise HTTPException(400, "공유할 대상이 없습니다")

    # safe_path/space_root 가 트래버설 + 소유자의 space 접근권을 강제한다
    root = files_mod.space_root(user, body.space)
    resolved = []          # [(rel, is_dir)]
    seen = set()
    for p in raw:
        target = files_mod.safe_path(user, p, body.space)
        if target == root:
            raise HTTPException(400, "저장소 루트는 공유할 수 없습니다")
        if not target.exists():
            raise HTTPException(404, f"대상을 찾을 수 없습니다: {p}")
        rel = target.relative_to(root).as_posix()
        if rel in seen:
            continue  # 중복 경로는 한 번만
        # 컬렉션은 항목 이름(basename)으로 공개 경로를 만들므로 이름이 겹치면 안 된다
        if target.name in {r.rsplit("/", 1)[-1] for r, _ in resolved}:
            raise HTTPException(400, "같은 이름의 항목은 함께 공유할 수 없습니다")
        seen.add(rel)
        resolved.append((rel, target.is_dir()))

    token = secrets.token_urlsafe(32)
    password_hash = salt = None
    if body.password:
        if len(body.password) < MIN_SHARE_PASSWORD:
            raise HTTPException(400, f"비밀번호는 {MIN_SHARE_PASSWORD}자 이상이어야 합니다")
        salt = secrets.token_hex(16)
        password_hash = hash_password(body.password, salt)
    expires_at = None
    if body.expires_days:
        expires_at = (_utcnow() + timedelta(days=body.expires_days)).isoformat()

    is_collection = len(resolved) > 1
    if is_collection:
        share_path, share_is_dir = "", 0            # 컬렉션 마커: path 비움
        name = f"{len(resolved)}개 항목"
        is_dir = True
    else:
        share_path, sdir = resolved[0]
        share_is_dir = 1 if sdir else 0
        name = _base_name(share_path, body.space)
        is_dir = sdir

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO shares (token, owner_id, space, path, is_dir, password_hash, salt, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (token, user["id"], body.space, share_path, share_is_dir, password_hash, salt, expires_at),
        )
        if is_collection:
            conn.executemany(
                "INSERT INTO share_items (share_token, path, is_dir) VALUES (?, ?, ?)",
                [(token, rel, 1 if d else 0) for rel, d in resolved],
            )
        conn.commit()
    finally:
        conn.close()
    return {
        "token": token,
        "path": f"/s/{token}",
        "name": name,
        "is_dir": is_dir,
        "collection": is_collection,
        "count": len(resolved),
        "protected": bool(password_hash),
        "expires_at": expires_at,
    }


@router.get("/list")
def list_shares(user: dict = Depends(current_user)):
    now = _utcnow()
    out = []
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT token, space, path, is_dir, (password_hash IS NOT NULL) AS protected, "
            "expires_at, created_at, last_access_at FROM shares "
            "WHERE owner_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
        for r in rows:
            expired = bool(r["expires_at"]) and datetime.fromisoformat(r["expires_at"]) < now
            collection = not (r["path"] or "")
            if collection:
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM share_items WHERE share_token = ?", (r["token"],)
                ).fetchone()["n"]
                name = f"{count}개 항목"
                is_dir = True
            else:
                count = 1
                name = _base_name(r["path"], r["space"])
                is_dir = bool(r["is_dir"])
            out.append({
                "token": r["token"],
                "space": r["space"],
                "path": r["path"],
                "name": name,
                "is_dir": is_dir,
                "collection": collection,
                "count": count,
                "protected": bool(r["protected"]),
                "expires_at": r["expires_at"],
                "expired": expired,
                "created_at": r["created_at"],
                "last_access_at": r["last_access_at"],
            })
    finally:
        conn.close()
    return {"shares": out}


class ShareToken(BaseModel):
    token: str = Field(min_length=1, max_length=200)


@router.post("/revoke")
def revoke_share(body: ShareToken, user: dict = Depends(current_user)):
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM shares WHERE token = ? AND owner_id = ?", (body.token, user["id"])
        )
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "공유를 찾을 수 없습니다")
    return {"ok": True}


# ---------- 공개 (로그인 불필요) ----------

class UnlockBody(BaseModel):
    password: str = Field(max_length=256)


@public_router.get("/{token}")
def share_meta(token: str, request: Request):
    conn = get_db()
    try:
        share = _get_share(conn, token)
        if share is None:
            raise HTTPException(404, "공유를 찾을 수 없습니다")
        if _expired(share):
            raise HTTPException(410, "만료된 공유입니다")
        protected = bool(share["password_hash"])
        unlocked = _is_unlocked(conn, request, token, share)
    finally:
        conn.close()
    resp = {"protected": protected, "unlocked": unlocked, "expires_at": share["expires_at"]}
    # 비밀번호가 걸려 있고 아직 안 풀렸으면 이름/종류조차 노출하지 않는다.
    # 노출 전에는 소유자 접근권/대상 존재를 재확인해 죽은 공유는 이름도 안 준다.
    if unlocked or not protected:
        if _is_collection(share):
            items = _collection_items(share)   # 접근 불가면 404
            resp["name"] = f"{len(items)}개 항목"
            resp["is_dir"] = True
            resp["collection"] = True
        else:
            _share_root_path(share)            # 접근 불가/대상 소멸이면 404 로 끝남
            resp["name"] = _base_name(share["path"], share["space"])
            resp["is_dir"] = bool(share["is_dir"])
            resp["collection"] = False
    return resp


@public_router.post("/{token}/unlock")
def share_unlock(token: str, body: UnlockBody, request: Request, response: Response):
    conn = get_db()
    try:
        share = _get_share(conn, token)
        if share is None:
            raise HTTPException(404, "공유를 찾을 수 없습니다")
        if _expired(share):
            raise HTTPException(410, "만료된 공유입니다")
        if not share["password_hash"]:
            return {"ok": True}  # 비밀번호 없는 공유 — 언락 불필요

        # 레이트리밋: 해시 계산 전에 IP·토큰 단위로 시도 횟수를 제한한다
        ip = _client_ip(request)
        if not _rl_hit(f"ip:{ip}:{token}", 5, 60.0) or not _rl_hit(f"tok:{token}", 20, 60.0):
            raise HTTPException(429, "시도가 너무 많습니다. 잠시 후 다시 시도하세요.")

        attempt = hash_password(body.password, share["salt"])
        if not secrets.compare_digest(share["password_hash"], attempt):
            raise HTTPException(401, "비밀번호가 올바르지 않습니다")

        # 만료된 언락 토큰 정리 (테이블 무한 증가 방지)
        conn.execute("DELETE FROM share_unlocks WHERE expires_at < ?", (_utcnow().isoformat(),))
        access = secrets.token_urlsafe(32)
        exp = _utcnow() + timedelta(hours=UNLOCK_HOURS)
        if share["expires_at"]:
            exp = min(exp, datetime.fromisoformat(share["expires_at"]))
        conn.execute(
            "INSERT INTO share_unlocks (access_token, share_token, expires_at) VALUES (?, ?, ?)",
            (access, token, exp.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    response.set_cookie(
        SHARE_COOKIE,
        access,
        max_age=UNLOCK_HOURS * 3600,
        path=_cookie_path(token),
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}


@public_router.get("/{token}/list")
def share_list(token: str, request: Request, path: str = ""):
    conn = get_db()
    try:
        share = _require_access(conn, request, token)
    finally:
        conn.close()

    if _is_collection(share):
        item_root, sub = _resolve_collection(share, path)
        if item_root is None:
            # 컬렉션 루트: 담긴 항목들을 나열
            items = _collection_items(share)
            entries = []
            for it in items:
                try:
                    entries.append(files_mod.entry_info(it["real"], it["real"].parent))
                except OSError:
                    continue
            entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
            _touch(token)
            return {"name": f"{len(items)}개 항목", "path": "", "entries": entries,
                    "truncated": False, "collection": True}
        # 항목 안 폴더 나열
        target = _resolve_within(item_root, sub)
        if not target.is_dir():
            raise HTTPException(404, "폴더를 찾을 수 없습니다")
        entries, truncated = _list_entries(target, path)
        _touch(token)
        return {"name": target.name, "path": path, "entries": entries, "truncated": truncated}

    # 단일 공유
    if not bool(share["is_dir"]):
        raise HTTPException(400, "폴더 공유가 아닙니다")
    root = _share_root_path(share)
    target = _resolve_within(root, path)
    if not target.is_dir():
        raise HTTPException(404, "폴더를 찾을 수 없습니다")
    rel = target.relative_to(root).as_posix() if target != root else ""
    entries, truncated = _list_entries(target, rel)
    _touch(token)
    return {"name": root.name, "path": rel, "entries": entries, "truncated": truncated}


@public_router.get("/{token}/download")
def share_download(token: str, request: Request, path: str = ""):
    conn = get_db()
    try:
        share = _require_access(conn, request, token)
    finally:
        conn.close()
    target = _resolve_public_file(share, path)
    resp = _public_serve(target, download=True)
    _touch(token)
    return resp


@public_router.get("/{token}/raw")
def share_raw(token: str, request: Request, path: str = ""):
    conn = get_db()
    try:
        share = _require_access(conn, request, token)
    finally:
        conn.close()
    target = _resolve_public_file(share, path)
    resp = _public_serve(target, download=False)
    _touch(token)
    return resp


@public_router.get("/{token}/thumb")
def share_thumb(token: str, request: Request, path: str = ""):
    conn = get_db()
    try:
        share = _require_access(conn, request, token)
    finally:
        conn.close()
    target = _resolve_public_file(share, path)
    if files_mod.file_kind(target) != "image":
        raise HTTPException(404, "썸네일을 만들 수 없습니다")
    stat = target.stat()
    # 캐시 키는 (토큰, 공개 경로)로 잡아 파일마다 고유하게 한다 (단일·컬렉션 공통).
    digest = hashlib.sha256(f"share:{token}\0{path}".encode()).hexdigest()[:32]
    cache_file = THUMBS_DIR / f"{digest}_{stat.st_mtime_ns}_{stat.st_size}.webp"
    if not cache_file.exists():
        try:
            from PIL import Image, ImageOps

            with Image.open(target) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((THUMB_SIZE, THUMB_SIZE))
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGB")
                buf = io.BytesIO()
                im.save(buf, "WEBP", quality=80)
            cache_file.write_bytes(buf.getvalue())
        except Exception:
            raise HTTPException(415, "지원하지 않는 이미지입니다")
    return Response(
        cache_file.read_bytes(),
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )
