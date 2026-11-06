$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path ".\data\beets", ".\data\music", ".\data\inbox" | Out-Null
beet -c .\beets-config.yaml import .\data\inbox
