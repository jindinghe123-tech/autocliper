@echo off
rem AutoStudio for Windows - just double-click this file.
rem First run downloads and installs everything it needs into this folder (no admin rights, nothing
rem system-wide), later runs start in seconds. The work is done by scripts\setup-windows.ps1.
rem Keep this file ASCII-only: cmd.exe misparses batch files that contain UTF-8 (e.g. Chinese) text.
setlocal
cd /d "%~dp0"
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "scripts\setup-windows.ps1" goto :not_extracted

"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup-windows.ps1" %*
if errorlevel 1 pause
exit /b

:not_extracted
rem Double-clicked inside the zip viewer: Windows extracted only this file to a temp folder.
"%PS%" -NoProfile -Command "Write-Host ([regex]::Unescape('\u8bf7\u5148\u628a\u4e0b\u8f7d\u7684\u538b\u7f29\u5305\u5b8c\u6574\u89e3\u538b\uff08\u53f3\u952e \u2192 \u5168\u90e8\u89e3\u538b\u7f29\uff09\uff0c\u518d\u53cc\u51fb\u89e3\u538b\u51fa\u6765\u7684\u6587\u4ef6\u5939\u91cc\u7684 AutoStudio.bat\u3002')) -ForegroundColor Yellow"
pause
exit /b 1
