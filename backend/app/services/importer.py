from pathlib import Path
from shutil import copy2
import wave

from mutagen import File
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import LocalTrack
from app.services.cache import bump_library_version
from app.services.lyrics import find_sidecar_lyrics, sync_track_lyrics_from_path


AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav"}


def _safe_part(value: str) -> str:
    cleaned = "".join(ch for ch in value.strip() if ch not in '<>:"/\\|?*')
    return cleaned or "未知"


def _first_tag(tags: object, keys: list[str], default: str) -> str:
    if not tags:
        return default
    for key in keys:
        value = tags.get(key) if hasattr(tags, "get") else None
        if isinstance(value, list) and value:
            return str(value[0])
        if value:
            return str(value)
    return default


def _audio_metadata(src: Path) -> tuple[str, str, str, int]:
    audio = File(src, easy=True)
    title = _first_tag(audio.tags if audio else None, ["title", "TITLE"], src.stem)
    artist = _first_tag(audio.tags if audio else None, ["artist", "ARTIST"], "未知歌手")
    album = _first_tag(audio.tags if audio else None, ["album", "ALBUM"], "未知专辑")
    duration = int(audio.info.length) if audio and audio.info and audio.info.length else 0
    if duration <= 0:
        try:
            fallback = File(src)
        except Exception:
            fallback = None
        duration = int(fallback.info.length) if fallback and fallback.info and fallback.info.length else 0
    if duration <= 0 and src.suffix.lower() == ".wav":
        try:
            with wave.open(str(src), "rb") as wav:
                frames = wav.getnframes()
                rate = wav.getframerate()
                duration = int(frames / rate) if rate else 0
        except Exception:
            duration = 0
    return title, artist, album, duration


def _target_path(settings: Settings, title: str, artist: str, album: str, suffix: str) -> Path:
    return settings.music_dir / _safe_part(artist) / _safe_part(album) / f"{_safe_part(title)}{suffix.lower()}"


def _remove_empty_parents(path: Path, stop_at: Path) -> None:
    current = path.parent
    stop = stop_at.resolve()
    while current.exists() and current.resolve() != stop:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _cleanup_import_source(src: Path, sidecar: Path | None, inbox_dir: Path) -> None:
    inbox_root = inbox_dir.resolve()
    for path in (sidecar, src):
        if not path:
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(inbox_root)
        except ValueError:
            continue
        if resolved.exists() and resolved.is_file():
            resolved.unlink()
            _remove_empty_parents(resolved, inbox_root)


def _sync_imported_lyrics(settings: Settings, db: Session, track_id: int, src: Path, target: Path, duration: int) -> Path | None:
    sidecar = find_sidecar_lyrics(src)
    lyrics_audio_path = src
    force = False
    if sidecar:
        target_lrc = target.with_suffix(".lrc")
        target_lrc.parent.mkdir(parents=True, exist_ok=True)
        if sidecar.resolve() != target_lrc.resolve():
            copy2(sidecar, target_lrc)
        lyrics_audio_path = target
        force = True
    sync_track_lyrics_from_path(settings, db, track_id, lyrics_audio_path, duration, force=force)
    return sidecar


def scan_inbox(settings: Settings, db: Session) -> dict[str, int]:
    inbox_dir = settings.inbox_dir
    inbox_dir.mkdir(parents=True, exist_ok=True)
    files = [path for path in inbox_dir.rglob("*") if path.is_file()]
    audio_files = [path for path in files if path.suffix.lower() in AUDIO_EXTENSIONS]
    existing_audio = 0
    pending_audio = 0
    for src in audio_files:
        title, artist, album, _ = _audio_metadata(src)
        target = _target_path(settings, title, artist, album, src.suffix)
        target_resolved = str(target.resolve())
        db_record = db.scalar(select(LocalTrack).where(LocalTrack.file_path == target_resolved))
        if target.exists() or db_record:
            existing_audio += 1
        else:
            pending_audio += 1
    return {
        "total_files": len(files),
        "audio_files": len(audio_files),
        "pending_audio": pending_audio,
        "existing_audio": existing_audio,
        "non_audio_files": len(files) - len(audio_files),
    }


def import_inbox(settings: Settings, db: Session) -> tuple[int, int]:
    inbox_dir = settings.inbox_dir
    music_dir = settings.music_dir
    inbox_dir.mkdir(parents=True, exist_ok=True)
    music_dir.mkdir(parents=True, exist_ok=True)

    imported = 0
    skipped = 0
    repaired = 0

    for src in list(inbox_dir.rglob("*")):
        if not src.is_file() or src.suffix.lower() not in AUDIO_EXTENSIONS:
            continue

        title, artist, album, duration = _audio_metadata(src)
        target = _target_path(settings, title, artist, album, src.suffix)
        target_dir = target.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        target_resolved = str(target.resolve())
        existing_record = db.scalar(select(LocalTrack).where(LocalTrack.file_path == target_resolved))
        if target.exists():
            if existing_record:
                sidecar = _sync_imported_lyrics(settings, db, existing_record.id, src, target, duration)
                _cleanup_import_source(src, sidecar, inbox_dir)
                skipped += 1
                continue
            existing_record = LocalTrack(
                title=title,
                artist=artist,
                album=album,
                duration_seconds=duration,
                file_path=target_resolved,
            )
            db.add(existing_record)
            db.flush()
            sidecar = _sync_imported_lyrics(settings, db, existing_record.id, src, target, duration)
            _cleanup_import_source(src, sidecar, inbox_dir)
            imported += 1
            continue

        copy2(src, target)
        record = LocalTrack(
            title=title,
            artist=artist,
            album=album,
            duration_seconds=duration,
            file_path=target_resolved,
        )
        db.add(record)
        db.flush()
        sidecar = _sync_imported_lyrics(settings, db, record.id, src, target, duration)
        _cleanup_import_source(src, sidecar, inbox_dir)
        imported += 1

    db.commit()

    # Repair records if files were copied in a previous interrupted run.
    for record in db.scalars(select(LocalTrack)).all():
        if not Path(record.file_path).exists():
            db.delete(record)
            repaired += 1
    db.commit()
    if imported or skipped or repaired:
        bump_library_version(settings)
    return imported, skipped


def import_audio_file(
    settings: Settings,
    db: Session,
    src: Path,
    *,
    title: str,
    artist: str = "AI",
    album: str = "AI Generated",
    remove_source: bool = False,
) -> LocalTrack:
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(str(src))
    if src.suffix.lower() not in AUDIO_EXTENSIONS:
        raise ValueError(f"Unsupported audio type: {src.suffix}")

    _, _, _, duration = _audio_metadata(src)
    target = _target_path(settings, title, artist, album, src.suffix)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        stem = target.stem
        for index in range(2, 10000):
            candidate = target.with_name(f"{stem}-{index}{target.suffix}")
            if not candidate.exists():
                target = candidate
                break

    copy2(src, target)
    if remove_source:
        try:
            src.unlink()
        except OSError:
            pass

    target_resolved = str(target.resolve())
    record = db.scalar(select(LocalTrack).where(LocalTrack.file_path == target_resolved))
    if not record:
        record = LocalTrack(
            title=title,
            artist=artist,
            album=album,
            duration_seconds=duration,
            file_path=target_resolved,
        )
        db.add(record)
        db.flush()
    db.commit()
    bump_library_version(settings)
    return record
