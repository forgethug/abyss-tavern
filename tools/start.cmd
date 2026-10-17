@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo Please run tools\setup.cmd first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -X utf8 tools\start.py
if errorlevel 1 pause
