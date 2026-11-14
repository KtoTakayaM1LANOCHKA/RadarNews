@echo off
cd /d %~dp0
if not exist .venv (py -m venv .venv)
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
set DEMO_MODE=1
set DEMO_OTP=1
set EDITOR_PASSWORD=change-this-before-deployment
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
