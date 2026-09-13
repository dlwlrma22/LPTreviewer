@echo off
setlocal
title LPT Reviewer Launcher
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\transcript-generator\start-reviewer.ps1"
if errorlevel 1 (
  echo.
  echo The reviewer could not be started. Read the message above for details.
  pause
)
endlocal
