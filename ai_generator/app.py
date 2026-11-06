from __future__ import annotations

import base64
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    comfy_base_url: str = "http://127.0.0.1:8000"
    api_key: str = ""
    work_dir: Path = Path("./data")
    min_work_free_gb: float = 20.0
    prompt_template_path: Path = Path(r"C:\Users\Admin\Desktop\可见\提取音调的提示词.txt")
    seedvc_root: Path = Path(r"F:\py_workspace_vscode\voice_models\seed_extract\seed-vc-main")
    seedvc_python: Path = Path(r"C:\Users\Admin\Documents\ComfyUI\.venv\Scripts\python.exe")
    seedvc_runtime_dir: Path = Path(r"F:\py_workspace_vscode\voice_models\seed_runtime")
    strict_voice_clone: bool = True
    use_vocal_separation_remix: bool = True
    melody_use_vocal_separation: bool = True
    melody_pitch_backend: str = "rmvpe"
    strict_melody_backend_enabled: bool = True
    comfy_fallback_enabled: bool = False
    require_strict_melody_success: bool = True
    ace_step_original_enabled: bool = True
    ace_step_original_task_type: str = "text2music"
    melody_svs_enabled: bool = True
    melody_svs_required: bool = True
    melody_svs_root: Path = Path(r"D:\music_svs")
    melody_svs_python: Path = Path(r"D:\music_svs\.venv\Scripts\python.exe")
    melody_svs_backend: str = "openvpi-diffsinger"
    melody_svs_runtime_dir: str = ""
    melody_svs_vocoder_runtime_dir: str = ""
    melody_svs_vocoder_ckpt: str = ""
    melody_svs_vocoder_class: str = "NsfHifiGAN"
    melody_svs_acoustic_exp: str = ""
    melody_svs_variance_exp: str = ""
    melody_svs_variance_predict: str = "dur,pitch,energy,breathiness,voicing,tension"
    melody_svs_speaker: str = ""
    melody_svs_language: str = "zh"
    melody_svs_steps: int = 20
    melody_svs_onnx_model_dir: Path = Path(r"D:\music_svs\onnx_models\qixuan")
    melody_svs_note_graph_enabled: bool = True
    melody_svs_note_events_enabled: bool = False
    melody_svs_force_reference_f0: bool = True
    ace_step_base_url: str = "http://127.0.0.1:8001"
    ace_step_model: str = "acestep/acestep-v15-turbo"
    ace_step_timeout_seconds: int = 3600
    ace_step_cover_strength: float = 0.78
    ace_step_scheduler: str = "euler"
    ace_step_steps: int = 8
    ace_step_guidance_scale: float = 15.0
    ace_step_cleanup_after_job: bool = True
    demucs_model: str = "htdemucs"
    final_audio_format: str = "mp3"
    seedvc_diffusion_steps: int = 30
    max_duration_seconds: float = 240.0
    poll_interval_seconds: float = 5.0
    timeout_seconds: int = 3600
    asr_enabled: bool = True
    asr_model_size: str = "small"
    asr_model_path: str = ""
    asr_openai_cache_dir: Path = Path.home() / ".cache" / "whisper"
    asr_download_models: bool = False
    asr_device: str = "cpu"
    asr_compute_type: str = "int8"
    ffmpeg_path: str = ""
    melody_quality_gate_enabled: bool = True
    melody_asr_min_score: float = 0.78
    melody_asr_min_line_ratio: float = 0.45
    melody_pitch_min_similarity: float = 0.62
    melody_pitch_min_voiced_ratio: float = 0.20

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
app = FastAPI(title="Comfy MiniMax Music Generator", version="0.1.0")
_ASR_MODEL: Any | None = None
_ASR_MODEL_KEY: tuple[str, ...] | None = None
_RMVPE_EXTRACTOR: Any | None = None
_RMVPE_EXTRACTOR_KEY: tuple[str, str] | None = None


class _GpuGenerationQueue:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._next_ticket = 0
        self._serving_ticket = 0
        self._active_job_id = ""

    def acquire(self, job_dir: Path, job_id: str) -> int:
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            while ticket != self._serving_ticket:
                position = max(1, ticket - self._serving_ticket)
                _write_job_status(
                    job_dir,
                    "waiting",
                    f"AI 创作排队中，前面还有 {position} 个任务，请耐心等待",
                    {
                        "jobId": job_id,
                        "queueTicket": ticket,
                        "queuePosition": position,
                        "activeJobId": self._active_job_id,
                    },
                )
                self._condition.wait(timeout=5)
            self._active_job_id = job_id
            _write_job_status(
                job_dir,
                "running",
                "AI 创作任务已开始，正在独占 GPU 资源",
                {"jobId": job_id, "queueTicket": ticket, "queuePosition": 0},
            )
            return ticket

    def release(self, ticket: int, job_id: str) -> None:
        with self._condition:
            if ticket == self._serving_ticket:
                self._serving_ticket += 1
                if self._active_job_id == job_id:
                    self._active_job_id = ""
                self._condition.notify_all()

    def status(self) -> dict[str, Any]:
        with self._condition:
            queued = max(0, self._next_ticket - self._serving_ticket - (1 if self._active_job_id else 0))
            return {
                "maxConcurrentGpuJobs": 1,
                "activeJobId": self._active_job_id,
                "queuedJobs": queued,
                "nextTicket": self._next_ticket,
                "servingTicket": self._serving_ticket,
            }


_GPU_GENERATION_QUEUE = _GpuGenerationQueue()
_GENERATION_LOCK = threading.Lock()


@app.get("/ping")
async def ping() -> dict:
    return {"ok": True}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    detail = _log_exception("unhandled", exc)
    return JSONResponse(status_code=500, content={"detail": detail})


@app.get("/health")
def health(full: bool = False) -> dict:
    comfy_ok = False
    try:
        response = httpx.get(f"{settings.comfy_base_url.rstrip('/')}/system_stats", timeout=5)
        comfy_ok = response.is_success
    except Exception:
        comfy_ok = False
    ace_status = _ace_step_status()
    disk_status = _work_disk_status()
    seedvc_ok = _seedvc_ready()
    remix_ok = _remix_ready()
    melody_svs_status = _melody_svs_status() if full else _melody_svs_quick_status()
    strict_backend_available = bool(ace_status.get("available"))
    if settings.strict_melody_backend_enabled and settings.melody_svs_required:
        strict_backend_available = bool(melody_svs_status.get("ready"))
    return {
        "ok": True,
        "comfy": comfy_ok,
        "voiceClone": seedvc_ok,
        "vocalSeparationRemix": remix_ok,
        "strictVoiceClone": settings.strict_voice_clone,
        "originalWithoutVoice": True,
        "finalAudioFormat": _final_audio_format(),
        "lyricsAlignment": _asr_status(),
        "workDisk": disk_status,
        "gpuQueue": _GPU_GENERATION_QUEUE.status(),
        "melodyRewrite": {
            "enabled": bool(settings.strict_melody_backend_enabled),
            "strictMelodyBackend": bool(settings.strict_melody_backend_enabled),
            "strictBackendAvailable": strict_backend_available,
            "backend": settings.melody_svs_backend if settings.melody_svs_required else "ACE-Step",
            "strictBackend": settings.melody_svs_backend if settings.melody_svs_required else "ACE-Step",
            "pitchBackend": settings.melody_pitch_backend,
            "baseUrl": settings.ace_step_base_url,
            "comfyFallbackEnabled": bool(settings.comfy_fallback_enabled),
            "requireStrictMelodySuccess": bool(settings.require_strict_melody_success),
            "aceStepOriginalEnabled": bool(settings.ace_step_original_enabled),
            "aceStepCleanupAfterJob": bool(settings.ace_step_cleanup_after_job),
            "fallback": "disabled" if not settings.comfy_fallback_enabled else "prompt_conditioned_melody_style",
            "status": ace_status,
        },
        "melodySvs": melody_svs_status,
    }


@app.post("/api/generate-from-files")
async def generate_from_files(
    mode: str = Form("reference"),
    sourceTitle: str = Form(""),
    sourceArtist: str = Form(""),
    sourceAlbum: str = Form(""),
    theme: str = Form(""),
    sourceLyrics: str = Form(""),
    creationMode: str = Form("original"),
    jobId: str = Form(""),
    voiceType: str = Form("auto"),
    electronicLevel: str = Form("auto"),
    mood: str = Form("auto"),
    tempo: str = Form("auto"),
    durationLevel: str = Form("auto"),
    optionSummary: str = Form(""),
    previewOnly: bool = Form(False),
    lyricsOverride: str = Form(""),
    sourceAudio: UploadFile | None = File(None),
    voiceAudio: UploadFile | None = File(None),
    x_api_key: str = Header("", alias="X-API-Key"),
) -> dict:
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="invalid api key")

    job_id = _safe_name(jobId or uuid.uuid4().hex[:12])
    job_dir = settings.work_dir / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    _ensure_work_disk_space(job_dir, reserve_gb=settings.min_work_free_gb)
    _write_job_status(job_dir, "received", "request accepted", {"jobId": job_id})
    source_path = job_dir / "no_reference.txt"
    if sourceAudio and sourceAudio.filename:
        source_path = await _save_upload(sourceAudio, job_dir / f"source{Path(sourceAudio.filename or '').suffix or '.audio'}")
    elif not source_path.exists():
        source_path.write_text("original generation without source audio", encoding="utf-8")
    voice_path = None
    if voiceAudio and voiceAudio.filename:
        voice_path = await _save_upload(voiceAudio, job_dir / f"voice{Path(voiceAudio.filename).suffix or '.m4a'}")
    requested_mode = (mode or "reference").strip().lower()
    _write_job_status(
        job_dir,
        "prepared",
        "source and voice files saved",
        {"mode": requested_mode, "hasSource": bool(sourceAudio and sourceAudio.filename), "hasVoice": bool(voice_path)},
    )
    needs_voice_clone = requested_mode in {"voice_reference", "voice_original", "clone", "voice"}
    if settings.strict_voice_clone and needs_voice_clone and not voice_path:
        raise HTTPException(status_code=400, detail="strict voice cloning requires a user voice recording")

    return await run_in_threadpool(
        _generate_from_prepared_files,
        job_dir,
        requested_mode,
        sourceTitle,
        sourceArtist,
        sourceAlbum,
        theme,
        sourceLyrics,
        creationMode,
        source_path,
        voice_path,
        voiceType,
        electronicLevel,
        mood,
        tempo,
        durationLevel,
        optionSummary,
        job_id,
        previewOnly,
        lyricsOverride,
    )


def _generate_from_prepared_files(*args: Any, **kwargs: Any) -> dict:
    job_dir = args[0] if args else kwargs["job_dir"]
    job_id = args[-3] if len(args) >= 3 else kwargs.get("job_id", "")
    if not isinstance(job_dir, Path):
        job_dir = Path(job_dir)
    job_id = str(job_id or job_dir.name)
    preview_only = bool(args[-2]) if len(args) >= 2 and isinstance(args[-2], bool) else bool(kwargs.get("preview_only", False))
    if preview_only:
        return _generate_from_prepared_files_impl(*args, **kwargs)
    queue_ticket = _GPU_GENERATION_QUEUE.acquire(job_dir, job_id)
    try:
        return _generate_from_prepared_files_impl(*args, **kwargs)
    finally:
        _cleanup_generation_gpu(job_dir, job_id)
        _GPU_GENERATION_QUEUE.release(queue_ticket, job_id)


def _generate_from_prepared_files_impl(
    job_dir: Path,
    requested_mode: str,
    sourceTitle: str,
    sourceArtist: str,
    sourceAlbum: str,
    theme: str,
    sourceLyrics: str,
    creationMode: str,
    source_path: Path,
    voice_path: Path | None,
    voiceType: str,
    electronicLevel: str,
    mood: str,
    tempo: str,
    durationLevel: str,
    optionSummary: str,
    job_id: str,
    preview_only: bool = False,
    lyrics_override: str = "",
) -> dict:
    creation_mode = _normalize_creation_mode(creationMode)
    analysis = _build_analysis(sourceTitle, sourceArtist, sourceAlbum, theme, source_path, voice_path)
    melody_template: dict[str, Any] = {}
    music3_original_mode = creation_mode == "music3_original"
    if creation_mode == "melody_rewrite" and source_path.exists() and source_path.suffix.lower() != ".txt":
        _write_job_status(job_dir, "melody_analyzing", "analyzing source lyric structure and melody template")
        melody_template = _build_reference_melody_template(source_path, sourceLyrics, job_dir)
        analysis += "\n\n## Melody Rewrite Template\n" + _format_melody_template(melody_template)
    caption = _build_caption(analysis, theme, voice_path is not None, creation_mode)
    caption = _apply_generation_options(caption, voiceType, electronicLevel, mood, tempo, durationLevel, optionSummary, voice_path is not None)
    if creation_mode == "melody_rewrite" and melody_template.get("lines"):
        _write_job_status(job_dir, "lyric_adapting", "writing new lyrics to fit the source melody structure")
        lyrics = _build_melody_rewrite_lyrics(sourceTitle, theme, melody_template)
    else:
        lyrics = lyrics_override.strip() if music3_original_mode and str(lyrics_override or "").strip() else _build_lyrics(sourceTitle, theme, sourceLyrics)
    title = _generated_title(sourceTitle, theme)
    if music3_original_mode:
        title = f"AI Music3 - {sourceTitle or '原创音乐'}".strip()
    elif creation_mode == "melody_rewrite" and title.startswith("AI鍘熷垱"):
        title = title.replace("AI鍘熷垱", "AI鏀圭紪", 1)
        title = title.replace("AI原创", "AI改编", 1)
    _write_generation_debug(
        job_dir,
        {
            "jobId": job_id,
            "creationMode": creation_mode,
            "requestedMode": requested_mode,
            "sourceTitle": sourceTitle,
            "sourceArtist": sourceArtist,
            "title": title,
            "theme": theme,
            "options": {
                "voiceType": voiceType,
                "electronicLevel": electronicLevel,
                "mood": mood,
                "tempo": tempo,
                "durationLevel": durationLevel,
                "optionSummary": optionSummary,
            },
            "analysis": analysis,
            "caption": caption,
            "lyrics": lyrics,
            "lyricsOverride": lyrics_override,
            "melodyTemplate": _debug_melody_template(melody_template),
        },
    )
    if preview_only:
        return {
            "title": title,
            "artist": "AI 创作",
            "album": "我生成的音乐",
            "format": "",
            "analysis": analysis,
            "caption": caption,
            "lyrics": lyrics,
            "syncedLyrics": "",
            "lyricsSource": "preview_only",
            "creationMode": creation_mode,
            "melodyTemplate": melody_template,
            "strictMelodyError": "",
            "audioBase64": "",
        }
    strict_melody_error = ""
    requires_strict_melody = (
        creation_mode == "melody_rewrite"
        and settings.strict_melody_backend_enabled
        and source_path.exists()
        and source_path.suffix.lower() != ".txt"
    )
    direct_music3_mode = creation_mode == "music3_original"
    uses_ace_original = (
        not requires_strict_melody
        and not direct_music3_mode
        and settings.ace_step_original_enabled
        and settings.strict_melody_backend_enabled
    )

    try:
        if not _GENERATION_LOCK.acquire(blocking=False):
            _write_job_status(job_dir, "waiting", "waiting for the previous AI creation job to finish")
            _GENERATION_LOCK.acquire()
        try:
            audio_bytes = b""
            if direct_music3_mode:
                try:
                    _write_job_status(job_dir, "music3_running", "submitting Music3 original generation")
                    audio_bytes = _generate_with_comfy(
                        caption=caption,
                        lyrics=lyrics,
                        max_duration=float(settings.max_duration_seconds),
                        seed=random.randint(1, 2**63 - 1),
                        output_prefix=f"audio/ai_music_{job_id}",
                    )
                    _write_job_status(job_dir, "music3_done", "Music3 audio generated", {"bytes": len(audio_bytes)})
                except Exception as exc:
                    detail = _log_exception("Music3 generation failed", exc)
                    public_message = _public_ai_service_error(exc)
                    _write_job_status(
                        job_dir,
                        "service_unavailable",
                        public_message,
                        {"internalError": detail[:500]},
                    )
                    raise HTTPException(status_code=503, detail=public_message) from exc
            if requires_strict_melody:
                svs_status = _melody_svs_status() if settings.melody_svs_enabled else {"ready": False, "missing": ["melody SVS disabled"]}
                if settings.melody_svs_enabled and svs_status.get("ready"):
                    try:
                        _write_job_status(job_dir, "melody_svs_running", "submitting melody-conditioned SVS generation")
                        audio_bytes = _generate_with_melody_svs(
                            source_path=source_path,
                            lyrics=lyrics,
                            title=title,
                            template=melody_template,
                            job_dir=job_dir,
                        )
                        _write_job_status(job_dir, "melody_svs_done", "melody-conditioned SVS audio generated", {"bytes": len(audio_bytes)})
                    except Exception as exc:
                        strict_melody_error = _log_exception("melody SVS failed", exc)
                        if settings.melody_svs_required:
                            public_message = _public_ai_service_error(exc)
                            _write_job_status(
                                job_dir,
                                "service_unavailable",
                                public_message,
                                {"internalError": strict_melody_error[:500]},
                            )
                            raise HTTPException(status_code=503, detail=public_message) from exc
                        _write_job_status(job_dir, "melody_svs_fallback", strict_melody_error)
                elif settings.melody_svs_required:
                    detail = f"melody SVS backend not ready: missing={svs_status.get('missing')}"
                    _write_job_status(job_dir, "service_unavailable", detail)
                    raise HTTPException(status_code=503, detail="AI 创作服务维护中，请恢复后再试")
                elif settings.melody_svs_enabled:
                    _write_job_status(
                        job_dir,
                        "melody_svs_unavailable",
                        "melody SVS backend is enabled but not ready; falling back to ACE-Step",
                        {"missing": svs_status.get("missing"), "warnings": svs_status.get("warnings")},
                    )
                try:
                    if not audio_bytes:
                        _write_job_status(job_dir, "strict_melody_running", "submitting ACE-Step cover generation")
                        audio_bytes = _generate_with_ace_step_cover(
                            source_path=source_path,
                            lyrics=lyrics,
                            caption=caption,
                            title=title,
                            job_dir=job_dir,
                        )
                        _write_job_status(job_dir, "strict_melody_done", "ACE-Step cover audio generated", {"bytes": len(audio_bytes)})
                except Exception as exc:
                    strict_melody_error = _log_exception("ACE-Step strict melody failed", exc)
                    if settings.require_strict_melody_success or not settings.comfy_fallback_enabled:
                        public_message = _public_ai_service_error(exc)
                        _write_job_status(
                            job_dir,
                            "service_unavailable",
                            public_message,
                            {"internalError": strict_melody_error[:500]},
                        )
                        raise HTTPException(status_code=503, detail=public_message) from exc
                    _write_job_status(job_dir, "strict_melody_fallback", strict_melody_error)
            if not audio_bytes and uses_ace_original:
                try:
                    _write_job_status(job_dir, "ace_step_running", "submitting ACE-Step original generation")
                    audio_bytes = _generate_with_ace_step_original(
                        lyrics=lyrics,
                        caption=caption,
                        title=title,
                        job_dir=job_dir,
                    )
                    _write_job_status(job_dir, "ace_step_done", "ACE-Step original audio generated", {"bytes": len(audio_bytes)})
                except Exception as exc:
                    detail = _log_exception("ACE-Step original generation failed", exc)
                    if not settings.comfy_fallback_enabled:
                        public_message = _public_ai_service_error(exc)
                        _write_job_status(
                            job_dir,
                            "service_unavailable",
                            public_message,
                            {"internalError": detail[:500]},
                        )
                        raise HTTPException(status_code=503, detail=public_message) from exc
                    _write_job_status(job_dir, "ace_step_fallback", detail)
            if not audio_bytes:
                if not settings.comfy_fallback_enabled:
                    public_message = "AI 创作服务维护中，请稍后再试"
                    _write_job_status(job_dir, "service_unavailable", public_message)
                    raise HTTPException(status_code=503, detail=public_message)
                _write_job_status(job_dir, "comfy_running", "submitted to Comfy Desktop")
                audio_bytes = _generate_with_comfy(
                    caption=caption,
                    lyrics=lyrics,
                    max_duration=float(settings.max_duration_seconds),
                    seed=random.randint(1, 2**63 - 1),
                    output_prefix=f"audio/ai_music_{job_id}",
                )
                _write_job_status(job_dir, "comfy_done", "Comfy audio generated", {"bytes": len(audio_bytes)})
            original_path = job_dir / "generated_original.mp3"
            original_path.write_bytes(audio_bytes)
            final_audio_path = original_path
            audio_format = "mp3"
            if voice_path and not direct_music3_mode:
                _write_job_status(job_dir, "voice_cloning", "voice cloning and remixing")
                if settings.use_vocal_separation_remix:
                    processed_path = _clone_vocals_and_remix(original_path, voice_path, job_dir / "voice_clone")
                else:
                    processed_path = _convert_singing_voice_with_seedvc(original_path, voice_path, job_dir / "voice_clone")
                final_path = _encode_final_audio(processed_path, job_dir / f"final.{_final_audio_format()}")
                audio_bytes = final_path.read_bytes()
                final_audio_path = final_path
                audio_format = final_path.suffix.strip(".").lower() or _final_audio_format()
            _write_job_status(job_dir, "lyrics_aligning", "building synced lyrics")
            lyrics_source = "ai_asr_lrc"
            if creation_mode == "melody_rewrite" and melody_template.get("lines"):
                validation = _validate_sung_lyrics(final_audio_path, lyrics)
                quality_report = _validate_melody_generation_quality(final_audio_path, lyrics, melody_template, validation, job_dir)
                if settings.melody_quality_gate_enabled and not quality_report.get("passed"):
                    retry = _retry_openutau_syllable_locked_candidate(source_path, lyrics, title, melody_template, job_dir, original_path, voice_path)
                    if retry:
                        audio_bytes = retry["audioBytes"]
                        final_audio_path = retry["finalAudioPath"]
                        original_path = retry["originalPath"]
                        audio_format = retry["audioFormat"]
                        validation = retry["validation"]
                        quality_report = retry["qualityReport"]
                _write_generation_quality_report(job_dir, quality_report)
                if settings.melody_quality_gate_enabled and not quality_report.get("passed"):
                    public_message = str(quality_report.get("message") or "AI 创作结果质量未达标，请重新生成")
                    _write_job_status(job_dir, "quality_rejected", public_message, quality_report)
                    raise HTTPException(status_code=422, detail=public_message)
                if validation.get("usable") and _should_use_actual_sung_lrc(validation):
                    synced_lyrics = _build_lrc_from_timed_items(title, validation.get("timedItems") or [])
                    lyrics_source = "ai_asr_actual_lrc"
                    _write_job_status(
                        job_dir,
                        "lyrics_actual_asr",
                        "generated vocal did not follow target lyrics; using actual sung lyrics",
                        {"score": validation.get("score"), "lines": validation.get("lineCount")},
                    )
                else:
                    synced_lyrics = _build_forced_aligned_lrc(
                        final_audio_path,
                        title,
                        lyrics,
                        melody_template,
                        validation.get("timedSpans") or [],
                    )
                    if synced_lyrics:
                        lyrics_source = "ai_forced_aligned_lrc"
                    else:
                        synced_lyrics = _build_hybrid_synced_lrc(final_audio_path, title, lyrics, melody_template)
                        lyrics_source = "ai_structure_locked_lrc"
            else:
                synced_lyrics = _build_forced_aligned_lrc(final_audio_path, title, lyrics, {})
                if synced_lyrics:
                    lyrics_source = "ai_forced_aligned_lrc"
                else:
                    synced_lyrics = _build_asr_synced_lrc(final_audio_path, title, lyrics)
            _write_job_status(job_dir, "success", "generation completed", {"format": audio_format, "bytes": len(audio_bytes)})
        finally:
            _GENERATION_LOCK.release()
    except HTTPException:
        raise
    except Exception as exc:
        detail = _log_exception(f"job {job_id}", exc)
        _write_job_status(job_dir, "failed", detail)
        raise
    return {
        "title": title,
        "artist": "AI 创作",
        "album": "我生成的音乐",
        "format": audio_format,
        "analysis": analysis,
        "caption": caption,
        "lyrics": lyrics,
        "syncedLyrics": synced_lyrics,
        "lyricsSource": lyrics_source,
        "creationMode": creation_mode,
        "melodyTemplate": melody_template,
        "strictMelodyError": strict_melody_error,
        "audioBase64": base64.b64encode(audio_bytes).decode("ascii"),
    }


async def _save_upload(upload: UploadFile, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _ensure_work_disk_space(destination.parent, reserve_gb=settings.min_work_free_gb)
    try:
        with destination.open("wb") as target:
            while chunk := await upload.read(1024 * 1024):
                target.write(chunk)
    except OSError as exc:
        if getattr(exc, "errno", None) == 28:
            raise HTTPException(status_code=507, detail=_disk_space_error(destination.parent)) from exc
        raise
    return destination


def _work_disk_status() -> dict[str, Any]:
    path = settings.work_dir
    try:
        path.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(path)
        return {
            "path": str(path),
            "totalGb": round(usage.total / 1024**3, 2),
            "freeGb": round(usage.free / 1024**3, 2),
            "minFreeGb": float(settings.min_work_free_gb),
            "ok": usage.free >= settings.min_work_free_gb * 1024**3,
        }
    except Exception as exc:
        return {"path": str(path), "ok": False, "error": str(exc)[:160], "minFreeGb": float(settings.min_work_free_gb)}


def _ensure_work_disk_space(path: Path, reserve_gb: float) -> None:
    path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    if usage.free < reserve_gb * 1024**3:
        raise HTTPException(status_code=507, detail=_disk_space_error(path, usage.free, reserve_gb))


def _disk_space_error(path: Path, free_bytes: int | None = None, reserve_gb: float | None = None) -> str:
    if free_bytes is None:
        try:
            free_bytes = shutil.disk_usage(path).free
        except Exception:
            free_bytes = 0
    free_gb = round(float(free_bytes) / 1024**3, 2)
    need_gb = float(reserve_gb if reserve_gb is not None else settings.min_work_free_gb)
    return f"AI 生成工作盘空间不足：当前可用 {free_gb}GB，至少需要预留 {need_gb:g}GB。请清理磁盘或把 WORK_DIR 配到 D 盘后重试。"


def _analyze_audio_profile(path: Path) -> str:
    if not path.exists() or path.suffix.lower() == ".txt":
        return "- mode: original_without_reference\n- note: no usable reference audio; rely on user theme and options."

    probe = _ffprobe_audio(path)
    duration = float(probe.get("duration") or 0.0)
    segments = _sample_loudness_segments(path, duration)
    energy = _energy_shape(segments)
    tempo_hint = _tempo_hint_from_energy(segments)
    format_name = str(probe.get("format") or path.suffix.strip(".") or "unknown")
    codec = str(probe.get("codec") or "unknown")
    bitrate = str(probe.get("bitrate") or "unknown")
    sample_rate = str(probe.get("sample_rate") or "unknown")
    channels = str(probe.get("channels") or "unknown")
    lines = [
        f"- format: {format_name}, codec: {codec}, bitrate: {bitrate}",
        f"- duration: {duration:.1f} seconds" if duration else "- duration: unknown",
        f"- sample_rate: {sample_rate}, channels: {channels}",
        f"- estimated_energy_curve: {energy}",
        f"- tempo_hint: {tempo_hint}",
    ]
    if segments:
        lines.append("- loudness_samples: " + "; ".join(f"{name} {value:.1f} dB" for name, value in segments))
    return "\n".join(lines)


def _ffprobe_audio(path: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
        )
    except Exception as exc:
        _log_exception("ffprobe failed", exc)
        return {}
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    streams = payload.get("streams") or []
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    fmt = payload.get("format") or {}
    return {
        "duration": fmt.get("duration") or audio.get("duration"),
        "format": fmt.get("format_name"),
        "codec": audio.get("codec_name"),
        "bitrate": fmt.get("bit_rate") or audio.get("bit_rate"),
        "sample_rate": audio.get("sample_rate"),
        "channels": audio.get("channels"),
    }


def _sample_loudness_segments(path: Path, duration_seconds: float) -> list[tuple[str, float]]:
    if duration_seconds <= 0:
        points = [("full", 0.0)]
    else:
        sample_len = min(18.0, max(8.0, duration_seconds * 0.08))
        points = [
            ("intro", max(0.0, duration_seconds * 0.08)),
            ("middle", max(0.0, duration_seconds * 0.48)),
            ("ending", max(0.0, duration_seconds - sample_len - 4.0)),
        ]
    samples: list[tuple[str, float]] = []
    for name, start in points:
        loudness = _segment_loudness(path, start)
        if loudness is not None:
            samples.append((name, loudness))
    return samples


def _segment_loudness(path: Path, start_seconds: float) -> float | None:
    try:
        result = subprocess.run(
            [
                _ffmpeg_binary(),
                "-hide_banner",
                "-nostats",
                "-ss",
                f"{max(0.0, start_seconds):.2f}",
                "-t",
                "12",
                "-i",
                str(path),
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=45,
        )
    except Exception as exc:
        _log_exception("ffmpeg loudness sample failed", exc)
        return None
    text = result.stderr or result.stdout or ""
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", text)
    if not match:
        return None
    return float(match.group(1))


def _energy_shape(samples: list[tuple[str, float]]) -> str:
    if len(samples) < 2:
        return "unknown; keep arrangement balanced"
    values = [value for _, value in samples]
    spread = max(values) - min(values)
    if spread < 2.5:
        return "stable and restrained; avoid sudden huge drops or jumps"
    intro, middle, ending = values[0], values[len(values) // 2], values[-1]
    if middle > intro + 2.0 and middle > ending + 1.0:
        return "gradual build into a stronger middle/chorus, then softer ending"
    if ending > middle + 2.0:
        return "late lift; final chorus should be the fullest section"
    if intro > middle + 2.0:
        return "strong opening followed by restrained verses"
    return "moderate dynamic contrast with a natural chorus lift"


def _tempo_hint_from_energy(samples: list[tuple[str, float]]) -> str:
    if not samples:
        return "unknown; follow user tempo option"
    avg = sum(value for _, value in samples) / len(samples)
    if avg <= -24:
        return "quiet ballad or acoustic-leaning slow pop"
    if avg <= -17:
        return "medium emotional Mandopop with controlled drums"
    return "energetic pop with clear rhythmic drive"


def _build_analysis(
    source_title: str,
    source_artist: str,
    source_album: str,
    theme: str,
    source_path: Path,
    voice_path: Path | None,
) -> str:
    template = _read_text(settings.prompt_template_path)
    audio_profile = _analyze_audio_profile(source_path)
    creative_theme = _clean_creative_theme(theme)
    reference_title = _clean_reference_title(source_title)
    voice_note = "用户已提供一段人声录音，最终会尝试做人声转换；生成阶段仍需先产生清晰自然的主唱。"
    if not voice_path:
        voice_note = "用户未提供人声录音，按用户选择的声音类型生成自然中文流行人声。"
    return (
        "## Reference Song Style Profile\n"
        f"Reference title: {reference_title or 'Untitled'}\n"
        f"Reference artist: {source_artist or 'Unknown artist'}\n"
        f"Reference album: {source_album or 'Unknown album'}\n"
        f"Reference file: {source_path.name}\n"
        f"User creative theme: {creative_theme or 'No explicit theme; derive a new theme from the reference mood and title imagery.'}\n"
        f"{voice_note}\n\n"
        "Audio-derived features:\n"
        f"{audio_profile}\n\n"
        "Creative instruction: keep the macro listening experience close to the reference: similar tempo band, emotional temperature, "
        "energy contour, verse/chorus contrast, arrangement density, vocal intimacy, and ending feel. "
        "Do not copy melody, lyrics, title phrase, hook, chord progression, or any recognizable arrangement detail.\n\n"
        f"Local prompt template note: {template[:900]}"
    )


def _build_caption(analysis: str, theme: str, has_voice: bool, creation_mode: str = "original") -> str:
    voice_hint = (
        "The final vocal will be voice-converted later, so generate a clear lead vocal that sits naturally inside a complete instrumental mix. "
        if has_voice
        else "Use a warm, natural Mandarin vocal performance. "
    )
    analysis_hint = re.sub(r"\s+", " ", analysis).strip()[:900]
    creative_theme = _clean_creative_theme(theme)
    if creation_mode == "music3_original":
        melody_instruction = (
            "Music3 original mode: use the prepared lyrics to compose a completely fresh melody and full arrangement. "
            "Treat the reference song only as emotional and structural inspiration. Do not preserve the original tune, chord progression, hook, "
            "or recognizable arrangement details. "
        )
    elif creation_mode == "melody_rewrite":
        melody_instruction = (
            "Melody rewrite mode for user-authorized material: keep the source song's phrase lengths, section contour, vocal entry timing, rhyme slots, "
            "energy contour, and broad melodic rise/fall feeling as closely as this generator allows. Replace the lyrics with a new coherent story; "
            "do not paste the old lyrics. Never sing metadata or credit lines such as title, songwriter, composer, arranger, producer, OP/SP, copyright, or label names. "
            "If exact melody conditioning is unavailable, prioritize singability against the provided line-duration and pitch template. "
        )
    else:
        melody_instruction = "Original melody only: do not copy the original melody, lyrics, title phrase, hook, chord progression, or recognizable arrangement details. "
    return (
        "Create a fully original Mandarin Chinese commercial pop song with finished record production quality. "
        "Use the reference for macro style matching: tempo range, mood, energy curve, groove feel, instrumentation density, section contrast, vocal distance, and outro shape. "
        + melody_instruction +
        f"The lyric theme should center on: {creative_theme or 'a fresh emotional story suggested by the reference title and mood, not by technical option labels'}. "
        "The rhythm must follow the reference analysis and user tempo choice. Keep it grounded and singable with a clear pulse, supportive backbeat or soft percussion, and a bass line that locks with the song mood. "
        "The mix must not sound a cappella or karaoke-thin. Keep audible accompaniment throughout the whole song, with instruments selected from the reference profile and user options. "
        "Use stereo arrangement width: center lead vocal, centered kick and bass, guitars, keys, pads, strings or synth layers spread tastefully. "
        "Verses should preserve the reference intimacy; pre-chorus should build tension; chorus should open emotionally with stronger arrangement density and a memorable refrain. "
        + voice_hint +
        "Use a complete commercial pop song structure: intro, verse 1, pre-chorus, chorus, post-chorus hook, verse 2, pre-chorus, chorus, bridge or instrumental break, final chorus, and outro. "
        "The chorus should repeat clearly several times, with a memorable repeated hook and a bigger final chorus. "
        "After the final chorus, the vocal should resolve softly and trail off naturally, followed by a 5 to 8 second instrumental outro where piano, guitar, pads, or strings slowly fade into silence. "
        "Avoid sparse accompaniment, dry solo voice, abrupt endings, hard cuts, sudden stops, or unfinished fade-outs. The ending must feel complete and natural. "
        f"Reference analysis note: {analysis_hint}"
    )


def _apply_generation_options(
    caption: str,
    voice_type: str,
    electronic_level: str,
    mood: str,
    tempo: str,
    duration_level: str,
    option_summary: str,
    has_voice: bool,
) -> str:
    parts: list[str] = []
    if not has_voice:
        voice_map = {
            "male": "Use a mature, warm male Mandarin lead vocal.",
            "female": "Use a clear, expressive female Mandarin lead vocal.",
            "auto": "Choose the most suitable Mandarin lead vocal automatically.",
        }
        parts.append(voice_map.get((voice_type or "auto").strip().lower(), voice_map["auto"]))
    electronic_map = {
        "more": "Add more tasteful electronic pop elements, synth bass, subtle sidechain movement, and modern rhythmic texture.",
        "less": "Keep electronic elements restrained; prioritize piano, guitar, strings, bass, and organic pop drums.",
        "auto": "Balance acoustic and electronic production naturally according to the song mood.",
    }
    mood_map = {
        "warm": "Overall mood should feel warm, healing, and hopeful.",
        "sad": "Overall mood should feel emotional, restrained, and bittersweet without becoming hopeless.",
        "energetic": "Overall mood should feel more powerful, uplifting, and rhythmically driven.",
        "quiet": "Overall mood should feel quiet, intimate, and reflective.",
        "auto": "Choose the emotional color automatically from the theme.",
    }
    tempo_map = {
        "slow": "Use a slower ballad tempo around 72 to 86 BPM.",
        "medium": "Use a medium pop tempo around 88 to 108 BPM.",
        "fast": "Use a faster pop tempo around 112 to 128 BPM.",
        "auto": "Choose a natural commercial pop tempo from the theme.",
    }
    duration_map = {
        "short": "Target a complete shorter song around 2:30 to 3:10.",
        "standard": "Target a complete standard song around 3:20 to 4:10.",
        "long": "Target a complete longer song around 4:10 to 4:50 with a fuller bridge and final chorus.",
        "auto": "Choose a natural commercial song length.",
    }
    parts.append(electronic_map.get((electronic_level or "auto").strip().lower(), electronic_map["auto"]))
    parts.append(mood_map.get((mood or "auto").strip().lower(), mood_map["auto"]))
    parts.append(tempo_map.get((tempo or "auto").strip().lower(), tempo_map["auto"]))
    parts.append(duration_map.get((duration_level or "auto").strip().lower(), duration_map["auto"]))
    if option_summary.strip():
        parts.append(f"User option summary for arrangement control only, never use these option words as the song title, lyric hook, or repeated lyric phrase: {option_summary.strip()}.")
    return caption + " " + " ".join(parts)


def _normalize_creation_mode(value: str) -> str:
    normalized = (value or "original").strip().lower()
    if normalized in {"melody_rewrite", "melody", "rewrite", "keep_melody"}:
        return "melody_rewrite"
    if normalized in {"music3", "music3_original", "music3_new", "full_music3"}:
        return "music3_original"
    return "original"


def _build_reference_melody_template(source_path: Path, source_lyrics: str, job_dir: Path) -> dict[str, Any]:
    duration = float(_ffprobe_audio(source_path).get("duration") or 0.0)
    timed_lines = _parse_lrc_timed_lines(source_lyrics)
    timed_lines = _filter_reference_timed_lines(timed_lines, source_path.stem)
    lyric_source = "lrc"
    if len(timed_lines) < 3:
        asr_lrc = _build_asr_synced_lrc(source_path, source_path.stem, "")
        timed_lines = _parse_lrc_timed_lines(asr_lrc)
        timed_lines = _filter_reference_timed_lines(timed_lines, source_path.stem)
        lyric_source = "whisper"
    if len(timed_lines) < 2:
        return {
            "source": lyric_source,
            "durationSeconds": duration,
            "lines": [],
            "note": "No usable timed lyrics were found; melody rewrite fell back to normal generation.",
        }

    lines: list[dict[str, Any]] = []
    for index, item in enumerate(timed_lines[:80]):
        next_start = timed_lines[index + 1]["startMs"] if index + 1 < len(timed_lines) else int(max(duration * 1000, item["startMs"] + 3000))
        end_ms = max(item["startMs"] + 900, min(next_start, item["startMs"] + 8500))
        text = str(item["text"]).strip()
        section = _section_name_for_lrc(index, len(timed_lines), text)
        lines.append(
            {
                "index": index + 1,
                "startMs": int(item["startMs"]),
                "endMs": int(end_ms),
                "durationMs": int(end_ms - item["startMs"]),
                "charCount": max(4, min(24, _chinese_len(text))),
                "pauseAfterMs": max(0, int(next_start - end_ms)) if index + 1 < len(timed_lines) else 0,
                "sourceText": text,
                "sourceEnding": text[-1:] if text else "",
                "section": section,
                "rhymeSlot": _rhyme_slot_for_section(index, len(timed_lines), section),
            }
        )

    analysis_audio = _prepare_vocal_reference_for_analysis(source_path, job_dir)
    pitch_features = _extract_line_pitch_features(analysis_audio, lines)
    for line, pitch in zip(lines, pitch_features):
        line.update(pitch)
    lines = _clean_melody_template_lines(lines)
    return {
        "source": lyric_source,
        "durationSeconds": duration,
        "analysisAudio": str(analysis_audio),
        "strictMelodyBackend": bool(settings.strict_melody_backend_enabled),
        "lines": lines,
        "sections": _summarize_sections(lines),
    }


def _clean_melody_template_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    dropped = 0
    for raw_line in lines:
        line = dict(raw_line)
        duration_ms = int(line.get("durationMs") or max(0, int(line.get("endMs") or 0) - int(line.get("startMs") or 0)))
        duration = max(0.1, duration_ms / 1000.0)
        source_count = _reference_char_count(line)
        voiced_ratio = float(line.get("voicedRatio") or 0.0)
        avg_f0 = float(line.get("avgF0") or 0.0)
        pitch_backend = str(line.get("pitchBackend") or "")

        if avg_f0 <= 0 or voiced_ratio < 0.08 or pitch_backend.endswith("unavailable"):
            dropped += 1
            continue

        voiced_start_ratio = line.get("voicedStartRatio")
        voiced_end_ratio = line.get("voicedEndRatio")
        if voiced_start_ratio is not None and voiced_end_ratio is not None:
            try:
                span_start = int(line.get("startMs") or 0)
                span_end = int(line.get("endMs") or span_start + duration_ms)
                span = max(1, span_end - span_start)
                vocal_start = span_start + int(span * float(voiced_start_ratio))
                vocal_end = span_start + int(span * float(voiced_end_ratio))
                if vocal_end - vocal_start >= 700:
                    line["originalStartMs"] = span_start
                    line["originalEndMs"] = span_end
                    line["startMs"] = vocal_start
                    line["endMs"] = vocal_end
                    line["durationMs"] = vocal_end - vocal_start
                    duration_ms = int(line["durationMs"])
                    duration = max(0.1, duration_ms / 1000.0)
                    line["melodyCleanupReason"] = "vad_aligned"
            except Exception:
                pass

        _, duration_max = _duration_char_window(duration_ms)
        phoneme_density = source_count * 2.0 / duration
        char_density = source_count / duration
        forced_max = duration_max
        reason = ""
        if duration_ms < 1200 and source_count > 4:
            forced_max = min(forced_max, 3 if duration_ms <= 1000 else 4)
            reason = "short_dense_lrc"
        elif phoneme_density > 6.4 or char_density > 3.2:
            # For longer sung lines, density should guide generation but not
            # hard-cap the lyric length too aggressively. Otherwise the model
            # is forced to cram words and the vocal stops sounding sung.
            if duration_ms >= 3000:
                forced_max = max(forced_max, min(24, source_count + max(2, int(duration * 1.8))))
            else:
                forced_max = min(forced_max, max(2, int(duration * 2.4)))
            reason = "high_phoneme_density"

        if forced_max < source_count:
            line["lyricMaxChars"] = max(1, forced_max)
            line["lyricTargetChars"] = max(1, min(source_count, forced_max))
            line["melodyCleanupReason"] = reason
        cleaned.append(line)

    if len(cleaned) < max(3, len(lines) // 3):
        return lines
    for index, line in enumerate(cleaned, 1):
        line["index"] = index
    return cleaned


def _parse_lrc_timed_lines(raw_text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw_line in (raw_text or "").splitlines():
        text = re.sub(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]", "", raw_line).strip()
        if not text or text.startswith("[") or _is_non_sung_lrc_text(text):
            continue
        for minute, second, fraction in re.findall(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]", raw_line):
            fraction_ms = int(((fraction or "0") + "00")[:3])
            items.append({"startMs": int(minute) * 60000 + int(second) * 1000 + fraction_ms, "text": text})
    seen: set[tuple[int, str]] = set()
    result: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda value: int(value["startMs"])):
        key = (int(item["startMs"]), str(item["text"]))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _is_non_sung_lrc_text(text: str) -> bool:
    clean = re.sub(r"\s+", "", text or "")
    if not clean:
        return True
    credit_words = "词|曲|作词|作曲|编曲|和声|混音|母带|统筹|监制|制作|制作人|录音|吉他|贝斯|鼓|弦乐|发行|出品|企划|宣发|OP|SP|CP|ISRC|版权|联合出品|音乐制作|配唱制作|录音棚|混音棚|母带棚"
    credit_patterns = (
        rf"^({credit_words})[:：]",
        rf"^({credit_words})[\u4e00-\u9fffA-Za-z0-9·•+\-&_/]{{1,24}}$",
        r"^(原唱|翻唱|演唱|歌手|作词人|作曲人)[:：]",
        r"^\d{4}.*(版权|公司|文化|音乐)",
    )
    if any(re.search(pattern, clean, flags=re.IGNORECASE) for pattern in credit_patterns):
        return True
    if " - " in text and len(clean) <= 24:
        return True
    if clean.startswith(("OP：", "SP：", "OP:", "SP:")):
        return True
    return False


def _filter_reference_timed_lines(timed_lines: list[dict[str, Any]], source_title: str) -> list[dict[str, Any]]:
    title_clean = _clean_lyric_text(source_title)
    filtered: list[dict[str, Any]] = []
    for item in timed_lines:
        text = str(item.get("text") or "").strip()
        clean = _clean_lyric_text(text)
        start_ms = int(item.get("startMs") or 0)
        if not clean:
            continue
        if _is_non_sung_lrc_text(text) or _is_non_sung_lrc_text(clean):
            continue
        # Many embedded LRC files put the song title at 00:00 before the real
        # vocal starts. Treat that as metadata so every generated line is not
        # shifted by one phrase.
        if title_clean and start_ms <= 3000 and clean == title_clean:
            continue
        filtered.append(item)
    return filtered


def _chinese_len(value: str) -> int:
    return len(re.sub(r"[\s，。,.!?！？；;：:、]+", "", value or ""))


def _section_name(index: int, total: int) -> str:
    if total <= 8:
        return "Verse"
    ratio = index / max(1, total - 1)
    if ratio < 0.10:
        return "Intro"
    if ratio < 0.30:
        return "Verse 1"
    if ratio < 0.42:
        return "Pre Chorus"
    if ratio < 0.58:
        return "Chorus"
    if ratio < 0.75:
        return "Verse 2"
    if ratio < 0.88:
        return "Bridge"
    return "Final Chorus"


def _section_name_for_lrc(index: int, total: int, text: str) -> str:
    clean = re.sub(r"\s+", "", text or "")
    if clean.startswith(("我要的不是", "等雪停以后", "雪停以后")):
        return "Chorus" if index < total * 0.85 else "Final Chorus"
    return _section_name(index, total)


def _rhyme_slot(index: int, total: int) -> bool:
    return (index + 1) % 2 == 0 or index + 1 == total or _section_name(index, total) in {"Chorus", "Final Chorus"}


def _rhyme_slot_for_section(index: int, total: int, section: str) -> bool:
    return (index + 1) % 2 == 0 or index + 1 == total or section in {"Chorus", "Final Chorus"}


def _summarize_sections(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for section in dict.fromkeys(str(line.get("section") or "Verse") for line in lines):
        section_lines = [line for line in lines if line.get("section") == section]
        if not section_lines:
            continue
        summaries.append(
            {
                "name": section,
                "lineCount": len(section_lines),
                "avgChars": round(sum(int(line.get("charCount") or 0) for line in section_lines) / len(section_lines), 1),
                "avgDurationMs": int(sum(int(line.get("durationMs") or 0) for line in section_lines) / len(section_lines)),
            }
        )
    return summaries


def _prepare_vocal_reference_for_analysis(source_path: Path, job_dir: Path) -> Path:
    if not settings.melody_use_vocal_separation:
        return source_path
    stems_root = job_dir / "melody_stems"
    try:
        result = subprocess.run(
            [
                str(settings.seedvc_python),
                "-m",
                "demucs",
                "--two-stems",
                "vocals",
                "-n",
                settings.demucs_model,
                "-o",
                str(stems_root),
                str(source_path),
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=900,
        )
        if result.returncode == 0:
            vocal_path = stems_root / settings.demucs_model / source_path.stem / "vocals.wav"
            if vocal_path.exists():
                return vocal_path
    except Exception as exc:
        _log_exception("melody vocal separation skipped", exc)
    return source_path


def _extract_line_pitch_features(audio_path: Path, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    backend = (settings.melody_pitch_backend or "rmvpe").strip().lower()
    strict = bool(settings.strict_melody_backend_enabled)
    candidates: list[str]
    if backend in {"parselmouth", "praat"}:
        candidates = ["parselmouth"]
        if not strict:
            candidates.append("autocorrelation")
    elif backend in {"crepe"}:
        candidates = ["crepe"]
        if not strict:
            candidates.extend(["parselmouth", "autocorrelation"])
    else:
        candidates = ["rmvpe", "crepe"]
        if not strict:
            candidates.extend(["parselmouth", "autocorrelation"])

    last_error = ""
    for candidate in candidates:
        features = None
        if candidate == "rmvpe":
            features = _extract_line_pitch_features_with_rmvpe(audio_path, lines)
        elif candidate == "crepe":
            features = _extract_line_pitch_features_with_crepe(audio_path, lines)
        elif candidate == "parselmouth":
            features = _extract_line_pitch_features_with_parselmouth(audio_path, lines)
        else:
            features = _extract_line_pitch_features_with_autocorrelation(audio_path, lines)
        if features is None:
            last_error = f"{candidate} unavailable"
            continue
        if strict and candidate in {"rmvpe", "crepe"} and not _pitch_features_are_strict_enough(features):
            last_error = f"{candidate} pitch quality below strict threshold"
            continue
        return features

    if strict:
        raise RuntimeError(last_error or f"strict melody pitch backend failed for {audio_path.name}")
    return [_empty_pitch_feature(line, "pitch_backend_unavailable") for line in lines]


def _extract_line_pitch_features_with_rmvpe(audio_path: Path, lines: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    runtime_dir = _svs_acoustic_runtime_dir()
    model_path = runtime_dir / "checkpoints" / "rmvpe" / "model.pt"
    if not model_path.exists():
        return None
    python_exe = settings.melody_svs_python if settings.melody_svs_python.exists() else Path(sys.executable)
    script = (
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "import numpy as np\n"
        "audio_path = Path(sys.argv[1])\n"
        "ffmpeg = sys.argv[2]\n"
        "runtime_dir = Path(sys.argv[3])\n"
        "model_path = Path(sys.argv[4])\n"
        "sys.path.insert(0, str(runtime_dir))\n"
        "from modules.pe.rmvpe import RMVPE\n"
        "result = subprocess.run([ffmpeg, '-hide_banner', '-nostats', '-i', str(audio_path), '-ac', '1', '-ar', '16000', '-f', 'f32le', '-'], capture_output=True)\n"
        "if result.returncode != 0 or not result.stdout:\n"
        "    raise RuntimeError('ffmpeg audio decode failed')\n"
        "waveform = np.frombuffer(result.stdout, dtype=np.float32)\n"
        "extractor = RMVPE(str(model_path))\n"
        "length = max(1, int(round(len(waveform) / 16000.0 / 0.01)))\n"
        "f0, uv = extractor.get_pitch(waveform, 16000, length, hop_size=160)\n"
        "curve = [float(x) if float(x) > 0 else 0.0 for x in f0.tolist()]\n"
        "print(json.dumps(curve))\n"
    )
    try:
        result = subprocess.run(
            [str(python_exe), "-c", script, str(audio_path), _ffmpeg_binary(), str(runtime_dir), str(model_path)],
            text=True,
            capture_output=True,
            timeout=900,
        )
        if result.returncode != 0 or not result.stdout.strip():
            _log_exception("RMVPE pitch extraction failed", RuntimeError((result.stderr or result.stdout or "rmvpe failed").strip()[:800]))
            return None
        curve = json.loads(result.stdout or "[]")
        return _line_pitch_features_from_curve(curve, lines, "rmvpe")
    except Exception as exc:
        _log_exception("RMVPE pitch extraction failed", exc)
        return None


def _extract_line_pitch_features_with_crepe(audio_path: Path, lines: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    python_exe = settings.melody_svs_python if settings.melody_svs_python.exists() else Path(sys.executable)
    script = (
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "import numpy as np\n"
        "audio_path = Path(sys.argv[1])\n"
        "ffmpeg = sys.argv[2]\n"
        "try:\n"
        "    import torch\n"
        "    import torchcrepe\n"
        "except Exception as exc:\n"
        "    raise RuntimeError(f'crepe unavailable: {exc}')\n"
        "result = subprocess.run([ffmpeg, '-hide_banner', '-nostats', '-i', str(audio_path), '-ac', '1', '-ar', '16000', '-f', 'f32le', '-'], capture_output=True)\n"
        "if result.returncode != 0 or not result.stdout:\n"
        "    raise RuntimeError('ffmpeg audio decode failed')\n"
        "waveform = np.frombuffer(result.stdout, dtype=np.float32)\n"
        "device = 'cuda' if torch.cuda.is_available() else 'cpu'\n"
        "wav = torch.from_numpy(waveform).float().unsqueeze(0).to(device)\n"
        "f0, pd = torchcrepe.predict(wav, 16000, 60, 900, step_size=10, pad=True, model='full', batch_size=1024, device=device, return_periodicity=True)\n"
        "pd = torchcrepe.filter.median(pd, 3)\n"
        "pd = torchcrepe.threshold.Silence(-60.)(pd, wav, 16000, 80)\n"
        "f0 = torchcrepe.threshold.At(0.3)(f0, pd)\n"
        "f0 = torchcrepe.filter.mean(f0, 3)\n"
        "curve = [float(x) if float(x) > 0 else 0.0 for x in f0[0].detach().cpu().numpy().tolist()]\n"
        "print(json.dumps(curve))\n"
    )
    try:
        result = subprocess.run(
            [str(python_exe), "-c", script, str(audio_path), _ffmpeg_binary()],
            text=True,
            capture_output=True,
            timeout=1200,
        )
        if result.returncode != 0 or not result.stdout.strip():
            _log_exception("CREPE pitch extraction failed", RuntimeError((result.stderr or result.stdout or "crepe failed").strip()[:800]))
            return None
        curve = json.loads(result.stdout or "[]")
        return _line_pitch_features_from_curve(curve, lines, "crepe")
    except Exception as exc:
        _log_exception("CREPE pitch extraction failed", exc)
        return None


def _extract_line_pitch_features_with_autocorrelation(audio_path: Path, lines: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    audio = _load_mono_audio(audio_path, 16000)
    if audio is None:
        return None
    samples, sample_rate = audio
    features: list[dict[str, Any]] = []
    for line in lines:
        start = int(max(0, int(line["startMs"]) * sample_rate / 1000))
        end = int(min(len(samples), int(line["endMs"]) * sample_rate / 1000))
        features.append(_pitch_feature(samples[start:end], sample_rate, line))
    return features


def _line_pitch_features_from_curve(curve: list[float] | tuple[float, ...] | Any, lines: list[dict[str, Any]], backend: str) -> list[dict[str, Any]] | None:
    try:
        import numpy as np

        values = np.asarray(curve or [], dtype=float)
        if values.size == 0:
            return None
        features: list[dict[str, Any]] = []
        for line in lines:
            start_frame = max(0, int(round(int(line.get("startMs") or 0) / 10.0)))
            end_frame = max(start_frame + 1, int(round(int(line.get("endMs") or 0) / 10.0)))
            segment = values[start_frame:end_frame]
            feature = _pitch_feature_from_values(segment, line, backend)
            voiced_window = _voiced_window_from_curve(segment)
            if voiced_window:
                feature["voicedStartRatio"] = voiced_window[0]
                feature["voicedEndRatio"] = voiced_window[1]
            features.append(feature)
        return features
    except Exception as exc:
        _log_exception(f"{backend} curve-to-lines conversion failed", exc)
        return None


def _voiced_window_from_curve(values: Any) -> tuple[float, float] | None:
    try:
        import numpy as np

        curve = np.asarray(values or [], dtype=float)
        if curve.size < 3:
            return None
        voiced = np.where((curve >= 60) & (curve <= 900))[0]
        if voiced.size < 2:
            return None
        pad = 1 if curve.size >= 30 else 0
        start = max(0, int(voiced[0]) - pad)
        end = min(int(curve.size) - 1, int(voiced[-1]) + pad)
        if end <= start:
            return None
        return round(start / max(1, curve.size - 1), 3), round(end / max(1, curve.size - 1), 3)
    except Exception:
        return None


def _pitch_features_are_strict_enough(features: list[dict[str, Any]]) -> bool:
    if not features or len(features) < 2:
        return False
    voiced = [feature for feature in features if int(feature.get("avgF0") or 0) > 0 and float(feature.get("voicedRatio") or 0) >= 0.12]
    mean_voiced = sum(float(feature.get("voicedRatio") or 0) for feature in features) / max(1, len(features))
    rmvpe_or_crepe = [feature for feature in features if str(feature.get("pitchBackend") or "").startswith(("rmvpe", "crepe"))]
    return len(voiced) >= max(2, len(features) // 2) and mean_voiced >= 0.10 and len(rmvpe_or_crepe) >= max(1, len(features) // 2)


def _extract_line_pitch_features_with_parselmouth(audio_path: Path, lines: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    try:
        import numpy as np
        import parselmouth

        sound = parselmouth.Sound(str(audio_path))
        pitch = sound.to_pitch(time_step=0.04, pitch_floor=80, pitch_ceiling=600)
        frequencies = pitch.selected_array["frequency"]
        if hasattr(pitch, "xs"):
            times = pitch.xs()
        else:
            times = np.arange(len(frequencies), dtype=float) * float(pitch.dx) + float(pitch.xmin)
        features: list[dict[str, Any]] = []
        for line in lines:
            start_sec = int(line["startMs"]) / 1000.0
            end_sec = int(line["endMs"]) / 1000.0
            mask = (times >= start_sec) & (times <= end_sec)
            values = frequencies[mask]
            features.append(_pitch_feature_from_values(values, line, "parselmouth"))
        return features
    except Exception as exc:
        _log_exception("parselmouth pitch extraction skipped", exc)
        return None


def _load_mono_audio(audio_path: Path, sample_rate: int) -> tuple[Any, int] | None:
    try:
        result = subprocess.run(
            [
                _ffmpeg_binary(),
                "-hide_banner",
                "-nostats",
                "-i",
                str(audio_path),
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-f",
                "f32le",
                "-",
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0 or not result.stdout:
            return None
        import numpy as np

        return np.frombuffer(result.stdout, dtype=np.float32), sample_rate
    except Exception as exc:
        _log_exception("melody audio load failed", exc)
        return None


def _empty_pitch_feature(line: dict[str, Any], backend: str = "none") -> dict[str, Any]:
    duration = max(0.8, int(line.get("durationMs") or 0) / 1000.0)
    return {
        "avgF0": 0,
        "voicedRatio": 0.0,
        "pitchContour": "unknown",
        "rhythmDensity": round(int(line.get("charCount") or 0) / duration, 2),
        "pitchBackend": backend,
    }


def _pitch_feature(segment: Any, sample_rate: int, line: dict[str, Any]) -> dict[str, Any]:
    duration = max(0.8, int(line.get("durationMs") or 0) / 1000.0)
    try:
        import numpy as np

        if segment.size < sample_rate * 0.25:
            return _empty_pitch_feature(line, "autocorrelation")
        frame = int(sample_rate * 0.04)
        hop = int(sample_rate * 0.08)
        min_lag = int(sample_rate / 600)
        max_lag = int(sample_rate / 80)
        pitches: list[float] = []
        for start in range(0, max(0, len(segment) - frame), hop):
            window = segment[start : start + frame]
            rms = float(np.sqrt(np.mean(window * window))) if window.size else 0.0
            if rms < 0.01:
                continue
            window = window - float(np.mean(window))
            corr = np.correlate(window, window, mode="full")[frame - 1 :]
            if corr.size <= max_lag or corr[0] <= 0:
                continue
            search = corr[min_lag:max_lag]
            lag = int(np.argmax(search)) + min_lag
            confidence = float(search[lag - min_lag] / max(corr[0], 1e-9))
            if confidence >= 0.25:
                pitches.append(sample_rate / lag)
        if len(pitches) < 2:
            return _empty_pitch_feature(line, "autocorrelation")
        voiced_ratio = min(1.0, len(pitches) * hop / max(1, len(segment)))
        feature = _pitch_feature_from_values(pitches, line, "autocorrelation")
        feature["voicedRatio"] = round(voiced_ratio, 2)
        return feature
    except Exception as exc:
        _log_exception("pitch feature failed", exc)
        return _empty_pitch_feature(line, "autocorrelation_failed")


def _pitch_feature_from_values(values: Any, line: dict[str, Any], backend: str) -> dict[str, Any]:
    try:
        import numpy as np

        raw_pitches = np.asarray(values, dtype=float)
        timeline = _compact_f0_timeline(raw_pitches)
        pitches = raw_pitches[(raw_pitches >= 60) & (raw_pitches <= 900)]
        pitches = pitches[pitches > 0]
        if pitches.size < 2:
            empty = _empty_pitch_feature(line, backend)
            if timeline:
                empty["f0Timeline"] = timeline
            return empty
        chunk = max(1, int(pitches.size // 3))
        first = float(np.mean(pitches[:chunk]))
        last = float(np.mean(pitches[-chunk:]))
        avg = float(np.mean(pitches))
        if last > first * 1.08:
            contour = "rising"
        elif last < first * 0.92:
            contour = "falling"
        elif float(np.max(pitches)) > avg * 1.12 and first < avg and last < avg:
            contour = "arched"
        else:
            contour = "stable"
        duration = max(0.8, int(line.get("durationMs") or 0) / 1000.0)
        feature = {
            "avgF0": int(avg),
            "f0Curve": _compact_f0_curve(pitches),
            "f0Timeline": timeline,
            "voicedRatio": round(min(1.0, pitches.size * 0.04 / duration), 2),
            "pitchContour": contour,
            "rhythmDensity": round(int(line.get("charCount") or 0) / duration, 2),
            "pitchBackend": backend,
        }
        return feature
    except Exception as exc:
        _log_exception("pitch feature values failed", exc)
        return _empty_pitch_feature(line, backend + "_failed")


def _format_melody_template(template: dict[str, Any]) -> str:
    lines = template.get("lines") or []
    if not lines:
        return str(template.get("note") or "No melody template available.")
    preview = []
    for line in lines[:36]:
        preview.append(
            "line {index}: section={section}, start={start:.2f}s, dur={duration:.2f}s, chars={chars}, rhyme={rhyme}, contour={contour}, f0={f0}, density={density}, pitch={pitch}".format(
                index=line.get("index"),
                section=line.get("section"),
                start=int(line.get("startMs") or 0) / 1000.0,
                duration=int(line.get("durationMs") or 0) / 1000.0,
                chars=line.get("charCount"),
                rhyme="yes" if line.get("rhymeSlot") else "no",
                contour=line.get("pitchContour") or "unknown",
                f0=line.get("avgF0") or 0,
                density=line.get("rhythmDensity") or 0,
                pitch=line.get("pitchBackend") or "unknown",
            )
        )
    return (
        f"source={template.get('source')}; strict_melody_backend={template.get('strictMelodyBackend')}; "
        f"duration={float(template.get('durationSeconds') or 0):.1f}s\n"
        + "\n".join(preview)
    )


def _ace_step_status() -> dict[str, Any]:
    base = settings.ace_step_base_url.rstrip("/")
    if not settings.strict_melody_backend_enabled:
        return {"available": False, "reason": "strict melody backend disabled"}
    try:
        health_response = httpx.get(base + "/health", timeout=5)
        health_response.raise_for_status()
        health_payload = health_response.json()
        health_data = health_payload.get("data") if isinstance(health_payload, dict) else {}
        if isinstance(health_data, dict) and not bool(health_data.get("models_initialized")):
            return {
                "available": False,
                "url": base,
                "probe": "/health",
                "statusCode": health_response.status_code,
                "modelsInitialized": False,
                "loadedModel": str(health_data.get("loaded_model") or ""),
                "reason": "ACE-Step model is not initialized",
            }
    except Exception as exc:
        last_error = str(exc)[:160]
    chat_endpoint = base + "/v1/chat/completions"
    try:
        response = httpx.get(base + "/openapi.json", timeout=5)
        if response.status_code < 500:
            try:
                data = response.json()
                paths = data.get("paths") if isinstance(data, dict) else {}
                has_chat = isinstance(paths, dict) and "/v1/chat/completions" in paths
            except Exception:
                has_chat = False
            return {
                "available": bool(has_chat),
                "url": base,
                "probe": "/openapi.json",
                "statusCode": response.status_code,
                "chatCompletions": bool(has_chat),
                "generationEndpoint": chat_endpoint if has_chat else "",
                "modelsInitialized": True,
                "model": settings.ace_step_model,
                "reason": "" if has_chat else "ACE-Step generation endpoint /v1/chat/completions is missing",
            }
    except Exception as exc:
        last_error = str(exc)[:160]
    for path in ("/health", "/", "/docs"):
        try:
            response = httpx.get(base + path, timeout=5)
            if response.status_code < 500:
                return {
                    "available": False,
                    "url": base,
                    "probe": path,
                    "statusCode": response.status_code,
                    "chatCompletions": False,
                    "reason": "ACE-Step health is reachable but generation endpoint was not confirmed",
                }
        except Exception as exc:
            last_error = str(exc)[:160]
    return {"available": False, "url": base, "reason": last_error if "last_error" in locals() else "not reachable"}


def _public_ai_service_error(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "AI 创作服务响应超时，请稍后再试"
    if "connection" in lowered or "not reachable" in lowered or "unavailable" in lowered:
        return "AI 创作服务维护中，请稍后再试"
    if "disk" in lowered or "space" in lowered or "no space" in lowered:
        return "AI 创作临时空间不足，请稍后再试"
    if "cuda" in lowered or "out of memory" in lowered or "显存" in text:
        return "AI 创作资源繁忙，请稍后再试"
    return "AI 创作服务暂时不可用，请稍后再试"


def _cleanup_ace_step_gpu() -> None:
    if not settings.ace_step_cleanup_after_job:
        return
    base = settings.ace_step_base_url.rstrip("/")
    try:
        with httpx.Client(timeout=20) as client:
            client.post(base + "/v1/gpu/cleanup")
    except Exception as exc:
        _log_exception("ACE-Step GPU cleanup skipped", exc)


def _cleanup_generation_gpu(job_dir: Path, job_id: str) -> None:
    before = _nvidia_smi_status()
    errors: list[str] = []
    for name, action in (
        ("ace_step", _cleanup_ace_step_gpu),
        ("svs_python", lambda: _cleanup_torch_cuda(settings.melody_svs_python if settings.melody_svs_python.exists() else Path(sys.executable))),
        ("service_python", lambda: _cleanup_torch_cuda(Path(sys.executable))),
    ):
        try:
            action()
        except Exception as exc:
            errors.append(f"{name}: {str(exc)[:160]}")
            _log_exception(f"GPU cleanup {name} failed", exc)
    after = _nvidia_smi_status()
    try:
        payload = {
            "jobId": job_id,
            "cleanedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "before": before,
            "after": after,
            "errors": errors,
        }
        (job_dir / "gpu_cleanup.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _melody_svs_quick_status() -> dict[str, Any]:
    if _is_openutau_onnx_backend():
        return _openutau_onnx_svs_status(full=False)
    root = settings.melody_svs_root
    try:
        disk = shutil.disk_usage(root if root.exists() else root.parent)
        disk_info = {
            "totalGb": round(disk.total / 1024**3, 2),
            "freeGb": round(disk.free / 1024**3, 2),
            "ok": disk.free >= 60 * 1024**3,
            "recommendedFreeGb": 60,
        }
    except Exception:
        disk_info = {"totalGb": 0, "freeGb": 0, "ok": False, "recommendedFreeGb": 60}
    runtime_root = _svs_acoustic_runtime_dir(root)
    vocoder_runtime = _svs_vocoder_runtime_dir(root)
    acoustic_cli = _svs_acoustic_cli(runtime_root)
    vocoder_script = vocoder_runtime / "scripts" / "vocode.py"
    ffmpeg_ok = _ffmpeg_available()
    runtime_status = {
        "openvpiDiffSinger": runtime_root.exists(),
        "inferScript": acoustic_cli["path"].exists(),
        "inferCli": bool(acoustic_cli["kind"]) and acoustic_cli["path"].exists(),
        "inferKind": acoustic_cli["kind"],
        "vocoderRuntime": vocoder_runtime.exists(),
        "vocoderScript": vocoder_script.exists(),
    }
    model_status = _svs_model_status(root, quick=True)
    missing: list[str] = []
    warnings: list[str] = []
    if not ffmpeg_ok:
        missing.append("ffmpeg")
    if not runtime_status["openvpiDiffSinger"]:
        missing.append("OpenVPI DiffSinger runtime")
    elif not runtime_status["inferCli"]:
        missing.append("OpenVPI DiffSinger inference CLI")
    if not runtime_status["vocoderRuntime"] or not runtime_status["vocoderScript"]:
        missing.append("OpenVPI DiffSinger vocoder CLI")
    if not model_status.get("acousticReady"):
        missing.append("OpenVPI DiffSinger acoustic checkpoint")
    if not model_status.get("vocoderReady"):
        missing.append("OpenVPI DiffSinger vocoder checkpoint")
    if settings.melody_svs_required and not model_status.get("varianceReady"):
        missing.append("OpenVPI DiffSinger variance checkpoint")
    pitch_status = _melody_pitch_status(root)
    if settings.strict_melody_backend_enabled and not pitch_status.get("rmvpeReady") and not pitch_status.get("crepeReady"):
        missing.append("RMVPE checkpoint or CREPE runtime")
    if not model_status.get("pypinyin"):
        missing.append("pypinyin in SVS Python environment")
    if not disk_info["ok"]:
        warnings.append("SVS disk free space is below 60GB")
    ready = bool(settings.melody_svs_enabled) and not missing
    return {
        "enabled": bool(settings.melody_svs_enabled),
        "required": bool(settings.melody_svs_required),
        "backend": settings.melody_svs_backend,
        "root": str(root),
        "python": str(settings.melody_svs_python),
        "config": {
            "runtimeDir": str(runtime_root),
            "vocoderRuntimeDir": str(vocoder_runtime),
            "vocoderCkpt": str(_svs_vocoder_ckpt_path(root)),
            "vocoderClass": settings.melody_svs_vocoder_class,
            "acousticExp": settings.melody_svs_acoustic_exp,
            "varianceExp": settings.melody_svs_variance_exp,
            "variancePredict": settings.melody_svs_variance_predict,
            "speaker": settings.melody_svs_speaker,
            "language": settings.melody_svs_language,
            "steps": settings.melody_svs_steps,
        },
        "disk": disk_info,
        "ffmpeg": ffmpeg_ok,
        "runtime": runtime_status,
        "models": model_status,
        "pitch": pitch_status,
        "missing": missing,
        "warnings": warnings,
        "ready": ready,
        "reason": "" if ready else "missing: " + ", ".join(missing),
        "fullCheck": "/health?full=true",
    }


def _melody_svs_status() -> dict[str, Any]:
    if _is_openutau_onnx_backend():
        return _openutau_onnx_svs_status(full=True)
    root = settings.melody_svs_root
    root.mkdir(parents=True, exist_ok=True)
    for child in ("models", "runtime", "outputs", "logs"):
        (root / child).mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(root)
    status: dict[str, Any] = {
        "enabled": bool(settings.melody_svs_enabled),
        "required": bool(settings.melody_svs_required),
        "backend": settings.melody_svs_backend,
        "root": str(root),
        "python": str(settings.melody_svs_python),
        "config": {
            "runtimeDir": str(_svs_acoustic_runtime_dir(root)),
            "vocoderRuntimeDir": str(_svs_vocoder_runtime_dir(root)),
            "vocoderCkpt": str(_svs_vocoder_ckpt_path(root)),
            "vocoderClass": settings.melody_svs_vocoder_class,
            "acousticExp": settings.melody_svs_acoustic_exp,
            "varianceExp": settings.melody_svs_variance_exp,
            "variancePredict": settings.melody_svs_variance_predict,
            "speaker": settings.melody_svs_speaker,
            "language": settings.melody_svs_language,
            "steps": settings.melody_svs_steps,
        },
        "disk": {
            "totalGb": round(disk.total / 1024**3, 2),
            "freeGb": round(disk.free / 1024**3, 2),
            "ok": disk.free >= 60 * 1024**3,
            "recommendedFreeGb": 60,
        },
        "ffmpeg": _ffmpeg_available(),
        "gpu": _nvidia_smi_status(),
        "torch": _svs_torch_status(),
        "runtime": _svs_runtime_status(root),
        "models": _svs_model_status(root),
        "pitch": _melody_pitch_status(root),
    }
    missing: list[str] = []
    warnings: list[str] = []
    if not status["ffmpeg"]:
        missing.append("ffmpeg")
    if not status["gpu"].get("available"):
        missing.append("nvidia-smi/GPU")
    elif int(status["gpu"].get("freeMb") or 0) < 8192:
        warnings.append("GPU free VRAM is below 8GB")
    if not status["torch"].get("installed"):
        missing.append("torch")
    elif not status["torch"].get("cudaAvailable"):
        missing.append("CUDA-enabled PyTorch")
    if not status["runtime"].get("openvpiDiffSinger"):
        missing.append("OpenVPI DiffSinger runtime")
    elif not status["runtime"].get("inferCli"):
        missing.append("OpenVPI DiffSinger inference CLI")
    if not status["runtime"].get("vocoderScript"):
        missing.append("OpenVPI DiffSinger vocoder CLI")
    if not status["models"].get("acousticReady"):
        missing.append("OpenVPI DiffSinger acoustic checkpoint")
    if not status["models"].get("vocoderReady"):
        missing.append("OpenVPI DiffSinger vocoder checkpoint")
    if settings.melody_svs_required and not status["models"].get("varianceReady"):
        missing.append("OpenVPI DiffSinger variance checkpoint")
    if not status["models"].get("pypinyin"):
        missing.append("pypinyin in SVS Python environment")
    status["pitch"] = _melody_pitch_status(root)
    if settings.strict_melody_backend_enabled and not status["pitch"].get("rmvpeReady") and not status["pitch"].get("crepeReady"):
        missing.append("RMVPE checkpoint or CREPE runtime")
    if not status["disk"]["ok"]:
        warnings.append("SVS disk free space is below 60GB")
    status["missing"] = missing
    status["warnings"] = warnings
    status["ready"] = not missing
    status["reason"] = "" if status["ready"] else "missing: " + ", ".join(missing)
    return status


def _svs_model_status(root: Path, quick: bool = False) -> dict[str, Any]:
    runtime_dir = _svs_acoustic_runtime_dir(root)
    checkpoints_dir = runtime_dir / "checkpoints"
    acoustic_exp = _resolve_svs_exp(checkpoints_dir, settings.melody_svs_acoustic_exp)
    variance_exp = _resolve_svs_exp(checkpoints_dir, settings.melody_svs_variance_exp) if settings.melody_svs_variance_exp else None
    vocoder_ckpt = _svs_vocoder_ckpt_path(root)
    vocoder_config = vocoder_ckpt.with_name("config.json")
    pypinyin_ok = False
    if not quick:
        python_exe = settings.melody_svs_python if settings.melody_svs_python.exists() else Path(sys.executable)
        try:
            result = subprocess.run(
                [str(python_exe), "-c", "import pypinyin; print('ok')"],
                text=True,
                capture_output=True,
                timeout=10,
            )
            pypinyin_ok = result.returncode == 0
        except Exception:
            pypinyin_ok = False
    return {
        "checkpointsDir": str(checkpoints_dir),
        "acousticExp": acoustic_exp["name"],
        "acousticPath": acoustic_exp["path"],
        "acousticReady": bool(acoustic_exp["ready"]),
        "varianceExp": variance_exp["name"] if variance_exp else "",
        "variancePath": variance_exp["path"] if variance_exp else "",
        "varianceReady": bool(variance_exp and variance_exp["ready"]),
        "vocoderCkpt": str(vocoder_ckpt),
        "vocoderConfig": str(vocoder_config),
        "vocoderReady": vocoder_ckpt.exists() and vocoder_config.exists(),
        "pypinyin": True if quick else pypinyin_ok,
        "candidates": [] if quick else _list_svs_checkpoint_candidates(checkpoints_dir),
    }


def _is_openutau_onnx_backend() -> bool:
    return settings.melody_svs_backend.strip().lower() in {"openutau-onnx", "openutau-onnx-diffsinger", "qixuan-onnx"}


def _openutau_onnx_svs_status(full: bool = False) -> dict[str, Any]:
    root = settings.melody_svs_root
    model_dir = settings.melody_svs_onnx_model_dir
    try:
        disk = shutil.disk_usage(root if root.exists() else root.parent)
        disk_info = {
            "totalGb": round(disk.total / 1024**3, 2),
            "freeGb": round(disk.free / 1024**3, 2),
            "ok": disk.free >= 60 * 1024**3,
            "recommendedFreeGb": 60,
        }
    except Exception:
        disk_info = {"totalGb": 0, "freeGb": 0, "ok": False, "recommendedFreeGb": 60}

    required_files = {
        "rootConfig": model_dir / "dsconfig.yaml",
        "acoustic": model_dir / "0101_qixuan_muon1_acoustic.qixuan.onnx",
        "acousticPhonemes": model_dir / "0101_qixuan_muon1_acoustic.phonemes.json",
        "acousticLanguages": model_dir / "0101_qixuan_muon1_acoustic.languages.json",
        "pitchLinguistic": model_dir / "dspitch" / "0102_qixuan_muon1_pitch.qixuan.linguistic.onnx",
        "pitch": model_dir / "dspitch" / "0102_qixuan_muon1_pitch.qixuan.pitch.onnx",
        "varianceLinguistic": model_dir / "dsvariance" / "0102_qixuan_muon1_multivar.qixuan.linguistic.onnx",
        "variance": model_dir / "dsvariance" / "0102_qixuan_muon1_multivar.qixuan.variance.onnx",
        "vocoder": model_dir / "dsvocoder" / "pc_nsf_hifigan_44.1k_hop512_128bin_2025.02.onnx",
    }
    missing = [name for name, path in required_files.items() if not path.exists()]
    warnings: list[str] = []
    if not disk_info["ok"]:
        warnings.append("SVS disk free space is below 60GB")

    runtime_status: dict[str, Any] = {"onnxruntime": False, "providers": [], "sessionsLoad": None}
    try:
        python_exe = settings.melody_svs_python if settings.melody_svs_python.exists() else Path(sys.executable)
        code = (
            "import json, onnxruntime as ort, pathlib\n"
            "root=pathlib.Path(r'%s')\n"
            "files=['0101_qixuan_muon1_acoustic.qixuan.onnx','dspitch/0102_qixuan_muon1_pitch.qixuan.linguistic.onnx','dspitch/0102_qixuan_muon1_pitch.qixuan.pitch.onnx','dsvariance/0102_qixuan_muon1_multivar.qixuan.linguistic.onnx','dsvariance/0102_qixuan_muon1_multivar.qixuan.variance.onnx','dsvocoder/pc_nsf_hifigan_44.1k_hop512_128bin_2025.02.onnx']\n"
            "available=ort.get_available_providers()\n"
            "out={'onnxruntime': True, 'providers': available, 'selectedProvider': None, 'sessionsLoad': None}\n"
            "if %s:\n"
            "  so=ort.SessionOptions(); so.graph_optimization_level=ort.GraphOptimizationLevel.ORT_DISABLE_ALL; so.intra_op_num_threads=1; so.inter_op_num_threads=1\n"
            "  candidates=[]\n"
            "  if 'CUDAExecutionProvider' in available: candidates.append(['CUDAExecutionProvider','CPUExecutionProvider'])\n"
            "  candidates.append(['CPUExecutionProvider'])\n"
            "  last_error=''\n"
            "  for selected in candidates:\n"
            "    try:\n"
            "      for rel in files:\n"
            "        ort.InferenceSession(str(root/rel), sess_options=so, providers=selected)\n"
            "      out['selectedProvider']=selected[0]\n"
            "      out['sessionsLoad']=True\n"
            "      break\n"
            "    except Exception as exc:\n"
            "      last_error=str(exc)[-500:]\n"
            "  if out['sessionsLoad'] is not True:\n"
            "    out['selectedProvider']=None\n"
            "    out['error']=last_error\n"
            "print(json.dumps(out))\n"
        ) % (str(model_dir).replace("\\", "\\\\"), "True" if full and not missing else "False")
        result = subprocess.run([str(python_exe), "-c", code], text=True, capture_output=True, timeout=45)
        if result.returncode == 0:
            runtime_status.update(json.loads(result.stdout or "{}"))
        else:
            missing.append("onnxruntime model load")
            runtime_status["error"] = (result.stderr or result.stdout).strip()[-500:]
    except Exception as exc:
        missing.append("onnxruntime")
        runtime_status["error"] = str(exc)

    ffmpeg_ok = _ffmpeg_available()
    if not ffmpeg_ok:
        missing.append("ffmpeg")
    pitch_status = _melody_pitch_status(root)
    if settings.strict_melody_backend_enabled and not pitch_status.get("rmvpeReady") and not pitch_status.get("crepeReady"):
        missing.append("RMVPE checkpoint or CREPE runtime")
    ready = bool(settings.melody_svs_enabled) and not missing
    return {
        "enabled": bool(settings.melody_svs_enabled),
        "required": bool(settings.melody_svs_required),
        "backend": settings.melody_svs_backend,
        "root": str(root),
        "python": str(settings.melody_svs_python),
        "config": {
            "onnxModelDir": str(model_dir),
            "speaker": settings.melody_svs_speaker,
            "language": settings.melody_svs_language,
            "steps": settings.melody_svs_steps,
            "noteGraphEnabled": bool(settings.melody_svs_note_graph_enabled),
            "noteEventsEnabled": bool(settings.melody_svs_note_events_enabled),
            "forceReferenceF0": bool(settings.melody_svs_force_reference_f0),
        },
        "disk": disk_info,
        "ffmpeg": ffmpeg_ok,
        "gpu": _nvidia_smi_status() if full else {},
        "torch": _svs_torch_status() if full else {},
        "runtime": runtime_status,
        "models": {
            "onnxDir": str(model_dir),
            "requiredFiles": {k: str(v) for k, v in required_files.items()},
            "varianceReady": required_files["variance"].exists() and required_files["varianceLinguistic"].exists(),
        },
        "pitch": pitch_status,
        "missing": missing,
        "warnings": warnings,
        "ready": ready,
        "reason": "" if ready else "missing: " + ", ".join(missing),
        "fullCheck": "/health?full=true",
    }


def _melody_pitch_status(root: Path | None = None) -> dict[str, Any]:
    root = root or settings.melody_svs_root
    runtime_dir = _svs_acoustic_runtime_dir(root)
    rmvpe_path = runtime_dir / "checkpoints" / "rmvpe" / "model.pt"
    if not rmvpe_path.exists():
        rmvpe_path = root / "runtime" / "OpenVPI-DiffSinger" / "checkpoints" / "rmvpe" / "model.pt"
    python_exe = settings.melody_svs_python if settings.melody_svs_python.exists() else Path(sys.executable)
    crepe_ready = False
    try:
        result = subprocess.run(
            [str(python_exe), "-c", "import torchcrepe; print('ok')"],
            text=True,
            capture_output=True,
            timeout=10,
        )
        crepe_ready = result.returncode == 0
    except Exception:
        crepe_ready = False
    return {
        "default": "rmvpe",
        "fallback": "crepe",
        "rmvpeReady": rmvpe_path.exists(),
        "rmvpePath": str(rmvpe_path),
        "crepeReady": crepe_ready,
        "strict": bool(settings.strict_melody_backend_enabled),
    }


def _resolve_svs_exp(checkpoints_dir: Path, configured: str) -> dict[str, Any]:
    configured = (configured or "").strip()
    candidates = _list_svs_checkpoint_candidates(checkpoints_dir)
    if configured:
        exact = checkpoints_dir / configured
        if exact.exists() and _checkpoint_dir_has_model(exact):
            return {"name": exact.name, "path": str(exact), "ready": True}
        for item in candidates:
            name = str(item.get("name") or "")
            if name.startswith(configured):
                return {"name": name, "path": str(item.get("path") or ""), "ready": True}
        return {"name": configured, "path": str(exact), "ready": False}
    if candidates:
        first = candidates[0]
        return {"name": str(first.get("name") or ""), "path": str(first.get("path") or ""), "ready": True}
    return {"name": "", "path": str(checkpoints_dir), "ready": False}


def _list_svs_checkpoint_candidates(checkpoints_dir: Path) -> list[dict[str, Any]]:
    if not checkpoints_dir.exists():
        return []
    items: list[dict[str, Any]] = []
    try:
        for child in sorted(checkpoints_dir.iterdir(), key=lambda path: path.name.lower()):
            if child.is_dir() and _checkpoint_dir_has_model(child):
                items.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "files": [path.name for path in child.iterdir() if path.is_file()][:12],
                    }
                )
    except Exception:
        return []
    return items


def _checkpoint_dir_has_model(path: Path) -> bool:
    try:
        if not path.exists() or not path.is_dir():
            return False
        has_config = any(child.suffix.lower() in {".yaml", ".yml", ".json"} for child in path.iterdir() if child.is_file())
        has_weight = any(child.suffix.lower() in {".ckpt", ".pt", ".pth", ".safetensors"} for child in path.iterdir() if child.is_file())
        return has_config and has_weight
    except Exception:
        return False


def _svs_runtime_status(root: Path) -> dict[str, Any]:
    runtime_dir = _svs_acoustic_runtime_dir(root)
    infer_cli = _svs_acoustic_cli(runtime_dir)
    infer_script = infer_cli["path"]
    vocoder_runtime = _svs_vocoder_runtime_dir(root)
    vocoder_script = vocoder_runtime / "scripts" / "vocode.py"
    data = {
        "openvpiDiffSinger": runtime_dir.exists(),
        "path": str(runtime_dir),
        "inferScript": infer_script.exists(),
        "inferKind": infer_cli["kind"],
        "inferCli": False,
        "vocoderRuntime": vocoder_runtime.exists(),
        "vocoderRuntimePath": str(vocoder_runtime),
        "vocoderScript": vocoder_script.exists(),
    }
    if not infer_script.exists():
        return data
    python_exe = settings.melody_svs_python if settings.melody_svs_python.exists() else Path(sys.executable)
    help_command = [str(python_exe), str(infer_script), "--help"]
    try:
        result = subprocess.run(
            help_command,
            cwd=str(runtime_dir),
            text=True,
            capture_output=True,
            timeout=20,
        )
        raw = (result.stdout or result.stderr or "").strip()
        data["inferCli"] = result.returncode == 0 and ("Commands:" in raw or "Run DiffSinger inference" in raw)
        data["probe"] = raw[:300]
    except Exception as exc:
        data["error"] = str(exc)[:300]
    return data


def _svs_acoustic_runtime_dir(root: Path | None = None) -> Path:
    root = root or settings.melody_svs_root
    configured = settings.melody_svs_runtime_dir.strip()
    if configured:
        return Path(configured)
    legacy = root / "runtime" / "OpenVPI-DiffSinger-v1.7.0"
    if legacy.exists():
        return legacy
    return root / "runtime" / "OpenVPI-DiffSinger"


def _svs_vocoder_runtime_dir(root: Path | None = None) -> Path:
    root = root or settings.melody_svs_root
    configured = settings.melody_svs_vocoder_runtime_dir.strip()
    if configured:
        return Path(configured)
    return root / "runtime" / "OpenVPI-DiffSinger"


def _svs_acoustic_cli(runtime_dir: Path) -> dict[str, Any]:
    new_cli = runtime_dir / "scripts" / "infer.py"
    if new_cli.exists():
        return {"kind": "openvpi-current", "path": new_cli}
    legacy_cli = runtime_dir / "main.py"
    if legacy_cli.exists():
        return {"kind": "openvpi-v1.7", "path": legacy_cli}
    return {"kind": "", "path": new_cli}


def _svs_vocoder_ckpt_path(root: Path | None = None) -> Path:
    root = root or settings.melody_svs_root
    configured = settings.melody_svs_vocoder_ckpt.strip()
    if configured:
        return Path(configured)
    return (
        root
        / "runtime"
        / "OpenVPI-DiffSinger"
        / "checkpoints"
        / "pc_nsf_hifigan_44.1k_hop512_128bin_2025.02"
        / "model.ckpt"
    )


def _nvidia_smi_status() -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,driver_version",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {"available": False, "error": (result.stderr or result.stdout or "").strip()[:300]}
        parts = [item.strip() for item in result.stdout.splitlines()[0].split(",")]
        return {
            "available": True,
            "name": parts[0] if len(parts) > 0 else "",
            "totalMb": int(float(parts[1])) if len(parts) > 1 else 0,
            "freeMb": int(float(parts[2])) if len(parts) > 2 else 0,
            "driver": parts[3] if len(parts) > 3 else "",
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)[:300]}


def _svs_torch_status() -> dict[str, Any]:
    python_exe = settings.melody_svs_python if settings.melody_svs_python.exists() else Path(sys.executable)
    code = (
        "import json\n"
        "try:\n"
        " import torch\n"
        " data={'installed':True,'version':getattr(torch,'__version__',''),'cudaAvailable':bool(torch.cuda.is_available())}\n"
        " data['device']=torch.cuda.get_device_name(0) if data['cudaAvailable'] else ''\n"
        " print(json.dumps(data))\n"
        "except Exception as exc:\n"
        " print(json.dumps({'installed':False,'error':str(exc)}))\n"
    )
    try:
        result = subprocess.run([str(python_exe), "-c", code], text=True, capture_output=True, timeout=20)
        raw = (result.stdout or result.stderr or "").strip()
        return json.loads(raw.splitlines()[-1]) if raw else {"installed": False, "error": "empty torch probe"}
    except Exception as exc:
        return {"installed": False, "error": str(exc)[:300]}


def _generate_with_ace_step_cover(source_path: Path, lyrics: str, caption: str, title: str, job_dir: Path) -> bytes:
    status = _ace_step_status()
    if not status.get("available"):
        raise RuntimeError(f"ACE-Step service unavailable: {status.get('reason') or status}")
    prompt = _ace_step_prompt(caption, title)
    lyric_text = _strip_lyric_sections(lyrics)
    duration = min(float(settings.max_duration_seconds), max(30.0, float(_ffprobe_audio(source_path).get("duration") or 0.0)))
    output_dir = job_dir / "ace_step"
    output_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    try:
        result = _call_ace_step_official(source_path, lyric_text, prompt, duration)
        if result:
            _cleanup_ace_step_gpu()
            return result
    except Exception as exc:
        errors.append(f"official-api: {str(exc)[:180]}")

    for endpoint in ("/api/generate", "/generate", "/api/cover", "/cover"):
        try:
            result = _call_ace_step_rest(endpoint, source_path, lyric_text, prompt, duration, output_dir)
            if result:
                _cleanup_ace_step_gpu()
                return result
        except Exception as exc:
            errors.append(f"{endpoint}: {str(exc)[:160]}")

    try:
        result = _call_ace_step_gradio(source_path, lyric_text, prompt, duration, output_dir)
        if result:
            _cleanup_ace_step_gpu()
            return result
    except Exception as exc:
        errors.append(f"gradio: {str(exc)[:160]}")

    _cleanup_ace_step_gpu()
    raise RuntimeError("ACE-Step cover failed; " + " | ".join(errors[:1] + errors[-5:]))


def _generate_with_ace_step_original(lyrics: str, caption: str, title: str, job_dir: Path) -> bytes:
    status = _ace_step_status()
    if not status.get("available"):
        raise RuntimeError(f"ACE-Step service unavailable: {status.get('reason') or status}")
    prompt = _ace_step_original_prompt(caption, title)
    lyric_text = _strip_lyric_sections(lyrics)
    duration = min(float(settings.max_duration_seconds), 180.0)
    output_dir = job_dir / "ace_step"
    output_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    try:
        result = _call_ace_step_text_official(lyric_text, prompt, duration)
        if result:
            _cleanup_ace_step_gpu()
            return result
    except Exception as exc:
        errors.append(f"official-api: {str(exc)[:180]}")
    for endpoint in ("/api/generate", "/generate"):
        try:
            result = _call_ace_step_text_rest(endpoint, lyric_text, prompt, duration, output_dir)
            if result:
                _cleanup_ace_step_gpu()
                return result
        except Exception as exc:
            errors.append(f"{endpoint}: {str(exc)[:160]}")
    _cleanup_ace_step_gpu()
    raise RuntimeError("ACE-Step original generation failed; " + " | ".join(errors[:1] + errors[-4:]))


def _generate_with_melody_svs(source_path: Path, lyrics: str, title: str, template: dict[str, Any], job_dir: Path) -> bytes:
    status = _melody_svs_status()
    if not settings.melody_svs_enabled:
        raise RuntimeError("melody SVS backend disabled")
    if not status.get("ready"):
        raise RuntimeError(f"melody SVS backend not ready: missing={status.get('missing')} warnings={status.get('warnings')}")
    if _is_openutau_onnx_backend():
        return _generate_with_openutau_onnx_svs(source_path, lyrics, title, template, job_dir)
    if settings.melody_svs_backend.strip().lower() != "openvpi-diffsinger":
        raise RuntimeError(f"unsupported melody SVS backend: {settings.melody_svs_backend}")

    svs_dir = job_dir / "melody_svs"
    svs_dir.mkdir(parents=True, exist_ok=True)
    project_path = _build_diffsinger_ds_project(lyrics, title, template, svs_dir / "input.ds")
    model_info = status.get("models") or {}
    acoustic_exp = str(model_info.get("acousticExp") or settings.melody_svs_acoustic_exp or "").strip()
    variance_exp = str(model_info.get("varianceExp") or settings.melody_svs_variance_exp or "").strip()
    if settings.melody_svs_required and not variance_exp:
        raise RuntimeError("strict melody SVS requires a DiffSinger variance checkpoint")
    runtime_dir = _svs_acoustic_runtime_dir()
    infer_cli = _svs_acoustic_cli(runtime_dir)
    infer_script = infer_cli["path"]
    python_exe = settings.melody_svs_python if settings.melody_svs_python.exists() else Path(sys.executable)

    acoustic_input = project_path
    if variance_exp:
        if infer_cli["kind"] != "openvpi-current":
            raise RuntimeError("DiffSinger variance inference requires the current OpenVPI runtime")
        variance_title = "variance"
        _run_svs_command(
            [
                str(python_exe),
                str(infer_script),
                "variance",
                str(project_path),
                "--exp",
                variance_exp,
                "--out",
                str(svs_dir),
                "--title",
                variance_title,
                "--steps",
                str(max(1, int(settings.melody_svs_steps))),
            ],
            runtime_dir,
            svs_dir / "variance.log",
            "DiffSinger variance inference failed",
        )
        predicted = svs_dir / f"{variance_title}.ds"
        if not predicted.exists():
            raise RuntimeError("DiffSinger variance inference finished but no DS output was produced")
        acoustic_input = predicted

    output_title = _safe_name(title) or "melody_svs"
    if infer_cli["kind"] == "openvpi-v1.7":
        command = [
            str(python_exe),
            str(infer_script),
            str(acoustic_input),
            "--exp",
            acoustic_exp,
            "--out",
            str(svs_dir),
            "--title",
            output_title,
            "--speedup",
            str(_svs_legacy_speedup()),
            "--mel",
        ]
        if settings.melody_svs_speaker.strip():
            command.extend(["--spk", settings.melody_svs_speaker.strip()])
        _run_svs_command(command, runtime_dir, svs_dir / "acoustic.log", "DiffSinger acoustic inference failed")
        mel_output = svs_dir / f"{output_title}.mel.pt"
        if not mel_output.exists():
            raise RuntimeError(f"DiffSinger did not produce mel in {svs_dir}")
        vocoder_runtime = _svs_vocoder_runtime_dir()
        vocoder_script = vocoder_runtime / "scripts" / "vocode.py"
        vocoder_ckpt = _svs_vocoder_ckpt_path()
        vocoder_command = [
            str(python_exe),
            str(vocoder_script),
            str(mel_output),
            "--exp",
            acoustic_exp,
            "--class",
            settings.melody_svs_vocoder_class.strip() or "NsfHifiGAN",
            "--ckpt",
            str(vocoder_ckpt),
            "--out",
            str(svs_dir),
            "--title",
            output_title,
        ]
        _run_svs_command(vocoder_command, vocoder_runtime, svs_dir / "vocoder.log", "DiffSinger vocoder inference failed")
    else:
        command = [
            str(python_exe),
            str(infer_script),
            "acoustic",
            str(acoustic_input),
            "--exp",
            acoustic_exp,
            "--out",
            str(svs_dir),
            "--title",
            output_title,
            "--steps",
            str(max(1, int(settings.melody_svs_steps))),
        ]
        if settings.melody_svs_speaker.strip():
            command.extend(["--spk", settings.melody_svs_speaker.strip()])
        if settings.melody_svs_language.strip():
            command.extend(["--lang", settings.melody_svs_language.strip()])
        _run_svs_command(command, runtime_dir, svs_dir / "acoustic.log", "DiffSinger acoustic inference failed")

    output_audio = _find_svs_output_audio(svs_dir, output_title)
    encoded = svs_dir / f"{output_title}.{_final_audio_format()}"
    if output_audio.suffix.lower().lstrip(".") != _final_audio_format():
        _encode_final_audio(output_audio, encoded)
        output_audio = encoded
    if settings.melody_use_vocal_separation:
        output_audio = _remix_svs_vocal_with_reference_accompaniment(source_path, output_audio, job_dir)
    _cleanup_torch_cuda(python_exe)
    return output_audio.read_bytes()


def _generate_with_openutau_onnx_svs(source_path: Path, lyrics: str, title: str, template: dict[str, Any], job_dir: Path) -> bytes:
    svs_dir = job_dir / "openutau_onnx_svs"
    svs_dir.mkdir(parents=True, exist_ok=True)
    project_path = _build_diffsinger_ds_project(lyrics, title, template, svs_dir / "input.ds")
    output_title = _safe_name(title) or "openutau_onnx_svs"
    script_path = Path(__file__).with_name("openutau_onnx_infer.py")
    if not script_path.exists():
        raise RuntimeError(f"OpenUtau ONNX inference script not found: {script_path}")
    python_exe = settings.melody_svs_python if settings.melody_svs_python.exists() else Path(sys.executable)
    command = [
        str(python_exe),
        str(script_path),
        str(project_path),
        "--model-dir",
        str(settings.melody_svs_onnx_model_dir),
        "--out",
        str(svs_dir),
        "--title",
        output_title,
        "--steps",
        str(max(1, int(settings.melody_svs_steps))),
        "--lang",
        settings.melody_svs_language.strip() or "zh",
    ]
    _run_svs_command(command, Path(__file__).parent, svs_dir / "openutau_onnx.log", "OpenUtau ONNX DiffSinger inference failed")
    output_audio = _find_svs_output_audio(svs_dir, output_title)
    encoded = svs_dir / f"{output_title}.{_final_audio_format()}"
    if output_audio.suffix.lower().lstrip(".") != _final_audio_format():
        _encode_final_audio(output_audio, encoded)
        output_audio = encoded
    if settings.melody_use_vocal_separation:
        output_audio = _remix_svs_vocal_with_reference_accompaniment(source_path, output_audio, job_dir)
    return output_audio.read_bytes()


def _retry_openutau_syllable_locked_candidate(
    source_path: Path,
    lyrics: str,
    title: str,
    template: dict[str, Any],
    job_dir: Path,
    original_path: Path,
    voice_path: Path | None,
) -> dict[str, Any] | None:
    if not _is_openutau_onnx_backend() or not settings.melody_svs_note_graph_enabled:
        return None
    _write_job_status(job_dir, "quality_retry", "note graph result rejected; retrying stable syllable-locked SVS")
    previous_note_graph = settings.melody_svs_note_graph_enabled
    previous_force_f0 = settings.melody_svs_force_reference_f0
    try:
        settings.melody_svs_note_graph_enabled = False
        settings.melody_svs_force_reference_f0 = True
        retry_dir = job_dir / "retry_syllable_locked"
        audio_bytes = _generate_with_openutau_onnx_svs(source_path, lyrics, title + "-syllable", template, retry_dir)
        retry_original = retry_dir / "generated_original.mp3"
        retry_original.write_bytes(audio_bytes)
        retry_final = retry_original
        audio_format = "mp3"
        if voice_path:
            if settings.use_vocal_separation_remix:
                processed_path = _clone_vocals_and_remix(retry_original, voice_path, retry_dir / "voice_clone")
            else:
                processed_path = _convert_singing_voice_with_seedvc(retry_original, voice_path, retry_dir / "voice_clone")
            retry_final = _encode_final_audio(processed_path, retry_dir / f"final.{_final_audio_format()}")
            audio_bytes = retry_final.read_bytes()
            audio_format = retry_final.suffix.strip(".").lower() or _final_audio_format()
        validation = _validate_sung_lyrics(retry_final, lyrics)
        quality_report = _validate_melody_generation_quality(retry_final, lyrics, template, validation, retry_dir)
        _write_generation_quality_report(retry_dir, quality_report)
        previous_report = {}
        try:
            previous_report = json.loads((job_dir / "quality_report.json").read_text(encoding="utf-8")) if (job_dir / "quality_report.json").exists() else {}
        except Exception:
            previous_report = {}
        previous_score = float(((previous_report.get("checks") or {}).get("asr") or {}).get("score") or 0.0)
        retry_score = float(((quality_report.get("checks") or {}).get("asr") or {}).get("score") or 0.0)
        if quality_report.get("passed") or retry_score > previous_score + 0.08:
            _write_job_status(
                job_dir,
                "quality_retry_selected",
                "stable syllable-locked SVS selected after quality retry",
                {"previousAsr": round(previous_score, 4), "retryAsr": round(retry_score, 4), "passed": bool(quality_report.get("passed"))},
            )
            return {
                "audioBytes": audio_bytes,
                "originalPath": retry_original,
                "finalAudioPath": retry_final,
                "audioFormat": audio_format,
                "validation": validation,
                "qualityReport": quality_report,
            }
        _write_job_status(
            job_dir,
            "quality_retry_rejected",
            "stable syllable-locked retry did not improve enough",
            {"previousAsr": round(previous_score, 4), "retryAsr": round(retry_score, 4), "passed": bool(quality_report.get("passed"))},
        )
        return None
    except Exception as exc:
        _write_job_status(job_dir, "quality_retry_failed", str(exc)[:500])
        return None
    finally:
        settings.melody_svs_note_graph_enabled = previous_note_graph
        settings.melody_svs_force_reference_f0 = previous_force_f0


def _remix_svs_vocal_with_reference_accompaniment(source_path: Path, svs_vocal: Path, job_dir: Path) -> Path:
    accompaniment = _find_reference_accompaniment(job_dir, source_path)
    if not accompaniment:
        _write_job_status(
            job_dir,
            "melody_svs_remix_failed",
            "SVS vocal generated, but reference accompaniment stem was unavailable",
            {"vocal": str(svs_vocal)},
        )
        raise RuntimeError("保留曲调创作未能分离出可用伴奏，已拒绝返回纯人声结果")

    remix_dir = job_dir / "melody_svs_remix"
    remix_dir.mkdir(parents=True, exist_ok=True)
    mixed_wav = remix_dir / "melody_rewrite_mix.wav"
    _run_ffmpeg(
        [
            "-y",
            "-i",
            str(accompaniment),
            "-i",
            str(svs_vocal),
            "-filter_complex",
            "[0:a]volume=1.08,bass=g=3:f=95:w=0.7,treble=g=1.2:f=7000,extrastereo=m=1.25[a0];"
            "[1:a]volume=1.02,highpass=f=70,lowpass=f=12500,acompressor=threshold=-18dB:ratio=2.4:attack=18:release=180[a1];"
            "[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0,loudnorm=I=-14:TP=-1.5:LRA=10,alimiter=limit=0.95[out]",
            "-map",
            "[out]",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(mixed_wav),
        ],
        "Remixing melody SVS vocal with reference accompaniment failed",
    )
    final_path = _encode_final_audio(mixed_wav, remix_dir / f"melody_rewrite_mix.{_final_audio_format()}")
    _write_job_status(
        job_dir,
        "melody_svs_remixed",
        "melody SVS vocal remixed with reference accompaniment",
        {"vocal": str(svs_vocal), "accompaniment": str(accompaniment), "output": str(final_path)},
    )
    return final_path


def _find_reference_accompaniment(job_dir: Path, source_path: Path) -> Path | None:
    stems_root = job_dir / "melody_stems" / settings.demucs_model
    candidates = [
        stems_root / source_path.stem / "no_vocals.wav",
        stems_root / "source" / "no_vocals.wav",
    ]
    candidates.extend((job_dir / "melody_stems").glob("**/no_vocals.wav"))
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def _svs_legacy_speedup() -> int:
    steps = max(1, int(settings.melody_svs_steps))
    return max(1, int(round(1000 / steps)))


def _build_diffsinger_ds_project(lyrics: str, title: str, template: dict[str, Any], destination: Path) -> Path:
    lines = template.get("lines") or []
    if not lines:
        raise RuntimeError("melody SVS requires a source melody template")
    if settings.strict_melody_backend_enabled:
        _validate_melody_template_quality(template)
    lyric_lines = _plain_lyric_lines_for_svs(lyrics)
    if not lyric_lines:
        raise RuntimeError("melody SVS requires generated lyric lines")
    dictionary = _load_diffsinger_dictionary()
    pinyin_lines = _pinyin_lines_with_svs_python(lyric_lines)
    params: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        text = lyric_lines[min(index, len(lyric_lines) - 1)]
        _validate_svs_lyric_line_fit(text, line)
        syllables = pinyin_lines[min(index, len(pinyin_lines) - 1)] if pinyin_lines else []
        params.append(_build_diffsinger_segment(title, text, syllables, line, dictionary, index))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


def _validate_svs_lyric_line_fit(text: str, line: dict[str, Any]) -> None:
    clean = _clean_lyric_text(text)
    count = _chinese_len(clean)
    section = str(line.get("section") or "Verse")
    min_count, max_count = _commercial_target_bounds(line, section)
    duration_ms = int(line.get("durationMs") or 0)
    duration = max(0.1, duration_ms / 1000.0)
    # Mandarin syllables often map to 1-3 phones. A fixed 2x estimate is too
    # strict for long commercial lines and rejects usable natural phrasing.
    phone_density = count * 1.65 / duration
    if count > max_count:
        raise RuntimeError(f"SVS lyric line too dense for melody slot: chars={count}, max={max_count}, durationMs={duration_ms}")
    if duration_ms <= 1000 and count > 3:
        raise RuntimeError(f"SVS lyric line too dense for sub-second slot: chars={count}, durationMs={duration_ms}")
    if phone_density > 7.2:
        raise RuntimeError(f"SVS lyric phoneme density too high: chars={count}, durationMs={duration_ms}, density={phone_density:.2f}")


def _validate_melody_template_quality(template: dict[str, Any]) -> None:
    lines = template.get("lines") or []
    if len(lines) < 3:
        raise RuntimeError("strict melody rewrite requires at least 3 timed lyric lines")
    pitch_backends = {str(line.get("pitchBackend") or "") for line in lines}
    if not any(name.startswith(("rmvpe", "crepe")) for name in pitch_backends):
        raise RuntimeError("strict melody rewrite requires RMVPE or CREPE pitch extraction")
    voiced_lines = [
        line
        for line in lines
        if int(line.get("avgF0") or 0) > 0 and float(line.get("voicedRatio") or 0) >= 0.12
    ]
    mean_voiced = sum(float(line.get("voicedRatio") or 0) for line in lines) / max(1, len(lines))
    if len(voiced_lines) < max(2, len(lines) // 2) or mean_voiced < 0.10:
        raise RuntimeError("strict melody rewrite rejected low-confidence melody template")


def _build_diffsinger_segment(
    title: str,
    text: str,
    syllables: list[str],
    line: dict[str, Any],
    dictionary: dict[str, list[str]],
    index: int,
) -> dict[str, Any]:
    clean_chars = _lyric_chars_for_svs(text)
    if not clean_chars:
        clean_chars = ["la"]
    if len(syllables) < len(clean_chars):
        syllables = syllables + ["la"] * (len(clean_chars) - len(syllables))
    syllables = syllables[: len(clean_chars)]

    duration = max(0.8, int(line.get("durationMs") or 0) / 1000.0)
    note_events = _line_note_events(line, len(clean_chars), duration) if settings.melody_svs_note_events_enabled else []
    note_graph = _build_syllable_note_graph(line, clean_chars, syllables, dictionary, duration) if settings.melody_svs_note_graph_enabled else []
    if note_graph:
        note_event_names, note_event_durations, note_event_slurs = _flatten_note_graph_events(note_graph)
        char_note_names = [str(item["note"]) for item in note_graph]
        syllable_durations = [float(item["duration"]) for item in note_graph]
    elif note_events:
        char_note_names = _syllable_note_names_from_events(note_events, len(clean_chars))
        syllable_durations = _syllable_durations_from_note_events(note_events, len(clean_chars), duration)
        note_event_names = [name for name, _ in note_events]
        note_event_durations = [dur for _, dur in note_events]
        note_event_slurs = [0 for _ in note_event_names]
    else:
        char_note_names = _line_note_names(line, len(clean_chars))
        note_event_names = char_note_names
        note_event_durations = []
        note_event_slurs = [0 for _ in note_event_names]
    syllable_phone_groups: list[list[str]] = []
    ph_num: list[str] = []
    if note_graph:
        for graph_item in note_graph:
            phones = [str(phone) for phone in (graph_item.get("phones") or [])]
            if not phones:
                phones = ["AP"] if graph_item.get("isRest") else (dictionary.get("la") or ["l", "a"])
            syllable_phone_groups.append(phones)
            ph_num.append(str(len(phones)))
    else:
        for index, syllable in enumerate(syllables):
            phones = dictionary.get(_normalize_pinyin_syllable(syllable)) or dictionary.get("la") or ["l", "a"]
            syllable_phone_groups.append(phones)
            ph_num.append(str(len(phones)))
    if not syllable_phone_groups:
        syllable_phone_groups = [["l", "a"]]
        ph_num = ["2"]

    if not note_graph and not note_events:
        syllable_weights = [1.0 for _ in syllable_phone_groups]
        syllable_weights[0] *= 0.92
        syllable_weights[-1] *= 1.12
        if str(line.get("section") or "").lower().startswith("chorus"):
            syllable_weights = [weight * 1.04 for weight in syllable_weights]
        if line.get("rhymeSlot"):
            syllable_weights[-1] *= 1.14
        weight_sum = sum(syllable_weights) or 1.0
        syllable_durations = [duration * weight / weight_sum for weight in syllable_weights]
        note_event_durations = syllable_durations

    ph_seq: list[str] = []
    phone_durations: list[float] = []
    for syllable_index, phones in enumerate(syllable_phone_groups):
        graph_item = note_graph[syllable_index] if note_graph and syllable_index < len(note_graph) else None
        graph_phone_durations = graph_item.get("phoneDurations") if isinstance(graph_item, dict) else None
        if isinstance(graph_phone_durations, list) and len(graph_phone_durations) == len(phones):
            for phone, phone_duration in zip(phones, graph_phone_durations):
                ph_seq.append(phone)
                phone_durations.append(max(0.02, float(phone_duration)))
        else:
            per_phone_weights = _phone_duration_weights(len(phones))
            phone_weight_sum = sum(per_phone_weights) or float(len(phones))
            for phone_index, phone in enumerate(phones):
                ph_seq.append(phone)
                phone_durations.append(syllable_durations[syllable_index] * per_phone_weights[phone_index] / phone_weight_sum)

    ph_dur_text = " ".join(f"{value:.4f}" for value in phone_durations)
    note_dur_text = " ".join(f"{value:.4f}" for value in note_event_durations)
    note_slur_text = " ".join(str(int(value)) for value in note_event_slurs)
    return {
        "offset": round(max(0, int(line.get("startMs") or 0)) / 1000.0, 3),
        "text": " ".join(clean_chars),
        "ph_seq": " ".join(ph_seq),
        "ph_dur": ph_dur_text,
        "ph_num": " ".join(ph_num),
        "note_seq": " ".join(note_event_names),
        "note_dur": note_dur_text,
        "note_dur_seq": note_dur_text,
        "note_slur": note_slur_text,
        "is_slur_seq": note_slur_text,
        "note_graph": note_graph,
        "note_binding_version": "syllable-note-graph-v2",
        "force_reference_f0": bool(settings.melody_svs_force_reference_f0),
        "f0_seq": " ".join(f"{value:.1f}" for value in _line_f0_values(line, duration)),
        "f0_timestep": "0.01",
        "name": f"{_safe_name(title) or 'song'}#{index}",
        "lang": settings.melody_svs_language.strip() or "zh",
        "input_type": "phoneme",
    }


def _plain_lyric_lines_for_svs(lyrics: str) -> list[str]:
    result: list[str] = []
    for raw in _strip_lyric_sections(lyrics).splitlines():
        # LRC timestamps must be removed before checking metadata/section tags.
        # Otherwise every timed line starts with '[' and is discarded.
        text = re.sub(r"\[\d{1,3}:\d{2}(?:\.\d{1,3})?\]", "", raw).strip()
        if not text or text.startswith("[") or _is_non_sung_lrc_text(text):
            continue
        clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text)
        if clean:
            result.append(clean)
    return result


def _lyric_chars_for_svs(text: str) -> list[str]:
    chars = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text or "")
    return chars[:32]


def _load_diffsinger_dictionary() -> dict[str, list[str]]:
    runtime_dir = _svs_acoustic_runtime_dir()
    dictionary_path = runtime_dir / "dictionaries" / "opencpop-extension.txt"
    if not dictionary_path.exists():
        dictionary_path = settings.melody_svs_root / "runtime" / "OpenVPI-DiffSinger" / "dictionaries" / "opencpop-extension.txt"
    mapping: dict[str, list[str]] = {}
    if dictionary_path.exists():
        for raw in dictionary_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = raw.strip().split("\t")
            if len(parts) == 2 and parts[0] and parts[1]:
                mapping[parts[0].strip()] = parts[1].split()
    if "la" not in mapping:
        mapping["la"] = ["l", "a"]
    return mapping


def _pinyin_lines_with_svs_python(lines: list[str]) -> list[list[str]]:
    python_exe = settings.melody_svs_python if settings.melody_svs_python.exists() else Path(sys.executable)
    code = (
        "import json, re, sys\n"
        "from pypinyin import Style, pinyin\n"
        "def fallback(text):\n"
        "    return ['la' for _ in text]\n"
        "raw=sys.stdin.buffer.read().decode('utf-8', errors='replace')\n"
        "texts=json.loads(raw or '[]')\n"
        "rows=[]\n"
        "for text in texts:\n"
        "    chars=''.join(re.findall(r'[\\u4e00-\\u9fff]', text or ''))\n"
        "    syllables=[item[0].replace('ü','v').replace('u:','v') for item in pinyin(chars, style=Style.NORMAL, heteronym=False, errors=fallback)]\n"
        "    rows.append(syllables)\n"
        "print(json.dumps(rows, ensure_ascii=False))\n"
    )
    result = subprocess.run(
        [str(python_exe), "-c", code],
        input=json.dumps(lines, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        stdout = (result.stdout or b"").decode("utf-8", errors="replace")
        raise RuntimeError(f"SVS pinyin conversion failed: {(stderr or stdout).strip()[:500]}")
    return json.loads((result.stdout or b"[]").decode("utf-8", errors="replace"))


def _normalize_pinyin_syllable(value: str) -> str:
    return (value or "la").strip().lower().replace("ü", "v").replace("u:", "v")


def _spread_duration(total: float, count: int, minimum: float) -> list[float]:
    count = max(1, count)
    total = max(minimum * count, float(total))
    return [total / count for _ in range(count)]


def _phone_duration_weights(phone_count: int) -> list[float]:
    phone_count = max(1, int(phone_count))
    if phone_count == 1:
        return [1.0]
    if phone_count == 2:
        return [0.42, 0.58]
    if phone_count == 3:
        return [0.26, 0.34, 0.40]
    middle_count = max(0, phone_count - 2)
    middle = [0.16 for _ in range(middle_count)]
    return [0.24, *middle, 0.32]


def _phone_durations_for_syllable(phones: list[str], syllable_duration: float, events: list[tuple[str, float]]) -> list[float]:
    duration = max(0.04, float(syllable_duration))
    phones = [str(phone) for phone in phones] or ["AP"]
    if phones == ["AP"] or phones == ["SP"]:
        return [duration]
    if len(phones) == 1:
        return [duration]

    event_count = len(events)
    voiced_tail = sum(max(0.0, float(value)) for _, value in events[1:]) if event_count > 1 else 0.0
    if event_count > 1 and voiced_tail >= duration * 0.18:
        onset = max(0.035, min(duration * 0.28, duration - voiced_tail))
        vowel = max(0.04, duration - onset)
        if len(phones) == 2:
            return [onset, vowel]
        middle_total = max(0.03 * (len(phones) - 2), duration - onset - vowel * 0.72)
        last = max(0.04, duration - onset - middle_total)
        middle_each = middle_total / max(1, len(phones) - 2)
        return [onset, *[middle_each for _ in phones[1:-1]], last]

    if len(phones) == 2:
        onset = max(0.04, duration * 0.18)
        return [onset, max(0.04, duration - onset)]
    if len(phones) == 3:
        onset = max(0.04, duration * 0.14)
        middle = max(0.04, duration * 0.18)
        tail = max(0.04, duration - onset - middle)
        return [onset, middle, tail]
    if len(phones) >= 4:
        onset = max(0.04, duration * 0.12)
        middle_total = max(0.08, duration * 0.24)
        tail = max(0.12, duration - onset - middle_total)
        middle_each = middle_total / max(1, len(phones) - 2)
        return [onset, *[middle_each for _ in phones[1:-1]], tail]

    weights = _phone_duration_weights(len(phones))
    total = sum(weights) or 1.0
    return [duration * weight / total for weight in weights]


def _syllable_slot_weights(
    line: dict[str, Any],
    count: int,
    duration: float,
    note_events: list[tuple[float, float, str]] | None = None,
) -> list[float]:
    count = max(1, int(count))
    duration = max(0.1, float(duration))
    weights = [1.0 + min(0.18, index * 0.025) for index in range(count)]
    if count >= 2:
        weights[0] *= 0.90
        weights[-1] *= 1.22 if line.get("rhymeSlot") else 1.12
    if count >= 3:
        if str(line.get("section") or "").lower().startswith("chorus"):
            weights[-2] *= 1.06
            weights[-1] *= 1.08
        contour = str(line.get("pitchContour") or "stable")
        if contour == "rising":
            weights[-1] *= 1.12
        elif contour == "falling":
            weights[0] *= 1.08
        elif contour == "arched":
            weights[count // 2] *= 1.10
    if duration < 1.5 and count > 4:
        for index in range(count - 1):
            weights[index] *= 0.92
        weights[-1] *= 1.18
    if note_events:
        window = duration / count
        event_profile = [0.0 for _ in range(count)]
        attack_profile = [0.0 for _ in range(count)]
        sustain_profile = [0.0 for _ in range(count)]
        for event_index, (start, end, _note) in enumerate(note_events):
            start = max(0.0, min(duration, float(start)))
            end = max(start, min(duration, float(end)))
            event_duration = end - start
            if event_duration <= 0:
                continue
            start_slot = max(0, min(count - 1, int(start / window)))
            end_slot = max(0, min(count - 1, int(max(start, end - 1e-6) / window)))
            for slot in range(start_slot, end_slot + 1):
                slot_start = slot * window
                slot_end = duration if slot == count - 1 else (slot + 1) * window
                overlap = min(end, slot_end) - max(start, slot_start)
                if overlap > 0:
                    event_profile[slot] += overlap
                    if event_index == 0:
                        attack_profile[slot] += overlap * 0.35
                    if event_index == len(note_events) - 1:
                        sustain_profile[slot] += overlap * 0.40
                    if end - start >= window * 0.9:
                        sustain_profile[slot] += overlap * 0.20
        profile_total = sum(event_profile)
        if profile_total > 0:
            for index in range(count):
                voiced_density = event_profile[index] / max(window, 0.05)
                sustain_boost = sustain_profile[index] / max(window, 0.05)
                attack_boost = attack_profile[index] / max(window, 0.05)
                event_weight = 0.55 + min(1.35, voiced_density * 0.72 + sustain_boost * 0.42 + attack_boost * 0.18)
                weights[index] = weights[index] * 0.42 + event_weight * 0.58
    return weights


def _build_syllable_note_graph(
    line: dict[str, Any],
    clean_chars: list[str],
    syllables: list[str],
    dictionary: dict[str, list[str]],
    duration: float,
) -> list[dict[str, Any]]:
    if not clean_chars:
        return []
    note_events = _extract_note_events_for_graph(line, duration, len(clean_chars))
    notes = _line_note_names(line, len(clean_chars))
    if len(notes) != len(clean_chars):
        return []
    if note_events:
        graph = _build_note_graph_from_events(note_events, clean_chars, syllables, dictionary, notes, duration, line)
        if graph:
            _validate_note_graph(graph, duration)
            return graph

    durations = _syllable_durations_for_graph(line, len(clean_chars), duration)
    if len(durations) != len(clean_chars):
        return []
    graph: list[dict[str, Any]] = []
    syllable_boundaries: list[tuple[float, float]] = []
    cursor = 0.0
    for value in durations:
        start = cursor
        cursor += max(0.04, float(value))
        syllable_boundaries.append((start, cursor))
    for index, char in enumerate(clean_chars):
        syllable = syllables[index] if index < len(syllables) else "la"
        normalized = _normalize_pinyin_syllable(syllable)
        phones = dictionary.get(normalized) or dictionary.get("la") or ["l", "a"]
        syllable_duration = max(0.08, float(durations[index]))
        per_char_events = _events_for_syllable(note_events, syllable_boundaries[index], notes[index], syllable_duration)
        phone_durations = _phone_durations_for_syllable(phones, syllable_duration, per_char_events)
        graph.append(
            {
                "char": char,
                "syllable": normalized,
                "phones": phones,
                "phoneDurations": [round(max(0.02, value), 4) for value in phone_durations],
                "note": per_char_events[0][0] if per_char_events else notes[index],
                "duration": round(sum(duration for _, duration in per_char_events) or syllable_duration, 4),
                "notes": [
                    {
                        "note": note,
                        "duration": round(max(0.04, note_duration), 4),
                        "slur": 0 if note_index == 0 else 1,
                    }
                    for note_index, (note, note_duration) in enumerate(per_char_events)
                ],
                "slur": 0,
            }
        )
    total = sum(float(item["duration"]) for item in graph)
    if total <= 0:
        return []
    scale = duration / total
    for item in graph:
        scaled_duration = max(0.08, float(item["duration"]) * scale)
        item["duration"] = round(scaled_duration, 4)
        events = item.get("notes") if isinstance(item.get("notes"), list) else []
        if events:
            event_total = sum(max(0.04, float(event.get("duration") or 0.0)) for event in events if isinstance(event, dict))
            if event_total > 0:
                event_scale = scaled_duration / event_total
                for event in events:
                    if isinstance(event, dict):
                        event["duration"] = round(max(0.04, float(event.get("duration") or 0.0) * event_scale), 4)
                correction = scaled_duration - sum(float(event.get("duration") or 0.0) for event in events if isinstance(event, dict))
                if isinstance(events[-1], dict):
                    events[-1]["duration"] = round(max(0.04, float(events[-1].get("duration") or 0.0) + correction), 4)
        phone_durations = item.get("phoneDurations") if isinstance(item.get("phoneDurations"), list) else []
        if phone_durations:
            phone_total = sum(max(0.02, float(value)) for value in phone_durations)
            if phone_total > 0:
                phone_scale = scaled_duration / phone_total
                item["phoneDurations"] = [round(max(0.02, float(value) * phone_scale), 4) for value in phone_durations]
                correction = scaled_duration - sum(float(value) for value in item["phoneDurations"])
                item["phoneDurations"][-1] = round(max(0.02, float(item["phoneDurations"][-1]) + correction), 4)
    _validate_note_graph(graph, duration)
    return graph


def _build_note_graph_from_events(
    note_events: list[tuple[float, float, str]],
    clean_chars: list[str],
    syllables: list[str],
    dictionary: dict[str, list[str]],
    fallback_notes: list[str],
    duration: float,
    line: dict[str, Any],
) -> list[dict[str, Any]]:
    slots = _split_note_events_to_syllable_slots(note_events, len(clean_chars), _syllable_slot_weights(line, len(clean_chars), duration, note_events))
    if not slots:
        return []
    graph: list[dict[str, Any]] = []
    cursor = 0.0
    for index, parts in enumerate(slots):
        if not parts:
            continue
        start = min(float(part[0]) for part in parts)
        end = max(float(part[1]) for part in parts)
        if start - cursor >= 0.045:
            graph.append(_rest_note_graph_item(start - cursor))
        syllable = syllables[index] if index < len(syllables) else "la"
        normalized = _normalize_pinyin_syllable(syllable)
        phones = dictionary.get(normalized) or dictionary.get("la") or ["l", "a"]
        fallback_note = fallback_notes[index] if index < len(fallback_notes) else "A3"
        per_char_events = _merge_slot_parts_to_events(parts, fallback_note)
        syllable_duration = sum(note_duration for _, note_duration in per_char_events)
        if syllable_duration <= 0:
            syllable_duration = max(0.08, end - start)
            per_char_events = [(fallback_note, syllable_duration)]
        phone_durations = _phone_durations_for_syllable(phones, syllable_duration, per_char_events)
        graph.append(
            {
                "char": clean_chars[index],
                "syllable": normalized,
                "phones": phones,
                "phoneDurations": [round(max(0.02, value), 4) for value in phone_durations],
                "note": per_char_events[0][0],
                "duration": round(syllable_duration, 4),
                "notes": [
                    {"note": note, "duration": round(max(0.04, note_duration), 4), "slur": 0 if note_index == 0 else 1}
                    for note_index, (note, note_duration) in enumerate(per_char_events)
                ],
                "slur": 0,
            }
        )
        cursor = max(cursor, end)
    if duration - cursor >= 0.045:
        graph.append(_rest_note_graph_item(duration - cursor))
    return _normalize_note_graph_total_duration(graph, duration)


def _split_note_events_to_syllable_slots(
    note_events: list[tuple[float, float, str]],
    count: int,
    weights: list[float] | None = None,
) -> list[list[tuple[float, float, str]]]:
    count = max(1, int(count))
    events = [(max(0.0, float(start)), max(0.0, float(end)), str(note)) for start, end, note in note_events if float(end) > float(start)]
    total_voiced = sum(end - start for start, end, _ in events)
    if total_voiced <= 0:
        return []
    raw_weights = [max(0.2, float(value)) for value in (weights or [])[:count]]
    if len(raw_weights) < count:
        raw_weights.extend([1.0 for _ in range(count - len(raw_weights))])
    weight_total = sum(raw_weights) or float(count)
    slot_edges: list[float] = [0.0]
    cursor = 0.0
    for value in raw_weights:
        cursor += total_voiced * value / weight_total
        slot_edges.append(cursor)
    slot_edges[-1] = total_voiced
    slots: list[list[tuple[float, float, str]]] = [[] for _ in range(count)]
    voice_cursor = 0.0
    for event_start, event_end, note in events:
        event_duration = event_end - event_start
        consumed = 0.0
        while consumed < event_duration - 1e-6:
            slot_index = min(count - 1, _slot_index_for_voice_cursor(slot_edges, voice_cursor))
            slot_end_voice = slot_edges[slot_index + 1]
            take = min(event_duration - consumed, max(0.001, slot_end_voice - voice_cursor))
            part_start = event_start + consumed
            part_end = min(event_end, part_start + take)
            if part_end - part_start >= 0.025:
                slots[slot_index].append((part_start, part_end, note))
            consumed += take
            voice_cursor += take
    return slots


def _slot_index_for_voice_cursor(edges: list[float], cursor: float) -> int:
    for index in range(max(0, len(edges) - 1)):
        if cursor < edges[index + 1] - 1e-6:
            return index
    return max(0, len(edges) - 2)


def _merge_slot_parts_to_events(parts: list[tuple[float, float, str]], fallback_note: str) -> list[tuple[str, float]]:
    merged: list[tuple[str, float]] = []
    for start, end, note in parts:
        duration = max(0.0, float(end) - float(start))
        if duration <= 0:
            continue
        note = str(note or fallback_note or "A3")
        if merged and merged[-1][0] == note:
            merged[-1] = (note, merged[-1][1] + duration)
        else:
            merged.append((note, duration))
    if not merged:
        return [(fallback_note or "A3", 0.12)]
    if len(merged) == 2:
        total = sum(value for _, value in merged)
        if total >= 0.45:
            first_note, first_duration = merged[0]
            second_note, second_duration = merged[1]
            dominant_index = 0 if first_duration >= second_duration else 1
            dominant_duration = merged[dominant_index][1]
            other_duration = merged[1 - dominant_index][1]
            if dominant_duration >= total * 0.64 and other_duration <= total * 0.28:
                return [(merged[dominant_index][0], total)]
    max_events = 2 if sum(value for _, value in merged) < 0.95 else 3
    compressed = _compress_syllable_note_overlaps(merged, max_events=max_events, min_seconds=0.11)
    return _shape_singing_note_events(compressed, fallback_note)


def _shape_singing_note_events(events: list[tuple[str, float]], fallback_note: str) -> list[tuple[str, float]]:
    normalized = [(str(note or fallback_note or "A3"), max(0.04, float(duration))) for note, duration in events if float(duration) > 0]
    if not normalized:
        return [(fallback_note or "A3", 0.12)]
    total = sum(duration for _, duration in normalized)
    if total < 0.32 or len(normalized) == 1:
        note = max(normalized, key=lambda item: item[1])[0]
        return [(note, total)]
    if len(normalized) > 3:
        normalized = _compress_syllable_note_overlaps(normalized, max_events=3, min_seconds=0.14)
    total = sum(duration for _, duration in normalized)
    if len(normalized) >= 2 and normalized[0][1] < total * 0.28:
        first_note, first_duration = normalized[0]
        second_note, second_duration = normalized[1]
        target_first = min(total * 0.34, max(first_duration, total * 0.22))
        delta = target_first - first_duration
        if delta > 0 and second_duration - delta >= 0.08:
            normalized[0] = (first_note, first_duration + delta)
            normalized[1] = (second_note, second_duration - delta)
    if len(normalized) >= 2 and normalized[-1][1] < total * 0.24:
        prev_note, prev_duration = normalized[-2]
        last_note, last_duration = normalized[-1]
        target_last = min(total * 0.38, max(last_duration, total * 0.26))
        delta = target_last - last_duration
        if delta > 0 and prev_duration - delta >= 0.08:
            normalized[-2] = (prev_note, prev_duration - delta)
            normalized[-1] = (last_note, last_duration + delta)
    correction = total - sum(duration for _, duration in normalized)
    note, duration = normalized[-1]
    normalized[-1] = (note, max(0.04, duration + correction))
    return normalized


def _rest_note_graph_item(duration: float) -> dict[str, Any]:
    value = round(max(0.04, float(duration)), 4)
    return {
        "char": "",
        "syllable": "SP",
        "phones": ["AP"],
        "phoneDurations": [value],
        "note": "rest",
        "duration": value,
        "notes": [{"note": "rest", "duration": value, "slur": 0}],
        "slur": 0,
        "isRest": True,
    }


def _normalize_note_graph_total_duration(graph: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    total = sum(float(item.get("duration") or 0.0) for item in graph)
    if total <= 0:
        return []
    scale = float(duration) / total
    for item in graph:
        scaled_duration = max(0.04 if item.get("isRest") else 0.08, float(item.get("duration") or 0.0) * scale)
        item["duration"] = round(scaled_duration, 4)
        events = item.get("notes") if isinstance(item.get("notes"), list) else []
        if events:
            event_total = sum(max(0.04, float(event.get("duration") or 0.0)) for event in events if isinstance(event, dict))
            if event_total > 0:
                event_scale = scaled_duration / event_total
                for event in events:
                    if isinstance(event, dict):
                        event["duration"] = round(max(0.04, float(event.get("duration") or 0.0) * event_scale), 4)
                correction = scaled_duration - sum(float(event.get("duration") or 0.0) for event in events if isinstance(event, dict))
                if isinstance(events[-1], dict):
                    events[-1]["duration"] = round(max(0.04, float(events[-1].get("duration") or 0.0) + correction), 4)
        phones = item.get("phones") if isinstance(item.get("phones"), list) else []
        phone_durations = item.get("phoneDurations") if isinstance(item.get("phoneDurations"), list) else []
        if phones:
            if len(phone_durations) != len(phones):
                phone_durations = _phone_durations_for_syllable([str(phone) for phone in phones], scaled_duration, [])
            phone_total = sum(max(0.02, float(value)) for value in phone_durations)
            if phone_total > 0:
                phone_scale = scaled_duration / phone_total
                item["phoneDurations"] = [round(max(0.02, float(value) * phone_scale), 4) for value in phone_durations]
                correction = scaled_duration - sum(float(value) for value in item["phoneDurations"])
                item["phoneDurations"][-1] = round(max(0.02, float(item["phoneDurations"][-1]) + correction), 4)
    return graph


def _extract_note_events_for_graph(line: dict[str, Any], duration: float, syllable_count: int) -> list[tuple[float, float, str]]:
    timeline = line.get("f0Timeline")
    if not timeline:
        return []
    try:
        import numpy as np

        curve = np.asarray(timeline, dtype=float)
        if curve.size < 4:
            return []
        frame_seconds = max(0.01, float(duration) / float(curve.size))
        smoothed = _smooth_f0_midi_curve(curve)
        events: list[tuple[float, float, int]] = []
        start_idx: int | None = None
        bucket: list[int] = []
        for frame_index, midi in enumerate(smoothed):
            if midi is None:
                if start_idx is not None:
                    _append_raw_note_event(events, start_idx, frame_index, bucket, frame_seconds)
                    start_idx = None
                    bucket = []
                continue
            if start_idx is None:
                start_idx = frame_index
                bucket = [midi]
                continue
            median = int(round(float(np.median(bucket)))) if bucket else midi
            if abs(midi - median) <= 1:
                bucket.append(midi)
            else:
                _append_raw_note_event(events, start_idx, frame_index, bucket, frame_seconds)
                start_idx = frame_index
                bucket = [midi]
        if start_idx is not None:
            _append_raw_note_event(events, start_idx, len(smoothed), bucket, frame_seconds)
        events = _merge_micro_note_events(events, min_seconds=0.09 if duration < 4 else 0.12)
        max_events = max(syllable_count, min(syllable_count * 3, syllable_count + 18))
        while len(events) > max_events:
            events = _merge_micro_note_events(events, min_seconds=max(event[1] - event[0] for event in events) + 0.001, force_once=True)
        return [(start, end, _midi_to_note_name(max(36, min(84, midi)))) for start, end, midi in events if end > start]
    except Exception as exc:
        _log_exception("note graph event extraction failed", exc)
        return []


def _smooth_f0_midi_curve(curve: Any) -> list[int | None]:
    try:
        import numpy as np

        values = np.asarray(curve, dtype=float)
        result: list[int | None] = []
        for index, value in enumerate(values):
            if not 60 <= float(value) <= 900:
                result.append(None)
                continue
            start = max(0, index - 1)
            end = min(values.size, index + 2)
            window = values[start:end]
            voiced = window[(window >= 60) & (window <= 900)]
            hz = float(np.median(voiced)) if voiced.size else float(value)
            result.append(max(36, min(84, int(round(_hz_to_midi(hz))))))
        return result
    except Exception:
        return []


def _append_raw_note_event(
    events: list[tuple[float, float, int]],
    start_idx: int,
    end_idx: int,
    bucket: list[int],
    frame_seconds: float,
) -> None:
    if end_idx <= start_idx or not bucket:
        return
    try:
        import numpy as np

        midi = int(round(float(np.median(bucket))))
    except Exception:
        midi = bucket[-1]
    events.append((start_idx * frame_seconds, end_idx * frame_seconds, midi))


def _merge_micro_note_events(
    events: list[tuple[float, float, int]],
    min_seconds: float,
    force_once: bool = False,
) -> list[tuple[float, float, int]]:
    if len(events) <= 1:
        return events
    result = list(events)
    merged_once = False
    while len(result) > 1:
        durations = [max(0.0, end - start) for start, end, _ in result]
        index = min(range(len(result)), key=lambda idx: durations[idx])
        if durations[index] >= min_seconds and not (force_once and not merged_once):
            break
        start, end, midi = result.pop(index)
        if index > 0:
            prev_start, prev_end, prev_midi = result[index - 1]
            chosen_midi = prev_midi if abs(prev_midi - midi) <= 3 or prev_end - prev_start >= end - start else midi
            result[index - 1] = (prev_start, max(prev_end, end), chosen_midi)
        else:
            next_start, next_end, next_midi = result[0]
            chosen_midi = next_midi if abs(next_midi - midi) <= 3 or next_end - next_start >= end - start else midi
            result[0] = (min(start, next_start), next_end, chosen_midi)
        merged_once = True
        if force_once:
            break
    return result


def _events_for_syllable(
    events: list[tuple[float, float, str]],
    boundary: tuple[float, float],
    fallback_note: str,
    fallback_duration: float,
) -> list[tuple[str, float]]:
    start, end = boundary
    overlaps: list[tuple[str, float]] = []
    for event_start, event_end, note in events:
        overlap = min(end, event_end) - max(start, event_start)
        if overlap >= 0.035:
            overlaps.append((note, overlap))
    if not overlaps:
        return [(fallback_note, fallback_duration)]
    syllable_duration = max(0.08, fallback_duration)
    if syllable_duration < 0.55:
        dominant_note = max(overlaps, key=lambda item: item[1])[0]
        return [(dominant_note, syllable_duration)]
    max_events = 2 if syllable_duration < 0.95 else 3
    overlaps = _compress_syllable_note_overlaps(overlaps, max_events=max_events, min_seconds=0.12)
    total = sum(duration for _, duration in overlaps)
    if total <= 0:
        return [(fallback_note, fallback_duration)]
    scale = syllable_duration / total
    normalized: list[tuple[str, float]] = []
    for note, duration in overlaps:
        value = max(0.04, duration * scale)
        if normalized and normalized[-1][0] == note:
            normalized[-1] = (note, normalized[-1][1] + value)
        else:
            normalized.append((note, value))
    correction = syllable_duration - sum(duration for _, duration in normalized)
    if normalized:
        note, value = normalized[-1]
        normalized[-1] = (note, max(0.04, value + correction))
    return normalized


def _compress_syllable_note_overlaps(
    overlaps: list[tuple[str, float]],
    max_events: int,
    min_seconds: float,
) -> list[tuple[str, float]]:
    result: list[tuple[str, float]] = []
    for note, duration in overlaps:
        duration = max(0.0, float(duration))
        if duration <= 0:
            continue
        if result and result[-1][0] == note:
            result[-1] = (note, result[-1][1] + duration)
        else:
            result.append((note, duration))
    if not result:
        return []
    while len(result) > 1 and (len(result) > max_events or min(duration for _, duration in result) < min_seconds):
        index = min(range(len(result)), key=lambda idx: result[idx][1])
        note, duration = result.pop(index)
        if index > 0:
            prev_note, prev_duration = result[index - 1]
            result[index - 1] = (prev_note, prev_duration + duration)
        else:
            next_note, next_duration = result[0]
            result[0] = (next_note, next_duration + duration)
    return result[:max_events]


def _flatten_note_graph_events(graph: list[dict[str, Any]]) -> tuple[list[str], list[float], list[int]]:
    notes: list[str] = []
    durations: list[float] = []
    slurs: list[int] = []
    for item in graph:
        events = item.get("notes") if isinstance(item.get("notes"), list) else []
        if events:
            for event_index, event in enumerate(events):
                if not isinstance(event, dict):
                    continue
                note = str(event.get("note") or item.get("note") or "A3")
                duration = max(0.04, float(event.get("duration") or 0.0))
                notes.append(note)
                durations.append(duration)
                slurs.append(0 if event_index == 0 else 1)
        else:
            notes.append(str(item.get("note") or "A3"))
            durations.append(max(0.08, float(item.get("duration") or 0.0)))
            slurs.append(0)
    if not notes:
        return ["A3"], [0.8], [0]
    return notes, durations, slurs


def _syllable_durations_for_graph(line: dict[str, Any], count: int, duration: float) -> list[float]:
    count = max(1, int(count))
    timeline = line.get("f0Timeline")
    if timeline:
        try:
            import numpy as np

            curve = np.asarray(timeline, dtype=float)
            if curve.size >= count:
                frame_seconds = max(0.01, float(duration) / float(curve.size))
                voiced = (curve >= 60) & (curve <= 900)
                weights: list[float] = []
                for index in range(count):
                    start = int(round(index * curve.size / count))
                    end = int(round((index + 1) * curve.size / count))
                    window = voiced[start:max(start + 1, end)]
                    # Voiced frames get slightly more duration, but unvoiced
                    # consonants still keep enough time for intelligibility.
                    weights.append(max(0.6, float(window.sum()) + 0.6) * frame_seconds)
                return _normalize_duration_values(weights, duration, 0.08)
        except Exception as exc:
            _log_exception("syllable graph duration extraction failed", exc)
    weights = [1.0 for _ in range(count)]
    if count >= 2:
        weights[0] *= 0.92
        weights[-1] *= 1.14 if line.get("rhymeSlot") else 1.08
    return _normalize_duration_values(weights, duration, 0.08)


def _normalize_duration_values(values: list[float], total: float, minimum: float) -> list[float]:
    if not values:
        return []
    count = len(values)
    total = max(minimum * count, float(total))
    raw = [max(minimum, float(value)) for value in values]
    scale = total / max(0.1, sum(raw))
    result = [max(minimum, value * scale) for value in raw]
    correction = total - sum(result)
    result[-1] = max(minimum, result[-1] + correction)
    return result


def _validate_note_graph(graph: list[dict[str, Any]], duration: float) -> None:
    if not graph:
        raise RuntimeError("SVS note graph is empty")
    missing = [item for item in graph if not item.get("phones") or not item.get("note")]
    if missing:
        raise RuntimeError("SVS note graph has syllables without phones or notes")
    total = sum(float(item.get("duration") or 0.0) for item in graph)
    if abs(total - float(duration)) > max(0.08, float(duration) * 0.035):
        raise RuntimeError(f"SVS note graph duration mismatch: graph={total:.3f}s line={float(duration):.3f}s")
    for item in graph:
        phone_durations = item.get("phoneDurations") if isinstance(item.get("phoneDurations"), list) else []
        if phone_durations and len(phone_durations) != len(item.get("phones") or []):
            raise RuntimeError("SVS note graph phone duration count mismatch")
        if phone_durations:
            phone_total = sum(float(value or 0.0) for value in phone_durations)
            if abs(phone_total - float(item.get("duration") or 0.0)) > 0.04:
                raise RuntimeError("SVS note graph phone durations do not match syllable duration")
        events = item.get("notes") if isinstance(item.get("notes"), list) else []
        if events:
            note_total = sum(float(event.get("duration") or 0.0) for event in events if isinstance(event, dict))
            if abs(note_total - float(item.get("duration") or 0.0)) > 0.04:
                raise RuntimeError("SVS note graph note durations do not match syllable duration")


def _line_note_names(line: dict[str, Any], count: int) -> list[str]:
    count = max(1, int(count))
    timeline = line.get("f0Timeline")
    if timeline:
        notes = _line_note_names_from_timeline(timeline, count)
        if notes:
            return _de_flatten_note_names(notes, line, count)
    curve = _resample_f0_curve(line.get("f0Curve"), max(1, count), 0.0)
    if any(value > 0 for value in curve):
        notes = [_midi_to_note_name(max(36, min(84, int(round(_hz_to_midi(value)))))) for value in curve]
        return _de_flatten_note_names(notes, line, count)
    base_f0 = float(line.get("avgF0") or 0) or 220.0
    contour = str(line.get("pitchContour") or "stable")
    notes: list[str] = []
    for idx in range(max(1, count)):
        ratio = idx / max(1, count - 1)
        shift = 0.0
        if contour == "rising":
            shift = -1.5 + ratio * 3.0
        elif contour == "falling":
            shift = 1.5 - ratio * 3.0
        elif contour == "arched":
            shift = 2.5 * (1.0 - abs(ratio - 0.5) * 2.0)
        midi = int(round(_hz_to_midi(base_f0) + shift))
        notes.append(_midi_to_note_name(max(36, min(84, midi))))
    return _de_flatten_note_names(notes, line, count)


def _line_note_names_from_timeline(timeline: Any, count: int) -> list[str]:
    try:
        import numpy as np

        values = np.asarray(timeline or [], dtype=float)
        if values.size < 2:
            return []
        notes: list[str] = []
        for index in range(max(1, count)):
            start = int(round(index * values.size / max(1, count)))
            end = int(round((index + 1) * values.size / max(1, count)))
            window = values[start:max(start + 1, end)]
            voiced = window[(window >= 60) & (window <= 900)]
            if voiced.size == 0:
                voiced = values[(values >= 60) & (values <= 900)]
            if voiced.size == 0:
                notes.append("A3")
                continue
            midi = int(round(float(np.median([_hz_to_midi(float(value)) for value in voiced]))))
            notes.append(_midi_to_note_name(max(36, min(84, midi))))
        return notes
    except Exception:
        return []


def _line_note_events(line: dict[str, Any], syllable_count: int, duration: float) -> list[tuple[str, float]]:
    timeline = line.get("f0Timeline")
    if not timeline:
        return []
    try:
        import numpy as np

        curve = np.asarray(timeline, dtype=float)
        if curve.size < 4:
            return []
        voiced = curve[(curve >= 60) & (curve <= 900)]
        if voiced.size < 3:
            return []
        frame_seconds = max(0.01, float(duration) / float(curve.size))
        events: list[tuple[int, int, int]] = []
        active_start: int | None = None
        bucket: list[int] = []
        for frame_index, value in enumerate(curve.tolist()):
            if 60 <= value <= 900:
                midi = max(36, min(84, int(round(_hz_to_midi(float(value))))))
                if active_start is None:
                    active_start = frame_index
                    bucket = [midi]
                else:
                    median = int(round(float(np.median(bucket)))) if bucket else midi
                    if abs(midi - median) <= 1:
                        bucket.append(midi)
                    else:
                        events.append((active_start, frame_index, median))
                        active_start = frame_index
                        bucket = [midi]
            elif active_start is not None:
                median = int(round(float(np.median(bucket)))) if bucket else 57
                events.append((active_start, frame_index, median))
                active_start = None
                bucket = []
        if active_start is not None:
            median = int(round(float(np.median(bucket)))) if bucket else 57
            events.append((active_start, len(curve), median))

        min_event = 0.10 if duration < 4.0 else 0.14
        merged: list[tuple[str, float]] = []
        for start, end, midi in events:
            seconds = max(frame_seconds, (end - start) * frame_seconds)
            note = _midi_to_note_name(midi)
            if merged and (seconds < min_event or merged[-1][0] == note):
                prev_note, prev_seconds = merged[-1]
                if prev_note == note or seconds < min_event:
                    merged[-1] = (prev_note, prev_seconds + seconds)
                else:
                    merged.append((note, seconds))
            else:
                merged.append((note, seconds))
        if not merged:
            return []
        total = sum(seconds for _, seconds in merged) or duration
        scaled = [(note, max(0.06, seconds * duration / total)) for note, seconds in merged]
        # The current ONNX bridge does not carry an explicit syllable-to-note
        # binding field. Keeping one primary note per sung syllable gives a
        # more stable vocal line than sending many micro note events for one
        # lyric character.
        max_notes = max(1, syllable_count)
        if len(scaled) > max_notes:
            scaled = _compress_note_events(scaled, max_notes)
        if len(scaled) < syllable_count:
            scaled = _expand_note_events_to_min_count(scaled, syllable_count)
        return scaled
    except Exception as exc:
        _log_exception("line note event extraction failed", exc)
        return []


def _compress_note_events(events: list[tuple[str, float]], max_count: int) -> list[tuple[str, float]]:
    result = list(events)
    max_count = max(1, int(max_count))
    while len(result) > max_count:
        best = min(range(len(result)), key=lambda idx: result[idx][1])
        if best > 0:
            note, dur = result.pop(best)
            prev_note, prev_dur = result[best - 1]
            result[best - 1] = (prev_note, prev_dur + dur)
        elif len(result) > 1:
            note, dur = result.pop(best)
            next_note, next_dur = result[0]
            result[0] = (next_note, next_dur + dur)
        else:
            break
    return result


def _expand_note_events_to_min_count(events: list[tuple[str, float]], min_count: int) -> list[tuple[str, float]]:
    result = list(events)
    min_count = max(1, int(min_count))
    if not result:
        return []
    while len(result) < min_count:
        index = max(range(len(result)), key=lambda idx: result[idx][1])
        note, duration = result.pop(index)
        left = max(0.06, duration * 0.48)
        right = max(0.06, duration - left)
        result[index:index] = [(note, left), (note, right)]
    return result


def _syllable_note_names_from_events(events: list[tuple[str, float]], count: int) -> list[str]:
    if not events:
        return []
    count = max(1, int(count))
    total = sum(max(0.01, duration) for _, duration in events)
    result: list[str] = []
    for index in range(count):
        position = (index + 0.5) * total / count
        acc = 0.0
        selected = events[-1][0]
        for note, duration in events:
            acc += max(0.01, duration)
            if position <= acc:
                selected = note
                break
        result.append(selected)
    return result


def _syllable_durations_from_note_events(events: list[tuple[str, float]], count: int, duration: float) -> list[float]:
    count = max(1, int(count))
    if not events:
        return _spread_duration(duration, count, 0.08)
    if len(events) == count:
        raw = [max(0.08, float(value)) for _, value in events]
        scale = duration / max(0.1, sum(raw))
        return [value * scale for value in raw]
    total = sum(max(0.01, value) for _, value in events) or max(0.1, duration)
    boundaries = [index * total / count for index in range(count + 1)]
    durations: list[float] = []
    for index in range(count):
        durations.append(max(0.08, (boundaries[index + 1] - boundaries[index]) * duration / total))
    scale = duration / max(0.1, sum(durations))
    return [value * scale for value in durations]


def _note_name_to_midi(note: str) -> int | None:
    match = re.fullmatch(r"([A-G])(#?)(-?\d+)", str(note or ""))
    if not match:
        return None
    names = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
    return (int(match.group(3)) + 1) * 12 + names[match.group(1)] + (1 if match.group(2) else 0)


def _max_same_run(values: list[str]) -> int:
    longest = 0
    current = 0
    previous = None
    for value in values:
        current = current + 1 if value == previous else 1
        previous = value
        longest = max(longest, current)
    return longest


def _de_flatten_note_names(notes: list[str], line: dict[str, Any], count: int) -> list[str]:
    count = max(1, int(count))
    if len(notes) < count:
        notes = notes + [notes[-1] if notes else "A3"] * (count - len(notes))
    notes = notes[:count]
    if count <= 2:
        return notes
    unique_count = len(set(notes))
    if unique_count >= max(2, min(4, count // 3)) and _max_same_run(notes) < max(4, int(count * 0.55)):
        return notes

    midis = [_note_name_to_midi(note) for note in notes]
    base_values = [value for value in midis if value is not None]
    if not base_values:
        base_values = [int(round(_hz_to_midi(float(line.get("avgF0") or 220.0))))]
    base = max(36, min(84, int(round(sum(base_values) / len(base_values)))))
    contour = str(line.get("pitchContour") or "stable")
    if contour == "rising":
        pattern = [-2, -1, 0, 1, 2, 2]
    elif contour == "falling":
        pattern = [2, 1, 0, -1, -2, -2]
    elif contour == "arched":
        pattern = [-1, 0, 2, 1, 0, -1]
    else:
        pattern = [0, 1, 0, -1, 1, 0]
    repaired: list[str] = []
    for idx in range(count):
        shift = pattern[idx % len(pattern)]
        if count >= 8 and idx == count - 1:
            shift -= 1
        repaired.append(_midi_to_note_name(max(36, min(84, base + shift))))
    return repaired


def _line_f0_values(line: dict[str, Any], duration: float) -> list[float]:
    frames = max(1, int(duration / 0.01))
    timeline_curve = _resample_f0_timeline_preserve_silence(line.get("f0Timeline"), frames, 0.0)
    if any(value > 0 for value in timeline_curve):
        return [max(60.0, min(900.0, value)) if value > 0 else 0.0 for value in timeline_curve]
    curve = _resample_f0_curve(line.get("f0Curve"), frames, 0.0)
    if any(value > 0 for value in curve):
        return [max(60.0, min(900.0, value)) for value in curve]
    base_f0 = float(line.get("avgF0") or 0) or 220.0
    contour = str(line.get("pitchContour") or "stable")
    values: list[float] = []
    for idx in range(frames):
        ratio = idx / max(1, frames - 1)
        multiplier = 1.0
        if contour == "rising":
            multiplier = 0.94 + ratio * 0.12
        elif contour == "falling":
            multiplier = 1.06 - ratio * 0.12
        elif contour == "arched":
            multiplier = 1.0 + 0.08 * (1.0 - abs(ratio - 0.5) * 2.0)
        values.append(max(60.0, min(900.0, base_f0 * multiplier)))
    return values


def _resample_f0_timeline_preserve_silence(values: Any, count: int, default: float = 0.0) -> list[float]:
    count = max(1, int(count))
    try:
        import numpy as np

        curve = np.asarray(values or [], dtype=float)
        if curve.size == 0:
            return [default for _ in range(count)]
        curve = np.where((curve >= 60) & (curve <= 900), curve, 0.0)
        if curve.size == 1:
            value = float(curve[0])
            return [value if value > 0 else default for _ in range(count)]
        result: list[float] = []
        for index in range(count):
            start = int(np.floor(index * curve.size / count))
            end = int(np.floor((index + 1) * curve.size / count))
            if end <= start:
                end = min(curve.size, start + 1)
            window = curve[start:end]
            voiced = window[(window >= 60) & (window <= 900)]
            voiced_ratio = float(voiced.size) / max(1, window.size)
            if voiced.size == 0 or voiced_ratio < 0.34:
                result.append(default)
            else:
                result.append(float(np.median(voiced)))
        return result
    except Exception:
        return [default for _ in range(count)]


def _compact_f0_timeline(values: Any, max_points: int = 512) -> list[float]:
    try:
        import numpy as np

        curve = np.asarray(values, dtype=float)
        if curve.size == 0:
            return []
        curve = np.where((curve >= 60) & (curve <= 900), curve, 0.0)
        if curve.size > max_points:
            compacted: list[float] = []
            for index in range(max_points):
                start = int(np.floor(index * curve.size / max_points))
                end = int(np.floor((index + 1) * curve.size / max_points))
                if end <= start:
                    end = min(curve.size, start + 1)
                window = curve[start:end]
                voiced = window[(window >= 60) & (window <= 900)]
                if voiced.size and float(voiced.size) / max(1, window.size) >= 0.34:
                    compacted.append(float(np.median(voiced)))
                else:
                    compacted.append(0.0)
            curve = np.asarray(compacted, dtype=float)
        return [round(float(value), 1) if float(value) > 0 else 0.0 for value in curve.tolist()]
    except Exception:
        return []


def _compact_f0_curve(values: Any, max_points: int = 96) -> list[float]:
    try:
        import numpy as np

        curve = np.asarray(values, dtype=float)
        curve = curve[(curve >= 60) & (curve <= 900)]
        curve = curve[curve > 0]
        if curve.size == 0:
            return []
        if curve.size > max_points:
            positions = np.linspace(0, curve.size - 1, max_points)
            curve = np.interp(positions, np.arange(curve.size), curve)
        return [round(float(value), 1) for value in curve.tolist()]
    except Exception:
        return []


def _resample_f0_curve(values: Any, count: int, default: float) -> list[float]:
    count = max(1, int(count))
    try:
        import numpy as np

        curve = np.asarray(values or [], dtype=float)
        curve = curve[(curve >= 60) & (curve <= 900)]
        if curve.size == 0:
            return [default for _ in range(count)]
        if curve.size == 1:
            return [float(curve[0]) for _ in range(count)]
        positions = np.linspace(0, curve.size - 1, count)
        return [float(value) for value in np.interp(positions, np.arange(curve.size), curve).tolist()]
    except Exception:
        return [default for _ in range(count)]


def _hz_to_midi(frequency: float) -> float:
    import math

    return 69.0 + 12.0 * math.log2(max(1.0, frequency) / 440.0)


def _midi_to_note_name(midi: int) -> str:
    names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
    return f"{names[midi % 12]}{midi // 12 - 1}"


def _run_svs_command(command: list[str], cwd: Path, log_path: Path, error_message: str) -> None:
    result = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=settings.timeout_seconds,
    )
    log_path.write_text((result.stdout or "") + "\n" + (result.stderr or ""), encoding="utf-8", errors="ignore")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or error_message).strip()[-1200:]
        raise RuntimeError(f"{error_message}: {detail}")


def _find_svs_output_audio(output_dir: Path, title: str) -> Path:
    candidates = list(output_dir.glob(f"{title}*.wav")) + list(output_dir.glob(f"{title}*.mp3"))
    if not candidates:
        candidates = list(output_dir.glob("*.wav")) + list(output_dir.glob("*.mp3"))
    if not candidates:
        raise RuntimeError(f"DiffSinger did not produce audio in {output_dir}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _cleanup_torch_cuda(python_exe: Path) -> None:
    try:
        subprocess.run(
            [str(python_exe), "-c", "import torch; torch.cuda.empty_cache() if torch.cuda.is_available() else None"],
            text=True,
            capture_output=True,
            timeout=20,
        )
    except Exception as exc:
        _log_exception("SVS CUDA cleanup skipped", exc)


def _call_ace_step_official(source_path: Path, lyrics: str, prompt: str, duration: float) -> bytes:
    chat_result = _call_ace_step_chat_completion(
        lyrics=lyrics,
        prompt=prompt,
        duration=duration,
        task_type="cover",
        source_path=source_path,
    )
    if chat_result:
        return chat_result

    base = settings.ace_step_base_url.rstrip("/")
    payload = {
        "prompt": prompt,
        "lyrics": lyrics,
        "vocal_language": "zh",
        "audio_format": "mp3",
        "task_type": "cover",
        "audio_duration": duration,
        "inference_steps": settings.ace_step_steps,
        "guidance_scale": settings.ace_step_guidance_scale,
        "audio_cover_strength": settings.ace_step_cover_strength,
        "thinking": False,
        "use_format": False,
        "batch_size": 1,
    }
    with httpx.Client(timeout=settings.ace_step_timeout_seconds) as client:
        with source_path.open("rb") as source_file:
            response = client.post(
                base + "/release_task",
                data={key: _form_value(value) for key, value in payload.items()},
                files={"src_audio": (source_path.name, source_file, "application/octet-stream")},
            )
        response.raise_for_status()
        data = response.json()
        task_data = data.get("data") if isinstance(data, dict) else data
        if not isinstance(task_data, dict):
            raise RuntimeError(f"invalid release_task response: {str(data)[:200]}")
        task_id = str(task_data.get("task_id") or task_data.get("id") or "")
        if not task_id:
            raise RuntimeError(f"release_task did not return task_id: {str(data)[:200]}")
        deadline = time.time() + settings.ace_step_timeout_seconds
        last_status = ""
        while time.time() < deadline:
            time.sleep(max(2.0, settings.poll_interval_seconds))
            query = client.post(base + "/query_result", json={"task_id_list": [task_id]})
            query.raise_for_status()
            query_payload = query.json()
            entries = query_payload.get("data") if isinstance(query_payload, dict) else query_payload
            if not isinstance(entries, list) or not entries:
                continue
            entry = entries[0]
            status = int(entry.get("status") or 0)
            last_status = str(status)
            if status == 2:
                raise RuntimeError(str(entry.get("error") or entry.get("message") or entry)[:500])
            if status != 1:
                continue
            result = entry.get("result")
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except json.JSONDecodeError:
                    result = [{"file": result}]
            return _download_ace_audio_result(client, base, result)
        raise TimeoutError(f"ACE-Step task timed out; last_status={last_status}")


def _call_ace_step_text_official(lyrics: str, prompt: str, duration: float) -> bytes:
    chat_result = _call_ace_step_chat_completion(
        lyrics=lyrics,
        prompt=prompt,
        duration=duration,
        task_type=settings.ace_step_original_task_type,
        source_path=None,
    )
    if chat_result:
        return chat_result

    base = settings.ace_step_base_url.rstrip("/")
    payload = {
        "prompt": prompt,
        "lyrics": lyrics,
        "vocal_language": "zh",
        "audio_format": "mp3",
        "task_type": settings.ace_step_original_task_type,
        "audio_duration": duration,
        "inference_steps": settings.ace_step_steps,
        "guidance_scale": settings.ace_step_guidance_scale,
        "thinking": False,
        "use_format": False,
        "batch_size": 1,
    }
    with httpx.Client(timeout=settings.ace_step_timeout_seconds) as client:
        response = client.post(
            base + "/release_task",
            data={key: _form_value(value) for key, value in payload.items()},
        )
        response.raise_for_status()
        data = response.json()
        task_data = data.get("data") if isinstance(data, dict) else data
        if not isinstance(task_data, dict):
            raise RuntimeError(f"invalid release_task response: {str(data)[:200]}")
        task_id = str(task_data.get("task_id") or task_data.get("id") or "")
        if not task_id:
            raise RuntimeError(f"release_task did not return task_id: {str(data)[:200]}")
        deadline = time.time() + settings.ace_step_timeout_seconds
        last_status = ""
        while time.time() < deadline:
            time.sleep(max(2.0, settings.poll_interval_seconds))
            query = client.post(base + "/query_result", json={"task_id_list": [task_id]})
            query.raise_for_status()
            query_payload = query.json()
            entries = query_payload.get("data") if isinstance(query_payload, dict) else query_payload
            if not isinstance(entries, list) or not entries:
                continue
            entry = entries[0]
            status = int(entry.get("status") or 0)
            last_status = str(status)
            if status == 2:
                raise RuntimeError(str(entry.get("error") or entry.get("message") or entry)[:500])
            if status != 1:
                continue
            result = entry.get("result")
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except json.JSONDecodeError:
                    result = [{"file": result}]
            return _download_ace_audio_result(client, base, result)
        raise TimeoutError(f"ACE-Step task timed out; last_status={last_status}")


def _call_ace_step_chat_completion(
    *,
    lyrics: str,
    prompt: str,
    duration: float,
    task_type: str,
    source_path: Path | None,
) -> bytes:
    base = settings.ace_step_base_url.rstrip("/")
    content: Any
    tagged = f"<prompt>{prompt}</prompt>\n<lyrics>{lyrics}</lyrics>"
    if source_path:
        audio_format = source_path.suffix.lower().lstrip(".") or "mp3"
        if audio_format == "m4a":
            audio_format = "mp4"
        encoded = base64.b64encode(source_path.read_bytes()).decode("ascii")
        content = [
            {"type": "text", "text": tagged},
            {"type": "input_audio", "input_audio": {"data": encoded, "format": audio_format}},
        ]
    else:
        content = tagged
    payload = {
        "model": settings.ace_step_model.strip() or "acestep/acestep-v15-turbo",
        "messages": [{"role": "user", "content": content}],
        "modalities": ["audio"],
        "task_type": task_type or "text2music",
        "lyrics": lyrics,
        "thinking": False,
        "use_format": False,
        "use_cot_caption": False,
        "use_cot_language": False,
        "inference_steps": settings.ace_step_steps,
        "guidance_scale": settings.ace_step_guidance_scale,
        "audio_cover_strength": settings.ace_step_cover_strength,
        "batch_size": 1,
        "audio_config": {
            "duration": duration,
            "format": "mp3",
            "vocal_language": "zh",
            "instrumental": False,
        },
    }
    with httpx.Client(timeout=settings.ace_step_timeout_seconds) as client:
        response = client.post(base + "/v1/chat/completions", json=payload)
        if response.status_code == 404:
            raise RuntimeError("endpoint not found")
        response.raise_for_status()
        return _download_ace_audio_result(client, base, response.json())


def _download_ace_audio_result(client: httpx.Client, base: str, result: Any) -> bytes:
    items = result if isinstance(result, list) else [result]
    for item in items:
        if not isinstance(item, dict):
            continue
        audio_url = item.get("audio_url")
        if isinstance(audio_url, dict):
            nested_url = str(audio_url.get("url") or "").strip()
            if nested_url:
                if nested_url.startswith("data:audio") and "," in nested_url:
                    return base64.b64decode(nested_url.split(",", 1)[1])
                item = {**item, "file": nested_url}
        choices = item.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message") or choice.get("delta") or {}
                if isinstance(message, dict):
                    audio_items = message.get("audio") or []
                    if isinstance(audio_items, list):
                        try:
                            nested = _download_ace_audio_result(client, base, audio_items)
                            if nested:
                                return nested
                        except RuntimeError:
                            pass
        message = item.get("message")
        if isinstance(message, dict):
            audio_items = message.get("audio") or []
            if isinstance(audio_items, list):
                try:
                    nested = _download_ace_audio_result(client, base, audio_items)
                    if nested:
                        return nested
                except RuntimeError:
                    pass
        file_url = str(item.get("file") or item.get("audio") or item.get("path") or item.get("url") or "").strip()
        if not file_url:
            continue
        if file_url.startswith("data:audio") and "," in file_url:
            return base64.b64decode(file_url.split(",", 1)[1])
        if file_url.startswith("http://") or file_url.startswith("https://"):
            url = file_url
        elif file_url.startswith("/"):
            url = base + file_url
        else:
            path = Path(file_url)
            if path.exists():
                return path.read_bytes()
            url = base + "/v1/audio?path=" + file_url
        response = client.get(url)
        response.raise_for_status()
        if response.content:
            return response.content
    raise RuntimeError(f"ACE-Step result did not contain downloadable audio: {str(result)[:300]}")


def _ace_step_prompt(caption: str, title: str) -> str:
    compact = re.sub(r"\s+", " ", caption or "").strip()
    return (
        f"Mandarin Chinese pop cover generation for {title}. Preserve the reference song melody contour, phrase timing, "
        "vocal rhythm, section energy, and singability while replacing the lyrics with the supplied new lyrics. "
        "Only sing the supplied lyric lines. Do not sing source credits, source title cards, songwriter/composer/arranger names, producer names, copyright lines, labels, or metadata. "
        "Natural expressive vocal, finished production, no dry a cappella, no abrupt ending. "
        + compact[:900]
    )


def _ace_step_original_prompt(caption: str, title: str) -> str:
    compact = re.sub(r"\s+", " ", caption or "").strip()
    return (
        f"Mandarin Chinese pop song generation for {title}. Create a complete original song with natural vocal, "
        "clear verse and chorus structure, polished arrangement, balanced mix, no abrupt ending. "
        + compact[:900]
    )


def _strip_lyric_sections(lyrics: str) -> str:
    lines = [raw.rstrip() for raw in (lyrics or "").splitlines() if raw.strip()]
    return "\n".join(lines).strip()


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _call_ace_step_rest(endpoint: str, source_path: Path, lyrics: str, prompt: str, duration: float, output_dir: Path) -> bytes:
    base = settings.ace_step_base_url.rstrip("/")
    payload = {
        "task_type": "cover",
        "audio2audio_enable": True,
        "src_audio_path": str(source_path.resolve()),
        "source_audio": str(source_path.resolve()),
        "lyrics": lyrics,
        "lyric": lyrics,
        "prompt": prompt,
        "audio_duration": duration,
        "duration": duration,
        "infer_step": settings.ace_step_steps,
        "steps": settings.ace_step_steps,
        "guidance_scale": settings.ace_step_guidance_scale,
        "scheduler_type": settings.ace_step_scheduler,
        "audio2audio_strength": settings.ace_step_cover_strength,
        "cover_strength": settings.ace_step_cover_strength,
        "output_dir": str(output_dir.resolve()),
    }
    with httpx.Client(timeout=settings.ace_step_timeout_seconds) as client:
        response = client.post(base + endpoint, json=payload)
        if response.status_code == 404:
            raise RuntimeError("endpoint not found")
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "audio" in content_type or "octet-stream" in content_type:
            return response.content
        data = response.json()
    return _read_ace_step_result(data, output_dir)


def _call_ace_step_text_rest(endpoint: str, lyrics: str, prompt: str, duration: float, output_dir: Path) -> bytes:
    base = settings.ace_step_base_url.rstrip("/")
    payload = {
        "task_type": settings.ace_step_original_task_type,
        "lyrics": lyrics,
        "lyric": lyrics,
        "prompt": prompt,
        "audio_duration": duration,
        "duration": duration,
        "infer_step": settings.ace_step_steps,
        "steps": settings.ace_step_steps,
        "guidance_scale": settings.ace_step_guidance_scale,
        "scheduler_type": settings.ace_step_scheduler,
        "output_dir": str(output_dir.resolve()),
    }
    with httpx.Client(timeout=settings.ace_step_timeout_seconds) as client:
        response = client.post(base + endpoint, json=payload)
        if response.status_code == 404:
            raise RuntimeError("endpoint not found")
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "audio" in content_type or "octet-stream" in content_type:
            return response.content
        data = response.json()
    return _read_ace_step_result(data, output_dir)


def _call_ace_step_gradio(source_path: Path, lyrics: str, prompt: str, duration: float, output_dir: Path) -> bytes:
    from gradio_client import Client, handle_file

    client = Client(settings.ace_step_base_url.rstrip("/"))
    api_names = ("/generate_music", "/predict", "/cover")
    last_error = ""
    for api_name in api_names:
        try:
            result = client.predict(
                prompt=prompt,
                lyrics=lyrics,
                src_audio_path=handle_file(str(source_path.resolve())),
                task_type="cover",
                audio_duration=duration,
                infer_step=settings.ace_step_steps,
                guidance_scale=settings.ace_step_guidance_scale,
                scheduler_type=settings.ace_step_scheduler,
                audio2audio_strength=settings.ace_step_cover_strength,
                api_name=api_name,
            )
            data = _read_ace_step_result(result, output_dir)
            if data:
                return data
        except Exception as exc:
            last_error = str(exc)[:180]
    raise RuntimeError(last_error or "gradio prediction failed")


def _read_ace_step_result(result: Any, output_dir: Path) -> bytes:
    candidates: list[Any] = []
    if isinstance(result, dict):
        for key in ("audio", "audio_path", "path", "file", "output", "result", "url"):
            if key in result:
                candidates.append(result[key])
        candidates.extend(result.values())
    elif isinstance(result, (list, tuple)):
        candidates.extend(result)
    else:
        candidates.append(result)

    for item in candidates:
        if isinstance(item, dict):
            nested = _read_ace_step_result(item, output_dir)
            if nested:
                return nested
        value = str(item or "").strip()
        if not value:
            continue
        if value.startswith("data:audio") and "," in value:
            return base64.b64decode(value.split(",", 1)[1])
        path = Path(value)
        if path.exists() and path.is_file():
            return path.read_bytes()
        local_path = output_dir / Path(value).name
        if local_path.exists() and local_path.is_file():
            return local_path.read_bytes()
    recent = sorted(output_dir.glob("*.*"), key=lambda item: item.stat().st_mtime, reverse=True)
    for path in recent:
        if path.suffix.lower() in {".wav", ".mp3", ".flac", ".ogg", ".m4a"}:
            return path.read_bytes()
    return b""


def _build_melody_rewrite_lyrics(source_title: str, theme: str, template: dict[str, Any]) -> str:
    lines = list(template.get("lines") or [])[:72]
    creative_theme = _clean_creative_theme(theme)
    story = _melody_story_plan(_clean_reference_title(source_title), creative_theme)
    group = story["rhymes"]
    output: list[str] = []
    current_section = ""
    section_counts: dict[str, int] = {}
    used_counts: dict[str, int] = {}
    for index, line in enumerate(lines):
        section = str(line.get("section") or "Verse")
        if section != current_section:
            output.append("")
            output.append(f"[{section}]")
            current_section = section
        target = int(line.get("charCount") or 10)
        section_counts[section] = section_counts.get(section, 0) + 1
        section_index = section_counts[section] - 1
        rhyme = group[index % len(group)] if line.get("rhymeSlot") else ""
        contour = str(line.get("pitchContour") or "stable")
        raw = _lyric_candidate(story, section, section_index, contour, target)
        fitted = _fit_lyric_line(raw, target, rhyme, story, section, section_index, used_counts)
        used_counts[fitted] = used_counts.get(fitted, 0) + 1
        output.append(fitted)
    return "\n".join(output).strip() + "\n"


def _melody_story_plan(source_title: str, theme: str) -> dict[str, Any]:
    text = f"{source_title} {theme}"
    if "雪" in text:
        return {
            "seed": text,
            "hook": "雪停以后我才敢回头",
            "shortHook": "雪停以后",
            "story": "一段失去后的冬夜告别：不是继续等一个人回来，而是在雪停以后把想念留在旧路口，学会体面放手。",
            "images": ["旧路口", "空站台", "玻璃窗", "围巾角", "半盏灯", "白月光", "旧街口", "回程票"],
            "actions": ["收好", "放下", "路过", "等过", "写完", "藏进", "走出", "轻放"],
            "rhymes": ["回头", "路口", "温柔", "等候", "伤口", "自由"],
            "chorus": [
                "雪停以后我才敢回头",
                "把你留在旧路口",
                "雪停以后我学会放手",
                "等春风替我开口",
                "遗憾像灯落在胸口",
                "照见我一生温柔",
                "雪停以后我不再等候",
                "把孤单走成自由",
            ],
            "verse": [
                "那年冬天风穿过旧路口",
                "你的名字还停在我胸口",
                "我把围巾叠进空抽屉",
                "像把一场梦轻轻收走",
                "后来城市亮起半盏灯",
                "我才承认爱不能强留",
                "有些告别不用说出口",
                "雪落下来替我低头",
            ],
            "bridge": [
                "如果重逢只剩沉默",
                "我愿把祝福说得温柔",
                "把昨天归还给昨天",
                "让明天慢慢牵我的手",
                "人海那么宽我不再追问",
                "谁会陪我走到最后",
            ],
        }
    return {
        "seed": text,
        "hook": _make_hook_phrase(_meaningful_keywords(theme or source_title), source_title),
        "shortHook": _make_hook_phrase(_meaningful_keywords(theme or source_title), source_title)[:6],
        "story": "一个人在旧时光里与自己和解，带着遗憾继续往前走。",
        "images": ["晚风", "旧街", "月光", "灯火", "人海", "清晨", "归途", "窗边"],
        "actions": ["照亮", "安放", "越过", "收藏", "靠近", "等候", "放下", "启航"],
        "rhymes": ["温柔", "回头", "自由", "渡口", "等候", "以后"],
        "chorus": [
            "把心事唱给晚风听",
            "让遗憾慢慢归零",
            "我穿过人海和旧梦",
            "终于看见天会晴",
            "那些没说完的深情",
            "都变成路上的星",
        ],
        "verse": [
            "晚风经过安静旧街",
            "我把心事放慢一些",
            "有些名字藏进岁月",
            "仍在梦里轻轻明灭",
            "后来学会不再追问",
            "把每次失去都体面",
        ],
        "bridge": [
            "若命运还要我经过黑夜",
            "我就把歌声留在身边",
            "等到清晨推开窗沿",
            "心会重新变得辽远",
        ],
    }


def _lyric_candidate(story: dict[str, Any], section: str, index: int, contour: str, target_count: int) -> str:
    if "Chorus" in section:
        pool = story["chorus"]
    elif "Bridge" in section:
        pool = story["bridge"]
    elif "Intro" in section:
        pool = [story["shortHook"], story["hook"], "旧路口还留着温柔", "我把冬天慢慢收走"]
    else:
        pool = story["verse"]
    base = str(pool[index % len(pool)])
    if target_count <= 7 and "Chorus" in section:
        short_pool = [story["shortHook"], "旧路口", "别回头", "慢慢走", "会自由"]
        return str(short_pool[index % len(short_pool)])
    if contour == "rising" and target_count >= 10 and "Chorus" not in section:
        rising = ["我终于敢向春天开口", "把旧梦交给风带走", "心在雪后慢慢自由"]
        return rising[index % len(rising)]
    if contour == "falling" and target_count >= 10 and "Chorus" not in section:
        falling = ["我把想念轻轻收走", "不再问你是否回头", "让沉默落在旧路口"]
        return falling[index % len(falling)]
    return base


def _fit_lyric_line(raw: str, target_count: int, rhyme: str, story: dict[str, Any], section: str, index: int, used_counts: dict[str, int]) -> str:
    target = max(4, min(22, int(target_count or 10)))
    candidates = _line_variants(raw, story, section, index)
    if rhyme:
        candidates.extend(_rhyme_variants(story, section, index, rhyme))
    best = ""
    best_score = 10_000
    for candidate in candidates:
        text = _clean_lyric_text(candidate)
        if not text:
            continue
        length = _chinese_len(text)
        score = abs(length - target)
        if rhyme:
            score += -1 if text.endswith(rhyme) else 2
        score += used_counts.get(text, 0) * 9
        if "Chorus" not in section and text.startswith(str(story.get("shortHook") or "")):
            score += 5
        if length > target + 3:
            score += 6
        if length < max(4, target - 4):
            score += 4
        if _looks_broken_lyric(text):
            score += 20
        if score < best_score:
            best = text
            best_score = score
    if best:
        return best
    return _clean_lyric_text(raw)


def _line_variants(raw: str, story: dict[str, Any], section: str, index: int) -> list[str]:
    image = story["images"][index % len(story["images"])]
    hook = story["hook"]
    variants = [
        raw,
        hook,
        story["shortHook"],
        f"{image}还留着温柔",
        f"不再问你是否回头",
        f"把想念留在旧路口",
        f"让沉默落在旧路口",
        f"心在雪后慢慢自由",
        "我把围巾叠进空抽屉",
        "旧车站还等在风里",
        "玻璃窗映着谁的背影",
        "半盏灯陪我到清晨",
        "回程票夹在旧书里",
        "白月光落在空椅",
        "那句再见终于说轻",
        "我把冬天慢慢收起",
    ]
    if "Chorus" in section:
        variants.extend(story["chorus"])
    elif "Bridge" in section:
        variants.extend(story["bridge"])
    else:
        variants.extend(story["verse"])
    return variants


def _rhyme_variants(story: dict[str, Any], section: str, index: int, rhyme: str) -> list[str]:
    by_rhyme = {
        "回头": ["雪停以后我才敢回头", "可我终于不再回头", "风把旧梦吹回头"],
        "路口": ["把你留在那年旧路口", "我把想念放在旧路口", "月光停在空空旧路口"],
        "温柔": ["照见我余生的温柔", "愿你此后仍有温柔", "我把遗憾放成温柔"],
        "等候": ["转身以后不再等候", "再痛也不必原地等候", "雪落尽我停止等候"],
        "伤口": ["遗憾像灯照着伤口", "那些誓言还在伤口", "我把沉默按住伤口"],
        "自由": ["把孤单走成自由", "一个人也走向自由", "心在雪后慢慢自由"],
        "以后": ["雪停以后我学会放手", "转身以后不再等候", "多年以后还算温柔"],
        "渡口": ["月光停在无人渡口", "我把往事送过渡口", "旧梦停在城市渡口"],
    }
    variants = list(by_rhyme.get(rhyme, []))
    if "Chorus" in section:
        variants.extend([line for line in story["chorus"] if str(line).endswith(rhyme)])
    return variants


def _clean_lyric_text(value: str) -> str:
    text = re.sub(r"[\s，。,.!?！？；;：:、]+", "", str(value or ""))
    text = re.sub(r"(发光){2,}", "发光", text)
    text = re.sub(r"(远方){2,}", "远方", text)
    text = re.sub(r"(天亮){2,}", "天亮", text)
    return text.strip()


def _looks_broken_lyric(text: str) -> bool:
    broken_markers = ("以越", "和自去", "继发", "心急继", "汗纱", "发心发", "雪地")
    if any(marker in text for marker in broken_markers):
        return True
    return bool(re.search(r"(.{1,3})\1\1", text))


# Commercial melody-rewrite v2.
# Keep this override close to the old implementation so the stable generation,
# upload, queue and lyrics-sync code paths do not need to change.
def _chinese_len(value: str) -> int:
    return len(re.sub(r"[\s，,。.!！?？、：:；;《》“”\"'（）()\[\]【】]+", "", value or ""))


def _clean_lyric_text(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\[[^\]]+\]", "", text)
    text = re.sub(r"[，,。.!！?？、：:；;《》“”\"'（）()\[\]【】\s]+", "", text)
    text = re.sub(r"(发光|远方|天亮|温柔|旧路口)\1+", r"\1", text)
    return text.strip()


def _build_melody_rewrite_lyrics(source_title: str, theme: str, template: dict[str, Any]) -> str:
    lines = list(template.get("lines") or [])[:72]
    creative_theme = _clean_creative_theme(theme)
    story = _melody_story_plan(_clean_reference_title(source_title), creative_theme)
    output: list[str] = []
    current_section = ""
    section_counts: dict[str, int] = {}
    used_counts: dict[str, int] = {}
    for line in lines:
        section = str(line.get("section") or "Verse")
        if section != current_section:
            output.append("")
            output.append(f"[{section}]")
            current_section = section
        section_counts[section] = section_counts.get(section, 0) + 1
        section_index = section_counts[section] - 1
        bounds = _commercial_target_bounds(line, section)
        target = _commercial_target_count(line, section)
        rhyme = _select_rhyme(story, line, section, section_index)
        contour = str(line.get("pitchContour") or "stable")
        raw = _lyric_candidate(story, section, section_index, contour, target)
        fitted = _fit_lyric_line(raw, target, rhyme, story, section, section_index, used_counts, bounds=bounds)
        used_counts[fitted] = used_counts.get(fitted, 0) + 1
        output.append(fitted)
    return "\n".join(output).strip() + "\n"


def _reference_char_count(line: dict[str, Any]) -> int:
    source_text = str(line.get("sourceText") or "")
    explicit_count = int(line.get("charCount") or 0)
    text_count = _chinese_len(source_text)
    if explicit_count and text_count:
        return max(1, min(24, max(explicit_count, text_count)))
    return max(1, min(24, explicit_count or text_count or 6))


def _duration_char_window(duration_ms: int) -> tuple[int, int]:
    seconds = max(0.1, int(duration_ms or 0) / 1000.0)
    if seconds <= 1.0:
        return 1, 3
    if seconds < 1.2:
        return 1, 4
    if seconds < 1.8:
        return 2, 5
    if seconds < 2.5:
        return 3, 7
    if seconds < 3.5:
        return 4, 8
    if seconds < 4.5:
        return 4, 10
    if seconds < 6.0:
        return 5, 12
    return 5, 15


def _commercial_target_bounds(line: dict[str, Any], section: str) -> tuple[int, int]:
    source_count = _reference_char_count(line)
    duration_ms = int(line.get("durationMs") or 4500)
    duration_min, duration_max = _duration_char_window(duration_ms)

    near_min = max(1, source_count - 1)
    # Keep source-length as a soft hint, not a hard cap. Long slots need room
    # for natural Chinese phrasing; otherwise the model is forced to cram.
    duration_friendly_max = max(duration_max, source_count + max(2, duration_ms // 3500))
    near_max = min(24, duration_friendly_max)
    if "Chorus" in section and duration_ms >= 3000:
        near_max = min(24, near_max + 1)
    if "Bridge" in section and duration_ms >= 4000:
        near_max = min(24, near_max + 1)

    min_count = max(duration_min, min(near_min, duration_max))
    max_count = max(min_count, near_max)

    # Template cleanup can add stricter per-line limits after VAD/pitch checks.
    try:
        if line.get("lyricMinChars") is not None:
            min_count = max(min_count, int(line.get("lyricMinChars") or min_count))
        if line.get("lyricMaxChars") is not None:
            max_count = min(max_count, int(line.get("lyricMaxChars") or max_count))
    except Exception:
        pass
    if min_count > max_count:
        min_count = max_count
    return max(1, min_count), max(1, max_count)


def _commercial_target_count(line: dict[str, Any], section: str) -> int:
    min_count, max_count = _commercial_target_bounds(line, section)
    preferred = _reference_char_count(line)
    return max(min_count, min(max_count, preferred))


def _select_rhyme(story: dict[str, Any], line: dict[str, Any], section: str, section_index: int) -> str:
    if not line.get("rhymeSlot"):
        return ""
    if "Chorus" in section:
        group = story.get("chorusRhymes") or story.get("rhymes") or []
    elif "Bridge" in section:
        group = story.get("bridgeRhymes") or story.get("rhymes") or []
    else:
        group = story.get("verseRhymes") or story.get("rhymes") or []
    return str(group[section_index % len(group)]) if group else ""


def _melody_story_plan(source_title: str, theme: str) -> dict[str, Any]:
    text = f"{source_title} {theme}"
    if "年少" in text or "少年" in text:
        return {
            "seed": text,
            "hook": "把少年留在晚风里",
            "shortHook": "少年晚风",
            "story": "一个成年人回望青春、朋友、奔跑和错过，不卖惨，不空喊励志；副歌要有记忆点，像把年轻时的自己温柔送别。",
            "images": ["旧操场", "单车铃", "晚风", "老街口", "毕业照", "白衬衫", "旧球鞋", "站台灯", "课桌角", "远山"],
            "repeatLimitTokens": ["少年", "晚风", "回头", "青春", "自由"],
            "rhymes": ["跑", "笑", "桥", "潮", "遥", "好"],
            "verseRhymes": ["口", "跑", "角", "笑"],
            "chorusRhymes": ["里", "起", "你", "已"],
            "bridgeRhymes": ["口", "手", "走", "候"],
            "commercialHook": [
                "把少年留在晚风里",
                "让旧日的梦慢慢远去",
                "我和从前轻轻道一句",
                "谢谢你曾陪我跑下去",
            ],
            "chorus": [
                "把少年留在晚风里",
                "让旧日的梦慢慢远去",
                "我和从前轻轻道一句",
                "谢谢你曾陪我跑下去",
                "花会再开人却回不去",
                "我把遗憾唱成了勇气",
                "走过半生才终于懂你",
                "年少的光还在心底",
            ],
            "verse": [
                "旧操场还记得奔跑",
                "单车铃穿过老街口",
                "白衬衫晾在那个午后",
                "旧球鞋踩碎一路风",
                "后来朋友各自远走",
                "毕业照还笑得温柔",
                "课桌角刻着没说出口",
                "站台灯照着人海尽头",
            ],
            "bridge": [
                "如果青春还能回一次头",
                "我想对当年的我挥手",
                "别怕后来风雨那么厚",
                "你已经把梦唱到最后",
                "那些没兑现的远走",
                "也会变成心里的灯火",
            ],
            "targetPatterns": {
                "Chorus": [
                    "把少年留在晚风里",
                    "让旧日的梦慢慢远去",
                    "我和从前轻轻道一句",
                    "谢谢你曾陪我跑下去",
                ],
                "Final Chorus": [
                    "把少年留在晚风里",
                    "让一路风雨慢慢远去",
                    "我和从前轻轻道一句",
                    "谢谢你曾照亮我心底",
                ],
            },
            "risingLines": ["我终于懂了那时的自己", "让旧日的梦慢慢远去", "年少的光还在心底"],
            "fallingLines": ["我把旧梦轻轻收起", "不再追问谁会回去", "把少年留在晚风里"],
            "genericVariants": [
                "旧操场还记得奔跑",
                "单车铃穿过老街口",
                "后来朋友各自远走",
                "毕业照还笑得温柔",
                "花会再开人却回不去",
                "我把遗憾唱成了勇气",
            ],
            "rhymeVariants": {
                "跑": ["旧操场还记得奔跑", "那年我们迎着风奔跑", "曾为梦想不停奔跑", "心里还有少年的奔跑"],
                "笑": ["毕业照还笑得温柔", "想起从前我仍会笑", "岁月带走当年的笑", "回忆深处有人在笑"],
                "桥": ["晚风吹过旧日的桥", "我们走散在那座桥", "月光还落在远方桥", "旧梦停在青春的桥"],
                "潮": ["人海翻成沉默的潮", "岁月推着我往前潮", "青春像退去的海潮", "心事落进晚风的潮"],
                "遥": ["年少的路已经很遥", "那道背影渐渐远遥", "远山之外仍有歌谣", "我把旧梦唱到云遥"],
                "好": ["愿从前的你一切都好", "把遗憾也轻轻放好", "后来才懂平凡也好", "风雨过后仍要好好"],
                "里": ["把少年留在晚风里", "年少的光还在心底", "旧日的梦藏在歌里", "我把遗憾放进风里"],
                "起": ["让旧日的梦慢慢远去", "谢谢你曾陪我跑下去", "我把昨天轻轻收起", "走过半生仍有勇气"],
                "你": ["我和从前轻轻道一句", "多年以后我才懂你", "青春深处还有一个你", "谢谢你曾照亮我心底"],
            },
        }
    if "雪" in text or "我要的不是雪" in text:
        return {
            "seed": text,
            "hook": "雪停以后我才敢转身走",
            "shortHook": "雪停以后",
            "story": "一个人从执念、疼痛、体面告别，走到真正放手。画面要具体，副歌要容易记住，结尾要有余韵。",
            "images": ["旧路口", "空站台", "玻璃窗", "围巾角", "半盏灯", "回程票", "白月光", "旧街口", "空抽屉", "雪后风"],
            "repeatLimitTokens": ["旧路口", "温柔", "回头", "自由", "伤口"],
            "rhymes": ["走", "口", "候", "由", "手", "痛"],
            "verseRhymes": ["口", "走", "痛", "候"],
            "chorusRhymes": ["走", "候", "手", "由"],
            "bridgeRhymes": ["口", "手", "由", "候"],
            "commercialHook": [
                "雪停以后我才敢转身走",
                "把你的名字还给旧路口",
                "不是不痛是我终于懂",
                "爱到最后也要放过双手",
            ],
            "chorus": [
                "雪停以后我才敢转身走",
                "把你的名字还给旧路口",
                "不是不痛是我终于懂",
                "爱到最后也要放过双手",
                "雪停以后我不再等候",
                "把一场梦慢慢走成自由",
                "回忆越冷我越要抬头",
                "让明天替我收留伤口",
            ],
            "verse": [
                "那年冬天风吹过旧路口",
                "你的名字还压在我胸口",
                "我把围巾叠进空抽屉",
                "像把一场梦轻轻收走",
                "后来城市亮起半盏灯",
                "我才承认爱不能强求",
                "有些告别不用说出口",
                "雪落下来替我低头",
                "空站台还等着一阵风",
                "回程票夹在旧书里头",
                "玻璃窗映出我的沉默",
                "白月光照着没有你的楼",
            ],
            "bridge": [
                "如果重逢只剩一句保重",
                "我愿把祝福说得温柔",
                "把昨天归还给昨天以后",
                "让明天慢慢牵我的手",
                "人海那么宽我不再追问",
                "谁会陪我走到最后",
            ],
            "targetPatterns": {
                "Chorus": [
                    "雪停以后我才敢转身走",
                    "把你的名字还给旧路口",
                    "不是不痛是我终于懂",
                    "爱到最后也要放过双手",
                ],
                "Final Chorus": [
                    "雪停以后我终于往前走",
                    "把你的名字留在旧路口",
                    "不是遗忘是我学会懂",
                    "爱过的人也能彼此放手",
                ],
            },
            "risingLines": ["我终于敢向春天开口", "把旧梦交给风带走", "心在雪后慢慢自由"],
            "fallingLines": ["我把想念轻轻收走", "不再问你是否回头", "让沉默落在旧路口"],
            "genericVariants": [
                "后来我学会一个人走",
                "不再问你会不会回头",
                "把一场梦慢慢走成自由",
                "我把昨天轻轻归还风中",
                "爱到最后也要放过双手",
                "不是不痛是我终于懂",
            ],
            "rhymeVariants": {
                "走": ["雪停以后我才敢转身走", "后来我学会一个人走", "把一场梦慢慢往前走", "痛到尽头我也继续走"],
                "口": ["把你的名字还给旧路口", "我把想念安放在旧路口", "玻璃窗照见沉默的伤口", "让明天替我收留伤口"],
                "候": ["雪停以后我不再等候", "再痛也不必原地等候", "后来我不再为谁等候", "把旧时光留在风里等候"],
                "由": ["把一场梦慢慢走成自由", "一个人也可以走向自由", "雪后的心终于学会自由", "放下以后才算真的自由"],
                "手": ["爱到最后也要放过双手", "让明天慢慢牵我的手", "我把遗憾交还给双手", "别让回忆再困住双手"],
                "痛": ["不是不痛是我终于懂", "你的沉默比风还要痛", "回忆越冷我越要懂", "爱到深处才知道会痛"],
                "梦": ["你把誓言吹散成旧梦", "我把冬天归还给旧梦", "雪落下来埋住那场梦", "后来我走出了那场梦"],
            },
        }
    keywords = _meaningful_keywords(theme or source_title)
    hook = _make_hook_phrase(keywords, source_title)
    title_hint = source_title or theme or "人生路"
    return {
        "seed": text,
        "hook": hook,
        "shortHook": hook[:6],
        "story": "一个人从现实起伏中学会沉稳向前，画面具体，副歌有可记忆的核心句，结尾有力量。",
        "images": ["晚风", "长街", "月光", "灯火", "人海", "清晨", "归途", "窗边", "山路", "渡口"],
        "repeatLimitTokens": ["温柔", "回头", "自由", "远方", "天亮", "路口"],
        "rhymes": ["走", "口", "候", "由", "手", "梦"],
        "verseRhymes": ["口", "走", "梦", "候"],
        "chorusRhymes": ["走", "候", "手", "由"],
        "bridgeRhymes": ["口", "手", "由", "候"],
        "commercialHook": [
            f"{hook}我还继续走",
            "把半生风雨酿成一口酒",
            "不是不痛是终于看透",
            "人间道场教我低头抬头",
        ],
        "chorus": [
            f"{hook}我还继续走",
            "把半生风雨酿成一口酒",
            "不是不痛是终于看透",
            "人间道场教我低头抬头",
            "那些没说完的执着",
            "都变成脚下的山丘",
        ],
        "verse": [
            "晚风经过安静长街",
            "我把心事放慢一些",
            "人到半途才懂进退",
            "低头也是一种体面",
            "后来学会不再争辩",
            "把每次跌倒都成全",
            "山路弯弯照见从前",
            "一盏灯陪我过长夜",
        ],
        "bridge": [
            "若命运还要我经过黑夜",
            "我就把歌声留在身边",
            "等到清晨推开窗沿",
            "心会重新变得辽远",
            "人生这一场修行未完",
            "我仍愿认真过每一天",
        ],
        "targetPatterns": {},
        "risingLines": ["我终于敢向明天开口", "把旧梦交给风带走", "心在路上慢慢自由"],
        "fallingLines": ["我把心事轻轻收走", "不再问谁是否回头", "让沉默落在长街尽头"],
        "genericVariants": [
            "后来我学会一个人走",
            "不再问谁会不会回头",
            "把半生风雨酿成一口酒",
            "我把昨天轻轻归还风中",
            "不是不痛是终于看透",
            "人间道场教我低头抬头",
            f"{title_hint}里我继续走",
        ],
        "rhymeVariants": {
            "走": ["后来我学会一个人走", "把半生风雨往前走", "跌到尽头我也继续走", f"{title_hint}里我继续走"],
            "口": ["把沉默留在人生渡口", "把心事放在风的路口", "一盏灯照见胸中伤口", "让明天替我收留伤口"],
            "候": ["后来我不再为谁等候", "把旧时光留在风里等候", "走过半生才懂等候", "清晨替我慢慢等候"],
            "由": ["一个人也可以走向自由", "放下以后才算真的自由", "风吹过后心慢慢自由", "人到中年才学会自由"],
            "手": ["让明天慢慢牵我的手", "我把遗憾交还给双手", "别让回忆再困住双手", "低头以后再握紧双手"],
            "梦": ["你把誓言吹散成旧梦", "我把昨天归还给旧梦", "后来我走出了那场梦", "半生风雨醒成一场梦"],
        },
    }


def _lyric_candidate(story: dict[str, Any], section: str, index: int, contour: str, target_count: int) -> str:
    patterns = story.get("targetPatterns") or {}
    if section in patterns:
        pool = patterns[section]
    elif "Chorus" in section:
        pool = story["chorus"]
    elif "Bridge" in section:
        pool = story["bridge"]
    elif "Intro" in section:
        pool = [story["hook"], story["shortHook"], *story["verse"][:3]]
    else:
        pool = story["verse"]
    base = str(pool[index % len(pool)])
    if contour == "rising" and "Chorus" not in section:
        lines = story.get("risingLines") or ["我终于敢向明天开口", "把旧梦交给风带走", "心在路上慢慢自由"]
        return str(lines[index % len(lines)])
    if contour == "falling" and "Chorus" not in section:
        lines = story.get("fallingLines") or ["我把心事轻轻收走", "不再问谁是否回头", "让沉默落在长街尽头"]
        return str(lines[index % len(lines)])
    return base


def _short_line_candidates(
    story: dict[str, Any],
    section: str,
    index: int,
    min_count: int,
    max_count: int,
    target: int,
    rhyme: str = "",
) -> list[str]:
    pools = [
        "\u653e\u4e0b",  # 放下
        "\u522b\u56de\u5934",  # 别回头
        "\u5411\u524d\u8d70",  # 向前走
        "\u98ce\u7ecf\u8fc7",  # 风经过
        "\u68a6\u9192\u4e86",  # 梦醒了
        "\u5fc3\u653e\u4e0b",  # 心放下
        "\u96ea\u505c\u540e",  # 雪停后
        "\u6211\u8be5\u8d70",  # 我该走
        "\u65e7\u68a6\u653e\u4e0b",  # 旧梦放下
        "\u98ce\u91cc\u8fdc\u8d70",  # 风里远走
        "\u4e0d\u518d\u7b49\u5019",  # 不再等候
        "\u5fc3\u4e8b\u6162\u6162\u653e\u4e0b",  # 心事慢慢放下
        "\u660e\u5929\u8fd8\u4f1a\u4eae",  # 明天还会亮
        "\u628a\u65e7\u68a6\u8fd8\u7ed9\u98ce",  # 把旧梦还给风
    ]
    if "Chorus" in section:
        pools.extend(
            [
                "\u96ea\u505c\u540e\u6211\u8d70",  # 雪停后我走
                "\u75db\u8fc7\u624d\u61c2",  # 痛过才懂
                "\u7231\u8fc7\u4e5f\u653e\u624b",  # 爱过也放手
                "\u6211\u7ec8\u4e8e\u6562\u653e\u624b",  # 我终于敢放手
            ]
        )
    else:
        pools.extend(
            [
                "\u65e7\u8def\u53e3\u8d77\u98ce",  # 旧路口起风
                "\u6708\u5149\u7167\u65e7\u697c",  # 月光照旧楼
                "\u8f6c\u8eab\u522b\u518d\u7b49",  # 转身别再等
            ]
        )
    pools.extend(story.get("shortVariants") or [])
    if max_count >= 8:
        pools.extend(
            [
                "\u96ea\u505c\u4ee5\u540e\u6211\u7ee7\u7eed\u8d70",  # 雪停以后我继续走
                "\u4e0d\u662f\u4e0d\u75db\u6211\u7ec8\u4e8e\u61c2\u4e86",  # 不是不痛我终于懂了
                "\u628a\u4f60\u7684\u540d\u5b57\u8fd8\u7ed9\u65e7\u8def\u53e3",  # 把你的名字还给旧路口
                "\u6211\u628a\u65e7\u68a6\u8f7b\u8f7b\u8fd8\u7ed9\u98ce",  # 我把旧梦轻轻还给风
                "\u8f6c\u8eab\u4ee5\u540e\u4e0d\u518d\u4e3a\u8c01\u7b49",  # 转身以后不再为谁等
                "\u7231\u5230\u6700\u540e\u7ec8\u4e8e\u5b66\u4f1a\u653e\u624b",  # 爱到最后终于学会放手
            ]
        )
    if max_count >= 11:
        pools.extend(
            [
                "\u96ea\u505c\u4ee5\u540e\u6211\u624d\u6562\u8f6c\u8eab\u8d70",  # 雪停以后我才敢转身走
                "\u628a\u4e00\u573a\u65e7\u68a6\u6162\u6162\u5531\u6210\u81ea\u7531",  # 把一场旧梦慢慢唱成自由
                "\u4e0d\u518d\u628a\u6628\u5929\u5f53\u6210\u4e00\u751f\u7684\u5019",  # 不再把昨天当成一生的候
            ]
        )
    clean_rhyme = str(rhyme or "")
    # Do not force rhyme by replacing the last character. That creates unnatural
    # phrases such as "放走". Rhyme should come from curated candidates only.
    seen: set[str] = set()
    result: list[str] = []
    for item in pools:
        text = _clean_lyric_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        length = _chinese_len(text)
        if length <= max_count:
            result.append(text)
    result.sort(key=lambda value: (0 if min_count <= _chinese_len(value) <= max_count else 1, abs(_chinese_len(value) - target)))
    return result


def _fit_lyric_line(
    raw: str,
    target_count: int,
    rhyme: str,
    story: dict[str, Any],
    section: str,
    index: int,
    used_counts: dict[str, int],
    bounds: tuple[int, int] | None = None,
) -> str:
    if bounds is None:
        center = max(1, int(target_count or 6))
        bounds = (max(1, center - 1), min(15, center + 1))
    min_count, max_count = max(1, int(bounds[0])), max(1, int(bounds[1]))
    if min_count > max_count:
        min_count = max_count
    target = max(min_count, min(max_count, int(target_count or min_count)))
    candidates = _line_variants(raw, story, section, index, target)
    if rhyme:
        candidates.extend(_rhyme_variants(story, section, index, rhyme, target))
    candidates.extend(_short_line_candidates(story, section, index, min_count, max_count, target, rhyme))
    best = ""
    best_score = 100_000
    for candidate in candidates:
        text = _clean_lyric_text(candidate)
        if not text:
            continue
        length = _chinese_len(text)
        if length > max_count:
            continue
        score = abs(length - target) * 4
        if length < min_count:
            score += (min_count - length) * 12
        if rhyme:
            score += -4 if text.endswith(rhyme) else 5
        score += used_counts.get(text, 0) * 30
        score += _repetition_penalty(text, story, used_counts)
        if "Chorus" not in section and text.startswith(str(story.get("shortHook") or "")):
            score += 8
        if max_count > 4 and _looks_broken_lyric(text):
            score += 60
        if _too_generic_commercial_line(text):
            score += 16
        if score < best_score:
            best = text
            best_score = score
    if best:
        return best
    fallback = _short_line_candidates(story, section, index, min_count, max_count, target, rhyme)
    if fallback:
        return fallback[0]
    clean_raw = _clean_lyric_text(raw)
    return clean_raw if _chinese_len(clean_raw) <= max_count else "\u5411\u524d\u8d70"


def _line_variants(raw: str, story: dict[str, Any], section: str, index: int, target: int = 12) -> list[str]:
    image = story["images"][index % len(story["images"])]
    next_image = story["images"][(index + 3) % len(story["images"])]
    variants = [
        raw,
        *_commercial_expand_line(raw, target),
        f"{image}还藏着没说完的痛",
        f"{image}替我留住你的回眸",
        f"我把想念安放在{image}",
        f"{next_image}照见我沉默的伤口",
    ]
    variants.extend(story.get("genericVariants") or [])
    if "Chorus" in section:
        variants.extend(story["chorus"])
        variants.extend(story.get("commercialHook") or [])
    elif "Bridge" in section:
        variants.extend(story["bridge"])
    else:
        variants.extend(story["verse"])
    return variants


def _commercial_expand_line(text: str, target: int) -> list[str]:
    clean = _clean_lyric_text(text)
    if not clean:
        return []
    length = _chinese_len(clean)
    variants = [clean]
    if length >= target - 1:
        return variants
    soft_prefixes = ["后来", "终于", "直到", "原来"]
    variants.extend(f"{prefix}{clean}" for prefix in soft_prefixes if not clean.startswith(tuple(soft_prefixes)))
    commercial_rewrites = {
        "不是不痛是我终于懂": ["不是不痛是我终于懂得放手", "不是不痛是我终于懂了"],
        "我愿把祝福说得温柔": ["我愿把祝福说得更温柔", "我愿把祝福说得很温柔"],
        "谁会陪我走到最后": ["谁会陪我走到最后一程", "谁会陪我走到最后"],
        "雪落下来替我低头": ["雪落下来替我慢慢低头", "雪落下来替我沉默低头"],
        "我把围巾叠进空抽屉": ["我把围巾叠进空空抽屉", "我把围巾叠回空抽屉"],
        "空站台还等着一阵风": ["空站台还等着昨夜的风", "空站台还等着你的晚风"],
        "回程票夹在旧书里头": ["回程票夹在泛黄旧书里", "回程票还夹在旧书里头"],
        "玻璃窗映出我的沉默": ["玻璃窗映出沉默的我", "玻璃窗还映着我的沉默"],
        "白月光照着没有你的楼": ["白月光照着没有你的楼", "白月光落在没有你的楼"],
        "如果重逢只剩一句保重": ["如果重逢只剩一句保重", "若再重逢只剩一句保重"],
        "把昨天归还给昨天以后": ["把昨天归还给昨天以后", "把昨天归还给旧梦以后"],
        "让明天慢慢牵我的手": ["让明天慢慢牵起我的手", "让明天重新牵住我的手"],
        "人海那么宽我不再追问": ["人海那么宽我不再追问", "人海太宽我不再追问"],
    }
    variants.extend(commercial_rewrites.get(clean, []))
    result: list[str] = []
    for variant in variants:
        variant = _clean_lyric_text(variant)
        count = _chinese_len(variant)
        if target - 2 <= count <= target + 2:
            result.append(variant)
    return result or [clean]


def _rhyme_variants(story: dict[str, Any], section: str, index: int, rhyme: str, target: int = 12) -> list[str]:
    by_rhyme = story.get("rhymeVariants") or {}
    variants = list(by_rhyme.get(rhyme, []))
    if "Chorus" in section:
        variants.extend([line for line in story["chorus"] if _clean_lyric_text(line).endswith(rhyme)])
    return variants


def _repetition_penalty(text: str, story: dict[str, Any], used_counts: dict[str, int]) -> int:
    penalty = 0
    for token in story.get("repeatLimitTokens", []):
        if not token or token not in text:
            continue
        seen = sum(count for line, count in used_counts.items() if token in line)
        if seen >= 4:
            penalty += 30
        elif seen >= 2:
            penalty += 10
    for prefix in ("后来", "终于", "直到", "原来"):
        if not text.startswith(prefix):
            continue
        seen_prefix = sum(count for line, count in used_counts.items() if line.startswith(prefix))
        if seen_prefix >= 5:
            penalty += 25
        elif seen_prefix >= 3:
            penalty += 10
    for phrase in ("还藏着没说完的痛", "替我留住你的回眸", "照见我沉默的伤口", "我把想念安放在"):
        if phrase not in text:
            continue
        seen_phrase = sum(count for line, count in used_counts.items() if phrase in line)
        if seen_phrase >= 2:
            penalty += 70
        elif seen_phrase >= 1:
            penalty += 28
    return penalty


def _too_generic_commercial_line(text: str) -> bool:
    return any(word in text for word in ("发光", "远方", "天亮", "星光", "梦想", "勇敢飞"))


def _looks_broken_lyric(text: str) -> bool:
    if not text or _chinese_len(text) < 4:
        return True
    broken_markers = ("以及", "和自去", "继发", "心急继", "汗纱", "发心发", "雪地", "你的的", "我的的", "后来后来", "终于终于")
    if any(marker in text for marker in broken_markers):
        return True
    return bool(re.search(r"(.{1,3})\1\1", text))


def _build_lyrics(source_title: str, theme: str, source_lyrics: str) -> str:
    creative_theme = _clean_creative_theme(theme)
    reference_title = _clean_reference_title(source_title)
    seed_text = creative_theme or reference_title or source_lyrics
    keywords = _meaningful_keywords(seed_text)
    hook = _make_hook_phrase(keywords, reference_title)
    image = _image_from_keywords(keywords, seed_text)
    place = _place_from_keywords(keywords, seed_text)
    variant = _seed_index(seed_text, 3)
    if variant == 1:
        return f"""[Intro]
{image}落在{place}
我把心事慢慢放亮

[Verse 1]
一路走过沉默的晚上
有人离开也有人回望
旧日的光还在掌心发烫
提醒我别急着投降

[Pre Chorus]
风把遗憾吹成了远方
我把倔强收进胸膛
越过那些没说完的伤
听见自己还在歌唱

[Chorus]
{hook}
让眼泪也有去处安放
{hook}
把黑夜唱成微亮
不怕人海太过匆忙
只怕忘了最初的模样
{hook}
我会慢慢走到天亮

[Verse 2]
后来懂得沉默也滚烫
平凡日子也值得回响
把每一次跌撞都原谅
心就有了新的方向

[Bridge]
若往事还在风里摇晃
就让它陪我看一场月光
我不再追问失去的重量
只把明天握在手上

[Final Chorus]
{hook}
让眼泪也有去处安放
{hook}
把黑夜唱成微亮
走到最后回头望
风雨也曾替我鼓掌
{hook}
我会慢慢走到天亮

[Outro]
{image}落在{place}
心终于变得晴朗"""
    if variant == 2:
        return f"""[Intro]
我听见{image}经过窗
像你轻轻说别慌

[Verse 1]
城市醒来以前我还在路上
把没出口的话放进旧行囊
有些梦被时间磨得发亮
有些痛不必再声张

[Pre Chorus]
当风吹过{place}
我学会和自己商量
把遗憾写成温柔的句子
再把明天慢慢点亮

[Chorus]
{hook}
让我再勇敢一场
{hook}
哪怕世界忽明忽暗
我仍把爱留在胸膛
不让岁月空空回响
{hook}
总会有人听见光

[Verse 2]
年少以为答案都在远方
后来才懂家也在心上
热闹散去以后并不荒凉
清醒的人更懂滚烫

[Bridge]
如果命运还有风浪
我就把歌声铺成船桨
穿过沉默穿过迷惘
去见更好的自己一趟

[Final Chorus]
{hook}
让我再勇敢一场
{hook}
哪怕世界忽明忽暗
走到最后回头看
每道裂缝都有星光
{hook}
总会有人听见光

[Outro]
我听见{image}经过窗
像明天轻轻说别慌"""
    return f"""[Intro]
{hook}
让{image}轻轻落下

[Verse 1]
走过一些沉默的晚上
也曾把心事握到发烫
后来才懂人要学会退让
把喧哗留在远方

有些答案不必追问
有些离别不必声张
只要灯还亮在{place}
明天就还有微光

[Pre Chorus]
风吹过旧时光
我把遗憾慢慢收藏
不再和往事较量
让心回到安静的地方

[Chorus]
{hook}
把重的梦轻轻放下
{hook}
让眼里的雨变成晚霞
不怕路远不怕人散
只怕忘了温柔地抵达
{hook}
这一生也能开出花

[Post Chorus / Hook]
{hook}
心就不慌
把没说完的话
交给岁月慢慢讲

[Verse 2]
年少总想赢过人海
后来只想睡得安稳
热闹散场也不算失败
清醒本来就动人

有人奔赴山高水长
有人守着一盏灯光
各自都有各自的重量
别轻易比较悲伤

[Pre Chorus]
风吹过旧时光
我把遗憾慢慢收藏
不再和往事较量
让心回到安静的地方

[Chorus]
{hook}
把重的梦轻轻放下
{hook}
让眼里的雨变成晚霞
不怕路远不怕人散
只怕忘了温柔地抵达
{hook}
这一生也能开出花

[Bridge]
把昨天放回昨天
把明天看得简单
人这一生跌跌撞撞
总要学会释然

[Final Chorus]
{hook}
把重的梦轻轻放下
{hook}
让眼里的雨变成晚霞
走到最后回头看啊
风雨也曾送我回家
{hook}
这一生也能开出花

[Final Hook]
{hook}
心就不慌
把没说完的话
交给岁月慢慢讲

[Outro]
{hook}
让{image}轻轻落下"""


def _generate_with_comfy(caption: str, lyrics: str, max_duration: float, seed: int, output_prefix: str) -> bytes:
    base = settings.comfy_base_url.rstrip("/")
    _free_comfy_memory(base)
    try:
        return _generate_once_with_comfy(caption, lyrics, max_duration, seed, output_prefix, base)
    except Exception as exc:
        if not _is_retryable_comfy_error(exc):
            raise
        _log_exception("comfy retryable failure; freeing memory and retrying once", exc)
        _free_comfy_memory(base)
        time.sleep(5)
        return _generate_once_with_comfy(caption, lyrics, max_duration, seed, output_prefix, base)


def _generate_once_with_comfy(caption: str, lyrics: str, max_duration: float, seed: int, output_prefix: str, base: str) -> bytes:
    graph = _comfy_prompt(caption, lyrics, max_duration, seed, output_prefix)
    client_id = uuid.uuid4().hex
    with httpx.Client(timeout=settings.timeout_seconds) as client:
        response = client.post(f"{base}/prompt", json={"prompt": graph, "client_id": client_id})
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Comfy prompt submit failed: {_response_text(exc.response)}") from exc
        prompt_id = response.json().get("prompt_id")
        if not prompt_id:
            raise RuntimeError("Comfy did not return prompt_id")

        deadline = time.time() + settings.timeout_seconds
        last_status = ""
        while time.time() < deadline:
            time.sleep(settings.poll_interval_seconds)
            history_response = client.get(f"{base}/history/{prompt_id}")
            try:
                history_response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(f"Comfy history query failed: {_response_text(exc.response)}") from exc
            history = history_response.json().get(prompt_id) or {}
            status_info = history.get("status") or {}
            status = status_info.get("status_str") or ""
            if status:
                last_status = status
            outputs = history.get("outputs") or {}
            audio_items = ((outputs.get("35") or {}).get("audio") or [])
            if audio_items:
                item = audio_items[0]
                view = client.get(
                    f"{base}/view",
                    params={
                        "filename": item.get("filename"),
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                    },
                )
                try:
                    view.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise RuntimeError(f"Comfy audio fetch failed: {_response_text(exc.response)}") from exc
                return view.content
            if status in {"error", "failed"}:
                raise RuntimeError(f"Comfy generation failed: {status}; {_short_text(status_info)}")
        raise TimeoutError(f"Comfy generation timed out; last status={last_status}")


def _convert_singing_voice_with_seedvc(source_audio: Path, voice_reference: Path, output_dir: Path) -> Path:
    if not settings.seedvc_root.exists():
        raise HTTPException(status_code=503, detail=f"Seed-VC source is missing: {settings.seedvc_root}")
    if not settings.seedvc_python.exists():
        raise HTTPException(status_code=503, detail=f"Seed-VC python env is not ready: {settings.seedvc_python}")
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir = settings.seedvc_runtime_dir / uuid.uuid4().hex
    runtime_output = runtime_dir / "out"
    runtime_output.mkdir(parents=True, exist_ok=True)
    runtime_source = runtime_dir / f"source{source_audio.suffix or '.audio'}"
    runtime_reference = runtime_dir / f"reference{voice_reference.suffix or '.audio'}"
    shutil.copy2(source_audio, runtime_source)
    shutil.copy2(voice_reference, runtime_reference)
    command = [
        str(settings.seedvc_python),
        "inference.py",
        "--source",
        str(runtime_source),
        "--target",
        str(runtime_reference),
        "--output",
        str(runtime_output),
        "--diffusion-steps",
        str(max(4, int(settings.seedvc_diffusion_steps))),
        "--length-adjust",
        "1.0",
        "--inference-cfg-rate",
        "0.7",
        "--f0-condition",
        "True",
        "--auto-f0-adjust",
        "False",
        "--semi-tone-shift",
        "0",
        "--fp16",
        "True",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=settings.seedvc_root,
            env={**os.environ, "HF_ENDPOINT": os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")},
            text=True,
            capture_output=True,
            timeout=settings.timeout_seconds,
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "Seed-VC voice conversion failed").strip()[-1200:]
            raise HTTPException(status_code=503, detail=message)
        candidates = sorted(runtime_output.glob("*.wav"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not candidates:
            raise HTTPException(status_code=503, detail="Seed-VC did not produce a converted wav")
        final_path = output_dir / "voice_cloned.wav"
        shutil.copy2(candidates[0], final_path)
        return final_path
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _clone_vocals_and_remix(source_mix: Path, voice_reference: Path, output_dir: Path) -> Path:
    if not settings.seedvc_python.exists():
        raise HTTPException(status_code=503, detail=f"Seed-VC python env is not ready: {settings.seedvc_python}")
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir = settings.seedvc_runtime_dir / uuid.uuid4().hex
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime_source = runtime_dir / f"source{source_mix.suffix or '.mp3'}"
    runtime_reference = runtime_dir / f"reference{voice_reference.suffix or '.audio'}"
    shutil.copy2(source_mix, runtime_source)
    shutil.copy2(voice_reference, runtime_reference)

    try:
        stems_root = runtime_dir / "stems"
        demucs_command = [
            str(settings.seedvc_python),
            "-m",
            "demucs",
            "--two-stems=vocals",
            "-n",
            settings.demucs_model,
            "--out",
            str(stems_root),
            str(runtime_source),
        ]
        demucs_result = subprocess.run(
            demucs_command,
            cwd=runtime_dir,
            env={**os.environ, "HF_ENDPOINT": os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")},
            text=True,
            capture_output=True,
            timeout=settings.timeout_seconds,
        )
        if demucs_result.returncode != 0:
            message = (demucs_result.stderr or demucs_result.stdout or "Vocal separation failed").strip()[-1200:]
            raise HTTPException(status_code=503, detail=message)

        stem_dir = stems_root / settings.demucs_model / runtime_source.stem
        vocals = stem_dir / "vocals.wav"
        accompaniment = stem_dir / "no_vocals.wav"
        if not vocals.exists() or not accompaniment.exists():
            raise HTTPException(status_code=503, detail="Vocal separation did not produce vocals and accompaniment stems")

        cloned_vocal = _convert_singing_voice_with_seedvc(vocals, runtime_reference, runtime_dir / "converted_vocal")
        remixed_path = output_dir / "voice_cloned_mix.wav"
        _run_ffmpeg(
            [
                "-y",
                "-i",
                str(accompaniment),
                "-i",
                str(cloned_vocal),
                "-filter_complex",
                "[0:a]volume=1.12,bass=g=7:f=95:w=0.7,treble=g=1.5:f=7000,extrastereo=m=1.6[a0];[1:a]volume=0.95,highpass=f=75,lowpass=f=12500[a1];[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0,alimiter=limit=0.95[out]",
                "-map",
                "[out]",
                "-ar",
                "44100",
                "-ac",
                "2",
                str(remixed_path),
            ],
            "Remixing accompaniment and cloned vocal failed",
        )
        return remixed_path
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _encode_final_audio(source_audio: Path, destination: Path) -> Path:
    fmt = _final_audio_format()
    destination = destination.with_suffix(f".{fmt}")
    if fmt == "wav":
        shutil.copy2(source_audio, destination)
        return destination
    if fmt == "flac":
        command = [
            "-y",
            "-i",
            str(source_audio),
            "-af",
            "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-codec:a",
            "flac",
            "-compression_level",
            "8",
            str(destination),
        ]
    else:
        command = [
            "-y",
            "-i",
            str(source_audio),
            "-af",
            "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "0",
            str(destination),
        ]
    _run_ffmpeg(command, f"Encoding final {fmt} audio failed")
    return destination


def _final_audio_format() -> str:
    value = (settings.final_audio_format or "mp3").strip().lower()
    return value if value in {"mp3", "flac", "wav"} else "mp3"


def _ffmpeg_binary() -> str:
    configured = str(settings.ffmpeg_path or "").strip()
    candidate = _resolve_ffmpeg_candidate(configured)
    if candidate:
        return candidate
    found = shutil.which("ffmpeg")
    if found:
        return found
    return "ffmpeg"


def _ffmpeg_available() -> bool:
    binary = _ffmpeg_binary()
    if binary.lower() != "ffmpeg":
        return Path(binary).exists()
    return bool(shutil.which("ffmpeg"))


def _ensure_ffmpeg_on_path() -> None:
    binary = _ffmpeg_binary()
    path = Path(binary)
    if binary.lower() == "ffmpeg" or not path.exists():
        return
    parent = str(path.parent)
    current = os.environ.get("PATH", "")
    parts = [part.strip() for part in current.split(os.pathsep) if part.strip()]
    if parent not in parts:
        os.environ["PATH"] = parent + os.pathsep + current


def _resolve_ffmpeg_candidate(configured: str = "") -> str:
    candidates: list[Path] = []
    if configured:
        path = Path(configured)
        if path.is_dir():
            candidates.extend([path / "ffmpeg.exe", path / "bin" / "ffmpeg.exe", path / "ffmpeg"])
        else:
            candidates.append(path)
    env_binary = os.environ.get("IMAGEIO_FFMPEG_EXE") or os.environ.get("FFMPEG_BINARY")
    if env_binary:
        candidates.append(Path(env_binary))
    try:
        import imageio_ffmpeg

        candidates.append(Path(imageio_ffmpeg.get_ffmpeg_exe()))
    except Exception:
        pass
    candidates.extend(
        [
            settings.seedvc_python.parent / "ffmpeg.exe",
            settings.melody_svs_python.parent / "ffmpeg.exe",
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
            Path(r"D:\ffmpeg\bin\ffmpeg.exe"),
            Path(r"F:\ffmpeg\bin\ffmpeg.exe"),
            Path(r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"),
            Path(r"C:\Users\Admin\Documents\ComfyUI\ffmpeg\bin\ffmpeg.exe"),
            Path(r"C:\Users\Admin\ComfyUI\ffmpeg\bin\ffmpeg.exe"),
            Path(r"C:\Users\Admin\ComfyUI-Shared\ffmpeg\bin\ffmpeg.exe"),
        ]
    )
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return ""


def _run_ffmpeg(args: list[str], error_message: str) -> None:
    command = [_ffmpeg_binary(), *args]
    result = subprocess.run(command, text=True, capture_output=True, timeout=settings.timeout_seconds)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or error_message).strip()[-1200:]
        raise HTTPException(status_code=503, detail=detail)


def _seedvc_ready() -> bool:
    if not settings.seedvc_root.exists() or not settings.seedvc_python.exists():
        return False
    try:
        result = subprocess.run(
            [str(settings.seedvc_python), "-c", "import torch, torchaudio; print(torch.cuda.is_available())"],
            text=True,
            capture_output=True,
            timeout=20,
        )
    except Exception:
        return False
    return result.returncode == 0


def _remix_ready() -> bool:
    if not settings.use_vocal_separation_remix:
        return True
    try:
        demucs = subprocess.run(
            [str(settings.seedvc_python), "-c", "import demucs, torch; print(torch.cuda.is_available())"],
            text=True,
            capture_output=True,
            timeout=20,
        )
        ffmpeg = subprocess.run([_ffmpeg_binary(), "-version"], text=True, capture_output=True, timeout=10)
    except Exception:
        return False
    return demucs.returncode == 0 and ffmpeg.returncode == 0


def _comfy_prompt(caption: str, lyrics: str, max_duration: float, seed: int, output_prefix: str) -> dict[str, Any]:
    return {
        "6": {"class_type": "UNETLoader", "inputs": {"unet_name": "minimax_music3_dit_fp16.safetensors", "weight_dtype": "default"}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": "minimax_music3_text_encoder_pruned_int8_convrot.safetensors", "type": "minimax", "device": "default"}},
        "7": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_music3_dav.safetensors"}},
        "13": {
            "class_type": "MiniMaxMusic3TextEncode",
            "inputs": {
                "clip": ["3", 0],
                "caption": caption,
                "lyrics": lyrics,
                "seed": seed,
                "max_duration": max(30.0, min(float(max_duration), 300.0)),
                "cfg_scale": 1.7,
                "top_k": 50,
            },
        },
        "10": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["13", 0]}},
        "15": {"class_type": "EmptyMiniMaxMusic3LatentAudio", "inputs": {"seconds": ["13", 1], "batch_size": 1}},
        "9": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["6", 0],
                "positive": ["13", 0],
                "negative": ["10", 0],
                "latent_image": ["15", 0],
                "seed": seed,
                "steps": 30,
                "cfg": 1.7,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        "42": {"class_type": "VAEDecodeAudioTiled", "inputs": {"samples": ["9", 0], "vae": ["7", 0], "tile_size": 1536, "overlap": 64}},
        "35": {"class_type": "SaveAudioAdvanced", "inputs": {"audio": ["42", 0], "filename_prefix": output_prefix, "format": "mp3", "format.quality": "V0"}},
    }


_OPTION_KEYWORDS = {
    "声音",
    "男声",
    "女声",
    "系统推荐",
    "电音",
    "电音多一点",
    "电音少一点",
    "情绪",
    "节奏",
    "时长",
    "慢速",
    "中速",
    "快速",
    "标准",
    "短一点",
    "长一点",
    "温暖",
    "伤感",
    "有力量",
}


def _clean_creative_theme(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    labels = ("声音", "电音", "情绪", "节奏", "时长")
    parts = [part.strip() for part in re.split(r"[;；|]+", text) if part.strip()]
    if parts and all(any(part.startswith(label + ":") or part.startswith(label + "：") for label in labels) for part in parts):
        return ""
    cleaned_parts: list[str] = []
    for part in parts or [text]:
        if any(part.startswith(label + ":") or part.startswith(label + "：") for label in labels):
            continue
        cleaned_parts.append(part)
    cleaned = " ".join(cleaned_parts).strip()
    return "" if cleaned in _OPTION_KEYWORDS else cleaned[:160]


def _clean_reference_title(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"^(AI\s*原创|AI\s*创作|AI原创|AI创作)\s*[-－:：]\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^(原创|创作)\s*[-－:：]\s*", "", text).strip()
    return text[:120]


def _meaningful_keywords(value: str) -> list[str]:
    text = _clean_creative_theme(value)
    if not text:
        return []
    words = _keywords(text)
    if words:
        return words
    chinese = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
    return [word for word in chinese if word not in _OPTION_KEYWORDS][:4]


def _make_hook_phrase(keywords: list[str], source_title: str) -> str:
    text = " ".join(keywords) or source_title
    if "年少" in text or "少年" in text:
        return "把少年留给晚风"
    if "雪" in text:
        return "等雪停以后"
    if "年华" in text or "岁月" in text:
        return "把年华唱成河"
    if "月" in text:
        return "借一束月光"
    if "风" in text:
        return "随风慢慢回响"
    if "爱" in text:
        return "把爱留在心上"
    if "家" in text:
        return "灯火为我等候"
    if keywords:
        keyword = re.sub(r"\s+", "", keywords[0])[:6]
        if keyword and keyword not in _OPTION_KEYWORDS:
            return f"把{keyword}唱到天亮"
    return "把心唱到天亮"


def _make_title_phrase(keywords: list[str], source_title: str) -> str:
    text = " ".join(keywords) or source_title
    if "年少" in text or "少年" in text:
        return "少年晚风"
    if "雪" in text:
        return "雪停以后"
    if "年华" in text or "岁月" in text:
        return "年华成河"
    if "月" in text:
        return "借月光"
    if "风" in text:
        return "风里回响"
    if "爱" in text:
        return "爱在心上"
    if "家" in text:
        return "灯火等候"
    if keywords:
        keyword = re.sub(r"\s+", "", keywords[0])[:6]
        if keyword and keyword not in _OPTION_KEYWORDS:
            return keyword
    return "新的回声"


def _image_from_keywords(keywords: list[str], seed_text: str) -> str:
    joined = " ".join(keywords)
    if "雪" in joined:
        return "雪光"
    if "月" in joined:
        return "月光"
    if "风" in joined:
        return "晚风"
    if "家" in joined:
        return "灯火"
    if "年华" in joined or "岁月" in joined:
        return "旧时光"
    return _pick_by_seed(seed_text, ["晚风", "灯光", "月色", "长街", "旧梦"])


def _place_from_keywords(keywords: list[str], seed_text: str) -> str:
    joined = " ".join(keywords)
    if "家" in joined:
        return "回家的路"
    if "年华" in joined or "岁月" in joined:
        return "岁月岸边"
    if "雪" in joined:
        return "安静街口"
    if "月" in joined:
        return "窗前"
    if "风" in joined:
        return "城市尽头"
    return _pick_by_seed(seed_text + "place", ["人海中央", "回家的路", "安静窗前", "城市尽头", "岁月岸边"])


def _seed_index(value: str, size: int) -> int:
    return sum(ord(ch) for ch in str(value or "")) % max(1, size)


def _pick_by_seed(value: str, options: list[str]) -> str:
    return options[_seed_index(value, len(options))]


def _keywords(value: str) -> list[str]:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", " ", _clean_creative_theme(value)).strip()
    words = [word for word in cleaned.split() if len(word) >= 2 and word not in _OPTION_KEYWORDS]
    if not words:
        return []
    return words[:4]


def _generated_title(source_title: str, theme: str) -> str:
    creative_theme = _clean_creative_theme(theme)
    reference_title = _clean_reference_title(source_title)
    keywords = _meaningful_keywords(creative_theme or reference_title)
    candidates = [
        _make_title_phrase(keywords, reference_title),
        _make_hook_phrase(keywords, reference_title),
        "新的回声",
    ]
    title = next((item for item in candidates if item and item not in _OPTION_KEYWORDS), "新的回声")
    return f"AI原创 - {title}"[:80]


def _build_template_synced_lrc(title: str, lyrics: str, template: dict[str, Any]) -> str:
    lyric_lines = _plain_lyric_lines(lyrics)
    template_lines = list(template.get("lines") or [])
    if not lyric_lines or not template_lines:
        return ""
    output = [f"[ti:{title.strip()}]"] if title.strip() else []
    output.append("[offset:0]")
    output.append("")
    count = min(len(lyric_lines), len(template_lines))
    for index in range(count):
        start_ms = int(template_lines[index].get("startMs") or 0)
        output.append(f"[{_format_lrc_time(start_ms)}]{lyric_lines[index]}")
    if len(lyric_lines) > count:
        last_ms = int(template_lines[-1].get("endMs") or template_lines[-1].get("startMs") or 0)
        for extra_index, text in enumerate(lyric_lines[count:], 1):
            output.append(f"[{_format_lrc_time(last_ms + extra_index * 2500)}]{text}")
    return "\n".join(output).strip() + "\n"


def _build_hybrid_synced_lrc(audio_path: Path, title: str, lyrics: str, template: dict[str, Any]) -> str:
    lyric_lines = _plain_lyric_lines(lyrics)
    if not lyric_lines:
        return ""
    template_times = _template_times_for_lyric_lines(lyric_lines, template)
    try:
        timed_items = _transcribe_audio_for_lrc(audio_path, "")
        asr_times = [ms for ms, text in timed_items if _is_usable_asr_time_text(text)]
        if template_times:
            times = _structure_locked_hybrid_times(template_times, asr_times)
            return _build_lrc_from_times(title, lyric_lines, times)
        if len(asr_times) >= max(6, int(len(lyric_lines) * 0.85)):
            return _build_lrc_from_times(title, lyric_lines, asr_times)
    except Exception as exc:
        _log_exception("hybrid melody lrc timing skipped", exc)
    return _build_template_synced_lrc(title, lyrics, template)


def _template_times_for_lyric_lines(lyric_lines: list[str], template: dict[str, Any]) -> list[int]:
    template_lines = list(template.get("lines") or [])
    if not lyric_lines or not template_lines:
        return []
    starts = [int(line.get("startMs") or 0) for line in template_lines]
    starts = sorted(ms for ms in starts if ms >= 0)
    if not starts:
        return []
    target_count = len(lyric_lines)
    source_count = len(starts)
    if target_count == source_count:
        return starts[:]
    if target_count == 1:
        return [starts[0]]
    if source_count == 1:
        return [starts[0] + index * 3000 for index in range(target_count)]
    result: list[int] = []
    for index in range(target_count):
        position = index * (source_count - 1) / max(1, target_count - 1)
        left = int(position)
        right = min(source_count - 1, left + 1)
        ratio = position - left
        result.append(int(starts[left] + (starts[right] - starts[left]) * ratio))
    return _enforce_monotonic_times(result)


def _structure_locked_hybrid_times(template_times: list[int], asr_times: list[int]) -> list[int]:
    template_times = _enforce_monotonic_times(template_times)
    if len(template_times) < 2:
        return template_times
    usable_asr = _enforce_monotonic_times(sorted(max(0, int(ms)) for ms in asr_times))
    if len(usable_asr) < 3:
        return template_times
    template_start = template_times[0]
    template_end = template_times[-1]
    asr_start = usable_asr[0]
    asr_end = usable_asr[-1]
    if asr_end <= asr_start or template_end <= template_start:
        return template_times

    span_ratio = (asr_end - asr_start) / max(1, template_end - template_start)
    if span_ratio < 0.55 or span_ratio > 1.45:
        shift = asr_start - template_start
        shifted = [max(0, ms + shift) for ms in template_times]
        return _enforce_monotonic_times(shifted)

    scaled = [
        int(asr_start + (ms - template_start) * span_ratio)
        for ms in template_times
    ]
    if len(usable_asr) >= max(6, int(len(template_times) * 0.55)):
        blended: list[int] = []
        for index, ms in enumerate(scaled):
            asr_index = round(index * (len(usable_asr) - 1) / max(1, len(template_times) - 1))
            # The melody structure remains the source of truth; ASR only nudges
            # global drift so Whisper segment merges do not shift every later line.
            blended.append(int(ms * 0.78 + usable_asr[asr_index] * 0.22))
        return _enforce_monotonic_times(blended)
    return _enforce_monotonic_times(scaled)


def _enforce_monotonic_times(times: list[int], min_gap_ms: int = 900) -> list[int]:
    result: list[int] = []
    previous = -min_gap_ms
    for raw in times:
        value = max(0, int(raw))
        if value < previous + min_gap_ms:
            value = previous + min_gap_ms
        result.append(value)
        previous = value
    return result


def _build_lrc_from_times(title: str, lyric_lines: list[str], times: list[int]) -> str:
    output = [f"[ti:{title.strip()}]"] if title.strip() else []
    output.append("[offset:0]")
    output.append("")
    usable_times = sorted(max(0, int(ms)) for ms in times)
    for index, text in enumerate(lyric_lines):
        if index < len(usable_times):
            start_ms = usable_times[index]
        elif usable_times:
            start_ms = usable_times[-1] + (index - len(usable_times) + 1) * 2500
        else:
            start_ms = index * 3000
        output.append(f"[{_format_lrc_time(start_ms)}]{text}")
    return "\n".join(output).strip() + "\n"


def _build_lrc_from_timed_items(title: str, timed_items: list[tuple[int, str]]) -> str:
    cleaned: list[tuple[int, str]] = []
    for ms, text in timed_items:
        clean = _clean_lyric_text(text)
        if clean and _is_usable_asr_time_text(clean):
            cleaned.append((int(ms), clean))
    cleaned = _dedupe_timed_lyrics(cleaned)
    if not cleaned:
        return ""
    output = [f"[ti:{title.strip()}]"] if title.strip() else []
    output.append("[offset:0]")
    output.append("")
    output.extend(f"[{_format_lrc_time(ms)}]{text}" for ms, text in cleaned[:160])
    return "\n".join(output).strip() + "\n"


def _build_forced_aligned_lrc(
    audio_path: Path,
    title: str,
    lyrics: str,
    template: dict[str, Any] | None = None,
    timed_spans: list[dict[str, Any]] | None = None,
) -> str:
    lyric_lines = _plain_lyric_lines(lyrics)
    if not settings.asr_enabled or not audio_path.exists() or not lyric_lines:
        return ""

    try:
        spans = timed_spans or _transcribe_audio_spans_for_lrc(audio_path, lyrics)
        spans = _dedupe_timed_spans(spans)
        usable_spans = [span for span in spans if _is_usable_asr_time_text(str(span.get("text") or ""))]
        if len(usable_spans) < 2:
            return ""

        expected_text = _normalize_lyric_for_match("".join(lyric_lines))
        actual_text = _normalize_lyric_for_match("".join(str(span.get("text") or "") for span in usable_spans))
        if len(expected_text) < 8 or len(actual_text) < 8:
            return ""
        if _sequence_similarity(expected_text, actual_text) < 0.55:
            return ""

        forced_times, matched_count = _forced_line_times_from_asr_spans(lyric_lines, usable_spans)
        if matched_count < max(2, int(len(lyric_lines) * 0.45)):
            return ""

        template_times = _template_times_for_lyric_lines(lyric_lines, template or {}) if template else []
        if template_times:
            forced_times = _blend_forced_and_template_times(forced_times, template_times)
        forced_times = _enforce_monotonic_times(forced_times, min_gap_ms=550)
        return _build_lrc_from_times(title, lyric_lines, forced_times)
    except Exception as exc:
        _log_exception("forced lyrics alignment failed", exc)
        return ""


def _transcribe_audio_spans_for_lrc(audio_path: Path, expected_lyrics: str = "") -> list[dict[str, Any]]:
    transcribers = (_transcribe_spans_with_faster_whisper, _transcribe_spans_with_openai_whisper)
    for transcriber in transcribers:
        try:
            spans = transcriber(audio_path, expected_lyrics)
            if len(spans) >= 3:
                return spans
        except Exception as exc:
            _log_exception(f"{transcriber.__name__} skipped", exc)
    return []


def _transcribe_spans_with_faster_whisper(audio_path: Path, expected_lyrics: str = "") -> list[dict[str, Any]]:
    model_ref = _resolve_faster_whisper_model()
    if not model_ref:
        return []
    model = _faster_whisper_model(model_ref)
    segments, _info = model.transcribe(
        str(audio_path),
        language="zh",
        beam_size=5,
        vad_filter=True,
        word_timestamps=False,
        condition_on_previous_text=False,
        initial_prompt=_asr_initial_prompt(expected_lyrics),
    )
    return _segments_to_lrc_spans(segments)


def _transcribe_spans_with_openai_whisper(audio_path: Path, expected_lyrics: str = "") -> list[dict[str, Any]]:
    model_name = _resolve_openai_whisper_model()
    if not model_name:
        return []
    _ensure_ffmpeg_on_path()
    model = _openai_whisper_model(model_name)
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = model.transcribe(
            str(audio_path),
            language="zh",
            fp16=False,
            verbose=False,
            initial_prompt=_asr_initial_prompt(expected_lyrics),
        )
    return _segments_to_lrc_spans(result.get("segments") or [])


def _segments_to_lrc_spans(segments: Any) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for segment in segments:
        if isinstance(segment, dict):
            raw_text = str(segment.get("text") or "")
            start = float(segment.get("start") or 0.0)
            end = float(segment.get("end") or (start + 1.0))
        else:
            raw_text = str(getattr(segment, "text", "") or "")
            start = float(getattr(segment, "start", 0.0) or 0.0)
            end = float(getattr(segment, "end", 0.0) or (start + 1.0))
        text = _normalize_asr_text(raw_text)
        if not text:
            continue
        start_ms = int(max(0.0, start) * 1000)
        end_ms = int(max(end, start + 1.0) * 1000)
        if start_ms < 3000 and len(text) <= 2:
            continue
        parts = _split_asr_lyric_text(text)
        if not parts:
            continue
        if len(parts) == 1:
            spans.append({"startMs": start_ms, "endMs": end_ms, "text": parts[0]})
            continue

        total_units = sum(max(1, len(_alignment_units(part))) for part in parts)
        cursor = start_ms
        elapsed_units = 0
        span_ms = max(1200, end_ms - start_ms)
        for index, part in enumerate(parts):
            units = max(1, len(_alignment_units(part)))
            elapsed_units += units
            part_end = start_ms + int(span_ms * elapsed_units / max(1, total_units))
            if index == len(parts) - 1:
                part_end = end_ms
            spans.append({"startMs": cursor, "endMs": max(cursor + 500, part_end), "text": part})
            cursor = max(cursor + 500, part_end)
    return spans


def _dedupe_timed_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    previous_text = ""
    previous_ms = -1000
    for span in sorted(spans, key=lambda item: int(item.get("startMs") or 0)):
        text = _normalize_asr_text(str(span.get("text") or ""))
        if not text:
            continue
        raw_start = max(0, int(span.get("startMs") or 0))
        raw_end = max(raw_start + 600, int(span.get("endMs") or (raw_start + 1200)))
        if text == previous_text and raw_start - previous_ms < 2000:
            continue
        start_ms = raw_start
        if result and start_ms <= int(result[-1]["startMs"]):
            start_ms = int(result[-1]["startMs"]) + 500
        end_ms = max(start_ms + 600, raw_end)
        result.append({"startMs": start_ms, "endMs": end_ms, "text": text})
        previous_text = text
        previous_ms = start_ms
    return result


def _forced_line_times_from_asr_spans(lyric_lines: list[str], spans: list[dict[str, Any]]) -> tuple[list[int], int]:
    expected_units: list[str] = []
    expected_line_indexes: list[int] = []
    for line_index, line in enumerate(lyric_lines):
        for unit in _alignment_units(line):
            expected_units.append(unit)
            expected_line_indexes.append(line_index)

    actual_units: list[str] = []
    actual_times: list[int] = []
    for span in spans:
        units = _alignment_units(str(span.get("text") or ""))
        if not units:
            continue
        start_ms = int(span.get("startMs") or 0)
        end_ms = max(start_ms + 600, int(span.get("endMs") or (start_ms + 1200)))
        duration = max(600, end_ms - start_ms)
        for index, unit in enumerate(units):
            actual_units.append(unit)
            actual_times.append(start_ms + int(duration * index / max(1, len(units))))

    if not expected_units or not actual_units:
        return [], 0

    limit = 3600
    expected_text = "".join(expected_units[:limit])
    actual_text = "".join(actual_units[:limit])
    line_times: list[list[int]] = [[] for _ in lyric_lines]
    matcher = SequenceMatcher(None, expected_text, actual_text, autojunk=False)
    for block in matcher.get_matching_blocks():
        if block.size <= 0:
            continue
        for offset in range(block.size):
            expected_index = block.a + offset
            actual_index = block.b + offset
            if expected_index >= len(expected_line_indexes) or actual_index >= len(actual_times):
                continue
            line_index = expected_line_indexes[expected_index]
            if 0 <= line_index < len(line_times):
                line_times[line_index].append(actual_times[actual_index])

    known_times: list[int | None] = []
    matched_count = 0
    for values in line_times:
        if not values:
            known_times.append(None)
            continue
        matched_count += 1
        values = sorted(values)
        anchor_index = max(0, min(len(values) - 1, int(len(values) * 0.18)))
        known_times.append(max(0, values[anchor_index] - 120))

    filled = _fill_missing_line_times(known_times, len(lyric_lines))
    return _enforce_monotonic_times(filled, min_gap_ms=550), matched_count


def _fill_missing_line_times(known_times: list[int | None], line_count: int) -> list[int]:
    if line_count <= 0:
        return []
    if not any(value is not None for value in known_times):
        return [index * 3000 for index in range(line_count)]

    result: list[int] = [0] * line_count
    known_indexes = [index for index, value in enumerate(known_times) if value is not None]
    first_index = known_indexes[0]
    first_time = int(known_times[first_index] or 0)
    for index in range(first_index + 1):
        result[index] = max(0, first_time - (first_index - index) * 2500)

    for left, right in zip(known_indexes, known_indexes[1:]):
        left_time = int(known_times[left] or 0)
        right_time = int(known_times[right] or left_time)
        gap = max(1, right - left)
        for index in range(left, right + 1):
            ratio = (index - left) / gap
            result[index] = int(left_time + (right_time - left_time) * ratio)

    last_index = known_indexes[-1]
    last_time = int(known_times[last_index] or 0)
    for index in range(last_index, line_count):
        result[index] = last_time + (index - last_index) * 2500
    return result


def _alignment_units(value: str) -> list[str]:
    normalized = _normalize_lyric_for_match(value)
    return [char for char in normalized if char.strip()]


def _blend_forced_and_template_times(forced_times: list[int], template_times: list[int]) -> list[int]:
    if len(forced_times) != len(template_times) or len(forced_times) < 2:
        return forced_times
    forced = _enforce_monotonic_times([int(value) for value in forced_times], min_gap_ms=550)
    template = _enforce_monotonic_times([int(value) for value in template_times], min_gap_ms=550)
    forced_start, forced_end = forced[0], forced[-1]
    template_start, template_end = template[0], template[-1]
    template_span = max(1, template_end - template_start)
    forced_span = max(1, forced_end - forced_start)
    ratio = forced_span / template_span
    if ratio < 0.55 or ratio > 1.65:
        shifted_template = [max(0, value + forced_start - template_start) for value in template]
    else:
        shifted_template = [int(forced_start + (value - template_start) * ratio) for value in template]

    blended: list[int] = []
    for forced_ms, template_ms in zip(forced, shifted_template):
        if abs(forced_ms - template_ms) > 4500:
            blended.append(forced_ms)
        else:
            blended.append(int(forced_ms * 0.82 + template_ms * 0.18))
    return _enforce_monotonic_times(blended, min_gap_ms=550)


def _validate_sung_lyrics(audio_path: Path, expected_lyrics: str) -> dict[str, Any]:
    if not settings.asr_enabled or not audio_path.exists():
        return {"usable": False, "score": 0.0, "timedItems": [], "lineCount": 0}
    expected_text = _normalize_lyric_for_match("".join(_plain_lyric_lines(expected_lyrics)))
    if len(expected_text) < 12:
        return {"usable": False, "score": 0.0, "timedItems": [], "lineCount": 0}
    try:
        timed_spans = _transcribe_audio_spans_for_lrc(audio_path, "")
        timed_items = [(int(item.get("startMs") or 0), str(item.get("text") or "")) for item in timed_spans]
    except Exception as exc:
        _log_exception("sung lyrics validation skipped", exc)
        return {"usable": False, "score": 0.0, "timedItems": [], "lineCount": 0}
    credit_line_count = sum(1 for _, text in timed_items if _is_non_sung_lrc_text(text))
    actual_lines = [text for _, text in timed_items if _is_usable_asr_time_text(text)]
    actual_text = _normalize_lyric_for_match("".join(actual_lines))
    if len(actual_text) < 8 or len(actual_lines) < 3:
        return {"usable": False, "score": 0.0, "timedItems": timed_items, "lineCount": len(actual_lines)}
    score = _sequence_similarity(expected_text, actual_text)
    return {
        "usable": True,
        "score": score,
        "timedItems": timed_items,
        "timedSpans": timed_spans,
        "lineCount": len(actual_lines),
        "actualCreditLineCount": credit_line_count,
    }


def _should_use_actual_sung_lrc(validation: dict[str, Any]) -> bool:
    score = float(validation.get("score") or 0.0)
    line_count = int(validation.get("lineCount") or 0)
    credit_count = int(validation.get("actualCreditLineCount") or 0)
    if line_count < 3:
        return False
    if credit_count >= 2:
        return True
    # ACE-Step cover can preserve source pronunciation while still sharing many
    # characters with the target lyrics. A stricter threshold prevents a
    # plausible-looking but wrong structure-locked LRC from reaching Android.
    return score < 0.78


def _write_generation_quality_report(job_dir: Path, report: dict[str, Any]) -> None:
    try:
        (job_dir / "quality_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        _log_exception("quality report write failed", exc)


def _validate_melody_generation_quality(
    audio_path: Path,
    expected_lyrics: str,
    template: dict[str, Any],
    asr_validation: dict[str, Any] | None = None,
    job_dir: Path | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "passed": True,
        "message": "quality gate passed",
        "checks": {},
    }
    if not settings.melody_quality_gate_enabled:
        report["checks"]["enabled"] = False
        return report
    checks = report["checks"]
    validation = asr_validation or _validate_sung_lyrics(audio_path, expected_lyrics)
    lyric_lines = _plain_lyric_lines(expected_lyrics)
    expected_line_count = max(1, len(lyric_lines))
    asr_score = float(validation.get("score") or 0.0)
    matched_lines = int(validation.get("lineCount") or 0)
    line_ratio = matched_lines / expected_line_count
    asr_usable = bool(validation.get("usable"))
    checks["asr"] = {
        "usable": asr_usable,
        "score": round(asr_score, 4),
        "matchedLines": matched_lines,
        "expectedLines": expected_line_count,
        "lineRatio": round(line_ratio, 4),
        "minScore": settings.melody_asr_min_score,
        "minLineRatio": settings.melody_asr_min_line_ratio,
    }
    failures: list[str] = []
    if not asr_usable:
        failures.append("ASR 未能可靠识别生成后的人声歌词")
    elif asr_score < float(settings.melody_asr_min_score):
        failures.append(f"ASR 歌词一致性不足: {asr_score:.2f}")
    if line_ratio < float(settings.melody_asr_min_line_ratio):
        failures.append(f"ASR 匹配行数不足: {matched_lines}/{expected_line_count}")

    pitch_report = _validate_generated_pitch_similarity(audio_path, template, job_dir)
    checks["pitch"] = pitch_report
    if not pitch_report.get("usable"):
        failures.append(str(pitch_report.get("message") or "成品音高无法可靠分析"))
    else:
        if float(pitch_report.get("similarity") or 0.0) < float(settings.melody_pitch_min_similarity):
            failures.append(f"成品旋律与原曲相似度不足: {float(pitch_report.get('similarity') or 0.0):.2f}")
        if float(pitch_report.get("voicedRatio") or 0.0) < float(settings.melody_pitch_min_voiced_ratio):
            failures.append(f"成品人声有效音高比例过低: {float(pitch_report.get('voicedRatio') or 0.0):.2f}")

    if failures:
        report["passed"] = False
        report["message"] = "；".join(failures[:3])
    return report


def _validate_generated_pitch_similarity(audio_path: Path, template: dict[str, Any], job_dir: Path | None = None) -> dict[str, Any]:
    lines = list(template.get("lines") or [])
    if not audio_path.exists() or len(lines) < 3:
        return {"usable": False, "message": "missing audio or melody template"}
    try:
        analysis_audio = audio_path
        if job_dir is not None and settings.melody_use_vocal_separation:
            analysis_audio = _prepare_vocal_reference_for_analysis(audio_path, job_dir / "generated_quality_stems")
        features = _extract_line_pitch_features(analysis_audio, lines)
    except Exception as exc:
        _log_exception("generated pitch validation failed", exc)
        return {"usable": False, "message": str(exc)[:300]}

    scores: list[float] = []
    voiced = 0
    total = 0
    for source_line, generated in zip(lines, features):
        total += 1
        if int(generated.get("avgF0") or 0) <= 0 or float(generated.get("voicedRatio") or 0.0) < 0.08:
            continue
        voiced += 1
        score = _line_pitch_similarity(source_line, generated)
        if score is not None:
            scores.append(score)
    if not scores:
        return {"usable": False, "message": "no usable generated pitch segments", "voicedLines": voiced, "totalLines": total}
    similarity = sum(scores) / len(scores)
    voiced_ratio = voiced / max(1, total)
    return {
        "usable": True,
        "similarity": round(float(similarity), 4),
        "voicedRatio": round(float(voiced_ratio), 4),
        "lineScores": [round(float(score), 4) for score in scores[:24]],
        "voicedLines": voiced,
        "totalLines": total,
        "minSimilarity": settings.melody_pitch_min_similarity,
        "minVoicedRatio": settings.melody_pitch_min_voiced_ratio,
    }


def _line_pitch_similarity(source_line: dict[str, Any], generated_line: dict[str, Any]) -> float | None:
    source_curve = _normalized_pitch_contour(source_line.get("f0Curve"), source_line.get("avgF0"))
    generated_curve = _normalized_pitch_contour(generated_line.get("f0Curve"), generated_line.get("avgF0"))
    if len(source_curve) < 3 or len(generated_curve) < 3:
        return None
    count = min(48, max(8, min(len(source_curve), len(generated_curve))))
    source = _resample_numeric_curve(source_curve, count)
    generated = _resample_numeric_curve(generated_curve, count)
    correlation = _safe_correlation(source, generated)
    interval_score = _interval_direction_similarity(source, generated)
    contour_score = max(0.0, (correlation + 1.0) / 2.0)
    return max(0.0, min(1.0, contour_score * 0.65 + interval_score * 0.35))


def _normalized_pitch_contour(values: Any, avg_f0: Any = None) -> list[float]:
    try:
        import numpy as np

        curve = np.asarray(values or [], dtype=float)
        curve = curve[(curve >= 60) & (curve <= 900)]
        curve = curve[curve > 0]
        if curve.size < 3:
            base = float(avg_f0 or 0.0)
            if base <= 0:
                return []
            curve = np.asarray([base, base, base], dtype=float)
        midi = np.asarray([_hz_to_midi(float(value)) for value in curve], dtype=float)
        median = float(np.median(midi))
        normalized = midi - median
        return [float(value) for value in normalized.tolist()]
    except Exception:
        return []


def _resample_numeric_curve(values: list[float], count: int) -> list[float]:
    count = max(1, int(count))
    if not values:
        return [0.0 for _ in range(count)]
    if len(values) == 1:
        return [float(values[0]) for _ in range(count)]
    try:
        import numpy as np

        src = np.asarray(values, dtype=float)
        positions = np.linspace(0, len(src) - 1, count)
        return [float(value) for value in np.interp(positions, np.arange(len(src)), src).tolist()]
    except Exception:
        if count == len(values):
            return [float(value) for value in values]
        result: list[float] = []
        for index in range(count):
            source_index = round(index * (len(values) - 1) / max(1, count - 1))
            result.append(float(values[source_index]))
        return result


def _safe_correlation(left: list[float], right: list[float]) -> float:
    try:
        import numpy as np

        a = np.asarray(left, dtype=float)
        b = np.asarray(right, dtype=float)
        if a.size != b.size or a.size < 2:
            return 0.0
        std_a = float(np.std(a))
        std_b = float(np.std(b))
        if std_a < 1e-4 and std_b < 1e-4:
            return 1.0
        if std_a < 1e-4 or std_b < 1e-4:
            return 0.0
        return float(np.corrcoef(a, b)[0, 1])
    except Exception:
        return 0.0


def _interval_direction_similarity(left: list[float], right: list[float]) -> float:
    if len(left) < 2 or len(right) < 2:
        return 0.0
    count = min(len(left), len(right))
    matches = 0
    total = 0
    for index in range(1, count):
        ldiff = left[index] - left[index - 1]
        rdiff = right[index] - right[index - 1]
        if abs(ldiff) < 0.25 and abs(rdiff) < 0.25:
            matches += 1
        elif ldiff * rdiff > 0:
            matches += 1
        total += 1
    return matches / max(1, total)


def _normalize_lyric_for_match(value: str) -> str:
    text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", value or "")
    return text.lower()


def _sequence_similarity(expected: str, actual: str) -> float:
    if not expected or not actual:
        return 0.0
    # Bounded LCS keeps validation cheap while still detecting severe mismatch.
    expected = expected[:1200]
    actual = actual[:1200]
    previous = [0] * (len(actual) + 1)
    for left in expected:
        current = [0]
        diagonal = 0
        for index, right in enumerate(actual, 1):
            top = previous[index]
            if left == right:
                value = diagonal + 1
            else:
                value = max(top, current[-1])
            current.append(value)
            diagonal = top
        previous = current
    lcs = previous[-1]
    return lcs / max(1, min(len(expected), len(actual)))


def _is_usable_asr_time_text(text: str) -> bool:
    clean = re.sub(r"[\s，。,.!?！？；;：:、]+", "", str(text or ""))
    if len(clean) < 2:
        return False
    if not re.search(r"[\u4e00-\u9fff]", clean):
        return False
    if _is_non_sung_lrc_text(clean):
        return False
    return True


def _plain_lyric_lines(lyrics: str) -> list[str]:
    result: list[str] = []
    for raw in (lyrics or "").splitlines():
        line = raw.strip()
        if not line or re.match(r"^\[[^\]]+\]$", line):
            continue
        text = _clean_lyric_text(line)
        if text:
            result.append(text)
    return result


def _build_asr_synced_lrc(audio_path: Path, title: str, expected_lyrics: str = "") -> str:
    if not settings.asr_enabled:
        return ""
    if not audio_path.exists():
        return ""
    try:
        timed_lines = _transcribe_audio_for_lrc(audio_path, expected_lyrics)
        timed_lines = _dedupe_timed_lyrics(timed_lines)
        if len(timed_lines) < 3:
            return ""
        output = [f"[ti:{title.strip()}]"] if title.strip() else []
        output.append("[offset:0]")
        output.append("")
        output.extend(f"[{_format_lrc_time(ms)}]{text}" for ms, text in timed_lines[:160])
        return "\n".join(output).strip() + "\n"
    except Exception as exc:
        _log_exception("ASR lyrics alignment failed", exc)
        return ""


def _transcribe_audio_for_lrc(audio_path: Path, expected_lyrics: str = "") -> list[tuple[int, str]]:
    transcribers = (_transcribe_with_faster_whisper, _transcribe_with_openai_whisper)
    for transcriber in transcribers:
        try:
            timed_lines = transcriber(audio_path, expected_lyrics)
            if len(timed_lines) >= 3:
                return timed_lines
        except Exception as exc:
            _log_exception(f"{transcriber.__name__} skipped", exc)
    return []


def _transcribe_with_faster_whisper(audio_path: Path, expected_lyrics: str = "") -> list[tuple[int, str]]:
    model_ref = _resolve_faster_whisper_model()
    if not model_ref:
        return []
    model = _faster_whisper_model(model_ref)
    segments, _info = model.transcribe(
        str(audio_path),
        language="zh",
        beam_size=5,
        vad_filter=True,
        word_timestamps=False,
        condition_on_previous_text=False,
        initial_prompt=_asr_initial_prompt(expected_lyrics),
    )
    return _segments_to_lrc_items(segments)


def _transcribe_with_openai_whisper(audio_path: Path, expected_lyrics: str = "") -> list[tuple[int, str]]:
    model_name = _resolve_openai_whisper_model()
    if not model_name:
        return []
    _ensure_ffmpeg_on_path()
    model = _openai_whisper_model(model_name)
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = model.transcribe(
            str(audio_path),
            language="zh",
            fp16=False,
            verbose=False,
            initial_prompt=_asr_initial_prompt(expected_lyrics),
        )
    return _segments_to_lrc_items(result.get("segments") or [])


def _faster_whisper_model(model_ref: str) -> Any:
    global _ASR_MODEL, _ASR_MODEL_KEY
    key = ("faster", model_ref, settings.asr_device, settings.asr_compute_type)
    if _ASR_MODEL is not None and _ASR_MODEL_KEY == key:
        return _ASR_MODEL
    from faster_whisper import WhisperModel

    _ASR_MODEL = WhisperModel(
        model_ref,
        device=settings.asr_device,
        compute_type=settings.asr_compute_type,
    )
    _ASR_MODEL_KEY = key
    return _ASR_MODEL


def _openai_whisper_model(model_name: str) -> Any:
    global _ASR_MODEL, _ASR_MODEL_KEY
    cache_dir = str(settings.asr_openai_cache_dir)
    key = ("openai", model_name, settings.asr_device, cache_dir)
    if _ASR_MODEL is not None and _ASR_MODEL_KEY == key:
        return _ASR_MODEL
    import whisper

    _ASR_MODEL = whisper.load_model(model_name, device=settings.asr_device, download_root=cache_dir)
    _ASR_MODEL_KEY = key
    return _ASR_MODEL


def _resolve_faster_whisper_model() -> str:
    configured = str(settings.asr_model_path or "").strip()
    if configured:
        configured_path = Path(configured)
        if configured_path.exists() and ((configured_path / "model.bin").exists() or configured_path.is_file()):
            return str(configured_path)

    candidates: list[Path] = [settings.work_dir / "models" / f"faster-whisper-{settings.asr_model_size}"]
    hf_root = Path.home() / ".cache" / "huggingface" / "hub"
    candidates.extend(hf_root.glob(f"models--*faster-whisper-{settings.asr_model_size}*/snapshots/*"))
    for candidate in candidates:
        if candidate.exists() and (candidate / "model.bin").exists():
            return str(candidate)
    if settings.asr_download_models:
        return str(settings.asr_model_size or "base")
    return ""


def _resolve_openai_whisper_model() -> str:
    cache_dir = Path(settings.asr_openai_cache_dir)
    preferred = [str(settings.asr_model_size or ""), "base", "tiny"]
    seen: set[str] = set()
    for name in preferred:
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if (cache_dir / f"{name}.pt").exists():
            return name
    if settings.asr_download_models:
        return str(settings.asr_model_size or "base")
    return ""


def _asr_status() -> dict[str, Any]:
    faster_model = _resolve_faster_whisper_model()
    openai_model = _resolve_openai_whisper_model()
    ffmpeg_available = _ffmpeg_available()
    backend = "faster-whisper" if faster_model else ("openai-whisper" if openai_model and ffmpeg_available else "fallback")
    model = Path(faster_model).name if faster_model and Path(faster_model).exists() else (faster_model or openai_model)
    return {
        "enabled": bool(settings.asr_enabled),
        "backend": backend if settings.asr_enabled else "disabled",
        "model": model,
        "ffmpegAvailable": ffmpeg_available,
        "ffmpeg": _ffmpeg_binary() if ffmpeg_available else "",
        "downloadModels": bool(settings.asr_download_models),
    }


def _asr_initial_prompt(expected_lyrics: str) -> str:
    # Prompting Whisper with the intended lyrics can make it hallucinate vocals during intros.
    # Keep alignment grounded in the generated audio itself.
    return ""


def _segments_to_lrc_items(segments: Any) -> list[tuple[int, str]]:
    return [(int(span.get("startMs") or 0), str(span.get("text") or "")) for span in _segments_to_lrc_spans(segments)]


def _normalize_asr_text(value: str) -> str:
    text = re.sub(r"\s+", "", value or "")
    text = re.sub(r"[♪♫]+", "", text)
    text = text.strip(" ，。,.!?！？；;：:、")
    return text


def _split_asr_lyric_text(value: str) -> list[str]:
    text = _normalize_asr_text(value)
    if not text:
        return []
    parts = [part.strip() for part in re.split(r"[，。,.!?！？；;：:、]+", text) if part.strip()]
    if len(parts) > 1:
        return [part for part in parts if len(part) >= 2]
    if len(text) <= 14:
        return [text]
    chunks: list[str] = []
    while text:
        chunks.append(text[:12])
        text = text[12:]
    return chunks

def _dedupe_timed_lyrics(items: list[tuple[int, str]]) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    previous_text = ""
    previous_ms = -1000
    for raw_ms, raw_text in sorted(items, key=lambda item: item[0]):
        text = _normalize_asr_text(raw_text)
        if not text:
            continue
        if text == previous_text and raw_ms - previous_ms < 2000:
            continue
        ms = max(0, int(raw_ms))
        if result and ms <= result[-1][0]:
            ms = result[-1][0] + 500
        result.append((ms, text))
        previous_text = text
        previous_ms = ms
    return result


def _format_lrc_time(ms: int) -> str:
    value = max(0, int(ms))
    minutes = value // 60000
    seconds = (value % 60000) // 1000
    centiseconds = (value % 1000) // 10
    return f"{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:80] or uuid.uuid4().hex[:12]


def _free_comfy_memory(base_url: str) -> None:
    try:
        with httpx.Client(timeout=20) as client:
            client.post(f"{base_url}/interrupt")
            client.post(f"{base_url}/free", json={"unload_models": True, "free_memory": True})
    except Exception as exc:
        _log_exception("comfy free memory skipped", exc)


def _is_retryable_comfy_error(exc: Exception) -> bool:
    text = str(exc).lower()
    retry_markers = (
        "hostbuffer.read_file_slice",
        "getoverlappedresult failed error=1450",
        "insufficient system resources",
        "out of memory",
        "cuda out of memory",
        "timed out",
    )
    return any(marker in text for marker in retry_markers)


def _short_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()[:1200]


def _response_text(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            return str(data.get("detail") or data)[:1200]
        return str(data)[:1200]
    except Exception:
        return (response.text or f"HTTP {response.status_code}")[:1200]


def _log_exception(prefix: str, exc: Exception) -> str:
    detail = str(exc) or exc.__class__.__name__
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        logs_dir = settings.work_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / "generator.log"
        with log_path.open("a", encoding="utf-8") as target:
            target.write(f"\n[{stamp}] {prefix}: {detail}\n{traceback.format_exc()}\n")
    except Exception:
        pass
    return detail[:600]


def _write_job_status(job_dir: Path, status: str, message: str, extra: dict[str, Any] | None = None) -> None:
    try:
        payload = {
            "status": status,
            "message": message,
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            **(extra or {}),
        }
        lines = [f"{key}: {value}" for key, value in payload.items()]
        (job_dir / "status.txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass


def _write_generation_debug(job_dir: Path, payload: dict[str, Any]) -> None:
    try:
        (job_dir / "generation_debug.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        _log_exception("generation debug write failed", exc)


def _debug_melody_template(template: dict[str, Any]) -> dict[str, Any]:
    lines = template.get("lines") or []
    return {
        "source": template.get("source"),
        "durationSeconds": template.get("durationSeconds"),
        "strictMelodyBackend": template.get("strictMelodyBackend"),
        "lineCount": len(lines),
        "sections": template.get("sections") or [],
        "hasAnalysisAudio": bool(template.get("analysisAudio")),
        "sampleLines": [
            {
                "index": line.get("index"),
                "section": line.get("section"),
                "startMs": line.get("startMs"),
                "endMs": line.get("endMs"),
                "durationMs": line.get("durationMs"),
                "charCount": line.get("charCount"),
                "lyricTargetChars": line.get("lyricTargetChars"),
                "lyricMaxChars": line.get("lyricMaxChars"),
                "melodyCleanupReason": line.get("melodyCleanupReason"),
                "pitchContour": line.get("pitchContour"),
                "avgF0": line.get("avgF0"),
                "voicedRatio": line.get("voicedRatio"),
                "rhythmDensity": line.get("rhythmDensity"),
            }
            for line in lines[:8]
        ],
    }


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


# Commercial melody-rewrite lyric planner v3.
# This late override keeps the public API and generation pipeline unchanged,
# while making lyric choices depend on the source rhythm and pitch template.
def _build_melody_rewrite_lyrics(source_title: str, theme: str, template: dict[str, Any]) -> str:
    lines = list(template.get("lines") or [])[:72]
    creative_theme = _clean_creative_theme(theme)
    story = _melody_story_plan(_clean_reference_title(source_title), creative_theme)
    story["musicProfile"] = _template_music_profile(template)
    output: list[str] = []
    current_section = ""
    section_counts: dict[str, int] = {}
    used_counts: dict[str, int] = {}
    for line in lines:
        section = str(line.get("section") or "Verse")
        if section != current_section:
            output.append("")
            output.append(f"[{section}]")
            current_section = section
        section_counts[section] = section_counts.get(section, 0) + 1
        section_index = section_counts[section] - 1
        bounds = _commercial_target_bounds(line, section)
        target = _commercial_target_count(line, section)
        rhyme = _select_rhyme(story, line, section, section_index)
        contour = str(line.get("pitchContour") or "stable")
        raw = _lyric_candidate(story, section, section_index, contour, target)
        fitted = _fit_lyric_line(raw, target, rhyme, story, section, section_index, used_counts, bounds=bounds)
        fitted = _polish_line_for_slot(fitted, story, section, section_index, bounds, target, rhyme, used_counts)
        used_counts[fitted] = used_counts.get(fitted, 0) + 1
        output.append(fitted)
    return "\n".join(output).strip() + "\n"


def _template_music_profile(template: dict[str, Any]) -> dict[str, Any]:
    lines = list(template.get("lines") or [])
    durations = [max(1, int(line.get("durationMs") or 0)) for line in lines]
    densities = [float(line.get("rhythmDensity") or 0.0) for line in lines]
    contours = [str(line.get("pitchContour") or "stable") for line in lines]
    avg_duration = sum(durations) / max(1, len(durations))
    avg_density = sum(densities) / max(1, len(densities))
    return {
        "avgDurationMs": int(avg_duration),
        "avgDensity": round(avg_density, 2),
        "slowSong": avg_duration >= 4200,
        "fastSong": avg_duration <= 2300 or avg_density >= 3.4,
        "risingRatio": contours.count("rising") / max(1, len(contours)),
        "fallingRatio": contours.count("falling") / max(1, len(contours)),
    }


def _melody_story_plan(source_title: str, theme: str) -> dict[str, Any]:
    seed = f"{source_title} {theme}".strip()
    text = seed or "人生"
    if any(word in text for word in ("雪", "我要的不是雪", "冬")):
        return _story_from_palette(
            seed=text,
            hook="雪停以后我终于放手",
            short_hook="雪停后",
            images=["旧路口", "玻璃窗", "空站台", "半盏灯", "回程票", "雪后街", "白月光", "围巾角"],
            emotions=["不再等候", "慢慢放下", "转身向前", "把爱归还"],
            rhymes=["口", "手", "候", "走"],
        )
    if any(word in text for word in ("少年", "年少", "青春")):
        return _story_from_palette(
            seed=text,
            hook="把少年留在晚风里",
            short_hook="少年啊",
            images=["旧操场", "单车铃", "白衬衫", "毕业照", "站台灯", "老街口", "课桌角", "远山外"],
            emotions=["轻轻告别", "继续奔跑", "笑着回头", "把梦收好"],
            rhymes=["里", "起", "你", "底"],
        )
    if any(word in text for word in ("佛", "求佛", "祈")):
        return _story_from_palette(
            seed=text,
            hook="我向人海求一个回声",
            short_hook="求回声",
            images=["旧香火", "长街雨", "青石阶", "夜钟声", "掌心纹", "山门外", "烛光里", "人海中"],
            emotions=["放下执念", "不问归程", "愿你安稳", "我也清醒"],
            rhymes=["声", "灯", "程", "醒"],
        )
    if any(word in text for word in ("年华", "岁月", "中年", "半生")):
        return _story_from_palette(
            seed=text,
            hook="把半生风雨唱成酒",
            short_hook="半生酒",
            images=["晚风里", "长街灯", "月光下", "老茶杯", "归家路", "山水间", "旧衣袖", "清晨后"],
            emotions=["学会低头", "仍旧温柔", "慢慢向前", "不再强求"],
            rhymes=["口", "酒", "走", "后"],
        )
    keywords = _meaningful_keywords(theme or source_title)
    hook = _make_hook_phrase(keywords, source_title)
    return _story_from_palette(
        seed=text,
        hook=hook,
        short_hook=hook[:4] or "向前走",
        images=["晚风里", "长街灯", "月光下", "人海中", "归途上", "窗台边", "旧梦里", "清晨后"],
        emotions=["慢慢释怀", "继续向前", "把梦收好", "心会放晴"],
        rhymes=["走", "口", "后", "有"],
    )


def _story_from_palette(seed: str, hook: str, short_hook: str, images: list[str], emotions: list[str], rhymes: list[str]) -> dict[str, Any]:
    image_a, image_b, image_c, image_d = (images + images[:4])[:4]
    emotion_a, emotion_b, emotion_c, emotion_d = (emotions + emotions[:4])[:4]
    chorus = [
        hook,
        f"{image_a}替我藏住伤口",
        f"不是不痛是我终于懂",
        f"{emotion_a}以后继续走",
        f"把旧梦轻轻还给风",
        f"让明天牵着我的手",
        f"爱过的人各自温柔",
        f"我在人海学会抬头",
    ]
    verse = [
        f"{image_a}吹过旧时候",
        f"{image_b}照见我低头",
        f"{image_c}还等一阵风",
        f"{emotion_b}藏进胸口",
        f"后来我学会不追问",
        f"把每次失去都成全",
        f"{image_d}陪我到清晨",
        f"心事慢慢变成温柔",
    ]
    bridge = [
        f"如果命运还要我经过黑夜",
        f"我就把歌声留在身边",
        f"等到清晨推开窗沿",
        f"{emotion_c}也算成全",
        f"人海那么宽我不再追问",
        f"{emotion_d}以后再认真",
    ]
    short = [
        short_hook,
        "别回头",
        "向前走",
        "风经过",
        "心放下",
        "梦醒了",
        "再出发",
        "会晴的",
        "不再等",
        "慢慢走",
    ]
    medium = [
        f"{image_a}起风",
        f"{image_b}无声",
        f"{emotion_a}以后",
        f"把旧梦放下",
        f"心事慢慢放下",
        f"转身别再等",
        f"月光照旧楼",
        f"明天还会亮",
        f"旧梦还给风",
    ]
    long = [
        f"{image_a}还藏着没说完的痛",
        f"我把旧梦轻轻还给风",
        f"转身以后不再为谁等",
        f"爱到最后终于学会放手",
        f"把你的名字还给旧路口",
        f"不是不痛我终于懂了",
    ]
    return {
        "seed": seed,
        "hook": hook,
        "shortHook": short_hook,
        "images": images,
        "emotions": emotions,
        "rhymes": rhymes,
        "verseRhymes": rhymes,
        "chorusRhymes": rhymes,
        "bridgeRhymes": rhymes,
        "chorus": chorus,
        "verse": verse,
        "bridge": bridge,
        "shortVariants": short,
        "mediumVariants": medium,
        "longVariants": long,
        "commercialHook": [hook, chorus[1], chorus[2], chorus[3]],
        "targetPatterns": {"Chorus": [hook, chorus[1], chorus[2], chorus[3]], "Final Chorus": [hook, chorus[4], chorus[5], chorus[7]]},
        "risingLines": [chorus[0], chorus[5], f"{emotion_c}以后我抬头"],
        "fallingLines": [f"{emotion_a}以后慢慢走", f"我把旧梦还给风", f"别让回忆困住手"],
        "genericVariants": [*verse, *medium, *long],
        "repeatLimitTokens": [short_hook, "旧梦", "明天", "人海", "温柔", "放手"],
        "rhymeVariants": _build_rhyme_variants(rhymes, chorus, verse, bridge, medium, long),
    }


def _build_rhyme_variants(rhymes: list[str], *pools: list[str]) -> dict[str, list[str]]:
    all_lines = [line for pool in pools for line in pool]
    variants: dict[str, list[str]] = {}
    fallback = {
        "口": ["把你的名字还给旧路口", "我把沉默留在旧路口", "风停在无人渡口"],
        "手": ["爱到最后终于学会放手", "让明天牵着我的手", "别让回忆困住手"],
        "走": ["转身以后继续向前走", "雪停以后我终于敢走", "后来我学会一个人走"],
        "候": ["不再把昨天当成等候", "转身以后不再等候", "雪停以后不再等候"],
        "里": ["把少年留在晚风里", "旧梦还藏在歌声里", "月光落在回忆里"],
        "起": ["把梦轻轻收起", "让旧日慢慢远去", "我和从前挥手别离"],
        "你": ["多年以后我才懂你", "青春深处还有个你", "谢谢你曾照亮心底"],
        "底": ["少年的光还在心底", "旧日的梦藏在心底", "那句告别落在心底"],
        "声": ["我向人海求一个回声", "夜钟敲醒旧日心声", "风把愿望吹成回声"],
        "灯": ["山门外还亮着一盏灯", "长街雨里只剩一盏灯", "掌心还留旧香灯"],
        "程": ["放下执念不问归程", "人海尽头还有归程", "愿你此后一路安稳"],
        "醒": ["痛过以后我终于清醒", "梦醒以后不再追寻", "夜深以后心会安静"],
        "酒": ["把半生风雨唱成酒", "旧梦入口慢慢成酒", "岁月教我低头敬酒"],
        "后": ["清晨以后继续往前走", "多年以后我还温柔", "风雨过后心会放晴"],
        "有": ["失去之后还会拥有", "人海尽头还有温柔", "明天仍会为我停留"],
    }
    for rhyme in rhymes:
        clean_rhyme = str(rhyme or "")[-1:]
        if not clean_rhyme:
            continue
        variants[clean_rhyme] = [line for line in all_lines if _clean_lyric_text(line).endswith(clean_rhyme)]
        variants[clean_rhyme].extend(fallback.get(clean_rhyme, []))
    return variants


def _lyric_candidate(story: dict[str, Any], section: str, index: int, contour: str, target_count: int) -> str:
    profile = story.get("musicProfile") or {}
    if target_count <= 4:
        pool = story.get("shortVariants") or [story.get("shortHook") or "向前走"]
    elif target_count <= 8:
        pool = story.get("mediumVariants") or story.get("verse") or []
    elif "Chorus" in section:
        pool = (story.get("targetPatterns") or {}).get(section) or story.get("commercialHook") or story.get("chorus") or []
    elif "Bridge" in section:
        pool = story.get("bridge") or story.get("longVariants") or []
    else:
        pool = story.get("verse") or story.get("longVariants") or []
    if contour == "rising" and target_count >= 7:
        pool = list(story.get("risingLines") or []) + list(pool)
    elif contour == "falling" and target_count >= 7:
        pool = list(story.get("fallingLines") or []) + list(pool)
    if profile.get("fastSong") and target_count <= 7:
        pool = list(story.get("mediumVariants") or []) + list(story.get("shortVariants") or []) + list(pool)
    if profile.get("slowSong") and target_count >= 8:
        pool = list(story.get("longVariants") or []) + list(pool)
    pool = [str(item) for item in pool if str(item or "").strip()]
    return pool[index % len(pool)] if pool else str(story.get("hook") or "向前走")


def _line_variants(raw: str, story: dict[str, Any], section: str, index: int, target: int = 12) -> list[str]:
    images = list(story.get("images") or ["晚风里", "长街灯", "月光下", "人海中"])
    image = images[index % len(images)]
    next_image = images[(index + 2) % len(images)]
    variants = [
        raw,
        *_commercial_expand_line(raw, target),
        f"{image}还藏着没说完的痛",
        f"{next_image}照见沉默的伤口",
        f"我把旧梦轻轻还给风",
        f"转身以后不再为谁等",
    ]
    for key in ("shortVariants", "mediumVariants", "longVariants", "genericVariants"):
        variants.extend(story.get(key) or [])
    if "Chorus" in section:
        variants.extend(story.get("chorus") or [])
        variants.extend(story.get("commercialHook") or [])
    elif "Bridge" in section:
        variants.extend(story.get("bridge") or [])
    else:
        variants.extend(story.get("verse") or [])
    return variants


def _short_line_candidates(
    story: dict[str, Any],
    section: str,
    index: int,
    min_count: int,
    max_count: int,
    target: int,
    rhyme: str = "",
) -> list[str]:
    pools: list[str] = []
    if max_count <= 4:
        pools.extend(story.get("shortVariants") or [])
    elif max_count <= 8:
        pools.extend(story.get("mediumVariants") or [])
        pools.extend(story.get("shortVariants") or [])
    else:
        pools.extend(story.get("longVariants") or [])
        pools.extend(story.get("mediumVariants") or [])
    if "Chorus" in section:
        pools.extend(story.get("commercialHook") or [])
        pools.extend(story.get("chorus") or [])
    elif "Bridge" in section:
        pools.extend(story.get("bridge") or [])
    else:
        pools.extend(story.get("verse") or [])
    pools.extend([
        "放下", "别回头", "向前走", "风经过", "心放下", "梦醒了", "再出发", "不再等",
        "旧梦放下", "风里远走", "转身别再等", "心事慢慢放下", "明天还会亮",
        "我把旧梦还给风", "把你的名字还给旧路口", "爱到最后终于学会放手",
    ])
    clean_rhyme = str(rhyme or "")[-1:]
    # Do not force rhyme by replacing the last character. That creates unnatural
    # phrases such as "放走". Rhyme should come from curated candidates only.
    seen: set[str] = set()
    result: list[str] = []
    for item in pools:
        text = _clean_lyric_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        length = _chinese_len(text)
        if length <= max_count:
            result.append(text)
    result.sort(key=lambda value: (0 if min_count <= _chinese_len(value) <= max_count else 1, abs(_chinese_len(value) - target)))
    return result


def _polish_line_for_slot(
    text: str,
    story: dict[str, Any],
    section: str,
    index: int,
    bounds: tuple[int, int],
    target: int,
    rhyme: str,
    used_counts: dict[str, int],
) -> str:
    clean = _clean_lyric_text(text)
    min_count, max_count = bounds
    if clean and min_count <= _chinese_len(clean) <= max_count and used_counts.get(clean, 0) == 0:
        return clean
    candidates = _short_line_candidates(story, section, index, min_count, max_count, target, rhyme)
    for candidate in candidates:
        count = _chinese_len(candidate)
        if min_count <= count <= max_count and used_counts.get(candidate, 0) == 0:
            return candidate
    for candidate in candidates:
        if _chinese_len(candidate) <= max_count:
            return candidate
    return clean if clean and _chinese_len(clean) <= max_count else "向前走"


# Late commercial lyric-fit override: keep source rhythm strict, but avoid
# repeated template lines and one-character-too-short false failures.
def _commercial_target_bounds(line: dict[str, Any], section: str) -> tuple[int, int]:
    source_count = _reference_char_count(line)
    duration_ms = int(line.get("durationMs") or 4500)
    duration_min, duration_max = _duration_char_window(duration_ms)

    near_min = max(1, source_count - 1)
    near_max = min(24, source_count + 1)
    if "Chorus" in section and duration_ms >= 3000:
        near_max = min(24, near_max + 1)
    if "Bridge" in section and duration_ms >= 4000:
        near_max = min(24, near_max + 1)

    min_count = max(duration_min, min(near_min, duration_max))
    max_count = min(duration_max, max(near_max, min_count))

    # Do not make medium/long slots exact unless the melody really requires it.
    # A 12-char source phrase can usually accept 11-13 natural Chinese chars.
    if min_count == max_count and max_count >= 8:
        min_count = max(duration_min, min_count - 1)
    if min_count == max_count and max_count >= 12:
        max_count = min(duration_max, max_count + 1)
    if min_count >= 10:
        min_count = max(duration_min, min_count - 1)
    if min_count >= 13:
        min_count = max(duration_min, min_count - 1)

    try:
        if line.get("lyricMinChars") is not None:
            min_count = max(min_count, int(line.get("lyricMinChars") or min_count))
        if line.get("lyricMaxChars") is not None:
            max_count = min(max_count, int(line.get("lyricMaxChars") or max_count))
    except Exception:
        pass
    if min_count > max_count:
        min_count = max_count
    return max(1, min_count), max(1, max_count)


def _phrase_reuse_penalty(text: str, used_counts: dict[str, int]) -> int:
    clean = _clean_lyric_text(text)
    if not clean:
        return 0
    penalty = used_counts.get(clean, 0) * 90
    for used, count in used_counts.items():
        if not used or count <= 0:
            continue
        if clean in used or used in clean:
            penalty += 35 * count
            continue
        overlap = 0
        for size in (7, 6, 5, 4):
            if len(clean) < size or len(used) < size:
                continue
            grams = {clean[i:i + size] for i in range(0, len(clean) - size + 1)}
            if any(gram in used for gram in grams):
                overlap = size
                break
        if overlap:
            penalty += overlap * 8 * count
    return penalty


def _line_variants(raw: str, story: dict[str, Any], section: str, index: int, target: int = 12) -> list[str]:
    images = list(story.get("images") or ["晚风里", "长街灯", "月光下", "人海中"])
    image = images[index % len(images)]
    next_image = images[(index + 2) % len(images)]
    third_image = images[(index + 4) % len(images)]
    variants = [
        raw,
        *_commercial_expand_line(raw, target),
        f"{image}还藏着没说完的痛",
        f"{next_image}照见沉默的伤口",
        f"{third_image}替我收好旧温柔",
        "我把旧梦轻轻还给风",
        "转身以后不再为谁等",
        "爱到最后终于学会放手",
        "不是不痛我终于懂了",
        "把沉默慢慢留在旧路口",
        "风把往事吹回旧路口",
        "后来我把执念还给人海",
        "把那场相逢写进晚风里",
        "心里的雪慢慢化成春水",
        "我把遗憾轻轻折成远方",
        "走过黑夜才敢拥抱天亮",
    ]
    for key in ("shortVariants", "mediumVariants", "longVariants", "genericVariants"):
        variants.extend(story.get(key) or [])
    if "Chorus" in section:
        variants.extend(story.get("commercialHook") or [])
        variants.extend(story.get("chorus") or [])
    elif "Bridge" in section:
        variants.extend(story.get("bridge") or [])
    else:
        variants.extend(story.get("verse") or [])
    return variants


def _fit_lyric_line(
    raw: str,
    target_count: int,
    rhyme: str,
    story: dict[str, Any],
    section: str,
    index: int,
    used_counts: dict[str, int],
    bounds: tuple[int, int] | None = None,
) -> str:
    if bounds is None:
        center = max(1, int(target_count or 6))
        bounds = (max(1, center - 1), min(15, center + 1))
    min_count, max_count = max(1, int(bounds[0])), max(1, int(bounds[1]))
    if min_count > max_count:
        min_count = max_count
    target = max(min_count, min(max_count, int(target_count or min_count)))

    candidates = _line_variants(raw, story, section, index, target)
    if rhyme:
        candidates.extend(_rhyme_variants(story, section, index, rhyme, target))
    candidates.extend(_short_line_candidates(story, section, index, min_count, max_count, target, rhyme))

    best = ""
    best_score = 100_000
    seen: set[str] = set()
    for candidate in candidates:
        text = _clean_lyric_text(candidate)
        if not text or text in seen:
            continue
        seen.add(text)
        length = _chinese_len(text)
        if length > max_count:
            continue
        score = abs(length - target) * 4
        if length < min_count:
            score += (min_count - length) * 14
        if rhyme:
            score += -3 if text.endswith(str(rhyme)[-1:]) else 4
        score += _phrase_reuse_penalty(text, used_counts)
        score += _repetition_penalty(text, story, used_counts)
        if "Chorus" not in section and text.startswith(str(story.get("shortHook") or "")):
            score += 8
        if max_count > 4 and _looks_broken_lyric(text):
            score += 80
        if _too_generic_commercial_line(text):
            score += 18
        if score < best_score:
            best = text
            best_score = score
    if best:
        return best
    fallback = _short_line_candidates(story, section, index, min_count, max_count, target, rhyme)
    for candidate in fallback:
        if used_counts.get(candidate, 0) == 0:
            return candidate
    if fallback:
        return fallback[0]
    clean_raw = _clean_lyric_text(raw)
    return clean_raw if clean_raw and _chinese_len(clean_raw) <= max_count else "向前走"


def _commercial_fallback_pool(story: dict[str, Any], section: str, index: int) -> list[str]:
    images = list(story.get("images") or ["晚风里", "长街灯", "月光下", "人海中", "旧路口", "清晨后"])
    emotions = list(story.get("emotions") or ["慢慢释怀", "继续向前", "把梦收好", "心会放晴"])
    a = images[index % len(images)]
    b = images[(index + 1) % len(images)]
    c = images[(index + 2) % len(images)]
    e = emotions[index % len(emotions)]
    pool = [
        f"{a}起风", f"{b}无声", f"{c}还亮", f"{e}以后",
        f"{a}吹过旧时候", f"{b}照见我低头", f"{c}等过一场梦", f"{e}藏进胸口",
        "我把旧梦还给风", "转身以后别再等", "后来学会不追问", "心事慢慢放下",
        "明天仍会亮", "风雨过后会晴", "人海尽头温柔", "把遗憾交给风",
        "我把旧梦轻轻还给风", "转身以后不再为谁等", "爱到最后终于学会放手",
        "把你的名字还给旧路口", "不是不痛我终于懂了", "把沉默慢慢留在旧路口",
        "风把往事吹回旧路口", "后来我把执念还给人海", "把那场相逢写进晚风里",
        "心里的雪慢慢化成春水", "我把遗憾轻轻折成远方", "走过黑夜才敢拥抱天亮",
        "多年以后我学会温柔", "清晨以后继续往前走", "人海那么宽不再追问",
        "旧梦入口慢慢成酒", "岁月教我低头敬酒", "把半生风雨唱成酒",
        "城市灯火替我守候", "长街尽头还有归程", "愿你此后一路安稳",
        "夜深以后心会安静", "痛过以后我终于清醒", "梦醒以后不再追寻",
        f"后来{a}吹过旧时候", f"终于{b}照见我低头", f"直到{c}等过一场梦",
        f"后来{e}藏进胸口", "后来我把旧梦还给晚风", "终于转身以后别再等候",
        "后来我学会不再追问", "终于把心事慢慢放下", "后来明天仍会为我亮",
        "终于风雨过后心会放晴", "后来人海尽头还有温柔", "终于把遗憾交给晚风",
        "后来我把旧梦轻轻还给风", "终于转身以后不再为谁等",
        "后来爱到最后终于学会放手", "终于把你的名字还给旧路口",
        "后来不是不痛我终于懂了", "终于把沉默慢慢留在旧路口",
        "后来风把往事吹回旧路口", "终于我把执念还给人海",
        "后来把那场相逢写进晚风里", "终于心里的雪慢慢化成春水",
        "后来我把遗憾轻轻折成远方", "终于走过黑夜才敢拥抱天亮",
        "后来多年以后我学会温柔", "终于清晨以后继续往前走",
        "后来人海那么宽不再追问", "终于旧梦入口慢慢酿成酒",
        "后来岁月教我低头敬杯酒", "终于把半生风雨唱成酒",
        "后来城市灯火替我守候", "终于长街尽头还有归程",
    ]
    if "Chorus" in section:
        pool.extend([
            story.get("hook") or "把旧梦还给风",
            "把爱放回人海以后",
            "这一次我不再回头",
            "让明天替我开口",
            "我在风里重新抬头",
            "把遗憾唱到天亮以后",
        ])
    return [str(item) for item in pool if str(item or "").strip()]


def _line_variants(raw: str, story: dict[str, Any], section: str, index: int, target: int = 12) -> list[str]:
    images = list(story.get("images") or ["晚风里", "长街灯", "月光下", "人海中"])
    image = images[index % len(images)]
    next_image = images[(index + 2) % len(images)]
    third_image = images[(index + 4) % len(images)]
    variants = [
        raw,
        *_commercial_expand_line(raw, target),
        f"{image}还藏着没说完的痛",
        f"{next_image}照见沉默的伤口",
        f"{third_image}替我收好旧温柔",
        *_commercial_fallback_pool(story, section, index),
    ]
    for key in ("shortVariants", "mediumVariants", "longVariants", "genericVariants"):
        variants.extend(story.get(key) or [])
    if "Chorus" in section:
        variants.extend(story.get("commercialHook") or [])
        variants.extend(story.get("chorus") or [])
    elif "Bridge" in section:
        variants.extend(story.get("bridge") or [])
    else:
        variants.extend(story.get("verse") or [])
    return variants


def _polish_line_for_slot(
    text: str,
    story: dict[str, Any],
    section: str,
    index: int,
    bounds: tuple[int, int],
    target: int,
    rhyme: str,
    used_counts: dict[str, int],
) -> str:
    clean = _clean_lyric_text(text)
    min_count, max_count = bounds
    if clean and min_count <= _chinese_len(clean) <= max_count and used_counts.get(clean, 0) == 0:
        return clean
    candidates = []
    candidates.extend(_line_variants(clean or text, story, section, index, target))
    candidates.extend(_short_line_candidates(story, section, index, min_count, max_count, target, rhyme))
    if rhyme:
        candidates.extend(_rhyme_variants(story, section, index, rhyme, target))

    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for item in candidates:
        candidate = _clean_lyric_text(item)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        count = _chinese_len(candidate)
        if count > max_count:
            continue
        score = abs(count - target) * 5
        if count < min_count:
            score += (min_count - count) * 15
        score += _phrase_reuse_penalty(candidate, used_counts)
        if rhyme:
            score += -2 if candidate.endswith(str(rhyme)[-1:]) else 3
        if max_count > 4 and _looks_broken_lyric(candidate):
            score += 80
        scored.append((score, candidate))
    if scored:
        scored.sort(key=lambda item: item[0])
        return scored[0][1]
    return clean if clean and _chinese_len(clean) <= max_count else "向前走"

