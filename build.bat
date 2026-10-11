@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo.
echo ============================================================
echo   WeChat Export - PyInstaller Build Script (Optimized)
echo ============================================================
echo.

set VENV=%~dp0build_venv\Scripts
set PYTHON=%VENV%\python.exe
set PIP=%VENV%\pip.exe

if not exist "%PYTHON%" (
    echo [!] Build venv not found, creating...
    python -m venv "%~dp0build_venv"
    "%PIP%" install --upgrade pip
    "%PIP%" install -r "%~dp0requirements-build.txt"
)

echo [*] Installing/updating dependencies...
"%PIP%" install -r "%~dp0requirements-build.txt"

echo.
echo [*] Downloading Whisper model for offline voice transcription...
"%PYTHON%" "%~dp0scripts\download_whisper_model.py"
if %errorlevel% neq 0 (
    echo [!] Model download failed — continuing build (voice transcription will use CDN fallback)
)

echo.
echo [*] Cleaning previous build and stray config files...
if exist "%~dp0build" rmdir /s /q "%~dp0build"
if exist "%~dp0dist" rmdir /s /q "%~dp0dist"
:: Remove .wechat_exp_config.json from src/ so it won't be bundled into the exe
if exist "%~dp0src\.wechat_exp_config.json" del /q "%~dp0src\.wechat_exp_config.json"

echo.
echo [*] Building...
echo.

:: Set build date for reproducible version embedding (SOURCE_DATE_EPOCH)
:: Use current timestamp; the version module reads this to produce YYYYMMDD
:: Get build date and epoch via Python (output to temp file to avoid quoting issues)
"%PYTHON%" -c "from datetime import datetime,timezone; now=datetime.now(timezone.utc); print(int(datetime(now.year,now.month,now.day,tzinfo=timezone.utc).timestamp()))" > "%TEMP%\wexp_epoch.txt"
set /p SOURCE_DATE_EPOCH=<"%TEMP%\wexp_epoch.txt"
"%PYTHON%" -c "from datetime import datetime,timezone; now=datetime.now(timezone.utc); print('%04d%02d%02d' %% (now.year,now.month,now.day))" > "%TEMP%\wexp_date.txt"
set /p BUILD_DATE=<"%TEMP%\wexp_date.txt"

echo [*] Build version date: %BUILD_DATE%

echo [*] Generating version info file...
"%PYTHON%" "%~dp0scripts\gen_version_file.py"
set /p APP_VERSION=<"%~dp0file_version.txt"
echo [*] App version: %APP_VERSION%

echo.
echo [*] Building README.html manual...
"%PYTHON%" "%~dp0scripts\build_readme_html.py"
if %errorlevel% neq 0 (
    echo [!] Manual generation failed — continuing build without manual
)

"%PYTHON%" -m PyInstaller ^
    --onefile ^
    --console ^
    --name wechat_exp ^
    --version-file "%~dp0file_version_info.txt" ^
    --add-data "src;src" ^
    --add-data "tools\silk_decoder.exe;tools" ^
    --add-data "tools\silk_decoder.c;tools" ^
    --hidden-import Crypto.Cipher.AES ^
    --hidden-import Crypto.Util.Padding ^
    --hidden-import flask ^
    --hidden-import werkzeug ^
    --hidden-import jinja2 ^
    --hidden-import blackboxprotobuf ^
    --hidden-import zstandard ^
    --hidden-import openpyxl ^
    --hidden-import jieba ^
    --hidden-import jieba.posseg ^
    --hidden-import requests ^
    --hidden-import zhconv ^
    --hidden-import lameenc ^
    --collect-all zhconv ^
    --hidden-import pypinyin ^
    --hidden-import psutil ^
    --collect-all jieba ^
    --exclude-module pytest ^
    --exclude-module _pytest ^
    --exclude-module tkinter ^
    --exclude-module _tkinter ^
    --exclude-module turtle ^
    --exclude-module idlelib ^
    --exclude-module ensurepip ^
    --exclude-module pip ^
    --exclude-module setuptools ^
    --exclude-module wheel ^
    --exclude-module pkg_resources ^
    --exclude-module multiprocessing ^
    --exclude-module concurrent.futures.process ^
    --exclude-module lib2to3 ^
    --exclude-module xmlrpc ^
    --exclude-module pydoc ^
    --exclude-module doctest ^
    --exclude-module bdb ^
    --strip ^
    "%~dp0src\main.py"

echo.
:: Rename with version suffix
if exist "%~dp0dist\wechat_exp.exe" (
    set "RENAMED=%~dp0dist\wechat_exp_!APP_VERSION!.exe"
    move "%~dp0dist\wechat_exp.exe" "!RENAMED!" >nul
)

echo ============================================================
echo   Build complete!
echo   Output: dist\wechat_exp_!APP_VERSION!.exe
echo ============================================================

:: Show file size
for %%F in ("%~dp0dist\wechat_exp_!APP_VERSION!.exe") do (
    set /a SIZE=%%~zF
    echo   Size: !SIZE! bytes
)

pause
