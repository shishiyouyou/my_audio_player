from fastapi import APIRouter, Depends
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import LocalTrack
from app.schemas import ImportResult, LocalTrackOut
from app.services.importer import import_inbox

router = APIRouter(prefix="/api/library", tags=["本地曲库"])


@router.get("/tracks", response_model=list[LocalTrackOut], summary="查看本地音乐")
def list_tracks(limit: int = 100, db: Session = Depends(get_db)) -> list[LocalTrack]:
    statement = select(LocalTrack).order_by(desc(LocalTrack.imported_at)).limit(limit)
    return list(db.scalars(statement).all())


@router.post("/import", response_model=ImportResult, summary="导入 inbox 中的合法音频")
def import_tracks(db: Session = Depends(get_db)) -> ImportResult:
    imported, skipped = import_inbox(get_settings(), db)
    return ImportResult(imported=imported, skipped=skipped, message=f"导入 {imported} 首，跳过 {skipped} 首。")
