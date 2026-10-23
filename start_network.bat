@echo off
title Shorts Maker - Network Server
cd /d "E:\project\reels\shorts-maker"

:: استخراج عنوان الـ IP الداخلي تلقائياً
set "LOCAL_IP=127.0.0.1"
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4 Address"') do (
    for /f "tokens=* delims= " %%b in ("%%a") do set "LOCAL_IP=%%b"
)

echo ========================================================
echo                SHORTS MAKER SERVER
echo ========================================================
echo.
echo [1] Local Link (This PC):
echo     http://localhost:8000
echo.
echo [2] Network Link (Other Devices / Phone on Wi-Fi):
echo     http://%LOCAL_IP%:8000
echo.
echo ========================================================
echo Starting server... Press Ctrl+C to stop.
echo.

:: فتح الرابط على المتصفح باستخدام عنوان الشبكة
start http://%LOCAL_IP%:8000

:: تشغيل السيرفر ليكون متاحاً لجميع أجهزة الشبكة
"E:\project\reels\shorts-maker\venv\Scripts\python.exe" -m uvicorn server:app --host 0.0.0.0 --port 8000

pause