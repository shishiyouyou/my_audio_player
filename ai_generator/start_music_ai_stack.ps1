$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

$mutexName = "Global\ChangTingMusicAiStack"
$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
if (-not $createdNew) {
    Write-Host "Music AI stack watchdog is already running."
    exit 0
}

$LogDir = "D:\music_ai_generator\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$StackLog = Join-Path $LogDir "music_ai_stack.log"

function Test-Url($Url) {
    try {
        Invoke-RestMethod -Uri $Url -TimeoutSec 8 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Write-StackLog($Message) {
    $line = "{0} {1}" -f (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $StackLog -Value $line -Encoding UTF8
}

function Test-AceReady {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:8001/health" -TimeoutSec 8
        return [bool]$response.data.models_initialized
    } catch {
        return $false
    }
}

function Test-ProcessCommand($Pattern) {
    try {
        $matched = Get-CimInstance Win32_Process |
            Where-Object { $_.CommandLine -and $_.CommandLine -like $Pattern } |
            Select-Object -First 1
        return [bool]$matched
    } catch {
        return $false
    }
}

function Start-AceIfNeeded {
    if (Test-AceReady) {
        return
    }
    if (Test-ProcessCommand "*start_ace_step.ps1*") {
        return
    }
    Write-StackLog "starting ACE-Step starter"
    Start-Process `
        -FilePath "powershell" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "start_ace_step.ps1")) `
        -WorkingDirectory $PSScriptRoot `
        -WindowStyle Hidden
}

function Start-GeneratorIfNeeded {
    if (Test-Url "http://127.0.0.1:18080/ping") {
        return
    }
    $existing = Get-NetTCPConnection -LocalPort 18080 -State Listen -ErrorAction SilentlyContinue
    if ($existing) {
        return
    }
    Write-StackLog "starting AI generator"
    Start-Process `
        -FilePath "C:\Python314\python.exe" `
        -ArgumentList @("-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18080") `
        -WorkingDirectory $PSScriptRoot `
        -RedirectStandardOutput (Join-Path $LogDir "ai_generator.out.log") `
        -RedirectStandardError (Join-Path $LogDir "ai_generator.err.log") `
        -WindowStyle Hidden
}

function Start-TunnelIfNeeded {
    $keyPath = Join-Path $env:USERPROFILE ".ssh\music_ai_ed25519"
    if (!(Test-Path -LiteralPath $keyPath)) {
        Write-Warning "SSH key not found, tunnel skipped: $keyPath"
        return
    }
    $bindAddress = ""
    try {
        $defaultNet = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address } | Select-Object -First 1
        if ($defaultNet) {
            $bindAddress = $defaultNet.IPv4Address.IPAddress
        }
    } catch {
        $bindAddress = ""
    }
    $sshArgs = @(
        "-i", $keyPath,
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new"
    )
    if ($bindAddress) {
        $sshArgs += @("-b", $bindAddress)
    }
    $sshArgs += @(
        "root@202.189.51.227",
        "curl -fsS --max-time 3 http://127.0.0.1:18081/ping >/dev/null"
    )
    $remoteHealthy = $false
    & ssh @sshArgs
    $remoteHealthy = $LASTEXITCODE -eq 0
    if ($remoteHealthy) {
        return
    }
    if (Test-ProcessCommand "*start_ai_tunnel.ps1*") {
        return
    }
    Write-StackLog "starting AI tunnel watchdog"
    Start-Process `
        -FilePath "powershell" `
        -ArgumentList @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "start_ai_tunnel.ps1")) `
        -WorkingDirectory $PSScriptRoot `
        -WindowStyle Hidden
}

while ($true) {
    Start-AceIfNeeded
    Start-Sleep -Seconds 2
    Start-GeneratorIfNeeded
    Start-Sleep -Seconds 2
    Start-TunnelIfNeeded
    Start-Sleep -Seconds 30
}
