from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import init_db
from app.routers import admin, ai_music, apk, health, library, mobile, trending
from app.scheduler import start_scheduler, stop_scheduler
from app.services import metrics


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    yield
    stop_scheduler()


settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    description="中文自建音乐库后台：同步热门榜单元数据，导入合法音乐文件，并提供移动端播放接口。",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def collect_request_metrics(request: Request, call_next):
    metrics.request_started()
    started = perf_counter()
    status_code = 0
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        elapsed_ms = (perf_counter() - started) * 1000
        metrics.request_finished(request.method, request.url.path, status_code, elapsed_ms)

app.include_router(health.router)
app.include_router(trending.router)
app.include_router(library.router)
app.include_router(mobile.router)
app.include_router(ai_music.router)
app.include_router(apk.router)
app.include_router(admin.router)
