@echo off
:: Set current directory
cd /d "%~dp0"

echo [1/2] Launching Python Bot...
if not exist "venv\Scripts\python.exe" (
    echo Error: venv not found. Please run 'python -m venv venv'.
    pause
    exit /b
)
:: Fail before opening the tunnel if security configuration is missing.
.\venv\Scripts\python.exe -c "from config import config; config.validate()"
if errorlevel 1 (
    echo Please complete .env security settings according to README.md.
    pause
    exit /b 1
)
for /f %%P in ('.\venv\Scripts\python.exe -c "from config import config; print(config.BOT_PORT)"') do set "TALKBOT_PORT=%%P"
:: Start Bot in a new window
start "NingNing_Bot" cmd /c ".\venv\Scripts\activate && python main.py"

timeout /t 3

echo [2/2] Launching Cloudflare Tunnel...
:: Change the path if it is different
set "CF_PATH=D:\Software\Cloudflared\cloudflared.exe"

if exist "%CF_PATH%" (
    start "CF_Tunnel" cmd /c ""%CF_PATH%" tunnel --url http://127.0.0.1:%TALKBOT_PORT%"
) else (
    echo Error: %CF_PATH% not found.
)

echo ----------------------------------------------------
echo Done! 
echo 1. Check the Tunnel window for the https URL.
echo 2. Copy it to your NapCat HTTP Client config.
echo ----------------------------------------------------
pause
