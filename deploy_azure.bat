@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\azure\push.ps1
pause
