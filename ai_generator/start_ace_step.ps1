$ErrorActionPreference = "Stop"

$mutexName = "Global\ChangTingAceStepStarter"
$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
if (-not $createdNew) {
    Write-Host "ACE-Step starter is already running."
    exit 0
}

$AceRoot = "D:\voice_models\ace_step\ACE-Step-1.5"
$AcePython = Join-Path $AceRoot ".venv\Scripts\python.exe"
$AceApi = Join-Path $AceRoot ".venv\Scripts\acestep-api.exe"
$LogDir = "D:\music_ai_generator\logs"
$HealthUrl = "http://127.0.0.1:8001/health"
$InitUrl = "http://127.0.0.1:8001/v1/init"
$ModelName = $env:ACE_STEP_INIT_MODEL
if ([string]::IsNullOrWhiteSpace($ModelName)) {
    $ModelName = "acestep-v15-turbo"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Test-AceStep {
    try {
        Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 8 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Test-AceStepReady {
    try {
        $response = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 8
        return [bool]$response.data.models_initialized
    } catch {
        return $false
    }
}

function Initialize-AceStep {
    try {
        $payload = @{
            model = $ModelName
            init_llm = $false
        } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri $InitUrl -Method Post -ContentType "application/json" -Body $payload -TimeoutSec 1200 | Out-Null
        return Test-AceStepReady
    } catch {
        Write-Warning "ACE-Step init failed: $($_.Exception.Message)"
        return $false
    }
}

if (Test-AceStepReady) {
    Write-Host "ACE-Step already ready: $HealthUrl"
    exit 0
}

if (Test-AceStep) {
    if (Initialize-AceStep) {
        Write-Host "ACE-Step initialized: $HealthUrl"
        exit 0
    }
    Write-Warning "ACE-Step is healthy but model initialization failed."
    exit 0
}

if (!(Test-Path -LiteralPath $AcePython)) {
    throw "ACE-Step python not found: $AcePython"
}
if (!(Test-Path -LiteralPath $AceApi)) {
    throw "ACE-Step API entry not found: $AceApi"
}

$listener = Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Warning "Port 8001 is already listening, but health check failed. ProcessId=$($listener.OwningProcess)"
    exit 2
}

$stdout = Join-Path $LogDir "ace_step.out.log"
$stderr = Join-Path $LogDir "ace_step.err.log"
Start-Process `
    -FilePath $AcePython `
    -ArgumentList @($AceApi, "--host", "127.0.0.1", "--port", "8001") `
    -WorkingDirectory $AceRoot `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    if (Test-AceStep) {
        if (Initialize-AceStep) {
            Write-Host "ACE-Step started and initialized: $HealthUrl"
            exit 0
        }
    }
}

throw "ACE-Step did not become healthy. See logs: $stdout, $stderr"
