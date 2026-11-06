from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.config import Settings, get_settings
from app.services.apk_versions import latest_published

router = APIRouter(prefix="/api/app", tags=["应用更新"])


@router.get("/latest", summary="获取最新 APK 版本")
def latest_app(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    version = latest_published(settings)
    if not version:
        return {"hasUpdate": False}
    download_base = _external_base_url(request, settings)
    return {
        "hasUpdate": True,
        "versionName": version["version_name"],
        "versionCode": version["version_code"],
        "releaseNotes": version.get("release_notes", ""),
        "size": version.get("size", 0),
        "downloadUrl": f"{download_base}/api/app/download/{version['id']}",
    }


@router.get("/download/{version_id}", summary="下载 APK")
def download_app(version_id: str, settings: Settings = Depends(get_settings)) -> FileResponse:
    version = latest_published(settings)
    if not version or version.get("id") != version_id:
        raise HTTPException(status_code=404, detail="版本不存在或未发布")
    path = settings.apk_dir / Path(version["file_name"]).name
    if not path.exists():
        raise HTTPException(status_code=404, detail="APK 文件不存在")
    return FileResponse(path, media_type="application/vnd.android.package-archive", filename=path.name)


def _external_base_url(request: Request, settings: Settings) -> str:
    if settings.public_app_base_url:
        return settings.public_app_base_url.rstrip("/")
    forwarded_proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    forwarded_prefix = request.headers.get("x-forwarded-prefix", "").strip().rstrip("/")
    if forwarded_prefix:
        return f"{forwarded_proto}://{forwarded_host}{forwarded_prefix}"
    return str(request.base_url).rstrip("/")
