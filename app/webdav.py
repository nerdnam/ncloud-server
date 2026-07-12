"""WebDAV endpoint — mount GenDisk as a native network drive (Windows/macOS/Linux).

Exposed at /dav. Authenticates with HTTP Basic (username/password), the scheme
WebDAV clients use. The first path segment is the space (`home` or an external
mount the user may access); the rest is the path within it. Honors the same
access, read-only, and quota rules as the rest of the app.
"""
import contextlib
import os
import shutil
import tempfile
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect
from starlette.responses import FileResponse, Response

from .auth import verify_basic_auth
from .files import (
    HOME_SPACE,
    _fs_error,
    _upload_lock,
    _writable,
    dir_size,
    accessible_mounts,
    space_root,
    user_quota,
    user_root,
)

DAV_METHODS = [
    "OPTIONS", "GET", "HEAD", "PUT", "DELETE", "PROPFIND",
    "PROPPATCH", "MKCOL", "MOVE", "COPY", "LOCK", "UNLOCK",
]

_UNAUTH = Response(
    "인증이 필요합니다",
    status_code=401,
    headers={"WWW-Authenticate": 'Basic realm="GenDisk WebDAV"'},
)


def _parse(path: str):
    """/dav/<space>/<rel...> → (space, rel). 루트면 (None, '')."""
    if path.startswith("/dav"):
        path = path[4:]
    parts = [unquote(p) for p in path.split("/") if p]
    if not parts:
        return None, ""
    return parts[0], "/".join(parts[1:])


def _safe(user: dict, space: str, rel: str) -> Path:
    from .files import safe_path
    return safe_path(user, rel, space)


def _is_home(space: str) -> bool:
    return space in ("", HOME_SPACE)


def _same_space(a: str, b: str) -> bool:
    return a == b or (_is_home(a) and _is_home(b))


def _path_size(p: Path) -> int:
    if p.is_dir():
        return dir_size(p)
    try:
        return p.stat().st_size
    except OSError:
        return 0


# XML 1.0에서 허용되지 않는 제어문자를 제거해 응답이 깨지지 않게 한다 (탭/개행/CR은 허용)
def _clean(s: str) -> str:
    return "".join(c for c in s if c in "\t\n\r" or ord(c) >= 0x20)


def _xml_escape(s: str) -> str:
    s = _clean(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def _href(space: str, rel: str, is_dir: bool) -> str:
    segs = ["dav", space] + [p for p in rel.split("/") if p]
    h = "/" + "/".join(quote(s) for s in segs)
    if is_dir and not h.endswith("/"):
        h += "/"
    return h


def _stat_etag(st: os.stat_result) -> str:
    return f"{st.st_mtime_ns:x}-{st.st_size:x}"


def _prop_xml(href: str, name: str, is_dir: bool, size: int, mtime: float, etag: str) -> str:
    lastmod = formatdate(mtime, usegmt=True)
    if is_dir:
        typ = "<D:resourcetype><D:collection/></D:resourcetype>"
        length = ""
    else:
        typ = "<D:resourcetype/>"
        length = f"<D:getcontentlength>{size}</D:getcontentlength>"
    return (
        "<D:response>"
        f"<D:href>{_xml_escape(href)}</D:href>"
        "<D:propstat><D:prop>"
        f"<D:displayname>{_xml_escape(name)}</D:displayname>"
        f"{typ}{length}"
        f"<D:getlastmodified>{lastmod}</D:getlastmodified>"
        f"<D:creationdate>{lastmod}</D:creationdate>"
        f'<D:getetag>"{_xml_escape(etag)}"</D:getetag>'
        "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
        "</D:response>"
    )


def _multistatus(body: str) -> Response:
    xml = ('<?xml version="1.0" encoding="utf-8"?>'
           '<D:multistatus xmlns:D="DAV:">' + body + "</D:multistatus>")
    return Response(xml, status_code=207, media_type='application/xml; charset="utf-8"')


# ---------- 메서드 핸들러 ----------

def _handle_options() -> Response:
    return Response(status_code=200, headers={
        "Allow": ", ".join(DAV_METHODS),
        "DAV": "1, 2",
        "MS-Author-Via": "DAV",
        "Content-Length": "0",
    })


def _propfind(user: dict, space: str | None, rel: str, depth: str) -> Response:
    if space is None:  # 가상 루트: 접근 가능한 저장소 목록
        responses = [_prop_xml("/dav/", "GenDisk", True, 0, 0, "root")]
        if depth != "0":
            spaces = [{"id": HOME_SPACE, "name": "내 파일"}] + [
                {"id": m.name, "name": m.name} for m in accessible_mounts(user)]
            for sp in spaces:
                responses.append(_prop_xml(_href(sp["id"], "", True), sp["name"], True, 0, 0, sp["id"]))
        return _multistatus("".join(responses))

    space_root(user, space)  # 접근 검증
    target = _safe(user, space, rel)
    if not target.exists():
        raise HTTPException(404)
    try:
        st = target.stat()
    except OSError as exc:
        raise _fs_error(exc)
    is_dir = target.is_dir()
    name = target.name if rel else space
    responses = [_prop_xml(_href(space, rel, is_dir), name, is_dir,
                           0 if is_dir else st.st_size, st.st_mtime, _stat_etag(st))]
    if is_dir and depth != "0":
        try:
            children = sorted(target.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            raise _fs_error(exc)
        for child in children:
            try:
                cst = child.stat()
            except OSError:
                continue
            crel = f"{rel}/{child.name}" if rel else child.name
            cis_dir = child.is_dir()
            responses.append(_prop_xml(
                _href(space, crel, cis_dir), child.name, cis_dir,
                0 if cis_dir else cst.st_size, cst.st_mtime, _stat_etag(cst)))
    return _multistatus("".join(responses))


async def _put(request, user: dict, space: str, rel: str) -> Response:
    if not rel:
        raise HTTPException(405)
    root = space_root(user, space)
    target = _safe(user, space, rel)
    if target == root or target.is_dir():
        raise HTTPException(405)
    if not target.parent.exists():
        raise HTTPException(409)
    is_home = _is_home(space)
    if not is_home and not _writable(root):
        raise HTTPException(403)

    quota = user_quota(user["id"]) if is_home else 0
    guard = _upload_lock(user["id"]) if quota > 0 else contextlib.nullcontext()
    async with guard:
        existed = target.exists()
        used = (await run_in_threadpool(dir_size, user_root(user))
                - (target.stat().st_size if existed else 0)) if quota > 0 else 0
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".davtmp-")
        tmp = Path(tmp_name)
        written = 0
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    if quota > 0 and used + written + len(chunk) > quota:
                        raise HTTPException(507)
                    out.write(chunk)
                    written += len(chunk)
            os.replace(tmp, target)
        except HTTPException:
            tmp.unlink(missing_ok=True); raise
        except OSError as exc:
            tmp.unlink(missing_ok=True); raise _fs_error(exc)
        except BaseException:  # ClientDisconnect 등 — 임시 파일 정리 후 전파
            tmp.unlink(missing_ok=True); raise
    return Response(status_code=204 if existed else 201)


def _get(user: dict, space: str, rel: str, head: bool) -> Response:
    if space is None:
        raise HTTPException(403)
    space_root(user, space)
    target = _safe(user, space, rel)
    if not target.is_file():
        raise HTTPException(404)
    if head:
        st = target.stat()
        return Response(status_code=200, headers={
            "Content-Length": str(st.st_size),
            "Last-Modified": formatdate(st.st_mtime, usegmt=True),
            "ETag": f'"{_stat_etag(st)}"',
        })
    return FileResponse(target)


def _delete(user: dict, space: str, rel: str) -> Response:
    root = space_root(user, space)
    target = _safe(user, space, rel)
    if target == root:  # 저장소 루트 자체는 삭제 불가
        raise HTTPException(403)
    if not target.exists():
        raise HTTPException(404)
    if not _is_home(space) and not _writable(root):
        raise HTTPException(403)
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        raise _fs_error(exc)
    return Response(status_code=204)


def _mkcol(user: dict, space: str, rel: str) -> Response:
    if not rel:
        raise HTTPException(405)
    space_root(user, space)
    target = _safe(user, space, rel)
    if target.exists():
        raise HTTPException(405)
    if not target.parent.exists():
        raise HTTPException(409)
    try:
        target.mkdir()
    except OSError as exc:
        raise _fs_error(exc)
    return Response(status_code=201)


def _dest_target(request, user: dict):
    dest = request.headers.get("Destination")
    if not dest:
        raise HTTPException(400)
    dspace, drel = _parse(urlsplit(dest).path)
    if dspace is None or not drel:
        raise HTTPException(403)
    droot = space_root(user, dspace)  # 접근 검증
    dst = _safe(user, dspace, drel)
    if dst == droot:  # 저장소 루트 위로 이동/복사 불가
        raise HTTPException(403)
    return dspace, droot, dst


async def _move_or_copy(request, user: dict, space: str, rel: str, is_move: bool) -> Response:
    root = space_root(user, space)
    src = _safe(user, space, rel)
    if src == root:  # 저장소 루트는 이동/복사 불가
        raise HTTPException(403)
    if not src.exists():
        raise HTTPException(404)
    dspace, droot, dst = _dest_target(request, user)
    if not _is_home(dspace) and not _writable(droot):
        raise HTTPException(403)
    if request.headers.get("Overwrite", "T").upper() == "F" and dst.exists():
        raise HTTPException(412)
    # 자기 자신/조상/하위로의 이동·복사 금지 (대상 삭제가 원본을 지우는 것 방지)
    if dst == src or dst in src.parents or src in dst.parents:
        raise HTTPException(409)

    # 홈으로 새 데이터가 유입되는 경우(복사, 또는 다른 저장소에서 이동) 용량 제한 적용
    dst_home = _is_home(dspace)
    adds_bytes = (not is_move) or (not _same_space(space, dspace))
    quota = user_quota(user["id"]) if (dst_home and adds_bytes) else 0
    guard = _upload_lock(user["id"]) if quota > 0 else contextlib.nullcontext()

    existed = dst.exists()

    def _do():
        if existed:
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        if is_move:
            shutil.move(str(src), str(dst))
        elif src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)

    async with guard:
        if quota > 0:
            base = await run_in_threadpool(dir_size, user_root(user))
            existing = _path_size(dst) if existed else 0
            add = await run_in_threadpool(_path_size, src)
            if base - existing + add > quota:
                raise HTTPException(507)
        try:
            await run_in_threadpool(_do)
        except OSError as exc:
            raise _fs_error(exc)
    return Response(status_code=204 if existed else 201)


def _lock(request, space: str, rel: str) -> Response:
    token = f"opaquelocktoken:{os.urandom(16).hex()}"
    href = _href(space or HOME_SPACE, rel, False)
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>'
        "<D:locktype><D:write/></D:locktype>"
        "<D:lockscope><D:exclusive/></D:lockscope>"
        "<D:depth>infinity</D:depth><D:timeout>Second-3600</D:timeout>"
        f"<D:locktoken><D:href>{token}</D:href></D:locktoken>"
        f"<D:lockroot><D:href>{_xml_escape(href)}</D:href></D:lockroot>"
        "</D:activelock></D:lockdiscovery></D:prop>"
    )
    return Response(body, status_code=200, media_type='application/xml; charset="utf-8"',
                    headers={"Lock-Token": f"<{token}>"})


def _proppatch(space: str, rel: str) -> Response:
    href = _href(space or HOME_SPACE, rel, False)
    body = (f"<D:response><D:href>{_xml_escape(href)}</D:href>"
            "<D:propstat><D:prop/><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
            "</D:response>")
    return _multistatus(body)


# ---------- 진입점 ----------

async def webdav_endpoint(request):
    method = request.method
    if method == "OPTIONS":
        return _handle_options()

    user = verify_basic_auth(request.headers.get("authorization"))
    if user is None:
        return _UNAUTH

    space, rel = _parse(request.url.path)
    try:
        if method in ("GET", "HEAD"):
            return _get(user, space, rel, head=(method == "HEAD"))
        if space is None and method != "PROPFIND":
            raise HTTPException(403)
        if method == "PROPFIND":
            return _propfind(user, space, rel, request.headers.get("Depth", "1"))
        if method == "PUT":
            return await _put(request, user, space, rel)
        if method == "DELETE":
            return _delete(user, space, rel)
        if method == "MKCOL":
            return _mkcol(user, space, rel)
        if method == "MOVE":
            return await _move_or_copy(request, user, space, rel, is_move=True)
        if method == "COPY":
            return await _move_or_copy(request, user, space, rel, is_move=False)
        if method == "LOCK":
            return _lock(request, space, rel)
        if method == "UNLOCK":
            return Response(status_code=204)
        if method == "PROPPATCH":
            return _proppatch(space, rel)
        raise HTTPException(405)
    except HTTPException as e:
        return Response(status_code=e.status_code)
    except ClientDisconnect:
        return Response(status_code=499)  # 클라이언트가 연결을 끊음
