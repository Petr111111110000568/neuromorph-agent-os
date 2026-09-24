@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -m workbench local-network %*
) else (
  py -3 -m workbench local-network %*
)
if errorlevel 1 pause
