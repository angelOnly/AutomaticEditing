from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from newsclip_agent.config import load_config
from newsclip_agent.multisource import MultiSourceItem, build_multi_source_video
from newsclip_agent.pipeline import main as pipeline_main
from newsclip_agent.remote_ucms import download_ucms_video, load_remote_ucms_config
from newsclip_agent.utils import ensure_dir, read_json, relpath, write_json


ROOT = Path(__file__).resolve().parent
OUTPUTS_DIR = ROOT / "outputs"


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
    _mark_source_prepare(task_dir, "running", request=request)
    try:
        config = load_config(ROOT / "config.toml")
        sources = _resolve_sources(request, config)
        multi_source_config = config.raw.get("multi_source", {})
        built = build_multi_source_video(
            sources=sources,
            task_dir=task_dir,
            root_dir=ROOT,
            aspect_ratio=str(request.get("aspect_ratio") or args.aspect_ratio),
            fps=int(multi_source_config.get("fps", 25)),
            sample_rate=int(multi_source_config.get("sample_rate", 48000)),
        )
        _mark_source_prepare(task_dir, "success", request=request, built=built)
    except Exception as exc:
        _mark_source_prepare(task_dir, "failed", request=request, error=str(exc))
        raise

    pipeline_args = [
        "--task-id",
        args.task_id,
        "--input",
        str(built["input_video"]),
        "--source-manifest",
        str(built["source_manifest"]),
        "--aspect-ratio",
        str(request.get("aspect_ratio") or args.aspect_ratio),
        *passthrough,
    ]
    return pipeline_main(pipeline_args)


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
        manifest["source_video"] = relpath(Path(built["input_video"]), task_dir)
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
