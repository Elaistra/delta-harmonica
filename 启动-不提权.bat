@echo off
chcp 65001 >nul
title 三角洲口琴自动演奏（不提权）
cd /d "%~dp0"
REM 不提权直接启动：SendInput 的按键对普通窗口照常生效，
REM 但可能被以管理员运行的窗口忽略（这就是 run.bat 要提权的原因）。
REM ★ 同 run.bat：不写死私人路径（改用 %LOCALAPPDATA%）
set PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe
if not exist "%PY%" set PY=python
start "" "%PY:python.exe=pythonw.exe%" -m hca.ui
exit /b 0
