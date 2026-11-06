from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass

import httpx


@dataclass
class Result:
    path: str
    status: int
    ms: float
    error: str = ""


async def main() -> None:
    parser = argparse.ArgumentParser(description="Lightweight backend smoke/performance check.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Backend base URL.")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    paths = ["/health", "/health/dependencies", "/api/mobile/tracks", "/api/mobile/categories"]
    track_id = await _first_track_id(base_url)
    if track_id:
        paths.append(f"/api/mobile/tracks/{track_id}/stream")

    tasks = []
    sem = asyncio.Semaphore(max(1, args.concurrency))
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for _ in range(max(1, args.rounds)):
            for path in paths:
                for _ in range(max(1, args.concurrency)):
                    tasks.append(_request(client, sem, base_url, path, head=path.endswith("/stream")))
        results = await asyncio.gather(*tasks)

    grouped: dict[str, list[Result]] = {}
    for result in results:
        grouped.setdefault(result.path, []).append(result)
    for path, items in grouped.items():
        latencies = [item.ms for item in items if not item.error]
        errors = [item for item in items if item.error or item.status >= 500]
        p95 = statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else (max(latencies) if latencies else 0)
        avg = statistics.fmean(latencies) if latencies else 0
        print(f"{path}: count={len(items)} errors={len(errors)} avg_ms={avg:.1f} p95_ms={p95:.1f}")


async def _first_track_id(base_url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{base_url}/api/mobile/tracks")
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list) and data:
                return str(data[0].get("id") or "")
    except Exception:
        return ""
    return ""


async def _request(client: httpx.AsyncClient, sem: asyncio.Semaphore, base_url: str, path: str, *, head: bool) -> Result:
    async with sem:
        started = time.perf_counter()
        try:
            if head:
                response = await client.head(f"{base_url}{path}")
            else:
                response = await client.get(f"{base_url}{path}")
            return Result(path=path, status=response.status_code, ms=(time.perf_counter() - started) * 1000)
        except Exception as exc:
            return Result(path=path, status=0, ms=(time.perf_counter() - started) * 1000, error=str(exc))


if __name__ == "__main__":
    asyncio.run(main())
