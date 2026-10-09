@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE="
for %%C in (python.exe python3.exe python3.15.exe python3.14.exe python3.13.exe python3.12.exe python3.11.exe python3.10.exe) do (
  for /f "delims=" %%P in ('where %%C 2^>nul') do (
    call :check_python "%%P"
    if not errorlevel 1 (
      set "PYTHON_EXE=%%P"
      goto python_found
    )
  )
)

if defined LOCALAPPDATA if exist "%LOCALAPPDATA%\Python" (
  for /r "%LOCALAPPDATA%\Python" %%P in (python.exe python3.*.exe) do (
    if exist "%%~fP" (
      call :check_python "%%~fP"
      if not errorlevel 1 (
        set "PYTHON_EXE=%%~fP"
        goto python_found
      )
    )
  )
)

for %%V in (15 14 13 12 11 10) do (
  for %%P in ("%SystemDrive%\Python3%%V\python3.%%V.exe") do (
    if exist "%%~fP" (
      call :check_python "%%~fP"
      if not errorlevel 1 (
        set "PYTHON_EXE=%%~fP"
        goto python_found
      )
    )
  )
)

for %%P in ("%SystemDrive%\Python3*\python3.*.exe") do (
  if exist "%%~fP" (
    call :check_python "%%~fP"
    if not errorlevel 1 (
      set "PYTHON_EXE=%%~fP"
      goto python_found
    )
  )
)

echo Windows x64 build requires Python 3.10 or newer on the build computer.
echo The legacy Python launcher may conflict with the new Python install manager; this script searches for a usable runtime directly.
pause
exit /b 1

:python_found
"%PYTHON_EXE%" -m pip install -r requirements-build.txt
if errorlevel 1 goto build_failed
"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean --onedir --noconsole --name DailyBot --distpath dist-portable --workpath build-portable --add-data "web;web" app.py
if errorlevel 1 goto build_failed

:build_succeeded
echo Desktop application created at dist-portable\DailyBot\DailyBot.exe
echo Keep the complete DailyBot folder together when moving or distributing it.
pause
exit /b 0

:build_failed
echo Windows build failed. See the messages above.
pause
exit /b 1

:check_python
"%~1" -c "import platform,sys; raise SystemExit(sys.version_info < (3, 10) or platform.machine().lower() not in ('amd64', 'x86_64'))" >nul 2>nul
exit /b %errorlevel%
