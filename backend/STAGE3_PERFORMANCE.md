# Stage 3 Performance Hardening

This stage keeps audio quality unchanged and does not increase bandwidth. It improves stability for 30-60 concurrent listeners by reducing API/database pressure and exposing runtime visibility.

## Included Changes

- Redis metadata cache for mobile track list, categories, and search suggestions.
- Cache invalidation after library import or missing-file repair.
- PostgreSQL connection pool settings.
- Lightweight rate limiting for mobile search-download requests only.
- Runtime request metrics at `/health/metrics`.
- Dependency check at `/health/dependencies`.
- Nginx media headers for byte-range playback and client-side cache reuse.
- Index helper script for existing databases.
- Lightweight smoke/performance script.

## Production Checklist

```bash
cd /home/music/code/backend
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python scripts/ensure_indexes.py
systemctl restart music-hub-backend.service
systemctl restart music-hub-worker.service
curl http://127.0.0.1:8000/health/dependencies
curl http://127.0.0.1:8000/health/metrics
./.venv/bin/python scripts/perf_smoke.py --base-url http://127.0.0.1:8000 --concurrency 30 --rounds 5
```

## Recommended Settings

```env
DATABASE_URL=postgresql+psycopg2://music_hub:CHANGE_ME@127.0.0.1:5432/music_hub
REDIS_URL=redis://127.0.0.1:6379/0
TASK_WORKER_CONCURRENCY=2
METADATA_CACHE_TTL_SECONDS=300
SEARCH_CACHE_TTL_SECONDS=60
SEARCH_DOWNLOAD_RATE_LIMIT_PER_MINUTE=12
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
DB_POOL_RECYCLE_SECONDS=1800
```
