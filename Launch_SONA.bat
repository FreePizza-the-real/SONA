@echo off
setlocal
cd /d "%~dp0"
py -3 "%~dp0SONA_LiveLyrics.py"
if errorlevel 1 (
    echo.
    echo SONA could not start. Check that Python, PySide6, and winrt-Windows.Media.Control are installed.
    pause
)
endlocal
