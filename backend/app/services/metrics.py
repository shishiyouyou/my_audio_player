from __future__ import annotations

import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


_LOCK = threading.Lock()
_STARTED_AT = time.time()
_ACTIVE_REQUESTS = 0


@dataclass
class RouteMetric:
    count: int = 0
    errors: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    status_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))


_ROUTES: dict[str, RouteMetric] = defaultdict(RouteMetric)


def request_started() -> None:
    global _ACTIVE_REQUESTS
    with _LOCK:
        _ACTIVE_REQUESTS += 1


def request_finished(method: str, path: str, status_code: int, elapsed_ms: float) -> None:
    global _ACTIVE_REQUESTS
    route = f"{method.upper()} {_normalize_path(path)}"
    status_group = f"{int(status_code / 100)}xx" if status_code else "error"
    with _LOCK:
        _ACTIVE_REQUESTS = max(0, _ACTIVE_REQUESTS - 1)
        metric = _ROUTES[route]
        metric.count += 1
        if status_code >= 500 or status_code == 0:
            metric.errors += 1
        metric.total_ms += elapsed_ms
        metric.max_ms = max(metric.max_ms, elapsed_ms)
        metric.status_counts[status_group] += 1


def snapshot() -> dict[str, Any]:
    with _LOCK:
        routes = []
        total_count = 0
        total_errors = 0
        total_ms = 0.0
        for route, metric in sorted(_ROUTES.items(), key=lambda item: item[1].count, reverse=True):
            avg_ms = metric.total_ms / metric.count if metric.count else 0.0
            routes.append(
                {
                    "route": route,
                    "count": metric.count,
                    "errors": metric.errors,
                    "avgMs": round(avg_ms, 2),
                    "maxMs": round(metric.max_ms, 2),
                    "statusCounts": dict(metric.status_counts),
                }
            )
            total_count += metric.count
            total_errors += metric.errors
            total_ms += metric.total_ms
        return {
            "uptimeSeconds": int(time.time() - _STARTED_AT),
            "activeRequests": _ACTIVE_REQUESTS,
            "totalRequests": total_count,
            "totalErrors": total_errors,
            "avgMs": round(total_ms / total_count, 2) if total_count else 0.0,
            "routes": routes[:50],
        }


def _normalize_path(path: str) -> str:
    path = re.sub(r"/api/mobile/tracks/\d+/(stream|download)$", r"/api/mobile/tracks/{id}/\1", path)
    path = re.sub(r"/api/mobile/search/status/[A-Za-z0-9_-]+$", "/api/mobile/search/status/{job_id}", path)
    path = re.sub(r"/api/app/download/[A-Za-z0-9_.-]+$", "/api/app/download/{version_id}", path)
    path = re.sub(r"/admin/apk/[A-Za-z0-9_.-]+/(publish|delete)$", r"/admin/apk/{version_id}/\1", path)
    return path
