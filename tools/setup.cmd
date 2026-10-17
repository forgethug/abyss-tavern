@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
where python >nul 2>nul
if errorlevel 1 (
    echo Please install Python 3.12+ and add Python to PATH, then run this file again.
    pause
    exit /b 1
)
python -X utf8 tools\setup.py
if errorlevel 1 pause
