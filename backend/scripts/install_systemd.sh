#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/music/code/backend"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$APP_DIR"
mkdir -p data/inbox data/music data/navidrome

if [ ! -f .env ]; then
  cp .env.example .env
fi

"$PYTHON_BIN" -m venv .venv
./.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --upgrade pip
./.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt

cat >/etc/systemd/system/music-hub-backend.service <<SERVICE
[Unit]
Description=Music Hub Backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

cat >/etc/systemd/system/music-hub-worker.service <<SERVICE
[Unit]
Description=Music Hub Worker
After=network-online.target music-hub-backend.service
Wants=network-online.target music-hub-backend.service

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python -m app.worker
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable --now music-hub-backend.service
systemctl enable --now music-hub-worker.service
