from __future__ import annotations

import os
import hashlib
import json
import shutil
import signal
import subprocess
import sys
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from newsclip_agent.config import load_config
from newsclip_agent.multisource import MultiSourceItem, build_multi_source_video
from newsclip_agent.remote_ucms import (
    count_ucms_videos,
    download_ucms_video,
    list_ucms_videos,
    load_remote_ucms_config,
    public_config as remote_ucms_public_config,
)
from newsclip_agent.utils import ensure_dir, read_json, relpath, write_json
from newsclip_agent.tts_omnivoice import generate_omnivoice_audio
from newsclip_agent.job_store import JobStore


ROOT = Path(__file__).resolve().parent
OUTPUTS_DIR = ROOT / "outputs"
COMMON_OUTPUTS_DIR = OUTPUTS_DIR / "__common__"
VIDEOS_DIR = ROOT / "videos"
STATIC_DIR = ROOT / "web_static"
RUNNER = ROOT / "run_pipeline.py"
MULTI_SOURCE_RUNNER = ROOT / "run_multisource_pipeline.py"

app = FastAPI(title="凤凰新闻视频智能拆条工作台")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

JOBS: dict[str, dict[str, Any]] = {}
JOB_LOCK = RLock()
PENDING_JOB_IDS: deque[str] = deque()
JOB_STORE = JobStore(OUTPUTS_DIR / ".jobs")
for job_id, job in JOB_STORE.load_jobs().items():
    JOBS[job_id] = job
    if job.get("status") == "pending":
        PENDING_JOB_IDS.append(job_id)
PROJECT_CONFIG = load_config(ROOT / "config.toml")
WORKFLOW_DEFAULTS = PROJECT_CONFIG.workflow
SHORT_VIDEO_DEFAULTS = PROJECT_CONFIG.short_video
VOICEOVER_DEFAULTS = PROJECT_CONFIG.voiceover
REMOTE_UCMS_CONFIG = load_remote_ucms_config(PROJECT_CONFIG)
REMOTE_STATION_COUNTS: dict[str, int] = {}
REMOTE_STATION_COUNT_ERRORS: dict[str, str] = {}
REMOTE_STATION_COUNT_LOCK = RLock()
WEB_CONCURRENCY = PROJECT_CONFIG.raw.get("web_concurrency", {})
MAX_RUNNING_JOBS = int(os.environ.get("WEB_MAX_RUNNING_JOBS", WEB_CONCURRENCY.get("max_running_jobs", 2)))
MAX_PENDING_JOBS = int(os.environ.get("WEB_MAX_PENDING_JOBS", WEB_CONCURRENCY.get("max_pending_jobs", 20)))
SAME_TASK_POLICY = str(WEB_CONCURRENCY.get("same_task_policy", "reject"))


from newsclip_agent.workflow_registry import (
    step_order,
    common_reusable_steps,
)


def _use_unified_source_pipeline(config: Any = PROJECT_CONFIG) -> bool:
    workflow = config.raw.get("workflow", {}) if hasattr(config, "raw") else {}
    return bool(workflow.get("use_unified_source_pipeline", True))


def _common_reusable_steps_for_request(
    *,
    source_count: int,
    use_unified_source_pipeline: bool,
) -> list[str]:
    unified = bool(use_unified_source_pipeline or source_count > 1)
    return common_reusable_steps(unified=unified)


def _step_order_for_production_mode(
    production_mode: str,
    *,
    use_unified_source_pipeline: bool = True,
    source_count: int = 1,
) -> list[str]:
    unified = bool(use_unified_source_pipeline or source_count > 1)
    return step_order(production_mode, unified=unified)


def _init_multisource_child_manifest(
    *,
    task_dir: Path,
    task_id: str,
    req: RunRequest,
    raw_source_items: list[dict[str, Any]],
    common_task_id: str,
    common_source_key: str,
    use_unified_source_pipeline: bool = True,
) -> None:
    """Create a complete pending manifest for a multi-source child task.

    The actual common analysis runs under outputs/__common__/common_xxx,
    but the UI should only read the child task manifest. Therefore we create
    all expected steps here and mark reusable common steps explicitly.
    """
    manifest_path = task_dir / "manifest.json"
    if manifest_path.exists():
        return

    now = datetime.now().isoformat(timespec="seconds")
    source_count = len(raw_source_items)
    common_steps = set(
        _common_reusable_steps_for_request(
            source_count=source_count,
            use_unified_source_pipeline=use_unified_source_pipeline,
        )
    )
    steps: dict[str, dict[str, Any]] = {}

    for step in _step_order_for_production_mode(
        req.production_mode,
        source_count=source_count,
        use_unified_source_pipeline=use_unified_source_pipeline,
    ):
        item: dict[str, Any] = {
            "step_name": step,
            "status": "pending",
            "updated_at": now,
            "can_rerun": False,
        }
        if step in common_steps:
            item["common_progress"] = True
            item["common_task_id"] = common_task_id
        steps[step] = item

    write_json(
        manifest_path,
        {
            "task_id": task_id,
            "created_at": now,
            "updated_at": now,
            "status": "pending",
            "source_mode": "multi_source_pending",
            "source_video": "",
            "source_manifest": "",
            "source_videos": [],
            "production_mode": req.production_mode,
            "common_task_id": common_task_id,
            "common_source_key": common_source_key,
            "source_count": source_count,
            "last_web_run_options": req.model_dump(),
            "steps": steps,
        },
    )


@app.on_event("startup")
def preload_remote_station_counts() -> None:
    _refresh_remote_station_counts()


class RunRequest(BaseModel):
    input_video: str | None = None
    input_videos: list[str] = Field(default_factory=list)
    remote_video: dict[str, Any] | None = None
    remote_videos: list[dict[str, Any]] = Field(default_factory=list)
    remote_video_id: str | None = None
    remote_record_station: str | None = None
    source_items: list[dict[str, Any]] = Field(default_factory=list)
    force_remote_download: bool = False
    task_id: str | None = None
    rerun: str | None = None
    rerun_from: str | None = None
    chunk: str | None = None
    failed_only: bool = False
    mode: str = "normal"
    use_version: list[str] = Field(default_factory=list)
    chunk_seconds: int = int(WORKFLOW_DEFAULTS.get("default_chunk_seconds", 60))
    frame_interval: int = int(WORKFLOW_DEFAULTS.get("default_frame_interval", 5))
    aspect_ratio: str = str(WORKFLOW_DEFAULTS.get("default_aspect_ratio", "16:9"))
    only_analysis: bool = False
    skip_tts: bool = False
    skip_render: bool = False
    target_duration_seconds: int = int(SHORT_VIDEO_DEFAULTS.get("default_target_seconds", 30))
    target_duration_mode: str = "fixed"
    output_mode: str = "single"
    max_output_videos: int = 1
    min_output_video_seconds: int = 30
    max_output_video_seconds: int = 90
    allow_long_video: bool = bool(SHORT_VIDEO_DEFAULTS.get("allow_long_video_default", False))
    require_tts: bool = bool(VOICEOVER_DEFAULTS.get("tts_required_by_default", True))
    voice_id: str | None = None
    audio_policy: str = "ai_voiceover"
    allow_original_audio_evidence: bool = False
    production_mode: str = "ai_voiceover"
    reassembly_output_mode: str = "single"
    reassembly_sort_mode: str = "editorial"
    reassembly_target_seconds: int | None = None
    reassembly_max_clip_count: int = 8
    reassembly_export_individual_clips: bool = False
    reuse_from_task_id: str | None = None
    client_id: str | None = None


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(
        (STATIC_DIR / "index.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "time": datetime.now().isoformat(timespec="seconds")}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return {
        "workflow": {
            "default_aspect_ratio": WORKFLOW_DEFAULTS.get("default_aspect_ratio", "16:9"),
            "default_chunk_seconds": int(WORKFLOW_DEFAULTS.get("default_chunk_seconds", 60)),
            "default_frame_interval": int(WORKFLOW_DEFAULTS.get("default_frame_interval", 5)),
            "default_run_mode": WORKFLOW_DEFAULTS.get("default_run_mode", "all"),
        },
        "short_video": {
            "default_target_seconds": int(SHORT_VIDEO_DEFAULTS.get("default_target_seconds", 30)),
            "hard_max_without_confirmation": int(SHORT_VIDEO_DEFAULTS.get("hard_max_without_confirmation", 60)),
            "allow_long_video_default": bool(SHORT_VIDEO_DEFAULTS.get("allow_long_video_default", False)),
        },
        "voiceover": {
            "tts_required_by_default": bool(VOICEOVER_DEFAULTS.get("tts_required_by_default", True)),
            "allow_original_audio_evidence": bool(VOICEOVER_DEFAULTS.get("allow_original_audio_evidence", False)),
            "default_voice_id": str(PROJECT_CONFIG.omnivoice.get("default_voice_id", "")),
        },
        "voices": _public_voice_catalog(),
        "remote_ucms": remote_ucms_public_config(
            REMOTE_UCMS_CONFIG,
            station_counts=_remote_station_counts_snapshot(),
            station_count_errors=_remote_station_count_errors_snapshot(),
        ),
    }


def _voice_catalog() -> dict[str, dict[str, Any]]:
    voices = PROJECT_CONFIG.raw.get("voices", {})
    if not isinstance(voices, dict):
        voices = {}

    catalog: dict[str, dict[str, Any]] = {}
    for key, item in voices.items():
        if not isinstance(item, dict):
            continue
        voice_id = str(item.get("id") or key).strip()
        if not voice_id or item.get("enabled") is False:
            continue
        catalog[voice_id] = {
            "id": voice_id,
            "name": str(item.get("name") or voice_id),
            "description": str(item.get("description") or ""),
            "style": str(item.get("style") or ""),
            "gender": str(item.get("gender") or ""),
            "reference_audio": str(item.get("reference_audio") or ""),
            "reference_text": str(item.get("reference_text") or ""),
            "preview_audio": str(item.get("preview_audio") or item.get("reference_audio") or ""),
            "speed": item.get("speed"),
        }

    if not catalog:
        default_id = str(PROJECT_CONFIG.omnivoice.get("default_voice_id") or "default")
        catalog[default_id] = {
            "id": default_id,
            "name": "默认音色",
            "description": "来自 [omnivoice] 默认参考音频",
            "style": "default",
            "gender": "",
            "reference_audio": str(PROJECT_CONFIG.omnivoice.get("reference_audio") or ""),
            "reference_text": str(PROJECT_CONFIG.omnivoice.get("reference_text") or ""),
            "preview_audio": str(PROJECT_CONFIG.omnivoice.get("reference_audio") or ""),
            "speed": PROJECT_CONFIG.omnivoice.get("speed"),
        }

    return catalog


def _public_voice_catalog() -> list[dict[str, Any]]:
    default_voice_id = str(PROJECT_CONFIG.omnivoice.get("default_voice_id") or "")
    return [
        {
            "id": voice["id"],
            "name": voice["name"],
            "description": voice["description"],
            "style": voice["style"],
            "gender": voice["gender"],
            "preview_url": f"/api/voices/{voice['id']}/preview",
            "is_default": bool(default_voice_id and voice["id"] == default_voice_id),
        }
        for voice in _voice_catalog().values()
    ]


@app.get("/api/voices")
def list_voices() -> list[dict[str, Any]]:
    return _public_voice_catalog()


@app.get("/api/voices/{voice_id}/preview")
def preview_voice(voice_id: str) -> FileResponse:
    voice = _voice_catalog().get(voice_id)
    if not voice:
        raise HTTPException(404, "voice not found")
    preview = voice.get("preview_audio") or voice.get("reference_audio")
    if not preview:
        raise HTTPException(404, "voice preview audio missing")
    path = PROJECT_CONFIG.resolve_path(str(preview))
    if not path or not path.exists() or not path.is_file():
        raise HTTPException(404, "voice preview file not found")

    root = ROOT.resolve()
    resolved = path.resolve()
    if root != resolved and root not in resolved.parents:
        raise HTTPException(400, "voice preview must be inside project directory")
    return FileResponse(str(resolved))


@app.post("/api/voices/{voice_id}/preview-tts")
def preview_voice_tts(voice_id: str, payload: dict[str, Any] | None = None) -> FileResponse:
    voice = _voice_catalog().get(voice_id)
    if not voice:
        raise HTTPException(404, "voice not found")

    payload = payload or {}
    text = str(
        payload.get("text")
        or "这里是凤凰新闻智能剪辑系统的配音试听。当前音色将用于生成新闻解说、字幕和粗剪成片。"
    ).strip()

    if not text:
        raise HTTPException(400, "preview text is empty")

    reference_audio = voice.get("reference_audio") or PROJECT_CONFIG.omnivoice.get("reference_audio")
    reference_text = voice.get("reference_text") or PROJECT_CONFIG.omnivoice.get("reference_text") or ""
    speed = voice.get("speed")
    if speed is None:
        speed = PROJECT_CONFIG.omnivoice.get("speed")

    ref_path = PROJECT_CONFIG.resolve_path(str(reference_audio))
    if not ref_path or not ref_path.exists() or not ref_path.is_file():
        raise HTTPException(404, "voice reference audio not found")

    model_path = PROJECT_CONFIG.resolve_path(
        str(PROJECT_CONFIG.omnivoice.get("model_path") or "models/OmniVoice")
    )
    if not model_path or not model_path.exists():
        raise HTTPException(404, "OmniVoice model path not found")

    preview_dir = OUTPUTS_DIR / "__voice_previews"
    ensure_dir(preview_dir)

    cache_key = hashlib.sha256(
        json.dumps(
            {
                "voice_id": voice_id,
                "text": text,
                "reference_audio": str(reference_audio),
                "reference_text": reference_text,
                "speed": speed,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]

    output_path = preview_dir / f"{voice_id}_{cache_key}.wav"

    if not output_path.exists() or output_path.stat().st_size <= 0:
        result = generate_omnivoice_audio(
            text=text,
            output_path=output_path,
            model_path=model_path,
            reference_audio=ref_path,
            reference_text=reference_text,
            speed=speed,
        )
        if result.status != "success" or not output_path.exists():
            raise HTTPException(500, f"TTS preview failed: {result.error}")

    return FileResponse(
        str(output_path),
        media_type="audio/wav",
        filename=output_path.name,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/remote-videos")
def list_remote_videos(
    record_station: str | None = None,
    current: int = 1,
    page_size: int | None = None,
    keyword: str | None = None,
    sort_order: str = "desc",
) -> dict[str, Any]:
    try:
        result = list_ucms_videos(
            REMOTE_UCMS_CONFIG,
            record_station=record_station,
            current=current,
            page_size=page_size,
            keyword=keyword,
            sort_order=sort_order,
        )
        station = record_station or REMOTE_UCMS_CONFIG.default_record_station
        total = result.get("pagination", {}).get("total")
        if station and total is not None:
            _set_remote_station_count(station, int(total))
        result["station_counts"] = _remote_station_counts_snapshot()
        result["station_count_errors"] = _remote_station_count_errors_snapshot()
        return result
    except Exception as exc:
        raise HTTPException(502, f"remote video list failed: {exc}") from exc


@app.post("/api/remote-videos/download")
def download_remote_video(payload: dict[str, Any]) -> dict[str, Any]:
    remote_video = payload.get("remote_video") or payload
    if not isinstance(remote_video, dict):
        raise HTTPException(400, "remote_video is required")
    try:
        return download_ucms_video(
            REMOTE_UCMS_CONFIG,
            remote_video,
            root_dir=ROOT,
            force=bool(payload.get("force_remote_download", False)),
        )
    except Exception as exc:
        raise HTTPException(502, f"remote video download failed: {exc}") from exc


def _refresh_remote_station_counts() -> None:
    stations = _configured_remote_stations()
    if not REMOTE_UCMS_CONFIG.enabled or not stations:
        return
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    max_workers = min(4, len(stations))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(count_ucms_videos, REMOTE_UCMS_CONFIG, record_station=station): station
            for station in stations
        }
        for future in as_completed(futures):
            station = futures[future]
            try:
                counts[station] = int(future.result())
            except Exception as exc:
                errors[station] = str(exc)
    with REMOTE_STATION_COUNT_LOCK:
        REMOTE_STATION_COUNTS.clear()
        REMOTE_STATION_COUNTS.update(counts)
        REMOTE_STATION_COUNT_ERRORS.clear()
        REMOTE_STATION_COUNT_ERRORS.update(errors)


def _configured_remote_stations() -> list[str]:
    seen: set[str] = set()
    stations = []
    for station in [REMOTE_UCMS_CONFIG.default_record_station, *REMOTE_UCMS_CONFIG.record_stations]:
        station = str(station or "").strip()
        if station and station not in seen:
            stations.append(station)
            seen.add(station)
    return stations


def _set_remote_station_count(station: str, count: int) -> None:
    with REMOTE_STATION_COUNT_LOCK:
        REMOTE_STATION_COUNTS[station] = count
        REMOTE_STATION_COUNT_ERRORS.pop(station, None)


def _remote_station_counts_snapshot() -> dict[str, int]:
    with REMOTE_STATION_COUNT_LOCK:
        return dict(REMOTE_STATION_COUNTS)


def _remote_station_count_errors_snapshot() -> dict[str, str]:
    with REMOTE_STATION_COUNT_LOCK:
        return dict(REMOTE_STATION_COUNT_ERRORS)


@app.get("/api/videos")
def list_videos() -> list[dict[str, Any]]:
    if not VIDEOS_DIR.exists():
        return []
    videos = []
    for path in sorted(VIDEOS_DIR.glob("*")):
        if path.suffix.lower() not in {".mp4", ".mov", ".mkv", ".m4v", ".avi"}:
            continue
        videos.append(
            {
                "name": path.name,
                "path": relpath(path, ROOT),
                "size_mb": round(path.stat().st_size / 1024 / 1024, 2),
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            }
        )
    return videos


@app.get("/api/videos/preview")
def preview_video(path: str):
    file_path = _resolve_input_video(path)
    return FileResponse(str(file_path))


@app.post("/api/videos/upload")
async def upload_video(file: UploadFile = File(...)) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".m4v", ".avi"}:
        raise HTTPException(400, "unsupported video type")
    ensure_dir(VIDEOS_DIR)
    target = _unique_video_path(_safe_filename(file.filename or f"video{suffix}"))
    try:
        with target.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                out.write(chunk)
    finally:
        await file.close()
    return {
        "name": target.name,
        "path": relpath(target, ROOT),
        "size_mb": round(target.stat().st_size / 1024 / 1024, 2),
        "updated_at": datetime.fromtimestamp(target.stat().st_mtime).isoformat(timespec="seconds"),
    }


@app.post("/api/videos/upload-multiple")
async def upload_multiple_videos(files: list[UploadFile] = File(...)) -> list[dict[str, Any]]:
    results = []
    for file in files:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".mp4", ".mov", ".mkv", ".m4v", ".avi"}:
            await file.close()
            raise HTTPException(400, f"unsupported video type: {file.filename}")
        ensure_dir(VIDEOS_DIR)
        target = _unique_video_path(_safe_filename(file.filename or f"video{suffix}"))
        try:
            with target.open("wb") as out:
                while chunk := await file.read(1024 * 1024):
                    out.write(chunk)
        finally:
            await file.close()
        results.append(
            {
                "name": target.name,
                "path": relpath(target, ROOT),
                "size_mb": round(target.stat().st_size / 1024 / 1024, 2),
                "updated_at": datetime.fromtimestamp(target.stat().st_mtime).isoformat(timespec="seconds"),
            }
        )
    return results


@app.delete("/api/videos")
def delete_video(path: str) -> dict[str, Any]:
    file_path = _resolve_input_video(path)
    file_path.unlink()
    return {"ok": True, "deleted": relpath(file_path, ROOT)}


def _display_title_from_task_id(task_id: str) -> str:
    value = task_id
    for alias in _all_mode_aliases():
        value = value.replace(f"_{alias}_", "_")
        if value.endswith(f"_{alias}"):
            value = value[: -len(alias) - 1]
    return value


def _task_group_meta(task_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    source_request = read_json(task_dir / "input" / "source_request.json", {})
    opts = manifest.get("last_web_run_options") or manifest.get("last_run_options") or {}

    production_mode = (
        manifest.get("production_mode")
        or opts.get("production_mode")
        or source_request.get("production_mode")
        or _task_id_production_mode(task_dir.name)
        or "ai_voiceover"
    )

    common_source_key = (
        manifest.get("common_source_key")
        or opts.get("common_source_key")
        or source_request.get("common_source_key")
        or ""
    )
    common_task_id = (
        manifest.get("common_task_id")
        or opts.get("common_task_id")
        or source_request.get("common_task_id")
        or ""
    )

    source_items = source_request.get("source_items") or []
    source_names: list[str] = []
    for index, item in enumerate(source_items, start=1):
        if not isinstance(item, dict):
            continue
        if item.get("display_name"):
            source_names.append(str(item["display_name"]))
            continue
        if item.get("path"):
            source_names.append(Path(str(item["path"])).name)
            continue
        remote = item.get("remote_video") if isinstance(item.get("remote_video"), dict) else item
        source_names.append(
            str(
                remote.get("display_name")
                or remote.get("name")
                or remote.get("title")
                or remote.get("id")
                or f"素材 {index}"
            )
        )

    source_count = len(source_names) or len(manifest.get("source_videos") or []) or 1
    if common_source_key:
        group_id = f"group_{common_source_key}"
    else:
        source_video = str(manifest.get("source_video") or "")
        group_id = (
            f"group_{hashlib.sha256(source_video.encode('utf-8')).hexdigest()[:16]}"
            if source_video
            else f"group_{task_dir.name}"
        )

    if source_count > 1:
        group_title = f"多源剪辑（{source_count}段）"
    elif source_names:
        group_title = Path(source_names[0]).stem
    else:
        group_title = _display_title_from_task_id(task_dir.name)

    return {
        "group_id": group_id,
        "group_title": group_title,
        "source_count": source_count,
        "source_names": source_names,
        "production_mode": production_mode,
        "mode_title": _mode_slug(production_mode),
        "common_source_key": common_source_key,
        "common_task_id": common_task_id,
    }


@app.get("/api/tasks")
def list_tasks() -> list[dict[str, Any]]:
    if not OUTPUTS_DIR.exists():
        return []
    tasks = []
    seen_task_ids: set[str] = set()
    for task_dir in sorted(
        [
            p
            for p in OUTPUTS_DIR.iterdir()
            if p.is_dir() and p.name != "__common__" and not p.name.startswith("common_")
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        manifest = read_json(task_dir / "manifest.json", {})
        manifest = _hydrate_common_progress_for_manifest(task_dir, manifest)
        steps = manifest.get("steps", {})
        source_request_exists = (task_dir / "input" / "source_request.json").exists()
        if not manifest and not source_request_exists:
            continue
        done = sum(1 for s in steps.values() if s.get("status") in {"success", "partial_success", "skipped"})
        stale = sum(1 for s in steps.values() if s.get("status") == "stale")
        failed = sum(1 for s in steps.values() if s.get("status") == "failed")
        job_status = _active_job_status_for_task(task_dir.name)
        group_meta = _task_group_meta(task_dir, manifest)
        seen_task_ids.add(task_dir.name)
        tasks.append(
            {
                "task_id": task_dir.name,
                "source_video": manifest.get("source_video", ""),
                "updated_at": manifest.get("updated_at", datetime.fromtimestamp(task_dir.stat().st_mtime).isoformat(timespec="seconds")),
                "created_at": manifest.get("created_at", ""),
                "done_steps": done,
                "stale_steps": stale,
                "failed_steps": failed,
                "step_count": len(steps) or (1 if source_request_exists else 0),
                "job_status": job_status,
                "common_task_id": group_meta["common_task_id"],
                "common_source_key": group_meta["common_source_key"],
                "group_id": group_meta["group_id"],
                "group_title": group_meta["group_title"],
                "source_count": group_meta["source_count"],
                "source_names": group_meta["source_names"],
                "production_mode": group_meta["production_mode"],
                "mode_title": group_meta["mode_title"],
                "reused_common_steps": manifest.get("reused_common_steps", []),
            }
        )
    for job in sorted(
        [_refresh_job(job_id) for job_id in list(JOBS)],
        key=lambda item: item.get("created_at") or item.get("started_at") or "",
        reverse=True,
    ):
        task_id = job.get("task_id")
        if not task_id or task_id in seen_task_ids or job.get("status") not in {"pending", "running"}:
            continue
        task_dir = OUTPUTS_DIR / task_id
        manifest = read_json(task_dir / "manifest.json", {}) if task_dir.exists() else {}
        group_meta = _task_group_meta(task_dir, manifest)
        seen_task_ids.add(task_id)
        tasks.append(
            {
                "task_id": task_id,
                "source_video": "",
                "updated_at": job.get("created_at", ""),
                "created_at": job.get("created_at", ""),
                "done_steps": 0,
                "stale_steps": 0,
                "failed_steps": 0,
                "step_count": 0,
                "job_status": job.get("status", ""),
                "common_task_id": group_meta["common_task_id"],
                "common_source_key": group_meta["common_source_key"],
                "group_id": group_meta["group_id"],
                "group_title": group_meta["group_title"],
                "source_count": group_meta["source_count"],
                "source_names": group_meta["source_names"],
                "production_mode": group_meta["production_mode"],
                "mode_title": group_meta["mode_title"],
            }
        )
    return tasks


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str) -> dict[str, Any]:
    for job in JOBS.values():
        if job.get("task_id") == task_id and _refresh_job(job["job_id"]).get("status") in {"pending", "running"}:
            raise HTTPException(409, "task is running")
    task_dir = _task_dir(task_id)
    shutil.rmtree(task_dir)
    return {"ok": True, "deleted": task_id}


@app.get("/api/tasks/{task_id}/manifest")
def get_manifest(task_id: str) -> dict[str, Any]:
    task_dir = _task_dir(task_id)
    manifest = read_json(task_dir / "manifest.json", None)
    if manifest is None:
        source_request = read_json(task_dir / "input" / "source_request.json", None)
        if source_request is None:
            raise HTTPException(404, "manifest.json not found")
        production_mode = source_request.get(
            "production_mode",
            _task_id_production_mode(task_id) or "ai_voiceover",
        )
        manifest = {
            "task_id": task_id,
            "created_at": source_request.get("created_at", ""),
            "updated_at": datetime.fromtimestamp(task_dir.stat().st_mtime).isoformat(timespec="seconds"),
            "source_video": "",
            "source_mode": "multi_source_pending",
            "source_manifest": "",
            "source_videos": [],
            "production_mode": production_mode,
            "common_task_id": source_request.get("common_task_id", ""),
            "common_source_key": source_request.get("common_source_key", ""),
            "last_web_run_options": {
                "production_mode": production_mode,
                "output_mode": source_request.get("output_mode", "single"),
                "max_output_videos": source_request.get("max_output_videos", 1),
                "min_output_video_seconds": source_request.get("min_output_video_seconds", 30),
                "max_output_video_seconds": source_request.get("max_output_video_seconds", 90),
                "aspect_ratio": source_request.get("aspect_ratio", "16:9"),
                "chunk_seconds": source_request.get(
                    "chunk_seconds",
                    int(WORKFLOW_DEFAULTS.get("default_chunk_seconds", 60)),
                ),
                "frame_interval": source_request.get(
                    "frame_interval",
                    int(WORKFLOW_DEFAULTS.get("default_frame_interval", 10)),
                ),
                "mode": source_request.get("mode", "normal"),
                "common_task_id": source_request.get("common_task_id", ""),
                "common_source_key": source_request.get("common_source_key", ""),
            },
            "steps": {
                "source_prepare": {
                    "status": "pending",
                    "source_count": len(source_request.get("source_items") or []),
                }
            },
        }
    _hydrate_manifest_options(task_dir, manifest)
    manifest = _hydrate_common_progress_for_manifest(task_dir, manifest)
    
    job_status = _active_job_status_for_task(task_id)
    if manifest.get("status") == "running" and job_status != "running":
        manifest["status"] = "failed"
        manifest["user_message"] = "任务意外终止或后台服务已重启。"
        try:
            write_json(task_dir / "manifest.json", manifest)
        except Exception:
            pass

    return manifest


@app.get("/api/tasks/{task_id}/tree")
def get_task_tree(task_id: str) -> list[dict[str, Any]]:
    task_dir = _task_dir(task_id)
    files = []
    for path in sorted(task_dir.rglob("*")):
        if path.is_file():
            files.append(
                {
                    "path": relpath(path, task_dir),
                    "size_kb": round(path.stat().st_size / 1024, 1),
                    "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                }
            )
    return files[:2000]


def _task_file_url(task_id: str, task_dir: Path, path: Path) -> str:
    relative = relpath(path, task_dir)
    version = path.stat().st_mtime_ns if path.exists() else 0
    return f"/api/tasks/{task_id}/file?path={quote(relative, safe='/')}&v={version}"


@app.get("/api/tasks/{task_id}/file")
def get_task_file(task_id: str, path: str):
    task_dir = _task_dir(task_id)
    file_path = _safe_child(task_dir, path)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "file not found")
    suffix = file_path.suffix.lower()
    if suffix in {".json"}:
        return JSONResponse(read_json(file_path, {}))
    if suffix in {".txt", ".srt", ".log", ".md"}:
        return PlainTextResponse(file_path.read_text(encoding="utf-8", errors="replace"))
    if suffix in {".mp4", ".mov", ".mkv", ".wav", ".mp3", ".jpg", ".jpeg", ".png"}:
        return FileResponse(str(file_path))
    return PlainTextResponse(file_path.read_text(encoding="utf-8", errors="replace"))


@app.get("/api/tasks/{task_id}/preview")
def preview_source(task_id: str):
    manifest = get_manifest(task_id)
    source = manifest.get("source_video")
    if not source:
        raise HTTPException(404, "source video missing")
    file_path = _resolve_task_source(_task_dir(task_id), source)
    return FileResponse(str(file_path))


@app.get("/api/tasks/{task_id}/source-videos")
def list_source_videos(task_id: str) -> list[dict[str, Any]]:
    task_dir = _task_dir(task_id)
    manifest = get_manifest(task_id)
    request_previews = _source_request_preview_items(task_dir, task_id)
    if request_previews:
        return request_previews

    sources: list[dict[str, Any]] = []
    source = manifest.get("source_video")
    if source:
        source_path = str(source)
        source_url = ""
        try:
            resolved_source = _resolve_task_source(task_dir, str(source))
            source_path = relpath(resolved_source, ROOT)
            source_url = f"/api/videos/preview?path={quote(source_path, safe='/')}"
        except HTTPException:
            source_url = ""
        sources.append(
            {
                "label": "拼接原片" if manifest.get("source_mode") == "multi_source_concat_proxy" else "原片",
                "file": source_path,
                "url": source_url,
                "source_index": 0,
                "source_type": "concat" if manifest.get("source_mode") == "multi_source_concat_proxy" else "single",
            }
        )
    for index, item in enumerate(manifest.get("source_videos") or [], start=1):
        if not isinstance(item, dict):
            continue
        file_ref = item.get("normalized_file") or ""
        url = f"/api/tasks/{task_id}/file?path={file_ref}" if file_ref and (task_dir / file_ref).exists() else ""
        original = item.get("original_path") or ""
        if not url and original:
            try:
                original_path = _resolve_input_video(str(original))
                url = f"/api/videos/preview?path={relpath(original_path, ROOT)}"
            except HTTPException:
                url = ""
        remote = item.get("remote_video") or item.get("remote") or {}
        if not url and isinstance(remote, dict):
            url = (
                remote.get("preview_url")
                or remote.get("media_low_url")
                or remote.get("mediaLow")
                or remote.get("media_high_url")
                or remote.get("mediaHigh")
                or remote.get("download_url")
                or ""
            )
        sources.append(
            {
                "label": item.get("display_name") or f"原始素材 {index}",
                "file": file_ref or original,
                "url": url,
                "source_index": item.get("source_index", index),
                "source_type": item.get("source_type", ""),
                "source_id": item.get("source_id", ""),
                "duration_seconds": item.get("duration_seconds"),
                "virtual_start": item.get("virtual_start", ""),
                "virtual_end": item.get("virtual_end", ""),
            }
        )
    return sources


@app.get("/api/tasks/{task_id}/latest-video")
def latest_video(task_id: str) -> dict[str, Any]:
    task_dir = _task_dir(task_id)
    latest = _find_latest_draft_video(task_dir)
    if not latest:
        return {"exists": False, "file": "", "url": ""}
    return {
        "exists": True,
        "file": relpath(latest, task_dir),
        "url": _task_file_url(task_id, task_dir, latest),
    }


@app.get("/api/tasks/{task_id}/drafts")
def list_draft_videos(task_id: str) -> list[dict[str, Any]]:
    task_dir = _task_dir(task_id)
    drafts = _list_draft_videos(task_dir)
    return [
        {
            "file": relpath(path, task_dir),
            "url": _task_file_url(task_id, task_dir, path),
            "name": path.name,
            "type": "highlight_reassembly_draft" if "reassembly_drafts" in path.parts else "ai_voiceover_draft",
            "size_mb": round(path.stat().st_size / 1024 / 1024, 2),
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        }
        for path in drafts
    ]


@app.post("/api/run")
def run_pipeline(req: RunRequest) -> dict[str, Any]:
    _normalize_output_options(req)
    if req.production_mode == "highlight_reassembly":
        req.audio_policy = "original"
        req.require_tts = False
        req.skip_tts = True
        req.allow_original_audio_evidence = True
    else:
        req.audio_policy = "ai_voiceover"
        req.allow_original_audio_evidence = False
    _prepare_mode_switched_run(req)
    raw_source_items = _collect_source_items(req)
    use_unified_source_pipeline = _use_unified_source_pipeline(PROJECT_CONFIG)
    should_use_source_request = bool(raw_source_items) and (
        use_unified_source_pipeline or len(raw_source_items) > 1
    )
    source_items: list[MultiSourceItem] = []
    source_manifest_path: Path | None = None
    source_request_path: Path | None = None
    if should_use_source_request:
        _validate_source_request_items(raw_source_items)
        fingerprint = _source_request_fingerprint(req, raw_source_items)
        common_source_key = _common_source_key(req, raw_source_items)
        common_task_id = f"common_{common_source_key}"
        input_name_for_task = _display_source_request_name(raw_source_items)
        task_id = _normalize_new_task_id(req.task_id, input_name_for_task, req.production_mode, fingerprint)
        task_id = _with_mode_suffix(task_id, req.production_mode)
        task_dir = ensure_dir(OUTPUTS_DIR / task_id)
        source_request_path = task_dir / "input" / "source_request.json"
        _write_source_request_for_preview(
            task_dir,
            task_id,
            req,
            raw_source_items,
            common_task_id=common_task_id,
            common_source_key=common_source_key,
        )
        _init_multisource_child_manifest(
            task_dir=task_dir,
            task_id=task_id,
            req=req,
            raw_source_items=raw_source_items,
            common_task_id=common_task_id,
            common_source_key=common_source_key,
            use_unified_source_pipeline=use_unified_source_pipeline,
        )
        input_path = None
    else:
        source_items = _resolve_run_sources(req)
    if source_items:
        input_path = source_items[0].path
        req.input_video = str(input_path)
        if source_items[0].source_type == "remote_ucms":
            req.remote_video = source_items[0].remote
        fingerprint = _run_fingerprint(req, input_path)
        input_name_for_task = _display_input_name(req)
        if input_name_for_task and not req.task_id:
            task_id = _make_task_id_from_fingerprint(input_name_for_task, req.production_mode, fingerprint)
        else:
            task_id = _resolve_run_task_id(req)
        task_id = _with_mode_suffix(task_id, req.production_mode)
        task_dir = ensure_dir(OUTPUTS_DIR / task_id)
    elif not raw_source_items:
        input_path = _prepare_run_input_video(req)
        fingerprint = _run_fingerprint(req, input_path) if input_path else ""
        input_name_for_task = _display_input_name(req)
        if input_name_for_task and not req.task_id:
            task_id = _make_task_id_from_fingerprint(input_name_for_task, req.production_mode, fingerprint)
        else:
            task_id = _resolve_run_task_id(req)
        task_id = _with_mode_suffix(task_id, req.production_mode)
        task_dir = ensure_dir(OUTPUTS_DIR / task_id)
        if (task_dir / "input" / "source_request.json").exists():
            source_request_path = task_dir / "input" / "source_request.json"
    if req.remote_video:
        write_json(
            task_dir / "remote_source.json",
            {
                "source_type": "remote_ucms",
                "remote_video": req.remote_video,
                "local_input_video": relpath(input_path, ROOT) if input_path else "",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
    if raw_source_items and should_use_source_request:
        _write_source_request_for_preview(task_dir, task_id, req, raw_source_items)

    existing_job = _find_active_job_by_task_id(task_id)
    if existing_job and not (req.rerun or req.rerun_from):
        status = existing_job.get("status", "")
        status_text = "排队中" if status == "pending" else "运行中"
        return {
            **_public_job(existing_job),
            "deduplicated": True,
            "message": f"该视频的{_mode_slug(req.production_mode)}任务正在{status_text}，已切换到现有任务。",
        }

    manifest = read_json(task_dir / "manifest.json", {})
    if not (req.rerun or req.rerun_from) and _is_task_already_completed(manifest, req.production_mode):
        return {
            "job_id": "",
            "task_id": task_id,
            "status": "success",
            "returncode": 0,
            "deduplicated": True,
            "message": "相同任务已完成，直接复用结果。",
        }

    _seed_reusable_outputs(req, task_id, task_dir)
    job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    log_dir = ensure_dir(task_dir / "web_jobs")
    log_path = log_dir / f"{job_id}.log"

    cmd = _build_run_command(req, task_id, input_path, source_manifest_path, source_request_path)
    _save_web_run_options(task_dir, req)

    with JOB_LOCK:
        active_job = _find_active_job_by_task_id(task_id)
        if active_job and SAME_TASK_POLICY == "reject":
            raise HTTPException(409, f"任务 {task_id} 已有运行中或排队中的 job，请不要对同一任务并发运行")
        pending_count = sum(1 for job in JOBS.values() if job.get("status") == "pending")
        if pending_count >= MAX_PENDING_JOBS:
            raise HTTPException(429, "等待队列已满，请稍后再提交")
        log_path.write_text(" ".join(cmd) + "\n\n=== job queued ===\n", encoding="utf-8", errors="replace")
        job = {
            "job_id": job_id,
            "task_id": task_id,
            "pid": None,
            "cmd": cmd,
            "log": relpath(log_path, ROOT),
            "log_path": str(log_path),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "started_at": "",
            "status": "pending",
            "returncode": None,
            "submitted_by": req.client_id or "anonymous",
            "task_fingerprint": fingerprint,
        }
        JOBS[job_id] = job
        PENDING_JOB_IDS.append(job_id)
        _persist_job(job)

    _schedule_jobs()
    return _public_job(JOBS[job_id])


def _build_run_command(
    req: RunRequest,
    task_id: str,
    input_path: Path | None = None,
    source_manifest_path: Path | None = None,
    source_request_path: Path | None = None,
) -> list[str]:
    runner = MULTI_SOURCE_RUNNER if source_request_path else RUNNER
    cmd = [
        sys.executable,
        "-u",
        str(runner),
        "--task-id",
        task_id,
        "--chunk-seconds",
        str(req.chunk_seconds),
        "--frame-interval",
        str(req.frame_interval),
        "--aspect-ratio",
        req.aspect_ratio,
        "--target-duration",
        str(req.target_duration_seconds),
        "--audio-policy",
        req.audio_policy,
        "--production-mode",
        req.production_mode,
        "--output-mode",
        req.output_mode,
        "--max-output-videos",
        str(req.max_output_videos),
        "--min-output-video-seconds",
        str(req.min_output_video_seconds),
        "--max-output-video-seconds",
        str(req.max_output_video_seconds),
    ]
    if req.voice_id:
        cmd.extend(["--voice-id", req.voice_id])
    if req.production_mode == "highlight_reassembly":
        cmd.extend(["--reassembly-output-mode", req.reassembly_output_mode])
        cmd.extend(["--reassembly-sort-mode", req.reassembly_sort_mode])
        cmd.extend(["--reassembly-max-clip-count", str(req.reassembly_max_clip_count)])
        if req.reassembly_target_seconds:
            cmd.extend(["--reassembly-target-seconds", str(req.reassembly_target_seconds)])
        if req.reassembly_export_individual_clips:
            cmd.append("--reassembly-export-individual-clips")
    if input_path:
        cmd.extend(["--input", str(input_path)])
    if source_manifest_path:
        cmd.extend(["--source-manifest", str(source_manifest_path)])
    if source_request_path:
        cmd.extend(["--source-request", str(source_request_path)])
    if req.rerun:
        cmd.extend(["--rerun", req.rerun])
    if req.rerun_from:
        cmd.extend(["--rerun-from", req.rerun_from])
    if req.chunk:
        cmd.extend(["--chunk", req.chunk])
    if req.failed_only:
        cmd.append("--failed-only")
    if req.mode:
        cmd.extend(["--mode", req.mode])
    for version_spec in req.use_version:
        if version_spec.strip():
            cmd.extend(["--use-version", version_spec.strip()])
    if req.only_analysis:
        cmd.append("--only-analysis")
    if req.skip_tts:
        cmd.append("--skip-tts")
    if req.skip_render:
        cmd.append("--skip-render")
    if req.allow_long_video:
        cmd.append("--allow-long-video")
    if req.require_tts:
        cmd.append("--require-tts")
    else:
        cmd.append("--no-require-tts")
    return cmd


def _terminate_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _start_job(job: dict[str, Any]) -> None:
    log_path = Path(job["log_path"])
    log_file = log_path.open("a", encoding="utf-8", errors="replace")
    log_file.write("\n=== job started ===\n")
    log_file.flush()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        job["cmd"],
        cwd=str(ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        start_new_session=(os.name != "nt"),
    )
    job.update(
        {
            "pid": process.pid,
            "process": process,
            "log_file": log_file,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "status": "running",
            "returncode": None,
        }
    )
    _persist_job(job)


def _schedule_jobs() -> None:
    with JOB_LOCK:
        running = _running_job_count()
        while running < MAX_RUNNING_JOBS and PENDING_JOB_IDS:
            job_id = PENDING_JOB_IDS.popleft()
            job = JOBS.get(job_id)
            if not job or job.get("status") != "pending":
                continue
            try:
                _start_job(job)
                running += 1
            except Exception as exc:
                job["status"] = "failed"
                job["returncode"] = -1
                job["user_message"] = str(exc)
                job["finished_at"] = datetime.now().isoformat(timespec="seconds")
                _persist_job(job)


def _running_job_count() -> int:
    count = 0
    for job_id in list(JOBS):
        public = _refresh_job(job_id, schedule_next=False)
        if public.get("status") == "running":
            count += 1
    return count


def _persist_job(job: dict[str, Any]) -> None:
    public_job = _public_job(job)
    write_json(OUTPUTS_DIR / job["task_id"] / "last_web_job.json", public_job)
    JOB_STORE.save_job(job["task_id"], public_job)


def _save_web_run_options(task_dir: Path, req: RunRequest) -> None:
    manifest_path = task_dir / "manifest.json"
    manifest = read_json(manifest_path, {})
    web_options = req.model_dump()
    web_options["updated_at"] = datetime.now().isoformat(timespec="seconds")
    manifest.setdefault("task_id", task_dir.name)
    manifest.setdefault("created_at", web_options["updated_at"])
    manifest["updated_at"] = web_options["updated_at"]
    manifest["production_mode"] = req.production_mode
    manifest["last_web_run_options"] = web_options
    write_json(manifest_path, manifest)


REUSABLE_MODE_SWITCH_STEPS = (
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "vision",
    "timeline",
    "video_understanding",
    "highlight_detection",
)


def _seed_reusable_outputs(req: RunRequest, task_id: str, task_dir: Path) -> None:
    if not req.reuse_from_task_id or (task_dir / "manifest.json").exists():
        return

    source_task_dir = _task_dir(req.reuse_from_task_id)
    source_manifest = read_json(source_task_dir / "manifest.json", {})
    source_steps = source_manifest.get("steps", {})
    copied_steps: dict[str, Any] = {}
    copied_versions: dict[str, str] = {}

    for step in REUSABLE_MODE_SWITCH_STEPS:
        step_info = source_steps.get(step) or {}
        if step_info.get("status") not in {"success", "partial_success"}:
            continue
        output = step_info.get("output")
        if not output:
            continue
        source_output = source_task_dir / output
        if not source_output.exists():
            continue
        source_copy_root = source_output.parent
        target_copy_root = task_dir / relpath(source_copy_root, source_task_dir)
        _copy_reusable_path(source_copy_root, target_copy_root)
        copied_steps[step] = step_info
        if step_info.get("version"):
            copied_versions[step] = step_info["version"]

    if not copied_steps:
        return

    source_video = Path(req.input_video).resolve() if req.input_video else _existing_task_source_video(source_task_dir, source_manifest)
    source_video_ref = relpath(source_video, task_dir)
    now = datetime.now().isoformat(timespec="seconds")
    write_json(
        task_dir / "task.json",
        {
            "task_id": task_id,
            "created_at": now,
            "source_video_original": str(source_video),
            "source_video": source_video_ref,
            "reused_from_task_id": req.reuse_from_task_id,
        },
    )
    write_json(
        task_dir / "manifest.json",
        {
            "task_id": task_id,
            "created_at": now,
            "updated_at": now,
            "source_video": source_video_ref,
            "source_video_original": str(source_video),
            "current_versions": copied_versions,
            "steps": copied_steps,
            "reused_from_task_id": req.reuse_from_task_id,
            "reused_steps": list(copied_steps),
        },
    )


def _copy_reusable_path(source: Path, target: Path) -> None:
    if target.exists():
        return
    ensure_dir(target.parent)
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    return sorted(
        [_refresh_job(job_id) for job_id in list(JOBS)],
        key=lambda item: item.get("created_at") or item.get("started_at") or "",
        reverse=True,
    )


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")
    return _refresh_job(job_id)


@app.get("/api/jobs/{job_id}/log")
def get_job_log(job_id: str) -> PlainTextResponse:
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")
    job = _refresh_job(job_id)
    path = Path(job["log_path"])
    if not path.exists():
        return PlainTextResponse("")
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace")[-80_000:])


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str) -> dict[str, Any]:
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")

    with JOB_LOCK:
        job = JOBS[job_id]
        status = job.get("status")
        if status == "pending":
            try:
                PENDING_JOB_IDS.remove(job_id)
            except ValueError:
                pass
            job["status"] = "cancelled"
            job["returncode"] = -9
            job["finished_at"] = datetime.now().isoformat(timespec="seconds")
            job["user_message"] = "任务已取消。"
            _persist_job(job)
            _schedule_jobs()
            return _public_job(job)

        if status != "running":
            return _public_job(job)

        pid = int(job.get("pid") or 0)
        _terminate_process_tree(pid)
        process = job.get("process")
        if process:
            try:
                process.terminate()
            except Exception:
                pass
            try:
                process.wait(timeout=3)
            except Exception:
                pass

        log_file = job.get("log_file")
        if log_file:
            try:
                log_file.write("\n=== job cancelled by user ===\n")
                log_file.flush()
                log_file.close()
            except Exception:
                pass

        job["status"] = "cancelled"
        job["returncode"] = -9
        job["finished_at"] = datetime.now().isoformat(timespec="seconds")
        job["user_message"] = "任务已手动终止。"
        _persist_job(job)
        _schedule_jobs()
        return _public_job(job)


def _refresh_job(job_id: str, schedule_next: bool = True) -> dict[str, Any]:
    job = JOBS[job_id]
    process = job.get("process")
    changed_to_finished = False
    if process and job["status"] == "running":
        code = process.poll()
        if code is not None:
            job["returncode"] = code
            job["status"] = "success" if code == 0 else "failed"
            job["finished_at"] = datetime.now().isoformat(timespec="seconds")
            log_file = job.get("log_file")
            if log_file:
                log_file.close()
            job["user_message"] = _extract_user_message_from_log(Path(job["log_path"]))
            _persist_job(job)
            changed_to_finished = True
    elif Path(job.get("log_path", "")).exists():
        job["user_message"] = _extract_user_message_from_log(Path(job["log_path"]))
    if changed_to_finished and schedule_next:
        _schedule_jobs()
    return _public_job(job)


def _extract_user_message_from_log(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    marker = "运行失败："
    if marker in text:
        tail = text.rsplit(marker, 1)[-1].strip()
        tail = tail.split("可在任务表中查看失败步骤", 1)[0].strip()
        return tail
    if "UnicodeEncodeError" in text and "request.encode('ascii')" in text:
        return (
            "Remote video download failed: UCMS returned a video URL with unescaped "
            "non-ASCII or special characters. Encode the download URL before making "
            "the HTTP request."
        )
    runtime_marker = "RuntimeError:"
    if runtime_marker in text:
        tail = text.rsplit(runtime_marker, 1)[-1].strip()
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        return "\n".join(lines[-4:])
    return ""


def _hydrate_common_progress_for_manifest(task_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    common_task_id = str(manifest.get("common_task_id") or "").strip()
    if not common_task_id:
        source_request = read_json(task_dir / "input" / "source_request.json", {})
        common_task_id = str(source_request.get("common_task_id") or "").strip()

    if not common_task_id:
        return manifest

    common_dir = COMMON_OUTPUTS_DIR / common_task_id
    common_manifest = read_json(common_dir / "manifest.json", {})
    if not common_manifest:
        return manifest

    manifest.setdefault("steps", {})
    common_steps = common_manifest.get("steps", {}) or {}

    from newsclip_agent.workflow_registry import UNIFIED_SOURCE_COMMON_REUSABLE_STEPS
    for step in UNIFIED_SOURCE_COMMON_REUSABLE_STEPS:
        item = common_steps.get(step)
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        copied["common_progress"] = True
        copied["common_task_id"] = common_task_id
        copied["reused_from"] = relpath(common_dir, ROOT)
        manifest["steps"][step] = copied

    for key in ("source_video", "source_mode", "source_manifest", "source_videos", "current_versions"):
        if common_manifest.get(key):
            if key == "current_versions":
                manifest.setdefault("current_versions", {}).update(common_manifest.get("current_versions") or {})
            else:
                manifest[key] = common_manifest[key]

    if common_manifest.get("status") == "failed":
        manifest["status"] = "failed"
        manifest["user_message"] = common_manifest.get("user_message") or manifest.get("user_message", "")

    manifest["common_task_id"] = common_task_id
    return manifest


def _hydrate_manifest_options(task_dir: Path, manifest: dict[str, Any]) -> None:
    if manifest.get("last_run_options") or manifest.get("last_web_run_options"):
        return
    options: dict[str, Any] = {}
    cut_output = manifest.get("steps", {}).get("cut_plan", {}).get("output")
    if cut_output:
        cut_plan = read_json(task_dir / cut_output, {})
        videos = cut_plan.get("output_videos", [])
        if videos and videos[0].get("aspect_ratio"):
            options["aspect_ratio"] = videos[0]["aspect_ratio"]
    if not options:
        return
    options.setdefault("chunk_seconds", int(WORKFLOW_DEFAULTS.get("default_chunk_seconds", 60)))
    options.setdefault("frame_interval", int(WORKFLOW_DEFAULTS.get("default_frame_interval", 5)))
    options["updated_at"] = datetime.now().isoformat(timespec="seconds")
    manifest["last_run_options"] = options


def _find_latest_draft_video(task_dir: Path) -> Path | None:
    drafts = _list_draft_videos(task_dir)
    return drafts[0] if drafts else None


def _list_draft_videos(task_dir: Path) -> list[Path]:
    render_indexes = sorted(
        list((task_dir / "edit" / "drafts").glob("v*/render_outputs.json"))
        + list((task_dir / "edit" / "reassembly_drafts").glob("v*/reassembly_render_outputs.json")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    ordered: list[Path] = []
    seen: set[Path] = set()
    for index in render_indexes:
        data = read_json(index, {})
        for item in data.get("outputs", []):
            file_value = item.get("file")
            if not file_value:
                continue
            path = (task_dir / file_value).resolve()
            if path.exists() and path.is_file() and path not in seen:
                ordered.append(path)
                seen.add(path)
    candidates = [
        p
        for root in [task_dir / "edit" / "drafts", task_dir / "edit" / "reassembly_drafts"]
        for p in root.rglob("*.mp4")
        if "_clips" not in p.parts and p.is_file()
    ]
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        resolved = path.resolve()
        if resolved not in seen:
            ordered.append(resolved)
            seen.add(resolved)
    return ordered


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in job.items() if k not in {"process", "log_file"}}


def _active_job_status_for_task(task_id: str) -> str:
    statuses = []
    for job_id, job in list(JOBS.items()):
        if job.get("task_id") != task_id:
            continue
        public = _refresh_job(job_id)
        if public.get("status") in {"pending", "running"}:
            statuses.append(public["status"])
    if "running" in statuses:
        return "running"
    if "pending" in statuses:
        return "pending"
    return ""


def _find_active_job_by_task_id(task_id: str) -> dict[str, Any] | None:
    for job_id, job in list(JOBS.items()):
        if job.get("task_id") != task_id:
            continue
        public = _refresh_job(job_id)
        if public.get("status") in {"pending", "running"}:
            return job
    return None


def _is_task_already_completed(manifest: dict[str, Any], production_mode: str) -> bool:
    if not manifest:
        return False
    steps = manifest.get("steps", {})
    final_step = "reassembly_render" if production_mode == "highlight_reassembly" else "render"
    return steps.get(final_step, {}).get("status") in {"success", "partial_success"}


def _task_dir(task_id: str) -> Path:
    task_dir = _safe_child(OUTPUTS_DIR, task_id)
    if not task_dir.exists():
        raise HTTPException(404, "task not found")
    return task_dir


def _safe_child(root: Path, child: str) -> Path:
    root = root.resolve()
    path = (root / child).resolve()
    if root != path and root not in path.parents:
        raise HTTPException(400, "invalid path")
    return path


def _resolve_input_video(input_video: str) -> Path:
    raw = Path(input_video)
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = (ROOT / raw).resolve()
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "input video not found")
    if ROOT != path and ROOT not in path.parents:
        raise HTTPException(400, "input must be inside project directory")
    return path


def _prepare_run_input_video(req: RunRequest) -> Path | None:
    if req.remote_video:
        try:
            cached = download_ucms_video(
                REMOTE_UCMS_CONFIG,
                req.remote_video,
                root_dir=ROOT,
                force=req.force_remote_download,
            )
        except Exception as exc:
            raise HTTPException(502, f"remote video download failed: {exc}") from exc
        req.input_video = str(cached["path"])
    return _resolve_input_video(req.input_video) if req.input_video else None


def _collect_source_items(req: RunRequest) -> list[dict[str, Any]]:
    if req.source_items:
        return req.source_items
    items: list[dict[str, Any]] = []
    if req.input_video:
        items.append({"source_type": "local", "path": req.input_video})
    for path in req.input_videos or []:
        items.append({"source_type": "local", "path": path})
    if req.remote_video:
        items.append({"source_type": "remote_ucms", "remote_video": req.remote_video})
    for remote in req.remote_videos or []:
        items.append({"source_type": "remote_ucms", "remote_video": remote})
    return items


def _write_source_request_for_preview(
    task_dir: Path,
    task_id: str,
    req: RunRequest,
    raw_source_items: list[dict[str, Any]],
    *,
    common_task_id: str = "",
    common_source_key: str = "",
) -> None:
    if not raw_source_items:
        return

    path = task_dir / "input" / "source_request.json"
    ensure_dir(path.parent)
    existing = read_json(path, {})
    if not isinstance(existing, dict):
        existing = {}
    now = datetime.now().isoformat(timespec="seconds")
    payload = {
        **existing,
        "task_id": task_id,
        "production_mode": req.production_mode,
        "source_items": raw_source_items,
        "force_remote_download": req.force_remote_download,
        "output_mode": req.output_mode,
        "max_output_videos": req.max_output_videos,
        "min_output_video_seconds": req.min_output_video_seconds,
        "max_output_video_seconds": req.max_output_video_seconds,
        "aspect_ratio": req.aspect_ratio,
        "chunk_seconds": req.chunk_seconds,
        "frame_interval": req.frame_interval,
        "mode": req.mode,
        "updated_at": now,
    }
    if common_task_id:
        payload["common_task_id"] = common_task_id
    if common_source_key:
        payload["common_source_key"] = common_source_key
    payload.setdefault("created_at", now)
    write_json(path, payload)


def _normalize_output_options(req: RunRequest) -> None:
    if req.output_mode not in {"single", "multiple"}:
        req.output_mode = "single"
    if req.output_mode == "single":
        req.max_output_videos = 1
    else:
        req.max_output_videos = max(2, min(int(req.max_output_videos or 5), 5))
    req.min_output_video_seconds = max(5, int(req.min_output_video_seconds or 30))
    req.max_output_video_seconds = max(req.min_output_video_seconds, int(req.max_output_video_seconds or 90))
    if req.production_mode == "highlight_reassembly":
        req.reassembly_output_mode = "multiple" if req.output_mode == "multiple" else "single"


def _validate_source_request_items(items: list[dict[str, Any]]) -> None:
    for index, item in enumerate(items, start=1):
        source_type = str(item.get("source_type") or item.get("type") or "local")
        if source_type in {"local", "video"}:
            path_value = item.get("path") or item.get("input_video")
            if not path_value:
                raise HTTPException(400, f"source_items[{index}].path is required")
            _resolve_input_video(str(path_value))
        elif source_type in {"remote", "remote_ucms"}:
            remote_video = item.get("remote_video") or item
            if not isinstance(remote_video, dict):
                raise HTTPException(400, f"source_items[{index}].remote_video is required")
        else:
            raise HTTPException(400, f"unsupported source_type: {source_type}")


def _resolve_run_sources(req: RunRequest) -> list[MultiSourceItem]:
    raw_items = _collect_source_items(req)
    if not raw_items:
        return []

    resolved: list[MultiSourceItem] = []
    for index, item in enumerate(raw_items, start=1):
        source_type = str(item.get("source_type") or item.get("type") or "local")
        if source_type in {"local", "video"}:
            path_value = item.get("path") or item.get("input_video")
            if not path_value:
                raise HTTPException(400, f"source_items[{index}].path is required")
            path = _resolve_input_video(str(path_value))
            resolved.append(
                MultiSourceItem(
                    source_id=str(item.get("source_id") or f"src_{index:03d}"),
                    source_type="local",
                    path=path,
                    display_name=str(item.get("display_name") or path.name),
                )
            )
            continue

        if source_type in {"remote", "remote_ucms"}:
            remote_video = item.get("remote_video") or item
            if not isinstance(remote_video, dict):
                raise HTTPException(400, f"source_items[{index}].remote_video is required")
            try:
                cached = download_ucms_video(
                    REMOTE_UCMS_CONFIG,
                    remote_video,
                    root_dir=ROOT,
                    force=req.force_remote_download,
                )
            except Exception as exc:
                raise HTTPException(502, f"remote video download failed: {exc}") from exc
            path = _resolve_input_video(str(cached["path"]))
            resolved.append(
                MultiSourceItem(
                    source_id=str(item.get("source_id") or remote_video.get("remote_id") or remote_video.get("id") or f"src_{index:03d}"),
                    source_type="remote_ucms",
                    path=path,
                    display_name=str(remote_video.get("display_name") or remote_video.get("name") or path.name),
                    remote=remote_video,
                )
            )
            continue

        raise HTTPException(400, f"unsupported source_type: {source_type}")

    return resolved


def _display_input_name(req: RunRequest) -> str | None:
    if req.remote_video:
        return str(
            req.remote_video.get("name")
            or req.remote_video.get("remote_id")
            or req.remote_video.get("id")
            or "remote_video"
        )
    return req.input_video


def _safe_filename(name: str) -> str:
    filename = Path(name).name.strip()
    if not filename:
        return f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    return "".join(ch if ch not in '<>:"/\\|?*' and ord(ch) >= 32 else "_" for ch in filename)


def _unique_video_path(filename: str) -> Path:
    path = _safe_child(VIDEOS_DIR, filename)
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for index in range(1, 1000):
        candidate = _safe_child(VIDEOS_DIR, f"{stem}_{stamp}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise HTTPException(409, "could not create unique filename")


def _resolve_task_source(task_dir: Path, source: str) -> Path:
    raw = Path(source)
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = (task_dir / raw).resolve()
        if not path.exists():
            path = (ROOT / raw).resolve()
    root = ROOT.resolve()
    task_root = task_dir.resolve()
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "source video not found")
    if root not in path.parents and task_root not in path.parents and path != root and path != task_root:
        raise HTTPException(400, "invalid source path")
    return path


def _source_request_preview_items(task_dir: Path, task_id: str) -> list[dict[str, Any]]:
    request = read_json(task_dir / "input" / "source_request.json", {})
    items = request.get("source_items") or []
    previews: list[dict[str, Any]] = []

    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        source_type = str(item.get("source_type") or item.get("type") or "local")
        if source_type in {"local", "video"}:
            path_value = item.get("path") or item.get("input_video")
            if not path_value:
                continue
            try:
                path = _resolve_input_video(str(path_value))
            except HTTPException:
                continue
            file_ref = relpath(path, ROOT)
            previews.append(
                {
                    "label": item.get("display_name") or path.name,
                    "file": file_ref,
                    "url": f"/api/videos/preview?path={file_ref}",
                    "source_index": index,
                    "source_type": "local",
                }
            )
            continue
        if source_type in {"remote", "remote_ucms"}:
            remote = item.get("remote_video") or item
            if not isinstance(remote, dict):
                continue
            url = (
                remote.get("preview_url")
                or remote.get("media_low_url")
                or remote.get("mediaLow")
                or remote.get("media_high_url")
                or remote.get("mediaHigh")
                or remote.get("download_url")
            )
            if not url:
                continue
            previews.append(
                {
                    "label": item.get("display_name")
                    or remote.get("display_name")
                    or remote.get("name")
                    or remote.get("title")
                    or f"远程素材 {index}",
                    "file": remote.get("name") or remote.get("remote_id") or remote.get("id") or "",
                    "url": url,
                    "source_index": index,
                    "source_type": "remote_ucms",
                }
            )

    return previews


def _existing_task_source_request(task_dir: Path) -> dict[str, Any]:
    request_path = task_dir / "input" / "source_request.json"
    if not request_path.exists() or not request_path.is_file():
        return {}
    data = read_json(request_path, {})
    return data if isinstance(data, dict) else {}


def _valid_source_request_items(source_request: dict[str, Any]) -> list[dict[str, Any]]:
    items = source_request.get("source_items") or []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _prepare_mode_switched_run(req: RunRequest) -> None:
    if _collect_source_items(req) or req.rerun or req.rerun_from or not req.task_id:
        return

    source_task_id = req.task_id.strip()
    if not source_task_id:
        return

    source_task_dir = _task_dir(source_task_id)
    source_manifest = read_json(source_task_dir / "manifest.json", {})

    current_mode = _task_id_production_mode(source_task_id) or _manifest_production_mode(source_manifest)
    if current_mode == req.production_mode:
        return

    source_request = _existing_task_source_request(source_task_dir)
    source_items = _valid_source_request_items(source_request)

    if source_items:
        req.source_items = source_items
        req.input_video = None
        req.input_videos = []
        req.remote_video = None
        req.remote_videos = []

        if source_request.get("aspect_ratio"):
            req.aspect_ratio = str(source_request.get("aspect_ratio"))
        if source_request.get("chunk_seconds"):
            req.chunk_seconds = int(source_request.get("chunk_seconds"))
        if source_request.get("frame_interval"):
            req.frame_interval = int(source_request.get("frame_interval"))
        if source_request.get("mode"):
            req.mode = str(source_request.get("mode"))

        req.force_remote_download = bool(source_request.get("force_remote_download", req.force_remote_download))
        req.task_id = _with_mode_suffix(source_task_id, req.production_mode)
        req.reuse_from_task_id = source_task_id
        return

    req.input_video = str(_existing_task_source_video(source_task_dir, source_manifest))
    req.task_id = _with_mode_suffix(source_task_id, req.production_mode)
    req.reuse_from_task_id = source_task_id


def _task_id_production_mode(task_id: str) -> str | None:
    if any(alias in task_id for alias in _mode_aliases("highlight_reassembly")):
        return "highlight_reassembly"
    if any(alias in task_id for alias in _mode_aliases("ai_voiceover")):
        return "ai_voiceover"
    return None


def _manifest_production_mode(manifest: dict[str, Any]) -> str:
    for key in ("last_web_run_options", "last_run_options"):
        mode = (manifest.get(key) or {}).get("production_mode")
        if mode in {"ai_voiceover", "highlight_reassembly"}:
            return mode
    return "ai_voiceover"


def _existing_task_source_video(task_dir: Path, manifest: dict[str, Any]) -> Path:
    task = read_json(task_dir / "task.json", {})
    original = task.get("source_video_original")
    if original and Path(original).exists():
        return Path(original).resolve()
    source = manifest.get("source_video")
    if not source:
        raise HTTPException(
            400,
            "已有任务缺少源视频，并且没有 input/source_request.json，无法从该任务切换生产模式。请重新选择原始素材后再启动目标模式。"
        )
    return _resolve_task_source(task_dir, source)


def _resolve_run_task_id(req: RunRequest) -> str:
    task_id = (req.task_id or "").strip()
    if req.input_video:
        if not task_id:
            return _make_task_id(req.input_video, req.production_mode)
        return _with_mode_suffix(task_id, req.production_mode)
    return task_id or _make_task_id(req.input_video, req.production_mode)


def _make_task_id(input_video: str | None, production_mode: str = "ai_voiceover") -> str:
    prefix = "task"
    if input_video:
        name = Path(input_video).stem
        safe = "".join(ch if ch.isalnum() else "_" for ch in name).strip("_")
        prefix = safe[:24] or "task"
    return f"{prefix}_{_mode_slug(production_mode)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _video_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime": round(stat.st_mtime, 3),
    }


def _run_fingerprint(req: RunRequest, input_path: Path | None) -> str:
    payload = {
        "video": _video_fingerprint(input_path) if input_path else None,
        "remote": _remote_video_fingerprint(req.remote_video),
        "production_mode": req.production_mode,
        "output_mode": req.output_mode,
        "max_output_videos": req.max_output_videos,
        "aspect_ratio": req.aspect_ratio,
        "chunk_seconds": req.chunk_seconds,
        "frame_interval": req.frame_interval,
        "target_duration_seconds": req.target_duration_seconds,
        "reassembly_target_seconds": req.reassembly_target_seconds,
        "reassembly_max_clip_count": req.reassembly_max_clip_count,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _multi_source_fingerprint(req: RunRequest, sources: list[MultiSourceItem]) -> str:
    payload = {
        "sources": [
            {
                "source_id": item.source_id,
                "source_type": item.source_type,
                "path": str(item.path.resolve()),
                "size": item.path.stat().st_size,
                "mtime": round(item.path.stat().st_mtime, 3),
                "remote": _remote_video_fingerprint(item.remote),
            }
            for item in sources
        ],
        "production_mode": req.production_mode,
        "output_mode": req.output_mode,
        "max_output_videos": req.max_output_videos,
        "aspect_ratio": req.aspect_ratio,
        "chunk_seconds": req.chunk_seconds,
        "frame_interval": req.frame_interval,
        "target_duration_seconds": req.target_duration_seconds,
        "reassembly_target_seconds": req.reassembly_target_seconds,
        "reassembly_max_clip_count": req.reassembly_max_clip_count,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _display_multi_source_name(sources: list[MultiSourceItem]) -> str:
    first = sources[0].display_name or sources[0].path.stem
    return f"multi_{len(sources)}_{first}"


def _source_request_fingerprint(req: RunRequest, items: list[dict[str, Any]]) -> str:
    payload_items = []
    for index, item in enumerate(items, start=1):
        source_type = str(item.get("source_type") or item.get("type") or "local")
        if source_type in {"local", "video"}:
            path = _resolve_input_video(str(item.get("path") or item.get("input_video")))
            payload_items.append({"order": index, "source_type": "local", **_video_fingerprint(path)})
        else:
            remote_video = item.get("remote_video") or item
            payload_items.append({"order": index, "source_type": "remote_ucms", "remote": _remote_video_fingerprint(remote_video)})
    payload = {
        "sources": payload_items,
        "production_mode": req.production_mode,
        "output_mode": req.output_mode,
        "max_output_videos": req.max_output_videos,
        "aspect_ratio": req.aspect_ratio,
        "chunk_seconds": req.chunk_seconds,
        "frame_interval": req.frame_interval,
        "target_duration_seconds": req.target_duration_seconds,
        "reassembly_target_seconds": req.reassembly_target_seconds,
        "reassembly_max_clip_count": req.reassembly_max_clip_count,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _common_source_key(req: RunRequest, items: list[dict[str, Any]]) -> str:
    payload_items: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        source_type = str(item.get("source_type") or item.get("type") or "local")
        if source_type in {"local", "video"}:
            path_value = item.get("path") or item.get("input_video")
            path = _resolve_input_video(str(path_value))
            payload_items.append({"order": index, "source_type": "local", **_video_fingerprint(path)})
            continue
        if source_type in {"remote", "remote_ucms"}:
            remote_video = item.get("remote_video") or item
            payload_items.append({"order": index, "source_type": "remote_ucms", "remote": _remote_video_fingerprint(remote_video)})
            continue
        payload_items.append({"order": index, "source_type": source_type, "raw": item})
    payload = {
        "sources": payload_items,
        "aspect_ratio": req.aspect_ratio,
        "chunk_seconds": req.chunk_seconds,
        "frame_interval": req.frame_interval,
        "mode": req.mode,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _display_source_request_name(items: list[dict[str, Any]]) -> str:
    first = items[0] if items else {}
    remote = first.get("remote_video") if isinstance(first.get("remote_video"), dict) else first
    name = first.get("display_name") or first.get("path") or remote.get("name") or remote.get("id") or "source"
    return f"multi_{len(items)}_{Path(str(name)).stem}"


def _remote_video_fingerprint(remote_video: dict[str, Any] | None) -> dict[str, Any] | None:
    if not remote_video:
        return None
    return {
        "source_type": remote_video.get("source_type") or "remote_ucms",
        "id": remote_video.get("id") or remote_video.get("remote_id"),
        "guid": remote_video.get("guid", ""),
        "name": remote_video.get("name", ""),
        "record_station": remote_video.get("record_station") or remote_video.get("recordStation", ""),
        "download_url": remote_video.get("download_url")
        or remote_video.get("media_high_url")
        or remote_video.get("mediaHigh", ""),
    }


def _make_task_id_from_fingerprint(input_video: str | None, production_mode: str, fingerprint: str) -> str:
    prefix = "task"
    if input_video:
        name = Path(input_video).stem
        safe = "".join(ch if ch.isalnum() else "_" for ch in name).strip("_")
        prefix = safe[:24] or "task"
    return f"{prefix}_{_mode_slug(production_mode)}_{fingerprint}"


def _normalize_new_task_id(task_id: str | None, input_name: str | None, production_mode: str, fingerprint: str) -> str:
    task_id = (task_id or "").strip()
    if not task_id:
        return _make_task_id_from_fingerprint(input_name, production_mode, fingerprint)
    return _with_mode_suffix(task_id, production_mode)


def _with_mode_suffix(task_id: str, production_mode: str) -> str:
    mode = _mode_slug(production_mode)
    desired_aliases = _mode_aliases(production_mode)
    if any(f"_{alias}_" in task_id or task_id.endswith(f"_{alias}") for alias in desired_aliases):
        return task_id

    for alias in _all_mode_aliases():
        marker = f"_{alias}_"
        if marker in task_id:
            return task_id.replace(marker, f"_{mode}_", 1)
        suffix = f"_{alias}"
        if task_id.endswith(suffix):
            return f"{task_id[: -len(suffix)]}_{mode}"

    parts = task_id.rsplit("_", 2)
    if len(parts) == 3 and len(parts[1]) == 8 and len(parts[2]) in {4, 6} and parts[1].isdigit() and parts[2].isdigit():
        time_part = parts[2] if len(parts[2]) == 6 else f"{parts[2]}{datetime.now().strftime('%S')}"
        return f"{parts[0]}_{mode}_{parts[1]}_{time_part}"
    return f"{task_id}_{mode}"


def _mode_slug(production_mode: str) -> str:
    return "视频重组" if production_mode == "highlight_reassembly" else "AI配音解说"


def _mode_aliases(production_mode: str) -> tuple[str, ...]:
    if production_mode == "highlight_reassembly":
        return ("highlight_reassembly", "视频重组")
    return ("ai_voiceover", "AI配音解说")


def _all_mode_aliases() -> tuple[str, ...]:
    return _mode_aliases("ai_voiceover") + _mode_aliases("highlight_reassembly")
