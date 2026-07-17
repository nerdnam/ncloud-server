"""genDISK — self-hosted personal cloud storage (gendisk.cloud)."""
import hashlib
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import admin, auth, files, serverinfo, shares, sync
from .database import init_db
from .webdav import DAV_METHODS, webdav_endpoint

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DOWNLOADS_DIR = BASE_DIR / "downloads"
WIN_CLIENT = "gendisk-sync.exe"

app = FastAPI(title="genDISK", version="0.1.0")
init_db()

app.include_router(auth.router)
app.include_router(files.router)
app.include_router(admin.router)
app.include_router(sync.router)
app.include_router(shares.router)
app.include_router(shares.public_router)
app.include_router(serverinfo.router)   # Nextcloud 호환 serverinfo (Homepage 위젯)

# WebDAV: /dav 및 그 하위 경로를 모든 WebDAV 메서드로 처리
app.add_route("/dav", webdav_endpoint, methods=DAV_METHODS)
app.add_route("/dav/{path:path}", webdav_endpoint, methods=DAV_METHODS)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _asset_version() -> str:
    """정적 JS/CSS 내용 기반 버전. 파일이 바뀌면 값도 바뀌어 브라우저·CDN(Cloudflare)
    캐시를 무효화한다. 컨테이너 수명 동안 파일은 고정이라 시작 시 한 번만 계산한다."""
    h = hashlib.sha1()
    for name in ("style.css", "app.js", "share.js"):
        try:
            h.update((STATIC_DIR / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:8]


_ASSET_VERSION = _asset_version()


def _serve_html(name: str) -> HTMLResponse:
    """HTML 을 서빙하며 정적 참조에 ?v=<버전> 을 붙인다(캐시 버스팅).
    HTML 자체는 no-cache 로 항상 재검증되게 해, 배포 후 새 JS/CSS 를 확실히 받게 한다."""
    html = (STATIC_DIR / name).read_text(encoding="utf-8")
    for asset in ("style.css", "app.js", "share.js"):
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={_ASSET_VERSION}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
def index():
    return _serve_html("index.html")


@app.get("/s/{token}", include_in_schema=False)
def share_page(token: str):
    """외부 공유 링크의 공개 열람 페이지. 실제 검증/열람은 페이지 JS가 API로 수행."""
    return _serve_html("share.html")


def _win_client_path():
    """이미지에 포함된 Windows 클라이언트 exe (버전명 파일도 허용)."""
    exact = DOWNLOADS_DIR / WIN_CLIENT
    if exact.is_file():
        return exact
    if DOWNLOADS_DIR.is_dir():
        for p in sorted(DOWNLOADS_DIR.glob("gendisk-sync*.exe")):
            return p
    return None


@app.get("/api/download/info")
def download_info():
    """웹 UI가 다운로드 버튼 표시 여부를 정하는 데 사용."""
    p = _win_client_path()
    if p is None:
        return {"windows": None}
    return {"windows": {"name": p.name, "size": p.stat().st_size, "url": "/download/windows"}}


@app.get("/download/windows", include_in_schema=False)
def download_windows():
    p = _win_client_path()
    if p is None:
        raise HTTPException(404, "Windows 클라이언트가 아직 제공되지 않습니다")
    return FileResponse(p, media_type="application/octet-stream", filename=WIN_CLIENT)
