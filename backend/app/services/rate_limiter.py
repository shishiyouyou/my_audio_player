from __future__ import annotations

import time

from app.config import Settings
from app.services.task_queue import get_task_queue


def allow_request(settings: Settings, bucket: str, identity: str, limit: int, window_seconds: int = 60) -> bool:
    if limit <= 0:
        return True
    identity = (identity or "anonymous").strip()[:160]
    now = int(time.time())
    window = now // max(1, window_seconds)
    namespace = settings.task_queue_namespace.strip(":") or "music_hub"
    key = f"{namespace}:rate:{bucket}:{identity}:{window}"
    try:
        redis = get_task_queue(settings).redis
        count = redis.incr(key)
        if count == 1:
            redis.expire(key, window_seconds + 5)
        return int(count) <= limit
    except Exception:
        # Fail open: Redis issues should never block playback or normal app use.
        return True
