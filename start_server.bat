@echo off
setlocal
cd /d "E:\HuaweiMoveData\Users\HUAWEI\Desktop\shiyi-coach"

echo Starting Shiyi Coach...
echo Project: %CD%
echo.

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: project virtual environment was not found.
  echo Expected: .venv\Scripts\python.exe
  pause
  exit /b 1
)

start "Shiyi Coach Browser" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8765"

".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8765 --log-level info

echo.
echo The server stopped. Check the error shown above.
pause
