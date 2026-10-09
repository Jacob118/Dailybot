@echo off
chcp 65001 >nul
cd /d "%~dp0"

py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul
if not errorlevel 1 goto use_py

python -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul
if not errorlevel 1 goto use_python

echo 未找到可用的 Python 3.10 或更新版本。
echo 请安装 Python 后重新启动 DailyBot。
pause
exit /b 1

:use_py
py -3 app.py
goto finish

:use_python
python app.py

:finish
set "launch_status=%errorlevel%"
if not "%launch_status%"=="0" pause
exit /b %launch_status%