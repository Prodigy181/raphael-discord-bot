@echo off
cd /d "%~dp0"
echo Installing Raphael libraries...
py -m pip install -r requirements.txt
if not exist .env (
  copy config.example.env .env
  echo Created .env - fill it in, save, then close Notepad.
  notepad .env
)
echo.
echo Setup finished. Now double-click start-raphael.bat
pause
