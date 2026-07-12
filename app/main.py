"""ncloud — self-hosted personal cloud storage."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import admin, auth, files
from .database import init_db

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="ncloud", version="0.1.0")
init_db()

app.include_router(auth.router)
app.include_router(files.router)
app.include_router(admin.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")
