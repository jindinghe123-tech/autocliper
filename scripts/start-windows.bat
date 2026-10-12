@echo off
rem AutoStudio launcher for Windows.
rem Keep this file ASCII-only: cmd.exe misparses batch files that contain UTF-8 (e.g. Chinese) text.
setlocal
pushd "%~dp0.." || goto :fail

set "VPY=.venv\Scripts\python.exe"
if exist "%VPY%" goto :run

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [AutoStudio] Python 3.10+ was not found.
  echo [AutoStudio] Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  goto :fail
)

echo [AutoStudio] First run: creating a virtual environment and installing dependencies. This takes a few minutes...
%PY% -m venv .venv
if errorlevel 1 goto :fail
"%VPY%" -m pip install --upgrade pip -q
"%VPY%" -m pip install -e ".[all]"
if errorlevel 1 goto :fail

:run
"%VPY%" -m autostudio %*
set "CODE=%errorlevel%"
popd
exit /b %CODE%

:fail
echo [AutoStudio] Setup failed. See the messages above.
popd
pause
exit /b 1
