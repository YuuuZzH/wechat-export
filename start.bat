@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo ============================================================
echo   WeChat Export - Quick Start
echo ============================================================
echo.

:: 1) Prefer packaged exe (wechat_exp.exe or wechat_exp_VERSION.exe)
if exist "%~dp0dist\wechat_exp.exe" (
    echo [*] Starting dist\wechat_exp.exe ...
    start "WeChat Export" "%~dp0dist\wechat_exp.exe"
    goto :opened
)
for %%F in ("%~dp0dist\wechat_exp_*.exe") do (
    echo [*] Starting %%~nxF ...
    start "WeChat Export" "%%~fF"
    goto :opened
)
if exist "%~dp0wechat_exp.exe" (
    echo [*] Starting wechat_exp.exe ...
    start "WeChat Export" "%~dp0wechat_exp.exe"
    goto :opened
)
for %%F in ("%~dp0wechat_exp_*.exe") do (
    echo [*] Starting %%~nxF ...
    start "WeChat Export" "%%~fF"
    goto :opened
)

:: 2) Source mode
set PY=
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PY=%LocalAppData%\Programs\Python\Python311\python.exe"
if not defined PY (
  where python >nul 2>&1 && for /f "delims=" %%i in ('where python') do (
    echo %%i | findstr /i "WindowsApps" >nul || (set "PY=%%i" & goto :run_src)
  )
)

:run_src
if not defined PY (
    echo [!] wechat_exp.exe not found, and no usable Python found
    echo     Run install_deps.bat first, or build_exe.bat / quick_build.bat to create the exe
    echo.
    pause
    exit /b 1
)

echo [*] Source mode: "%PY%" src\main.py
echo [*] Open http://127.0.0.1:5000 in your browser if it does not open automatically
echo [*] Do not close this window; press Ctrl+C to stop
echo.

:: Prefer backup created by packaged exe under dist\backup
set "DEC="
for /f "delims=" %%D in ('dir /b /ad /o-n "%~dp0dist\backup\20*" 2^>nul') do (
  if exist "%~dp0dist\backup\%%D\message" (
    set "DEC=%~dp0dist\backup\%%D"
    goto :have_dec
  )
)
:have_dec
if defined DEC (
  echo [*] Using backup: %DEC%
  "%PY%" "%~dp0src\main.py" serve --decrypted-dir "%DEC%"
) else (
  "%PY%" "%~dp0src\main.py"
)
if errorlevel 1 (
    echo.
    echo [!] Start failed. Run install_deps.bat first.
    pause
)
goto :eof

:opened
echo.
echo Browser should open http://127.0.0.1:5000
echo Do not close the program console window.
timeout /t 3 >nul
goto :eof
