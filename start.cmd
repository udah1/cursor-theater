@echo off
REM Launch Cursor Theater (standalone Python server). It opens the browser
REM itself once the server is listening (no first-load race).
cd /d "%~dp0"
python cursor_theater.py
