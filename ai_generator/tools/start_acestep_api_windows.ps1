$ErrorActionPreference = "Stop"

$Repo = "D:\voice_models\ace_step\ACE-Step-1.5"
$Port = 8001
$env:Path = "C:\Users\Admin\.local\bin;$env:Path"

if (-not (Test-Path $Repo)) {
    throw "ACE-Step repo not found. Run install_acestep_windows.ps1 first."
}

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existing) {
    Write-Host "ACE-Step API already listening on port $Port, pid=$($existing.OwningProcess)"
    exit 0
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is not installed. Run install_acestep_windows.ps1 first."
}

$env:ACESTEP_INIT_LLM = "auto"
$env:ACESTEP_NO_INIT = "true"
Start-Process -FilePath "uv" -ArgumentList @("run", "--no-sync", "acestep-api", "--host", "127.0.0.1", "--port", "$Port") -WorkingDirectory $Repo -WindowStyle Hidden

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 4
    $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listening) {
        Write-Host "ACE-Step API started on http://127.0.0.1:$Port, pid=$($listening.OwningProcess)"
        exit 0
    }
}

throw "ACE-Step API did not start within 120 seconds. Run D:\voice_models\ace_step\ACE-Step-1.5\start_api_server.bat manually to see logs."
