@echo off
cd /d "E:\project\reels\shorts-maker"
start http://localhost:8000
venv\Scripts\python.exe -m uvicorn server:app --port 8000 --reload
pause