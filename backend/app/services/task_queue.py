from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from functools import lru_cache
from typing import Any

from redis import Redis

from app.config import Settings


TERMINAL_STATUSES = {"success", "failed", "cancelled"}


def utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def safe_dedupe_key(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())
    return cleaned[:180] or uuid.uuid4().hex


class TaskQueue:
    def __init__(self, redis_url: str, namespace: str):
        self.redis = Redis.from_url(redis_url, decode_responses=True, health_check_interval=30)
        self.namespace = namespace.strip(":") or "music_hub"

    @property
    def queue_key(self) -> str:
        return f"{self.namespace}:queue"

    def job_key(self, job_id: str) -> str:
        return f"{self.namespace}:job:{job_id}"

    def items_key(self, job_id: str) -> str:
        return f"{self.namespace}:job:{job_id}:items"

    def latest_key(self, kind: str) -> str:
        return f"{self.namespace}:latest:{kind}"

    def active_key(self, kind: str) -> str:
        return f"{self.namespace}:active:{kind}"

    def dedupe_key(self, kind: str, key: str) -> str:
        return f"{self.namespace}:dedupe:{kind}:{safe_dedupe_key(key)}"

    def enqueue_job(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        dedupe_key: str | None = None,
    ) -> tuple[str, bool]:
        payload = dict(payload or {})
        redis_dedupe_key = self.dedupe_key(kind, dedupe_key) if dedupe_key else ""
        job_id = uuid.uuid4().hex[:12]
        if redis_dedupe_key:
            existing = self.redis.get(redis_dedupe_key)
            if existing:
                return existing, False
            if not self.redis.set(redis_dedupe_key, job_id, nx=True, ex=24 * 60 * 60):
                return str(self.redis.get(redis_dedupe_key) or job_id), False

        now = utc_now()
        mapping = {
            "jobId": job_id,
            "kind": kind,
            "status": "queued",
            "percent": "0",
            "total": "0",
            "done": "0",
            "success": "0",
            "skipped": "0",
            "failed": "0",
            "current": "",
            "message": "\u5df2\u52a0\u5165\u540e\u53f0\u961f\u5217",
            "payload": json.dumps(payload, ensure_ascii=False),
            "result": "{}",
            "track": "null",
            "dedupeKey": redis_dedupe_key,
            "createdAt": now,
            "updatedAt": now,
            "startedAt": "",
            "finishedAt": "",
        }

        pipe = self.redis.pipeline()
        pipe.hset(self.job_key(job_id), mapping=mapping)
        pipe.rpush(self.queue_key, job_id)
        pipe.set(self.latest_key(kind), job_id)
        pipe.execute()
        return job_id, True

    def claim_job(self, timeout: int = 5) -> str | None:
        result = self.redis.blpop(self.queue_key, timeout=timeout)
        if not result:
            return None
        return str(result[1])

    def mark_running(self, job_id: str) -> dict[str, Any] | None:
        job = self.get_job(job_id, include_items=False)
        if not job:
            return None
        now = utc_now()
        self.redis.hset(
            self.job_key(job_id),
            mapping={
                "status": "running",
                "startedAt": job.get("startedAt") or now,
                "updatedAt": now,
            },
        )
        self.redis.set(self.active_key(str(job.get("kind") or "")), job_id)
        return self.get_job(job_id, include_items=False)

    def finish_job(self, job_id: str, status: str, message: str) -> None:
        job = self.get_job(job_id, include_items=False)
        kind = str(job.get("kind") or "") if job else ""
        current_percent = _to_int(job.get("percent") if job else 0)
        final_percent = 100 if status == "success" else max(0, min(current_percent, 95))
        now = utc_now()
        self.redis.hset(
            self.job_key(job_id),
            mapping={
                "status": status,
                "percent": str(final_percent),
                "current": "",
                "message": message,
                "finishedAt": now,
                "updatedAt": now,
            },
        )
        if kind:
            self.clear_active_job(kind, job_id)
        self.release_dedupe(job_id)

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        mapping: dict[str, str] = {"updatedAt": utc_now()}
        for key, value in fields.items():
            if key in {"payload", "result", "track"}:
                mapping[key] = json.dumps(value, ensure_ascii=False)
            else:
                mapping[key] = "" if value is None else str(value)
        self.redis.hset(self.job_key(job_id), mapping=mapping)

    def append_item(self, job_id: str, item: dict[str, Any], *, limit: int = 500) -> None:
        self.redis.rpush(self.items_key(job_id), json.dumps(item, ensure_ascii=False))
        self.redis.ltrim(self.items_key(job_id), -limit, -1)

    def add_progress_item(self, job_id: str, name: str, status: str, message: str, path: str = "") -> None:
        item = {
            "time": utc_now(),
            "name": name,
            "status": status,
            "message": message,
            "path": path,
        }
        pipe = self.redis.pipeline()
        pipe.rpush(self.items_key(job_id), json.dumps(item, ensure_ascii=False))
        pipe.ltrim(self.items_key(job_id), -500, -1)
        pipe.hincrby(self.job_key(job_id), "done", 1)
        if status == "success":
            pipe.hincrby(self.job_key(job_id), "success", 1)
        elif status == "skipped":
            pipe.hincrby(self.job_key(job_id), "skipped", 1)
        else:
            pipe.hincrby(self.job_key(job_id), "failed", 1)
        pipe.hset(self.job_key(job_id), mapping={"message": message, "updatedAt": utc_now()})
        pipe.execute()
        self._refresh_percent(job_id)

    def increment_total(self, job_id: str, delta: int) -> None:
        if delta <= 0:
            return
        self.redis.hincrby(self.job_key(job_id), "total", int(delta))
        self._refresh_percent(job_id)

    def get_active_job_id(self, kind: str) -> str:
        return str(self.redis.get(self.active_key(kind)) or "")

    def clear_active_job(self, kind: str, job_id: str) -> None:
        if self.get_active_job_id(kind) == job_id:
            self.redis.delete(self.active_key(kind))

    def queued_job_ids(self, kind: str | None = None) -> list[str]:
        ids = [str(item) for item in self.redis.lrange(self.queue_key, 0, -1)]
        if not kind:
            return ids
        matched: list[str] = []
        for job_id in ids:
            job_kind = self.redis.hget(self.job_key(job_id), "kind")
            if job_kind == kind:
                matched.append(job_id)
        return matched

    def queued_ahead_count(self, kind: str, job_id: str) -> int:
        count = 0
        for queued_id in self.queued_job_ids(kind):
            if queued_id == job_id:
                return count
            count += 1
        return count

    def get_latest_job_id(self, kind: str) -> str:
        return str(self.redis.get(self.latest_key(kind)) or "")

    def get_status_for_kind(self, kind: str) -> dict[str, Any]:
        job_id = self.get_active_job_id(kind) or self.get_latest_job_id(kind)
        if not job_id:
            return empty_status()
        return self.get_job(job_id) or empty_status()

    def get_job(self, job_id: str, *, include_items: bool = True) -> dict[str, Any] | None:
        raw = self.redis.hgetall(self.job_key(job_id))
        if not raw:
            return None

        payload = _loads(raw.get("payload"), {})
        status = raw.get("status", "unknown")
        job: dict[str, Any] = {
            "jobId": raw.get("jobId", job_id),
            "kind": raw.get("kind", ""),
            "status": status,
            "running": status not in TERMINAL_STATUSES and status not in {"queued", "idle", "unknown"},
            "queued": status == "queued",
            "query": raw.get("query", "") or (payload.get("query", "") if isinstance(payload, dict) else ""),
            "current": raw.get("current", ""),
            "message": raw.get("message", ""),
            "createdAt": raw.get("createdAt", ""),
            "updatedAt": raw.get("updatedAt", ""),
            "startedAt": raw.get("startedAt", ""),
            "finishedAt": raw.get("finishedAt", ""),
            "payload": payload,
            "result": _loads(raw.get("result"), {}),
            "track": _loads(raw.get("track"), None),
        }
        for key in ("percent", "total", "done", "success", "skipped", "failed"):
            job[key] = _to_int(raw.get(key))
        if include_items:
            job["items"] = self._items(job_id)
        return job

    def release_dedupe(self, job_id: str) -> None:
        raw = self.redis.hgetall(self.job_key(job_id))
        key = raw.get("dedupeKey") if raw else ""
        if key and self.redis.get(key) == job_id:
            self.redis.delete(key)

    def _items(self, job_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for raw in self.redis.lrange(self.items_key(job_id), -500, -1):
            loaded = _loads(raw, None)
            if isinstance(loaded, dict):
                items.append(loaded)
        return items

    def _refresh_percent(self, job_id: str) -> None:
        raw = self.redis.hmget(self.job_key(job_id), ["total", "done"])
        total = max(_to_int(raw[0] if raw else 0), 1)
        done = _to_int(raw[1] if raw and len(raw) > 1 else 0)
        percent = min(100, int(done * 100 / total))
        self.update_job(job_id, percent=percent)


def empty_status() -> dict[str, Any]:
    return {
        "jobId": "",
        "kind": "",
        "status": "idle",
        "running": False,
        "queued": False,
        "percent": 0,
        "total": 0,
        "done": 0,
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "current": "",
        "message": "",
        "items": [],
    }


@lru_cache
def _cached_queue(redis_url: str, namespace: str) -> TaskQueue:
    return TaskQueue(redis_url, namespace)


def get_task_queue(settings: Settings) -> TaskQueue:
    return _cached_queue(settings.redis_url, settings.task_queue_namespace)


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0
