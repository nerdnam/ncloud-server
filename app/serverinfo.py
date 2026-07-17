"""Nextcloud 호환 serverinfo API — Homepage 대시보드의 nextcloud 위젯 지원.

Homepage 의 nextcloud 위젯은
    GET /ocs/v2.php/apps/serverinfo/api/v1/info?format=json
를 호출하고, 인증은 `NC-Token: <토큰>` 헤더(설정한 토큰) 또는 관리자 HTTP Basic 을 쓴다.
응답은 Nextcloud OCS 형식이며 위젯은 다음 필드를 읽는다:
  ocs.data.nextcloud.system.cpuload[0]         (CPU 부하)
  ocs.data.nextcloud.system.mem_total/mem_free (메모리 사용률 계산)
  ocs.data.nextcloud.system.freespace          (여유 공간, 바이트)
  ocs.data.nextcloud.storage.num_files         (파일 수)
  ocs.data.nextcloud.shares.num_shares         (공유 수)
  ocs.data.activeUsers.last24hours             (활성 사용자)

토큰은 환경변수 GENDISK_SERVERINFO_TOKEN 으로 설정한다. 미설정 시 관리자 Basic 만 허용.
시스템 정보를 노출하므로 인증이 없으면 항상 401.
"""
import os
import secrets
import shutil
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request

from . import files as files_mod
from .auth import verify_basic_auth
from .database import DATA_DIR, FILES_DIR, get_db, get_setting

router = APIRouter(tags=["serverinfo"])

# 환경변수 토큰(선택). 관리자 웹 UI 에서 설정한 DB 토큰이 우선.
ENV_TOKEN = os.environ.get("GENDISK_SERVERINFO_TOKEN", "")


def _configured_token() -> str:
    """유효한 위젯 토큰 — 웹 UI(관리자 설정) 저장값 우선, 없으면 환경변수."""
    return get_setting("serverinfo_token") or ENV_TOKEN

# 파일 수는 전체 트리를 훑어야 하므로 캐시(폴링마다 재계산하지 않도록).
_FILES_TTL = 300.0
_files_cache = {"at": 0.0, "n": 0}


def _authorized(request: Request) -> bool:
    """NC-Token(설정 토큰) 또는 관리자 Basic 인증이면 허용."""
    configured = _configured_token()
    token = request.headers.get("NC-Token")
    # 바이트로 비교 (compare_digest 는 비-ASCII str 에 TypeError → 헤더에 유니코드가 와도 안전하게 실패)
    if configured and token and secrets.compare_digest(
            token.encode("utf-8"), configured.encode("utf-8")):
        return True
    user = verify_basic_auth(request.headers.get("Authorization"))
    return bool(user and user["is_admin"])


def _loadavg() -> list[float]:
    try:
        return [round(x, 2) for x in os.getloadavg()]   # (1분, 5분, 15분)
    except (OSError, AttributeError):
        return [0.0, 0.0, 0.0]                            # Linux 외 환경


def _meminfo_kb() -> tuple[int, int]:
    """(mem_total, mem_free) KB — /proc/meminfo(Linux). 실패 시 (0, 0)."""
    try:
        info = {}
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    info[key.strip()] = int(parts[0])
        total = info.get("MemTotal", 0)
        free = info.get("MemAvailable", info.get("MemFree", 0))
        return total, free
    except (OSError, ValueError):
        return 0, 0


def _count_files() -> int:
    """개인 저장소 + 외부 마운트의 총 파일 수 (5분 캐시)."""
    now = time.time()
    if now - _files_cache["at"] < _FILES_TTL and _files_cache["at"] > 0:
        return _files_cache["n"]
    roots = [FILES_DIR] + files_mod.list_mounts()
    n = 0
    for r in roots:
        for _root, _dirs, fs in os.walk(r, onerror=lambda e: None):
            n += len(fs)
    _files_cache["at"] = now
    _files_cache["n"] = n
    return n


def _active_users(conn) -> tuple[int, int, int]:
    """last_seen 기준 최근 5분/1시간/24시간 활성(고유) 사용자 수."""
    now = datetime.now(timezone.utc)

    def count(minutes: int) -> int:
        cutoff = (now - timedelta(minutes=minutes)).isoformat()
        row = conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM sessions "
            "WHERE last_seen IS NOT NULL AND last_seen >= ?",
            (cutoff,),
        ).fetchone()
        return row["n"]

    return count(5), count(60), count(60 * 24)


@router.get("/ocs/v2.php/apps/serverinfo/api/v1/info")
def serverinfo(request: Request):
    if not _authorized(request):
        raise HTTPException(401, "serverinfo 접근 권한이 없습니다")

    conn = get_db()
    try:
        num_users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        num_shares = conn.execute("SELECT COUNT(*) AS n FROM shares").fetchone()["n"]
        a5, a1h, a24 = _active_users(conn)
    finally:
        conn.close()

    mem_total, mem_free = _meminfo_kb()
    try:
        freespace = shutil.disk_usage(DATA_DIR).free
    except OSError:
        freespace = 0
    num_storages = 1 + len(files_mod.list_mounts())   # 개인 저장소 + 마운트

    return {
        "ocs": {
            "meta": {"status": "ok", "statuscode": 200, "message": "OK"},
            "data": {
                "nextcloud": {
                    "system": {
                        "version": "0.1.0",
                        "cpuload": _loadavg(),
                        "mem_total": mem_total,
                        "mem_free": mem_free,
                        "freespace": freespace,
                    },
                    "storage": {
                        "num_users": num_users,
                        "num_files": _count_files(),
                        "num_storages": num_storages,
                    },
                    "shares": {
                        "num_shares": num_shares,
                        "num_fed_shares_sent": 0,
                        "num_fed_shares_received": 0,
                    },
                },
                "activeUsers": {
                    "last5minutes": a5,
                    "last1hour": a1h,
                    "last24hours": a24,
                },
            },
        }
    }
