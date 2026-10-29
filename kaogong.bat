@echo off
chcp 65001 >nul
setlocal
set "PY=python"

if "%~1"=="" (
  "%PY%" "%~dp0scripts\kaogong.py" show --days 1
  echo.
  echo 用法: kaogong.bat show --days 3   看最近 3 天
  echo       kaogong.bat --help          全部命令
  pause
  exit /b 0
)

"%PY%" "%~dp0scripts\kaogong.py" %*
