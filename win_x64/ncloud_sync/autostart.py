"""Windows 로그인 시 자동 실행 등록/해제 (HKCU Run 레지스트리 키)."""
import os
import sys

APP_NAME = "ncloud-sync"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _command() -> str:
    """자동 시작 시 실행할 명령. --startup 플래그로 시작하면 창을 최소화한다."""
    if getattr(sys, "frozen", False):        # PyInstaller .exe
        return f'"{sys.executable}" --startup'
    # 스크립트 실행: pythonw로 창 없이
    main = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    pyw = sys.executable
    if pyw.lower().endswith("python.exe"):
        pyw = pyw[:-len("python.exe")] + "pythonw.exe"
    return f'"{pyw}" "{main}" --startup'


def enable():
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _command())


def disable():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME)
    except FileNotFoundError:
        pass


def is_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except FileNotFoundError:
        return False


def sync(enabled: bool):
    """설정값에 맞춰 등록 상태를 맞춘다."""
    if enabled and not is_enabled():
        enable()
    elif not enabled and is_enabled():
        disable()
