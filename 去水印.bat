@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

rem 挑一个装了 opencv 的 Python（系统里可能有多套）
set "PY="
call :Probe "python"
call :Probe "py -3.11"
call :Probe "py -3"
if not defined PY (
  echo [错误] 没找到已安装 opencv-python 的 Python。
  echo 请在命令行执行:  pip install opencv-python numpy
  echo 或直接改用命令行模式，用 --box 手动指定水印位置。
  echo.
  pause
  exit /b 1
)

set "OUT=%~dp0output"
if not exist "%OUT%" mkdir "%OUT%"

echo ==================================================
echo   视频批量去水印
echo   使用的 Python: %PY%
echo   输出目录: %OUT%
echo ==================================================
echo.

if "%~1"=="" (
  echo [用法] 把一个或多个视频文件、或整个文件夹拖到本文件上再松手。
  echo        也可以直接双击，处理本文件夹下的所有视频。
  echo.
)

%PY% "%~dp0dewatermark.py" %* -o "%OUT%" --recursive
echo.
echo 处理结束，按任意键关闭...
pause >nul
exit /b 0

:Probe
if defined PY exit /b 0
%~1 -c "import cv2, numpy" >nul 2>nul
if not errorlevel 1 set "PY=%~1"
exit /b 0
