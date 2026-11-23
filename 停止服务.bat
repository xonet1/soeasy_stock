@echo off
chcp 65001 >nul
title 停止股票分析工作台服务
echo 正在查找占用 8899 端口的进程...

set "FOUND=0"
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8899" ^| findstr "LISTENING"') do (
    if "%%P" neq "0" (
        echo 结束进程 PID=%%P
        taskkill /PID %%P /F >nul 2>&1
        set "FOUND=1"
    )
)
if "%FOUND%" equ "1" (
    echo 服务已停止。
) else (
    echo 未发现正在运行的服务。
)
echo.
pause
