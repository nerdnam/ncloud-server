"""genDISK Drive 컨트롤러 — 브랜디드 온디맨드 드라이브의 수명주기 관리.

vfs.Provider(CfAPI 온디맨드) 를 서버 클라이언트에 연결하고, navdrive(탐색기 노드)를
등록/해제한다. 트레이 앱이 소유하며, 콜백(list_dir/fetch_range)은 세션 만료 시 자동
재로그인 후 1회 재시도한다. 관리자 권한/서명/패키지 불필요(navdrive 참고).
"""
import os
import shutil
import threading
import time

from . import navdrive, vfs
from .client import ApiError, AuthError, GenDiskClient
from .icon import icon_path


class TransferTracker:
    """진행 중인 전송(업로드/다운로드)을 스레드 안전하게 추적 — FTP식 파일별 상태 표시용.
    갱신이 expire 초 넘게 멈춘 항목은 snapshot 에서 자동 제거(완료/중단된 다운로드 정리)."""
    def __init__(self, expire=6.0):
        self._lock = threading.Lock()
        self._items = {}   # key -> {name, dir, done, total, updated, rate, _rt, _rb}
        self._expire = expire

    def update(self, key, name, direction, done, total):
        now = time.monotonic()
        done = int(done)
        with self._lock:
            it = self._items.get(key)
            if it is None:
                self._items[key] = {"name": name, "dir": direction, "done": done,
                                    "total": max(0, int(total or 0)), "updated": now,
                                    "rate": 0.0, "_rt": now, "_rb": done}
                return
            dt = now - it["_rt"]
            if dt >= 0.4:                        # 전송률: 지수이동평균(누적 done 기준)
                inst = max(0, done - it["_rb"]) / dt
                it["rate"] = inst if it["rate"] == 0 else it["rate"] * 0.6 + inst * 0.4
                it["_rt"] = now
                it["_rb"] = done
            it["done"] = done
            it["updated"] = now
            if total:
                it["total"] = int(total)

    def finish(self, key):
        with self._lock:
            self._items.pop(key, None)

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            for k in [k for k, v in self._items.items() if now - v["updated"] > self._expire]:
                self._items.pop(k, None)
            return [dict(v) for v in self._items.values()]

# CfAPI ProviderId (고정)
PROVIDER_GUID = "{61B70D09-051E-4A68-87A3-F6DD4A72F9C0}"


def stable_icon_path() -> str:
    """번들 아이콘(.ico)을 %LOCALAPPDATA%\\genDISK 로 복사하고 그 영구 경로를 돌려준다.
    레지스트리에 넣는 아이콘 경로는 앱 종료 후에도 살아있어야 하므로(onefile 의 _MEIPASS
    임시경로 금지) 영구 위치로 복사한다."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "genDISK")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, "gendisk-icon.ico")
    try:
        src = icon_path()
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
    except OSError:
        pass
    return dst


def _log_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "genDISK", "drive.log")


class DriveController:
    def __init__(self, cfg, on_reauth=None, log=print, notify=None, progress=None):
        """cfg: Config. on_reauth: () -> bool (재로그인 시도, 성공 시 cfg.token 갱신).
        notify: (msg) -> None 토스트. progress: (key,name,dir,done,total) -> None 전송 진행."""
        self.cfg = cfg
        self.on_reauth = on_reauth
        self._notify = notify
        self._progress = progress
        self._applog = log
        self.log = self._make_log(log)
        self.provider = None
        self._lock = threading.Lock()
        self._refresh_stop = None       # threading.Event — 원격 변경 반영 폴링 중지 신호
        self._refresh_thread = None
        self._events_stop = None        # threading.Event — 실시간 이벤트(SSE) 스레드 중지 신호
        self._events_thread = None
        self._events_stream = None      # 열려 있는 EventStream (종료 시 close 로 읽기 깨우기)
        self._cached_client = None      # keep-alive 연결을 살리려 클라이언트를 재사용

    def _make_log(self, applog):
        """GUI 로그 + 파일 로그(%LOCALAPPDATA%\\genDISK\\drive.log) 동시 기록(진단용)."""
        path = _log_path()

        def _log(msg):
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(str(msg) + "\n")
            except OSError:
                pass
            try:
                applog(msg)
            except Exception:
                pass
        return _log

    def _list_spaces(self):
        return self._with_reauth(lambda c: c.spaces())

    # --- 서버 호출 (세션 만료 시 1회 재로그인 후 재시도) ---
    def _client(self):
        # 클라이언트를 재사용해 스레드별 keep-alive 연결을 살린다(매 호출 새 TLS 핸드셰이크 방지).
        # 서버 주소가 바뀌면 새로 만들고, 토큰만 바뀌었으면(재로그인) 연결은 유지한 채 토큰만 갱신.
        c = self._cached_client
        base = self.cfg.server_url.rstrip("/")
        if c is None or c.base_url != base:
            c = GenDiskClient(self.cfg.server_url, self.cfg.token)
            self._cached_client = c
        else:
            c.token = self.cfg.token
        return c

    def _with_reauth(self, fn):
        try:
            return fn(self._client())
        except AuthError:
            if self.on_reauth and self.on_reauth():
                return fn(self._client())
            raise

    def _list_dir(self, space, rel):
        return self._with_reauth(lambda c: c.list_dir(space, rel))

    def _fetch_range(self, meta, offset, length):
        return self._with_reauth(
            lambda c: c.download_range(meta["space"], meta["path"], offset, length))

    def _upload_file(self, space, path, local_path):
        # 로컬→원격: 큰 파일은 put_smart 가 자동으로 청크 업로드로 전환한다.
        name = os.path.basename(local_path)

        def prog(done, total):
            if self._progress:
                self._progress(local_path, name, "up", done, total)
        try:
            self._with_reauth(lambda c: c.put_smart(space, path, local_path, progress=prog))
        finally:
            if self._progress:
                self._progress(local_path, name, "up", None, None)   # 완료/종료 표식

    def _delete_file(self, space, path):
        # 로컬 삭제 → 서버 삭제. 이미 없으면(404) 서버가 알아서 처리.
        self._with_reauth(lambda c: c.delete(space, path))

    def _rename_file(self, space, src, dst, src_space=None, dst_space=None):
        # 로컬 이름변경/이동 → 서버 이동(move). 저장소 간이면 src_space/dst_space 지정.
        self._with_reauth(lambda c: c.move(space, src, dst, src_space, dst_space))

    def _mkdir_dir(self, space, path):
        # 로컬 새 폴더 → 서버 mkdir.
        self._with_reauth(lambda c: c.mkdir(space, path))

    # --- 원격 변경 반영 (폰/웹 업로드가 드라이브에 나타나게) ---
    def _refresh_loop(self):
        interval = max(15, int(getattr(self.cfg, "interval_sec", 30) or 30))
        stop = self._refresh_stop
        while stop is not None:
            prov = self.provider
            if prov is None:
                break
            try:
                prov.refresh()          # 원격→로컬: 다른 기기가 올린 새 파일 반영
            except Exception as e:      # _with_reauth 로 세션 만료 자동 처리. 스레드는 안 죽게.
                self.log(f"[drive] refresh loop: {e!r}")
            try:
                prov.upload_scan()      # 로컬→원격: 드롭한 파일 업로드 + '보류중' 해소
            except Exception as e:      # noqa: BLE001
                self.log(f"[drive] upload loop: {e!r}")
            if stop.wait(interval):     # 반영 후 대기 — 중지 신호면 종료
                break

    def refresh_now(self) -> int:
        """수동 새로고침 — 지금 즉시 원격 변경을 반영한다. 반영된 신규 항목 수 반환."""
        prov = self.provider
        if prov is None:
            return 0
        return prov.refresh()

    # --- 실시간 반영 (서버 SSE 이벤트 → 변경 폴더만 즉시 새로고침) ---
    def _open_event_stream(self):
        """전용 연결로 서버 이벤트 스트림을 연다(세션 만료 시 1회 재로그인 후 재시도)."""
        try:
            return self._client().open_events()
        except AuthError:
            if self.on_reauth and self.on_reauth():
                return self._client().open_events()
            raise

    def _on_change_event(self, ev):
        """서버 변경 이벤트 {space, dir} → 그 폴더만 즉시 대조/반영(추가·서버삭제 모두)."""
        if not isinstance(ev, dict):
            return
        space = ev.get("space")
        server_dir = ev.get("dir", "")
        if not space:
            return
        prov = self.provider
        if prov is None:
            return
        try:
            n = prov.refresh_dir(space, server_dir)
            if n:
                self.log(f"[drive] 실시간 반영 {space}:/{server_dir} (+{n})")
        except Exception as e:  # noqa: BLE001
            self.log(f"[drive] refresh_dir 오류: {e!r}")

    def _events_loop(self):
        """서버 SSE(/api/sync/events)를 구독해 변경을 즉시 반영한다. 연결이 끊기면
        백오프 후 재연결한다. 서버가 미지원(404 등)이면 조용히 폴링만 쓰도록 종료한다.
        폴링 루프(_refresh_loop)는 폴백으로 계속 돈다(이벤트 유실·연결 공백 대비)."""
        backoff = 1.0
        stop = self._events_stop
        while stop is not None and not stop.is_set():
            if self.provider is None:
                break
            try:
                stream = self._open_event_stream()
            except ApiError as e:
                if getattr(e, "status", None) in (404, 405, 501):
                    self.log("[drive] 서버가 실시간 이벤트(/events) 미지원 → 폴링만 사용")
                    return
                self.log(f"[drive] 이벤트 연결 오류: {e}")
                if stop.wait(min(30.0, backoff)):
                    break
                backoff = min(30.0, backoff * 2)
                continue
            except Exception as e:  # noqa: BLE001  (AuthError 재로그인 실패 포함)
                self.log(f"[drive] 이벤트 연결 실패: {e!r}")
                if stop.wait(min(30.0, backoff)):
                    break
                backoff = min(30.0, backoff * 2)
                continue

            self._events_stream = stream
            # stop() 이 '연결 중'(스트림 발행 전)에 불렸으면 닫을 핸들이 없어 못 깨웠다 →
            # 발행 직후 재확인해 여기서 스스로 정리한다. (stop 은 이벤트를 먼저 set 하고
            # 스트림을 읽으므로, 어느 쪽이든 한쪽은 반드시 close 를 수행한다.)
            if stop.is_set():
                self._events_stream = None
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
                break
            self.log("[drive] 실시간 이벤트 연결됨")
            backoff = 1.0
            try:
                for ev in stream:
                    if stop.is_set():
                        break
                    self._on_change_event(ev)
            except Exception as e:  # noqa: BLE001
                self.log(f"[drive] 이벤트 스트림 끊김: {e!r}")
            finally:
                self._events_stream = None
                try:
                    stream.close()
                except Exception:
                    pass
            if stop.wait(min(5.0, backoff)):   # 재연결 전 짧은 대기
                break
        self.log("[drive] 실시간 이벤트 종료")

    # --- 수명주기 ---
    @property
    def running(self) -> bool:
        return self.provider is not None

    def start(self):
        """싱크루트 등록 + 연결 + 최상위 채우기 + 탐색기 노드 등록. (네트워크 → 스레드에서 호출)"""
        with self._lock:
            if self.provider is not None:
                return
            root = self.cfg.vfs_root_path()
            os.makedirs(root, exist_ok=True)
            icon = stable_icon_path()
            vfs.set_expose_placeholders()
            prov = vfs.Provider(root, PROVIDER_GUID, self._fetch_range,
                                list_dir=self._list_dir, list_spaces=self._list_spaces,
                                upload=self._upload_file, delete=self._delete_file,
                                rename=self._rename_file, mkdir=self._mkdir_dir,
                                notify=self._notify, progress=self._progress,
                                space=self.cfg.space, log=self.log)
            prov.register()
            prov.connect()                 # 내부에서 populate_root()
            navdrive.register_drive(root, icon)
            self.provider = prov
            # 원격 변경 반영 폴링 시작 (폴백 + 로컬 드롭 업로드 감지)
            self._refresh_stop = threading.Event()
            self._refresh_thread = threading.Thread(
                target=self._refresh_loop, name="gendisk-drive-refresh", daemon=True)
            self._refresh_thread.start()
            # 실시간 이벤트(SSE) 구독 시작 (폰/웹/다른 기기 변경을 즉시 반영)
            self._events_stop = threading.Event()
            self._events_thread = threading.Thread(
                target=self._events_loop, name="gendisk-drive-events", daemon=True)
            self._events_thread.start()
            self.log(f"[drive] genDISK Drive 연결됨: {root}")

    def stop(self, remove_node: bool = False):
        """provider 연결 해제. remove_node=True 면 탐색기 노드+싱크루트도 제거."""
        with self._lock:
            if self._events_stop is not None:       # 실시간 이벤트 스레드 중지 + 열린 스트림 닫기
                self._events_stop.set()
                self._events_stop = None
                self._events_thread = None
                st = self._events_stream            # 블록된 readline 을 깨워 스레드가 빠져나오게
                if st is not None:
                    try:
                        st.close()
                    except Exception:  # noqa: BLE001
                        pass
                self._events_stream = None
            if self._refresh_stop is not None:      # 폴링 먼저 멈춘다(provider 를 더 안 건드리게)
                self._refresh_stop.set()
                self._refresh_stop = None
                self._refresh_thread = None
            if self.provider is not None:
                try:
                    self.provider.disconnect()
                except Exception as e:  # noqa: BLE001
                    self.log(f"[drive] disconnect: {e}")
                self.provider = None
            if remove_node:
                try:
                    navdrive.unregister_drive()
                except Exception as e:  # noqa: BLE001
                    self.log(f"[drive] unregister node: {e}")
                try:
                    vfs.C.CfUnregisterSyncRoot(self.cfg.vfs_root_path())
                except Exception:
                    pass
            self.log("[drive] genDISK Drive 해제됨")
