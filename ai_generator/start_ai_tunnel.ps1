$ErrorActionPreference = "Continue"

$mutexName = "Global\ChangTingMusicAiTunnel"
$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
if (-not $createdNew) {
    Write-Host "Music AI tunnel watchdog is already running."
    exit 0
}

$keyPath = Join-Path $env:USERPROFILE ".ssh\music_ai_ed25519"
if (!(Test-Path $keyPath)) {
    throw "SSH key not found: $keyPath"
}

$remote = "root@202.189.51.227"
$bindAddress = ""
try {
    $defaultNet = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address } | Select-Object -First 1
    if ($defaultNet) {
        $bindAddress = $defaultNet.IPv4Address.IPAddress
    }
} catch {
    $bindAddress = ""
}

function Invoke-RemoteSsh($RemoteCommand, [switch]$Tunnel) {
    $args = @(
        "-i", $keyPath,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10"
    )
    if ($bindAddress) {
        $args += @("-b", $bindAddress)
    }
    if ($Tunnel) {
        $args += @(
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-N",
            "-R", "127.0.0.1:18081:127.0.0.1:18080",
            $remote
        )
    } else {
        $args += @("-o", "BatchMode=yes", $remote, $RemoteCommand)
    }
    & ssh @args
}

function Test-LocalGenerator {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:18080/ping" -TimeoutSec 3 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Test-RemoteTunnel {
    Invoke-RemoteSsh "curl -fsS --max-time 3 http://127.0.0.1:18081/ping >/dev/null"
    return $LASTEXITCODE -eq 0
}

while ($true) {
    if (!(Test-LocalGenerator)) {
        Start-Sleep -Seconds 10
        continue
    }

    if (Test-RemoteTunnel) {
        Start-Sleep -Seconds 30
        continue
    }

    Invoke-RemoteSsh "" -Tunnel

    Start-Sleep -Seconds 5
}
