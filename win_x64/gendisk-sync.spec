# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 스펙 — 단일 실행파일(onefile), 창 모드(콘솔 숨김)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['gendisk_sync', 'gendisk_sync.app', 'gendisk_sync.client',
                   'gendisk_sync.config', 'gendisk_sync.engine',
                   'gendisk_sync.autostart', 'gendisk_sync.secret',
                   'gendisk_sync.webdav_mount'],
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
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
