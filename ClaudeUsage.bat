@echo off
rem Starts the Claude Usage widget. The first run sets up a private Python
rem environment in this folder (.venv) and downloads three packages into it.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" goto start

set "PY=python"
where py >nul 2>nul && set "PY=py -3"
echo Setting up Claude Usage - first run only, this takes a minute...
%PY% -m venv .venv
if errorlevel 1 goto nopython
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto nopackages
".venv\Scripts\python.exe" -c "import PIL.Image, pystray, requests"
if errorlevel 1 goto broken

:start
start "" ".venv\Scripts\pythonw.exe" "%~dp0claude_usage_tray.py"
exit /b 0

:nopython
echo.
echo Python 3.10 or newer is needed and was not found.
echo Install it from https://www.python.org/downloads/ - tick "Add python.exe to PATH" - then run this again.
pause
exit /b 1

:nopackages
echo.
echo Could not download the packages the widget needs. Check the internet connection and run this again.
rmdir /s /q .venv
pause
exit /b 1

:broken
echo.
echo The packages were installed but will not load. This usually means this folder's path is too long
echo for Windows. Move the folder somewhere shorter, such as C:\ClaudeUsageTray, and run this again.
rmdir /s /q .venv
pause
exit /b 1
