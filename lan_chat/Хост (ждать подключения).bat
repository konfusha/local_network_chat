@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
    python chat.py host
    goto :end
)
where py >nul 2>nul
if %errorlevel%==0 (
    py chat.py host
    goto :end
)
echo Python not found. Install it from https://www.python.org/downloads/
:end
echo.
pause
