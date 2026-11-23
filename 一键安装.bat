@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title 股票分析工作台 - Windows 一键安装 beta0.1

cd /d "%~dp0"

echo ============================================================
echo   股票分析工作台 beta0.1  -  Windows 一键安装
echo ============================================================
echo.
echo 本脚本将自动完成：
echo   1. 检测 Python 3.12（缺失则用 winget 自动安装）
echo   2. 创建独立虚拟环境 venv\
echo   3. 从国内镜像安装全部依赖
echo   4. 创建桌面快捷方式
echo   5. 启动服务并打开浏览器
echo.
echo 说明：需要 Windows 10 1809+ 或 Windows 11（含 winget）。
echo       全程联网仅用于安装 Python 与依赖，不上传任何本地数据。
echo.
pause

REM ---------- 1. 定位 Python ----------
set "PYEXE="
for %%P in ("python.exe" "py.exe") do (
    where %%P >nul 2>&1
    if !errorlevel! equ 0 if not defined PYEXE (
        for /f "usebackq delims=" %%I in (`where %%P 2^>nul`) do (
            if not defined PYEXE set "PYEXE=%%I"
        )
    )
)

REM 校验已找到的 Python 是否 >=3.12（低于则尝试 winget 安装）
if defined PYEXE (
    for /f "usebackq tokens=2 delims= " %%V in (`""%PYEXE%" -c "import sys;print(str(sys.version_info[0])+'.'+str(sys.version_info[1]))" 2^>nul"`) do set "PYVER=%%V"
    echo 检测到 Python !PYVER! 于 !PYEXE!
    for /f "tokens=1,2 delims=." %%a in ("!PYVER!") do (
        if %%a LSS 3 ( set "PYEXE=" ) else if %%a EQU 3 if %%b LSS 12 ( set "PYEXE=" )
    )
)

if not defined PYEXE (
    echo [1/5] 未找到 Python 3.12+，尝试用 winget 自动安装...
    winget --version >nul 2>&1
    if !errorlevel! neq 0 (
        echo [错误] 本机没有 winget，无法自动安装 Python。
        echo 请手动安装 Python 3.12（https://www.python.org/downloads/ ，勾选 Add to PATH）后重新运行本脚本。
        pause
        exit /b 1
    )
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    if !errorlevel! neq 0 (
        echo [错误] winget 安装 Python 失败，请检查网络或手动安装。
        pause
        exit /b 1
    )
    echo 安装完成，正在刷新环境变量...
    call refreshenv.cmd >nul 2>&1
    for /f "usebackq delims=" %%I in (`where python 2^>nul`) do (
        if not defined PYEXE set "PYEXE=%%I"
    )
    if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    )
)

if not defined PYEXE (
    echo [错误] 仍无法定位 Python，请手动安装后重试。
    pause
    exit /b 1
)
echo 使用 Python: !PYEXE!

REM ---------- 2. 虚拟环境 ----------
if not exist "venv\Scripts\python.exe" (
    echo [2/5] 创建虚拟环境 venv\ ...
    "!PYEXE!" -m venv venv
    if !errorlevel! neq 0 (
        echo [错误] 创建虚拟环境失败。
        pause
        exit /b 1
    )
) else (
    echo [2/5] 虚拟环境已存在，跳过。
)
set "VPY=%CD%\venv\Scripts\python.exe"

REM ---------- 3. 依赖 ----------
echo [3/5] 安装/更新依赖（阿里云镜像）...
"!VPY!" -m pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/ >nul 2>&1
"!VPY!" -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
if !errorlevel! neq 0 (
    echo [错误] 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
)

REM ---------- 4. 桌面快捷方式 ----------
echo [4/5] 创建桌面快捷方式...
set "DESKTOP=%USERPROFILE%\Desktop"
if not exist "%DESKTOP%" set "DESKTOP=%USERPROFILE%\OneDrive\Desktop"
set "LNK=%DESKTOP%\股票分析工作台.lnk"
powershell -NoProfile -Command ^
  "$ws=New-Object -ComObject WScript.Shell;" ^
  "$s=$ws.CreateShortcut('%LNK%');" ^
  "$s.TargetPath='%~dp0启动分析工具.bat';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "$s.Description='股票分析工作台 beta0.1';" ^
  "$s.Save()" >nul 2>&1
if exist "%LNK%" echo 快捷方式已创建: %LNK%

REM ---------- 5. 启动 ----------
echo [5/5] 启动服务...
start "" http://127.0.0.1:8899
start "股票分析工作台服务" /min "!VPY!" "%CD%\backend\app.py" --port 8899
echo.
echo ============================================================
echo   安装完成！服务正在后台启动。
echo   浏览器已打开 http://127.0.0.1:8899
echo   若页面未自动出现，稍等数秒后手动访问上述地址。
echo   首次启动会解析本地客户端的股票名称表，约需数秒。
echo.
echo   日常使用：双击桌面「股票分析工作台」快捷方式即可。
echo   停止服务：任务管理器结束 python.exe，或运行 停止服务.bat
echo ============================================================
echo.
pause
