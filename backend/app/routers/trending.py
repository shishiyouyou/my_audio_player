from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import TrendingTrack
from app.schemas import SyncResult, TrendingTrackOut
from app.services.lastfm import LastFmService
from app.services.musicdl_trending import preview_playlist

router = APIRouter(prefix="/api/trending", tags=["热门榜单"])


@router.get("", response_model=list[TrendingTrackOut], summary="查看热门音乐榜单")
def list_trending(limit: int = 50, db: Session = Depends(get_db)) -> list[TrendingTrack]:
    statement = select(TrendingTrack).order_by(desc(TrendingTrack.fetched_at), TrendingTrack.rank).limit(limit)
    return list(db.scalars(statement).all())


@router.post("/sync", response_model=SyncResult, summary="立即同步热门音乐榜单")
async def sync_trending(limit: int = 50, db: Session = Depends(get_db)) -> SyncResult:
    synced = await LastFmService(get_settings()).sync_top_tracks(db, limit=limit)
    if synced == 0:
        return SyncResult(synced=0, message="未同步到数据，请检查 Last.fm API Key 是否已配置。")
    return SyncResult(synced=synced, message=f"已同步 {synced} 首热门音乐元数据。")


@router.get("/netease/preview", summary="预览网易云榜单待下载歌曲")
def preview_netease_playlist(
    playlist: str = "hot",
    limit: int = 20,
    skip_existing: bool = True,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        preview = preview_playlist(
            settings,
            db,
            playlist,
            limit=max(1, min(int(limit or 20), 100)),
            skip_existing=skip_existing,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"读取榜单失败：{str(exc)[:180]}") from exc
    return {
        "playlist_name": preview.playlist_name,
        "playlist_id": preview.playlist_id,
        "total": preview.total,
        "skipped_existing": preview.skipped_existing,
        "songs": [song.__dict__ for song in preview.songs],
    }
