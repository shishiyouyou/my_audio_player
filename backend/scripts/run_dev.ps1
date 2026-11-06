$ErrorActionPreference = "Stop"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "已创建 .env，请填写 LASTFM_API_KEY 后再同步榜单。"
}
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
