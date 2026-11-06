from __future__ import annotations

import json
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
import random
from shutil import copyfileobj
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import LicenseCode, LocalTrack, TrackLyrics, TrendingTrack
from app.security import login_response, logout_response, require_admin
from app.services import apk_versions
from app.services.legal_downloader import download_configured_sources, enqueue_playlist_download, enqueue_txt_download, get_download_status, load_config, save_config, sources_to_text
from app.services.importer import AUDIO_EXTENSIONS, import_inbox, scan_inbox
from app.services.lastfm import LastFmService
from app.services.lyrics import delete_track_lyrics, ensure_track_lyrics, enqueue_lyrics_backfill, get_lyrics_backfill_status
from app.services.musicdl_trending import NETEASE_PLAYLISTS
from app.services.navidrome import trigger_scan

router = APIRouter(tags=["管理后台"])


def _layout(title: str, body: str, active: str = "") -> HTMLResponse:
    nav_items = [
        ("首页", "/admin"),
        ("上传歌曲", "/admin/upload"),
        ("本地曲库", "/admin/library"),
        ("热门榜单", "/admin/trending"),
        ("歌词管理", "/admin/lyrics"),
        ("APK版本", "/admin/apk"),
        ("下载任务", "/admin/downloads"),
        ("序列号", "/admin/licenses"),
        ("用户地图", "/admin/users-map"),
        ("Navidrome", "/admin/navidrome"),
    ]
    nav = "".join(
        f'<a class="{"active" if label == active else ""}" href="{href}">{label}</a>'
        for label, href in nav_items
    )
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} - 自建音乐库</title>
  <style>
    :root {{ --bg:#f6f7f9; --panel:#fff; --text:#1f2937; --muted:#6b7280; --line:#d8dee8; --primary:#2563eb; --ok:#16a34a; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; }}
    header {{ height:56px; display:flex; align-items:center; justify-content:space-between; padding:0 22px; background:#111827; color:white; }}
    header a {{ color:#dbeafe; text-decoration:none; }}
    .shell {{ display:grid; grid-template-columns:180px 1fr; min-height:calc(100vh - 56px); }}
    nav {{ background:#fff; border-right:1px solid var(--line); padding:14px; }}
    nav a {{ display:block; color:var(--text); text-decoration:none; padding:10px 12px; border-radius:6px; margin-bottom:4px; }}
    nav a.active, nav a:hover {{ background:#e8f0ff; color:#174ea6; }}
    main {{ padding:22px; max-width:1180px; width:100%; }}
    h1 {{ font-size:24px; margin:0 0 16px; }}
    h2 {{ font-size:18px; margin:0 0 10px; }}
    .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; margin-bottom:16px; }}
    .grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-bottom:16px; }}
    .metric {{ background:#fff; border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .metric strong {{ display:block; font-size:26px; }}
    label {{ display:block; margin:10px 0 6px; color:#374151; }}
    input, button {{ font:inherit; }}
    input[type=text], input[type=password], input[type=file], input[type=number] {{ width:100%; padding:10px; border:1px solid var(--line); border-radius:6px; background:white; }}
    button, .button {{ display:inline-flex; align-items:center; border:0; background:var(--primary); color:white; padding:9px 14px; border-radius:6px; text-decoration:none; cursor:pointer; margin-right:8px; }}
    button.secondary {{ background:#374151; }}
    button.danger, .button.danger {{ background:#dc2626; }}
    button:disabled {{ opacity:.55; cursor:not-allowed; }}
    .toolbar {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
    table {{ width:100%; border-collapse:collapse; background:white; border:1px solid var(--line); }}
    th, td {{ padding:10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    th {{ background:#f3f4f6; }}
    .path {{ max-width:360px; word-break:break-all; }}
    .checkbox-cell {{ width:44px; text-align:center; }}
    .muted {{ color:var(--muted); }}
    .notice {{ padding:10px 12px; border-radius:6px; background:#ecfdf5; color:#065f46; margin-bottom:14px; }}
    .warn {{ background:#fff7ed; color:#9a3412; }}
    .progress {{ height:14px; background:#e5e7eb; border-radius:999px; overflow:hidden; margin:12px 0 8px; }}
    .progress > div {{ width:0; height:100%; background:linear-gradient(90deg,#2563eb,#16a34a); transition:width .2s ease; }}
    .upload-list {{ margin:10px 0 0; padding-left:18px; max-height:240px; overflow:auto; }}
    .upload-list li {{ margin:4px 0; }}
    .ok {{ color:var(--ok); }}
    .bad {{ color:#dc2626; }}
    .tag {{ display:inline-block; padding:3px 8px; border-radius:999px; background:#eef2ff; color:#3730a3; }}
    .tag.published {{ background:#dcfce7; color:#166534; }}
    .tag.draft {{ background:#fef3c7; color:#92400e; }}
    .tag.archived {{ background:#f3f4f6; color:#4b5563; }}
    @media (max-width: 760px) {{ .shell {{ grid-template-columns:1fr; }} nav {{ border-right:0; border-bottom:1px solid var(--line); }} .grid {{ grid-template-columns:1fr 1fr; }} }}
  </style>
</head>
<body>
  <header><strong>自建音乐库管理后台</strong><a href="/logout">退出登录</a></header>
  <div class="shell"><nav>{nav}</nav><main>{body}</main></div>
</body>
</html>"""
    return HTMLResponse(html)


def _message_html(message: str | None, ok: bool = True) -> str:
    if not message:
        return ""
    return f'<div class="notice {"warn" if not ok else ""}">{escape(message)}</div>'


def _redirect(path: str, message: str | None = None) -> RedirectResponse:
    if message:
        path = f"{path}?message={quote(message)}"
    return RedirectResponse(path, status_code=303)


def _format_duration(seconds: int | None) -> str:
    total = max(0, int(seconds or 0))
    minutes, sec = divmod(total, 60)
    return f"{minutes}:{sec:02d}"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _remove_empty_parents(path: Path, stop_at: Path) -> None:
    current = path.parent
    stop = stop_at.resolve()
    while current.exists() and current.resolve() != stop:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _delete_local_tracks(track_ids: list[int], db: Session, settings: Settings) -> tuple[int, int]:
    deleted = 0
    missing = 0
    music_root = settings.music_dir.resolve()
    tracks = db.scalars(select(LocalTrack).where(LocalTrack.id.in_(track_ids))).all() if track_ids else []
    for track in tracks:
        resolved = Path(track.file_path).resolve()
        if _is_relative_to(resolved, music_root):
            if resolved.exists() and resolved.is_file():
                resolved.unlink()
                deleted += 1
                _remove_empty_parents(resolved, music_root)
            else:
                missing += 1
        else:
            missing += 1
        db.delete(track)
    db.commit()
    return deleted, missing


def _clear_inbox_files(settings: Settings) -> int:
    removed = 0
    inbox_root = settings.inbox_dir.resolve()
    if not inbox_root.exists():
        return removed
    for path in sorted(inbox_root.rglob("*"), reverse=True):
        if path.is_file() and _is_relative_to(path, inbox_root):
            path.unlink()
            removed += 1
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    return removed


def _track_has_lyrics(record: TrackLyrics | None) -> bool:
    if not record:
        return False
    try:
        lines = json.loads(record.lines_json or "[]")
    except json.JSONDecodeError:
        return False
    return isinstance(lines, list) and bool(lines)


def _generate_license_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(alphabet) for _ in range(6))


def _online_cutoff() -> datetime:
    return datetime.utcnow() - timedelta(minutes=3)


def _is_online(license_code: LicenseCode) -> bool:
    return bool(license_code.last_seen_at and license_code.last_seen_at >= _online_cutoff())


def _save_upload(file: UploadFile, settings: Settings) -> tuple[bool, str]:
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in AUDIO_EXTENSIONS:
        return False, "不是支持的音频格式"
    safe_name = Path(file.filename or f"upload{suffix}").name
    target = settings.inbox_dir / safe_name
    counter = 1
    while target.exists():
        target = settings.inbox_dir / f"{Path(safe_name).stem}-{counter}{suffix}"
        counter += 1
    with target.open("wb") as out:
        copyfileobj(file.file, out)
    return True, target.name


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/admin")


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(message: str | None = None) -> HTMLResponse:
    notice = _message_html(message, ok=False)
    return HTMLResponse(f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 - 自建音乐库</title>
<style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f6f7f9;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;color:#1f2937}}
.box{{width:min(420px,calc(100vw - 32px));background:white;border:1px solid #d8dee8;border-radius:8px;padding:24px}}
h1{{margin:0 0 18px;font-size:24px}}label{{display:block;margin:12px 0 6px}}input{{width:100%;padding:10px;border:1px solid #d8dee8;border-radius:6px}}button{{width:100%;margin-top:18px;border:0;border-radius:6px;padding:10px;background:#2563eb;color:white;font:inherit;cursor:pointer}}.notice{{padding:10px;background:#fff7ed;color:#9a3412;border-radius:6px;margin-bottom:14px}}
</style></head><body><form class="box" method="post" action="/login"><h1>登录管理后台</h1>{notice}<label>用户名</label><input name="username" autocomplete="username" required><label>密码</label><input name="password" type="password" autocomplete="current-password" required><button type="submit">登录</button></form></body></html>""")


@router.post("/login", include_in_schema=False)
def do_login(
    username: str = Form(...),
    password: str = Form(...),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    if username == settings.admin_username and password == settings.admin_password:
        return login_response(username, settings)
    return _redirect("/login", "用户名或密码错误")


@router.get("/logout", include_in_schema=False)
def logout() -> RedirectResponse:
    return logout_response()


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_admin)])
def dashboard(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> HTMLResponse:
    local_count = db.scalar(select(func.count(LocalTrack.id))) or 0
    trending_count = db.scalar(select(func.count(TrendingTrack.id))) or 0
    inbox = scan_inbox(settings, db)
    music_count = sum(1 for p in settings.music_dir.rglob("*") if p.is_file()) if settings.music_dir.exists() else 0
    body = f"""<h1>后台首页</h1>
<div class="grid">
  <div class="metric"><span class="muted">本地曲库</span><strong>{local_count}</strong></div>
  <div class="metric"><span class="muted">热门榜单</span><strong>{trending_count}</strong></div>
  <div class="metric"><span class="muted">未导入音频</span><strong>{inbox["pending_audio"]}</strong></div>
  <div class="metric"><span class="muted">音乐目录文件</span><strong>{music_count}</strong></div>
</div>
<div class="panel"><h2>常用操作</h2>
  <p><a class="button" href="/admin/upload">上传歌曲</a><a class="button" href="/admin/library">查看本地曲库</a><a class="button" href="/admin/trending">同步热门榜单</a></p>
  <p class="muted">inbox 当前共 {inbox["total_files"]} 个文件：可识别音频 {inbox["audio_files"]} 个，未导入 {inbox["pending_audio"]} 个，已存在 {inbox["existing_audio"]} 个，辅助文件 {inbox["non_audio_files"]} 个。</p>
</div>"""
    return _layout("后台首页", body, "首页")


@router.get("/admin/upload", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_admin)])
def upload_page(message: str | None = None) -> HTMLResponse:
    body = """<h1>上传歌曲</h1>""" + _message_html(message) + """
<div class="panel">
  <form id="uploadForm" method="post" action="/admin/upload" enctype="multipart/form-data">
    <label>选择音频文件</label>
    <input id="files" type="file" name="files" accept="audio/*,.mp3,.flac,.m4a,.ogg,.opus,.wav" multiple required>
    <label><input id="importNow" type="checkbox" name="import_now" value="yes" checked> 上传完成后立即导入本地曲库</label>
    <p><button id="uploadBtn" type="submit">上传并处理</button></p>
  </form>
  <div id="progressBox" style="display:none">
    <div class="progress"><div id="progressBar"></div></div>
    <div id="progressText" class="muted">等待上传</div>
    <ol id="uploadList" class="upload-list"></ol>
  </div>
  <p class="muted">支持 mp3、flac、m4a、ogg、opus、wav。文件先保存到 data/inbox，再导入到 data/music。</p>
</div>
<script>
const form = document.getElementById('uploadForm');
const fileInput = document.getElementById('files');
const importNow = document.getElementById('importNow');
const button = document.getElementById('uploadBtn');
const box = document.getElementById('progressBox');
const bar = document.getElementById('progressBar');
const text = document.getElementById('progressText');
const list = document.getElementById('uploadList');

function setProgress(done, total, current) {
  const pct = total ? Math.round(done * 100 / total) : 0;
  bar.style.width = pct + '%';
  text.textContent = `共 ${total} 个文件，已处理 ${done} 个，当前：${current || '等待中'}，进度 ${pct}%`;
}

function addLine(name, ok, message) {
  const li = document.createElement('li');
  li.className = ok ? 'ok' : 'bad';
  li.textContent = `${ok ? '成功' : '失败'}：${name} - ${message}`;
  list.appendChild(li);
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const files = Array.from(fileInput.files || []);
  if (!files.length) return;
  button.disabled = true;
  box.style.display = 'block';
  list.innerHTML = '';
  setProgress(0, files.length, files[0].name);

  let done = 0;
  for (const file of files) {
    const data = new FormData();
    data.append('file', file);
    try {
      const response = await fetch('/admin/upload-one', { method: 'POST', body: data });
      const result = await response.json();
      done += 1;
      addLine(file.name, result.ok, result.message);
    } catch (error) {
      done += 1;
      addLine(file.name, false, String(error));
    }
    setProgress(done, files.length, files[done] ? files[done].name : '处理完成');
  }

  if (importNow.checked) {
    text.textContent = '上传完成，正在导入本地曲库...';
    const response = await fetch('/admin/library/import-json', { method: 'POST' });
    const result = await response.json();
    addLine('导入本地曲库', result.ok, result.message);
  }

  text.textContent = `全部完成：共 ${files.length} 个文件，已处理 ${done} 个。`;
  button.disabled = false;
});
</script>"""
    return _layout("上传歌曲", body, "上传歌曲")


@router.post("/admin/upload-one", include_in_schema=False, dependencies=[Depends(require_admin)])
async def upload_one(file: UploadFile = File(...), settings: Settings = Depends(get_settings)) -> JSONResponse:
    ok, message = _save_upload(file, settings)
    return JSONResponse({"ok": ok, "message": message, "filename": file.filename})


@router.post("/admin/upload", include_in_schema=False, dependencies=[Depends(require_admin)])
async def upload_files(
    files: list[UploadFile] = File(...),
    import_now: str | None = Form(None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    saved = 0
    skipped = 0
    for file in files:
        ok, _ = _save_upload(file, settings)
        if ok:
            saved += 1
        else:
            skipped += 1
    message = f"已上传 {saved} 个文件，跳过 {skipped} 个非音频文件。"
    if import_now == "yes":
        imported, import_skipped = import_inbox(settings, db)
        message += f" 已导入 {imported} 首，跳过 {import_skipped} 首。"
    return _redirect("/admin/upload", message)


@router.get("/admin/library", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_admin)])
def library_page(message: str | None = None, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> HTMLResponse:
    tracks = db.scalars(select(LocalTrack).order_by(desc(LocalTrack.imported_at)).limit(300)).all()
    inbox = scan_inbox(settings, db)
    rows = "".join(
        f"""<tr>
  <td class="checkbox-cell"><input class="track-check" type="checkbox" name="track_ids" value="{t.id}"></td>
  <td>{escape(t.title)}</td>
  <td>{escape(t.artist)}</td>
  <td>{escape(t.album or "")}</td>
  <td>{_format_duration(t.duration_seconds)}</td>
  <td class="path">{escape(t.file_path)}</td>
  <td><form method="post" action="/admin/library/{t.id}/delete" onsubmit="return confirm('确定删除这首歌曲吗？文件也会从硬盘删除。')"><button class="danger" type="submit">删除</button></form></td>
</tr>"""
        for t in tracks
    ) or '<tr><td colspan="7" class="muted">暂无本地歌曲。</td></tr>'
    body = f"""<h1>本地曲库</h1>{_message_html(message)}
<div class="panel">
  <h2>待导入文件</h2>
  <p class="muted">inbox 当前共 <strong>{inbox["total_files"]}</strong> 个文件：可识别音频 <strong>{inbox["audio_files"]}</strong> 个，未导入 <strong>{inbox["pending_audio"]}</strong> 个，已存在 <strong>{inbox["existing_audio"]}</strong> 个，辅助文件 <strong>{inbox["non_audio_files"]}</strong> 个。</p>
  <div class="toolbar">
    <form method="post" action="/admin/library/import"><button type="submit">导入 inbox 文件</button></form>
    <form method="post" action="/admin/library/inbox/clear" onsubmit="return confirm('确定清空待导入文件吗？这只会删除 inbox 里的文件。')"><button class="secondary" type="submit">清空待导入文件</button></form>
  </div>
</div>
<form method="post" action="/admin/library/delete-selected" onsubmit="return confirmBatchDelete()">
  <div class="panel toolbar">
    <button class="danger" type="submit">批量删除选中音乐</button>
    <span class="muted">删除会同时移除数据库记录和 data/music 中的实际文件。</span>
  </div>
  <table><thead><tr><th class="checkbox-cell"><input id="checkAll" type="checkbox" title="全选"></th><th>歌曲</th><th>歌手</th><th>专辑</th><th>时长</th><th>文件路径</th><th>操作</th></tr></thead><tbody>{rows}</tbody></table>
</form>
<script>
const checkAll = document.getElementById('checkAll');
const checks = Array.from(document.querySelectorAll('.track-check'));
if (checkAll) {{
  checkAll.addEventListener('change', () => checks.forEach(item => item.checked = checkAll.checked));
}}
function confirmBatchDelete() {{
  const count = checks.filter(item => item.checked).length;
  if (!count) {{
    alert('请先勾选要删除的歌曲。');
    return false;
  }}
  return confirm(`确定删除选中的 ${{count}} 首歌曲吗？文件也会从硬盘删除。`);
}}
</script>"""
    return _layout("本地曲库", body, "本地曲库")


@router.post("/admin/library/import", include_in_schema=False, dependencies=[Depends(require_admin)])
def import_library(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> RedirectResponse:
    before = scan_inbox(settings, db)
    imported, skipped = import_inbox(settings, db)
    after = scan_inbox(settings, db)
    return _redirect(
        "/admin/library",
        f"导入完成：新增 {imported} 首，已存在跳过 {skipped} 首；"
        f"inbox 音频 {before['audio_files']} 个，当前未导入 {after['pending_audio']} 个，辅助文件 {after['non_audio_files']} 个。",
    )


@router.post("/admin/library/import-json", include_in_schema=False, dependencies=[Depends(require_admin)])
def import_library_json(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> JSONResponse:
    before = scan_inbox(settings, db)
    imported, skipped = import_inbox(settings, db)
    after = scan_inbox(settings, db)
    return JSONResponse(
        {
            "ok": True,
            "imported": imported,
            "skipped": skipped,
            "message": (
                f"导入完成：新增 {imported} 首，已存在跳过 {skipped} 首；"
                f"inbox 音频 {before['audio_files']} 个，当前未导入 {after['pending_audio']} 个，辅助文件 {after['non_audio_files']} 个。"
            ),
        }
    )


@router.post("/admin/library/{track_id}/delete", include_in_schema=False, dependencies=[Depends(require_admin)])
async def delete_local_track(track_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> RedirectResponse:
    deleted, missing = _delete_local_tracks([track_id], db, settings)
    _, scan_message = await trigger_scan(settings)
    return _redirect("/admin/library", f"已删除 {deleted} 个音乐文件，清理 {missing} 条缺失或异常记录。{scan_message}")


@router.post("/admin/library/delete-selected", include_in_schema=False, dependencies=[Depends(require_admin)])
async def delete_selected_tracks(track_ids: list[int] = Form([]), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> RedirectResponse:
    if not track_ids:
        return _redirect("/admin/library", "请先勾选要删除的歌曲。")
    deleted, missing = _delete_local_tracks(track_ids, db, settings)
    _, scan_message = await trigger_scan(settings)
    return _redirect("/admin/library", f"批量删除完成：已删除 {deleted} 个音乐文件，清理 {missing} 条缺失或异常记录。{scan_message}")


@router.post("/admin/library/inbox/clear", include_in_schema=False, dependencies=[Depends(require_admin)])
def clear_inbox_files(settings: Settings = Depends(get_settings)) -> RedirectResponse:
    removed = _clear_inbox_files(settings)
    return _redirect("/admin/library", f"已清空待导入文件 {removed} 个。")


@router.get("/admin/lyrics", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_admin)])
def lyrics_page(message: str | None = None, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> HTMLResponse:
    rows = db.execute(
        select(LocalTrack, TrackLyrics)
        .outerjoin(TrackLyrics, TrackLyrics.track_id == LocalTrack.id)
        .order_by(desc(LocalTrack.imported_at))
    ).all()
    total = len(rows)
    with_lyrics = sum(1 for _, lyrics in rows if _track_has_lyrics(lyrics))
    synced = sum(1 for _, lyrics in rows if _track_has_lyrics(lyrics) and not (lyrics.source or "").endswith("_plain"))
    missing = total - with_lyrics
    missing_rows: list[str] = []
    for track, lyrics in rows:
        if _track_has_lyrics(lyrics):
            continue
        missing_rows.append(
            f"""<tr>
  <td>{escape(track.title)}</td>
  <td>{escape(track.artist)}</td>
  <td>{escape(track.album or "")}</td>
  <td><span class="tag draft">无歌词</span></td>
  <td class="path">{escape(track.file_path)}</td>
  <td>
    <form method="post" action="/admin/lyrics/{track.id}/refresh" style="display:inline"><button type="submit">刷新</button></form>
  </td>
</tr>"""
        )
        if len(missing_rows) >= 200:
            break

    job = get_lyrics_backfill_status(settings)
    body = f"""<h1>歌词管理</h1>{_message_html(message)}
<div class="grid">
  <div class="metric"><span class="muted">总曲目</span><strong>{total}</strong></div>
  <div class="metric"><span class="muted">已有歌词</span><strong>{with_lyrics}</strong></div>
  <div class="metric"><span class="muted">同步歌词</span><strong>{synced}</strong></div>
  <div class="metric"><span class="muted">无歌词</span><strong>{missing}</strong></div>
</div>
<div class="panel">
  <h2>批量补全</h2>
  <form id="lyricsBackfillForm" method="post" action="/admin/lyrics/backfill">
    <label><input id="lyricsOnlyMissing" type="checkbox" name="only_missing" value="yes" checked> 仅补全缺失歌词</label>
    <label><input id="lyricsForce" type="checkbox" name="force" value="yes"> 重新扫描全部歌曲</label>
    <p><button id="lyricsBackfillBtn" type="submit">开始补全</button></p>
  </form>
</div>
<div class="panel">
  <h2>补全进度</h2>
  <div class="progress"><div id="lyricsProgressBar"></div></div>
  <p id="lyricsProgressText" class="muted">正在读取状态...</p>
  <table><thead><tr><th>时间</th><th>歌曲</th><th>状态</th><th>说明</th><th>路径</th></tr></thead><tbody id="lyricsStatusRows"><tr><td colspan="5" class="muted">暂无记录</td></tr></tbody></table>
</div>
<div class="panel">
  <h2>缺失歌词歌曲</h2>
  <table><thead><tr><th>歌曲</th><th>歌手</th><th>专辑</th><th>状态</th><th>路径</th><th>操作</th></tr></thead><tbody>{''.join(missing_rows) or '<tr><td colspan="6" class="muted">暂无缺失歌词</td></tr>'}</tbody></table>
</div>"""
    body += """<script>
function escapeHtml(value) {
  return String(value || '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function statusLabel(value) {
  return {success:'成功', skipped:'跳过', failed:'失败', running:'运行中', queued:'排队中', idle:'空闲'}[value] || value || '';
}
async function refreshLyricsStatus() {
  try {
    const response = await fetch('/admin/lyrics/status-json', {cache:'no-store'});
    const data = await response.json();
    const pct = data.percent || 0;
    document.getElementById('lyricsProgressBar').style.width = pct + '%';
    document.getElementById('lyricsProgressText').textContent = `${statusLabel(data.status)} | ${data.message || ''} | 进度 ${data.done || 0}/${data.total || 0} | 成功 ${data.success || 0} | 跳过 ${data.skipped || 0} | 失败 ${data.failed || 0}`;
    document.getElementById('lyricsBackfillBtn').disabled = !!data.running || !!data.queued;
    document.getElementById('lyricsForce').disabled = !!data.running || !!data.queued;
    document.getElementById('lyricsOnlyMissing').disabled = !!data.running || !!data.queued;
    const rows = (data.items || []).slice(-100).reverse().map(item => `<tr><td>${escapeHtml(item.time)}</td><td>${escapeHtml(item.name)}</td><td>${escapeHtml(statusLabel(item.status))}</td><td>${escapeHtml(item.message)}</td><td>${escapeHtml(item.path)}</td></tr>`).join('');
    document.getElementById('lyricsStatusRows').innerHTML = rows || '<tr><td colspan="5" class="muted">暂无记录</td></tr>';
  } catch (error) {
    document.getElementById('lyricsProgressText').textContent = '读取状态失败：' + error;
  }
}
document.getElementById('lyricsBackfillForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const data = new FormData();
  if (document.getElementById('lyricsOnlyMissing').checked) data.append('only_missing', 'yes');
  if (document.getElementById('lyricsForce').checked) data.append('force', 'yes');
  document.getElementById('lyricsBackfillBtn').disabled = true;
  await fetch('/admin/lyrics/backfill', {method:'POST', body:data, redirect:'manual'});
  refreshLyricsStatus();
});
refreshLyricsStatus();
setInterval(refreshLyricsStatus, 2000);
</script>"""
    return _layout("歌词管理", body, "歌词管理")


@router.post("/admin/lyrics/backfill", include_in_schema=False, dependencies=[Depends(require_admin)])
def backfill_lyrics(
    only_missing: str | None = Form(None),
    force: str | None = Form(None),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    _, message = enqueue_lyrics_backfill(settings, force=force == "yes", only_missing=only_missing == "yes")
    return _redirect("/admin/lyrics", message)


@router.get("/admin/lyrics/status-json", include_in_schema=False, dependencies=[Depends(require_admin)])
def lyrics_status_json(settings: Settings = Depends(get_settings)) -> JSONResponse:
    return JSONResponse(get_lyrics_backfill_status(settings))


@router.post("/admin/lyrics/{track_id}/refresh", include_in_schema=False, dependencies=[Depends(require_admin)])
def refresh_track_lyrics(track_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> RedirectResponse:
    track = db.get(LocalTrack, track_id)
    if not track:
        return _redirect("/admin/lyrics", "歌曲不存在。")
    if ensure_track_lyrics(settings, db, track, force=True):
        db.commit()
        return _redirect("/admin/lyrics", "已重新扫描歌词。")
    return _redirect("/admin/lyrics", "未找到可用歌词。")


@router.post("/admin/lyrics/{track_id}/delete", include_in_schema=False, dependencies=[Depends(require_admin)])
def delete_track_lyrics_route(track_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> RedirectResponse:
    if delete_track_lyrics(settings, db, track_id):
        return _redirect("/admin/lyrics", "已删除歌词。")
    return _redirect("/admin/lyrics", "歌词记录不存在。")


@router.get("/admin/trending", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_admin)])
def trending_page(message: str | None = None, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> HTMLResponse:
    tracks = db.scalars(select(TrendingTrack).order_by(desc(TrendingTrack.fetched_at), TrendingTrack.rank).limit(300)).all()
    rows = "".join(
        f'<tr><td>{t.rank}</td><td>{escape(t.title)}</td><td>{escape(t.artist)}</td><td>{t.listeners}</td><td><a href="{escape(t.source_url or "#")}" target="_blank">来源</a></td></tr>'
        for t in tracks
    ) or '<tr><td colspan="5" class="muted">暂无热门榜单元数据。</td></tr>'
    options = "".join(
        f'<option value="{escape(key)}">{escape(item["name"])}</option>'
        for key, item in NETEASE_PLAYLISTS.items()
    )
    body = f"""<h1>热门榜单</h1>{_message_html(message)}
<div class="panel">
  <h2>网易云榜单下载</h2>
  <form id="playlistDownloadForm">
    <label>下载榜单</label><select id="playlistSelect" name="playlist" style="width:100%;padding:10px;border:1px solid #d8dee8;border-radius:6px">{options}</select>
    <label>下载数量</label><input id="playlistLimit" type="number" name="limit" value="20" min="1" max="100">
    <label><input id="playlistSkip" type="checkbox" name="skip_existing" value="yes" checked> 不下载重复歌曲</label>
    <label><input id="playlistImport" type="checkbox" name="import_after" value="yes" checked> 下载后导入本地曲库</label>
    <p><button id="playlistDownloadBtn" type="submit">下载到 inbox</button></p>
  </form>
</div>
<div class="panel">
  <h2>待下载歌曲</h2>
  <p id="playlistPreviewText" class="muted">正在读取榜单...</p>
  <table><thead><tr><th>排名</th><th>歌曲</th><th>歌手</th></tr></thead><tbody id="playlistPreviewRows"><tr><td colspan="3" class="muted">正在读取榜单...</td></tr></tbody></table>
</div>
<div class="panel">
  <h2>下载进度</h2>
  <div class="progress"><div id="downloadProgressBar"></div></div>
  <p id="downloadProgressText" class="muted">正在读取状态...</p>
  <table><thead><tr><th>时间</th><th>歌曲</th><th>状态</th><th>说明</th><th>保存位置</th></tr></thead><tbody id="downloadStatusRows"><tr><td colspan="5" class="muted">暂无记录</td></tr></tbody></table>
</div>
<div class="panel"><form method="post" action="/admin/trending/sync"><h2>热门元数据同步</h2><label>同步数量</label><input type="number" name="limit" value="50" min="1" max="200"><p><button type="submit">一键同步热门元数据</button></p></form></div>
<table><thead><tr><th>排名</th><th>歌曲</th><th>歌手</th><th>听众数</th><th>链接</th></tr></thead><tbody>{rows}</tbody></table>
<script>
function escapeHtml(value) {{
  return String(value || '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
}}
function statusLabel(value) {{
  return {{success:'成功', skipped:'跳过', failed:'失败', running:'运行中', queued:'排队中', idle:'空闲'}}[value] || value || '';
}}
const playlistSelect = document.getElementById('playlistSelect');
const playlistLimit = document.getElementById('playlistLimit');
const playlistSkip = document.getElementById('playlistSkip');
const playlistImport = document.getElementById('playlistImport');
const previewRows = document.getElementById('playlistPreviewRows');
const previewText = document.getElementById('playlistPreviewText');
const downloadButton = document.getElementById('playlistDownloadBtn');
let previewTimer = null;

async function refreshPreview() {{
  const params = new URLSearchParams({{playlist: playlistSelect.value, limit: playlistLimit.value || '20', skip_existing: playlistSkip.checked ? 'true' : 'false'}});
  previewText.textContent = '正在读取榜单...';
  previewRows.innerHTML = '<tr><td colspan="3" class="muted">正在读取榜单...</td></tr>';
  try {{
    const response = await fetch('/api/trending/netease/preview?' + params.toString(), {{cache:'no-store'}});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || response.status);
    previewText.textContent = `${{data.playlist_name || ''}}：共 ${{data.total || 0}} 首，已过滤 ${{data.skipped_existing || 0}} 首，当前显示 ${{(data.songs || []).length}} 首。`;
    previewRows.innerHTML = (data.songs || []).map(song => `<tr><td>${{song.rank}}</td><td>${{escapeHtml(song.title)}}</td><td>${{escapeHtml(song.artist)}}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">没有可下载歌曲。</td></tr>';
  }} catch (error) {{
    previewText.textContent = '读取榜单失败：' + error;
    previewRows.innerHTML = '<tr><td colspan="3" class="muted">读取失败</td></tr>';
  }}
}}
function queuePreview() {{
  clearTimeout(previewTimer);
  previewTimer = setTimeout(refreshPreview, 300);
}}
playlistSelect.addEventListener('change', refreshPreview);
playlistLimit.addEventListener('input', queuePreview);
playlistSkip.addEventListener('change', refreshPreview);

async function refreshDownloadStatus() {{
  try {{
    const response = await fetch('/admin/downloads/status-json', {{cache:'no-store'}});
    const data = await response.json();
    const pct = data.percent || 0;
    document.getElementById('downloadProgressBar').style.width = pct + '%';
    document.getElementById('downloadProgressText').textContent = `${{statusLabel(data.status)}} | ${{data.message || ''}} | 进度 ${{data.done || 0}}/${{data.total || 0}} | 成功 ${{data.success || 0}} | 跳过 ${{data.skipped || 0}} | 失败 ${{data.failed || 0}}`;
    downloadButton.disabled = !!data.running || !!data.queued;
    const rows = (data.items || []).slice(-100).reverse().map(item => `<tr><td>${{escapeHtml(item.time)}}</td><td>${{escapeHtml(item.name)}}</td><td>${{escapeHtml(statusLabel(item.status))}}</td><td>${{escapeHtml(item.message)}}</td><td>${{escapeHtml(item.path)}}</td></tr>`).join('');
    document.getElementById('downloadStatusRows').innerHTML = rows || '<tr><td colspan="5" class="muted">暂无记录</td></tr>';
  }} catch (error) {{
    document.getElementById('downloadProgressText').textContent = '读取状态失败：' + error;
  }}
}}
document.getElementById('playlistDownloadForm').addEventListener('submit', async (event) => {{
  event.preventDefault();
  const data = new FormData();
  data.append('playlist', playlistSelect.value);
  data.append('limit', playlistLimit.value || '20');
  if (playlistSkip.checked) data.append('skip_existing', 'yes');
  if (playlistImport.checked) data.append('import_after', 'yes');
  downloadButton.disabled = true;
  const response = await fetch('/admin/trending/netease/download', {{method:'POST', body:data, redirect:'manual'}});
  if (!response.ok && response.status !== 303) {{
    document.getElementById('downloadProgressText').textContent = '启动下载失败：HTTP ' + response.status;
  }}
  refreshDownloadStatus();
}});
refreshPreview();
refreshDownloadStatus();
setInterval(refreshDownloadStatus, 2000);
</script>"""
    return _layout("热门榜单", body, "热门榜单")


@router.post("/admin/trending/sync", include_in_schema=False, dependencies=[Depends(require_admin)])
async def sync_trending(limit: int = Form(50), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> RedirectResponse:
    synced = await LastFmService(settings).sync_top_tracks(db, limit=limit)
    if synced == 0:
        return _redirect("/admin/trending", "未同步到数据，请检查 Last.fm API Key 是否已配置。")
    return _redirect("/admin/trending", f"已同步 {synced} 首热门音乐元数据。")


@router.post("/admin/trending/netease/download", include_in_schema=False, dependencies=[Depends(require_admin)])
async def download_netease_from_admin(
    playlist: str = Form("hot"),
    limit: int = Form(20),
    skip_existing: str | None = Form(None),
    import_after: str | None = Form(None),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    _, message = await enqueue_playlist_download(
        settings,
        playlist,
        limit=limit,
        skip_existing=skip_existing == "yes",
        import_after=import_after == "yes",
    )
    return _redirect("/admin/trending", message)


@router.get("/admin/licenses", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_admin)])
def licenses_page(message: str | None = None, db: Session = Depends(get_db)) -> HTMLResponse:
    codes = db.scalars(select(LicenseCode).order_by(desc(LicenseCode.created_at)).limit(500)).all()
    rows = []
    for item in codes:
        status_text = "已激活" if item.status == "active" else "未使用"
        online_text = "在线" if _is_online(item) else "离线"
        rows.append(
            f"""<tr>
  <td><strong>{escape(item.code)}</strong></td>
  <td>{status_text}</td>
  <td>{online_text}</td>
  <td>{escape(item.device_id or "")}</td>
  <td>{escape(item.device_model or "")}</td>
  <td>{escape(item.app_version or "")}</td>
  <td>{item.activated_at.strftime("%Y-%m-%d %H:%M:%S") if item.activated_at else ""}</td>
  <td>{item.last_seen_at.strftime("%Y-%m-%d %H:%M:%S") if item.last_seen_at else ""}</td>
</tr>"""
        )
    body = f"""<h1>序列号管理</h1>{_message_html(message)}
<div class="panel">
  <h2>生成 6 位序列号</h2>
  <form method="post" action="/admin/licenses/generate">
    <label>生成数量</label><input type="number" name="count" value="10" min="1" max="1000">
    <p><button type="submit">生成序列号</button></p>
  </form>
  <p class="muted">序列号由 6 位大写字母和数字组成，去除了容易混淆的 0、1、I、O。每个序列号默认只绑定一台设备。</p>
</div>
<table><thead><tr><th>序列号</th><th>状态</th><th>在线</th><th>设备ID</th><th>设备型号</th><th>APP版本</th><th>激活时间</th><th>最后在线</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan="8" class="muted">暂无序列号。</td></tr>'}</tbody></table>"""
    return _layout("序列号管理", body, "序列号")


@router.post("/admin/licenses/generate", include_in_schema=False, dependencies=[Depends(require_admin)])
def generate_licenses(count: int = Form(10), db: Session = Depends(get_db)) -> RedirectResponse:
    count = max(1, min(count, 1000))
    existing = set(db.scalars(select(LicenseCode.code)).all())
    created = 0
    while created < count:
        code = _generate_license_code()
        if code in existing:
            continue
        existing.add(code)
        db.add(LicenseCode(code=code))
        created += 1
    db.commit()
    return _redirect("/admin/licenses", f"已生成 {created} 个序列号。")


@router.get("/admin/users-map", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_admin)])
def users_map_page() -> HTMLResponse:
    body = """<h1>用户在线地图</h1>
<div class="grid">
  <div class="metric"><span class="muted">总用户</span><strong id="totalUsers">0</strong></div>
  <div class="metric"><span class="muted">当前在线</span><strong id="onlineUsers">0</strong></div>
  <div class="metric"><span class="muted">已激活</span><strong id="activeUsers">0</strong></div>
  <div class="metric"><span class="muted">未使用序列号</span><strong id="unusedCodes">0</strong></div>
</div>
<div class="panel">
  <div id="map" style="height:520px;border:1px solid #d8dee8;border-radius:8px;background:#eef2ff"></div>
  <p class="muted">地图每 10 秒自动刷新。在线定义：最近 3 分钟内 APP 上报过心跳。位置由手机 GPS 授权后上报。</p>
</div>
<table><thead><tr><th>序列号</th><th>状态</th><th>在线</th><th>设备</th><th>版本</th><th>经纬度</th><th>最后在线</th></tr></thead><tbody id="userRows"><tr><td colspan="7" class="muted">正在加载...</td></tr></tbody></table>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
let map = null;
let markers = [];
function escapeHtml(value) {
  return String(value || '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function ensureMap() {
  if (map || !window.L) return;
  map = L.map('map').setView([35.8617, 104.1954], 4);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 18}).addTo(map);
}
async function refreshUsers() {
  const response = await fetch('/admin/users-map/data', {cache:'no-store'});
  const data = await response.json();
  document.getElementById('totalUsers').textContent = data.total || 0;
  document.getElementById('onlineUsers').textContent = data.online || 0;
  document.getElementById('activeUsers').textContent = data.active || 0;
  document.getElementById('unusedCodes').textContent = data.unused || 0;
  document.getElementById('userRows').innerHTML = (data.users || []).map(user => `<tr><td><strong>${escapeHtml(user.code)}</strong></td><td>${escapeHtml(user.statusText)}</td><td>${user.online ? '在线' : '离线'}</td><td>${escapeHtml(user.deviceModel || user.deviceId)}</td><td>${escapeHtml(user.appVersion)}</td><td>${user.lat && user.lng ? `${user.lat}, ${user.lng}` : ''}</td><td>${escapeHtml(user.lastSeen || '')}</td></tr>`).join('') || '<tr><td colspan="7" class="muted">暂无用户。</td></tr>';
  ensureMap();
  if (map) {
    markers.forEach(marker => marker.remove());
    markers = [];
    (data.users || []).filter(user => user.lat && user.lng).forEach(user => {
      const marker = L.marker([user.lat, user.lng]).addTo(map);
      marker.bindPopup(`<strong>${escapeHtml(user.code)}</strong><br>${user.online ? '在线' : '离线'}<br>${escapeHtml(user.deviceModel || '')}<br>${escapeHtml(user.lastSeen || '')}`);
      markers.push(marker);
    });
  }
}
refreshUsers();
setInterval(refreshUsers, 10000);
</script>"""
    return _layout("用户在线地图", body, "用户地图")


@router.get("/admin/users-map/data", include_in_schema=False, dependencies=[Depends(require_admin)])
def users_map_data(db: Session = Depends(get_db)) -> JSONResponse:
    codes = db.scalars(select(LicenseCode).order_by(desc(LicenseCode.last_seen_at), desc(LicenseCode.created_at))).all()
    users = []
    for item in codes:
        online = _is_online(item)
        users.append(
            {
                "code": item.code,
                "status": item.status,
                "statusText": "已激活" if item.status == "active" else "未使用",
                "online": online,
                "deviceId": item.device_id or "",
                "deviceModel": item.device_model or "",
                "appVersion": item.app_version or "",
                "lat": item.last_lat,
                "lng": item.last_lng,
                "lastSeen": item.last_seen_at.strftime("%Y-%m-%d %H:%M:%S") if item.last_seen_at else "",
            }
        )
    return JSONResponse(
        {
            "total": len(codes),
            "active": sum(1 for item in codes if item.status == "active"),
            "unused": sum(1 for item in codes if item.status != "active"),
            "online": sum(1 for item in codes if _is_online(item)),
            "users": users,
        }
    )


@router.get("/admin/apk", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_admin)])
def apk_page(request: Request, message: str | None = None, settings: Settings = Depends(get_settings)) -> HTMLResponse:
    versions = apk_versions.list_versions(settings)
    rows = "".join(
        f"""<tr>
  <td>{escape(item.get("version_name", ""))}</td>
  <td>{item.get("version_code", "")}</td>
  <td><span class="tag {escape(item.get("status", ""))}">{_status_text(item.get("status", ""))}</span></td>
  <td>{_format_size(int(item.get("size") or 0))}</td>
  <td>{escape(item.get("release_notes", ""))}</td>
  <td>
    <a class="button" href="/api/app/download/{escape(item.get("id", ""))}">下载</a>
    <form method="post" action="/admin/apk/{escape(item.get("id", ""))}/publish" style="display:inline"><button type="submit">发布</button></form>
    <form method="post" action="/admin/apk/{escape(item.get("id", ""))}/delete" style="display:inline"><button class="secondary" type="submit">删除</button></form>
  </td>
</tr>"""
        for item in versions
    ) or '<tr><td colspan="6" class="muted">暂无 APK 版本记录</td></tr>'
    latest = apk_versions.latest_published(settings)
    latest_download_url = _apk_download_url(request, settings, latest["id"]) if latest else ""
    latest_html = (
        f'<p>当前已发布版本：<strong>{escape(latest["version_name"])}</strong> '
        f'({latest["version_code"]}) <a href="{escape(latest_download_url, quote=True)}">下载 APK</a> '
        f'<button type="button" class="secondary" onclick="copyLatestApkLink(\'{escape(latest_download_url, quote=True)}\')">复制下载链接</button></p>'
        if latest
        else '<p class="muted">当前没有已发布的 APK 版本</p>'
    )
    body = f"""<h1>APK 版本管理</h1>{_message_html(message)}
<div class="panel">
  <h2>当前发布版本</h2>
  {latest_html}
  <p class="muted">这里只显示当前线上版本，按钮可直接复制公开下载链接。</p>
</div>
<div class="panel">
  <h2>上传新版本</h2>
  <form method="post" action="/admin/apk/upload" enctype="multipart/form-data">
    <label>版本名称</label><input type="text" name="version_name" value="0.1.1" required>
    <label>版本号</label><input type="number" name="version_code" value="2" min="1" required>
    <label>更新说明</label><input type="text" name="release_notes" value="请输入更新说明">
    <label>APK 文件</label><input type="file" name="file" accept=".apk,application/vnd.android.package-archive" required>
    <label><input type="checkbox" name="publish_now" value="yes"> 上传后立即发布</label>
    <p><button type="submit">上传 APK</button></p>
  </form>
</div>
<table><thead><tr><th>版本名称</th><th>版本号</th><th>状态</th><th>大小</th><th>更新说明</th><th>操作</th></tr></thead><tbody>{rows}</tbody></table>"""
    body += """<script>
async function copyLatestApkLink(url) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(url);
      alert('已复制下载链接');
      return;
    }
    const input = document.createElement('input');
    input.value = url;
    document.body.appendChild(input);
    input.select();
    document.execCommand('copy');
    input.remove();
    alert('已复制下载链接');
  } catch (error) {
    prompt('复制失败，请手动复制链接', url);
  }
}
</script>"""
    return _layout("APK 版本管理", body, "APK管理")


def _apk_download_url(request: Request, settings: Settings, version_id: str) -> str:
    if settings.public_app_base_url:
        return f"{settings.public_app_base_url.rstrip('/')}/api/app/download/{version_id}"
    forwarded_proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    forwarded_prefix = request.headers.get("x-forwarded-prefix", "").strip().rstrip("/")
    if forwarded_prefix:
        return f"{forwarded_proto}://{forwarded_host}{forwarded_prefix}/api/app/download/{version_id}"
    return f"{forwarded_proto}://{forwarded_host}/api/app/download/{version_id}"


@router.post("/admin/apk/upload", include_in_schema=False, dependencies=[Depends(require_admin)])
async def upload_apk(
    file: UploadFile = File(...),
    version_name: str = Form(...),
    version_code: int = Form(...),
    release_notes: str = Form(""),
    publish_now: str | None = Form(None),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    try:
        apk_versions.save_upload(
            file=file,
            version_name=version_name,
            version_code=version_code,
            status="published" if publish_now == "yes" else "draft",
            release_notes=release_notes,
            settings=settings,
        )
    except ValueError as exc:
        return _redirect("/admin/apk", str(exc))
    return _redirect("/admin/apk", "APK 已上传。")


@router.post("/admin/apk/{version_id}/publish", include_in_schema=False, dependencies=[Depends(require_admin)])
def publish_apk(version_id: str, settings: Settings = Depends(get_settings)) -> RedirectResponse:
    if not apk_versions.publish(version_id, settings):
        return _redirect("/admin/apk", "版本不存在。")
    return _redirect("/admin/apk", "版本已发布，安卓端将收到更新提示。")


@router.post("/admin/apk/{version_id}/delete", include_in_schema=False, dependencies=[Depends(require_admin)])
def delete_apk(version_id: str, settings: Settings = Depends(get_settings)) -> RedirectResponse:
    if not apk_versions.delete(version_id, settings):
        return _redirect("/admin/apk", "版本不存在。")
    return _redirect("/admin/apk", "版本已删除。")


@router.get("/admin/downloads", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_admin)])
def download_page(message: str | None = None, settings: Settings = Depends(get_settings)) -> HTMLResponse:
    config = load_config(settings)
    sources_text = sources_to_text(config)
    status = "已启用" if config["enabled"] else "未启用"
    playlist_options = "".join(
        f'<label style="display:inline-block;width:180px;margin:6px 10px 6px 0"><input type="checkbox" name="playlists" value="{escape(key)}" {"checked" if key in config.get("playlists", []) else ""}> {escape(item["name"])}</label>'
        for key, item in NETEASE_PLAYLISTS.items()
    )
    source_rows = "".join(
        f'<tr><td>{escape(item.get("url", ""))}</td><td>{escape(item.get("filename", ""))}</td></tr>'
        for item in config.get("sources", [])
    ) or '<tr><td colspan="2" class="muted">暂无直接下载 URL</td></tr>'
    body = f"""<h1>下载任务</h1>{_message_html(message)}
<div class="panel">
  <p>定时下载：<strong>{status}</strong></p>
  <form method="post" action="/admin/downloads/save">
    <label><input type="checkbox" name="enabled" value="yes" {"checked" if config["enabled"] else ""}> 启用定时下载</label>
    <label>执行时间</label>
    <div style="display:flex; gap:8px"><input type="number" name="hour" min="0" max="23" value="{config['hour']}" style="flex:1"><input type="number" name="minute" min="0" max="59" value="{config['minute']}" style="flex:1"></div>
    <label>下载来源榜单（可多选）</label><div>{playlist_options}</div>
    <label>每个榜单下载数量</label><input type="number" name="playlist_limit" value="{config.get('playlist_limit', 20)}" min="1" max="100">
    <label><input type="checkbox" name="skip_existing" value="yes" {"checked" if config.get("skip_existing", True) else ""}> 不重复下载已存在歌曲</label>
    <label><input type="checkbox" name="import_after" value="yes" {"checked" if config.get("import_after", True) else ""}> 下载完成后自动导入本地曲库</label>
    <label>直接下载 URL</label>
    <textarea name="sources" rows="7" style="width:100%;padding:10px;border:1px solid #d8dee8;border-radius:6px">{escape(sources_text)}</textarea>
    <p class="muted">每行一个音频 URL，也可以写成：URL | 保存文件名。榜单与直接 URL 会在同一个任务中处理。</p>
    <p><button type="submit">保存设置</button></p>
  </form>
</div>
<div class="panel">
  <h2>立即执行</h2>
  <form method="post" action="/admin/downloads/run" style="display:inline"><button type="submit">按当前设置开始下载</button></form>
</div>
<div class="panel">
  <h2>导入 TXT 下载</h2>
  <form method="post" action="/admin/downloads/txt" enctype="multipart/form-data">
    <label>TXT 文件</label><input type="file" name="file" accept=".txt,text/plain" required>
    <p class="muted">支持每行一个歌名，或“歌名 - 歌手”。重复歌曲会自动跳过。</p>
    <p><button type="submit">导入 TXT 并下载</button></p>
  </form>
</div>
<div class="panel">
  <h2>下载进度</h2>
  <div class="progress"><div id="downloadProgressBar"></div></div>
  <p id="downloadProgressText" class="muted">正在读取状态...</p>
  <table><thead><tr><th>时间</th><th>歌曲/来源</th><th>状态</th><th>说明</th><th>保存位置</th></tr></thead><tbody id="downloadStatusRows"><tr><td colspan="5" class="muted">暂无记录</td></tr></tbody></table>
</div>
<table><thead><tr><th>直接 URL</th><th>保存名</th></tr></thead><tbody>{source_rows}</tbody></table>
<script>
function escapeHtml(value) {{
  return String(value || '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
}}
function statusLabel(value) {{
  return {{success:'成功', skipped:'跳过', failed:'失败', running:'运行中', queued:'排队中', idle:'空闲'}}[value] || value || '';
}}
async function refreshDownloadStatus() {{
  try {{
    const response = await fetch('/admin/downloads/status-json', {{cache:'no-store'}});
    const data = await response.json();
    const pct = data.percent || 0;
    document.getElementById('downloadProgressBar').style.width = pct + '%';
    document.getElementById('downloadProgressText').textContent = `${{statusLabel(data.status)}} | ${{data.message || ''}} | 进度 ${{data.done || 0}}/${{data.total || 0}} | 成功 ${{data.success || 0}} | 跳过 ${{data.skipped || 0}} | 失败 ${{data.failed || 0}}`;
    const rows = (data.items || []).slice(-80).reverse().map(item => `<tr><td>${{escapeHtml(item.time)}}</td><td>${{escapeHtml(item.name)}}</td><td>${{escapeHtml(statusLabel(item.status))}}</td><td>${{escapeHtml(item.message)}}</td><td>${{escapeHtml(item.path)}}</td></tr>`).join('');
    document.getElementById('downloadStatusRows').innerHTML = rows || '<tr><td colspan="5" class="muted">暂无记录</td></tr>';
  }} catch (error) {{
    document.getElementById('downloadProgressText').textContent = '读取状态失败：' + error;
  }}
}}
refreshDownloadStatus();
setInterval(refreshDownloadStatus, 2000);
</script>"""
    return _layout("下载任务", body, "下载任务")


@router.post("/admin/downloads/save", include_in_schema=False, dependencies=[Depends(require_admin)])
def save_downloads(
    enabled: str | None = Form(None),
    hour: int = Form(4),
    minute: int = Form(20),
    sources: str = Form(""),
    playlists: list[str] = Form([]),
    playlist_limit: int = Form(20),
    skip_existing: str | None = Form(None),
    import_after: str | None = Form(None),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    save_config(
        settings,
        enabled == "yes",
        hour,
        minute,
        sources,
        playlists=playlists,
        playlist_limit=playlist_limit,
        skip_existing=skip_existing == "yes",
        import_after=import_after == "yes",
    )
    from app.scheduler import configure_legal_download_job

    configure_legal_download_job()
    return _redirect("/admin/downloads", "下载设置已保存。")


@router.post("/admin/downloads/run", include_in_schema=False, dependencies=[Depends(require_admin)])
async def run_downloads(settings: Settings = Depends(get_settings)) -> RedirectResponse:
    messages = await download_configured_sources(settings)
    if not messages:
        return _redirect("/admin/downloads", "没有可执行的下载来源。")
    return _redirect("/admin/downloads", "；".join(messages[:8]))


@router.post("/admin/downloads/txt", include_in_schema=False, dependencies=[Depends(require_admin)])
async def upload_txt_download(file: UploadFile = File(...), settings: Settings = Depends(get_settings)) -> RedirectResponse:
    content = (await file.read()).decode("utf-8-sig", errors="ignore")
    _, message = await enqueue_txt_download(settings, content, file.filename or "songs.txt")
    return _redirect("/admin/downloads", message)


@router.get("/admin/downloads/status-json", include_in_schema=False, dependencies=[Depends(require_admin)])
def download_status_json(settings: Settings = Depends(get_settings)) -> JSONResponse:
    return JSONResponse(get_download_status(settings))


@router.get("/admin/navidrome", response_class=HTMLResponse, include_in_schema=False, dependencies=[Depends(require_admin)])
def navidrome_page(message: str | None = None) -> HTMLResponse:
    body = f"""<h1>Navidrome 扫描</h1>{_message_html(message)}
<div class="panel">
  <form method="post" action="/admin/navidrome/scan"><button type="submit">一键触发 Navidrome 扫描</button></form>
  <p class="muted">需要在 .env 中配置 NAVIDROME_USERNAME 和 NAVIDROME_PASSWORD，并确保 Navidrome 服务已启动。</p>
</div>"""
    return _layout("Navidrome 扫描", body, "Navidrome")


@router.post("/admin/navidrome/scan", include_in_schema=False, dependencies=[Depends(require_admin)])
async def scan_navidrome(settings: Settings = Depends(get_settings)) -> RedirectResponse:
    _, message = await trigger_scan(settings)
    return _redirect("/admin/navidrome", message)


def _status_text(status: str) -> str:
    return {"draft": "草稿", "published": "已发布", "archived": "已归档"}.get(status, status)


def _format_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"
