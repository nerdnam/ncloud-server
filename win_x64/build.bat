@echo off
REM gendisk-sync 를 단일 .exe 로 빌드 (win_x64\dist\gendisk-sync.exe)
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller
python -m PyInstaller --noconfirm --clean gendisk-sync.spec

echo.
echo 빌드 완료: dist\gendisk-sync.exe
pause
