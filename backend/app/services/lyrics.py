from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from mutagen import File
from mutagen.id3 import ID3
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import SessionLocal
from app.models import LocalTrack, TrackLyrics
from app.services.cache import delete_cached_json
from app.services.task_queue import empty_status, get_task_queue


_TIME_TAG_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_META_TAG_RE = re.compile(r"^\[(ti|ar|al|by|re|ve|offset):([^\]]*)\]\s*", re.IGNORECASE)
_ANY_META_LINE_RE = re.compile(r"^\[([^:\]]+):([^\]]*)\]\s*$")
LYRICS_METADATA_KEYS = ("ar", "al", "by", "作词", "作曲", "编曲", "制作")
DEFAULT_LYRICS_METADATA = {
    "ar": "络新+V:ABCD59113",
    "al": "人到中年",
    "by": "络新",
    "作词": "络新",
    "作曲": "络新",
    "编曲": "络新",
    "制作": "络新",
}


def parse_lrc_text(raw_text: str) -> dict:
    lines: list[dict[str, object]] = []
    offset_ms = 0
    metadata: dict[str, str] = {}

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        for tag, value in _META_TAG_RE.findall(line):
            key = tag.lower()
            metadata[key] = value.strip()
            if key == "offset":
                try:
                    offset_ms = int(float(value.strip() or 0))
                except ValueError:
                    offset_ms = 0

        timestamps = _TIME_TAG_RE.findall(line)
        if not timestamps:
            continue
        text = _TIME_TAG_RE.sub("", line).strip()
        if not text:
            continue

        for minute_text, second_text, fraction_text in timestamps:
            minute = int(minute_text)
            second = int(second_text)
            fraction = fraction_text or "0"
            fraction_ms = int((fraction + "00")[:3])
            lines.append(
                {
                    "timeMs": minute * 60_000 + second * 1_000 + fraction_ms,
                    "text": text,
                }
            )

    deduped: dict[tuple[int, str], dict[str, object]] = {}
    for item in lines:
        key = (int(item["timeMs"]), str(item["text"]))
        deduped[key] = item
    normalized = sorted(deduped.values(), key=lambda item: int(item["timeMs"]))
    return {
        "lines": normalized,
        "offsetMs": offset_ms,
        "title": metadata.get("ti", ""),
        "artist": metadata.get("ar", ""),
        "album": metadata.get("al", ""),
        "synced": bool(normalized),
    }


def parse_lyrics_text(raw_text: str, duration_seconds: int = 0) -> dict:
    parsed = parse_lrc_text(raw_text)
    if parsed["lines"]:
        return parsed

    plain_lines = [
        line.strip()
        for line in raw_text.splitlines()
        if line.strip() and not _ANY_META_LINE_RE.match(line.strip())
    ]
    if not plain_lines:
        return {
            "lines": [],
            "offsetMs": 0,
            "title": "",
            "artist": "",
            "album": "",
            "synced": False,
        }

    duration_ms = max(0, int(duration_seconds or 0) * 1000)
    if duration_ms > 0 and len(plain_lines) > 1:
        step_ms = max(1500, duration_ms // max(1, len(plain_lines) - 1))
    else:
        step_ms = 2500
    parsed["lines"] = [
        {"timeMs": index * step_ms, "text": text}
        for index, text in enumerate(plain_lines)
    ]
    parsed["synced"] = False
    return parsed


def read_lrc_file(path: Path) -> tuple[str, dict]:
    raw = path.read_bytes()
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = raw.decode("utf-8", errors="replace")
    return text, parse_lyrics_text(text)


def lrc_metadata_defaults() -> dict[str, str]:
    return dict(DEFAULT_LYRICS_METADATA)


def extract_lrc_metadata(raw_text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    known = set(LYRICS_METADATA_KEYS)
    for raw_line in (raw_text or "").splitlines():
        match = _ANY_META_LINE_RE.match(raw_line.strip())
        if not match:
            continue
        key = match.group(1).strip()
        value = match.group(2).strip()
        if key in known:
            found[key] = value
    return found


def merged_lrc_metadata(raw_text: str = "") -> dict[str, str]:
    metadata = lrc_metadata_defaults()
    metadata.update({key: value for key, value in extract_lrc_metadata(raw_text).items() if value})
    return metadata


def replace_lrc_metadata(raw_text: str, metadata: dict[str, str]) -> str:
    values = lrc_metadata_defaults()
    values.update({key: str(value or "").strip() for key, value in (metadata or {}).items() if key in values})
    known = set(LYRICS_METADATA_KEYS)
    content_lines: list[str] = []
    for raw_line in (raw_text or "").splitlines():
        stripped = raw_line.strip()
        match = _ANY_META_LINE_RE.match(stripped)
        if match and match.group(1).strip() in known:
            continue
        content_lines.append(raw_line.rstrip())
    while content_lines and not content_lines[0].strip():
        content_lines.pop(0)
    header_lines = [f"[{key}:{values.get(key, '')}]" for key in LYRICS_METADATA_KEYS]
    body = "\n".join(content_lines).strip()
    return "\n".join(header_lines + ([""] if body else []) + ([body] if body else [])) + "\n"


def lrc_text_from_record(record: TrackLyrics) -> str:
    raw = (record.raw_lrc or "").strip()
    if raw:
        return raw + "\n"
    try:
        lines = json.loads(record.lines_json or "[]")
    except json.JSONDecodeError:
        lines = []
    if not isinstance(lines, list):
        lines = []
    output: list[str] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        time_ms = max(0, int(line.get("timeMs") or 0))
        minutes = time_ms // 60000
        seconds = (time_ms % 60000) // 1000
        centiseconds = (time_ms % 1000) // 10
        output.append(f"[{minutes:02d}:{seconds:02d}.{centiseconds:02d}]{text}")
    return "\n".join(output) + ("\n" if output else "")


def write_track_lrc_file(track: LocalTrack, raw_text: str) -> Path | None:
    audio_path = Path(track.file_path)
    if not audio_path.exists():
        return None
    lrc_path = audio_path.with_suffix(".lrc")
    lrc_path.parent.mkdir(parents=True, exist_ok=True)
    lrc_path.write_text(raw_text, encoding="utf-8")
    return lrc_path


_AI_SECTION_RE = re.compile(
    r"^\[(intro|verse|pre\s*chorus|pre-chorus|chorus|post\s*chorus|post-chorus|hook|bridge|final\s*chorus|final\s*hook|outro)[^\]]*\]\s*$",
    re.IGNORECASE,
)


def build_ai_synced_lrc(
    title: str,
    raw_lyrics: str,
    duration_seconds: int,
    *,
    metadata: dict[str, str] | None = None,
) -> str:
    lyric_lines = _clean_ai_lyric_lines(raw_lyrics)
    if not lyric_lines:
        return ""

    duration_ms = max(30_000, int(duration_seconds or 0) * 1000)
    start_ms = min(6_000, max(1_500, duration_ms // 12))
    end_guard_ms = min(8_000, max(2_000, duration_ms // 18))
    end_ms = max(start_ms + 1_000, duration_ms - end_guard_ms)
    if len(lyric_lines) == 1:
        timed_lines = [(start_ms, lyric_lines[0])]
    else:
        timed_lines = _estimate_ai_lyric_times(lyric_lines, start_ms, end_ms)

    values = lrc_metadata_defaults()
    if metadata:
        values.update({key: str(value or "").strip() for key, value in metadata.items() if key in values})

    output = [f"[ti:{title.strip()}]"] if title.strip() else []
    output.extend(f"[{key}:{values.get(key, '')}]" for key in LYRICS_METADATA_KEYS)
    output.append("[offset:0]")
    output.append("")
    output.extend(f"[{_format_lrc_time(ms)}]{text}" for ms, text in timed_lines)
    return "\n".join(output).strip() + "\n"


def save_ai_generated_lyrics(
    settings: Settings,
    db: Session,
    track: LocalTrack,
    raw_lyrics: str,
    *,
    metadata: dict[str, str] | None = None,
    synced_lrc: str | None = None,
    synced_source: str = "ai_asr_lrc",
) -> bool:
    lrc_text = ""
    if synced_lrc and parse_lrc_text(synced_lrc).get("lines"):
        lrc_text = replace_lrc_metadata(synced_lrc, metadata or {})
        source = synced_source or "ai_asr_lrc"
    else:
        lrc_text = build_ai_synced_lrc(
            track.title,
            raw_lyrics,
            track.duration_seconds,
            metadata=metadata,
        )
        source = "ai_generated_lrc"
    if not lrc_text:
        return False
    if not upsert_track_lyrics_text(
        settings,
        db,
        track.id,
        lrc_text,
        source,
        duration_seconds=track.duration_seconds,
    ):
        return False
    write_track_lrc_file(track, lrc_text)
    return True


def _clean_ai_lyric_lines(raw_lyrics: str) -> list[str]:
    lines: list[str] = []
    for raw_line in (raw_lyrics or "").splitlines():
        line = raw_line.strip()
        if not line or _ANY_META_LINE_RE.match(line) or _AI_SECTION_RE.match(line):
            continue
        line = _TIME_TAG_RE.sub("", line).strip()
        line = re.sub(r"^[\-*•\d.\s]+", "", line).strip()
        if not line:
            continue
        parts = [part.strip() for part in re.split(r"[，,；;。！？!?]\s*", line) if part.strip()]
        if len(parts) > 1 and any(len(part) >= 4 for part in parts):
            lines.extend(part for part in parts if len(part) >= 2)
        else:
            lines.append(line)
    deduped: list[str] = []
    previous = ""
    for line in lines:
        if line != previous:
            deduped.append(line)
        previous = line
    return deduped[:120]


def _estimate_ai_lyric_times(lyric_lines: list[str], start_ms: int, end_ms: int) -> list[tuple[int, str]]:
    available_ms = max(1_000, end_ms - start_ms)
    weights = [_lyric_line_weight(line) for line in lyric_lines]
    total_weight = max(1.0, sum(weights))
    timed: list[tuple[int, str]] = []
    elapsed = 0.0
    min_gap = 900
    previous_ms = start_ms - min_gap
    for index, line in enumerate(lyric_lines):
        if index == 0:
            ms = start_ms
        else:
            ms = start_ms + int(available_ms * elapsed / total_weight)
            if ms - previous_ms < min_gap:
                ms = previous_ms + min_gap
        timed.append((min(ms, end_ms), line))
        elapsed += weights[index]
        previous_ms = timed[-1][0]
    return timed


def _lyric_line_weight(line: str) -> float:
    text = re.sub(r"\s+", "", line or "")
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z0-9]", text))
    # Chinese singing usually holds 4-7 characters per phrase; longer lines need more time.
    weight = max(1.0, cjk_count / 5.5 + latin_count / 9.0)
    if any(mark in line for mark in ("啊", "哦", "啦", "呀")):
        weight += 0.4
    return min(weight, 4.5)


def _format_lrc_time(ms: int) -> str:
    value = max(0, int(ms))
    minutes = value // 60000
    seconds = (value % 60000) // 1000
    centiseconds = (value % 1000) // 10
    return f"{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def apply_track_lyrics_metadata(
    settings: Settings,
    db: Session,
    track: LocalTrack,
    metadata: dict[str, str],
) -> bool:
    record = db.scalar(select(TrackLyrics).where(TrackLyrics.track_id == track.id))
    if record is None:
        return False
    raw_text = replace_lrc_metadata(lrc_text_from_record(record), metadata)
    parsed = parse_lyrics_text(raw_text, duration_seconds=track.duration_seconds)
    if not parsed["lines"]:
        return False
    record.lines_json = json.dumps(parsed["lines"], ensure_ascii=False)
    record.raw_lrc = raw_text.strip()
    record.source = record.source or "local_lrc"
    record.offset_ms = int(parsed.get("offsetMs") or 0)
    record.updated_at = datetime.utcnow()
    write_track_lrc_file(track, raw_text)
    db.flush()
    delete_cached_json(settings, f"lyrics:{track.id}")
    return True


def generate_track_lrc_file(
    settings: Settings,
    db: Session,
    track: LocalTrack,
    *,
    metadata: dict[str, str] | None = None,
    force: bool = False,
    allow_network: bool = True,
) -> tuple[bool, str]:
    if force or not db.scalar(select(TrackLyrics).where(TrackLyrics.track_id == track.id)):
        ensure_track_lyrics(settings, db, track, force=force, allow_network=allow_network)
    record = db.scalar(select(TrackLyrics).where(TrackLyrics.track_id == track.id))
    if record is None:
        return False, "未找到可用歌词，无法生成 LRC 文件。"
    values = merged_lrc_metadata(record.raw_lrc)
    if metadata:
        values.update({key: str(value or "").strip() for key, value in metadata.items() if key in DEFAULT_LYRICS_METADATA})
    if not apply_track_lyrics_metadata(settings, db, track, values):
        return False, "歌词内容为空或解析失败，未生成 LRC 文件。"
    db.commit()
    lrc_path = Path(track.file_path).with_suffix(".lrc")
    return True, f"已生成歌词文件：{lrc_path}"


def find_sidecar_lyrics(audio_path: Path) -> Path | None:
    exact = audio_path.with_suffix(".lrc")
    if exact.exists():
        return exact

    candidates = sorted(audio_path.parent.glob("*.lrc"))
    if not candidates:
        return None
    audio_key = _normalize_key(audio_path.stem)
    for candidate in candidates:
        candidate_key = _normalize_key(candidate.stem)
        if audio_key and (audio_key in candidate_key or candidate_key in audio_key):
            return candidate
    return candidates[0]


def _preserve_manual_sync_mode(record: TrackLyrics | None, source: str) -> str:
    cleaned = source.strip() or "local_lrc"
    if record and (record.source or "").endswith("_plain") and not cleaned.endswith("_plain"):
        return f"{cleaned}_plain"
    return cleaned


def upsert_track_lyrics(settings: Settings, db: Session, track_id: int, lrc_path: Path | None) -> bool:
    if not lrc_path or not lrc_path.exists():
        return False
    try:
        raw_text, parsed = read_lrc_file(lrc_path)
    except OSError:
        return False
    if not parsed["lines"]:
        return False

    record = db.scalar(select(TrackLyrics).where(TrackLyrics.track_id == track_id))
    if record is None:
        record = TrackLyrics(track_id=track_id)
        db.add(record)
    record.lines_json = json.dumps(parsed["lines"], ensure_ascii=False)
    record.raw_lrc = raw_text
    record.source = _preserve_manual_sync_mode(record, str(lrc_path.name))
    record.offset_ms = int(parsed.get("offsetMs") or 0)
    record.updated_at = datetime.utcnow()
    db.flush()
    delete_cached_json(settings, f"lyrics:{track_id}")
    return True


def upsert_track_lyrics_text(
    settings: Settings,
    db: Session,
    track_id: int,
    raw_text: str,
    source: str,
    duration_seconds: int = 0,
) -> bool:
    cleaned = (raw_text or "").strip()
    if not cleaned:
        return False
    parsed = parse_lyrics_text(cleaned, duration_seconds=duration_seconds)
    if not parsed["lines"]:
        return False

    record = db.scalar(select(TrackLyrics).where(TrackLyrics.track_id == track_id))
    if record is None:
        record = TrackLyrics(track_id=track_id)
        db.add(record)
    record.lines_json = json.dumps(parsed["lines"], ensure_ascii=False)
    record.raw_lrc = cleaned
    parsed_source = source if parsed.get("synced") else f"{source}_plain"
    record.source = _preserve_manual_sync_mode(record, parsed_source)
    record.offset_ms = int(parsed.get("offsetMs") or 0)
    record.updated_at = datetime.utcnow()
    db.flush()
    delete_cached_json(settings, f"lyrics:{track_id}")
    return True


def extract_audio_lyrics(audio_path: Path) -> str:
    try:
        audio = File(audio_path)
    except Exception:
        audio = None
    if not audio:
        return ""

    values: list[str] = []
    tags = getattr(audio, "tags", None)
    if tags:
        for key in ("lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS", "lyric", "LYRIC", "©lyr"):
            value = None
            try:
                if hasattr(tags, "__getitem__"):
                    value = tags[key]
                elif hasattr(tags, "get"):
                    value = tags.get(key)
            except Exception:
                value = None
            values.extend(_flatten_tag_value(value))

    if audio_path.suffix.lower() == ".mp3":
        try:
            id3 = ID3(audio_path)
        except Exception:
            id3 = None
        if id3:
            for frame in id3.getall("USLT"):
                values.extend(_flatten_tag_value(getattr(frame, "text", "")))
            for frame in id3.getall("SYLT"):
                values.extend(_flatten_tag_value(_sylt_to_text(frame)))

    for value in values:
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return ""


def sync_track_lyrics_from_path(
    settings: Settings,
    db: Session,
    track_id: int,
    audio_path: Path,
    duration_seconds: int = 0,
    *,
    title: str = "",
    artist: str = "",
    album: str = "",
    force: bool = False,
    allow_network: bool = False,
) -> bool:
    if not audio_path.exists():
        return False
    record = db.scalar(select(TrackLyrics).where(TrackLyrics.track_id == track_id))
    if record and not force:
        try:
            lines = json.loads(record.lines_json or "[]")
        except json.JSONDecodeError:
            lines = []
        if isinstance(lines, list) and lines:
            return False

    lrc_path = find_sidecar_lyrics(audio_path)
    if lrc_path and upsert_track_lyrics(settings, db, track_id, lrc_path):
        return True

    raw_text = extract_audio_lyrics(audio_path)
    if raw_text:
        return upsert_track_lyrics_text(
            settings,
            db,
            track_id,
            raw_text,
            source="embedded",
            duration_seconds=duration_seconds,
        )
    if allow_network:
        online = fetch_online_lyrics(title=title, artist=artist, album=album, duration_seconds=duration_seconds)
        if online:
            raw_text, source = online
            return upsert_track_lyrics_text(
                settings,
                db,
                track_id,
                raw_text,
                source=source,
                duration_seconds=duration_seconds,
            )
    return False


def ensure_track_lyrics(
    settings: Settings,
    db: Session,
    track: LocalTrack,
    *,
    force: bool = False,
    allow_network: bool = False,
) -> bool:
    return sync_track_lyrics_from_path(
        settings,
        db,
        track.id,
        Path(track.file_path),
        duration_seconds=track.duration_seconds,
        title=track.title,
        artist=track.artist,
        album=track.album or "",
        force=force,
        allow_network=allow_network,
    )


def backfill_library_lyrics(
    settings: Settings,
    db: Session,
    *,
    only_missing: bool = True,
    force: bool = False,
    limit: int = 0,
) -> dict[str, int]:
    tracks = db.scalars(select(LocalTrack).order_by(LocalTrack.imported_at.desc())).all()
    processed = 0
    updated = 0
    skipped = 0
    missing = 0
    for track in tracks:
        if limit and processed >= limit:
            break
        processed += 1
        if not force:
            record = db.scalar(select(TrackLyrics).where(TrackLyrics.track_id == track.id))
            if record:
                try:
                    lines = json.loads(record.lines_json or "[]")
                except json.JSONDecodeError:
                    lines = []
                if isinstance(lines, list) and lines:
                    if only_missing:
                        skipped += 1
                        continue
        if ensure_track_lyrics(settings, db, track, force=force, allow_network=True):
            record = db.scalar(select(TrackLyrics).where(TrackLyrics.track_id == track.id))
            if record:
                apply_track_lyrics_metadata(settings, db, track, merged_lrc_metadata(record.raw_lrc))
            updated += 1
            db.commit()
        else:
            missing += 1
    if updated:
        db.commit()
    return {"processed": processed, "updated": updated, "skipped": skipped, "missing": missing}


def delete_track_lyrics(settings: Settings, db: Session, track_id: int) -> bool:
    record = db.scalar(select(TrackLyrics).where(TrackLyrics.track_id == track_id))
    if not record:
        return False
    db.delete(record)
    db.commit()
    delete_cached_json(settings, f"lyrics:{track_id}")
    return True


def set_track_lyrics_synced(settings: Settings, db: Session, track_id: int, synced: bool) -> bool:
    record = db.scalar(select(TrackLyrics).where(TrackLyrics.track_id == track_id))
    if not record:
        return False
    source = (record.source or "local_lrc").strip() or "local_lrc"
    if synced:
        while source.endswith("_plain"):
            source = source[: -len("_plain")]
        record.source = source or "local_lrc"
    elif not source.endswith("_plain"):
        record.source = f"{source}_plain"
    record.updated_at = datetime.utcnow()
    db.flush()
    delete_cached_json(settings, f"lyrics:{track_id}")
    return True


QUEUE_KIND = "lyrics_backfill"


def enqueue_lyrics_backfill(settings: Settings, *, force: bool = False, only_missing: bool = True) -> tuple[bool, str]:
    try:
        job_id, created = get_task_queue(settings).enqueue_job(
            QUEUE_KIND,
            {"force": bool(force), "onlyMissing": bool(only_missing)},
            dedupe_key=f"lyrics:{'force' if force else 'missing'}",
        )
    except Exception as exc:
        return False, f"后台队列不可用：{str(exc)[:160]}"
    return created, f"歌词补全任务已进入后台队列：{job_id}" if created else f"复用后台任务：{job_id}"


def get_lyrics_backfill_status(settings: Settings) -> dict[str, Any]:
    try:
        return get_task_queue(settings).get_status_for_kind(QUEUE_KIND) or empty_status()
    except Exception as exc:
        status = empty_status()
        status["status"] = "failed"
        status["message"] = f"后台队列不可用：{str(exc)[:160]}"
        return status


def run_lyrics_backfill_job(settings: Settings, job_id: str) -> dict[str, Any]:
    queue = get_task_queue(settings)
    job = queue.get_job(job_id, include_items=False) or {}
    payload = dict(job.get("payload") or {})
    force = bool(payload.get("force"))
    only_missing = bool(payload.get("onlyMissing", True))

    db = SessionLocal()
    try:
        rows = db.execute(
            select(LocalTrack, TrackLyrics)
            .outerjoin(TrackLyrics, TrackLyrics.track_id == LocalTrack.id)
            .order_by(LocalTrack.imported_at.desc())
        ).all()
        candidates: list[LocalTrack] = []
        for track, record in rows:
            if only_missing and not force and record:
                try:
                    lines = json.loads(record.lines_json or "[]")
                except json.JSONDecodeError:
                    lines = []
                if isinstance(lines, list) and lines:
                    continue
            candidates.append(track)

        queue.update_job(job_id, total=len(candidates), message="正在准备歌词补全")
        updated = 0
        skipped = 0
        missing = 0
        for track in candidates:
            label = f"{track.title} - {track.artist}"
            queue.update_job(job_id, current=label, message=f"正在补全：{label}")
            ok = ensure_track_lyrics(settings, db, track, force=force, allow_network=True)
            if ok:
                record = db.scalar(select(TrackLyrics).where(TrackLyrics.track_id == track.id))
                if record:
                    apply_track_lyrics_metadata(settings, db, track, merged_lrc_metadata(record.raw_lrc))
                db.commit()
                updated += 1
                queue.add_progress_item(job_id, label, "success", "已补全歌词并生成 LRC", path=track.file_path)
            else:
                skipped += 1
                missing += 1
                queue.add_progress_item(job_id, label, "skipped", "未找到可用歌词", path=track.file_path)
        queue.update_job(job_id, result={"updated": updated, "skipped": skipped, "missing": missing})
        if missing:
            queue.finish_job(job_id, "success", f"歌词补全完成，已更新 {updated} 首，仍缺失 {missing} 首")
        else:
            queue.finish_job(job_id, "success", f"歌词补全完成，已更新 {updated} 首")
        return queue.get_job(job_id) or empty_status()
    except Exception as exc:
        queue.finish_job(job_id, "failed", f"歌词补全失败：{str(exc)[:300]}")
        return queue.get_job(job_id) or empty_status()
    finally:
        db.close()


def lyrics_payload(record: TrackLyrics | None, track_id: int) -> dict:
    if record is None:
        return {"trackId": str(track_id), "hasLyrics": False, "isSynced": False, "offsetMs": 0, "lines": []}
    try:
        lines = json.loads(record.lines_json or "[]")
    except json.JSONDecodeError:
        lines = []
    if not isinstance(lines, list):
        lines = []
    source = record.source or "local_lrc"
    return {
        "trackId": str(track_id),
        "hasLyrics": bool(lines),
        "isSynced": not source.endswith("_plain"),
        "offsetMs": int(record.offset_ms or 0),
        "source": source,
        "updatedAt": record.updated_at.isoformat() if record.updated_at else "",
        "lines": lines,
    }


def _normalize_key(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "").casefold())


def _flatten_tag_value(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        flattened: list[str] = []
        for item in value:
            flattened.extend(_flatten_tag_value(item))
        return flattened
    text = str(value).strip()
    return [text] if text else []


def _sylt_to_text(frame: object) -> str:
    items = getattr(frame, "text", None) or getattr(frame, "sylt", None) or []
    parts: list[str] = []
    for item in items:
        try:
            timestamp = int(getattr(item, "time", 0) or getattr(item, "timestamp", 0) or 0)
            lyric = str(getattr(item, "text", "") or getattr(item, "lyric", "") or "").strip()
        except Exception:
            continue
        if lyric:
            parts.append(f"[{timestamp // 1000 // 60:02d}:{timestamp // 1000 % 60:02d}.{timestamp % 1000:03d}]{lyric}")
    return "\n".join(parts)


def fetch_online_lyrics(
    *,
    title: str,
    artist: str,
    album: str = "",
    duration_seconds: int = 0,
) -> tuple[str, str] | None:
    if not title.strip() or not artist.strip():
        return None

    params: dict[str, Any] = {
        "track_name": title.strip(),
        "artist_name": artist.strip(),
    }
    if album.strip():
        params["album_name"] = album.strip()
    if duration_seconds:
        params["duration"] = int(duration_seconds)

    headers = {"User-Agent": "MusicHubLyrics/1.0"}
    try:
        with httpx.Client(timeout=12, follow_redirects=True, headers=headers) as client:
            for endpoint in ("https://lrclib.net/api/get", "https://lrclib.net/api/search"):
                try:
                    response = client.get(endpoint, params=params)
                    if response.status_code >= 400:
                        continue
                    payload = response.json()
                    candidate = _pick_online_lyrics(payload)
                    if candidate:
                        return candidate
                except Exception:
                    continue

            try:
                response = client.get(
                    f"https://api.lyrics.ovh/v1/{quote(artist.strip(), safe='')}/{quote(title.strip(), safe='')}"
                )
                if response.status_code < 400:
                    payload = response.json()
                    text = str(payload.get("lyrics") or "").strip()
                    if text:
                        return text, "lyrics_ovh"
            except Exception:
                pass
    except Exception:
        return None
    return None


def _pick_online_lyrics(payload: Any) -> tuple[str, str] | None:
    candidates: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        candidates = [payload]
    elif isinstance(payload, list):
        candidates = [item for item in payload if isinstance(item, dict)]

    for item in candidates:
        synced = str(item.get("syncedLyrics") or item.get("synced_lyrics") or "").strip()
        if synced:
            return synced, "lrclib"
    for item in candidates:
        plain = str(item.get("plainLyrics") or item.get("plain_lyrics") or item.get("lyrics") or "").strip()
        if plain:
            return plain, "lrclib_plain"
    return None
