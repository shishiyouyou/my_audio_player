from fastapi import APIRouter
from sqlalchemy import text

from app.config import get_settings
from app.db import engine
from app.schemas import HealthResponse
from app.services.cache import get_library_version
from app.services import metrics
from app.services.task_queue import get_task_queue

router = APIRouter(tags=["\u7cfb\u7edf"])


@router.get("/health", response_model=HealthResponse, summary="\u5065\u5eb7\u68c0\u67e5")
def health() -> HealthResponse:
    return HealthResponse(status="ok", message="\u81ea\u5efa\u97f3\u4e50\u5e93\u540e\u53f0\u8fd0\u884c\u6b63\u5e38")


@router.get("/health/dependencies", summary="\u68c0\u67e5\u6570\u636e\u5e93\u548c Redis")
def dependency_health() -> dict:
    result = {"status": "ok", "database": "ok", "redis": "ok"}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        result["status"] = "degraded"
        result["database"] = str(exc)[:200]

    try:
        get_task_queue(get_settings()).redis.ping()
    except Exception as exc:
        result["status"] = "degraded"
        result["redis"] = str(exc)[:200]
    return result


@router.get("/health/metrics", summary="\u8fd0\u884c\u6307\u6807")
def runtime_metrics() -> dict:
    settings = get_settings()
    data = metrics.snapshot()
    data["libraryCacheVersion"] = get_library_version(settings)
    try:
        queue = get_task_queue(settings)
        data["queueLength"] = int(queue.redis.llen(queue.queue_key))
    except Exception as exc:
        data["queueLength"] = None
        data["queueError"] = str(exc)[:200]
    return data
