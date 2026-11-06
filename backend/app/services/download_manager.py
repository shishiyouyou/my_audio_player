
from __future__ import annotations

import asyncio
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db import SessionLocal
from app.services.importer import AUDIO_EXTENSIONS, import_inbox
from app.services.legal_downloader import DownloadSource, download_one, load_config
from app.services.musicdl_trending import (
    NETEASE_PLAYLISTS,
    PlaylistSong,
    download_song,
    find_existing_track,
    playlist_display_name,
    preview_playlist,
)

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "running": False,
    "trigger": "idle",
    "started_at": None,
    "finished_at": None,
    "total": 0,
    "done": 0,
    "success": 0,
    "skipped": 0,
    "failed": 0,
    "current": "",
    "message": "",
    "items": [],
}


def status_file(settings: Settings) -> Path:
    return settings.downloader_config_file.with_name("download_status.json")


def get_status(settings: Settings) -> dict[str, Any]:
    with _LOCK:
        state = dict(_STATE)
        state["items"] = list(_STATE.get("items", []))[-300:]
    if state.get("started_at"):
        state["percent"] = min(100, int((state.get("done") or 0) * 100 / max(int(state.get("total") or 1), 1)))
        return state
    path = status_file(settings)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            loaded["percent"] = min(100, int((loaded.get("done") or 0) * 100 / max(int(loaded.get("total") or 1), 1)))
            return loaded
        except Exception:
            pass
    state["percent"] = 0
    return state


def is_running() -> bool:
    with _LOCK:
        return bool(_STATE.get("running"))


def start_configured_download(settings: Settings, trigger: str = "manual") -> bool:
    if is_running():
        return False
    thread = threading.Thread(target=run_configured_download, args=(settings, trigger), daemon=True)
    thread.start()
    return True


def start_txt_download(settings: Settings, text: str, filename: str = "songs.txt") -> bool:
    if is_running():
        return False
    thread = threading.Thread(target=run_txt_download, args=(settings, text, filename), daemon=True)
    thread.start()
    return True


def start_playlist_download(
    settings: Settings,
    playlist: str,
    limit: int = 20,
    skip_existing: bool = True,
    import_after: bool = True,
) -> bool:
    if is_running():
        return False
    thread = threading.Thread(
        target=run_playlist_download,
        args=(settings, playlist, limit, skip_existing, import_after),
        daemon=True,
    )
    thread.start()
    return True


def run_configured_download(settings: Settings, trigger: str = "scheduled") -> dict[str, Any]:
    config = load_config(settings)
    direct_sources = list(config.get("sources", []))
    playlists = list(config.get("playlists", []))
    playlist_limit = int(config.get("playlist_limit") or 20)
    estimated_total = len(direct_sources) + len(playlists) * max(playlist_limit, 1)
    _reset(settings, trigger, estimated_total, "准备下载")
    try:
        for item in direct_sources:
            source = DownloadSource(url=item.get("url", ""), filename=item.get("filename", ""))
            _run_direct_source(settings, source)
        for playlist in playlists:
            _run_playlist(settings, playlist, playlist_limit, bool(config.get("skip_existing", True)))
        if bool(config.get("import_after", True)) and _success_count() > 0:
            _import_after(settings)
        _finish(settings, "下载任务完成")
    except Exception as exc:
        _add_item(settings, "任务", "failed", f"任务异常：{exc}")
        _finish(settings, f"下载任务异常：{exc}")
    return get_status(settings)


def run_playlist_download(
    settings: Settings,
    playlist: str,
    limit: int = 20,
    skip_existing: bool = True,
    import_after: bool = True,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 20), 100))
    _reset(settings, f"playlist:{playlist}", limit, f"准备下载榜单：{playlist_display_name(playlist)}")
    try:
        _run_playlist(settings, playlist, limit, skip_existing)
        if import_after and _success_count() > 0:
            _import_after(settings)
        _finish(settings, "榜单下载任务完成")
    except Exception as exc:
        _add_item(settings, "榜单任务", "failed", f"任务异常：{exc}")
        _finish(settings, f"榜单下载任务异常：{exc}")
    return get_status(settings)


def run_txt_download(settings: Settings, text: str, filename: str = "songs.txt") -> dict[str, Any]:
    songs = parse_txt_songs(text)
    _reset(settings, f"txt:{filename}", len(songs), "解析 TXT 歌单")
    db = SessionLocal()
    try:
        for song in songs:
            _run_song(settings, db, song, skip_existing=True)
        if _success_count() > 0:
            _import_after(settings)
        _finish(settings, "TXT 下载任务完成")
    except Exception as exc:
        _add_item(settings, "TXT", "failed", f"任务异常：{exc}")
        _finish(settings, f"TXT 下载任务异常：{exc}")
    finally:
        db.close()
    return get_status(settings)


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
        songs.append(PlaylistSong(rank=len(songs) + 1, song_id=f"txt-{len(songs)+1}", title=title, artist=artist or "Unknown"))
    return songs


def _run_direct_source(settings: Settings, source: DownloadSource) -> None:
    label = source.filename or source.url
    _set_current(settings, label)
    try:
        message = asyncio.run(download_one(settings, source))
        if message.startswith("已下载"):
            _add_item(settings, label, "success", message)
        elif message.startswith("已跳过"):
            _add_item(settings, label, "skipped", message)
        else:
            _add_item(settings, label, "failed", message)
    except Exception as exc:
        _add_item(settings, label, "failed", f"失败：{exc}")


def _run_playlist(settings: Settings, playlist: str, limit: int, skip_existing: bool) -> None:
    db = SessionLocal()
    try:
        preview = preview_playlist(settings, db, playlist, limit=limit, skip_existing=False)
        _increase_total(settings, max(len(preview.songs) - limit, 0))
        for song in preview.songs[:limit]:
            _run_song(settings, db, song, skip_existing=skip_existing)
    finally:
        db.close()


def _run_song(settings: Settings, db, song: PlaylistSong, skip_existing: bool) -> None:
    label = song.query
    _set_current(settings, label)
    existing = find_existing_track(settings, db, song) if skip_existing else None
    if existing:
        _add_item(settings, label, "skipped", "已跳过：歌曲已存在", path=str(existing))
        return
    before = _audio_files(settings)
    try:
        ok = download_song(settings, song)
    except Exception as exc:
        _add_item(settings, label, "failed", f"失败：{str(exc)[:160]}")
        return
    if ok:
        downloaded_path = find_existing_track(settings, db, song) or _new_downloaded_file(settings, before)
        _add_item(settings, label, "success", "下载成功", path=str(downloaded_path) if downloaded_path else "")
    else:
        _add_item(settings, label, "failed", "下载失败：未找到可用资源")


def _import_after(settings: Settings) -> None:
    db = SessionLocal()
    try:
        imported, skipped = import_inbox(settings, db)
        _set_message(settings, f"导入完成：新增 {imported} 首，跳过 {skipped} 首")
    except Exception as exc:
        _set_message(settings, f"导入失败：{exc}")
    finally:
        db.close()


def _reset(settings: Settings, trigger: str, total: int, message: str) -> None:
    with _LOCK:
        _STATE.clear()
        _STATE.update({
            "running": True,
            "trigger": trigger,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "total": max(int(total or 0), 0),
            "done": 0,
            "success": 0,
            "skipped": 0,
            "failed": 0,
            "current": "",
            "message": message,
            "items": [],
        })
    _save(settings)


def _finish(settings: Settings, message: str) -> None:
    with _LOCK:
        _STATE["running"] = False
        _STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _STATE["current"] = ""
        _STATE["message"] = message
    _save(settings)


def _increase_total(settings: Settings, delta: int) -> None:
    if delta <= 0:
        return
    with _LOCK:
        _STATE["total"] = int(_STATE.get("total") or 0) + delta
    _save(settings)


def _set_current(settings: Settings, current: str) -> None:
    with _LOCK:
        _STATE["current"] = current
        _STATE["message"] = f"正在处理：{current}"
    _save(settings)


def _set_message(settings: Settings, message: str) -> None:
    with _LOCK:
        _STATE["message"] = message
    _save(settings)


def _success_count() -> int:
    with _LOCK:
        return int(_STATE.get("success") or 0)


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


def _add_item(settings: Settings, name: str, status: str, message: str, path: str = "") -> None:
    with _LOCK:
        _STATE["done"] = int(_STATE.get("done") or 0) + 1
        if status == "success":
            _STATE["success"] = int(_STATE.get("success") or 0) + 1
        elif status == "skipped":
            _STATE["skipped"] = int(_STATE.get("skipped") or 0) + 1
        else:
            _STATE["failed"] = int(_STATE.get("failed") or 0) + 1
        _STATE.setdefault("items", []).append({
            "time": datetime.now().isoformat(timespec="seconds"),
            "name": name,
            "status": status,
            "message": message,
            "path": path,
        })
        _STATE["message"] = message
    _save(settings)


def _save(settings: Settings) -> None:
    path = status_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        data = dict(_STATE)
        data["items"] = list(_STATE.get("items", []))[-500:]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _clean_title(value: str) -> str:
    value = re.sub(r"^\d+\.\s*", "", value.strip())
    value = re.sub(r"\(.*?\)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\uff08.*?\uff09", "", value)
    value = re.sub(r"(\u4f34\u594f|\u4f34\u594f\u7248|\u539f\u5531|\u7ffb\u5531|Remix)", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def _normalize_key(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "").casefold())
