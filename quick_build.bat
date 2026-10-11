@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

:: Lightweight build: skip Whisper model download; output dist\wechat_exp.exe
:: For a full release build, use build.bat / build_exe.bat

set PY=%LocalAppData%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

echo [*] Python: %PY%
echo [*] Installing build dependencies...
"%PY%" -m pip install -r requirements-build.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 120
if errorlevel 1 (
  echo [!] Dependency install failed
  pause
  exit /b 1
)

if exist "%~dp0build" rmdir /s /q "%~dp0build"
if exist "%~dp0dist\wechat_exp.exe" del /q "%~dp0dist\wechat_exp.exe"

echo [*] PyInstaller packaging (may take a few minutes)...
"%PY%" -m PyInstaller ^
    --noconfirm --clean --onefile --console ^
    --name wechat_exp ^
    --paths "%~dp0src" ^
    --add-data "%~dp0src;src" ^
    --add-data "%~dp0README.md;." ^
    --add-data "%~dp0tools\silk_decoder.exe;tools" ^
    --hidden-import Crypto.Cipher.AES ^
    --hidden-import Crypto.Util.Padding ^
    --hidden-import flask ^
    --hidden-import werkzeug ^
    --hidden-import jinja2 ^
    --hidden-import markdown ^
    --hidden-import blackboxprotobuf ^
    --hidden-import zstandard ^
    --hidden-import openpyxl ^
    --hidden-import jieba ^
    --hidden-import pypinyin ^
    --hidden-import psutil ^
    --hidden-import requests ^
    --exclude-module pytest ^
    --exclude-module tkinter ^
    "%~dp0src\main.py"

if exist "%~dp0dist\wechat_exp.exe" (
  echo.
  echo [+] Done: dist\wechat_exp.exe
  echo     You can now double-click start.bat
) else (
  echo [!] Output not found. Check errors above.
)
pause
