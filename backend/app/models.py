from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TrendingTrack(Base):
    __tablename__ = "trending_tracks"
    __table_args__ = (UniqueConstraint("artist", "title", "source", name="uq_track_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    artist: Mapped[str] = mapped_column(String(300), index=True)
    source: Mapped[str] = mapped_column(String(80), default="lastfm")
    source_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    listeners: Mapped[int] = mapped_column(Integer, default=0)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class LocalTrack(Base):
    __tablename__ = "local_tracks"
    __table_args__ = (
        Index("ix_local_tracks_artist_album", "artist", "album"),
        Index("ix_local_tracks_title_artist", "title", "artist"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    artist: Mapped[str] = mapped_column(String(300), index=True)
    album: Mapped[str | None] = mapped_column(String(300), nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    file_path: Mapped[str] = mapped_column(String(1000), unique=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class TrackLyrics(Base):
    __tablename__ = "track_lyrics"
    __table_args__ = (UniqueConstraint("track_id", name="uq_track_lyrics_track"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    track_id: Mapped[int] = mapped_column(Integer, index=True)
    lines_json: Mapped[str] = mapped_column(Text, default="[]")
    raw_lrc: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(200), default="local_lrc")
    offset_ms: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class LicenseCode(Base):
    __tablename__ = "license_codes"
    __table_args__ = (
        Index("ix_license_status_seen", "status", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    code: Mapped[str] = mapped_column(String(6), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="unused", index=True)
    device_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    device_model: Mapped[str | None] = mapped_column(String(200), nullable=True)


class ReferralShare(Base):
    __tablename__ = "referral_shares"
    __table_args__ = (
        Index("ix_referral_shares_referrer_created", "referrer_code", "created_at"),
        UniqueConstraint("batch_token", name="uq_referral_shares_batch_token"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    referrer_code: Mapped[str] = mapped_column(String(6), index=True)
    referrer_device_id: Mapped[str] = mapped_column(String(120), index=True)
    batch_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    codes_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class MobileSearchRecord(Base):
    __tablename__ = "mobile_search_records"
    __table_args__ = (
        Index("ix_mobile_search_device_updated", "device_id", "updated_at"),
        Index("ix_mobile_search_license_updated", "license_code", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    device_id: Mapped[str] = mapped_column(String(120), index=True)
    license_code: Mapped[str | None] = mapped_column(String(6), nullable=True, index=True)
    query: Mapped[str] = mapped_column(String(300), index=True)
    track_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    artist: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    message: Mapped[str] = mapped_column(String(600), default="")
    file_path: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class AiMusicGeneration(Base):
    __tablename__ = "ai_music_generations"
    __table_args__ = (
        Index("ix_ai_music_device_updated", "device_id", "updated_at"),
        Index("ix_ai_music_license_updated", "license_code", "updated_at"),
        UniqueConstraint("job_id", name="uq_ai_music_job_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    job_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    device_id: Mapped[str] = mapped_column(String(120), index=True)
    license_code: Mapped[str | None] = mapped_column(String(6), nullable=True, index=True)
    source_track_id: Mapped[int] = mapped_column(Integer, index=True)
    generated_track_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    artist: Mapped[str] = mapped_column(String(300), default="AI")
    theme: Mapped[str] = mapped_column(String(600), default="")
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    message: Mapped[str] = mapped_column(String(600), default="")
    voice_path: Mapped[str] = mapped_column(String(1000), default="")
    result_path: Mapped[str] = mapped_column(String(1000), default="")
    analysis: Mapped[str] = mapped_column(Text, default="")
    caption: Mapped[str] = mapped_column(Text, default="")
    lyrics: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
