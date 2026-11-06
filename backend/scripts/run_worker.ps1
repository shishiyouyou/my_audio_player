$ErrorActionPreference = "Stop"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "已创建 .env，请确认 REDIS_URL / DATABASE_URL 后再启动 worker。"
}
python -m app.worker
