@echo off
setlocal
cd /d "%~dp0"

for /f "usebackq delims=" %%i in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$ports=5000,5001,5002,8000,8001,8080; foreach($p in $ports){$l=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,$p); try{$l.Start();$l.Stop();$p;break}catch{if($l){$l.Stop()}}}"`) do set FREE_PORT=%%i
if not defined FREE_PORT set FREE_PORT=5001

set "PYTHON_EXE="
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import flask,cv2,easyocr" >nul 2>nul
  if not errorlevel 1 set "PYTHON_EXE=.venv\Scripts\python.exe"
)
if not defined PYTHON_EXE (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_EXE=python"
)
if not defined PYTHON_EXE (
  echo Required Python packages are missing.
  echo Run: python -m pip install -r requirements.txt
  pause
  exit /b 1
)

set "PORT=%FREE_PORT%"
echo Starting Legal Metrology Scanner on http://127.0.0.1:%FREE_PORT%
start "Legal Metrology Scanner" /b %PYTHON_EXE% app_example.py
powershell -NoProfile -ExecutionPolicy Bypass -Command "$url='http://127.0.0.1:%FREE_PORT%/health'; for($i=0;$i -lt 60;$i++){try{$r=Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2;if($r.StatusCode -eq 200){Start-Process 'http://127.0.0.1:%FREE_PORT%';exit 0}}catch{};Start-Sleep -Seconds 1}; exit 1"
if errorlevel 1 (
  echo Flask did not become ready. Check the server process for errors.
  pause
)
