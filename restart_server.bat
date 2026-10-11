@echo off
chcp 65001 >nul
echo [*] Checking port 5000...

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000.*LISTENING"') do (
    echo [*] Killing PID %%a on port 5000...
    taskkill /F /PID %%a 2>nul
)

echo [*] Starting WeChat Export...
python src/main.py
pause
