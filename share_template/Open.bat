@echo off
cd /d "%~dp0"
if not exist "%~dp0wechat_exp.exe" (
  echo wechat_exp.exe not found in this folder.
  pause
  exit /b 1
)
start "" "%~dp0wechat_exp.exe"
echo Started. Browser should open http://127.0.0.1:5000
echo Keep the black console window open while using the app.
timeout /t 4 >nul
