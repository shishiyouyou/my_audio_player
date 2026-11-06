from __future__ import annotations

import json
from typing import Any, Callable, TypeVar

from app.config import Settings
from app.services.task_queue import get_task_queue


T = TypeVar("T")
LIBRARY_VERSION_KEY = "cache:library:version"


def get_library_version(settings: Settings) -> int:
    try:
        raw = get_task_queue(settings).redis.get(_key(settings, LIBRARY_VERSION_KEY))
        return int(raw or 1)
    except Exception:
        return 0


def bump_library_version(settings: Settings) -> None:
    try:
        get_task_queue(settings).redis.incr(_key(settings, LIBRARY_VERSION_KEY))
    except Exception:
        return


def cached_json(settings: Settings, key: str, ttl_seconds: int, builder: Callable[[], T]) -> T:
    if ttl_seconds <= 0:
        return builder()
    redis_key = _key(settings, f"cache:{key}")
    try:
        redis = get_task_queue(settings).redis
        raw = redis.get(redis_key)
        if raw:
            return json.loads(raw)
        value = builder()
        redis.set(redis_key, json.dumps(value, ensure_ascii=False), ex=ttl_seconds)
        return value
    except Exception:
        return builder()


def delete_cached_json(settings: Settings, key: str) -> None:
    try:
        get_task_queue(settings).redis.delete(_key(settings, f"cache:{key}"))
    except Exception:
        return


def _key(settings: Settings, suffix: str) -> str:
    namespace = settings.task_queue_namespace.strip(":") or "music_hub"
    return f"{namespace}:{suffix}"
