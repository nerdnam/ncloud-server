# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 스펙 — 단일 실행파일(onefile), 창 모드(콘솔 숨김)
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = [
    'gendisk_sync', 'gendisk_sync.app', 'gendisk_sync.client',
    'gendisk_sync.config', 'gendisk_sync.engine',
    'gendisk_sync.autostart', 'gendisk_sync.secret',
    'gendisk_sync.webdav_mount', 'gendisk_sync.icon',
    'pystray', 'pystray._win32',
    'PIL', 'PIL.Image', 'PIL.ImageDraw',
]

# 앱 아이콘(.ico, 창/exe)과 로고 PNG(트레이·헤더 로고)를 번들 루트에 넣는다.
datas += [('gendisk.ico', '.'), ('logo/gendisk-icon.png', '.')]

# customtkinter 는 테마 JSON·폰트 등 데이터 파일을 함께 번들해야 실행된다.
# darkdetect 는 시스템 다크/라이트 감지에 쓰인다.
for pkg in ('customtkinter', 'darkdetect'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='gendisk-sync',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI 앱 — 콘솔 창 숨김
    icon='gendisk.ico',     # 안드로이드 앱과 동일한 genDISK 마크
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
