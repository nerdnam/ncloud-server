"""genDISK 앱 로고/아이콘 — 실제 로고 PNG(win_x64/logo/gendisk-icon.png)를 그대로 쓴다.

이 PNG 는 안드로이드/Play 스토어와 동일한 원본 디자인(win_x64/logo/gendisk-icon.svg 렌더).
트레이 아이콘·창 아이콘·헤더 로고에 모두 이 그림을 리샘플해서 쓴다.
"""
import os
import sys


def _png_source() -> str:
    """번들된 로고 PNG 경로. onefile 은 _MEIPASS, 소스 실행은 win_x64/logo/."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "gendisk-icon.png")
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logo", "gendisk-icon.png")


def render_icon(size: int):
    """로고 PNG 를 size×size 로 리샘플해 돌려준다 (트레이·헤더 로고용)."""
    from PIL import Image

    return Image.open(_png_source()).convert("RGBA").resize((size, size), Image.LANCZOS)


def icon_path() -> str:
    """번들된 gendisk.ico 경로 (창 제목줄·exe 아이콘). onefile 은 _MEIPASS."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "gendisk.ico")
