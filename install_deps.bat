@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY=
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set PY=%LocalAppData%\Programs\Python\Python312\python.exe
if not defined PY if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set PY=%LocalAppData%\Programs\Python\Python311\python.exe
if not defined PY (
  where python >nul 2>&1 && for /f "delims=" %%i in ('where python') do set PY=%%i & goto :found
)
:found
if not defined PY (
  echo [!] Python not found. Install Python 3.10+ and check "Add to PATH".
  pause
  exit /b 1
)

echo [*] Using: %PY%
echo [*] Installing dependencies (Tsinghua PyPI mirror)...
"%PY%" -m pip install -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple
"%PY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 120
if errorlevel 1 (
  echo [!] Install failed
  pause
  exit /b 1
)
echo [+] Done. Double-click start.bat to run.
pause
