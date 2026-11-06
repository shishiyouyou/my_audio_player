$ErrorActionPreference = "Stop"

$backendRoot = Split-Path -Parent $PSScriptRoot
$androidRoot = Join-Path $backendRoot "..\android"
$androidRoot = [System.IO.Path]::GetFullPath($androidRoot)

Write-Host "== Backend syntax =="
python -m py_compile (Get-ChildItem -Path (Join-Path $backendRoot "app") -Recurse -Filter "*.py" | ForEach-Object { $_.FullName })

Write-Host "== Backend smoke =="
@'
from fastapi.testclient import TestClient
from app.main import app
client = TestClient(app)
for path in ["/health", "/health/dependencies", "/health/metrics", "/api/mobile/tracks", "/api/mobile/categories"]:
    response = client.get(path)
    print(path, response.status_code)
'@ | python -

Write-Host "== Android build =="
Push-Location $androidRoot
try {
    .\gradlew.bat compileDebugKotlin --no-daemon
} finally {
    Pop-Location
}

Write-Host "== Nginx config =="
$nginxConfig = Join-Path $backendRoot "deploy\nginx\music-hub.conf"
if (-not (Test-Path $nginxConfig)) {
    throw "Missing Nginx config: $nginxConfig"
}
Write-Host $nginxConfig

Write-Host "Release verification completed."
