from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "\u81ea\u5efa\u97f3\u4e50\u5e93\u540e\u53f0"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    public_app_base_url: str = "https://music.6sy12.top/music"

    # Production should use PostgreSQL, for example:
    # postgresql+psycopg2://music_hub:password@127.0.0.1:5432/music_hub
    database_url: str = "sqlite:///./data/music_hub.db"

    redis_url: str = "redis://127.0.0.1:6379/0"
    task_queue_namespace: str = "music_hub"
    task_worker_concurrency: int = 2
    metadata_cache_ttl_seconds: int = 300
    search_cache_ttl_seconds: int = 60
    search_download_rate_limit_per_minute: int = 12
    ai_generate_rate_limit_per_minute: int = 3
    ai_generator_base_url: str = ""
    ai_generator_api_key: str = ""
    ai_generator_timeout_seconds: int = 3600
    ai_upload_dir: Path = Path("./data/ai_uploads")
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle_seconds: int = 1800

    admin_username: str = "admin"
    admin_password: str = "admin123456"
    session_secret: str = "please-change-this-secret"
    lastfm_api_key: str = ""
    lastfm_country: str = "China"
    sync_cron_hour: int = 3
    sync_cron_minute: int = 10
    inbox_dir: Path = Path("./data/inbox")
    music_dir: Path = Path("./data/music")
    apk_dir: Path = Path("./data/apk")
    apk_versions_file: Path = Path("./data/apk_versions.json")
    downloader_config_file: Path = Path("./data/download_sources.json")
    navidrome_base_url: str = "http://127.0.0.1:4533"
    navidrome_username: str = ""
    navidrome_password: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
