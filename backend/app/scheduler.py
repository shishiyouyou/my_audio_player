from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.db import SessionLocal
from app.services.legal_downloader import download_configured_sources, load_config
from app.services.lastfm import LastFmService


scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


async def sync_trending_job() -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        await LastFmService(settings).sync_top_tracks(db)
    finally:
        db.close()


async def legal_download_job() -> None:
    settings = get_settings()
    await download_configured_sources(settings, trigger="scheduled")


def configure_legal_download_job() -> None:
    settings = get_settings()
    config = load_config(settings)
    job_id = "legal_audio_download"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if config["enabled"]:
        scheduler.add_job(
            legal_download_job,
            "cron",
            hour=config["hour"],
            minute=config["minute"],
            id=job_id,
            replace_existing=True,
        )


def start_scheduler() -> None:
    settings = get_settings()
    scheduler.add_job(
        sync_trending_job,
        "cron",
        hour=settings.sync_cron_hour,
        minute=settings.sync_cron_minute,
        id="sync_lastfm_trending",
        replace_existing=True,
    )
    configure_legal_download_job()
    scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown()
