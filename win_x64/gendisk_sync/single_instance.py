"""단일 인스턴스 보장.

두 번째 실행은 새 창을 띄우지 않고, 이미 실행 중인 인스턴스에 '창을 보여라' 신호만
보내고 종료한다. 자동시작(안정 사본)과 수동 실행(바탕화면 exe)이 서로 다른 파일이라도
전역 네임드 뮤텍스로 하나만 살아있게 한다.

- 감지: Local\\gendisk-sync-singleton 네임드 뮤텍스(원자적).
- 신호: 주 인스턴스가 127.0.0.1 임시 포트를 열고 포트를 파일에 남긴다. 두 번째 인스턴스는
  그 포트로 접속해 'show' 를 보낸다 → 주 인스턴스가 창을 복원/전면화한다.
"""
import ctypes
import os
import socket
import threading

_MUTEX_NAME = "Local\\gendisk-sync-singleton"
_PORT_FILE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                          "genDISK", ".instance")
_ERROR_ALREADY_EXISTS = 183

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateMutexW.restype = ctypes.c_void_p
_k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]

_mutex = None      # 주 인스턴스 동안 유지(닫으면 안 됨)
_srv = None


def is_primary() -> bool:
    """이 프로세스가 첫(주) 인스턴스면 True. 이미 실행 중이면 False."""
    global _mutex
    h = _k32.CreateMutexW(None, 0, _MUTEX_NAME)
    err = ctypes.get_last_error()
    if not h:
        return True                       # 뮤텍스 생성 실패 시 안전하게 진행
    if err == _ERROR_ALREADY_EXISTS:
        _k32.CloseHandle(ctypes.c_void_p(h))
        return False
    _mutex = h                            # 프로세스 수명 동안 유지
    return True


def signal_existing():
    """이미 실행 중인 인스턴스에 창을 띄우라고 신호(best-effort)."""
    try:
        with open(_PORT_FILE, encoding="utf-8") as f:
            port = int(f.read().strip())
        with socket.create_connection(("127.0.0.1", port), timeout=1.5) as s:
            s.sendall(b"show")
    except Exception:
        pass


def start_show_listener(on_show):
    """주 인스턴스: 신호를 받으면 on_show() 를 호출(데몬 스레드에서)."""
    global _srv
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        s.listen(5)
    except OSError:
        s.close()
        return
    _srv = s
    try:
        os.makedirs(os.path.dirname(_PORT_FILE), exist_ok=True)
        with open(_PORT_FILE, "w", encoding="utf-8") as f:
            f.write(str(s.getsockname()[1]))
    except OSError:
        pass

    def loop():
        while True:
            try:
                conn, _ = s.accept()
            except OSError:
                break
            try:
                conn.recv(64)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
            try:
                on_show()
            except Exception:
                pass

    threading.Thread(target=loop, daemon=True).start()


def cleanup():
    """종료 시 포트 파일 정리(best-effort)."""
    try:
        if _srv is not None:
            _srv.close()
    except OSError:
        pass
    try:
        os.remove(_PORT_FILE)
    except OSError:
        pass
