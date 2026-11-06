from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.config import Settings, get_settings


SESSION_COOKIE = "music_admin_session"
SESSION_TTL_SECONDS = 60 * 60 * 12


def _sign(value: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session(username: str, settings: Settings) -> str:
    payload = f"{username}:{int(time.time())}"
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    return f"{encoded}.{_sign(encoded, settings.session_secret)}"


def verify_session(token: str | None, settings: Settings) -> bool:
    if not token or "." not in token:
        return False
    encoded, signature = token.rsplit(".", 1)
    expected = _sign(encoded, settings.session_secret)
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        payload = base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
        username, issued_at = payload.rsplit(":", 1)
    except Exception:
        return False
    if username != settings.admin_username:
        return False
    return int(time.time()) - int(issued_at) <= SESSION_TTL_SECONDS


def require_admin(
    request: Request,
    session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    if verify_session(session, settings):
        return
    if request.url.path.startswith("/api/"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录后台")
    raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})


def login_response(username: str, settings: Settings) -> RedirectResponse:
    response = RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE,
        create_session(username, settings),
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,
    )
    return response


def logout_response() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE)
    return response
