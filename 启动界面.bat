@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

rem 找一个同时装了 opencv 和 pywebview 的 Python
set "PY="
call :Probe "python"
call :Probe "py -3.11"
call :Probe "py -3"
if not defined PY (
  echo [错误] 没找到装好依赖的 Python。
  echo 请先执行:  pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

for /f "delims=" %%i in ('%PY% -c "import sys,os;p=os.path.join(os.path.dirname(sys.executable),'pythonw.exe');print(p if os.path.exists(p) else sys.executable)"') do set "PYW=%%i"
if not defined PYW set "PYW=%PY%"

start "" "%PYW%" "%~dp0app.py"
exit /b 0

:Probe
if defined PY exit /b 0
%~1 -c "import cv2, webview" >nul 2>nul
if not errorlevel 1 set "PY=%~1"
exit /b 0
