Set-Location $PSScriptRoot

$ports = @(5000, 5001, 5002, 8000, 8001, 8080)
$freePort = $null
foreach ($port in $ports) {
    try {
        $socket = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $port)
        $socket.Start()
        $freePort = $port
        $socket.Stop()
        break
    }
    catch {
    }
}
if (-not $freePort) { $freePort = 5001 }

$env:PORT = [string]$freePort
Write-Host "Starting Legal Metrology Scanner on http://127.0.0.1:$freePort"

$pythonExe = $null
if (Test-Path ".venv\Scripts\python.exe") {
    & ".venv\Scripts\python.exe" -c "import flask,cv2,easyocr" 2>$null
    if ($LASTEXITCODE -eq 0) { $pythonExe = (Resolve-Path ".venv\Scripts\python.exe").Path }
}
if (-not $pythonExe -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $pythonExe = (Get-Command python).Source
}
if (-not $pythonExe) {
    Write-Host "Required Python packages are missing."
    Write-Host "Run: python -m pip install -r requirements.txt"
    Read-Host "Press Enter to exit"
    exit 1
}

$process = Start-Process -FilePath $pythonExe -ArgumentList "app_example.py" -WorkingDirectory $PSScriptRoot -PassThru
$healthUrl = "http://127.0.0.1:$freePort/health"
$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
        if ($response.StatusCode -eq 200) { $ready = $true; break }
    } catch {
    }
    Start-Sleep -Seconds 1
}
if (-not $ready) {
    Write-Host "Flask did not become ready. Process id: $($process.Id)"
    Read-Host "Press Enter to exit"
    exit 1
}
Start-Process "http://127.0.0.1:$freePort"
