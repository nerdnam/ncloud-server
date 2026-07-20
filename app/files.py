"""File/folder management, download, thumbnails, media streaming.

Every endpoint takes a "space": "home" is the user's private storage
(data/files/<username>); any other value is an external mount — a directory
that appears under MOUNTS_DIR (in Docker: -v /host/path:/app/mounts/<name>).
"""
import asyncio
import contextlib
import errno
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from .auth import current_user
from .database import FILES_DIR, MOUNTS_DIR, THUMBS_DIR, get_db

router = APIRouter(prefix="/api/files", tags=["files"])

# 진행 중인 청크 업로드 임시 저장소 (data/uploads). data/files 와 같은 볼륨이라
# 완료 시 os.replace 로 원자적 이동이 가능하다.
UPLOADS_DIR = FILES_DIR.parent / "uploads"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a"}
THUMB_SIZE = 256

HOME_SPACE = "home"
SPACE_NAME_RE = re.compile(r"^[A-Za-z0-9가-힣 _.-]{1,64}$")


def user_root(user: dict) -> Path:
    root = FILES_DIR / user["username"]
    root.mkdir(parents=True, exist_ok=True)
    return root


def _valid_space_name(name: str) -> bool:
    return bool(SPACE_NAME_RE.match(name)) and name.strip(". ") != ""


def list_mounts() -> list[Path]:
    if not MOUNTS_DIR.is_dir():
        return []
    return sorted(
        (p for p in MOUNTS_DIR.iterdir() if p.is_dir() and _valid_space_name(p.name)),
        key=lambda p: p.name.lower(),
    )


def dir_size(path: Path) -> int:
    """디렉토리 안 모든 파일의 총 바이트 수 (심볼릭 링크는 따라가지 않음)."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for name in files:
            fp = os.path.join(root, name)
            if os.path.islink(fp):
                continue
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def granted_mounts(user_id: int) -> set[str]:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT mount_name FROM mount_grants WHERE user_id = ?", (user_id,)
        ).fetchall()
    finally:
        conn.close()
    return {r["mount_name"] for r in rows}


def accessible_mounts(user: dict) -> list[Path]:
    """관리자는 모든 마운트, 일반 사용자는 부여받은 마운트만."""
    mounts = list_mounts()
    if user["is_admin"]:
        return mounts
    allowed = granted_mounts(user["id"])
    return [m for m in mounts if m.name in allowed]


def _can_access_mount(user: dict, name: str) -> bool:
    if user["is_admin"]:
        return True
    return name in granted_mounts(user["id"])


def space_root(user: dict, space: str) -> Path:
    if space in ("", HOME_SPACE):
        return user_root(user).resolve()
    # 정규식이 경로 구분자를 차단하고, 점으로만 된 이름("..", "...")을 별도로 거른다.
    # 마운트 디렉토리 안의 심볼릭 링크는 관리자가 만든 것이므로 따라가도 안전하다.
    if not _valid_space_name(space):
        raise HTTPException(404, "저장소를 찾을 수 없습니다")
    candidate = MOUNTS_DIR / space
    if not candidate.is_dir():
        raise HTTPException(404, "저장소를 찾을 수 없습니다")
    # 권한 없는 사용자에게는 저장소 존재 자체를 숨긴다 (404)
    if not _can_access_mount(user, space):
        raise HTTPException(404, "저장소를 찾을 수 없습니다")
    return candidate.resolve()


def safe_path(user: dict, rel: str, space: str = HOME_SPACE) -> Path:
    """Resolve a client-supplied relative path, refusing traversal outside the space root."""
    root = space_root(user, space)
    target = (root / rel.strip("/").replace("\\", "/")).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(400, "잘못된 경로입니다")
    return target


def file_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return "file"


def etag_of(stat: os.stat_result) -> str:
    """콘텐츠 버전 식별자 — 파일이 바뀌면 값이 달라진다 (동기화 변경 감지용)."""
    return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"


def entry_info(path: Path, root: Path, de: "os.DirEntry | None" = None) -> dict:
    # de(os.scandir 의 DirEntry)가 있으면 열거 때 이미 캐시된 stat/is_dir 을 재사용해
    # 파일당 stat syscall 2번을 아낀다(폴더 목록 응답 지연의 큰 부분). 없으면 직접 stat.
    if de is not None:
        stat = de.stat()
        is_dir = de.is_dir()
    else:
        stat = path.stat()
        is_dir = path.is_dir()
    return {
        "name": path.name,
        "path": path.relative_to(root).as_posix(),
        "is_dir": is_dir,
        "size": 0 if is_dir else stat.st_size,
        "mtime": int(stat.st_mtime),
        "kind": "folder" if is_dir else file_kind(path),
        "etag": None if is_dir else etag_of(stat),
    }


def _writable(path: Path) -> bool:
    return os.access(path, os.W_OK)


def _fs_error(exc: OSError) -> HTTPException:
    if isinstance(exc, PermissionError) or exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM):
        return HTTPException(403, "읽기 전용이거나 권한이 없는 저장소입니다")
    if exc.errno == errno.ENOENT:
        return HTTPException(404, "대상을 찾을 수 없습니다")
    if exc.errno == errno.ENOTDIR:
        return HTTPException(400, "잘못된 경로입니다")
    if exc.errno == errno.EEXIST:
        return HTTPException(409, "이미 존재하는 이름입니다")
    return HTTPException(500, f"파일 시스템 오류: {exc.strerror or '알 수 없는 오류'}")


@router.get("/spaces")
def spaces(user: dict = Depends(current_user)):
    result = [{"id": HOME_SPACE, "name": "내 파일", "readonly": False}]
    for mount in accessible_mounts(user):
        result.append(
            {"id": mount.name, "name": mount.name, "readonly": not _writable(mount)}
        )
    return {"spaces": result}


@router.get("/list")
def list_dir(path: str = "", space: str = HOME_SPACE, user: dict = Depends(current_user)):
    target = safe_path(user, path, space)
    if not target.is_dir():
        raise HTTPException(404, "폴더를 찾을 수 없습니다")
    root = space_root(user, space)
    rel_base = target.relative_to(root).as_posix() if target != root else ""
    try:
        with os.scandir(target) as it:
            dirents = list(it)
    except OSError as exc:
        raise _fs_error(exc)
    entries = []
    for de in dirents:
        try:
            entries.append(entry_info(Path(de.path), root, de=de))
        except OSError:
            # stat 이 막혀도(권한, 깨진 링크, 마운트 문제 등) 이름은 보여준다.
            # 통째로 숨기면 클라이언트에서 "폴더가 비어 보이는" 원인 불명 증상이 된다.
            try:
                is_dir = de.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            entries.append({
                "name": de.name,
                "path": f"{rel_base}/{de.name}" if rel_base else de.name,
                "is_dir": is_dir,
                "size": 0,
                "mtime": 0,
                "kind": "folder" if is_dir else file_kind(Path(de.name)),
                "etag": None,
            })
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return {
        "space": space or HOME_SPACE,
        "path": target.relative_to(root).as_posix() if target != root else "",
        "entries": entries,
    }


def user_quota(user_id: int) -> int:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT quota_bytes FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    return row["quota_bytes"] if row else 0


# 사용자별 업로드 잠금: 같은 사용자의 동시 업로드가 용량 검사를 직렬화하도록 해
# 스냅샷 경쟁(TOCTOU)으로 용량 제한을 우회하는 것을 막는다 (용량 제한이 있을 때만 사용)
_upload_locks: dict[int, asyncio.Lock] = {}


def _upload_lock(user_id: int) -> asyncio.Lock:
    lock = _upload_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _upload_locks[user_id] = lock
    return lock


@router.post("/upload")
async def upload(
    files: list[UploadFile],
    path: str = "",
    space: str = HOME_SPACE,
    paths: list[str] | None = Form(None),
    user: dict = Depends(current_user),
):
    """파일 업로드. paths[i] 가 있으면 그 상대경로(하위폴더 포함)를 보존한다(폴더 업로드).
    없으면 파일명만 사용(일반 파일 업로드)."""
    target_dir = safe_path(user, path, space)
    if not target_dir.is_dir():
        raise HTTPException(404, "폴더를 찾을 수 없습니다")

    # 용량 제한은 개인 저장소(home)에만 적용 (외부 마운트는 공유 저장소)
    is_home = space in ("", HOME_SPACE)
    quota = user_quota(user["id"]) if is_home else 0

    # 용량 제한이 있으면 사용자별 잠금으로 검사+쓰기를 직렬화 (동시 업로드 우회 방지)
    guard = _upload_lock(user["id"]) if quota > 0 else contextlib.nullcontext()
    async with guard:
        # dir_size는 트리 전체를 훑으므로 이벤트 루프를 막지 않도록 스레드풀에서 실행
        used = await run_in_threadpool(dir_size, user_root(user)) if quota > 0 else 0

        saved = []
        made_dirs: set[Path] = set()
        for i, f in enumerate(files):
            # 클라이언트가 준 상대경로(폴더 업로드) 또는 파일명. traversal 성분(.. 등)은 제거.
            rel = (paths[i] if paths and i < len(paths) else "") or (f.filename or "")
            parts = [p for p in rel.replace("\\", "/").split("/")
                     if p and p not in (".", "..")]
            if not parts:
                continue
            name = parts[-1]
            subdir = "/".join(parts[:-1])
            try:
                if subdir:
                    base_dir = safe_path(user, f"{path}/{subdir}", space)
                    if base_dir not in made_dirs:
                        base_dir.mkdir(parents=True, exist_ok=True)
                        made_dirs.add(base_dir)
                else:
                    base_dir = target_dir
            except OSError as exc:
                raise _fs_error(exc)
            dest = base_dir / name
            # avoid overwriting: append (1), (2), ...
            counter = 1
            while dest.exists():
                dest = base_dir / f"{Path(name).stem} ({counter}){Path(name).suffix}"
                counter += 1
            try:
                # 스트리밍으로 저장하면서 실시간으로 용량 제한을 확인한다
                written = 0
                with dest.open("wb") as out:
                    while True:
                        chunk = await f.read(1024 * 1024)
                        if not chunk:
                            break
                        if quota > 0 and used + written + len(chunk) > quota:
                            out.close()
                            dest.unlink(missing_ok=True)
                            raise HTTPException(
                                413,
                                f"용량 제한({_fmt_size(quota)})을 초과했습니다. "
                                f"'{name}'을(를) 저장할 수 없습니다.",
                            )
                        out.write(chunk)
                        written += len(chunk)
            except OSError as exc:
                dest.unlink(missing_ok=True)
                raise _fs_error(exc)
            used += written
            saved.append(dest.relative_to(target_dir).as_posix())
    return {"saved": saved}


# ---------- 청크(분할) 업로드 ----------
# 큰 파일(수십 GB)을 조각으로 나눠 올려, 앞단(리버스 프록시·Cloudflare)의 요청당 크기 제한과
# 단일 요청 타임아웃을 우회한다. 서버는 임시 .part 에 이어붙였다가 완료 시 원자적으로 옮긴다.

class UploadInit(BaseModel):
    space: str = HOME_SPACE
    path: str = ""
    rel: str = ""            # 하위폴더 포함 상대경로 또는 파일명 (폴더 구조 보존)
    size: int = 0            # 총 크기(선택, 표시용)
    overwrite: bool = False  # True면 같은 경로를 원자적으로 덮어쓴다(카메라 백업 등 멱등 재시도용)


def _session_files(upload_id: str) -> tuple[Path, Path]:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", upload_id)[:64]
    if not safe:
        raise HTTPException(400, "잘못된 업로드 ID")
    return UPLOADS_DIR / f"{safe}.part", UPLOADS_DIR / f"{safe}.json"


def _load_session(upload_id: str, user: dict) -> tuple[dict, Path, Path]:
    part, meta_p = _session_files(upload_id)
    if not meta_p.exists() or not part.exists():
        raise HTTPException(404, "업로드 세션을 찾을 수 없습니다")
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(404, "업로드 세션을 찾을 수 없습니다")
    if meta.get("user_id") != user["id"]:
        raise HTTPException(403, "권한이 없습니다")
    return meta, part, meta_p


def _cleanup_sessions(max_age_h: int = 24) -> None:
    try:
        cutoff = time.time() - max_age_h * 3600
        for f in UPLOADS_DIR.glob("*"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


@router.post("/upload/init")
def upload_init(body: UploadInit, user: dict = Depends(current_user)):
    """청크 업로드 세션 시작 → upload_id 발급."""
    space = body.space or HOME_SPACE
    root = space_root(user, space)               # 접근 검증
    target_dir = safe_path(user, body.path, space)
    if not target_dir.is_dir():
        raise HTTPException(404, "폴더를 찾을 수 없습니다")
    if space not in ("", HOME_SPACE) and not _writable(root):
        raise HTTPException(403, "읽기 전용 저장소입니다")
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_sessions()
    upload_id = secrets.token_urlsafe(24)
    part, meta_p = _session_files(upload_id)
    part.touch()
    meta_p.write_text(json.dumps({
        "user_id": user["id"], "space": space, "path": body.path,
        "rel": body.rel, "size": int(body.size or 0),
        "overwrite": bool(body.overwrite),
    }), encoding="utf-8")
    return {"upload_id": upload_id}


@router.get("/upload/status")
def upload_status(upload_id: str, user: dict = Depends(current_user)):
    """지금까지 서버가 받은 바이트 수 (재개용)."""
    _, part, _ = _load_session(upload_id, user)
    return {"received": part.stat().st_size}


@router.put("/upload/chunk")
async def upload_chunk(request: Request, upload_id: str, offset: int = 0,
                       user: dict = Depends(current_user)):
    """조각을 이어붙인다. offset 은 현재 받은 크기와 같아야 한다(불일치 409 → 재동기화)."""
    _, part, _ = _load_session(upload_id, user)
    cur = part.stat().st_size
    if offset != cur:
        raise HTTPException(409, f"offset 불일치 (현재 {cur})")
    try:
        with part.open("ab") as out:
            async for chunk in request.stream():
                out.write(chunk)
    except OSError as exc:
        _truncate_part(part, cur)
        raise _fs_error(exc)
    except BaseException:
        # 스트림 중단(모바일 연결 끊김 등)·취소로 조각을 끝까지 못 받으면 되돌린다.
        _truncate_part(part, cur)
        raise
    return {"received": part.stat().st_size}


def _truncate_part(part: Path, size: int) -> None:
    """조각 파일을 마지막으로 온전했던 지점(size)으로 되돌린다. 부분 바이트가 남으면
    클라이언트가 offset==현재크기 로 재동기화할 수 없어 업로드가 막히므로, 실패 시 항상 호출."""
    try:
        with part.open("r+b") as f:
            f.truncate(size)
    except OSError:
        pass


@router.post("/upload/complete")
def upload_complete(upload_id: str, user: dict = Depends(current_user)):
    """마무리: 용량 검사 후 대상 위치로 원자적 이동(이름 겹치면 (1) 회피)."""
    meta, part, meta_p = _load_session(upload_id, user)
    space, path, rel = meta["space"], meta["path"], meta.get("rel", "")
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p not in (".", "..")]
    if not parts:
        part.unlink(missing_ok=True); meta_p.unlink(missing_ok=True)
        raise HTTPException(400, "파일 이름이 없습니다")
    name = parts[-1]
    subdir = "/".join(parts[:-1])
    root = space_root(user, space)
    if space not in ("", HOME_SPACE) and not _writable(root):
        part.unlink(missing_ok=True); meta_p.unlink(missing_ok=True)
        raise HTTPException(403, "읽기 전용 저장소입니다")
    target_dir = safe_path(user, f"{path}/{subdir}" if subdir else path, space)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        part.unlink(missing_ok=True); meta_p.unlink(missing_ok=True)
        raise _fs_error(exc)
    is_home = space in ("", HOME_SPACE)
    quota = user_quota(user["id"]) if is_home else 0
    if quota > 0:
        used = dir_size(user_root(user))
        if used + part.stat().st_size > quota:
            part.unlink(missing_ok=True); meta_p.unlink(missing_ok=True)
            raise HTTPException(413, f"용량 제한({_fmt_size(quota)})을 초과했습니다.")
    dest = target_dir / name
    if not meta.get("overwrite"):
        # 기본: 이름이 겹치면 " (1)"로 회피(절대 덮어쓰지 않음). overwrite=True면 이 회피를
        # 건너뛰어 os.replace 가 같은 경로를 원자적으로 덮어쓴다(카메라 백업의 멱등 재시도).
        counter = 1
        while dest.exists():
            dest = target_dir / f"{Path(name).stem} ({counter}){Path(name).suffix}"
            counter += 1
    try:
        os.replace(part, dest)      # data/uploads → data/files: 같은 볼륨, 원자적 이동
    except OSError as exc:
        part.unlink(missing_ok=True); meta_p.unlink(missing_ok=True)
        raise _fs_error(exc)
    meta_p.unlink(missing_ok=True)
    return {"saved": dest.relative_to(root).as_posix()}


def _fmt_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    v = float(n)
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.0f} {units[i]}" if i == 0 else f"{v:.1f} {units[i]}"


class PathBody(BaseModel):
    path: str
    space: str = HOME_SPACE


class RenameBody(BaseModel):
    path: str
    new_name: str
    space: str = HOME_SPACE


@router.post("/mkdir")
def mkdir(body: PathBody, user: dict = Depends(current_user)):
    target = safe_path(user, body.path, body.space)
    if target == space_root(user, body.space):
        raise HTTPException(400, "폴더 이름을 입력하세요")
    if target.exists():
        raise HTTPException(409, "이미 존재하는 이름입니다")
    try:
        target.mkdir(parents=True)
    except OSError as exc:
        raise _fs_error(exc)
    return {"ok": True}


@router.post("/delete")
def delete(body: PathBody, user: dict = Depends(current_user)):
    target = safe_path(user, body.path, body.space)
    if target == space_root(user, body.space):
        raise HTTPException(400, "루트 폴더는 삭제할 수 없습니다")
    if not target.exists():
        raise HTTPException(404, "대상을 찾을 수 없습니다")
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        raise _fs_error(exc)
    # 삭제된 경로(및 하위)에 걸린 공유 링크가 나중에 같은 경로의 다른 파일을 가리키지 않도록 제거
    from . import shares
    shares.drop_shares_for(user["id"], body.space, body.path)
    return {"ok": True}


@router.post("/rename")
def rename(body: RenameBody, user: dict = Depends(current_user)):
    target = safe_path(user, body.path, body.space)
    if target == space_root(user, body.space):
        raise HTTPException(400, "루트 폴더의 이름은 바꿀 수 없습니다")
    if not target.exists():
        raise HTTPException(404, "대상을 찾을 수 없습니다")
    new_name = Path(body.new_name).name
    if not new_name or new_name in (".", ".."):
        raise HTTPException(400, "잘못된 이름입니다")
    dest = target.parent / new_name
    if dest.exists():
        raise HTTPException(409, "이미 존재하는 이름입니다")
    try:
        target.rename(dest)
    except OSError as exc:
        raise _fs_error(exc)
    # 이름이 바뀌면 이전 경로의 공유는 대상을 잃으므로 제거
    from . import shares
    shares.drop_shares_for(user["id"], body.space, body.path)
    return {"ok": True}


class MoveBody(BaseModel):
    src: str
    dst: str
    space: str = HOME_SPACE
    src_space: str | None = None   # 없으면 space — 저장소 간 이동 지원
    dst_space: str | None = None   # 없으면 space


# 모든 이동을 직렬화해, "대상 존재 확인 → 이동" 사이의 경쟁으로 파일이 조용히
# 덮어써지는 것을 막는다 (공유 마운트라 사용자별 잠금으로는 부족해 전역 잠금 사용).
# 이동은 대부분 즉시 끝나는 메타데이터 연산이라 전역 직렬화 비용이 미미하다.
_move_lock = asyncio.Lock()


@router.post("/move")
async def move(body: MoveBody, user: dict = Depends(current_user)):
    """파일/폴더를 다른 위치로 이동한다 (폴더 간 + 저장소 간 이동 포함).
    src_space/dst_space 를 다르게 주면 저장소 간 이동(shutil.move 가 볼륨 넘어가면 복사+삭제)."""
    ss = body.src_space or body.space
    ds = body.dst_space or body.space
    src_root = space_root(user, ss)
    dst_root = space_root(user, ds)
    src = safe_path(user, body.src, ss)
    dst = safe_path(user, body.dst, ds)
    if src == src_root:
        raise HTTPException(400, "루트 폴더는 이동할 수 없습니다")
    if not src.exists():
        raise HTTPException(404, "대상을 찾을 수 없습니다")
    if dst == dst_root:
        raise HTTPException(400, "잘못된 대상 경로입니다")
    if ds not in ("", HOME_SPACE) and not _writable(dst_root):
        raise HTTPException(403, "읽기 전용 저장소입니다")
    # 폴더를 자기 자신 또는 자기 하위로 이동하는 것을 막는다
    if src.is_dir() and (dst == src or src in dst.parents):
        raise HTTPException(400, "폴더를 자기 하위로 이동할 수 없습니다")

    def _do_move():
        dst.parent.mkdir(parents=True, exist_ok=True)
        # shutil.move는 같은 파일시스템이면 원자적 rename, 아니면 복사+삭제로 폴백
        shutil.move(str(src), str(dst))

    async with _move_lock:
        if dst.exists():  # 잠금 안에서 다시 확인 → 이 확인과 이동 사이에 경쟁 없음
            raise HTTPException(409, "이미 존재하는 이름입니다")
        try:
            await run_in_threadpool(_do_move)
        except OSError as exc:
            raise _fs_error(exc)
    # 이동하면 원래 경로의 공유는 대상을 잃으므로 제거
    from . import shares
    shares.drop_shares_for(user["id"], ss, body.src)
    return {"ok": True, "path": dst.relative_to(dst_root).as_posix()}


@router.get("/usage")
async def usage(user: dict = Depends(current_user)):
    """로그인한 사용자의 개인 저장소 사용량과 용량 제한 (여유 공간 표시용)."""
    used = await run_in_threadpool(dir_size, user_root(user))
    return {"usage_bytes": used, "quota_bytes": user_quota(user["id"])}


def _serve_file(path: Path, download: bool) -> FileResponse:
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    disposition = "attachment" if download else "inline"
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(path.name)}"
    }
    return FileResponse(path, media_type=media_type, headers=headers)


@router.get("/download")
def download(path: str, space: str = HOME_SPACE, user: dict = Depends(current_user)):
    target = safe_path(user, path, space)
    if not target.is_file():
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    return _serve_file(target, download=True)


@router.get("/raw")
def raw(path: str, space: str = HOME_SPACE, user: dict = Depends(current_user)):
    """Inline serving for previews; FileResponse supports HTTP Range for video seeking."""
    target = safe_path(user, path, space)
    if not target.is_file():
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    return _serve_file(target, download=False)


@router.get("/thumb")
def thumbnail(path: str, space: str = HOME_SPACE, user: dict = Depends(current_user)):
    target = safe_path(user, path, space)
    if not target.is_file() or file_kind(target) != "image":
        raise HTTPException(404, "썸네일을 만들 수 없습니다")
    stat = target.stat()
    root = space_root(user, space)
    rel = target.relative_to(root).as_posix()
    scope = f"u{user['id']}" if space in ("", HOME_SPACE) else f"m:{space}"
    # 해시로 키를 만들어 (scope, 경로) 쌍이 서로 충돌하지 않게 한다
    digest = hashlib.sha256(f"{scope}\0{rel}".encode()).hexdigest()[:32]
    cache_key = f"{digest}_{stat.st_mtime_ns}_{stat.st_size}.webp"
    cache_file = THUMBS_DIR / cache_key
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
        headers={"Cache-Control": "private, max-age=86400"},
    )
