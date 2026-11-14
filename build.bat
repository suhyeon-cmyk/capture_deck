@echo off
rem CaptureDeck.exe 빌드 (결과: dist\CaptureDeck.exe)
chcp 65001 >nul
cd /d "%~dp0"

python -c "import PIL, PyInstaller" 2>nul
if errorlevel 1 (
  echo [오류] 빌드에 필요한 패키지가 없습니다. 아래 명령으로 설치하세요:
  echo   python -m pip install opencv-python sounddevice numpy pillow pygrabber imageio-ffmpeg pyinstaller
  goto fail
)

python make_icon.py
if errorlevel 1 goto fail

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name CaptureDeck ^
  --icon capturedeck.ico ^
  --add-data "capturedeck.ico;." ^
  --version-file version_info.txt ^
  --collect-data imageio_ffmpeg ^
  --collect-all pygrabber ^
  --hidden-import sounddevice ^
  usb3_capture.py
if errorlevel 1 goto fail

echo.
echo 빌드 완료: dist\CaptureDeck.exe
pause
exit /b 0

:fail
echo.
echo 빌드 실패. 위 오류 메시지를 확인하세요.
pause
exit /b 1
