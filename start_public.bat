@echo off
rem One double-click: Saarthi live on this laptop with public links (CEO office + one per driver).
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scripts\start_public.py
) else (
  python scripts\start_public.py
)
pause
