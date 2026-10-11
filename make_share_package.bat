@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo Build wechat_exp.exe and fill share_package\ for end users
echo.

set PY=%LocalAppData%\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

echo [*] Building exe...
"%PY%" -m PyInstaller --noconfirm --onefile --console --name wechat_exp --paths src --add-data "src;src" --add-data "tools\silk_decoder.exe;tools" --hidden-import Crypto.Cipher.AES --hidden-import Crypto.Util.Padding --hidden-import flask --hidden-import werkzeug --hidden-import jinja2 --hidden-import blackboxprotobuf --hidden-import zstandard --hidden-import openpyxl --hidden-import jieba --hidden-import pypinyin --hidden-import psutil --hidden-import requests --exclude-module pytest --exclude-module tkinter src\main.py
if not exist "%~dp0dist\wechat_exp.exe" (
  echo [!] Build failed
  pause
  exit /b 1
)

if exist "%~dp0share_package" rmdir /s /q "%~dp0share_package"
mkdir "%~dp0share_package"
copy /y "%~dp0dist\wechat_exp.exe" "%~dp0share_package\wechat_exp.exe" >nul
copy /y "%~dp0share_template\*" "%~dp0share_package\" >nul

echo.
echo [+] Ready: share_package\
echo     Zip that folder and send it. Do NOT include backup/export/config.
explorer "%~dp0share_package"
pause
