from __future__ import annotations

import json
import re
import secrets
from html import escape as html_escape
from mimetypes import guess_type
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.config import get_settings
from app.db import SessionLocal
from app.models import LicenseCode, LocalTrack, MobileSearchRecord, ReferralShare, TrackLyrics
from app.services.cache import cached_json, get_library_version
from app.services.importer import import_inbox
from app.services.lyrics import ensure_track_lyrics, lyrics_payload
from app.services.musicdl_trending import PlaylistSong, download_song, find_existing_track
from app.services.rate_limiter import allow_request
from app.services.task_queue import TERMINAL_STATUSES, get_task_queue
from app.services.apk_versions import latest_published

router = APIRouter(prefix="/api/mobile", tags=["移动端"])

_MUSIC_INTERNAL_PREFIX = "/music-media/"
_SEARCH_KIND = "mobile_search"


class LicenseActivateIn(BaseModel):
    code: str
    deviceId: str
    appVersion: str = ""
    deviceModel: str = ""
    lat: float | None = None
    lng: float | None = None


class LicenseHeartbeatIn(BaseModel):
    code: str = ""
    deviceId: str
    appVersion: str = ""
    deviceModel: str = ""
    lat: float | None = None
    lng: float | None = None


class ReferralShareIn(BaseModel):
    code: str
    deviceId: str
    appVersion: str = ""
    deviceModel: str = ""
    lat: float | None = None
    lng: float | None = None


class SearchDownloadIn(BaseModel):
    query: str
    code: str = ""
    deviceId: str
    appVersion: str = ""
    deviceModel: str = ""
    lat: float | None = None
    lng: float | None = None


def _track_payload(track: LocalTrack) -> dict:
    return {
        "id": str(track.id),
        "title": track.title,
        "artist": track.artist,
        "album": track.album or "",
        "durationSeconds": track.duration_seconds,
        "streamUrl": f"/api/mobile/tracks/{track.id}/stream",
        "downloadUrl": f"/api/mobile/tracks/{track.id}/download",
    }


def _external_base_url(request: Request, settings) -> str:
    if settings.public_app_base_url:
        return settings.public_app_base_url.rstrip("/")
    forwarded_proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    forwarded_prefix = request.headers.get("x-forwarded-prefix", "").strip().rstrip("/")
    if forwarded_prefix:
        return f"{forwarded_proto}://{forwarded_host}{forwarded_prefix}"
    return str(request.base_url).rstrip("/")


def _new_referral_codes(db: Session, count: int = 5) -> list[str]:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    existing = set(db.scalars(select(LicenseCode.code)).all())
    codes: list[str] = []
    while len(codes) < count:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if code in existing:
            continue
        existing.add(code)
        codes.append(code)
        db.add(LicenseCode(code=code))
    return codes


def _referral_share_content(
    download_url: str,
    version_name: str,
    codes: list[str],
) -> tuple[str, str]:
    code_lines = "\n".join(codes)
    html_code_lines = "<br>".join(html_escape(code) for code in codes)
    text = (
        "畅听音乐 APP｜终身免费畅听，尽享无损音质\n"
        "喜欢听歌的朋友，推荐一款高品质音乐 APP：\n"
        "🎵 终身免费畅听\n"
        "🎧 全无损高品质音乐\n"
        "📱 支持手机安装，随时随地畅享好音乐\n"
        "🔑 安装后输入激活码即可使用\n"
        f"最新版本：v{version_name}\n"
        f"下载地址：{download_url}\n\n"
        "限量赠送 5 个一次性激活码，每个激活码限 1 台设备使用：\n"
        f"{code_lines}\n\n"
        "使用方法：\n"
        "下载安装 APP → 打开应用 → 输入激活码 → 开始畅听。\n"
        "名额有限，先到先得，喜欢无损音乐的朋友别错过！"
    )
    html = (
        "<div><strong>畅听音乐 APP｜终身免费畅听，尽享无损音质</strong><br>"
        "喜欢听歌的朋友，推荐一款高品质音乐 APP：<br>"
        "🎵 终身免费畅听<br>"
        "🎧 全无损高品质音乐<br>"
        "📱 支持手机安装，随时随地畅享好音乐<br>"
        "🔑 安装后输入激活码即可使用<br>"
        f"最新版本：v{html_escape(version_name)}<br>"
        f"下载地址：{html_escape(download_url)}<br><br>"
        "限量赠送 5 个一次性激活码，每个激活码限 1 台设备使用：<br>"
        f"{html_code_lines}<br><br>"
        "使用方法：<br>"
        "下载安装 APP → 打开应用 → 输入激活码 → 开始畅听。<br>"
        "名额有限，先到先得，喜欢无损音乐的朋友别错过！</div>"
    )
    return text, html


@router.get("/tracks/{track_id}/lyrics", summary="获取歌曲同步歌词")
def track_lyrics(
    track_id: int,
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()

    def build() -> dict:
        track = db.get(LocalTrack, track_id)
        if not track:
            raise HTTPException(status_code=404, detail="歌曲不存在")
        record = db.scalar(select(TrackLyrics).where(TrackLyrics.track_id == track_id))
        if not _lyrics_ready(record) and ensure_track_lyrics(settings, db, track, force=False):
            record = db.scalar(select(TrackLyrics).where(TrackLyrics.track_id == track_id))
        return lyrics_payload(record, track_id)

    return cached_json(settings, f"lyrics:{track_id}", settings.metadata_cache_ttl_seconds, build)


def _track_payloads(tracks: list[LocalTrack]) -> list[dict]:
    return [_track_payload(track) for track in tracks]


def _supports_accel(request: Request) -> bool:
    return request.headers.get("x-accel-redirect-enabled") == "1"


def _accel_response(path: Path, download: bool = False) -> Response | None:
    settings = get_settings()
    try:
        relative = path.resolve().relative_to(settings.music_dir.resolve()).as_posix()
    except Exception:
        return None
    internal_path = f"{_MUSIC_INTERNAL_PREFIX}{quote(relative)}"
    media_type = guess_type(path.name)[0] or "application/octet-stream"
    encoded_name = quote(path.name)
    headers = {
        "X-Accel-Redirect": internal_path,
        "Content-Disposition": f'{"attachment" if download else "inline"}; filename*=UTF-8\'\'{encoded_name}',
        "Cache-Control": "public, max-age=86400",
        "Accept-Ranges": "bytes",
        "X-Accel-Buffering": "no",
    }
    return Response(content=b"", media_type=media_type, headers=headers)


def _search_job_payload(job_id: str) -> dict:
    return dict(get_task_queue(get_settings()).get_job(job_id) or {})


def _set_search_job(job_id: str, **values: Any) -> None:
    queue = get_task_queue(get_settings())
    queue.update_job(job_id, **values)
    status = str(values.get("status") or "")
    if status in TERMINAL_STATUSES:
        queue.finish_job(job_id, status, str(values.get("message") or ""))


def _normalize_key(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "").casefold())


def _lyrics_ready(record: TrackLyrics | None) -> bool:
    if not record:
        return False
    try:
        lines = json.loads(record.lines_json or "[]")
    except json.JSONDecodeError:
        return False
    return isinstance(lines, list) and bool(lines)


def _parse_search_query(query: str) -> tuple[str, str]:
    value = re.sub(r"\s+", " ", query.strip())
    for splitter in (" - ", "-", "/", "\t"):
        if splitter in value:
            title, artist = [part.strip() for part in value.split(splitter, 1)]
            return title or value, artist or "Unknown"
    return value, "Unknown"


def _identity(code: str, device_id: str) -> str:
    return (code or "").strip().upper() or (device_id or "").strip() or "anonymous"


def _search_key(code: str, device_id: str, query: str) -> str:
    return f"{_identity(code, device_id)}:{_normalize_key(query)}"


def _find_local_track_by_query(db: Session, query: str) -> LocalTrack | None:
    title, artist = _parse_search_query(query)
    query_key = _normalize_key(query)
    title_key = _normalize_key(title)
    artist_key = _normalize_key(artist)
    best: tuple[int, LocalTrack] | None = None
    for track in db.scalars(select(LocalTrack).order_by(desc(LocalTrack.imported_at)).limit(3000)).all():
        db_title_key = _normalize_key(track.title)
        db_artist_key = _normalize_key(track.artist)
        db_key = _normalize_key(f"{track.title} - {track.artist}")
        score = 0
        if query_key and query_key == db_key:
            score = 100
        elif title_key and title_key == db_title_key and (artist == "Unknown" or artist_key == db_artist_key):
            score = 90
        elif title_key and title_key == db_title_key:
            score = 80
        elif title_key and (title_key in db_key or db_title_key in title_key):
            score = 60
        elif query_key and (query_key in db_key or db_key in query_key):
            score = 50
        if score and (best is None or score > best[0]):
            best = (score, track)
            if score >= 90:
                break
    return best[1] if best else None


def _save_search_record(
    db: Session,
    payload: SearchDownloadIn,
    query: str,
    status: str,
    message: str,
    track: LocalTrack | None = None,
) -> None:
    device_id = payload.deviceId.strip() or "unknown"
    code = payload.code.strip().upper() or None
    record = None
    if track:
        record = db.scalar(
            select(MobileSearchRecord)
            .where(MobileSearchRecord.device_id == device_id)
            .where(MobileSearchRecord.track_id == track.id)
        )
    if not record:
        record = db.scalar(
            select(MobileSearchRecord)
            .where(MobileSearchRecord.device_id == device_id)
            .where(MobileSearchRecord.query == query)
            .order_by(desc(MobileSearchRecord.created_at))
        )
    if not record:
        record = MobileSearchRecord(device_id=device_id, license_code=code, query=query)
        db.add(record)
    record.license_code = code or record.license_code
    record.track_id = track.id if track else record.track_id
    record.title = track.title if track else record.title
    record.artist = track.artist if track else record.artist
    record.status = status
    record.message = message
    record.file_path = track.file_path if track else record.file_path
    record.updated_at = datetime.utcnow()
    db.commit()


def _record_payload(db: Session, record: MobileSearchRecord) -> dict:
    track = db.get(LocalTrack, record.track_id) if record.track_id else None
    return {
        "id": record.id,
        "query": record.query,
        "status": record.status,
        "message": record.message,
        "track": _track_payload(track) if track else None,
        "createdAt": record.created_at.isoformat(timespec="seconds") if record.created_at else "",
        "updatedAt": record.updated_at.isoformat(timespec="seconds") if record.updated_at else "",
    }


def _touch_license(payload: SearchDownloadIn, db: Session) -> None:
    code = payload.code.strip().upper()
    if not code:
        return
    license_code = db.scalar(select(LicenseCode).where(LicenseCode.code == code))
    if license_code and license_code.status == "active" and (not license_code.device_id or license_code.device_id == payload.deviceId):
        _update_license_device(license_code, payload)
        db.commit()


def run_mobile_search_job(job_id: str, payload_data: dict[str, Any]) -> None:
    settings = get_settings()
    payload = SearchDownloadIn(**payload_data)
    query = payload.query.strip()
    db = SessionLocal()
    try:
        _set_search_job(job_id, status="downloading", percent=20, message="\u6b63\u5728\u641c\u7d22\u6b4c\u66f2")
        track = _find_local_track_by_query(db, query)
        if track:
            _save_search_record(db, payload, query, "success", "\u6b4c\u66f2\u5df2\u5b58\u5728", track)
            _set_search_job(job_id, status="success", percent=100, message="\u6b4c\u66f2\u5df2\u5b58\u5728", track=_track_payload(track))
            return

        title, artist = _parse_search_query(query)
        song = PlaylistSong(rank=1, song_id=f"mobile-{job_id}", title=title, artist=artist)
        existing_path = find_existing_track(settings, db, song)
        if existing_path:
            import_inbox(settings, db)
            track = _find_local_track_by_query(db, query)
            if track:
                _save_search_record(db, payload, query, "success", "\u6b4c\u66f2\u5df2\u5b58\u5728", track)
                _set_search_job(job_id, status="success", percent=100, message="\u6b4c\u66f2\u5df2\u5b58\u5728", track=_track_payload(track))
                return

        _set_search_job(job_id, status="downloading", percent=45, message="\u6b63\u5728\u4e0b\u8f7d")
        ok = download_song(settings, song)
        if not ok:
            _save_search_record(db, payload, query, "failed", "\u672a\u627e\u5230\u53ef\u7528\u8d44\u6e90")
            _set_search_job(job_id, status="failed", percent=100, message="\u672a\u627e\u5230\u53ef\u7528\u8d44\u6e90")
            return

        _set_search_job(job_id, status="importing", percent=75, message="\u6b63\u5728\u5bfc\u5165\u66f2\u5e93")
        import_inbox(settings, db)
        db.expire_all()
        track = _find_local_track_by_query(db, query) or _find_local_track_by_query(db, title)
        if not track:
            _save_search_record(db, payload, query, "failed", "\u4e0b\u8f7d\u5b8c\u6210\uff0c\u4f46\u672a\u80fd\u5bfc\u5165\u66f2\u5e93")
            _set_search_job(job_id, status="failed", percent=100, message="\u4e0b\u8f7d\u5b8c\u6210\uff0c\u4f46\u672a\u80fd\u5bfc\u5165\u66f2\u5e93")
            return

        _save_search_record(db, payload, query, "success", "\u4e0b\u8f7d\u5b8c\u6210", track)
        _set_search_job(job_id, status="success", percent=100, message="\u4e0b\u8f7d\u5b8c\u6210", track=_track_payload(track))
    except Exception as exc:
        message = str(exc)[:300] or "\u4e0b\u8f7d\u5931\u8d25"
        _save_search_record(db, payload, query, "failed", message)
        _set_search_job(job_id, status="failed", percent=100, message=message)
    finally:
        db.close()
        get_task_queue(settings).release_dedupe(job_id)


def _load_tracks(db: Session) -> list[LocalTrack]:
    return db.scalars(select(LocalTrack).order_by(desc(LocalTrack.imported_at)).limit(1000)).all()


def _license_payload(license_code: LicenseCode | None) -> dict:
    if not license_code:
        return {"active": False, "message": "序列号无效"}
    return {
        "active": license_code.status == "active",
        "code": license_code.code,
        "message": "已激活" if license_code.status == "active" else "序列号未激活",
    }


def _update_license_device(
    license_code: LicenseCode,
    payload: LicenseActivateIn | LicenseHeartbeatIn | ReferralShareIn,
) -> None:
    license_code.device_id = payload.deviceId.strip()
    license_code.app_version = payload.appVersion.strip() or license_code.app_version
    license_code.device_model = payload.deviceModel.strip() or license_code.device_model
    license_code.last_seen_at = datetime.utcnow()
    if payload.lat is not None and payload.lng is not None:
        license_code.last_lat = payload.lat
        license_code.last_lng = payload.lng


@router.post("/license/activate", summary="移动端激活序列号")
def activate_license(payload: LicenseActivateIn, db: Session = Depends(get_db)) -> dict:
    code = payload.code.strip().upper()
    license_code = db.scalar(select(LicenseCode).where(LicenseCode.code == code))
    if not license_code:
        raise HTTPException(status_code=404, detail="序列号不存在")
    if license_code.status == "active" and license_code.device_id and license_code.device_id != payload.deviceId:
        raise HTTPException(status_code=409, detail="序列号已被其他设备使用")
    license_code.status = "active"
    license_code.activated_at = license_code.activated_at or datetime.utcnow()
    _update_license_device(license_code, payload)
    db.commit()
    return _license_payload(license_code)


@router.post("/license/status", summary="移动端查询激活状态")
def license_status(payload: LicenseHeartbeatIn, db: Session = Depends(get_db)) -> dict:
    code = payload.code.strip().upper()
    if not code:
        return {"active": False, "message": "未激活"}
    license_code = db.scalar(select(LicenseCode).where(LicenseCode.code == code))
    if license_code and license_code.status == "active" and (not license_code.device_id or license_code.device_id == payload.deviceId):
        _update_license_device(license_code, payload)
        db.commit()
    return _license_payload(license_code)


@router.post("/license/heartbeat", summary="移动端在线心跳")
def license_heartbeat(payload: LicenseHeartbeatIn, db: Session = Depends(get_db)) -> dict:
    code = payload.code.strip().upper()
    license_code = db.scalar(select(LicenseCode).where(LicenseCode.code == code)) if code else None
    if license_code and license_code.status == "active" and (not license_code.device_id or license_code.device_id == payload.deviceId):
        _update_license_device(license_code, payload)
        db.commit()
    return {
        "ok": True,
        "active": bool(license_code and license_code.status == "active"),
        "onlineWindowSeconds": 180,
    }


@router.post("/referral/share", summary="激活用户生成分享内容")
def referral_share(
    payload: ReferralShareIn,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    code = payload.code.strip().upper()
    if not code or not payload.deviceId.strip():
        raise HTTPException(status_code=400, detail="缺少激活码或设备信息")

    settings = get_settings()
    if not allow_request(settings, "referral_share", f"{code}:{payload.deviceId}", 6, 60):
        raise HTTPException(status_code=429, detail="分享请求过于频繁，请稍后再试")

    license_code = db.scalar(select(LicenseCode).where(LicenseCode.code == code))
    if not license_code or license_code.status != "active":
        raise HTTPException(status_code=403, detail="只有已激活用户可以分享")
    if license_code.device_id and license_code.device_id != payload.deviceId:
        raise HTTPException(status_code=409, detail="当前激活码已绑定其他设备")

    _update_license_device(license_code, payload)
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=24)
    share = db.scalar(
        select(ReferralShare)
        .where(ReferralShare.referrer_code == code)
        .where(ReferralShare.referrer_device_id == payload.deviceId)
        .where(ReferralShare.created_at >= cutoff)
        .order_by(desc(ReferralShare.created_at))
    )

    if share:
        try:
            codes = [str(item).strip().upper() for item in json.loads(share.codes_json or "[]")]
        except (TypeError, json.JSONDecodeError):
            codes = []
    else:
        codes = _new_referral_codes(db, 5)
        share = ReferralShare(
            referrer_code=code,
            referrer_device_id=payload.deviceId,
            batch_token=secrets.token_urlsafe(18),
            codes_json=json.dumps(codes, ensure_ascii=False),
            created_at=now,
        )
        db.add(share)

    latest = latest_published(settings)
    if not latest:
        db.rollback()
        raise HTTPException(status_code=503, detail="当前没有可分享的线上 APK 版本")

    download_url = f"{_external_base_url(request, settings)}/api/app/download/{latest['id']}"
    share_text, share_html_text = _referral_share_content(
        download_url,
        latest.get("version_name", ""),
        codes[:5],
    )
    db.commit()
    return {
        "active": True,
        "codes": codes[:5],
        "versionName": latest.get("version_name", ""),
        "downloadUrl": download_url,
        "shareText": share_text,
        "shareHtmlText": share_html_text,
        "batchToken": share.batch_token,
        "expiresAt": (share.created_at + timedelta(hours=24)).isoformat(timespec="seconds"),
    }


@router.get("/tracks", summary="移动端获取歌曲列表")
def mobile_tracks(db: Session = Depends(get_db)) -> list[dict]:
    settings = get_settings()
    version = get_library_version(settings)
    return cached_json(
        settings,
        f"mobile:tracks:v{version}",
        settings.metadata_cache_ttl_seconds,
        lambda: _track_payloads(_load_tracks(db)),
    )


@router.get("/search/suggest", summary="移动端搜索联想")
def search_suggest(q: str = "", limit: int = 20, db: Session = Depends(get_db)) -> list[dict]:
    query = q.strip()
    if len(query) < 1:
        return []
    settings = get_settings()
    version = get_library_version(settings)
    safe_limit = max(1, min(limit, 30))

    def build() -> list[dict]:
        query_key = _normalize_key(query)
        matched: list[LocalTrack] = []
        for track in _load_tracks(db):
            haystack = _normalize_key(f"{track.title} - {track.artist} {track.album or ''}")
            if query_key and query_key in haystack:
                matched.append(track)
            if len(matched) >= safe_limit:
                break
        return _track_payloads(matched)

    return cached_json(
        settings,
        f"mobile:suggest:v{version}:{_normalize_key(query)}:{safe_limit}",
        settings.search_cache_ttl_seconds,
        build,
    )


@router.get("/search/list", summary="移动端获取我的搜索")
def search_list(deviceId: str = "", code: str = "", db: Session = Depends(get_db)) -> list[dict]:
    device_id = deviceId.strip()
    license_code = code.strip().upper()
    if not device_id and not license_code:
        return []
    query = select(MobileSearchRecord).order_by(desc(MobileSearchRecord.updated_at)).limit(300)
    if device_id:
        query = query.where(MobileSearchRecord.device_id == device_id)
    elif license_code:
        query = query.where(MobileSearchRecord.license_code == license_code)
    records = db.scalars(query).all()
    return [_record_payload(db, record) for record in records]


@router.post("/search/download", summary="移动端搜索并下载")
def search_download(payload: SearchDownloadIn, request: Request, db: Session = Depends(get_db)) -> dict:
    query = payload.query.strip()
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="\u8bf7\u8f93\u5165\u81f3\u5c11 2 \u4e2a\u5b57")
    settings = get_settings()
    identity = _identity(payload.code, payload.deviceId)
    client_ip = request.client.host if request.client else ""
    if not allow_request(
        settings,
        "mobile_search_download",
        identity or client_ip,
        settings.search_download_rate_limit_per_minute,
    ):
        raise HTTPException(status_code=429, detail="\u641c\u7d22\u4e0b\u8f7d\u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5")
    _touch_license(payload, db)
    track = _find_local_track_by_query(db, query)
    if track:
        _save_search_record(db, payload, query, "success", "\u6b4c\u66f2\u5df2\u5b58\u5728", track)
        return {
            "jobId": "",
            "status": "success",
            "percent": 100,
            "message": "\u6b4c\u66f2\u5df2\u5b58\u5728",
            "track": _track_payload(track),
        }

    key = _search_key(payload.code, payload.deviceId, query)
    queue = get_task_queue(settings)
    try:
        job_id, created = queue.enqueue_job(_SEARCH_KIND, payload.model_dump(), dedupe_key=key)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"\u540e\u53f0\u961f\u5217\u4e0d\u53ef\u7528\uff1a{str(exc)[:160]}") from exc
    if created:
        queue.update_job(
            job_id,
            query=query,
            message="\u5df2\u52a0\u5165\u641c\u7d22\u4e0b\u8f7d\u961f\u5217",
            track=None,
        )
    return _search_job_payload(job_id)


@router.get("/search/status/{job_id}", summary="移动端查询搜索下载状态")
def search_status(job_id: str) -> dict:
    try:
        job = _search_job_payload(job_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"\u540e\u53f0\u961f\u5217\u4e0d\u53ef\u7528\uff1a{str(exc)[:160]}") from exc
    if not job:
        raise HTTPException(status_code=404, detail="\u4efb\u52a1\u4e0d\u5b58\u5728")
    return job


@router.get("/categories", summary="移动端获取自动分类")
def mobile_categories(db: Session = Depends(get_db)) -> list[dict]:
    settings = get_settings()
    version = get_library_version(settings)
    return cached_json(
        settings,
        f"mobile:categories:v{version}",
        settings.metadata_cache_ttl_seconds,
        lambda: _build_categories(db),
    )


def _build_categories(db: Session) -> list[dict]:
    tracks = _load_tracks(db)
    categories: list[dict] = [
        {
            "id": "all",
            "name": "全部歌曲",
            "count": len(tracks),
            "trackIds": [str(track.id) for track in tracks],
        },
        {
            "id": "recent",
            "name": "最近上传",
            "count": min(len(tracks), 50),
            "trackIds": [str(track.id) for track in tracks[:50]],
        },
    ]

    def add_group(prefix: str, name_prefix: str, values: dict[str, list[str]], limit: int) -> None:
        for name, ids in sorted(values.items(), key=lambda item: (-len(item[1]), item[0]))[:limit]:
            if not name.strip():
                continue
            categories.append(
                {
                    "id": f"{prefix}:{name}",
                    "name": f"{name_prefix}{name}",
                    "count": len(ids),
                    "trackIds": ids,
                }
            )

    artists: dict[str, list[str]] = {}
    albums: dict[str, list[str]] = {}
    durations: dict[str, list[str]] = {"短歌": [], "常规": [], "长歌": []}

    for track in tracks:
        track_id = str(track.id)
        artists.setdefault(track.artist or "未知歌手", []).append(track_id)
        albums.setdefault(track.album or "未知专辑", []).append(track_id)
        if track.duration_seconds and track.duration_seconds < 180:
            durations["短歌"].append(track_id)
        elif track.duration_seconds and track.duration_seconds > 300:
            durations["长歌"].append(track_id)
        else:
            durations["常规"].append(track_id)

    add_group("artist", "歌手：", artists, 12)
    add_group("album", "专辑：", albums, 12)
    add_group("duration", "时长：", durations, 3)
    return [category for category in categories if category["count"] > 0]


@router.api_route("/tracks/{track_id}/stream", methods=["GET", "HEAD"], summary="移动端播放音频", response_model=None)
def stream_track(track_id: int, request: Request, db: Session = Depends(get_db)) -> FileResponse | Response:
    track = db.get(LocalTrack, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="歌曲不存在")
    path = Path(track.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="音频文件不存在")
    if _supports_accel(request):
        accel = _accel_response(path, download=False)
        if accel is not None:
            return accel
    return FileResponse(
        path,
        media_type=guess_type(path.name)[0] or "audio/mpeg",
        filename=path.name,
        headers={"Cache-Control": "public, max-age=86400", "Accept-Ranges": "bytes"},
    )


@router.api_route("/tracks/{track_id}/download", methods=["GET", "HEAD"], summary="移动端下载音频", response_model=None)
def download_track(track_id: int, request: Request, db: Session = Depends(get_db)) -> FileResponse | Response:
    track = db.get(LocalTrack, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="歌曲不存在")
    path = Path(track.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="音频文件不存在")
    if _supports_accel(request):
        accel = _accel_response(path, download=True)
        if accel is not None:
            return accel
    return FileResponse(
        path,
        media_type=guess_type(path.name)[0] or "application/octet-stream",
        filename=path.name,
        headers={"Cache-Control": "public, max-age=86400", "Accept-Ranges": "bytes"},
    )
