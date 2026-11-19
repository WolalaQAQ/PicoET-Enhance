@echo off
if exist "%~dp0..\PicoET-PC-Studio.exe" (
  start "" "%~dp0..\PicoET-PC-Studio.exe" %*
) else if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -B -X utf8 "%~dp0backend.py" %*
) else (
  python -B -X utf8 "%~dp0backend.py" %*
)
