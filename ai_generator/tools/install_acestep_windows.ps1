$ErrorActionPreference = "Stop"

$Root = "D:\voice_models\ace_step"
$Repo = Join-Path $Root "ACE-Step-1.5"
$Python = "python"

New-Item -ItemType Directory -Force -Path $Root | Out-Null

if (-not (Test-Path $Repo)) {
    git clone https://github.com/ace-step/ACE-Step-1.5.git $Repo
}

Set-Location $Repo

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    if (Test-Path ".\install_uv.bat") {
        cmd /c ".\install_uv.bat"
    } else {
        & $Python -m pip install --upgrade uv
    }
}

$env:Path = "C:\Users\Admin\.local\bin;$env:Path"
uv sync

Write-Host "ACE-Step install finished: $Repo"
Write-Host "Next: run tools\start_acestep_api_windows.ps1"
