from __future__ import annotations

import base64
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, get_db
from app.models import AiMusicGeneration, LicenseCode, LocalTrack, TrackLyrics
from app.services.importer import import_audio_file
from app.services.lyrics import ensure_track_lyrics, save_ai_generated_lyrics
from app.services.rate_limiter import allow_request
from app.services.task_queue import TERMINAL_STATUSES, get_task_queue

router = APIRouter(prefix="/api/mobile/ai", tags=["AI music"])

_AI_KIND = "ai_music_generation"
_AI_EXECUTOR_LOCK_TTL_SECONDS = 7200
_AI_GENERATOR_HEALTH_GRACE_SECONDS = 180
_AI_GENERATOR_HEALTH_POLL_SECONDS = 10
_VOICE_EXTENSIONS = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".flac"}
_AI_OPTIONS = {
    "voiceType": {"auto": "系统推荐", "male": "男声", "female": "女声"},
    "electronicLevel": {"auto": "系统推荐", "more": "电音多一点", "less": "电音少一点"},
    "mood": {"auto": "系统推荐", "warm": "温暖", "sad": "伤感", "energetic": "有力量", "quiet": "安静"},
    "tempo": {"auto": "系统推荐", "slow": "慢速", "medium": "中速", "fast": "快速"},
    "durationLevel": {"auto": "系统推荐", "short": "短一点", "standard": "标准", "long": "长一点"},
}


def _clean_option(name: str, value: str) -> str:
    options = _AI_OPTIONS.get(name, {})
    normalized = (value or "auto").strip()
    return normalized if normalized in options else "auto"


_CREATION_MODES = {"original", "melody_rewrite", "music3_original"}


def _clean_creation_mode(value: str) -> str:
    normalized = (value or "original").strip().lower()
    return normalized if normalized in _CREATION_MODES else "original"


def _option_summary(payload_data: dict[str, Any]) -> str:
    pairs = [
        ("声音", "voiceType"),
        ("电音", "electronicLevel"),
        ("情绪", "mood"),
        ("节奏", "tempo"),
        ("时长", "durationLevel"),
    ]
    return "；".join(
        f"{label}:{_AI_OPTIONS[key].get(str(payload_data.get(key) or 'auto'), '系统推荐')}"
        for label, key in pairs
    )


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


def _identity(code: str, device_id: str) -> str:
    return (code or "").strip().upper() or (device_id or "").strip() or "anonymous"


def _clean_author(value: str) -> str:
    cleaned = re.sub(r"[\r\n\t<>:\"/\\|?*]+", " ", str(value or "")).strip()
    return cleaned[:80] or "AI 创作"


def _require_active_license(db: Session, code: str, device_id: str) -> None:
    normalized = code.strip().upper()
    if not normalized or not device_id.strip():
        raise HTTPException(status_code=403, detail="AI 创作需要先激活账号")
    license_code = db.scalar(select(LicenseCode).where(LicenseCode.code == normalized))
    if not license_code or license_code.status != "active":
        raise HTTPException(status_code=403, detail="AI 创作需要先激活账号")
    if license_code.device_id and license_code.device_id != device_id:
        raise HTTPException(status_code=409, detail="当前激活码已绑定其它设备")


def _generation_payload(db: Session, record: AiMusicGeneration) -> dict:
    track = db.get(LocalTrack, record.generated_track_id) if record.generated_track_id else None
    source = db.get(LocalTrack, record.source_track_id) if record.source_track_id else None
    return {
        "id": record.id,
        "jobId": record.job_id,
        "status": record.status,
        "message": record.message,
        "title": record.title,
        "artist": record.artist,
        "theme": record.theme,
        "analysis": record.analysis,
        "caption": record.caption,
        "lyrics": record.lyrics,
        "sourceTrack": _track_payload(source) if source else None,
        "track": _track_payload(track) if track else None,
        "createdAt": record.created_at.isoformat(timespec="seconds") if record.created_at else "",
        "updatedAt": record.updated_at.isoformat(timespec="seconds") if record.updated_at else "",
    }


def _queue_payload(job_id: str) -> dict:
    settings = get_settings()
    queue = get_task_queue(settings)
    return _with_ai_queue_info(queue, dict(queue.get_job(job_id) or {}), job_id)


@router.post("/generate", summary="提交 AI 创作任务")
async def create_ai_generation(
    request: Request,
    trackId: int = Form(0),
    author: str = Form(""),
    theme: str = Form(""),
    creationMode: str = Form("original"),
    voiceType: str = Form("auto"),
    electronicLevel: str = Form("auto"),
    mood: str = Form("auto"),
    tempo: str = Form("auto"),
    durationLevel: str = Form("auto"),
    code: str = Form(""),
    deviceId: str = Form(""),
    appVersion: str = Form(""),
    deviceModel: str = Form(""),
    voice: UploadFile | None = File(None),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    _require_active_license(db, code, deviceId)
    identity = _identity(code, deviceId)
    client_ip = request.client.host if request.client else ""
    if not allow_request(settings, "ai_music_generate", identity or client_ip, settings.ai_generate_rate_limit_per_minute):
        raise HTTPException(status_code=429, detail="AI 创作请求过于频繁，请稍后再试")

    source = db.get(LocalTrack, trackId) if trackId > 0 else None
    if trackId > 0:
        if not source:
            raise HTTPException(status_code=404, detail="源歌曲不存在")
        source_path = Path(source.file_path)
        if not source_path.exists():
            raise HTTPException(status_code=404, detail="源歌曲文件不存在")

    creation_mode = _clean_creation_mode(creationMode)
    _require_generator_available_for_mode(settings, creation_mode)

    voice_path = ""
    if voice and voice.filename:
        suffix = Path(voice.filename).suffix.lower() or ".m4a"
        if suffix not in _VOICE_EXTENSIONS:
            raise HTTPException(status_code=400, detail="录音格式不支持")
        settings.ai_upload_dir.mkdir(parents=True, exist_ok=True)
        tmp_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{deviceId[:20]}{suffix}"
        destination = settings.ai_upload_dir / tmp_name
        with destination.open("wb") as target:
            while chunk := await voice.read(1024 * 1024):
                target.write(chunk)
        voice_path = str(destination.resolve())

    ai_options = {
        "voiceType": _clean_option("voiceType", voiceType),
        "electronicLevel": _clean_option("electronicLevel", electronicLevel),
        "mood": _clean_option("mood", mood),
        "tempo": _clean_option("tempo", tempo),
        "durationLevel": _clean_option("durationLevel", durationLevel),
    }
    author_name = _clean_author(author)
    theme_text = theme.strip()
    option_summary = _option_summary(ai_options)
    mode_label = "保留曲调换歌词" if creation_mode == "melody_rewrite" else "Music3 全新创作" if creation_mode == "music3_original" else "全新创作"
    stored_theme = f"{theme_text}｜{mode_label}｜{option_summary}" if theme_text else f"{mode_label}｜{option_summary}"

    queue = get_task_queue(settings)
    try:
        job_id, _ = queue.enqueue_job(
            _AI_KIND,
            {
                "sourceTrackId": source.id if source else 0,
                "theme": theme_text,
                "author": author_name,
                "code": code.strip().upper(),
                "deviceId": deviceId.strip(),
                "appVersion": appVersion.strip(),
                "deviceModel": deviceModel.strip(),
                "voicePath": voice_path,
                "creationMode": creation_mode,
                **ai_options,
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"后台队列不可用：{str(exc)[:160]}") from exc

    title_prefix = "AI 改编" if creation_mode == "melody_rewrite" and source else "AI Music3" if creation_mode == "music3_original" else "AI 创作"
    title = f"{title_prefix} - {source.title}".strip() if source else "AI 原创音乐"
    record = AiMusicGeneration(
        job_id=job_id,
        device_id=deviceId.strip() or "unknown",
        license_code=code.strip().upper() or None,
        source_track_id=source.id if source else 0,
        title=title,
        artist=author_name,
        theme=stored_theme,
        status="queued",
        message="已加入 AI 创作队列",
        voice_path=voice_path,
    )
    db.add(record)
    db.commit()
    queue.update_job(job_id, query=title, message=record.message, result=_generation_payload(db, record))
    return _queue_payload(job_id)


@router.get("/status/{job_id}", summary="查询 AI 创作任务")
def ai_generation_status(job_id: str, db: Session = Depends(get_db)) -> dict:
    job = _queue_payload(job_id)
    record = db.scalar(select(AiMusicGeneration).where(AiMusicGeneration.job_id == job_id))
    if not job and not record:
        raise HTTPException(status_code=404, detail="任务不存在")
    if record:
        job["result"] = _generation_payload(db, record)
        if record.generated_track_id:
            track = db.get(LocalTrack, record.generated_track_id)
            if track:
                job["track"] = _track_payload(track)
    return job


@router.get("/generated/list", summary="我生成的音乐")
def generated_list(deviceId: str = "", code: str = "", db: Session = Depends(get_db)) -> list[dict]:
    device_id = deviceId.strip()
    license_code = code.strip().upper()
    if not device_id and not license_code:
        return []
    statement = select(AiMusicGeneration).order_by(desc(AiMusicGeneration.updated_at)).limit(200)
    if device_id:
        statement = statement.where(AiMusicGeneration.device_id == device_id)
    elif license_code:
        statement = statement.where(AiMusicGeneration.license_code == license_code)
    return [_generation_payload(db, record) for record in db.scalars(statement).all()]


def run_ai_music_generation_job(job_id: str, payload_data: dict[str, Any]) -> None:
    settings = get_settings()
    queue = get_task_queue(settings)
    db = SessionLocal()
    has_executor_lock = False
    try:
        record = db.scalar(select(AiMusicGeneration).where(AiMusicGeneration.job_id == job_id))
        if not record:
            queue.finish_job(job_id, "failed", "AI 创作记录不存在")
            return

        source_track_id = int(payload_data.get("sourceTrackId") or record.source_track_id or 0)
        source = db.get(LocalTrack, source_track_id) if source_track_id > 0 else None
        if source_track_id > 0 and (not source or not Path(source.file_path).exists()):
            _finish_failed(db, queue, record, job_id, "源歌曲文件不存在")
            return
        if not settings.ai_generator_base_url.strip():
            _finish_failed(db, queue, record, job_id, "AI 生成服务未配置，请设置 AI_GENERATOR_BASE_URL")
            return

        creation_mode = _clean_creation_mode(str(payload_data.get("creationMode") or "original"))
        has_executor_lock = _wait_for_ai_executor(settings, queue, db, record, job_id, creation_mode=creation_mode)
        if not has_executor_lock:
            _finish_failed(db, queue, record, job_id, record.message or "AI 创作服务维护中，请恢复后再试")
            return

        record.status = "analyzing"
        record.message = "正在分析原曲结构" if creation_mode in {"melody_rewrite", "music3_original"} and source else "正在准备 AI 创作参数"
        record.updated_at = datetime.utcnow()
        db.commit()
        queue.update_job(job_id, status="analyzing", percent=15, message=record.message)

        lyrics_record = db.scalar(select(TrackLyrics).where(TrackLyrics.track_id == source.id)) if source else None
        if source and not lyrics_record:
            ensure_track_lyrics(settings, db, source, force=False)
            lyrics_record = db.scalar(select(TrackLyrics).where(TrackLyrics.track_id == source.id))

        record.status = "generating"
        if creation_mode == "music3_original" and source:
            record.message = "正在按参考歌曲生成歌词，并交给 Music3 创作新歌"
        elif creation_mode == "melody_rewrite" and source:
            record.message = "正在适配原曲歌词并生成 AI 音频"
        else:
            record.message = "正在生成 AI 音频"
        record.updated_at = datetime.utcnow()
        db.commit()
        queue.update_job(job_id, status="generating", percent=35, message=record.message)

        if not _wait_for_generator_ready(settings, queue, db, record, job_id, percent=35, creation_mode=creation_mode):
            _finish_failed(db, queue, record, job_id, "AI 创作服务维护中，请恢复后再试")
            return

        response_data = _call_generator(settings, source, lyrics_record, record, payload_data)
        queue.update_job(job_id, status="importing", percent=82, message="正在保存创作音频")
        audio_raw = base64.b64decode(str(response_data.get("audioBase64") or ""))
        if not audio_raw:
            _finish_failed(db, queue, record, job_id, "AI 生成服务未返回音频")
            return

        results_dir = settings.ai_upload_dir / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        suffix = "." + str(response_data.get("format") or "mp3").strip(".").lower()
        result_path = results_dir / f"{job_id}{suffix}"
        result_path.write_bytes(audio_raw)

        title = str(response_data.get("title") or record.title or (f"AI 创作 - {source.title}" if source else "AI 原创音乐"))[:260]
        author_name = _clean_author(str(payload_data.get("author") or record.artist or response_data.get("artist") or ""))
        artist = author_name[:260]
        track = import_audio_file(
            settings,
            db,
            result_path,
            title=title,
            artist=artist,
            album="我生成的音乐",
            remove_source=False,
        )

        queue.update_job(job_id, status="lyrics", percent=92, message="正在生成同步歌词")
        generated_lyrics = str(response_data.get("lyrics") or "")
        lyrics_metadata = {
            "ar": author_name,
            "by": author_name,
            "作词": author_name,
            "作曲": author_name,
            "编曲": author_name,
            "制作": author_name,
        }
        synced_lrc = str(response_data.get("syncedLyrics") or "")
        synced_source = str(response_data.get("lyricsSource") or "ai_asr_lrc")
        if not save_ai_generated_lyrics(
            settings,
            db,
            track,
            generated_lyrics,
            metadata=lyrics_metadata,
            synced_lrc=synced_lrc,
            synced_source=synced_source,
        ):
            _finish_failed(db, queue, record, job_id, "音乐已生成，但同步歌词生成失败，请重新提交或在后台补全歌词")
            return

        record.generated_track_id = track.id
        record.title = title
        record.artist = artist
        record.status = "success"
        record.message = "AI 创作完成，歌词已同步"
        record.result_path = track.file_path
        record.analysis = str(response_data.get("analysis") or "")
        record.caption = str(response_data.get("caption") or "")
        record.lyrics = generated_lyrics
        record.updated_at = datetime.utcnow()
        db.commit()
        queue.update_job(
            job_id,
            status="success",
            percent=100,
            message=record.message,
            result=_generation_payload(db, record),
            track=_track_payload(track),
        )
        queue.finish_job(job_id, "success", record.message)
    except Exception as exc:
        message = str(exc)[:300] or "AI 创作失败"
        record = db.scalar(select(AiMusicGeneration).where(AiMusicGeneration.job_id == job_id))
        if record:
            _finish_failed(db, queue, record, job_id, message)
        else:
            queue.finish_job(job_id, "failed", message)
    finally:
        if has_executor_lock:
            _release_ai_executor(queue, job_id)
        _remove_ai_waiting(queue, job_id)
        db.close()
        queue.release_dedupe(job_id)


def _call_generator(settings, source: LocalTrack | None, lyrics_record: TrackLyrics | None, record: AiMusicGeneration, payload_data: dict[str, Any]) -> dict:
    base_url = settings.ai_generator_base_url.rstrip("/")
    url = base_url + "/api/generate-from-files"
    headers = {}
    if settings.ai_generator_api_key:
        headers["X-API-Key"] = settings.ai_generator_api_key
    error = _generator_mode_availability_error(settings, _clean_creation_mode(str(payload_data.get("creationMode") or "original")))
    if error:
        raise RuntimeError(error)

    voice_path = str(payload_data.get("voicePath") or record.voice_path or "")
    has_voice = bool(voice_path and Path(voice_path).exists())
    mode = "reference"
    if source and has_voice:
        mode = "voice_reference"
    elif not source and has_voice:
        mode = "voice_original"
    elif not source:
        mode = "original"
    data = {
        "mode": mode,
        "sourceTitle": source.title if source else "",
        "sourceArtist": source.artist if source else "",
        "sourceAlbum": (source.album or "") if source else "",
        "theme": str(payload_data.get("theme") or ""),
        "sourceLyrics": str(payload_data.get("sourceLyrics") or (lyrics_record.raw_lrc if lyrics_record else "")),
        "creationMode": _clean_creation_mode(str(payload_data.get("creationMode") or "original")),
        "jobId": record.job_id,
        "lyricsOverride": str(payload_data.get("lyricsOverride") or ""),
        "voiceType": _clean_option("voiceType", str(payload_data.get("voiceType") or "auto")),
        "electronicLevel": _clean_option("electronicLevel", str(payload_data.get("electronicLevel") or "auto")),
        "mood": _clean_option("mood", str(payload_data.get("mood") or "auto")),
        "tempo": _clean_option("tempo", str(payload_data.get("tempo") or "auto")),
        "durationLevel": _clean_option("durationLevel", str(payload_data.get("durationLevel") or "auto")),
        "optionSummary": _option_summary(payload_data),
    }
    files = {}
    if source:
        files["sourceAudio"] = (Path(source.file_path).name, Path(source.file_path).open("rb"), "application/octet-stream")
    if voice_path and Path(voice_path).exists():
        files["voiceAudio"] = (Path(voice_path).name, Path(voice_path).open("rb"), "application/octet-stream")
    try:
        with httpx.Client(timeout=settings.ai_generator_timeout_seconds) as client:
            try:
                response = client.post(url, data=data, files=files or None, headers=headers)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                detail = _response_error_detail(exc.response)
                raise RuntimeError(detail or "AI 生成失败，请稍后重试") from exc
            except httpx.TimeoutException as exc:
                raise RuntimeError("AI 生成超时，请稍后在“我生成的音乐”中刷新查看，或重新提交") from exc
            except httpx.ConnectError as exc:
                raise RuntimeError("AI 生成服务连接失败，请确认本地生成机和服务器隧道已启动") from exc
            except httpx.HTTPError as exc:
                raise RuntimeError("AI 生成服务网络异常，请稍后重试") from exc
    finally:
        for file_tuple in files.values():
            file_tuple[1].close()


def _response_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail[:300]
    except Exception:
        pass
    return response.text[:300]


def _finish_failed(db: Session, queue, record: AiMusicGeneration, job_id: str, message: str) -> None:
    record.status = "failed"
    record.message = message[:600]
    record.updated_at = datetime.utcnow()
    db.commit()
    current = queue.get_job(job_id, include_items=False) or {}
    percent = max(0, min(int(current.get("percent") or 0), 95))
    queue.update_job(job_id, status="failed", percent=percent, message=record.message, result=_generation_payload(db, record))
    queue.finish_job(job_id, "failed", record.message)


def _generator_availability_error(settings) -> str:
    base_url = settings.ai_generator_base_url.strip().rstrip("/")
    if not base_url:
        return "AI 创作服务维护中，请恢复后再试"
    headers = {}
    if settings.ai_generator_api_key:
        headers["X-API-Key"] = settings.ai_generator_api_key
    try:
        with httpx.Client(timeout=8) as client:
            response = client.get(base_url + "/health", headers=headers)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        return "AI 生成服务暂时不可用，请稍后重试；本地生成机或服务器隧道未连接"
    if not _generator_health_ready(payload):
        return "AI 创作服务维护中，请恢复后再试"
    return ""


def _require_generator_available(settings) -> None:
    error = _generator_availability_error(settings)
    if error:
        raise HTTPException(status_code=503, detail="AI 创作服务维护中，请恢复后再试")


def _generator_mode_availability_error(settings, creation_mode: str) -> str:
    if creation_mode != "melody_rewrite":
        if creation_mode == "music3_original":
            base_url = settings.ai_generator_base_url.strip().rstrip("/")
            if not base_url:
                return "Music3 创作服务维护中，请恢复后再试"
            headers = {}
            if settings.ai_generator_api_key:
                headers["X-API-Key"] = settings.ai_generator_api_key
            try:
                with httpx.Client(timeout=10) as client:
                    response = client.get(base_url + "/health", headers=headers)
                    response.raise_for_status()
                    payload = response.json()
            except Exception:
                return "Music3 创作服务维护中，请恢复后再试"
            if bool(payload.get("comfy")):
                return ""
            return "Music3 创作服务维护中，请恢复后再试"
        return _generator_availability_error(settings)
    base_url = settings.ai_generator_base_url.strip().rstrip("/")
    if not base_url:
        return "保留曲调创作模型维护中，请恢复后再试"
    headers = {}
    if settings.ai_generator_api_key:
        headers["X-API-Key"] = settings.ai_generator_api_key
    try:
        with httpx.Client(timeout=10) as client:
            response = client.get(base_url + "/health", headers=headers)
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return "保留曲调创作模型维护中，请恢复后再试"
    if not bool(payload.get("ok")):
        return "保留曲调创作模型维护中，请恢复后再试"
    melody_svs = payload.get("melodySvs") if isinstance(payload.get("melodySvs"), dict) else {}
    if bool(melody_svs.get("ready")):
        return ""
    reason = str(melody_svs.get("reason") or "")
    if reason == "full SVS status requires /health?full=true":
        try:
            with httpx.Client(timeout=30) as client:
                response = client.get(base_url + "/health?full=true", headers=headers)
                response.raise_for_status()
                payload = response.json()
        except Exception:
            return "保留曲调创作模型维护中，请恢复后再试"
        if not bool(payload.get("ok")):
            return "保留曲调创作模型维护中，请恢复后再试"
        melody_svs = payload.get("melodySvs") if isinstance(payload.get("melodySvs"), dict) else {}
        if bool(melody_svs.get("ready")):
            return ""
    missing = melody_svs.get("missing") if isinstance(melody_svs.get("missing"), list) else []
    if missing:
        return "保留曲调创作模型维护中，请恢复后再试：" + "、".join(str(item) for item in missing[:3])
    return "保留曲调创作模型维护中，请恢复后再试"


def _require_generator_available_for_mode(settings, creation_mode: str) -> None:
    error = _generator_mode_availability_error(settings, creation_mode)
    if error:
        raise HTTPException(status_code=503, detail=error)


def _wait_for_generator_ready(
    settings,
    queue,
    db: Session,
    record: AiMusicGeneration,
    job_id: str,
    *,
    percent: int,
    creation_mode: str = "",
) -> bool:
    deadline = time.time() + _AI_GENERATOR_HEALTH_GRACE_SECONDS
    last_error = ""
    while time.time() < deadline:
        last_error = _generator_mode_availability_error(settings, creation_mode)
        if not last_error:
            return True
        record.status = "queued"
        record.message = "AI 创作服务正在恢复中，请稍候"
        record.updated_at = datetime.utcnow()
        db.commit()
        queue.update_job(
            job_id,
            status="queued",
            percent=percent,
            message=record.message,
            result=_generation_payload(db, record),
        )
        time.sleep(_AI_GENERATOR_HEALTH_POLL_SECONDS)
    if last_error:
        record.message = last_error[:600]
        db.commit()
    return False


def _generator_health_ready(payload: dict[str, Any]) -> bool:
    if not bool(payload.get("ok")):
        return False
    if bool(payload.get("comfy")):
        return True
    melody = payload.get("melodyRewrite") if isinstance(payload.get("melodyRewrite"), dict) else {}
    return bool(melody.get("strictBackendAvailable"))


def _ai_executor_lock_key(queue) -> str:
    return f"{queue.namespace}:lock:{_AI_KIND}:executor"


def _ai_waiting_key(queue) -> str:
    return f"{queue.namespace}:waiting:{_AI_KIND}"


def _acquire_ai_executor(queue, job_id: str) -> bool:
    return bool(queue.redis.set(_ai_executor_lock_key(queue), job_id, nx=True, ex=_AI_EXECUTOR_LOCK_TTL_SECONDS))


def _release_ai_executor(queue, job_id: str) -> None:
    key = _ai_executor_lock_key(queue)
    if queue.redis.get(key) == job_id:
        queue.redis.delete(key)


def _remove_ai_waiting(queue, job_id: str) -> None:
    queue.redis.zrem(_ai_waiting_key(queue), job_id)


def _ai_queue_ahead(queue, job_id: str) -> int:
    ahead = queue.queued_ahead_count(_AI_KIND, job_id)
    active = str(queue.redis.get(_ai_executor_lock_key(queue)) or "")
    if active and active != job_id:
        ahead += 1
    waiting_ids = queue.redis.zrange(_ai_waiting_key(queue), 0, -1)
    for waiting_id in waiting_ids:
        if waiting_id == job_id:
            break
        if waiting_id != active:
            ahead += 1
    return max(0, ahead)


def _with_ai_queue_info(queue, job: dict, job_id: str) -> dict:
    if not job or job.get("kind") != _AI_KIND:
        return job
    if str(job.get("status") or "") in TERMINAL_STATUSES:
        job["queueAhead"] = 0
        job["queuePosition"] = 0
        return job
    ahead = _ai_queue_ahead(queue, job_id)
    job["queueAhead"] = ahead
    job["queuePosition"] = ahead + 1
    if str(job.get("status") or "") == "queued" and ahead > 0:
        job["message"] = f"前面还有 {ahead} 个任务在排队，请耐心等待"
    return job


def _wait_for_ai_executor(settings, queue, db: Session, record: AiMusicGeneration, job_id: str, creation_mode: str = "") -> bool:
    if _acquire_ai_executor(queue, job_id):
        _remove_ai_waiting(queue, job_id)
        return True

    queue.redis.zadd(_ai_waiting_key(queue), {job_id: time.time()})
    while True:
        ahead = _ai_queue_ahead(queue, job_id)
        record.status = "queued"
        record.message = f"前面还有 {ahead} 个任务在排队，请耐心等待" if ahead > 0 else "正在等待 AI 创作资源"
        record.updated_at = datetime.utcnow()
        db.commit()
        queue.update_job(job_id, status="queued", percent=0, message=record.message, result=_generation_payload(db, record))
        time.sleep(10)
        if not _wait_for_generator_ready(settings, queue, db, record, job_id, percent=0, creation_mode=creation_mode):
            return False
        if _acquire_ai_executor(queue, job_id):
            _remove_ai_waiting(queue, job_id)
            return True
