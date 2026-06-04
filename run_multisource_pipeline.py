from __future__ import annotations

import argparse
import json
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from newsclip_agent.config import load_config
from newsclip_agent.multisource import MultiSourceItem, build_multi_source_video, prepare_multi_source_manifest
from newsclip_agent.pipeline import main as pipeline_main
from newsclip_agent.resource_locks import file_slot_lock
from newsclip_agent.remote_ucms import download_ucms_video, load_remote_ucms_config
from newsclip_agent.utils import ensure_dir, read_json, relpath, write_json


ROOT = Path(__file__).resolve().parent
OUTPUTS_DIR = ROOT / "outputs"
COMMON_OUTPUTS_DIR = OUTPUTS_DIR / "__common__"

COMMON_REUSABLE_STEPS = [
    "source_prepare",
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "vision",
    "timeline",
    "timeline_digest",
]


def _sync_common_progress_to_child(
    *,
    common_dir: Path,
    task_dir: Path,
    request: dict[str, Any],
) -> None:
    """Mirror common analysis step statuses into one child task manifest.

    The common pipeline writes real progress to outputs/__common__/common_xxx.
    The Web UI reads outputs/<child_task>/manifest.json. This function keeps
    child manifest in sync without making the UI aware of common directories.
    """
    common_manifest = read_json(common_dir / "manifest.json", {})
    if not common_manifest:
        return

    child_manifest_path = task_dir / "manifest.json"
    child_manifest = read_json(child_manifest_path, {})
    now = datetime.now().isoformat(timespec="seconds")

    child_manifest.setdefault("task_id", task_dir.name)
    child_manifest.setdefault("created_at", now)
    child_manifest["updated_at"] = common_manifest.get("updated_at", now)
    child_manifest["common_task_id"] = common_dir.name
    child_manifest["common_source_key"] = request.get("common_source_key", "")
    child_manifest["production_mode"] = request.get(
        "production_mode",
        child_manifest.get("production_mode", "ai_voiceover"),
    )
    child_manifest["source_count"] = len(request.get("source_items") or [])
    child_manifest.setdefault("steps", {})

    common_steps = common_manifest.get("steps", {}) or {}

    for step in COMMON_REUSABLE_STEPS:
        common_step = common_steps.get(step)
        if not common_step:
            continue

        synced = dict(common_step)
        synced["common_progress"] = True
        synced["common_task_id"] = common_dir.name
        synced["reused_from"] = relpath(common_dir, ROOT)
        child_manifest["steps"][step] = synced

    for key in ("source_video", "source_mode", "source_manifest", "source_videos"):
        if common_manifest.get(key):
            child_manifest[key] = common_manifest[key]

    if "current_versions" in common_manifest:
        child_manifest.setdefault("current_versions", {}).update(common_manifest["current_versions"])

    if common_manifest.get("status") == "failed":
        child_manifest["status"] = "failed"
        child_manifest["user_message"] = common_manifest.get("user_message", "")
    elif any((common_steps.get(step) or {}).get("status") == "running" for step in COMMON_REUSABLE_STEPS):
        child_manifest["status"] = "running"
    elif all(
        (common_steps.get(step) or {}).get("status") in {"success", "partial_success", "skipped"}
        for step in COMMON_REUSABLE_STEPS
        if step in common_steps
    ):
        # Common part is ready, but mode-specific steps may still be pending/running.
        child_manifest["status"] = child_manifest.get("status") or "running"

    write_json(child_manifest_path, child_manifest)


def _start_common_progress_sync(
    *,
    common_dir: Path,
    task_dir: Path,
    request: dict[str, Any],
    interval_seconds: float = 2.0,
) -> threading.Event:
    stop_event = threading.Event()

    def loop() -> None:
        while not stop_event.is_set():
            try:
                _sync_common_progress_to_child(
                    common_dir=common_dir,
                    task_dir=task_dir,
                    request=request,
                )
            except Exception as exc:
                _append_common_log(common_dir, f"sync common progress to child failed: {exc}")
            stop_event.wait(interval_seconds)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return stop_event


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(description="Prepare multi-source inputs, then run the editing pipeline.")
    parser.add_argument("--source-request", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--aspect-ratio", default="16:9")
    return parser.parse_known_args(argv)


def main(argv: list[str] | None = None) -> int:
    args, passthrough = parse_args(argv)
    task_dir = ensure_dir(OUTPUTS_DIR / args.task_id)
    request_path = Path(args.source_request).resolve()
    request = read_json(request_path, {})

    common_task_id = str(request.get("common_task_id") or "").strip()
    if common_task_id:
        common_dir = ensure_dir(COMMON_OUTPUTS_DIR / common_task_id)
        stop_sync = _start_common_progress_sync(
            common_dir=common_dir,
            task_dir=task_dir,
            request=request,
        )
        try:
            _sync_common_progress_to_child(
                common_dir=common_dir,
                task_dir=task_dir,
                request=request,
            )
            _ensure_common_analysis(common_dir=common_dir, request=request, args=args, passthrough=passthrough)
            _sync_common_progress_to_child(
                common_dir=common_dir,
                task_dir=task_dir,
                request=request,
            )
            _copy_common_outputs_to_task(common_dir=common_dir, task_dir=task_dir, request=request)
            _sync_common_progress_to_child(
                common_dir=common_dir,
                task_dir=task_dir,
                request=request,
            )
            return _run_mode_specific_pipeline(
                args=args,
                passthrough=passthrough,
                task_dir=task_dir,
                request=request,
            )
        finally:
            stop_sync.set()
            try:
                _sync_common_progress_to_child(
                    common_dir=common_dir,
                    task_dir=task_dir,
                    request=request,
                )
            except Exception:
                pass

    _mark_source_prepare(task_dir, "running", request=request)
    try:
        config = load_config(ROOT / "config.toml")
        sources = _resolve_sources(request, config)
        multi_source_config = config.raw.get("multi_source", {})
        built = prepare_multi_source_manifest(
            sources=sources,
            task_dir=task_dir,
            root_dir=ROOT,
            aspect_ratio=str(request.get("aspect_ratio") or args.aspect_ratio),
        )
        _mark_source_prepare(task_dir, "success", request=request, built=built)
    except Exception as exc:
        _mark_source_prepare(task_dir, "failed", request=request, error=str(exc))
        raise

    pipeline_args = [
        "--task-id",
        args.task_id,
        "--source-manifest",
        str(built["source_manifest"]),
        "--aspect-ratio",
        str(request.get("aspect_ratio") or args.aspect_ratio),
        *passthrough,
    ]
    if built.get("input_video"):
        pipeline_args.extend(["--input", str(built["input_video"])])
    return pipeline_main(pipeline_args)


def _append_common_log(common_dir: Path, message: str) -> None:
    log_dir = ensure_dir(common_dir / "web_jobs")
    log_path = log_dir / "common_analysis.log"
    timestamp = datetime.now().isoformat(timespec="seconds")
    with log_path.open("a", encoding="utf-8", errors="replace") as f:
        f.write(f"[{timestamp}] {message}\n")


def _ensure_common_analysis(*, common_dir: Path, request: dict[str, Any], args: argparse.Namespace, passthrough: list[str]) -> None:
    ensure_dir(common_dir)
    ensure_dir(common_dir / "web_jobs")
    lock_name = f"common_source_{common_dir.name}"
    
    rerun = ""
    rerun_from = ""
    for index, item in enumerate(passthrough):
        if item == "--rerun" and index + 1 < len(passthrough):
            rerun = passthrough[index + 1]
        if item == "--rerun-from" and index + 1 < len(passthrough):
            rerun_from = passthrough[index + 1]
            
    force_rerun_common = bool(rerun in COMMON_REUSABLE_STEPS or rerun_from in COMMON_REUSABLE_STEPS)
    
    with file_slot_lock(lock_name, slots=1):
        if not force_rerun_common and _common_analysis_ready(common_dir):
            print(f"Reuse common analysis outputs: {common_dir}")
            _append_common_log(common_dir, "reuse common analysis outputs")
            return

        print(f"Start common analysis: {common_dir}")
        _append_common_log(common_dir, "start common analysis")
        _mark_source_prepare(common_dir, "running", request=request)
        try:
            config = load_config(ROOT / "config.toml")
            sources = _resolve_sources(request, config)
            multi_source_config = config.raw.get("multi_source", {})
            built = prepare_multi_source_manifest(
                sources=sources,
                task_dir=common_dir,
                root_dir=ROOT,
                aspect_ratio=str(request.get("aspect_ratio") or args.aspect_ratio),
            )
            _append_common_log(common_dir, f"source_prepare success: {built.get('input_video')}")
            _mark_source_prepare(common_dir, "success", request=request, built=built)
        except Exception as exc:
            _append_common_log(common_dir, f"source_prepare failed: {exc}")
            _mark_source_prepare(common_dir, "failed", request=request, error=str(exc))
            raise

        pipeline_args = [
            "--task-id",
            common_dir.name,
            "--outputs-dir",
            str(common_dir.parent),
            "--source-manifest",
            str(built["source_manifest"]),
            "--aspect-ratio",
            str(request.get("aspect_ratio") or args.aspect_ratio),
            "--chunk-seconds",
            str(request.get("chunk_seconds") or 60),
            "--frame-interval",
            str(request.get("frame_interval") or 10),
            "--mode",
            str(request.get("mode") or "normal"),
            "--production-mode",
            "ai_voiceover",
            "--common-only",
        ]
        if built.get("input_video"):
            pipeline_args.extend(["--input", str(built["input_video"])])
        
        if rerun and rerun in COMMON_REUSABLE_STEPS:
            pipeline_args.extend(["--rerun", rerun])
        if rerun_from and rerun_from in COMMON_REUSABLE_STEPS:
            pipeline_args.extend(["--rerun-from", rerun_from])
        try:
            _append_common_log(common_dir, "run common pipeline: " + " ".join(map(str, pipeline_args)))
            result = pipeline_main(pipeline_args)
            _append_common_log(common_dir, f"common pipeline finished with return code {result}")
            if result != 0:
                raise RuntimeError(f"common analysis failed with return code {result}")

            if not _common_analysis_ready(common_dir):
                raise RuntimeError("common analysis finished but timeline_digest is not ready")
            _append_common_log(common_dir, "common analysis ready")
        except Exception as exc:
            _append_common_log(common_dir, f"common analysis failed: {exc}")
            raise


def _common_analysis_ready(common_dir: Path) -> bool:
    manifest = read_json(common_dir / "manifest.json", {})
    steps = manifest.get("steps", {})
    return all(steps.get(step, {}).get("status") in {"success", "partial_success", "skipped"} for step in COMMON_REUSABLE_STEPS)


def _copy_common_outputs_to_task(*, common_dir: Path, task_dir: Path, request: dict[str, Any]) -> None:
    ensure_dir(task_dir)
    copied_dirs = ["input", "preprocess"] + COMMON_REUSABLE_STEPS
    for name in copied_dirs:
        src = common_dir / name
        if not src.exists():
            continue
        dst = task_dir / name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)

    ensure_dir(task_dir / "input")
    write_json(task_dir / "input" / "source_request.json", request)

    common_manifest = read_json(common_dir / "manifest.json", {})
    manifest = read_json(task_dir / "manifest.json", {})
    now = datetime.now().isoformat(timespec="seconds")

    manifest.setdefault("task_id", task_dir.name)
    manifest.setdefault("created_at", now)
    manifest["updated_at"] = now
    manifest["common_task_id"] = common_dir.name
    manifest["common_source_key"] = request.get("common_source_key", "")
    manifest["production_mode"] = request.get("production_mode", manifest.get("production_mode", "ai_voiceover"))

    for key in ("source_video", "source_mode", "source_manifest", "source_videos"):
        if key in common_manifest:
            manifest[key] = common_manifest[key]

    if "current_versions" in common_manifest:
        manifest.setdefault("current_versions", {}).update(common_manifest["current_versions"])

    manifest["reused_common_steps"] = COMMON_REUSABLE_STEPS
    manifest.setdefault("steps", {})

    for step in COMMON_REUSABLE_STEPS:
        status = dict((common_manifest.get("steps") or {}).get(step, {}))
        if status:
            status["reused_from"] = relpath(common_dir, ROOT)
            status["common_progress"] = True
            status["common_task_id"] = common_dir.name
            manifest["steps"][step] = status

    write_json(task_dir / "manifest.json", manifest)


def _run_mode_specific_pipeline(
    *,
    args: argparse.Namespace,
    passthrough: list[str],
    task_dir: Path,
    request: dict[str, Any],
) -> int:
    production_mode = str(request.get("production_mode") or _production_mode_from_passthrough(passthrough) or "ai_voiceover")
    start_step = "video_understanding" if production_mode == "highlight_reassembly" else "content_analysis"
    source_video = _try_resolve_task_manifest_path(task_dir, "source_video")
    source_manifest = _resolve_task_manifest_path(task_dir, "source_manifest")
    rerun = None
    rerun_from = None
    for i, arg in enumerate(passthrough):
        if arg == "--rerun" and i + 1 < len(passthrough):
            rerun = passthrough[i + 1]
        elif arg == "--rerun-from" and i + 1 < len(passthrough):
            rerun_from = passthrough[i + 1]
            
    child_rerun_args = []
    if rerun and rerun not in COMMON_REUSABLE_STEPS:
        child_rerun_args.extend(["--rerun", rerun])
    if rerun_from:
        if rerun_from not in COMMON_REUSABLE_STEPS:
            child_rerun_args.extend(["--rerun-from", rerun_from])
        else:
            # If rerun_from is in common steps, the common pipeline already reran it.
            # Downstream common steps were marked stale. We should force the child
            # pipeline to rerun from its start step so that it picks up the new common outputs.
            child_rerun_args.extend(["--rerun-from", start_step])
            
    # If the user requested to rerun a single common step, the common pipeline reran it.
    # The child pipeline needs to rerun from its start_step to pick up the changes.
    if rerun and rerun in COMMON_REUSABLE_STEPS:
        child_rerun_args.extend(["--rerun-from", start_step])
            
    # Note: If no rerun options are provided, we do NOT force `--rerun-from start_step`.
    # This allows normal resuming of child steps.
    
    pipeline_args = [
        "--task-id",
        args.task_id,
        "--source-manifest",
        str(source_manifest),
        "--aspect-ratio",
        str(request.get("aspect_ratio") or args.aspect_ratio),
        *child_rerun_args,
        *_strip_passthrough_rerun(passthrough),
    ]
    if source_video:
        pipeline_args.extend(["--input", str(source_video)])
    return pipeline_main(pipeline_args)


def _resolve_task_manifest_path(task_dir: Path, key: str) -> Path:
    manifest = read_json(task_dir / "manifest.json", {})
    value = str(manifest.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"manifest.{key} is missing")
    path = Path(value)
    return path if path.is_absolute() else task_dir / path


def _try_resolve_task_manifest_path(task_dir: Path, key: str) -> Path | None:
    manifest = read_json(task_dir / "manifest.json", {})
    value = str(manifest.get(key) or "").strip()
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else task_dir / path


def _production_mode_from_passthrough(passthrough: list[str]) -> str:
    for index, item in enumerate(passthrough):
        if item == "--production-mode" and index + 1 < len(passthrough):
            return passthrough[index + 1]
    return ""


def _strip_passthrough_rerun(passthrough: list[str]) -> list[str]:
    stripped: list[str] = []
    skip_next = False
    options_with_values = {"--rerun", "--rerun-from"}
    for item in passthrough:
        if skip_next:
            skip_next = False
            continue
        if item in options_with_values:
            skip_next = True
            continue
        stripped.append(item)
    return stripped


def _resolve_sources(request: dict[str, Any], config: Any) -> list[MultiSourceItem]:
    remote_config = load_remote_ucms_config(config)
    force_remote_download = bool(request.get("force_remote_download"))
    resolved: list[MultiSourceItem] = []
    for index, item in enumerate(request.get("source_items") or [], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"source_items[{index}] must be an object")
        source_type = str(item.get("source_type") or item.get("type") or "local")
        if source_type in {"local", "video"}:
            path_value = item.get("path") or item.get("input_video")
            if not path_value:
                raise ValueError(f"source_items[{index}].path is required")
            path = _resolve_project_file(str(path_value))
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
                raise ValueError(f"source_items[{index}].remote_video is required")
            cached = download_ucms_video(
                remote_config,
                remote_video,
                root_dir=ROOT,
                force=force_remote_download,
            )
            path = _resolve_project_file(str(cached["path"]))
            resolved.append(
                MultiSourceItem(
                    source_id=str(item.get("source_id") or remote_video.get("remote_id") or remote_video.get("id") or f"src_{index:03d}"),
                    source_type="remote_ucms",
                    path=path,
                    display_name=str(item.get("display_name") or remote_video.get("display_name") or remote_video.get("name") or path.name),
                    remote=remote_video,
                )
            )
            continue

        raise ValueError(f"unsupported source_type: {source_type}")
    if len(resolved) < 2:
        raise ValueError("multi-source run requires at least 2 source items")
    return resolved


def _resolve_project_file(value: str) -> Path:
    raw = Path(value)
    path = raw.resolve() if raw.is_absolute() else (ROOT / raw).resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"source video not found: {value}")
    root = ROOT.resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"source video must be inside project directory: {value}")
    return path


def _mark_source_prepare(
    task_dir: Path,
    status: str,
    *,
    request: dict[str, Any],
    built: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    manifest_path = task_dir / "manifest.json"
    manifest = read_json(manifest_path, {})
    now = datetime.now().isoformat(timespec="seconds")
    manifest.setdefault("task_id", task_dir.name)
    manifest.setdefault("created_at", now)
    manifest["updated_at"] = now
    manifest.setdefault("steps", {})
    output = ""
    if built:
        built_manifest = built.get("manifest") or {}
        manifest["source_mode"] = built_manifest.get("source_mode", "multi_source_concat_proxy")
        if built.get("input_video"):
            manifest["source_video"] = relpath(Path(built["input_video"]), task_dir)
        else:
            manifest["source_video"] = ""
        manifest["source_manifest"] = relpath(Path(built["source_manifest"]), task_dir)
        manifest["source_videos"] = built_manifest.get("sources", [])
        output = manifest["source_manifest"]
    manifest["steps"]["source_prepare"] = {
        "status": status,
        "output": output,
        "updated_at": now,
        "source_count": len(request.get("source_items") or []),
    }
    if error:
        manifest["steps"]["source_prepare"]["error"] = error
    write_json(manifest_path, manifest)


if __name__ == "__main__":
    raise SystemExit(main())
