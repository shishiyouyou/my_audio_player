from datetime import datetime

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    status: str
    message: str


class TrendingTrackOut(BaseModel):
    id: int
    title: str
    artist: str
    source: str
    source_url: str | None
    listeners: int
    rank: int
    fetched_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LocalTrackOut(BaseModel):
    id: int
    title: str
    artist: str
    album: str | None
    duration_seconds: int
    file_path: str
    imported_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ImportResult(BaseModel):
    imported: int
    skipped: int
    message: str


class SyncResult(BaseModel):
    synced: int
    message: str
