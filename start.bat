@echo off
setlocal
cd /d "%~dp0"
python video_splitter.py
if errorlevel 1 pause
