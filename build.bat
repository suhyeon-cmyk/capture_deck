@echo off
rem CaptureDeck.exe 빌드 (결과: dist\CaptureDeck.exe)
chcp 65001 >nul
cd /d "%~dp0"
python make_icon.py
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name CaptureDeck ^
  --icon capturedeck.ico ^
  --add-data "capturedeck.ico;." ^
  --version-file version_info.txt ^
  --collect-data imageio_ffmpeg ^
  --collect-all pygrabber ^
  --hidden-import sounddevice ^
  usb3_capture.py
