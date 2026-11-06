from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from shutil import copyfileobj

from fastapi import UploadFile

from app.config import Settings


@dataclass
class ApkVersion:
    id: str
    version_name: str
    version_code: int
    status: str
    file_name: str
    size: int
    release_notes: str
    created_at: int
    published_at: int | None = None


def _read(settings: Settings) -> list[dict]:
    path = settings.apk_versions_file
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    versions: list[dict] = []
    for item in data:
        if isinstance(item, dict):
            versions.append(item)
        elif isinstance(item, str):
            try:
                parsed = json.loads(item)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                versions.append(parsed)
    return versions


def _write(settings: Settings, versions: list[dict]) -> None:
    settings.apk_versions_file.parent.mkdir(parents=True, exist_ok=True)
    settings.apk_versions_file.write_text(json.dumps(versions, ensure_ascii=False, indent=2), encoding="utf-8")


def list_versions(settings: Settings) -> list[dict]:
    return sorted(_read(settings), key=lambda item: item.get("created_at", 0), reverse=True)


def latest_published(settings: Settings) -> dict | None:
    versions = [item for item in _read(settings) if item.get("status") == "published"]
    if not versions:
        return None
    return max(versions, key=lambda item: int(item.get("version_code") or 0))


def save_upload(
    file: UploadFile,
    version_name: str,
    version_code: int,
    status: str,
    release_notes: str,
    settings: Settings,
) -> dict:
    if not (file.filename or "").lower().endswith(".apk"):
        raise ValueError("请上传 APK 文件")
    settings.apk_dir.mkdir(parents=True, exist_ok=True)
    version_id = uuid.uuid4().hex[:12]
    safe_version = "".join(ch for ch in version_name if ch.isalnum() or ch in ".-_") or str(version_code)
    file_name = f"music-{safe_version}-{version_id}.apk"
    target = settings.apk_dir / file_name
    with target.open("wb") as out:
        copyfileobj(file.file, out)
    item = {
        "id": version_id,
        "version_name": version_name,
        "version_code": version_code,
        "status": "draft",
        "file_name": file_name,
        "size": target.stat().st_size,
        "release_notes": release_notes.strip(),
        "created_at": int(time.time()),
        "published_at": None,
    }
    versions = _read(settings)
    if status == "published":
        for version in versions:
            if version.get("status") == "published":
                version["status"] = "archived"
        item["status"] = "published"
        item["published_at"] = int(time.time())
    versions.append(item)
    _write(settings, versions)
    return item


def publish(version_id: str, settings: Settings) -> bool:
    versions = _read(settings)
    found = False
    for version in versions:
        if version.get("id") == version_id:
            version["status"] = "published"
            version["published_at"] = int(time.time())
            found = True
        elif version.get("status") == "published":
            version["status"] = "archived"
    if found:
        _write(settings, versions)
    return found


def delete(version_id: str, settings: Settings) -> bool:
    versions = _read(settings)
    kept = []
    removed: dict | None = None
    for version in versions:
        if version.get("id") == version_id:
            removed = version
        else:
            kept.append(version)
    if not removed:
        return False
    file_name = removed.get("file_name")
    if file_name:
        path = settings.apk_dir / Path(file_name).name
        if path.exists():
            path.unlink()
    _write(settings, kept)
    return True
