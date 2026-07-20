"""Sync API for desktop storage-provider clients (e.g. a macOS File Provider app).

Lets a sync client mirror ncloud like a third-party cloud service in Finder:
  - enumerate: full recursive snapshot of a space, each item with a content etag
  - delta: only items created/modified since a cursor (efficient polling)
  - put: create/overwrite a file at an exact path (sync writes, not auto-renamed)
  - download/mkdir/delete: reuse the existing /api/files endpoints

Every operation honors the same space-access, read-only, and quota rules as the
web UI. Deletions are not carried in `delta` (mtime can't reveal them); a client
should periodically re-`enumerate` to reconcile removals.

Cursors are wall-clock nanoseconds captured at the START of a walk. `delta`
returns everything modified at or after (cursor - SAFETY), so a modification is
never missed — at worst re-delivered once (harmless, the client re-checks the
etag). etags for files up to SYNC_HASH_MAX are content hashes, so a same-size
overwrite is still detected even on coarse-mtime filesystems.
"""
import asyncio
import contextlib
import hashlib
import os
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import events
from .auth import current_user
from .files import (
    HOME_SPACE,
    _fs_error,
    _upload_lock,
    _writable,
    dir_size,
    etag_of,
    file_kind,
    safe_path,
    space_root,
    user_quota,
    user_root,
)

router = APIRouter(prefix="/api/sync", tags=["sync"])

CHUNK = 1024 * 1024
# 커서가 놓칠 수 있는 구간(느슨한 mtime 정밀도·시계 오차)을 흡수하는 여유. 이 창 안의
# 변경은 중복 전달될 수 있으나(무해) 절대 누락되지 않는다.
SAFETY_NS = 2_000_000_000  # 2초
# 이 크기 이하 파일은 etag를 콘텐츠 해시로 계산해 같은 크기 덮어쓰기도 감지한다.
SYNC_HASH_MAX = 8 * 1024 * 1024


def sync_etag(path: Path, stat: os.stat_result) -> str:
    """동기화용 콘텐츠 버전. 작은 파일은 실제 내용 해시, 큰 파일은 mtime+size."""
    if stat.st_size <= SYNC_HASH_MAX:
        h = hashlib.blake2b(digest_size=16)
        try:
            with path.open("rb") as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
            return "h" + h.hexdigest()
        except OSError:
            pass  # 읽기 실패 시 아래 mtime+size로 폴백
    return "s" + etag_of(stat)


@router.get("/info")
def info(user: dict = Depends(current_user)):
    return {
        "server": "gendisk",
        "api": "sync/v1",
        "features": ["enumerate", "delta", "put", "download", "events"],
        "chunk_size": CHUNK,
        "user": user["username"],
    }


def _walk_space(base: Path, root: Path, min_mtime_ns: int = 0):
    """base 하위를 재귀적으로 훑으며, mtime_ns >= min_mtime_ns 인 항목을 yield.
    심볼릭 링크는 따라가지 않는다."""
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        d = Path(dirpath)
        for name in dirnames:
            p = d / name
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime_ns >= min_mtime_ns:
                yield {
                    "id": p.relative_to(root).as_posix(),
                    "name": name,
                    "path": p.relative_to(root).as_posix(),
                    "is_dir": True,
                    "size": 0,
                    "mtime": int(st.st_mtime),
                    "etag": None,
                }
        for name in filenames:
            p = d / name
            if p.is_symlink():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime_ns >= min_mtime_ns:
                yield {
                    "id": p.relative_to(root).as_posix(),
                    "name": name,
                    "path": p.relative_to(root).as_posix(),
                    "is_dir": False,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "etag": sync_etag(p, st),
                    "kind": file_kind(p),
                }


@router.get("/events")
async def change_events(request: Request, user: dict = Depends(current_user)):
    """파일 변경 실시간 알림(SSE).

    변경 즉시 `data: {"space", "dir"}` 이벤트를 보내고, 25초마다 핑(코멘트)을
    보내 프록시(Cloudflare 등)의 유휴 타임아웃을 피한다. 클라이언트는 이벤트를
    받으면 해당 폴더를 재열거한다 — 주기 폴링 없는 실시간 동기화.
    """
    q = events.subscribe(user["id"])

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=25.0)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": ping\n\n"
                    continue
                yield f"data: {payload}\n\n"
        finally:
            events.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/enumerate")
async def enumerate_space(
    space: str = HOME_SPACE, path: str = "", user: dict = Depends(current_user)
):
    """공간 전체(또는 하위 폴더)를 재귀적으로 나열하고 현재 상태 커서를 반환한다."""
    root = space_root(user, space)  # 접근 권한 검증 포함
    base = safe_path(user, path, space)
    if not base.is_dir():
        raise HTTPException(404, "폴더를 찾을 수 없습니다")

    started = time.time_ns()  # 커서는 '훑기 시작 시각' — 이후 변경을 놓치지 않기 위함

    def collect():
        return list(_walk_space(base, root))

    items = await run_in_threadpool(collect)
    return {
        "space": space or HOME_SPACE,
        "path": base.relative_to(root).as_posix() if base != root else "",
        "cursor": str(started),
        "items": items,
    }


@router.get("/delta")
async def delta(
    space: str = HOME_SPACE, cursor: str = "0", user: dict = Depends(current_user)
):
    """커서 이후에 생성·수정된 항목만 반환한다 (삭제는 재열거로 확인)."""
    root = space_root(user, space)
    try:
        since = max(0, int(cursor) - SAFETY_NS)
    except ValueError:
        since = 0

    started = time.time_ns()

    def collect():
        return list(_walk_space(root, root, min_mtime_ns=since))

    changed = await run_in_threadpool(collect)
    return {
        "space": space or HOME_SPACE,
        "cursor": str(started),
        "changed": changed,
        "deletions_tracked": False,
    }


@router.post("/put")
async def put(
    request: Request,
    space: str = HOME_SPACE,
    path: str = "",
    user: dict = Depends(current_user),
):
    """정확한 경로에 파일을 생성/덮어쓴다 (동기화 업로드). 부모 폴더는 자동 생성."""
    rel = path.strip("/")
    if not rel:
        raise HTTPException(400, "파일 경로가 필요합니다")
    root = space_root(user, space)
    target = safe_path(user, path, space)
    if target == root or target.is_dir():
        raise HTTPException(409, "폴더가 있는 경로에는 파일을 쓸 수 없습니다")

    is_home = space in ("", HOME_SPACE)
    if not is_home and not _writable(root):
        raise HTTPException(403, "읽기 전용 저장소입니다")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _fs_error(exc)

    quota = user_quota(user["id"]) if is_home else 0
    guard = _upload_lock(user["id"]) if quota > 0 else contextlib.nullcontext()
    async with guard:
        # 덮어쓰기면 기존 파일 크기는 용량 계산에서 제외
        existing = target.stat().st_size if target.exists() else 0
        used = (
            await run_in_threadpool(dir_size, user_root(user)) - existing
            if quota > 0
            else 0
        )
        # 요청마다 고유한 임시 파일 (동시 put이 서로의 임시 파일을 덮어쓰지 않도록)
        try:
            fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".ncup-")
        except OSError as exc:
            raise _fs_error(exc)
        tmp = Path(tmp_name)
        written = 0
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    if quota > 0 and used + written + len(chunk) > quota:
                        raise HTTPException(413, "용량 제한을 초과했습니다")
                    out.write(chunk)
                    written += len(chunk)
            os.replace(tmp, target)  # 같은 폴더 내 원자적 교체
        except HTTPException:
            tmp.unlink(missing_ok=True)
            raise
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise _fs_error(exc)

    st = target.stat()
    events.notify_change(space or HOME_SPACE, events.parent_of(rel),
                         user["id"], private=is_home)
    return {
        "ok": True,
        "path": target.relative_to(root).as_posix(),
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "etag": sync_etag(target, st),
    }
