"""파일 변경 실시간 알림 허브 (SSE 구독자 관리).

변경을 일으키는 모든 API(files/sync/webdav)가 notify_change() 를 호출하면
/api/sync/events 를 구독 중인 클라이언트(macOS File Provider 앱 등)가 즉시
알림을 받아 해당 폴더만 다시 열거한다 — 주기 폴링 없는 실시간 동기화.

notify_change() 는 어느 스레드에서 불러도 안전하다(스레드풀에서 도는 동기
엔드포인트 포함): 이벤트 루프에 call_soon_threadsafe 로 넘겨 처리한다.
개인 공간(home) 이벤트는 같은 사용자 구독자에게만 전달한다(경로 노출 방지).
"""
import asyncio
import contextlib
import json
import posixpath

# queue → 구독한 사용자 id. 구독/해지는 이벤트 루프에서만 일어난다.
_subscribers: dict[asyncio.Queue, int] = {}
# 구독이 생길 때 잡아두는 이벤트 루프 — 워커 스레드의 notify 를 넘겨받을 곳.
_loop: asyncio.AbstractEventLoop | None = None


def parent_of(rel: str) -> str:
    """상대 경로의 부모 폴더 상대 경로('' = 공간 루트)."""
    return posixpath.dirname((rel or "").strip("/"))


def subscribe(user_id: int) -> asyncio.Queue:
    global _loop
    _loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    _subscribers[q] = user_id
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.pop(q, None)


def _mount_allowed_ids(space: str) -> set[int]:
    """마운트 공간 이벤트를 받아도 되는 사용자(관리자 + grant 보유자) id 집합.

    space_root() 가 권한 없는 사용자에게 마운트 존재 자체를 404 로 숨기는 것과
    같은 기준 — 이벤트로 마운트 이름/내부 경로가 새어 나가면 안 된다.
    """
    from .database import get_db  # 순환 import 회피(지연 import)

    conn = get_db()
    try:
        ids = {
            row["user_id"]
            for row in conn.execute(
                "SELECT user_id FROM mount_grants WHERE mount_name = ?", (space,)
            )
        }
        ids |= {row["id"] for row in conn.execute("SELECT id FROM users WHERE is_admin = 1")}
        return ids
    finally:
        conn.close()


def notify_change(space: str, dir_rel: str, user_id: int, private: bool) -> None:
    """space 의 dir_rel 폴더에서 내용이 바뀌었음을 구독자들에게 알린다.

    private=True(개인 공간)면 같은 user_id 구독자에게만,
    마운트 공간이면 접근 권한(grant/관리자)이 있는 구독자에게만 보낸다.
    구독자가 없거나 루프가 아직 없으면 조용히 무시한다(비용 0).
    """
    loop = _loop
    if loop is None or not _subscribers:
        return
    if private:
        allowed = {user_id}
    else:
        try:
            allowed = _mount_allowed_ids(space)
        except Exception:
            allowed = {user_id}  # 권한 조회 실패 시 안전한 쪽(본인만)으로
    payload = json.dumps(
        {"space": space, "dir": (dir_rel or "").strip("/")}, ensure_ascii=False
    )

    def _emit() -> None:
        for q, uid in list(_subscribers.items()):
            if uid not in allowed:
                continue
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(payload)

    loop.call_soon_threadsafe(_emit)
