from __future__ import annotations

import os
import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from newsclip_agent.config import load_config
from newsclip_agent.utils import ensure_dir, read_json, relpath, write_json


ROOT = Path(__file__).resolve().parent
OUTPUTS_DIR = ROOT / "outputs"
VIDEOS_DIR = ROOT / "videos"
STATIC_DIR = ROOT / "web_static"
RUNNER = ROOT / "run_pipeline.py"

app = FastAPI(title="凤凰新闻视频智能拆条工作台")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

JOBS: dict[str, dict[str, Any]] = {}
JOB_LOCK = RLock()
PENDING_JOB_IDS: deque[str] = deque()
PROJECT_CONFIG = load_config(ROOT / "config.toml")
WORKFLOW_DEFAULTS = PROJECT_CONFIG.workflow
SHORT_VIDEO_DEFAULTS = PROJECT_CONFIG.short_video
VOICEOVER_DEFAULTS = PROJECT_CONFIG.voiceover
WEB_CONCURRENCY = PROJECT_CONFIG.raw.get("web_concurrency", {})
MAX_RUNNING_JOBS = int(WEB_CONCURRENCY.get("max_running_jobs", 2))
MAX_PENDING_JOBS = int(WEB_CONCURRENCY.get("max_pending_jobs", 20))
SAME_TASK_POLICY = str(WEB_CONCURRENCY.get("same_task_policy", "reject"))


class RunRequest(BaseModel):
    input_video: str | None = None
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
    allow_long_video: bool = bool(SHORT_VIDEO_DEFAULTS.get("allow_long_video_default", False))
    require_tts: bool = bool(VOICEOVER_DEFAULTS.get("tts_required_by_default", True))
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
        },
    }


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


@app.delete("/api/videos")
def delete_video(path: str) -> dict[str, Any]:
    file_path = _resolve_input_video(path)
    file_path.unlink()
    return {"ok": True, "deleted": relpath(file_path, ROOT)}


@app.get("/api/tasks")
def list_tasks() -> list[dict[str, Any]]:
    if not OUTPUTS_DIR.exists():
        return []
    tasks = []
    seen_task_ids: set[str] = set()
    for task_dir in sorted([p for p in OUTPUTS_DIR.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True):
        manifest = read_json(task_dir / "manifest.json", {})
        if not manifest:
            continue
        steps = manifest.get("steps", {})
        done = sum(1 for s in steps.values() if s.get("status") in {"success", "partial_success", "skipped"})
        stale = sum(1 for s in steps.values() if s.get("status") == "stale")
        failed = sum(1 for s in steps.values() if s.get("status") == "failed")
        job_status = _active_job_status_for_task(task_dir.name)
        seen_task_ids.add(task_dir.name)
        tasks.append(
            {
                "task_id": task_dir.name,
                "source_video": manifest.get("source_video", ""),
                "updated_at": manifest.get("updated_at", ""),
                "created_at": manifest.get("created_at", ""),
                "done_steps": done,
                "stale_steps": stale,
                "failed_steps": failed,
                "step_count": len(steps),
                "job_status": job_status,
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
        raise HTTPException(404, "manifest.json not found")
    _hydrate_manifest_options(task_dir, manifest)
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


@app.get("/api/tasks/{task_id}/latest-video")
def latest_video(task_id: str) -> dict[str, Any]:
    task_dir = _task_dir(task_id)
    latest = _find_latest_draft_video(task_dir)
    if not latest:
        return {"exists": False, "file": "", "url": ""}
    return {
        "exists": True,
        "file": relpath(latest, task_dir),
        "url": f"/api/tasks/{task_id}/file?path={relpath(latest, task_dir)}",
    }


@app.get("/api/tasks/{task_id}/drafts")
def list_draft_videos(task_id: str) -> list[dict[str, Any]]:
    task_dir = _task_dir(task_id)
    drafts = _list_draft_videos(task_dir)
    return [
        {
            "file": relpath(path, task_dir),
            "url": f"/api/tasks/{task_id}/file?path={relpath(path, task_dir)}",
            "name": path.name,
            "type": "highlight_reassembly_draft" if "reassembly_drafts" in path.parts else "ai_voiceover_draft",
            "size_mb": round(path.stat().st_size / 1024 / 1024, 2),
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        }
        for path in drafts
    ]


@app.post("/api/run")
def run_pipeline(req: RunRequest) -> dict[str, Any]:
    if req.production_mode == "highlight_reassembly":
        req.audio_policy = "original"
        req.require_tts = False
        req.skip_tts = True
        req.allow_original_audio_evidence = True
    else:
        req.audio_policy = "ai_voiceover"
        req.allow_original_audio_evidence = False
    _prepare_mode_switched_run(req)
    input_path = _resolve_input_video(req.input_video) if req.input_video else None
    fingerprint = _run_fingerprint(req, input_path) if input_path else ""
    if req.input_video and not req.task_id:
        task_id = _make_task_id_from_fingerprint(req.input_video, req.production_mode, fingerprint)
    else:
        task_id = _resolve_run_task_id(req)
    task_dir = ensure_dir(OUTPUTS_DIR / task_id)

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

    cmd = _build_run_command(req, task_id, input_path)
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


def _build_run_command(req: RunRequest, task_id: str, input_path: Path | None = None) -> list[str]:
    cmd = [
        sys.executable,
        "-u",
        str(RUNNER),
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
    ]
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
    write_json(OUTPUTS_DIR / job["task_id"] / "last_web_job.json", _public_job(job))


def _save_web_run_options(task_dir: Path, req: RunRequest) -> None:
    manifest_path = task_dir / "manifest.json"
    manifest = read_json(manifest_path, {})
    web_options = req.model_dump()
    web_options["updated_at"] = datetime.now().isoformat(timespec="seconds")
    manifest["last_web_run_options"] = web_options
    if manifest:
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
    runtime_marker = "RuntimeError:"
    if runtime_marker in text:
        tail = text.rsplit(runtime_marker, 1)[-1].strip()
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        return "\n".join(lines[-4:])
    return ""


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
    root = ROOT.resolve()
    task_root = task_dir.resolve()
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "source video not found")
    if root not in path.parents and task_root not in path.parents and path != root and path != task_root:
        raise HTTPException(400, "invalid source path")
    return path


def _prepare_mode_switched_run(req: RunRequest) -> None:
    if req.input_video or req.rerun or req.rerun_from or not req.task_id:
        return

    source_task_id = req.task_id.strip()
    if not source_task_id:
        return
    task_dir = _task_dir(source_task_id)
    manifest = read_json(task_dir / "manifest.json", {})
    current_mode = _task_id_production_mode(source_task_id) or _manifest_production_mode(manifest)
    if current_mode == req.production_mode:
        return

    req.input_video = str(_existing_task_source_video(task_dir, manifest))
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
        raise HTTPException(400, "existing task source video missing")
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
        "production_mode": req.production_mode,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _make_task_id_from_fingerprint(input_video: str | None, production_mode: str, fingerprint: str) -> str:
    prefix = "task"
    if input_video:
        name = Path(input_video).stem
        safe = "".join(ch if ch.isalnum() else "_" for ch in name).strip("_")
        prefix = safe[:24] or "task"
    return f"{prefix}_{_mode_slug(production_mode)}_{fingerprint}"


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
