@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0si104_launcher.py"
) else (
  where py >nul 2>&1
  if errorlevel 1 (
    python "%~dp0si104_launcher.py"
  ) else (
    py -3 "%~dp0si104_launcher.py"
  )
)
if errorlevel 1 (
  echo SI104 控制台启动失败，请查看上方错误信息。
  pause
  exit /b 1
)
