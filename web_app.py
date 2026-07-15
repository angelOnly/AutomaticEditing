from __future__ import annotations

import os
import hashlib
import json
import re
import shutil
import signal
import subprocess
import sys
import uuid
import stat
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
from PIL import Image
from pydantic import BaseModel, Field

from newsclip_agent.config import load_config
from newsclip_agent.multisource import MultiSourceItem, build_multi_source_video
from newsclip_agent.remote_ucms import (
    count_ucms_videos,
    download_ucms_video,
    list_ucms_videos,
    load_remote_ucms_config,
    remote_cache_path,
    public_config as remote_ucms_public_config,
)
from newsclip_agent.utils import ensure_dir, ffprobe_json, read_json, relpath, seconds_to_timecode, write_json
from newsclip_agent.common_cache import build_common_analysis_profile, build_common_source_key
from newsclip_agent.tts_omnivoice import generate_omnivoice_audio
from newsclip_agent.job_store import JobStore
from newsclip_agent import commentary
from newsclip_agent import ad_detection
from newsclip_agent.cover_service import (
    CoverService,
    CoverServiceError,
    compose_cover,
    public_provider_config,
    validate_font_file,
)
from newsclip_agent.manual_editor import (
    ManualEditorError,
    ProjectConflictError,
    ProjectValidationError,
    add_asset,
    apply_frontend_project,
    initialize_project,
    load_project,
    save_project_revision,
    to_frontend_project,
)


ROOT = Path(__file__).resolve().parent
OUTPUTS_DIR = ROOT / "outputs"
COMMON_OUTPUTS_DIR = OUTPUTS_DIR / "__common__"
VIDEOS_DIR = ROOT / "videos"
STATIC_DIR = ROOT / "web_static"
RUNNER = ROOT / "run_pipeline.py"
MULTI_SOURCE_RUNNER = ROOT / "run_multisource_pipeline.py"
MANUAL_EDITOR_RUNNER = ROOT / "run_manual_editor.py"

app = FastAPI(title="凤凰新闻视频智能拆条工作台")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

JOBS: dict[str, dict[str, Any]] = {}
JOB_LOCK = RLock()
EDITOR_PROJECT_LOCK = RLock()
PENDING_JOB_IDS: deque[str] = deque()
JOB_STORE = JobStore(OUTPUTS_DIR / ".jobs")
for job_id, job in JOB_STORE.load_jobs().items():
    JOBS[job_id] = job
    if job.get("status") == "pending":
        PENDING_JOB_IDS.append(job_id)
PROJECT_CONFIG = load_config(ROOT / "config.toml")
COVER_SERVICE = CoverService(PROJECT_CONFIG)
COVER_CONFIG = COVER_SERVICE.config
COVER_DEFAULT_ASPECT_RATIO = COVER_CONFIG.allowed_ratios[0]
COVER_DEFAULT_SIZE = COVER_CONFIG.allowed_sizes[0]
COVER_DEFAULT_RESTORE_SIZE = COVER_CONFIG.allowed_sizes[-1]
COVER_DEFAULT_SAFE_AREA = COVER_CONFIG.allowed_safe_areas[0]
WORKFLOW_DEFAULTS = PROJECT_CONFIG.workflow
SHORT_VIDEO_DEFAULTS = PROJECT_CONFIG.short_video
VOICEOVER_DEFAULTS = PROJECT_CONFIG.voiceover
REMOTE_UCMS_CONFIG = load_remote_ucms_config(PROJECT_CONFIG)
REMOTE_STATION_COUNTS: dict[str, int] = {}
REMOTE_STATION_COUNT_ERRORS: dict[str, str] = {}
REMOTE_STATION_COUNT_LOCK = RLock()
# Background prefetch of remote sources into the shared download cache so 原片
# preview can fall back to a local copy once the remote signed URL has expired.
REMOTE_PREFETCH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="remote-prefetch")
REMOTE_PREFETCH_INFLIGHT: set[str] = set()
REMOTE_PREFETCH_LOCK = RLock()
WEB_CONCURRENCY = PROJECT_CONFIG.raw.get("web_concurrency", {})
MAX_RUNNING_JOBS = int(os.environ.get("WEB_MAX_RUNNING_JOBS", WEB_CONCURRENCY.get("max_running_jobs", 2)))
MAX_PENDING_JOBS = int(os.environ.get("WEB_MAX_PENDING_JOBS", WEB_CONCURRENCY.get("max_pending_jobs", 20)))
# 活跃任务（pending+running）总数上限，超过即拒绝创建新任务，避免一次性堆出大量任务把机器/Web 拖垮。
MAX_ACTIVE_JOBS = int(os.environ.get("WEB_MAX_ACTIVE_JOBS", WEB_CONCURRENCY.get("max_active_jobs", 4)))
SAME_TASK_POLICY = str(WEB_CONCURRENCY.get("same_task_policy", "reject"))


from newsclip_agent.workflow_registry import (
    step_order,
    common_reusable_steps,
    common_progress_steps,
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


def _common_progress_steps_for_request(
    *,
    source_count: int,
    use_unified_source_pipeline: bool,
) -> list[str]:
    unified = bool(use_unified_source_pipeline or source_count > 1)
    return common_progress_steps(unified=unified)


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
        _common_progress_steps_for_request(
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
    target_duration_mode: str = str(SHORT_VIDEO_DEFAULTS.get("ai_voiceover_target_mode", "soft"))
    output_mode: str = "single"
    max_output_videos: int = 1
    min_output_video_seconds: int = 30
    max_output_video_seconds: int = int(
        SHORT_VIDEO_DEFAULTS.get(
            "ai_voiceover_max_output_seconds",
            SHORT_VIDEO_DEFAULTS.get("max_long_video_seconds", 180),
        )
    )
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
    target_column: str | None = None
    reuse_from_task_id: str | None = None
    client_id: str | None = None


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(
        (STATIC_DIR / "index.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/editor", response_class=HTMLResponse)
def editor_page() -> HTMLResponse:
    return HTMLResponse(
        (STATIC_DIR / "editor.html").read_text(encoding="utf-8"),
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
            "max_long_video_seconds": int(SHORT_VIDEO_DEFAULTS.get("max_long_video_seconds", 180)),
            "ai_voiceover_target_mode": str(SHORT_VIDEO_DEFAULTS.get("ai_voiceover_target_mode", "soft")),
            "ai_voiceover_max_output_seconds": int(
                SHORT_VIDEO_DEFAULTS.get(
                    "ai_voiceover_max_output_seconds",
                    SHORT_VIDEO_DEFAULTS.get("max_long_video_seconds", 180),
                )
            ),
        },
        "voiceover": {
            "tts_required_by_default": bool(VOICEOVER_DEFAULTS.get("tts_required_by_default", True)),
            "allow_original_audio_evidence": bool(VOICEOVER_DEFAULTS.get("allow_original_audio_evidence", False)),
            "ai_voiceover_min_ratio": float(VOICEOVER_DEFAULTS.get("ai_voiceover_min_ratio", 0.8)),
            "duration_mismatch_block_threshold_seconds": float(
                VOICEOVER_DEFAULTS.get("duration_mismatch_block_threshold_seconds", 1.0)
            ),
            "ai_voiceover_compact_to_tts": bool(VOICEOVER_DEFAULTS.get("ai_voiceover_compact_to_tts", True)),
            "ai_voiceover_mismatch_policy": str(VOICEOVER_DEFAULTS.get("ai_voiceover_mismatch_policy", "block")),
            "default_voice_id": str(PROJECT_CONFIG.omnivoice.get("default_voice_id", "")),
        },
        "voices": _public_voice_catalog(),
        "remote_ucms": remote_ucms_public_config(
            REMOTE_UCMS_CONFIG,
            station_counts=_remote_station_counts_snapshot(),
            station_count_errors=_remote_station_count_errors_snapshot(),
        ),
        "full_concat": {
            "enabled": bool((PROJECT_CONFIG.raw.get("full_concat", {}) or {}).get("enabled", True)),
            "program_columns": ad_detection.schedule_columns(ad_detection.load_schedule(PROJECT_CONFIG)),
        },
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
    start_time: str | None = None,
    end_time: str | None = None,
) -> dict[str, Any]:
    try:
        result = list_ucms_videos(
            REMOTE_UCMS_CONFIG,
            record_station=record_station,
            current=current,
            page_size=page_size,
            keyword=keyword,
            sort_order=sort_order,
            start_time=start_time,
            end_time=end_time,
        )
        station = record_station or REMOTE_UCMS_CONFIG.default_record_station
        total = result.get("pagination", {}).get("total")
        # 仅在无过滤条件时刷新信号源总数缓存：带 keyword/时间区间时 total 是过滤后的子集数，
        # 用它覆盖会让信号源旁边的数量徽标显示成搜索结果数。
        is_filtered = bool((keyword or "").strip() or (start_time or "").strip() or (end_time or "").strip())
        if station and total is not None and not is_filtered:
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


def _remote_local_preview_url(remote_video: dict[str, Any]) -> str:
    """Return a local preview URL when the remote source is already cached on disk, else ""."""
    if not REMOTE_UCMS_CONFIG.enabled or not isinstance(remote_video, dict):
        return ""
    try:
        cached = remote_cache_path(REMOTE_UCMS_CONFIG, remote_video)
    except Exception:
        return ""
    try:
        if cached.exists() and cached.is_file() and cached.stat().st_size > 0:
            return f"/api/videos/preview?path={quote(relpath(cached, ROOT), safe='/')}"
    except OSError:
        return ""
    return ""


def _prefetch_remote_source(remote_video: dict[str, Any]) -> None:
    """Best-effort background download of a single remote source into the shared cache."""
    if not REMOTE_UCMS_CONFIG.enabled or not isinstance(remote_video, dict):
        return
    try:
        key = str(remote_cache_path(REMOTE_UCMS_CONFIG, remote_video))
    except Exception:
        return
    with REMOTE_PREFETCH_LOCK:
        if key in REMOTE_PREFETCH_INFLIGHT:
            return
        try:
            cached = Path(key)
            if cached.exists() and cached.stat().st_size > 0:
                return
        except OSError:
            pass
        REMOTE_PREFETCH_INFLIGHT.add(key)

    def _worker() -> None:
        try:
            download_ucms_video(REMOTE_UCMS_CONFIG, remote_video, root_dir=ROOT, force=False)
        except Exception as exc:  # noqa: BLE001 - prefetch is best-effort
            print(f"[remote-prefetch] download failed: {exc}", file=sys.stderr)
        finally:
            with REMOTE_PREFETCH_LOCK:
                REMOTE_PREFETCH_INFLIGHT.discard(key)

    REMOTE_PREFETCH_POOL.submit(_worker)


def _prefetch_remote_sources(raw_source_items: list[dict[str, Any]]) -> None:
    """Kick off background downloads for every remote item in a run request."""
    for item in raw_source_items or []:
        if not isinstance(item, dict):
            continue
        source_type = str(item.get("source_type") or item.get("type") or "local")
        if source_type not in {"remote", "remote_ucms"}:
            continue
        remote_video = item.get("remote_video") or item
        if isinstance(remote_video, dict):
            _prefetch_remote_source(remote_video)


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
    task_dir = _task_dir(task_id)
    with JOB_LOCK:
        matching_job_ids = [
            job_id
            for job_id, job in list(JOBS.items())
            if job.get("task_id") == task_id
        ]
        for job_id in matching_job_ids:
            if _refresh_job(job_id).get("status") in {"pending", "running"}:
                raise HTTPException(409, "task is running")
        for job_id in matching_job_ids:
            JOBS.pop(job_id, None)
            try:
                PENDING_JOB_IDS.remove(job_id)
            except ValueError:
                pass
            JOB_STORE.delete_job(job_id)
        # 兼容旧版本按 task_id 持久化的单文件。
        JOB_STORE.delete_job(task_id)
    try:
        _rmtree_task_dir(task_dir)
    except PermissionError as exc:
        raise HTTPException(409, f"task files are in use, close previews or logs and retry: {exc}") from exc
    except OSError as exc:
        raise HTTPException(500, f"failed to delete task files: {exc}") from exc
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
    if suffix in {".mp4", ".mov", ".mkv", ".wav", ".mp3", ".jpg", ".jpeg", ".png", ".webp", ".ttf", ".otf"}:
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
            # Prefer the cached local copy over the expiring remote signed URL.
            url = _remote_local_preview_url(remote) or (
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
                "local_time_range": f"00:00:00.000-{seconds_to_timecode(float(item.get('duration_seconds') or 0), ms=True)}",
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
            "type": (
                "manual_editor_export" if "manual_editor" in path.parts and "exports" in path.parts
                else "full_concat_draft" if "full_concat_drafts" in path.parts
                else "highlight_reassembly_draft" if "reassembly_drafts" in path.parts
                else "ai_voiceover_draft"
            ),
            "size_mb": round(path.stat().st_size / 1024 / 1024, 2),
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        }
        for path in drafts
    ]


@app.get("/api/tasks/{task_id}/commentary")
def get_task_commentary(task_id: str, reassembly_id: str | None = None, refresh: int = 0) -> dict[str, Any]:
    task_dir = _task_dir(task_id)
    manifest = read_json(task_dir / "manifest.json", {})
    mode = (
        manifest.get("production_mode")
        or _task_id_production_mode(task_id)
        or _manifest_production_mode(manifest)
    )
    if mode != "highlight_reassembly":
        raise HTTPException(400, "该任务不是视频重组任务，没有图文解说")

    rids = commentary.renderable_rids(task_dir, manifest)
    if not rids:
        raise HTTPException(404, "暂无可解说的成片，请先完成视频重组")
    rid = reassembly_id or rids[0]
    if rid not in rids:
        raise HTTPException(404, f"找不到成片 {rid}")

    artifact = None if refresh else commentary.load_latest_commentary(task_dir, rid)
    if artifact is None:
        try:
            commentary.generate_for_task(PROJECT_CONFIG, task_dir, reassembly_id=rid)
        except commentary.CommentaryError as exc:
            raise HTTPException(503, str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"图文解说生成失败：{exc}")
        artifact = commentary.load_latest_commentary(task_dir, rid)
    if artifact is None:
        raise HTTPException(503, "图文解说生成失败，请稍后重试")

    video_file = (artifact.get("video") or {}).get("file")
    if video_file:
        video_path = _safe_child(task_dir, video_file)
        if video_path.exists() and video_path.is_file():
            artifact.setdefault("video", {})["url"] = _task_file_url(task_id, task_dir, video_path)
    artifact["available_reassembly_ids"] = rids
    return artifact


@app.get("/api/tasks/{task_id}/ad-report")
def get_task_ad_report(task_id: str) -> dict[str, Any]:
    """完整版·去广告的移除报告：被切掉的广告块清单 + 统计 + 成片可播放地址。"""
    task_dir = _task_dir(task_id)
    manifest = read_json(task_dir / "manifest.json", {})
    mode = (
        manifest.get("production_mode")
        or _task_id_production_mode(task_id)
        or _manifest_production_mode(manifest)
    )
    if mode != "full_concat":
        raise HTTPException(400, "该任务不是完整版去广告任务，没有广告报告")

    steps = manifest.get("steps", {})
    render_out = (steps.get("full_concat_render", {}) or {}).get("output")
    data = read_json(task_dir / render_out, {}) if render_out else {}
    if not data:
        plan_out = (steps.get("full_concat_plan", {}) or {}).get("output")
        plan = read_json(task_dir / plan_out, {}) if plan_out else {}
        data = {
            "outputs": [],
            "removed_blocks": plan.get("removed_blocks", []),
            "ad_stats": plan.get("ad_stats", {}),
            "target_column": plan.get("target_column", ""),
            "target_column_source": plan.get("target_column_source", ""),
            "vip": plan.get("vip", {}),
        }

    videos = []
    for item in data.get("outputs", []) or []:
        file_rel = item.get("file")
        url = None
        if file_rel:
            video_path = _safe_child(task_dir, file_rel)
            if video_path.exists() and video_path.is_file():
                url = _task_file_url(task_id, task_dir, video_path)
        videos.append({
            "reassembly_id": item.get("reassembly_id"),
            "file": file_rel,
            "url": url,
            "quality_check": item.get("quality_check", {}),
            "publishable": bool(data.get("publishable", item.get("publishable", False))),
            "quarantined": bool(data.get("quarantined", item.get("quarantined", False))),
        })

    qc_step = (steps.get("full_concat_output_qc", {}) or {})
    qc_doc = read_json(task_dir / qc_step.get("output", ""), {}) if qc_step.get("output") else {}

    return {
        "task_id": task_id,
        "production_mode": mode,
        "target_column": data.get("target_column", ""),
        "target_column_source": data.get("target_column_source", ""),
        "vip": data.get("vip", {}),
        "stats": data.get("ad_stats", {}),
        "removed_blocks": data.get("removed_blocks", []),
        "videos": videos,
        "boundary_refinement": _manifest_step_json(task_dir, steps, "full_concat_boundary_refine"),
        "plan_qc": _manifest_step_json(task_dir, steps, "full_concat_plan_qc"),
        "output_qc": qc_doc,
        "publishable": bool(qc_doc.get("publishable", data.get("publishable", False))),
        "quarantined": bool(qc_doc.get("quarantined", data.get("quarantined", False))),
    }


def _manifest_step_json(task_dir: Path, steps: dict[str, Any], step: str) -> dict[str, Any]:
    output = (steps.get(step, {}) or {}).get("output")
    return read_json(task_dir / output, {}) if output else {}


def _vip_store_path() -> Path:
    raw = (PROJECT_CONFIG.raw.get("vip_persons", {}) or {})
    store = raw.get("store_file") or "data/vip_persons.json"
    p = Path(store)
    return p if p.is_absolute() else ROOT / p


@app.get("/api/vip-persons")
def get_vip_persons() -> dict[str, Any]:
    """返回当前生效的重点人物名单（store_file 优先，否则 config 打底）+ 打底名单。"""
    raw = (PROJECT_CONFIG.raw.get("vip_persons", {}) or {})
    baseline = [str(x).strip() for x in (raw.get("names") or ad_detection.DEFAULT_VIP_NAMES) if str(x).strip()]
    return {
        "names": ad_detection.load_vip_names(PROJECT_CONFIG),
        "baseline": baseline,
        "store_file": relpath(_vip_store_path(), ROOT),
    }


class VipPersonsRequest(BaseModel):
    names: list[str]


@app.put("/api/vip-persons")
def put_vip_persons(req: VipPersonsRequest) -> dict[str, Any]:
    """前端在线编辑重点人物名单，写入 store_file，运行时优先读取。"""
    names: list[str] = []
    for raw_name in req.names or []:
        name = str(raw_name).strip()
        if name and name not in names:
            names.append(name)
    write_json(_vip_store_path(), {"names": names, "updated_at": datetime.now().isoformat(timespec="seconds")})
    return {"names": names, "store_file": relpath(_vip_store_path(), ROOT)}


class CoverPromptRequest(BaseModel):
    title: str
    summary: str = ""
    requirements: str = ""
    aspect_ratio: str = COVER_DEFAULT_ASPECT_RATIO
    size: str = COVER_DEFAULT_SIZE
    safe_area: str = COVER_DEFAULT_SAFE_AREA


class CoverGenerateRequest(BaseModel):
    prompt_doc: dict[str, Any]
    max_images: int = 1
    watermark: bool = False
    reference_file: str | None = None
    edit_instruction: str = ""


class CoverComposeRequest(BaseModel):
    source_file: str
    width: int = 1920
    height: int = 1080
    crop: dict[str, Any] = Field(default_factory=dict)
    text_layers: list[dict[str, Any]] = Field(default_factory=list)
    enhance: str = "standard"
    output_format: str = "png"


class CoverRestoreRequest(BaseModel):
    source_file: str
    requirements: str = ""
    size: str = COVER_DEFAULT_RESTORE_SIZE


class RemoteEditorAssetRequest(BaseModel):
    remote_video: dict[str, Any]
    force_remote_download: bool = False


def _full_concat_editor_task(task_id: str) -> tuple[Path, dict[str, Any]]:
    task_dir = _task_dir(task_id)
    manifest = read_json(task_dir / "manifest.json", {})
    mode = (
        manifest.get("production_mode")
        or _task_id_production_mode(task_id)
        or _manifest_production_mode(manifest)
    )
    if mode != "full_concat":
        raise HTTPException(400, "当前仅支持编辑完整版任务")
    return task_dir, manifest


def _manual_editor_root(task_dir: Path) -> Path:
    return task_dir / "edit" / "manual_editor"


def _full_concat_plan_path(task_dir: Path, manifest: dict[str, Any]) -> Path:
    candidates: list[Path] = []
    step = (manifest.get("steps") or {}).get("full_concat_plan") or {}
    for value in [step.get("output"), *(step.get("output_files") or [])]:
        if not value:
            continue
        try:
            candidate = _safe_child(task_dir, str(value))
        except HTTPException:
            continue
        if candidate.name == "full_concat_cut_plan.json":
            candidates.append(candidate)

    version = str((manifest.get("current_versions") or {}).get("full_concat_plan") or "").strip()
    if version:
        candidates.append(task_dir / "edit" / "full_concat_cut_plan" / version / "full_concat_cut_plan.json")
    candidates.extend((task_dir / "edit" / "full_concat_cut_plan").glob("v*/full_concat_cut_plan.json"))
    existing = [path.resolve() for path in candidates if path.is_file()]
    if not existing:
        raise HTTPException(409, "完整版剪辑计划尚未生成，暂时不能进入精剪")
    return max(existing, key=lambda path: path.stat().st_mtime_ns)


def _fill_editor_asset_durations(project: dict[str, Any]) -> bool:
    changed = False
    for asset in project.get("assets") or []:
        try:
            current_duration = float(asset.get("duration_seconds") or 0)
        except (TypeError, ValueError):
            current_duration = 0.0
        if current_duration > 0:
            continue
        path = Path(str(asset.get("path") or "")).expanduser().resolve()
        if not path.is_file():
            raise ManualEditorError(f"素材文件不存在：{asset.get('display_name') or asset.get('asset_id')}")
        try:
            duration = float(ffprobe_json(path).get("duration_seconds") or 0)
        except Exception as exc:  # noqa: BLE001 - normalize ffprobe failures for the editor boundary
            raise ManualEditorError(f"无法读取素材时长：{path.name}（{exc}）") from exc
        if duration <= 0:
            raise ManualEditorError(f"素材没有有效视频时长：{path.name}")
        asset["duration_seconds"] = round(duration, 6)
        changed = True
    return changed


def _load_or_initialize_editor_project(task_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    editor_root = _manual_editor_root(task_dir)
    project_path = editor_root / "project.json"
    with EDITOR_PROJECT_LOCK:
        if project_path.is_file():
            project = load_project(project_path)
            if _fill_editor_asset_durations(project):
                project["revision"] = int(project.get("revision", 0)) + 1
                project["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                save_project_revision(project, editor_root)
            return project

        plan_path = _full_concat_plan_path(task_dir, manifest)
        plan = read_json(plan_path, {})
        videos = plan.get("output_videos") or []
        reassembly_id = ""
        if len(videos) > 1 and isinstance(videos[0], dict):
            reassembly_id = str(videos[0].get("reassembly_id") or "")
        project = initialize_project(
            plan_path,
            reassembly_id=reassembly_id or None,
            task_root=task_dir,
        )
        _fill_editor_asset_durations(project)
        save_project_revision(project, editor_root)
        return project


def _editor_asset_url(task_id: str, asset_id: str) -> str:
    return (
        f"/api/editor/tasks/{quote(task_id, safe='')}/assets/"
        f"{quote(asset_id, safe='')}/preview"
    )


def _editor_public_project(task_id: str, project: dict[str, Any]) -> dict[str, Any]:
    urls = {
        str(asset.get("asset_id")): _editor_asset_url(task_id, str(asset.get("asset_id")))
        for asset in project.get("assets") or []
        if asset.get("asset_id")
    }
    return to_frontend_project(project, asset_urls=urls)


def _remote_editor_identity_values(document: Any) -> set[str]:
    """Collect stable UCMS identifiers without retaining signed media URLs."""

    if not isinstance(document, dict):
        return set()
    values: set[str] = set()
    for key in ("remote_id", "id", "guid"):
        value = document.get(key)
        if value not in (None, ""):
            values.add(str(value).strip())
    for key in ("raw", "remote_video"):
        nested = document.get(key)
        if isinstance(nested, dict):
            values.update(_remote_editor_identity_values(nested))
    return {value for value in values if value}


def _remote_editor_value(document: Any, keys: tuple[str, ...]) -> str:
    if not isinstance(document, dict):
        return ""
    for key in keys:
        value = document.get(key)
        if value not in (None, ""):
            return str(value).strip()
    for key in ("raw", "remote_video"):
        value = _remote_editor_value(document.get(key), keys)
        if value:
            return value
    return ""


def _remote_editor_stable_key(remote_video: dict[str, Any], cache_path: Path) -> str:
    station = _remote_editor_value(remote_video, ("record_station", "recordStation"))
    identifier_type = "cache"
    identifier = ""
    for key in ("guid", "remote_id", "id"):
        identifier = _remote_editor_value(remote_video, (key,))
        if identifier:
            identifier_type = key
            break
    if not identifier:
        identifier = str(cache_path.expanduser().resolve()).casefold()
    digest = hashlib.sha256(
        f"{station.casefold()}|{identifier_type}|{identifier}".encode("utf-8")
    ).hexdigest()[:24]
    return f"ucms_{digest}"


def _find_remote_editor_asset(
    project: dict[str, Any],
    remote_video: dict[str, Any],
    *,
    cache_path: Path | None = None,
) -> dict[str, Any] | None:
    identities = _remote_editor_identity_values(remote_video)
    expected_path = cache_path.expanduser().resolve() if cache_path else None
    remote_key = _remote_editor_stable_key(remote_video, expected_path) if expected_path else ""
    incoming_station = _remote_editor_value(remote_video, ("record_station", "recordStation"))
    for asset in project.get("assets") or []:
        asset_path_text = str(asset.get("path") or "").strip()
        if expected_path and asset_path_text:
            try:
                if Path(asset_path_text).expanduser().resolve() == expected_path:
                    return asset
            except OSError:
                pass
        if str(asset.get("source_type") or "") != "remote_ucms":
            continue
        if remote_key and str(asset.get("remote_key") or "") == remote_key:
            return asset
        asset_identities = _remote_editor_identity_values(asset)
        asset_identities.update(
            _remote_editor_identity_values((asset.get("metadata") or {}).get("remote_ucms"))
        )
        asset_station = _remote_editor_value(asset, ("record_station", "recordStation"))
        if (
            identities
            and identities.intersection(asset_identities)
            and (not incoming_station or not asset_station or incoming_station == asset_station)
        ):
            return asset
    return None


def _remote_editor_public_metadata(
    remote_video: dict[str, Any],
    download_result: dict[str, Any],
) -> dict[str, Any]:
    download_metadata = download_result.get("metadata") or {}
    normalized = download_metadata.get("remote_video") if isinstance(download_metadata, dict) else None
    source = normalized if isinstance(normalized, dict) else remote_video
    return {
        key: source.get(key)
        for key in (
            "remote_id",
            "id",
            "guid",
            "name",
            "display_name",
            "duration",
            "duration_text",
            "create_time",
            "record_station",
            "tv_station",
            "mime_type",
            "aspect",
        )
        if source.get(key) not in (None, "")
    }


def _first_editor_text(document: Any, keys: tuple[str, ...]) -> str:
    if not isinstance(document, dict):
        return ""
    for key in keys:
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("result", "content", "analysis", "news", "overview"):
        nested = document.get(key)
        if isinstance(nested, dict):
            value = _first_editor_text(nested, keys)
            if value:
                return value
    return ""


def _editor_task_copy(task_dir: Path, manifest: dict[str, Any]) -> tuple[str, str, str]:
    plan = read_json(_full_concat_plan_path(task_dir, manifest), {})
    title = _first_editor_text(plan, ("title", "news_title", "topic", "target_column"))
    summary = _first_editor_text(plan, ("summary", "content_summary", "abstract"))

    step = (manifest.get("steps") or {}).get("video_understanding") or {}
    output = step.get("output")
    if output:
        try:
            understanding = read_json(_safe_child(task_dir, str(output)), {})
        except HTTPException:
            understanding = {}
        title = title or _first_editor_text(
            understanding,
            ("title", "news_title", "topic", "event_title", "subject"),
        )
        summary = summary or _first_editor_text(
            understanding,
            ("summary", "content_summary", "event_summary", "abstract", "main_event"),
        )

    group_title = _task_group_meta(task_dir, manifest).get("group_title") or ""
    title = title or str(group_title) or _display_title_from_task_id(task_dir.name)
    cover_title = _first_editor_text(plan, ("cover_title", "title", "target_column")) or title
    return title, summary, cover_title


def _manual_editor_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ProjectConflictError):
        return HTTPException(409, str(exc))
    if isinstance(exc, ProjectValidationError):
        return HTTPException(422, str(exc))
    return HTTPException(400, str(exc))


@app.get("/api/editor/tasks/{task_id}")
def get_editor_project(task_id: str) -> dict[str, Any]:
    task_dir, manifest = _full_concat_editor_task(task_id)
    try:
        project = _load_or_initialize_editor_project(task_dir, manifest)
        title, summary, cover_title = _editor_task_copy(task_dir, manifest)
    except ManualEditorError as exc:
        raise _manual_editor_http_error(exc) from exc
    return {
        "task_id": task_id,
        "title": title,
        "summary": summary,
        "cover_title": cover_title,
        "project": _editor_public_project(task_id, project),
        "cover_config": public_provider_config(PROJECT_CONFIG),
    }


@app.put("/api/editor/tasks/{task_id}/project")
def save_editor_project(task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    task_dir, manifest = _full_concat_editor_task(task_id)
    editor_root = _manual_editor_root(task_dir)
    try:
        with EDITOR_PROJECT_LOCK:
            current = _load_or_initialize_editor_project(task_dir, manifest)
            edited = apply_frontend_project(current, payload)
            paths = save_project_revision(edited, editor_root)
    except ManualEditorError as exc:
        raise _manual_editor_http_error(exc) from exc
    return {
        "ok": True,
        "project": _editor_public_project(task_id, edited),
        "project_file": relpath(paths["project"], task_dir),
        "revision_file": relpath(paths["revision"], task_dir),
    }


@app.get("/api/editor/tasks/{task_id}/assets/{asset_id}/preview")
def preview_editor_asset(task_id: str, asset_id: str):
    task_dir, manifest = _full_concat_editor_task(task_id)
    try:
        project = _load_or_initialize_editor_project(task_dir, manifest)
    except ManualEditorError as exc:
        raise _manual_editor_http_error(exc) from exc
    asset = next(
        (item for item in project.get("assets") or [] if str(item.get("asset_id")) == asset_id),
        None,
    )
    if not asset:
        raise HTTPException(404, "素材不存在")
    path = Path(str(asset.get("path") or "")).expanduser().resolve()
    if not path.is_file():
        raise HTTPException(404, "素材文件不存在")
    if path.suffix.lower() not in {".mp4", ".mov", ".mkv", ".m4v", ".avi"}:
        raise HTTPException(400, "素材格式不支持预览")
    return FileResponse(str(path))


@app.post("/api/editor/tasks/{task_id}/assets")
async def upload_editor_asset(task_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    task_dir, manifest = _full_concat_editor_task(task_id)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".m4v", ".avi"}:
        raise HTTPException(400, "仅支持 MP4/MOV/MKV/M4V/AVI 视频")
    upload_dir = ensure_dir(_manual_editor_root(task_dir) / "assets")
    safe_name = _safe_filename(file.filename or f"video{suffix}")
    target = upload_dir / f"{uuid.uuid4().hex[:10]}_{safe_name}"
    max_bytes = int(os.environ.get("MANUAL_EDITOR_MAX_UPLOAD_BYTES", 8 * 1024 * 1024 * 1024))
    size = 0
    try:
        with target.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, "上传视频超过精剪素材大小限制")
                out.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    try:
        with EDITOR_PROJECT_LOCK:
            current = _load_or_initialize_editor_project(task_dir, manifest)
            edited = add_asset(
                current,
                target,
                asset_id=f"asset_{uuid.uuid4().hex[:12]}",
                display_name=file.filename or target.name,
                probe=True,
            )
            save_project_revision(edited, _manual_editor_root(task_dir))
    except ManualEditorError as exc:
        target.unlink(missing_ok=True)
        raise _manual_editor_http_error(exc) from exc

    public_project = _editor_public_project(task_id, edited)
    public_asset = next(
        item for item in public_project["assets"] if item["asset_id"] == edited["assets"][-1]["asset_id"]
    )
    return {"ok": True, "asset": public_asset, "project": public_project}


@app.post("/api/editor/tasks/{task_id}/remote-assets")
def add_remote_editor_asset(task_id: str, req: RemoteEditorAssetRequest) -> dict[str, Any]:
    """Download a UCMS item to the shared cache and add it to the manual editor."""

    task_dir, manifest = _full_concat_editor_task(task_id)
    remote_video = req.remote_video
    if not remote_video:
        raise HTTPException(400, "remote_video is required")
    if not REMOTE_UCMS_CONFIG.enabled:
        raise HTTPException(409, "远程素材库未启用")

    try:
        expected_cache_path = remote_cache_path(REMOTE_UCMS_CONFIG, remote_video)
    except Exception as exc:  # noqa: BLE001 - normalize provider payload errors at the API boundary
        print(f"[manual-editor] invalid remote asset payload: {exc}", file=sys.stderr)
        raise HTTPException(502, "远程素材信息无效") from exc

    # A normal repeat add can return immediately without another remote request.
    if not req.force_remote_download:
        try:
            with EDITOR_PROJECT_LOCK:
                current = _load_or_initialize_editor_project(task_dir, manifest)
                existing = _find_remote_editor_asset(
                    current,
                    remote_video,
                    cache_path=expected_cache_path,
                )
                if existing and Path(str(existing.get("path") or "")).is_file():
                    public_project = _editor_public_project(task_id, current)
                    public_asset = next(
                        item
                        for item in public_project["assets"]
                        if item["asset_id"] == existing["asset_id"]
                    )
                    return {
                        "ok": True,
                        "duplicate": True,
                        "downloaded": False,
                        "asset": public_asset,
                        "project": public_project,
                    }
        except ManualEditorError as exc:
            raise _manual_editor_http_error(exc) from exc

    try:
        download_result = download_ucms_video(
            REMOTE_UCMS_CONFIG,
            remote_video,
            root_dir=ROOT,
            force=req.force_remote_download,
        )
    except Exception as exc:  # noqa: BLE001 - remote transport/provider failures are upstream errors
        print(f"[manual-editor] remote asset download failed: {exc}", file=sys.stderr)
        raise HTTPException(502, "远程素材下载失败，请稍后重试") from exc

    # The cache location is derived again from server configuration and UCMS identity;
    # no path from the browser or provider response is trusted here.
    cached_path = expected_cache_path.expanduser().resolve()
    if not cached_path.is_file() or cached_path.stat().st_size <= 0:
        raise HTTPException(502, "远程素材缓存文件不存在或为空")

    remote_metadata = _remote_editor_public_metadata(remote_video, download_result)
    display_name = str(
        remote_metadata.get("display_name")
        or remote_metadata.get("name")
        or remote_video.get("display_name")
        or remote_video.get("name")
        or cached_path.name
    )
    source_id = str(
        remote_metadata.get("remote_id")
        or remote_metadata.get("id")
        or remote_metadata.get("guid")
        or f"remote_{hashlib.sha256(str(cached_path).encode('utf-8')).hexdigest()[:16]}"
    )
    remote_key = _remote_editor_stable_key(remote_video, cached_path)

    try:
        with EDITOR_PROJECT_LOCK:
            # Re-check after the potentially long download so concurrent adds stay idempotent.
            current = _load_or_initialize_editor_project(task_dir, manifest)
            existing = _find_remote_editor_asset(
                current,
                remote_video,
                cache_path=cached_path,
            )
            if existing:
                public_project = _editor_public_project(task_id, current)
                public_asset = next(
                    item
                    for item in public_project["assets"]
                    if item["asset_id"] == existing["asset_id"]
                )
                return {
                    "ok": True,
                    "duplicate": True,
                    "downloaded": bool(download_result.get("downloaded")),
                    "asset": public_asset,
                    "project": public_project,
                }

            edited = add_asset(
                current,
                cached_path,
                asset_id=f"asset_remote_{uuid.uuid4().hex[:12]}",
                source_id=source_id,
                display_name=display_name,
                probe=True,
            )
            added = edited["assets"][-1]
            added.update(
                {
                    "source_type": "remote_ucms",
                    "origin": "remote_ucms",
                    "remote_key": remote_key,
                    "remote_id": str(remote_metadata.get("remote_id") or source_id),
                    "remote_guid": str(remote_metadata.get("guid") or ""),
                    "record_station": str(remote_metadata.get("record_station") or ""),
                    "create_time": str(remote_metadata.get("create_time") or ""),
                    "metadata": {"remote_ucms": remote_metadata},
                }
            )
            save_project_revision(edited, _manual_editor_root(task_dir))
    except ManualEditorError as exc:
        print(f"[manual-editor] remote asset could not be added: {exc}", file=sys.stderr)
        if isinstance(exc, ProjectConflictError):
            raise HTTPException(409, "编辑工程版本已更新，请刷新后重试") from exc
        if isinstance(exc, ProjectValidationError):
            raise HTTPException(422, "远程素材无法加入当前编辑工程") from exc
        raise HTTPException(400, "远程素材无法加入编辑工程，请确认视频文件有效") from exc

    public_project = _editor_public_project(task_id, edited)
    public_asset = next(
        item for item in public_project["assets"] if item["asset_id"] == added["asset_id"]
    )
    return {
        "ok": True,
        "duplicate": False,
        "downloaded": bool(download_result.get("downloaded")),
        "asset": public_asset,
        "project": public_project,
    }


@app.post("/api/editor/tasks/{task_id}/export")
def export_editor_video(task_id: str) -> dict[str, Any]:
    task_dir, manifest = _full_concat_editor_task(task_id)
    try:
        with EDITOR_PROJECT_LOCK:
            project = _load_or_initialize_editor_project(task_dir, manifest)
            paths = save_project_revision(project, _manual_editor_root(task_dir))
    except ManualEditorError as exc:
        raise _manual_editor_http_error(exc) from exc
    if not (project.get("timeline") or {}).get("clips"):
        raise HTTPException(400, "时间轴没有可导出的片段")

    revision = int(project["revision"])
    export_dir = ensure_dir(_manual_editor_root(task_dir) / "exports")
    cache_dir = ensure_dir(_manual_editor_root(task_dir) / "cache")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = export_dir / f"manual_export_v{revision:04d}_{stamp}_{uuid.uuid4().hex[:6]}.mp4"
    job_id = f"job_editor_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    log_dir = ensure_dir(task_dir / "web_jobs")
    log_path = log_dir / f"{job_id}.log"
    cmd = [
        sys.executable,
        "-u",
        str(MANUAL_EDITOR_RUNNER),
        "export",
        str(paths["revision"]),
        str(output),
        "--cache-dir",
        str(cache_dir),
    ]

    with JOB_LOCK:
        active_job = _find_active_job_by_task_id(task_id)
        if active_job and SAME_TASK_POLICY == "reject":
            raise HTTPException(409, "当前任务已有运行中或排队中的处理，请完成后再导出")
        active_count = sum(
            1
            for existing_job_id in list(JOBS)
            if _refresh_job(existing_job_id, schedule_next=False).get("status") in {"pending", "running"}
        )
        if active_count >= MAX_ACTIVE_JOBS:
            raise HTTPException(429, "后台处理已满，请稍后再导出")
        pending_count = sum(1 for job in JOBS.values() if job.get("status") == "pending")
        if pending_count >= MAX_PENDING_JOBS:
            raise HTTPException(429, "等待队列已满，请稍后再导出")
        log_path.write_text(" ".join(cmd) + "\n\n=== job queued ===\n", encoding="utf-8")
        job = {
            "job_id": job_id,
            "job_type": "manual_editor_export",
            "task_id": task_id,
            "pid": None,
            "cmd": cmd,
            "log": relpath(log_path, ROOT),
            "log_path": str(log_path),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "started_at": "",
            "status": "pending",
            "returncode": None,
            "submitted_by": "manual_editor",
            "project_revision": revision,
            "output_file": relpath(output, task_dir),
            "output_url": _task_file_url(task_id, task_dir, output),
        }
        JOBS[job_id] = job
        PENDING_JOB_IDS.append(job_id)
        _persist_job(job)

    _schedule_jobs()
    return _public_job(JOBS[job_id])


def _cover_result_urls(task_id: str, task_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    public = dict(result)
    public["files"] = []
    for item in result.get("files", []) or []:
        copied = dict(item)
        path = _safe_child(task_dir, str(item.get("file") or ""))
        if path.exists() and path.is_file():
            copied["url"] = _task_file_url(task_id, task_dir, path)
        public["files"].append(copied)
    return public


@app.get("/api/editor/tasks/{task_id}/cover-config")
def get_editor_cover_config(task_id: str) -> dict[str, Any]:
    _full_concat_editor_task(task_id)
    return public_provider_config(PROJECT_CONFIG)


@app.post("/api/editor/tasks/{task_id}/covers/prompt")
def create_editor_cover_prompt(task_id: str, req: CoverPromptRequest) -> dict[str, Any]:
    _full_concat_editor_task(task_id)
    try:
        return COVER_SERVICE.create_seedream_prompt(**req.model_dump())
    except CoverServiceError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - provider errors need a stable user-facing boundary
        raise HTTPException(502, f"豆包生成 Seedream 提示词失败：{exc}") from exc


@app.post("/api/editor/tasks/{task_id}/covers/generate")
def generate_editor_covers(task_id: str, req: CoverGenerateRequest) -> dict[str, Any]:
    task_dir, _ = _full_concat_editor_task(task_id)
    reference = _safe_child(task_dir, req.reference_file) if req.reference_file else None
    if reference is not None and (not reference.exists() or reference.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}):
        raise HTTPException(400, "参考图片无效")
    try:
        result = COVER_SERVICE.generate_images(
            task_dir=task_dir,
            prompt_doc=req.prompt_doc,
            max_images=req.max_images,
            watermark=req.watermark,
            reference_image=reference,
            edit_instruction=req.edit_instruction,
        )
    except CoverServiceError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Seedream 生图失败：{exc}") from exc
    return _cover_result_urls(task_id, task_dir, result)


@app.post("/api/editor/tasks/{task_id}/covers/ai-restore")
def restore_editor_cover(task_id: str, req: CoverRestoreRequest) -> dict[str, Any]:
    task_dir, _ = _full_concat_editor_task(task_id)
    source = _safe_child(task_dir, req.source_file)
    if not source.exists() or source.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(400, "待修复图片无效")
    instruction = (
        "对参考图进行高保真高清修复和细节增强，保持原构图、人物身份、面部特征、服装、物体、"
        "新闻现场关系和色彩基调不变，不新增或删除事实性内容，不生成文字、Logo或水印。"
        + str(req.requirements or "")[:600]
    )
    prompt_doc = {
        "prompt": instruction,
        "size": req.size if req.size in COVER_CONFIG.allowed_sizes else COVER_CONFIG.allowed_sizes[-1],
        "aspect_ratio": COVER_DEFAULT_ASPECT_RATIO,
        "safe_area": COVER_DEFAULT_SAFE_AREA,
        "chat_model": "manual_restore_instruction",
        "skill_hash": "seedream_reference_restore_v1",
    }
    try:
        result = COVER_SERVICE.generate_images(
            task_dir=task_dir,
            prompt_doc=prompt_doc,
            max_images=1,
            watermark=False,
            reference_image=source,
            edit_instruction="",
        )
    except CoverServiceError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Seedream 高清修复失败：{exc}") from exc
    return _cover_result_urls(task_id, task_dir, result)


@app.post("/api/editor/tasks/{task_id}/covers/upload")
async def upload_editor_cover(task_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    task_dir, _ = _full_concat_editor_task(task_id)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(400, "仅支持 JPG/PNG/WebP 图片")
    upload_dir = ensure_dir(task_dir / "edit" / "manual_editor" / "cover_uploads")
    target = upload_dir / f"upload_{uuid.uuid4().hex}{suffix}"
    size = 0
    try:
        with target.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > COVER_CONFIG.max_image_bytes:
                    raise HTTPException(413, f"图片不能超过 {COVER_CONFIG.max_image_bytes // (1024 * 1024)}MB")
                out.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    try:
        with Image.open(target) as image:
            image.verify()
        with Image.open(target) as image:
            width, height = image.size
        if width * height > COVER_CONFIG.max_image_pixels:
            raise ValueError("图片像素过大")
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(400, f"图片文件无效：{exc}") from exc
    return {
        "file": relpath(target, task_dir),
        "url": _task_file_url(task_id, task_dir, target),
        "width": width,
        "height": height,
    }


@app.get("/api/editor/tasks/{task_id}/fonts")
def list_editor_fonts(task_id: str) -> list[dict[str, Any]]:
    task_dir, _ = _full_concat_editor_task(task_id)
    fonts_dir = task_dir / "edit" / "manual_editor" / "fonts"
    if not fonts_dir.exists():
        return []
    return [
        {"name": path.name, "file": relpath(path, task_dir), "url": _task_file_url(task_id, task_dir, path)}
        for path in sorted(fonts_dir.iterdir())
        if path.is_file() and path.suffix.lower() in {".ttf", ".otf"}
    ]


@app.post("/api/editor/tasks/{task_id}/fonts")
async def upload_editor_font(task_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    task_dir, _ = _full_concat_editor_task(task_id)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".ttf", ".otf"}:
        raise HTTPException(400, "仅支持 TTF/OTF 字体")
    fonts_dir = ensure_dir(task_dir / "edit" / "manual_editor" / "fonts")
    safe_stem = re.sub(r"[^A-Za-z0-9_-]", "_", Path(file.filename or "font").stem)[:60] or "font"
    target = fonts_dir / f"{safe_stem}_{uuid.uuid4().hex[:8]}{suffix}"
    size = 0
    try:
        with target.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 20 * 1024 * 1024:
                    raise HTTPException(413, "字体不能超过 20MB")
                out.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    try:
        metadata = validate_font_file(target)
    except CoverServiceError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    return {**metadata, "file": relpath(target, task_dir), "url": _task_file_url(task_id, task_dir, target)}


@app.post("/api/editor/tasks/{task_id}/covers/compose")
def compose_editor_cover(task_id: str, req: CoverComposeRequest) -> dict[str, Any]:
    task_dir, _ = _full_concat_editor_task(task_id)
    source = _safe_child(task_dir, req.source_file)
    if not source.exists() or source.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(400, "封面源图片无效")
    output_format = "jpg" if req.output_format.lower() in {"jpg", "jpeg"} else "png"
    export_dir = ensure_dir(task_dir / "edit" / "manual_editor" / "cover_exports")
    export_id = f"cover_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    output = export_dir / f"{export_id}.{output_format}"
    fonts_dir = task_dir / "edit" / "manual_editor" / "fonts"
    try:
        result = compose_cover(
            source_path=source,
            output_path=output,
            width=req.width,
            height=req.height,
            crop=req.crop,
            text_layers=req.text_layers,
            enhance=req.enhance,
            fonts_dir=fonts_dir,
        )
    except CoverServiceError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"封面导出失败：{exc}") from exc
    composition = {
        "export_id": export_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_file": req.source_file,
        "output_file": relpath(output, task_dir),
        "width": req.width,
        "height": req.height,
        "crop": req.crop,
        "text_layers": req.text_layers,
        "enhance": req.enhance,
    }
    write_json(export_dir / f"{export_id}.json", composition)
    return {
        **result,
        "file": relpath(output, task_dir),
        "url": _task_file_url(task_id, task_dir, output),
        "composition": composition,
    }


@app.post("/api/run")
def run_pipeline(req: RunRequest) -> dict[str, Any]:
    _normalize_output_options(req)
    if req.production_mode in {"highlight_reassembly", "full_concat"}:
        req.audio_policy = "original"
        req.require_tts = False
        req.skip_tts = True
        req.allow_original_audio_evidence = True
    else:
        req.audio_policy = "ai_voiceover"
        req.allow_original_audio_evidence = False
    _prepare_mode_switched_run(req)
    _apply_full_concat_runtime_defaults(req)
    raw_source_items = _collect_source_items(req)
    # Start downloading remote sources to the local cache immediately, while the
    # remote signed URL is still fresh, so 原片 preview can fall back to the local
    # copy after the URL expires. Reuses the same cache the run pipeline reads from.
    _prefetch_remote_sources(raw_source_items)
    use_unified_source_pipeline = _use_unified_source_pipeline(PROJECT_CONFIG)
    should_use_source_request = bool(raw_source_items) and (
        use_unified_source_pipeline or len(raw_source_items) > 1
    )
    source_items: list[MultiSourceItem] = []
    source_manifest_path: Path | None = None
    source_request_path: Path | None = None
    common_source_key = _common_source_key(req, raw_source_items) if should_use_source_request else ""
    common_task_id = f"common_{common_source_key}" if common_source_key else ""
    if should_use_source_request:
        _validate_source_request_items(raw_source_items)
        fingerprint = _source_request_fingerprint(req, raw_source_items)
        input_name_for_task = _display_source_request_name(raw_source_items)
        if _should_fingerprint_task_id(req):
            task_id = _make_task_id_from_fingerprint(input_name_for_task, req.production_mode, fingerprint)
        else:
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
        if input_name_for_task and _should_fingerprint_task_id(req):
            task_id = _make_task_id_from_fingerprint(input_name_for_task, req.production_mode, fingerprint)
        else:
            task_id = _resolve_run_task_id(req)
        task_id = _with_mode_suffix(task_id, req.production_mode)
        task_dir = ensure_dir(OUTPUTS_DIR / task_id)
    elif not raw_source_items:
        input_path = _prepare_run_input_video(req)
        fingerprint = _run_fingerprint(req, input_path) if input_path else ""
        input_name_for_task = _display_input_name(req)
        if input_name_for_task and _should_fingerprint_task_id(req):
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
        active_count = sum(
            1
            for job_id_existing in list(JOBS)
            if _refresh_job(job_id_existing, schedule_next=False).get("status") in {"pending", "running"}
        )
        if active_count >= MAX_ACTIVE_JOBS:
            raise HTTPException(
                429,
                f"同时最多只能处理 {MAX_ACTIVE_JOBS} 个视频，现在 {active_count} 个都在忙。等其中一个做完，再来试试吧～",
            )
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
            "common_task_id": common_task_id,
            "common_source_key": common_source_key,
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
    max_output_seconds = int(req.max_output_video_seconds or 0)
    configured_max = int(
        SHORT_VIDEO_DEFAULTS.get(
            "ai_voiceover_max_output_seconds",
            SHORT_VIDEO_DEFAULTS.get("max_long_video_seconds", 180),
        )
    )

    if req.production_mode == "ai_voiceover":
        if max_output_seconds <= 0:
            max_output_seconds = configured_max
        max_output_seconds = max(30, min(max_output_seconds, configured_max))
    else:
        max_output_seconds = max(1, max_output_seconds or req.max_output_video_seconds)

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
        "--target-duration-mode",
        str(req.target_duration_mode or "soft"),
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
        str(max_output_seconds),
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
    if req.production_mode == "full_concat" and (req.target_column or "").strip():
        cmd.extend(["--target-column", req.target_column.strip()])
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


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            return f'"{pid}"' in result.stdout or f",{pid}," in result.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _mark_job_finished(
    job: dict[str, Any],
    *,
    status: str,
    returncode: int,
    user_message: str,
) -> dict[str, Any]:
    job["status"] = status
    job["returncode"] = returncode
    job["finished_at"] = datetime.now().isoformat(timespec="seconds")
    job["user_message"] = user_message
    log_file = job.get("log_file")
    if log_file:
        try:
            log_file.close()
        except Exception:
            pass
    _persist_job(job)
    return job


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
    JOB_STORE.save_job(job["job_id"], public_job)


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
        if not job.get("process") and not _pid_exists(pid):
            _mark_job_finished(
                job,
                status="cancelled",
                returncode=-9,
                user_message="任务进程已不存在，已清理残留运行状态。",
            )
            _schedule_jobs()
            return _public_job(job)

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
    if not process and job.get("status") in {"pending", "running"}:
        if job.get("status") == "pending" and job_id in PENDING_JOB_IDS:
            return _public_job(job)
        pid = int(job.get("pid") or 0)
        if job.get("status") == "running" and _pid_exists(pid):
            return _public_job(job)
        if job.get("status") == "pending":
            try:
                PENDING_JOB_IDS.remove(job_id)
            except ValueError:
                pass
        _mark_job_finished(
            job,
            status="failed",
            returncode=-9,
            user_message="任务进程已不存在，可能是 Web 服务被重启或手动结束，已标记为中断。",
        )
        changed_to_finished = True
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
    elif not changed_to_finished and Path(job.get("log_path", "")).exists():
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

    for step in common_progress_steps(unified=True):
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
        + list((task_dir / "edit" / "reassembly_drafts").glob("v*/reassembly_render_outputs.json"))
        + list((task_dir / "edit" / "full_concat_drafts").glob("v*/full_concat_render_outputs.json")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    ordered: list[Path] = []
    seen: set[Path] = set()
    for index in render_indexes:
        data = read_json(index, {})
        if "full_concat_drafts" in index.parts and data.get("semantic_qc_status") in {"fail", "failed"}:
            continue
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
        for root in [
            task_dir / "edit" / "drafts",
            task_dir / "edit" / "reassembly_drafts",
            task_dir / "edit" / "full_concat_drafts",
            task_dir / "edit" / "manual_editor" / "exports",
        ]
        for p in root.rglob("*.mp4")
        if "_clips" not in p.parts and p.is_file()
        and not ("full_concat_drafts" in p.parts and _full_concat_is_quarantined(task_dir))
    ]
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        resolved = path.resolve()
        if resolved not in seen:
            ordered.append(resolved)
            seen.add(resolved)
    return sorted(ordered, key=lambda path: path.stat().st_mtime_ns, reverse=True)


def _full_concat_is_quarantined(task_dir: Path) -> bool:
    manifest = read_json(task_dir / "manifest.json", {})
    step = (manifest.get("steps", {}) or {}).get("full_concat_output_qc", {}) or {}
    output = step.get("output")
    if not output:
        return False
    doc = read_json(task_dir / output, {})
    return bool(doc.get("quarantined") or doc.get("publishable") is False)


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


def _find_active_job_by_common_task_id(common_task_id: str) -> dict[str, Any] | None:
    if not common_task_id:
        return None

    for job_id, job in list(JOBS.items()):
        public = _refresh_job(job_id)
        if public.get("status") not in {"pending", "running"}:
            continue
        if job.get("common_task_id") == common_task_id:
            return job

    return None


def _is_task_already_completed(manifest: dict[str, Any], production_mode: str) -> bool:
    if not manifest:
        return False
    steps = manifest.get("steps", {})
    final_step = {
        "highlight_reassembly": "reassembly_render",
        "full_concat": "full_concat_render",
    }.get(production_mode, "render")
    if production_mode == "full_concat" and "full_concat_output_qc" in steps:
        qc = steps.get("full_concat_output_qc", {}) or {}
        return qc.get("status") in {"success", "partial_success"} and bool(qc.get("publishable", False))
    return steps.get(final_step, {}).get("status") in {"success", "partial_success"}


def _task_dir(task_id: str) -> Path:
    task_dir = _safe_child(OUTPUTS_DIR, task_id)
    if not task_dir.exists():
        raise HTTPException(404, "task not found")
    return task_dir


def _rmtree_task_dir(task_dir: Path) -> None:
    def onexc(function: Any, path: str, exc_info: Any) -> None:
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            function(path)
        except Exception:
            raise exc_info[1]

    if not task_dir.exists():
        return
    kwargs: dict[str, Any] = {}
    if sys.version_info >= (3, 12):
        kwargs["onexc"] = onexc
    else:
        kwargs["onerror"] = lambda function, path, exc_info: onexc(function, path, exc_info)
    shutil.rmtree(task_dir, **kwargs)


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
            remote_url = (
                remote.get("preview_url")
                or remote.get("media_low_url")
                or remote.get("mediaLow")
                or remote.get("media_high_url")
                or remote.get("mediaHigh")
                or remote.get("download_url")
                or ""
            )
            # Prefer the locally cached copy: the remote signed URL expires, the
            # local file does not. Remote URL is only used while the prefetch
            # download is still in flight.
            local_url = _remote_local_preview_url(remote)
            url = local_url or remote_url
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
                    "remote_url": remote_url,
                    "local_url": local_url,
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

        _apply_full_concat_runtime_defaults(req)
        req.force_remote_download = bool(source_request.get("force_remote_download", req.force_remote_download))
        req.task_id = _with_mode_suffix(source_task_id, req.production_mode)
        req.reuse_from_task_id = source_task_id
        return

    req.input_video = str(_existing_task_source_video(source_task_dir, source_manifest))
    req.task_id = _with_mode_suffix(source_task_id, req.production_mode)
    req.reuse_from_task_id = source_task_id
    _apply_full_concat_runtime_defaults(req)


def _apply_full_concat_runtime_defaults(req: RunRequest) -> None:
    if req.production_mode != "full_concat":
        return
    # 完整版需更细的分段，让短的片头/台标/赞助卡/预告独立成段被精准删。
    # 这里必须在“从已有任务切换模式”之后再套一次，避免旧 source_request 的 60s 覆盖本模式粒度。
    fc = PROJECT_CONFIG.raw.get("full_concat", {}) or {}
    req.chunk_seconds = int(fc.get("chunk_seconds", 10) or 10)
    req.frame_interval = int(fc.get("frame_interval", 5) or 5)


def _task_id_production_mode(task_id: str) -> str | None:
    if any(alias in task_id for alias in _mode_aliases("highlight_reassembly")):
        return "highlight_reassembly"
    if any(alias in task_id for alias in _mode_aliases("full_concat")):
        return "full_concat"
    if any(alias in task_id for alias in _mode_aliases("ai_voiceover")):
        return "ai_voiceover"
    return None


def _manifest_production_mode(manifest: dict[str, Any]) -> str:
    for key in ("last_web_run_options", "last_run_options"):
        mode = (manifest.get(key) or {}).get("production_mode")
        if mode in {"ai_voiceover", "highlight_reassembly", "full_concat"}:
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
    request = {
        "production_mode": req.production_mode,
        "aspect_ratio": req.aspect_ratio,
        "chunk_seconds": req.chunk_seconds,
        "frame_interval": req.frame_interval,
        "mode": req.mode,
        "source_items": items,
    }
    profile = build_common_analysis_profile(request, config=PROJECT_CONFIG)
    return build_common_source_key(items, profile, root=ROOT)


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


def _is_auto_generated_task_id(task_id: str | None) -> bool:
    """前端 suggestedTaskId() 生成的 id 以 _YYYYMMDD_HHMM(SS) 时间戳结尾。
    这类 id 每次提交都不同，会让基于 task_id 的去重永远失效，因此视为“自动生成”，
    交给基于素材内容指纹的 task_id 去重（相同素材+相同参数复用同一任务）。"""
    return bool(re.search(r"_\d{8}_\d{4,6}$", (task_id or "").strip()))


def _should_fingerprint_task_id(req: RunRequest) -> bool:
    """是否用素材内容指纹生成 task_id（相同素材+相同参数复用同一任务）。
    仅在“新建任务”时生效：未指定 task_id，或指定的是前端自动生成的带时间戳 id；
    且不是对已有任务的重跑/复用（rerun / rerun_from / reuse_from_task_id），以免改写到别的目录。"""
    if req.rerun or req.rerun_from or req.reuse_from_task_id:
        return False
    return not (req.task_id or "").strip() or _is_auto_generated_task_id(req.task_id)


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
    if production_mode == "highlight_reassembly":
        return "视频重组"
    if production_mode == "full_concat":
        return "完整版"
    return "AI配音解说"


def _mode_aliases(production_mode: str) -> tuple[str, ...]:
    if production_mode == "highlight_reassembly":
        return ("highlight_reassembly", "视频重组")
    if production_mode == "full_concat":
        return ("full_concat", "完整版")
    return ("ai_voiceover", "AI配音解说")


def _all_mode_aliases() -> tuple[str, ...]:
    return (
        _mode_aliases("ai_voiceover")
        + _mode_aliases("highlight_reassembly")
        + _mode_aliases("full_concat")
    )
