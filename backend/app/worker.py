from __future__ import annotations

import signal
import threading
import time
from typing import Any

from app.config import get_settings
from app.db import init_db
from app.services.task_queue import TERMINAL_STATUSES, get_task_queue


_STOP = threading.Event()


def main() -> None:
    settings = get_settings()
    init_db()
    queue = get_task_queue(settings)
    concurrency = max(1, min(int(settings.task_worker_concurrency or 1), 8))

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    workers = [
        threading.Thread(target=_worker_loop, args=(index + 1,), name=f"music-worker-{index + 1}", daemon=False)
        for index in range(concurrency)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


def _worker_loop(worker_id: int) -> None:
    settings = get_settings()
    queue = get_task_queue(settings)
    while not _STOP.is_set():
        try:
            job_id = queue.claim_job(timeout=5)
            if not job_id:
                continue
            job = queue.mark_running(job_id)
            if not job:
                continue
            _run_job(settings, job_id, job)
        except Exception as exc:
            print(f"[worker-{worker_id}] job loop error: {exc}")
            time.sleep(1)


def _run_job(settings, job_id: str, job: dict[str, Any]) -> None:
    queue = get_task_queue(settings)
    kind = str(job.get("kind") or "")
    try:
        if kind == "configured_download":
            from app.services.legal_downloader import run_configured_download_job

            run_configured_download_job(settings, job_id)
        elif kind == "lyrics_backfill":
            from app.services.lyrics import run_lyrics_backfill_job

            run_lyrics_backfill_job(settings, job_id)
        elif kind == "mobile_search":
            from app.routers.mobile import run_mobile_search_job

            run_mobile_search_job(job_id, dict(job.get("payload") or {}))
        elif kind == "ai_music_generation":
            from app.routers.ai_music import run_ai_music_generation_job

            run_ai_music_generation_job(job_id, dict(job.get("payload") or {}))
        else:
            queue.finish_job(job_id, "failed", f"Unknown job type: {kind}")
            return

        latest = queue.get_job(job_id, include_items=False) or {}
        if str(latest.get("status") or "") not in TERMINAL_STATUSES:
            queue.finish_job(job_id, "success", "\u4efb\u52a1\u5b8c\u6210")
    except Exception as exc:
        queue.finish_job(job_id, "failed", f"\u4efb\u52a1\u5f02\u5e38\uff1a{str(exc)[:300]}")


def _stop(signum, frame) -> None:
    _STOP.set()


if __name__ == "__main__":
    main()
