from __future__ import annotations

import hashlib
import secrets

import httpx

from app.config import Settings


def _token(password: str, salt: str) -> str:
    return hashlib.md5(f"{password}{salt}".encode("utf-8")).hexdigest()


async def trigger_scan(settings: Settings) -> tuple[bool, str]:
    if not settings.navidrome_username or not settings.navidrome_password:
        return False, "未配置 Navidrome 用户名和密码，无法调用扫描接口。"

    salt = secrets.token_hex(8)
    params = {
        "u": settings.navidrome_username,
        "t": _token(settings.navidrome_password, salt),
        "s": salt,
        "v": "1.16.1",
        "c": "music-hub-admin",
        "f": "json",
    }
    url = f"{settings.navidrome_base_url.rstrip('/')}/rest/startScan.view"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        return False, f"无法连接 Navidrome：{exc}"

    status_value = payload.get("subsonic-response", {}).get("status")
    if status_value == "ok":
        return True, "已触发 Navidrome 扫描。"
    return False, f"Navidrome 返回失败：{payload}"
