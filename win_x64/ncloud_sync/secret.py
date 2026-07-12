"""비밀번호를 Windows DPAPI로 암호화/복호화 (현재 Windows 사용자 계정에 묶임).

다른 사용자·다른 PC에서는 복호화되지 않는다. 외부 의존성 없이 ctypes로 구현.
Windows가 아니거나 DPAPI 실패 시에는 저장하지 않도록 None을 돌려준다.
"""
import base64
import ctypes
import sys
from ctypes import wintypes


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _run(func, data: bytes) -> bytes:
    src = _BLOB(len(data),
                ctypes.cast(ctypes.create_string_buffer(data, len(data)),
                            ctypes.POINTER(ctypes.c_char)))
    out = _BLOB()
    if not func(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def encrypt(text: str) -> str | None:
    """평문 → base64(DPAPI). 실패 시 None."""
    if sys.platform != "win32" or not text:
        return None
    try:
        enc = _run(ctypes.windll.crypt32.CryptProtectData, text.encode("utf-8"))
        return base64.b64encode(enc).decode("ascii")
    except OSError:
        return None


def decrypt(token: str) -> str | None:
    """base64(DPAPI) → 평문. 실패 시 None."""
    if sys.platform != "win32" or not token:
        return None
    try:
        raw = base64.b64decode(token)
        return _run(ctypes.windll.crypt32.CryptUnprotectData, raw).decode("utf-8")
    except (OSError, ValueError):
        return None
