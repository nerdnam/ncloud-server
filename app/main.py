"""GenDisk — self-hosted personal cloud storage (gendisk.cloud)."""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import admin, auth, files, sync
from .database import init_db
from .webdav import DAV_METHODS, webdav_endpoint

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DOWNLOADS_DIR = BASE_DIR / "downloads"
WIN_CLIENT = "gendisk-sync.exe"

app = FastAPI(title="GenDisk", version="0.1.0")
init_db()

app.include_router(auth.router)
app.include_router(files.router)
app.include_router(admin.router)
app.include_router(sync.router)

# WebDAV: /dav 및 그 하위 경로를 모든 WebDAV 메서드로 처리
app.add_route("/dav", webdav_endpoint, methods=DAV_METHODS)
app.add_route("/dav/{path:path}", webdav_endpoint, methods=DAV_METHODS)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


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
