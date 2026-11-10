@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (.venv\Scripts\python.exe scripts\review_demo.py) else (python scripts\review_demo.py)
pause
