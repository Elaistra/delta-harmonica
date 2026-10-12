@echo off
chcp 65001 >nul
title 口琴演奏器 · 新版
cd /d "%~dp0"

REM ── 提权（与旧版 requireAdministrator 一致）─────────────────────────
REM 旧版 exe 的 manifest 是 requireAdministrator；新版用 bat 自提权达到同一效果：
REM   * 未提权时走 fltmc 检测 → 弹系统 UAC 确认框 → 以管理员重启本脚本
REM   * 提权只改变"按键能被哪些完整性级别的窗口接收"，不改变事件本身的性质
REM   * 每次启动都会弹一次 UAC —— 系统行为，确认即可
>nul 2>&1 fltmc || (
    echo  正在请求管理员权限，请在弹出的 UAC 窗口点"是"...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs -WorkingDirectory '%~dp0'"
    if errorlevel 1 (
        echo.
        echo  [!] 提权失败：多半是 UAC 窗口被点了"否"，或系统禁止了提权。
        echo      不提权也可以直接用（按键可能被部分窗口忽略）：
        echo      直接把本窗口关掉，然后双击 hca 文件夹里的「启动-不提权.bat」
        timeout /t 8 >nul
    )
    exit /b
)

REM ★ 用 %LOCALAPPDATA% 而不是写死 C:\Users\某人 —— 开源分发时不带私人目录，
REM   换台机器也能自己找到 Python。找不到就退回 PATH 里的 python。
set PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe
if not exist "%PY%" set PY=python
set PYW=%PY:python.exe=pythonw.exe%
if not exist "%PYW%" set PYW=%PY%

REM 双击（不带参数）= 直接打开界面；带 cli 参数 = 命令行菜单
if /i not "%~1"=="cli" goto gui

:menu
echo  口琴演奏器 · 新版 —— 命令行 / 自检入口（已提权）
echo  ────────────────────────────────────────────
echo   [1] 列出曲库
echo   [2] 看某一首的解析结果
echo   [3] 预演演奏计划（不发按键）
echo   [4] 真弹（会发按键！目标窗口要在前台）
echo   [5] 解析层 + 动作层 验收（对旧内核逐条比）
echo   [6] 底线自检（不改内存 / 不碰规避性质的 API）
echo   [7] 界面自检（不显示窗口、不发按键）
echo   [8] 打开网页参照器（验证扫描码 -^> e.code）
echo   [9] 退出
echo.
set /p CH= 选一个（1-9）：
if "%CH%"=="1" call :run --list
if "%CH%"=="2" (set /p S= 曲名关键字：& call :run --info "%S%")
if "%CH%"=="3" (set /p S= 曲名关键字：& call :run --song "%S%" --dry-run)
if "%CH%"=="4" (set /p S= 曲名关键字：& call :run --song "%S%")
if "%CH%"=="5" "%PY%" verify\golden_regression.py --actions
if "%CH%"=="6" "%PY%" verify\redline_check.py
if "%CH%"=="7" "%PY%" -m hca.ui --selftest
if "%CH%"=="8" start "" "%~dp0web\harmonica.html"
if "%CH%"=="9" exit /b 0
echo.
pause
goto :eof

:run
"%PY%" -m hca.cli %*
goto :eof

:gui
REM 走到这里 = fltmc 通过 = 已经是管理员（.lnk 的 RunAsUser 或 UAC 提权后都会到这）
REM 用环境变量告诉程序"当前是管理员"，窗口标题会显示出来
set HCA_ELEVATED=1
start "" "%PYW%" -m hca.ui
exit /b 0
