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
import mimetypes
import os
import re
import shutil
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from .auth import current_user
from .database import FILES_DIR, MOUNTS_DIR, THUMBS_DIR, get_db

router = APIRouter(prefix="/api/files", tags=["files"])

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


def entry_info(path: Path, root: Path) -> dict:
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
            entries.append(entry_info(Path(de.path), root))
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


# 모든 이동을 직렬화해, "대상 존재 확인 → 이동" 사이의 경쟁으로 파일이 조용히
# 덮어써지는 것을 막는다 (공유 마운트라 사용자별 잠금으로는 부족해 전역 잠금 사용).
# 이동은 대부분 즉시 끝나는 메타데이터 연산이라 전역 직렬화 비용이 미미하다.
_move_lock = asyncio.Lock()


@router.post("/move")
async def move(body: MoveBody, user: dict = Depends(current_user)):
    """같은 저장소 안에서 파일/폴더를 다른 위치로 이동한다 (폴더 간 이동 포함)."""
    root = space_root(user, body.space)
    src = safe_path(user, body.src, body.space)
    dst = safe_path(user, body.dst, body.space)
    if src == root:
        raise HTTPException(400, "루트 폴더는 이동할 수 없습니다")
    if not src.exists():
        raise HTTPException(404, "대상을 찾을 수 없습니다")
    if dst == root:
        raise HTTPException(400, "잘못된 대상 경로입니다")
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
    shares.drop_shares_for(user["id"], body.space, body.src)
    return {"ok": True, "path": dst.relative_to(root).as_posix()}


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
