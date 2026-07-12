"""Windows에서 WebDAV를 네트워크 드라이브로 연결/해제.

`net use`는 비밀번호가 명령줄에 노출(프로세스 목록·감사 로그)되므로, 자격증명을
프로세스 안에서만 전달하는 Win32 API WNetAddConnection2W(mpr.dll)를 ctypes로 호출한다.

WebDAV URL을 UNC(\\\\server@SSL@port\\dav)로 바꿔 매핑한다. 평문 HTTP일 때는
Windows WebClient가 기본적으로 Basic 인증을 막으므로 HTTPS 사용을 권장한다.
"""
import ctypes
from ctypes import wintypes
from urllib.parse import urlsplit

RESOURCETYPE_DISK = 0x00000001
CONNECT_UPDATE_PROFILE = 0x00000001  # 재부팅 후에도 유지(persistent)
_ERROR_NOT_CONNECTED = 2250


class _NETRESOURCE(ctypes.Structure):
    _fields_ = [
        ("dwScope", wintypes.DWORD),
        ("dwType", wintypes.DWORD),
        ("dwDisplayType", wintypes.DWORD),
        ("dwUsage", wintypes.DWORD),
        ("lpLocalName", wintypes.LPWSTR),
        ("lpRemoteName", wintypes.LPWSTR),
        ("lpComment", wintypes.LPWSTR),
        ("lpProvider", wintypes.LPWSTR),
    ]


def _mpr():
    mpr = ctypes.WinDLL("mpr.dll")
    mpr.WNetAddConnection2W.argtypes = [
        ctypes.POINTER(_NETRESOURCE), wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    mpr.WNetAddConnection2W.restype = wintypes.DWORD
    mpr.WNetCancelConnection2W.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL]
    mpr.WNetCancelConnection2W.restype = wintypes.DWORD
    return mpr


def _unc(server_url: str) -> str:
    u = urlsplit(server_url)
    host = u.hostname or ""
    port = u.port
    secure = u.scheme == "https"
    at = "@SSL" if secure else ""
    if secure and port == 443:
        at = "@SSL@443"
    elif port and not (secure and port == 443) and not (not secure and port == 80):
        at += f"@{port}"
    return rf"\\{host}{at}\dav"


def _error_message(err: int, unc: str) -> str:
    msg = ctypes.FormatError(err).strip()
    hint = ""
    if err in (1326, 86, 1327):        # 로그온 실패 / 잘못된 비밀번호
        hint = "\n아이디 또는 비밀번호를 확인하세요."
    elif err in (67, 53, 1222, 66):     # 네트워크 경로/장치 문제
        hint = ("\n서버 주소를 확인하세요. HTTP(비-HTTPS) 서버면 Windows가 Basic 인증을 "
                "막을 수 있습니다(HTTPS 권장). WebClient 서비스가 실행 중인지도 확인하세요.")
    return f"드라이브 연결 실패 (코드 {err}: {msg}){hint}\n대상: {unc}"


def connect_drive(drive: str, server_url: str, username: str, password: str) -> str:
    unc = _unc(server_url)
    drive = drive.rstrip("\\")
    mpr = _mpr()
    # 기존 매핑이 있으면 먼저 해제 (오류 무시)
    mpr.WNetCancelConnection2W(drive, 0, True)
    nr = _NETRESOURCE()
    nr.dwType = RESOURCETYPE_DISK
    nr.lpLocalName = drive
    nr.lpRemoteName = unc
    err = mpr.WNetAddConnection2W(ctypes.byref(nr), password, username, CONNECT_UPDATE_PROFILE)
    if err != 0:
        raise RuntimeError(_error_message(err, unc))
    return unc


def disconnect_drive(drive: str):
    drive = drive.rstrip("\\")
    err = _mpr().WNetCancelConnection2W(drive, CONNECT_UPDATE_PROFILE, True)
    if err not in (0, _ERROR_NOT_CONNECTED):
        raise RuntimeError(f"연결 해제 실패 (코드 {err}: {ctypes.FormatError(err).strip()})")
