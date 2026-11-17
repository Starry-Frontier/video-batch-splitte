@echo off
setlocal
cd /d "%~dp0"

if not exist "bin\ffmpeg.exe" (
    echo Missing bin\ffmpeg.exe. See bin\README.md.
    goto :error
)
if not exist "bin\ffprobe.exe" (
    echo Missing bin\ffprobe.exe. See bin\README.md.
    goto :error
)

if not exist ".build-venv\Scripts\python.exe" (
    python -m venv .build-venv || goto :error
)

".build-venv\Scripts\python.exe" -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
    ".build-venv\Scripts\python.exe" -m pip install -r requirements-dev.txt || goto :error
)

".build-venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm VideoSplitter.spec || goto :error
echo.
echo Build completed: dist\视频批量分割器.exe
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
