@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo 没找到 python，请先装 Python 3.10+ 并加进 PATH
  pause
  exit /b 1
)

python -m snowlink %*
pause
