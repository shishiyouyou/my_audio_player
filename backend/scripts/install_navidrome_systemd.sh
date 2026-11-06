#!/usr/bin/env bash
set -euo pipefail

VERSION="${NAVIDROME_VERSION:-0.58.0}"
BASE_DIR="/home/music/navidrome"
MUSIC_DIR="/home/music/code/backend/data/music"
DATA_DIR="/home/music/code/backend/data/navidrome"
ARCHIVE="navidrome_${VERSION}_Linux_x86_64.tar.gz"
URL="https://github.com/navidrome/navidrome/releases/download/v${VERSION}/${ARCHIVE}"

mkdir -p "$BASE_DIR" "$MUSIC_DIR" "$DATA_DIR"
cd "$BASE_DIR"

if [ ! -x "$BASE_DIR/navidrome" ]; then
  curl -L --retry 3 --connect-timeout 20 -o "$ARCHIVE" "$URL"
  tar -xzf "$ARCHIVE"
  chmod +x "$BASE_DIR/navidrome"
fi

cat >/etc/systemd/system/navidrome.service <<SERVICE
[Unit]
Description=Navidrome Music Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BASE_DIR
ExecStart=$BASE_DIR/navidrome --datafolder "$DATA_DIR" --musicfolder "$MUSIC_DIR" --address 0.0.0.0 --port 4533 --scanSchedule 1h
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable --now navidrome.service
