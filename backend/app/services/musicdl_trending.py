from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import LocalTrack
from app.services.importer import AUDIO_EXTENSIONS, import_inbox


NETEASE_PLAYLIST_API = "https://music.163.com/api/playlist/detail"
NETEASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://music.163.com/",
}
NETEASE_PLAYLISTS = {
    "hot": {"name": "\u70ed\u6b4c\u699c", "playlist_id": "3778678"},
    "soaring": {"name": "\u98d9\u5347\u699c", "playlist_id": "19723756"},
    "new": {"name": "\u65b0\u6b4c\u699c", "playlist_id": "3779629"},
    "original": {"name": "\u539f\u521b\u699c", "playlist_id": "2884035"},
    "guofeng": {"name": "\u56fd\u98ce\u699c", "playlist_id": "5059642708"},
    "rap_cn": {"name": "\u4e2d\u6587\u8bf4\u5531\u699c", "playlist_id": "991319590"},
    "acg": {"name": "ACG\u699c", "playlist_id": "71385702"},
    "western": {"name": "\u6b27\u7f8e\u70ed\u699c", "playlist_id": "2809513713"},
}
PLATFORMS = ["qq", "netease"]
QUALITY = "hi"
MUSICDL_SOURCE_MAP = {
    "qq": "QQMusicClient",
    "netease": "NeteaseMusicClient",
}


@dataclass(frozen=True)
class PlaylistSong:
    rank: int
    song_id: str
    title: str
    artist: str

    @property
    def query(self) -> str:
        if self.artist and self.artist != "Unknown":
            return f"{self.title} - {self.artist}"
        return self.title


@dataclass(frozen=True)
class PlaylistPreview:
    playlist_name: str
    playlist_id: str
    total: int
    songs: list[PlaylistSong]
    skipped_existing: int


@dataclass(frozen=True)
class MusicDownloadSummary:
    total: int
    downloaded: int
    skipped_existing: int
    failed: int
    imported: int
    import_skipped: int
    messages: list[str]


def resolve_playlist_id(value: str) -> str:
    value = str(value or "").strip()
    if value in NETEASE_PLAYLISTS:
        return NETEASE_PLAYLISTS[value]["playlist_id"]
    if value.isdigit():
        return value
    return NETEASE_PLAYLISTS["hot"]["playlist_id"]


def playlist_display_name(playlist_id: str) -> str:
    for item in NETEASE_PLAYLISTS.values():
        if item["playlist_id"] == str(playlist_id):
            return item["name"]
    return f"\u7f51\u6613\u4e91\u6b4c\u5355 {playlist_id}"


def fetch_netease_playlist(playlist_id: str, limit: int = 100) -> list[PlaylistSong]:
    playlist_id = resolve_playlist_id(playlist_id)
    limit = max(1, min(int(limit or 100), 200))
    with httpx.Client(timeout=20, headers=NETEASE_HEADERS, follow_redirects=True) as client:
        response = client.get(NETEASE_PLAYLIST_API, params={"id": playlist_id})
        response.raise_for_status()
    data = response.json()
    if data.get("code") != 200:
        message = data.get("msg") or data.get("message") or f"\u65e0\u6cd5\u83b7\u53d6\u699c\u5355\uff1a{playlist_id}"
        raise RuntimeError(message)
    tracks = data.get("result", {}).get("tracks", []) or data.get("playlist", {}).get("tracks", []) or []
    songs: list[PlaylistSong] = []
    for track in tracks:
        title = _clean_title(str(track.get("name") or ""))
        if not title:
            continue
        artist = _extract_artist(track)
        songs.append(PlaylistSong(rank=len(songs) + 1, song_id=str(track.get("id") or ""), title=title, artist=artist))
        if len(songs) >= limit:
            break
    return songs


def preview_playlist(settings: Settings, db: Session, playlist_id: str, limit: int = 100, skip_existing: bool = True) -> PlaylistPreview:
    resolved_id = resolve_playlist_id(playlist_id)
    songs = fetch_netease_playlist(resolved_id, limit=limit)
    filtered: list[PlaylistSong] = []
    skipped = 0
    seen: set[str] = set()
    for song in songs:
        key = _normalize_key(song.query)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        if skip_existing and find_existing_track(settings, db, song):
            skipped += 1
            continue
        filtered.append(song)
    return PlaylistPreview(
        playlist_name=playlist_display_name(resolved_id),
        playlist_id=resolved_id,
        total=len(songs),
        songs=filtered,
        skipped_existing=skipped,
    )


def download_playlist_to_inbox(
    settings: Settings,
    db: Session,
    playlist_id: str,
    limit: int = 20,
    skip_existing: bool = True,
    import_after: bool = True,
) -> MusicDownloadSummary:
    preview = preview_playlist(settings, db, playlist_id, limit=limit, skip_existing=skip_existing)
    messages: list[str] = []
    downloaded = 0
    failed = 0
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    for song in preview.songs:
        if skip_existing and find_existing_track(settings, db, song):
            messages.append(f"\u8df3\u8fc7\uff1a{song.query} \u5df2\u5b58\u5728")
            continue
        try:
            if download_song(settings, song):
                downloaded += 1
                messages.append(f"\u5df2\u4e0b\u8f7d\uff1a{song.query}")
            else:
                failed += 1
                messages.append(f"\u5931\u8d25\uff1a{song.query} \u672a\u627e\u5230\u53ef\u7528\u8d44\u6e90")
        except Exception as exc:
            failed += 1
            messages.append(f"\u5931\u8d25\uff1a{song.query} {str(exc)[:120]}")
        time.sleep(1)
    imported = 0
    import_skipped = 0
    if import_after and downloaded:
        imported, import_skipped = import_inbox(settings, db)
    return MusicDownloadSummary(
        total=preview.total,
        downloaded=downloaded,
        skipped_existing=preview.skipped_existing,
        failed=failed,
        imported=imported,
        import_skipped=import_skipped,
        messages=messages,
    )


def _ensure_musicdl_typing_compat() -> None:
    import typing

    if hasattr(typing, "Unpack"):
        return
    try:
        from typing_extensions import Unpack
    except ImportError as exc:
        raise RuntimeError("Python 3.10 requires typing_extensions for musicdl compatibility.") from exc
    typing.Unpack = Unpack


def download_song(settings: Settings, song: PlaylistSong) -> bool:
    _ensure_musicdl_typing_compat()
    try:
        from musicdl.musicdl import MusicClient
    except ImportError as exc:
        raise RuntimeError("\u670d\u52a1\u5668\u7f3a\u5c11 musicdl\uff0c\u8bf7\u5728 backend \u865a\u62df\u73af\u5883\u5b89\u88c5 musicdl\u3002") from exc

    client = _create_music_client(settings.inbox_dir)
    search_results = client.search(keyword=song.query)
    song_info = _select_best_song(search_results, song.title)
    if song_info is None:
        return False
    downloaded = client.download(song_infos=[song_info])
    return bool(downloaded)


def find_existing_track(settings: Settings, db: Session, song: PlaylistSong) -> Path | None:
    title_key = _normalize_key(song.title)
    query_key = _normalize_key(song.query)
    for track in db.scalars(select(LocalTrack)).all():
        db_key = _normalize_key(f"{track.title} - {track.artist}")
        db_title_key = _normalize_key(track.title)
        if title_key and (title_key == db_title_key or title_key in db_key):
            return Path(track.file_path)
        if query_key and query_key == db_key:
            return Path(track.file_path)
    for root in (settings.music_dir, settings.inbox_dir):
        match = _find_existing_file(root, title_key, query_key)
        if match:
            return match
    return None


def _create_music_client(output_dir: Path):
    _ensure_musicdl_typing_compat()
    from musicdl.musicdl import MusicClient

    sources = [MUSICDL_SOURCE_MAP[p] for p in PLATFORMS if p in MUSICDL_SOURCE_MAP]
    search_size = 5 if QUALITY == "hi" else 3
    init_cfg = {
        source: {
            "work_dir": str(output_dir),
            "search_size_per_source": search_size,
            "search_size_per_page": search_size,
            "disable_print": True,
            "max_retries": 3,
        }
        for source in sources
    }
    return MusicClient(
        music_sources=sources,
        init_music_clients_cfg=init_cfg,
        clients_threadings={source: 1 for source in sources},
        requests_overrides={source: {"timeout": 20} for source in sources},
    )


def _select_best_song(search_results: dict, keyword: str):
    preferred_sources = [MUSICDL_SOURCE_MAP[p] for p in PLATFORMS if p in MUSICDL_SOURCE_MAP]
    candidates = []
    for source in preferred_sources:
        candidates.extend(search_results.get(source, []) or [])
    valid = [item for item in candidates if getattr(item, "with_valid_download_url", False)]
    if not valid:
        return None
    keyword_key = _normalize_key(keyword)
    exact = [item for item in valid if _normalize_key(getattr(item, "song_name", "")) == keyword_key]
    return max(exact or valid, key=lambda item: int(getattr(item, "file_size_bytes", None) or 0))


def _find_existing_file(root: Path, title_key: str, query_key: str) -> Path | None:
    if not root.exists():
        return None
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        name_key = _normalize_key(path.stem)
        if title_key and (title_key == name_key or title_key in name_key or name_key in title_key):
            return path
        if query_key and (query_key == name_key or query_key in name_key or name_key in query_key):
            return path
    return None


def _extract_artist(track: dict) -> str:
    artists = track.get("artists") or track.get("ar") or []
    if isinstance(artists, list) and artists:
        names = [str(item.get("name") or "").strip() for item in artists if isinstance(item, dict)]
        names = [name for name in names if name]
        if names:
            return "/".join(names[:3])
    artist = track.get("artist") or track.get("singer")
    if isinstance(artist, dict):
        return str(artist.get("name") or "Unknown")
    if artist:
        return str(artist)
    return "Unknown"


def _clean_title(value: str) -> str:
    value = re.sub(r"^\d+\.\s*", "", value.strip())
    value = re.sub(r"\(.*?\)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\uff08.*?\uff09", "", value)
    value = re.sub(r"(\u4f34\u594f|\u4f34\u594f\u7248|\u539f\u5531|\u7ffb\u5531|Remix)", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def _normalize_key(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(value or "").casefold())
