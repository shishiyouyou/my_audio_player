from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import TrendingTrack


class LastFmService:
    api_url = "https://ws.audioscrobbler.com/2.0/"

    def __init__(self, settings: Settings):
        self.settings = settings

    async def fetch_top_tracks(self, limit: int = 50) -> list[dict]:
        if not self.settings.lastfm_api_key or self.settings.lastfm_api_key.startswith("请填写"):
            return []

        params = {
            "method": "geo.gettoptracks",
            "country": self.settings.lastfm_country,
            "api_key": self.settings.lastfm_api_key,
            "format": "json",
            "limit": limit,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(self.api_url, params=params)
            response.raise_for_status()
            payload = response.json()

        tracks = payload.get("tracks", {}).get("track", [])
        result: list[dict] = []
        for index, item in enumerate(tracks, start=1):
            artist = item.get("artist", {})
            result.append(
                {
                    "title": item.get("name", "").strip(),
                    "artist": artist.get("name", "").strip(),
                    "source_url": item.get("url"),
                    "listeners": int(item.get("listeners") or 0),
                    "rank": index,
                }
            )
        return [track for track in result if track["title"] and track["artist"]]

    async def sync_top_tracks(self, db: Session, limit: int = 50) -> int:
        tracks = await self.fetch_top_tracks(limit=limit)
        now = datetime.utcnow()
        synced = 0

        for track in tracks:
            existing = db.scalar(
                select(TrendingTrack).where(
                    TrendingTrack.artist == track["artist"],
                    TrendingTrack.title == track["title"],
                    TrendingTrack.source == "lastfm",
                )
            )
            if existing:
                existing.source_url = track["source_url"]
                existing.listeners = track["listeners"]
                existing.rank = track["rank"]
                existing.fetched_at = now
            else:
                db.add(
                    TrendingTrack(
                        title=track["title"],
                        artist=track["artist"],
                        source="lastfm",
                        source_url=track["source_url"],
                        listeners=track["listeners"],
                        rank=track["rank"],
                        fetched_at=now,
                    )
                )
            synced += 1

        db.commit()
        return synced
