@echo off
chcp 65001 >nul
title 股票分析工作台
cd /d "%~dp0"

REM 优先使用一键安装创建的虚拟环境，否则退回系统 Python
set "PYEXE=%CD%\venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

echo ============================================================
echo   股票分析工作台 beta0.1
echo ============================================================
echo.
echo 数据源：通达信内核券商客户端本地盘后数据
echo 正在启动，请稍候...
echo.

REM 检查端口是否已被占用，避免重复启动
netstat -ano | findstr ":8899" | findstr "LISTENING" >nul 2>&1
if %errorlevel% equ 0 (
    echo 服务已在运行，直接打开浏览器。
    start "" http://127.0.0.1:8899
    timeout /t 3 >nul
    exit /b 0
)

start "" http://127.0.0.1:8899
"%PYEXE%" backend\app.py --port 8899

pause
