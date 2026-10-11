@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo Calling build.bat to package wechat_exp.exe
echo This may take a while. Python must be installed.
echo When finished, the exe will be under dist\. Then run start.bat.
echo.
call "%~dp0build.bat"
