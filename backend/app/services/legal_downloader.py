from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from app.config import Settings
from app.db import SessionLocal
from app.services.importer import AUDIO_EXTENSIONS, import_inbox
from app.services.musicdl_trending import NETEASE_PLAYLISTS, PlaylistSong, download_song, find_existing_track, playlist_display_name, preview_playlist
from app.services.task_queue import empty_status, get_task_queue


QUEUE_KIND = "configured_download"


@dataclass
class DownloadSource:
    url: str
    filename: str = ""


def default_config() -> dict:
    return {
        "enabled": False,
        "hour": 4,
        "minute": 20,
        "sources": [],
        "playlists": [],
        "playlist_limit": 20,
        "skip_existing": True,
        "import_after": True,
    }


def load_config(settings: Settings) -> dict:
    path = settings.downloader_config_file
    if not path.exists():
        return default_config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default_config()
    config = default_config()
    if isinstance(data, dict):
        config.update({key: data.get(key, config[key]) for key in config})
    config["enabled"] = bool(config.get("enabled"))
    config["hour"] = max(0, min(23, int(config.get("hour") or 0)))
    config["minute"] = max(0, min(59, int(config.get("minute") or 0)))
    config["sources"] = [
        item
        for item in config.get("sources", [])
        if isinstance(item, dict) and str(item.get("url", "")).startswith(("http://", "https://"))
    ]
    config["playlists"] = [
        str(item)
        for item in config.get("playlists", [])
        if str(item) in NETEASE_PLAYLISTS or str(item).isdigit()
    ]
    config["playlist_limit"] = max(1, min(100, int(config.get("playlist_limit") or 20)))
    config["skip_existing"] = bool(config.get("skip_existing", True))
    config["import_after"] = bool(config.get("import_after", True))
    return config


def save_config(
    settings: Settings,
    enabled: bool,
    hour: int,
    minute: int,
    sources_text: str,
    playlists: list[str] | None = None,
    playlist_limit: int = 20,
    skip_existing: bool = True,
    import_after: bool = True,
) -> dict:
    sources: list[dict] = []
    for line in sources_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            url, filename = [part.strip() for part in line.split("|", 1)]
        else:
            url, filename = line, ""
        if url.startswith(("http://", "https://")):
            sources.append({"url": url, "filename": filename})
    config = {
        "enabled": enabled,
        "hour": max(0, min(23, int(hour))),
        "minute": max(0, min(59, int(minute))),
        "sources": sources,
        "playlists": [
            str(item)
            for item in (playlists or [])
            if str(item) in NETEASE_PLAYLISTS or str(item).isdigit()
        ],
        "playlist_limit": max(1, min(100, int(playlist_limit or 20))),
        "skip_existing": bool(skip_existing),
        "import_after": bool(import_after),
    }
    settings.downloader_config_file.parent.mkdir(parents=True, exist_ok=True)
    settings.downloader_config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


async def download_configured_sources(settings: Settings, trigger: str = "manual") -> list[str]:
    config = load_config(settings)
    if not config["sources"] and not config["playlists"]:
        return ["\u6ca1\u6709\u53ef\u6267\u884c\u7684\u4e0b\u8f7d\u6e90"]
    try:
        queue = get_task_queue(settings)
        job_id, created = queue.enqueue_job(
            QUEUE_KIND,
            {"mode": "configured", "trigger": trigger, "sourceCount": len(config["sources"]), "playlistCount": len(config["playlists"])},
            dedupe_key="download",
        )
    except Exception as exc:
        return [f"\u540e\u53f0\u961f\u5217\u4e0d\u53ef\u7528\uff1a{str(exc)[:160]}"]
    if created:
        return [f"\u4e0b\u8f7d\u4efb\u52a1\u5df2\u8fdb\u5165\u540e\u53f0\u961f\u5217\uff1a{job_id}"]
    return [f"\u590d\u7528\u540e\u53f0\u4efb\u52a1\uff1a{job_id}"]


async def enqueue_playlist_download(
    settings: Settings,
    playlist: str,
    limit: int = 20,
    skip_existing: bool = True,
    import_after: bool = True,
) -> tuple[bool, str]:
    try:
        job_id, created = get_task_queue(settings).enqueue_job(
            QUEUE_KIND,
            {
                "mode": "playlist",
                "playlist": playlist,
                "limit": max(1, min(100, int(limit or 20))),
                "skipExisting": bool(skip_existing),
                "importAfter": bool(import_after),
            },
            dedupe_key="download",
        )
    except Exception as exc:
        return False, f"\u540e\u53f0\u961f\u5217\u4e0d\u53ef\u7528\uff1a{str(exc)[:160]}"
    return created, f"\u699c\u5355\u4e0b\u8f7d\u4efb\u52a1\u5df2\u8fdb\u5165\u540e\u53f0\u961f\u5217\uff1a{job_id}" if created else f"\u590d\u7528\u540e\u53f0\u4efb\u52a1\uff1a{job_id}"


async def enqueue_txt_download(settings: Settings, text: str, filename: str = "songs.txt") -> tuple[bool, str]:
    songs = parse_txt_songs(text)
    if not songs:
        return False, "\u672a\u5728 TXT \u4e2d\u8bc6\u522b\u5230\u53ef\u4e0b\u8f7d\u7684\u6b4c\u66f2"
    try:
        job_id, created = get_task_queue(settings).enqueue_job(
            QUEUE_KIND,
            {
                "mode": "txt",
                "filename": filename,
                "songs": [song.__dict__ for song in songs],
            },
            dedupe_key="download",
        )
    except Exception as exc:
        return False, f"\u540e\u53f0\u961f\u5217\u4e0d\u53ef\u7528\uff1a{str(exc)[:160]}"
    return created, f"TXT \u4e0b\u8f7d\u4efb\u52a1\u5df2\u8fdb\u5165\u540e\u53f0\u961f\u5217\uff1a{job_id}" if created else f"\u590d\u7528\u540e\u53f0\u4efb\u52a1\uff1a{job_id}"


def get_download_status(settings: Settings) -> dict:
    try:
        queue = get_task_queue(settings)
        return queue.get_status_for_kind(QUEUE_KIND) or empty_status()
    except Exception as exc:
        status = empty_status()
        status["status"] = "failed"
        status["message"] = f"\u540e\u53f0\u961f\u5217\u4e0d\u53ef\u7528\uff1a{str(exc)[:160]}"
        return status


def run_configured_download_job(settings: Settings, job_id: str) -> dict:
    queue = get_task_queue(settings)
    existing = queue.get_job(job_id, include_items=False) or {}
    payload = dict(existing.get("payload") or {})
    mode = str(payload.get("mode") or "configured")

    if mode == "playlist":
        limit = max(1, min(100, int(payload.get("limit") or 20)))
        queue.update_job(job_id, total=limit, message="\u6b63\u5728\u51c6\u5907\u699c\u5355\u4e0b\u8f7d")
        _run_playlist(settings, job_id, str(payload.get("playlist") or "hot"), limit, bool(payload.get("skipExisting", True)))
        if bool(payload.get("importAfter", True)) and _job_success_count(queue, job_id) > 0:
            _import_after(settings, job_id)
    elif mode == "txt":
        songs = [
            PlaylistSong(
                rank=int(item.get("rank") or index + 1),
                song_id=str(item.get("song_id") or f"txt-{index + 1}"),
                title=str(item.get("title") or ""),
                artist=str(item.get("artist") or "Unknown"),
            )
            for index, item in enumerate(payload.get("songs") or [])
            if isinstance(item, dict) and str(item.get("title") or "").strip()
        ]
        queue.update_job(job_id, total=len(songs), message="\u6b63\u5728\u51c6\u5907 TXT \u4e0b\u8f7d")
        _run_songs(settings, job_id, songs, skip_existing=True)
        if _job_success_count(queue, job_id) > 0:
            _import_after(settings, job_id)
    else:
        config = load_config(settings)
        sources = list(config.get("sources", []))
        playlists = list(config.get("playlists", []))
        playlist_limit = int(config.get("playlist_limit") or 20)
        estimated_total = len(sources) + len(playlists) * max(playlist_limit, 1)
        queue.update_job(
            job_id,
            payload={**payload, "sourceCount": len(sources), "playlistCount": len(playlists)},
            total=estimated_total,
            message="\u6b63\u5728\u51c6\u5907\u4e0b\u8f7d",
        )
        if not sources and not playlists:
            queue.finish_job(job_id, "success", "\u6ca1\u6709\u53ef\u6267\u884c\u7684\u4e0b\u8f7d\u6e90")
            return queue.get_job(job_id) or empty_status()

        for item in sources:
            source = DownloadSource(url=item.get("url", ""), filename=item.get("filename", ""))
            _run_direct_source(settings, job_id, source)
        for playlist in playlists:
            _run_playlist(settings, job_id, playlist, playlist_limit, bool(config.get("skip_existing", True)))
        if bool(config.get("import_after", True)) and _job_success_count(queue, job_id) > 0:
            _import_after(settings, job_id)

    finished = queue.get_job(job_id, include_items=False) or empty_status()
    if finished.get("failed", 0):
        queue.finish_job(job_id, "failed", "\u4e0b\u8f7d\u5b8c\u6210\uff0c\u90e8\u5206\u4efb\u52a1\u5931\u8d25")
    else:
        queue.finish_job(job_id, "success", "\u4e0b\u8f7d\u4efb\u52a1\u5b8c\u6210")
    return queue.get_job(job_id) or empty_status()


def parse_txt_songs(text: str) -> list[PlaylistSong]:
    songs: list[PlaylistSong] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^\d+\.\s*", "", line)
        if " - " in line:
            title, artist = [part.strip() for part in line.split(" - ", 1)]
        elif "\t" in line:
            title, artist = [part.strip() for part in line.split("\t", 1)]
        else:
            title, artist = line, "Unknown"
        title = _clean_title(title)
        if not title:
            continue
        key = _normalize_key(f"{title}-{artist}")
        if key in seen:
            continue
        seen.add(key)
        songs.append(PlaylistSong(rank=len(songs) + 1, song_id=f"txt-{len(songs) + 1}", title=title, artist=artist or "Unknown"))
    return songs


def _run_direct_source(settings: Settings, job_id: str, source: DownloadSource) -> None:
    queue = get_task_queue(settings)
    label = source.filename or source.url
    queue.update_job(job_id, current=label, message=f"\u6b63\u5728\u4e0b\u8f7d\uff1a{label}")
    try:
        message = asyncio.run(download_one(settings, source))
        status = "success"
        if "\u8df3\u8fc7" in message:
            status = "skipped"
        elif "\u5931\u8d25" in message:
            status = "failed"
        queue.add_progress_item(job_id, label, status, message, path="")
    except Exception as exc:
        queue.add_progress_item(job_id, label, "failed", f"\u4e0b\u8f7d\u5931\u8d25\uff1a{str(exc)[:160]}")
    time.sleep(0.3)


def _run_playlist(settings: Settings, job_id: str, playlist: str, limit: int, skip_existing: bool) -> None:
    db = SessionLocal()
    try:
        get_task_queue(settings).update_job(job_id, current=playlist_display_name(playlist), message=f"\u6b63\u5728\u8bfb\u53d6\u699c\u5355\uff1a{playlist_display_name(playlist)}")
        preview = preview_playlist(settings, db, playlist, limit=limit, skip_existing=False)
        _run_songs(settings, job_id, preview.songs[:limit], skip_existing)
    finally:
        db.close()


def _run_songs(settings: Settings, job_id: str, songs: list[PlaylistSong], skip_existing: bool) -> None:
    db = SessionLocal()
    try:
        for song in songs:
            _run_song(settings, job_id, db, song, skip_existing)
    finally:
        db.close()


def _run_song(settings: Settings, job_id: str, db, song: PlaylistSong, skip_existing: bool) -> None:
    queue = get_task_queue(settings)
    label = song.query
    queue.update_job(job_id, current=label, message=f"\u6b63\u5728\u5904\u7406\uff1a{label}")
    existing = find_existing_track(settings, db, song) if skip_existing else None
    if existing:
        queue.add_progress_item(job_id, label, "skipped", "\u5df2\u8df3\u8fc7\uff1a\u6b4c\u66f2\u5df2\u5b58\u5728", path=str(existing))
        return
    before = _audio_files(settings)
    try:
        ok = download_song(settings, song)
    except Exception as exc:
        queue.add_progress_item(job_id, label, "failed", f"\u5931\u8d25\uff1a{str(exc)[:160]}")
        return
    if ok:
        downloaded_path = find_existing_track(settings, db, song) or _new_downloaded_file(settings, before)
        queue.add_progress_item(job_id, label, "success", "\u4e0b\u8f7d\u6210\u529f", path=str(downloaded_path) if downloaded_path else "")
    else:
        queue.add_progress_item(job_id, label, "failed", "\u4e0b\u8f7d\u5931\u8d25\uff1a\u672a\u627e\u5230\u53ef\u7528\u8d44\u6e90")


def _import_after(settings: Settings, job_id: str) -> None:
    db = SessionLocal()
    try:
        imported, skipped = import_inbox(settings, db)
        get_task_queue(settings).update_job(job_id, message=f"\u5bfc\u5165\u5b8c\u6210\uff1a\u65b0\u589e {imported} \u9996\uff0c\u8df3\u8fc7 {skipped} \u9996")
    except Exception as exc:
        get_task_queue(settings).add_progress_item(job_id, "\u5bfc\u5165\u66f2\u5e93", "failed", f"\u5bfc\u5165\u5931\u8d25\uff1a{str(exc)[:160]}")
    finally:
        db.close()


def _job_success_count(queue, job_id: str) -> int:
    job = queue.get_job(job_id, include_items=False) or {}
    return int(job.get("success") or 0)


def _audio_files(settings: Settings) -> set[Path]:
    files: set[Path] = set()
    for root in (settings.inbox_dir, settings.music_dir):
        if not root.exists():
            continue
        files.update(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS)
    return files


def _new_downloaded_file(settings: Settings, before: set[Path]) -> Path | None:
    new_files = list(_audio_files(settings) - before)
    if not new_files:
        return None
    return max(new_files, key=lambda path: path.stat().st_mtime)


async def download_one(settings: Settings, source: DownloadSource) -> str:
    parsed = urlparse(source.url)
    if parsed.scheme not in {"http", "https"}:
        return f"\u5931\u8d25\uff1a\u53ea\u652f\u6301 HTTP/HTTPS \u94fe\u63a5 {source.url}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        async with client.stream("GET", source.url) as response:
            if response.status_code >= 400:
                return f"\u5931\u8d25\uff1aHTTP {response.status_code} {source.url}"
            filename = _resolve_filename(source, response)
            suffix = Path(filename).suffix.lower()
            if suffix not in AUDIO_EXTENSIONS:
                return f"\u5931\u8d25\uff1a\u4e0d\u662f\u652f\u6301\u7684\u97f3\u9891\u6587\u4ef6 {filename}"
            target = _unique_path(settings.inbox_dir / Path(filename).name)
            settings.inbox_dir.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as out:
                async for chunk in response.aiter_bytes():
                    out.write(chunk)
    return f"\u5df2\u4e0b\u8f7d\uff1a{target.name}"


def sources_to_text(config: dict) -> str:
    lines = []
    for item in config.get("sources", []):
        url = item.get("url", "")
        filename = item.get("filename", "")
        lines.append(f"{url} | {filename}" if filename else url)
    return "\n".join(lines)


def _resolve_filename(source: DownloadSource, response: httpx.Response) -> str:
    if source.filename:
        return source.filename
    disposition = response.headers.get("content-disposition", "")
    if "filename=" in disposition:
        filename = disposition.split("filename=", 1)[1].strip().strip('"')
        if filename:
            return unquote(filename)
    parsed_name = Path(unquote(urlparse(source.url).path)).name
    if parsed_name:
        return parsed_name
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    suffix = {
        "audio/mpeg": ".mp3",
        "audio/flac": ".flac",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/mp4": ".m4a",
    }.get(content_type, ".mp3")
    return f"download{suffix}"


def _unique_path(path: Path) -> Path:
    target = path
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        counter += 1
    return target


def _clean_title(value: str) -> str:
    value = re.sub(r"^\d+\.\s*", "", value.strip())
    value = re.sub(r"\(.*?\)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\uff08.*?\uff09", "", value)
    value = re.sub(r"(\u4f34\u594f|\u4f34\u594f\u7248|\u539f\u5531|\u7ffb\u5531|Remix)", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def _normalize_key(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "").casefold())
