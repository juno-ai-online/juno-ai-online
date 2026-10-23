@echo off
setlocal
cd /d "%~dp0"
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Created .env from .env.example.
  echo Please edit .env and set JUNO_ADMIN_EMAIL and JUNO_ADMIN_PASSWORD before using the Admin Console.
  pause
)
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto failed
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 goto failed
python app.py
pause
exit /b 0
:failed
echo JUNO setup failed. Check Python and your internet connection, then try again.
pause
exit /b 1
