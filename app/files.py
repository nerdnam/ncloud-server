"""File/folder management, download, thumbnails, media streaming.

Every endpoint takes a "space": "home" is the user's private storage
(data/files/<username>); any other value is an external mount — a directory
that appears under MOUNTS_DIR (in Docker: -v /host/path:/app/mounts/<name>).
"""
import errno
import hashlib
import io
import mimetypes
import os
import re
import shutil
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .auth import current_user
from .database import FILES_DIR, MOUNTS_DIR, THUMBS_DIR

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
    for mount in list_mounts():
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
    try:
        children = list(target.iterdir())
    except OSError as exc:
        raise _fs_error(exc)
    entries = []
    for p in children:
        try:
            entries.append(entry_info(p, root))
        except OSError:
            continue  # 접근 불가 항목(권한 등)은 건너뜀
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return {
        "space": space or HOME_SPACE,
        "path": target.relative_to(root).as_posix() if target != root else "",
        "entries": entries,
    }


@router.post("/upload")
async def upload(
    files: list[UploadFile],
    path: str = "",
    space: str = HOME_SPACE,
    user: dict = Depends(current_user),
):
    target_dir = safe_path(user, path, space)
    if not target_dir.is_dir():
        raise HTTPException(404, "폴더를 찾을 수 없습니다")
    saved = []
    for f in files:
        name = Path(f.filename or "unnamed").name  # strip any client-sent directories
        if not name or name in (".", ".."):
            continue
        dest = target_dir / name
        # avoid overwriting: append (1), (2), ...
        counter = 1
        while dest.exists():
            dest = target_dir / f"{Path(name).stem} ({counter}){Path(name).suffix}"
            counter += 1
        try:
            with dest.open("wb") as out:
                shutil.copyfileobj(f.file, out)
        except OSError as exc:
            raise _fs_error(exc)
        saved.append(dest.name)
    return {"saved": saved}


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
    return {"ok": True}


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
