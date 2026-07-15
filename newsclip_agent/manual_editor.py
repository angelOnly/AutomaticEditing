from __future__ import annotations

import copy
import json
import math
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .utils import read_json, stable_hash, timecode_to_seconds, write_json


SCHEMA_NAME = "newsclip.manual_editor"
SCHEMA_VERSION = 1
CACHE_FORMAT_VERSION = 1
EPSILON = 1e-6


class ManualEditorError(RuntimeError):
    """Base error raised by the manual editing core."""


class ProjectValidationError(ManualEditorError):
    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__("Invalid manual editing project:\n- " + "\n- ".join(self.errors))


class ProjectConflictError(ManualEditorError):
    """Raised when a browser tries to overwrite a newer saved revision."""


class ExportQualityError(ManualEditorError):
    def __init__(self, report: Mapping[str, Any]):
        self.report = dict(report)
        errors = report.get("errors") or ["unknown quality-control failure"]
        super().__init__("Export failed quality control:\n- " + "\n- ".join(map(str, errors)))


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _round_time(value: float) -> float:
    return round(float(value), 6)


def _project_clips(project: Mapping[str, Any]) -> list[dict[str, Any]]:
    timeline = project.get("timeline")
    if not isinstance(timeline, dict) or not isinstance(timeline.get("clips"), list):
        raise ProjectValidationError(["timeline.clips must be a list"])
    return timeline["clips"]


def _asset_map(project: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    assets = project.get("assets") or []
    return {
        str(asset.get("asset_id")): asset
        for asset in assets
        if isinstance(asset, dict) and asset.get("asset_id")
    }


def _parse_resolution(value: Any, aspect_ratio: str) -> tuple[int, int]:
    text = str(value or "").lower().replace("\u00d7", "x")
    try:
        width_text, height_text = text.split("x", 1)
        width, height = int(width_text), int(height_text)
        if width > 0 and height > 0:
            return width - width % 2, height - height % 2
    except (TypeError, ValueError):
        pass
    return (1080, 1920) if aspect_ratio == "9:16" else (1920, 1080)


def _load_document(value: str | Path | Mapping[str, Any], label: str) -> tuple[dict[str, Any], Path | None]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value)), None
    path = Path(value).expanduser().resolve()
    doc = read_json(path, None)
    if not isinstance(doc, dict):
        raise ManualEditorError(f"{label} is not a JSON object: {path}")
    return doc, path


def _infer_task_root(plan_path: Path | None, explicit: str | Path | None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if plan_path is None:
        return None
    for parent in plan_path.parents:
        if (parent / "manifest.json").is_file() or (parent / "input" / "source_manifest.json").is_file():
            return parent
        if parent.name == "edit":
            return parent.parent
    return plan_path.parent


def _resolve_media_path(value: Any, roots: Iterable[Path | None]) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = Path(text).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    root_list = [root for root in roots if root is not None]
    for root in root_list:
        resolved = (root / candidate).resolve()
        if resolved.exists():
            return resolved
    return (root_list[0] / candidate).resolve() if root_list else candidate.resolve()


def _discover_source_manifest(
    plan: Mapping[str, Any],
    *,
    task_root: Path | None,
    source_manifest: str | Path | Mapping[str, Any] | None,
) -> tuple[dict[str, Any], Path | None]:
    if source_manifest is not None:
        return _load_document(source_manifest, "source manifest")

    candidates: list[Path] = []
    manifest_value = plan.get("source_manifest")
    if manifest_value:
        resolved = _resolve_media_path(manifest_value, [task_root])
        if resolved:
            candidates.append(resolved)
    if task_root:
        task_manifest = read_json(task_root / "manifest.json", {})
        if isinstance(task_manifest, dict) and task_manifest.get("source_manifest"):
            resolved = _resolve_media_path(task_manifest["source_manifest"], [task_root])
            if resolved:
                candidates.append(resolved)
        candidates.append(task_root / "input" / "source_manifest.json")

    for candidate in candidates:
        doc = read_json(candidate, None)
        if isinstance(doc, dict):
            return doc, candidate.resolve()
    return {}, None


def _clip_source_bounds(clip: Mapping[str, Any]) -> tuple[float, float]:
    start = next(
        (
            timecode_to_seconds(clip.get(key))
            for key in ("local_start_seconds", "source_start_seconds", "local_start", "source_start")
            if clip.get(key) not in (None, "")
        ),
        0.0,
    )
    end = next(
        (
            timecode_to_seconds(clip.get(key))
            for key in ("local_end_seconds", "source_end_seconds", "local_end", "source_end")
            if clip.get(key) not in (None, "")
        ),
        0.0,
    )
    return float(start), float(end)


def initialize_project(
    plan: str | Path | Mapping[str, Any],
    *,
    source_manifest: str | Path | Mapping[str, Any] | None = None,
    source_paths: Mapping[str, str | Path] | None = None,
    reassembly_id: str | None = None,
    task_root: str | Path | None = None,
    project_id: str | None = None,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an independent, single-track project from a full-concat cut plan.

    The returned document contains source ranges and references only; it never
    changes the original cut plan or source media.
    """

    plan_doc, plan_path = _load_document(plan, "cut plan")
    output_videos = plan_doc.get("output_videos")
    if not isinstance(output_videos, list) or not output_videos:
        raise ManualEditorError("cut plan has no output_videos")
    if reassembly_id:
        video = next((item for item in output_videos if item.get("reassembly_id") == reassembly_id), None)
        if video is None:
            raise ManualEditorError(f"reassembly_id not found: {reassembly_id}")
    elif len(output_videos) == 1:
        video = output_videos[0]
    else:
        raise ManualEditorError("cut plan contains multiple videos; reassembly_id is required")
    if not isinstance(video, dict):
        raise ManualEditorError("selected output video is invalid")

    root = _infer_task_root(plan_path, task_root)
    source_manifest_doc, source_manifest_path = _discover_source_manifest(
        plan_doc, task_root=root, source_manifest=source_manifest
    )
    manifest_root = source_manifest_path.parent.parent if source_manifest_path else root
    source_overrides = {str(key): Path(value).expanduser().resolve() for key, value in (source_paths or {}).items()}
    manifest_sources = {
        str(item.get("source_id")): item
        for item in (source_manifest_doc.get("sources") or [])
        if isinstance(item, dict) and item.get("source_id")
    }
    raw_clips = video.get("clips") or []
    if not isinstance(raw_clips, list) or not raw_clips:
        raise ManualEditorError("selected output video has no clips")

    source_ids: list[str] = []
    for index, raw_clip in enumerate(raw_clips, start=1):
        source_id = str(raw_clip.get("source_id") or f"source_{index}")
        if source_id not in source_ids:
            source_ids.append(source_id)

    assets: list[dict[str, Any]] = []
    single_source = _resolve_media_path(plan_doc.get("source_video"), [root, plan_path.parent if plan_path else None])
    for source_id in source_ids:
        source_item = manifest_sources.get(source_id, {})
        media_path = source_overrides.get(source_id)
        if media_path is None and source_item:
            original = _resolve_media_path(source_item.get("original_path"), [manifest_root, root])
            normalized = _resolve_media_path(source_item.get("normalized_file"), [manifest_root, root])
            media_path = original if original and original.exists() else normalized or original
        if media_path is None and len(source_ids) == 1:
            media_path = single_source
        duration = source_item.get("duration_seconds") or source_item.get("original_duration_seconds")
        assets.append(
            {
                "asset_id": source_id,
                "source_id": source_id,
                "path": str(media_path) if media_path else "",
                "display_name": str(source_item.get("display_name") or (media_path.name if media_path else source_id)),
                "duration_seconds": _round_time(float(duration)) if duration not in (None, "") else None,
                "origin": "cut_plan",
            }
        )

    clips: list[dict[str, Any]] = []
    for index, raw_clip in enumerate(raw_clips, start=1):
        source_id = str(raw_clip.get("source_id") or source_ids[0])
        source_in, source_out = _clip_source_bounds(raw_clip)
        if source_out <= source_in:
            raise ManualEditorError(f"invalid source range in clip {raw_clip.get('clip_id') or index}")
        clips.append(
            {
                "clip_id": str(raw_clip.get("clip_id") or f"clip_{index:04d}"),
                "asset_id": source_id,
                "source_id": source_id,
                "source_in": _round_time(source_in),
                "source_out": _round_time(source_out),
                "duration_seconds": _round_time(source_out - source_in),
                "timeline_start": 0.0,
                "timeline_end": 0.0,
                "effects": [],
                "origin": "cut_plan",
                "metadata": {
                    key: copy.deepcopy(raw_clip.get(key))
                    for key in ("source_clip_id", "source_index", "role", "selection_reason", "crop_mode")
                    if raw_clip.get(key) not in (None, "")
                },
            }
        )

    aspect_ratio = str(video.get("aspect_ratio") or "16:9")
    width, height = _parse_resolution(video.get("target_resolution"), aspect_ratio)
    output_settings: dict[str, Any] = {
        "width": width,
        "height": height,
        "fps": 25,
        "sample_rate": 48000,
        "audio_channels": 2,
        "video_codec": "libx264",
        "audio_codec": "aac",
        "pixel_format": "yuv420p",
        "fit": "contain",
        "background": "black",
    }
    output_settings.update(dict(settings or {}))
    now = _now_iso()
    project: dict[str, Any] = {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "project_id": str(project_id or plan_doc.get("project_id") or _new_id("manual")),
        "revision": 0,
        "created_at": now,
        "updated_at": now,
        "source_plan": {
            "path": str(plan_path) if plan_path else "",
            "content_hash": stable_hash(plan_doc),
            "production_mode": plan_doc.get("production_mode", ""),
            "reassembly_id": video.get("reassembly_id", ""),
        },
        "settings": output_settings,
        "assets": assets,
        "timeline": {
            "mode": "single_track",
            "track_id": "video_1",
            "duration_seconds": 0.0,
            "clips": clips,
        },
    }
    _recalculate_timeline(project)
    validate_project(project)
    return project


def project_errors(project: Mapping[str, Any], *, check_files: bool = False) -> list[str]:
    errors: list[str] = []
    if not isinstance(project, Mapping):
        return ["project must be an object"]
    if project.get("schema") != SCHEMA_NAME:
        errors.append(f"schema must be {SCHEMA_NAME!r}")
    if project.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not str(project.get("project_id") or "").strip():
        errors.append("project_id is required")
    try:
        revision = int(project.get("revision"))
        if revision < 0:
            errors.append("revision must be non-negative")
    except (TypeError, ValueError):
        errors.append("revision must be an integer")

    settings = project.get("settings")
    if not isinstance(settings, Mapping):
        errors.append("settings must be an object")
        settings = {}
    for key in ("width", "height"):
        try:
            value = int(settings.get(key))
            if value < 2 or value % 2:
                errors.append(f"settings.{key} must be a positive even integer")
        except (TypeError, ValueError):
            errors.append(f"settings.{key} must be a positive even integer")
    try:
        fps = float(settings.get("fps"))
        if not 1 <= fps <= 120:
            errors.append("settings.fps must be between 1 and 120")
    except (TypeError, ValueError):
        errors.append("settings.fps must be a number")
    try:
        sample_rate = int(settings.get("sample_rate"))
        if not 8000 <= sample_rate <= 192000:
            errors.append("settings.sample_rate must be between 8000 and 192000")
    except (TypeError, ValueError):
        errors.append("settings.sample_rate must be an integer")
    try:
        channels = int(settings.get("audio_channels"))
        if channels not in (1, 2):
            errors.append("settings.audio_channels must be 1 or 2")
    except (TypeError, ValueError):
        errors.append("settings.audio_channels must be 1 or 2")

    assets = project.get("assets")
    if not isinstance(assets, list):
        errors.append("assets must be a list")
        assets = []
    asset_ids: set[str] = set()
    asset_map: dict[str, Mapping[str, Any]] = {}
    for index, asset in enumerate(assets):
        prefix = f"assets[{index}]"
        if not isinstance(asset, Mapping):
            errors.append(f"{prefix} must be an object")
            continue
        asset_id = str(asset.get("asset_id") or "")
        if not asset_id:
            errors.append(f"{prefix}.asset_id is required")
        elif asset_id in asset_ids:
            errors.append(f"duplicate asset_id: {asset_id}")
        else:
            asset_ids.add(asset_id)
            asset_map[asset_id] = asset
        path = str(asset.get("path") or "")
        if check_files and (not path or not Path(path).is_file()):
            errors.append(f"asset file does not exist: {asset_id or index} ({path or 'empty path'})")
        duration = asset.get("duration_seconds")
        if duration not in (None, ""):
            try:
                if float(duration) <= 0:
                    errors.append(f"{prefix}.duration_seconds must be positive")
            except (TypeError, ValueError):
                errors.append(f"{prefix}.duration_seconds must be a number or null")

    timeline = project.get("timeline")
    if not isinstance(timeline, Mapping):
        errors.append("timeline must be an object")
        return errors
    if timeline.get("mode") != "single_track":
        errors.append("timeline.mode must be 'single_track'")
    clips = timeline.get("clips")
    if not isinstance(clips, list):
        errors.append("timeline.clips must be a list")
        return errors

    clip_ids: set[str] = set()
    effect_ids: set[str] = set()
    cursor = 0.0
    for index, clip in enumerate(clips):
        prefix = f"timeline.clips[{index}]"
        if not isinstance(clip, Mapping):
            errors.append(f"{prefix} must be an object")
            continue
        clip_id = str(clip.get("clip_id") or "")
        if not clip_id:
            errors.append(f"{prefix}.clip_id is required")
        elif clip_id in clip_ids:
            errors.append(f"duplicate clip_id: {clip_id}")
        else:
            clip_ids.add(clip_id)
        asset_id = str(clip.get("asset_id") or "")
        if asset_id not in asset_map:
            errors.append(f"{prefix}.asset_id does not reference an asset: {asset_id}")
        try:
            source_in = float(clip.get("source_in"))
            source_out = float(clip.get("source_out"))
            duration = float(clip.get("duration_seconds"))
            timeline_start = float(clip.get("timeline_start"))
            timeline_end = float(clip.get("timeline_end"))
            expected_duration = source_out - source_in
            if source_in < -EPSILON or expected_duration <= EPSILON:
                errors.append(f"{prefix} has an invalid source range")
            if abs(duration - expected_duration) > 0.002:
                errors.append(f"{prefix}.duration_seconds does not match its source range")
            if abs(timeline_start - cursor) > 0.002:
                errors.append(f"{prefix}.timeline_start is not ripple-contiguous")
            if abs(timeline_end - (cursor + expected_duration)) > 0.002:
                errors.append(f"{prefix}.timeline_end is invalid")
            asset_duration = asset_map.get(asset_id, {}).get("duration_seconds")
            if asset_duration not in (None, "") and source_out > float(asset_duration) + 0.05:
                errors.append(f"{prefix}.source_out exceeds asset duration")
            cursor += expected_duration
        except (TypeError, ValueError):
            errors.append(f"{prefix} time values must be numbers")
            duration = 0.0

        effects = clip.get("effects")
        if not isinstance(effects, list):
            errors.append(f"{prefix}.effects must be a list")
            continue
        for effect_index, effect in enumerate(effects):
            effect_prefix = f"{prefix}.effects[{effect_index}]"
            if not isinstance(effect, Mapping):
                errors.append(f"{effect_prefix} must be an object")
                continue
            effect_id = str(effect.get("effect_id") or "")
            if not effect_id:
                errors.append(f"{effect_prefix}.effect_id is required")
            elif effect_id in effect_ids:
                errors.append(f"duplicate effect_id: {effect_id}")
            else:
                effect_ids.add(effect_id)
            if effect.get("type") != "mosaic":
                errors.append(f"{effect_prefix}.type must be 'mosaic'")
            try:
                effect_start = float(effect.get("start_seconds"))
                effect_end = float(effect.get("end_seconds"))
                if effect_start < -EPSILON or effect_end <= effect_start + EPSILON:
                    errors.append(f"{effect_prefix} has an invalid time range")
                if effect_end > duration + 0.002:
                    errors.append(f"{effect_prefix}.end_seconds exceeds clip duration")
            except (TypeError, ValueError):
                errors.append(f"{effect_prefix} time values must be numbers")
            try:
                x = float(effect.get("x"))
                y = float(effect.get("y"))
                width = float(effect.get("width"))
                height = float(effect.get("height"))
                if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
                    errors.append(f"{effect_prefix} rectangle must fit in normalized 0..1 coordinates")
            except (TypeError, ValueError):
                errors.append(f"{effect_prefix} rectangle values must be numbers")
            try:
                block_size = int(effect.get("block_size"))
                if not 2 <= block_size <= 256:
                    errors.append(f"{effect_prefix}.block_size must be between 2 and 256")
            except (TypeError, ValueError):
                errors.append(f"{effect_prefix}.block_size must be an integer")

    try:
        declared_duration = float(timeline.get("duration_seconds"))
        if abs(declared_duration - cursor) > 0.002:
            errors.append("timeline.duration_seconds does not match clips")
    except (TypeError, ValueError):
        errors.append("timeline.duration_seconds must be a number")
    return errors


def validate_project(project: Mapping[str, Any], *, check_files: bool = False) -> Mapping[str, Any]:
    errors = project_errors(project, check_files=check_files)
    if errors:
        raise ProjectValidationError(errors)
    return project


def load_project(path: str | Path, *, check_files: bool = False) -> dict[str, Any]:
    project, _ = _load_document(path, "manual editing project")
    validate_project(project, check_files=check_files)
    return project


def save_project(project: Mapping[str, Any], path: str | Path) -> Path:
    validate_project(project)
    return write_json(path, dict(project))


def save_project_revision(project: Mapping[str, Any], project_dir: str | Path) -> dict[str, Path]:
    """Atomically save the current project plus an immutable revision snapshot."""

    validate_project(project)
    root = Path(project_dir)
    revision = int(project["revision"])
    revision_path = root / "revisions" / f"v{revision:04d}.json"
    if revision_path.exists():
        existing = read_json(revision_path, None)
        if stable_hash(existing) != stable_hash(project):
            raise ManualEditorError(f"revision already exists with different content: {revision_path}")
    else:
        write_json(revision_path, dict(project))
    current_path = write_json(root / "project.json", dict(project))
    return {"project": current_path, "revision": revision_path}


def to_frontend_project(
    project: Mapping[str, Any],
    *,
    asset_urls: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return the flat, path-safe document consumed by ``editor.js``.

    Filesystem paths stay server-side. Effects are flattened because the UI
    renders one effect lane across the single video track.
    """

    return to_web_project(project, asset_urls=asset_urls)


def apply_frontend_project(
    current_project: Mapping[str, Any],
    frontend_project: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and apply an editor.js save payload to the current revision.

    The current server-side asset table is authoritative, preventing a browser
    payload from injecting arbitrary local paths.
    """

    return from_web_project(frontend_project, current_project)


def to_web_project(
    project: Mapping[str, Any],
    *,
    asset_urls: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Flatten the canonical project for the browser editor.

    The renderer keeps effects nested under clips, while the browser benefits
    from a single effect track. This adapter is the only schema boundary.
    """

    validate_project(project)
    urls = dict(asset_urls or {})
    public_source_plan = copy.deepcopy(project.get("source_plan") or {})
    public_source_plan.pop("path", None)
    public = {
        "schema": project.get("schema"),
        "schema_version": project.get("schema_version"),
        "project_id": project.get("project_id"),
        "revision": project.get("revision"),
        "created_at": project.get("created_at"),
        "updated_at": project.get("updated_at"),
        "source_plan": public_source_plan,
        "settings": copy.deepcopy(project.get("settings") or {}),
        "duration_seconds": project.get("timeline", {}).get("duration_seconds", 0),
        "assets": [],
        "clips": [],
        "effects": [],
    }
    for asset in project.get("assets") or []:
        item = copy.deepcopy(asset)
        item["label"] = item.get("display_name") or item.get("asset_id")
        item["preview_url"] = urls.get(str(item.get("asset_id")), "")
        # Never expose absolute server paths to the browser.
        item.pop("path", None)
        public["assets"].append(item)
    for clip in _project_clips(project):
        item = {key: copy.deepcopy(value) for key, value in clip.items() if key != "effects"}
        public["clips"].append(item)
        for effect in clip.get("effects") or []:
            public["effects"].append(
                {
                    "effect_id": effect.get("effect_id"),
                    "type": "mosaic",
                    "clip_id": clip.get("clip_id"),
                    "start": effect.get("start_seconds"),
                    "end": effect.get("end_seconds"),
                    "rect": {
                        "x": effect.get("x"),
                        "y": effect.get("y"),
                        "w": effect.get("width"),
                        "h": effect.get("height"),
                    },
                    "strength": effect.get("block_size", 18),
                    "group_id": effect.get("group_id", ""),
                }
            )
    return public


def from_web_project(web_project: Mapping[str, Any], base_project: Mapping[str, Any]) -> dict[str, Any]:
    """Validate browser state and rebuild the canonical render project.

    Asset paths and other trusted fields always come from ``base_project``;
    clients cannot submit arbitrary filesystem paths or raw FFmpeg filters.
    """

    validate_project(base_project)
    if not isinstance(web_project, Mapping):
        raise ProjectValidationError(["web project must be an object"])
    if str(web_project.get("project_id") or "") != str(base_project.get("project_id") or ""):
        raise ProjectValidationError(["web project_id does not match the current project"])
    if web_project.get("schema_version") != SCHEMA_VERSION:
        raise ProjectValidationError([f"web schema_version must be {SCHEMA_VERSION}"])
    try:
        web_revision = int(web_project.get("revision"))
    except (TypeError, ValueError) as exc:
        raise ProjectValidationError(["web project revision must be an integer"]) from exc
    base_revision = int(base_project.get("revision", 0))
    if web_revision != base_revision:
        raise ProjectConflictError(
            f"project revision conflict: expected v{base_revision}, got v{web_revision}"
        )
    submitted_clips = web_project.get("clips")
    submitted_effects = web_project.get("effects")
    if not isinstance(submitted_clips, list) or not isinstance(submitted_effects, list):
        raise ProjectValidationError(["clips and effects must be lists"])
    edited = copy.deepcopy(dict(base_project))
    trusted_assets = _asset_map(base_project)
    trusted_clips = {str(clip["clip_id"]): clip for clip in _project_clips(base_project)}
    seen_clip_ids: set[str] = set()
    clips: list[dict[str, Any]] = []
    for index, raw in enumerate(submitted_clips):
        if not isinstance(raw, Mapping):
            raise ProjectValidationError([f"clips[{index}] must be an object"])
        clip_id = str(raw.get("clip_id") or "")
        asset_id = str(raw.get("asset_id") or "")
        if not clip_id or clip_id in seen_clip_ids:
            raise ProjectValidationError([f"clips[{index}] has a missing or duplicate clip_id"])
        if asset_id not in trusted_assets:
            raise ProjectValidationError([f"clips[{index}].asset_id is not available"])
        seen_clip_ids.add(clip_id)
        try:
            source_in = float(raw.get("source_in"))
            source_out = float(raw.get("source_out"))
        except (TypeError, ValueError) as exc:
            raise ProjectValidationError([f"clips[{index}] has invalid source times"]) from exc
        asset_duration = trusted_assets[asset_id].get("duration_seconds")
        if source_in < 0 or source_out <= source_in + EPSILON:
            raise ProjectValidationError([f"clips[{index}] has an invalid source range"])
        if asset_duration not in (None, "") and source_out > float(asset_duration) + 0.05:
            raise ProjectValidationError([f"clips[{index}] exceeds its asset duration"])
        previous = trusted_clips.get(clip_id, {})
        clips.append(
            {
                "clip_id": clip_id,
                "asset_id": asset_id,
                "source_id": str(trusted_assets[asset_id].get("source_id") or asset_id),
                "source_in": _round_time(source_in),
                "source_out": _round_time(source_out),
                "duration_seconds": _round_time(source_out - source_in),
                "timeline_start": 0.0,
                "timeline_end": 0.0,
                "effects": [],
                "origin": str(previous.get("origin") or raw.get("origin") or "manual"),
                "metadata": copy.deepcopy(previous.get("metadata") or {}),
            }
        )
    by_clip = {str(clip["clip_id"]): clip for clip in clips}
    effect_ids: set[str] = set()
    for index, raw in enumerate(submitted_effects):
        if not isinstance(raw, Mapping):
            raise ProjectValidationError([f"effects[{index}] must be an object"])
        if raw.get("type") != "mosaic":
            raise ProjectValidationError([f"effects[{index}].type must be 'mosaic'"])
        effect_id = str(raw.get("effect_id") or "")
        clip_id = str(raw.get("clip_id") or "")
        if not effect_id or effect_id in effect_ids or clip_id not in by_clip:
            raise ProjectValidationError([f"effects[{index}] has an invalid id or clip_id"])
        effect_ids.add(effect_id)
        rect = raw.get("rect") if isinstance(raw.get("rect"), Mapping) else {}
        try:
            effect = _mosaic_effect(
                start_seconds=float(raw.get("start")),
                end_seconds=float(raw.get("end")),
                x=float(rect.get("x")),
                y=float(rect.get("y")),
                width=float(rect.get("w")),
                height=float(rect.get("h")),
                block_size=max(2, min(int(raw.get("strength") or 18), 256)),
                effect_id=effect_id,
            )
        except (TypeError, ValueError) as exc:
            raise ProjectValidationError([f"effects[{index}] has invalid values"]) from exc
        if raw.get("group_id"):
            effect["group_id"] = str(raw.get("group_id"))
        by_clip[clip_id]["effects"].append(effect)
    edited["timeline"]["clips"] = clips
    return _finish_edit(edited)


def _recalculate_timeline(project: dict[str, Any]) -> None:
    cursor = 0.0
    for clip in _project_clips(project):
        duration = float(clip["source_out"]) - float(clip["source_in"])
        clip["source_in"] = _round_time(float(clip["source_in"]))
        clip["source_out"] = _round_time(float(clip["source_out"]))
        clip["duration_seconds"] = _round_time(duration)
        clip["timeline_start"] = _round_time(cursor)
        cursor += duration
        clip["timeline_end"] = _round_time(cursor)
    project["timeline"]["duration_seconds"] = _round_time(cursor)


def _finish_edit(project: dict[str, Any]) -> dict[str, Any]:
    _recalculate_timeline(project)
    project["revision"] = int(project.get("revision", 0)) + 1
    project["updated_at"] = _now_iso()
    validate_project(project)
    return project


def _find_clip_index(project: Mapping[str, Any], clip_id: str) -> int:
    for index, clip in enumerate(_project_clips(project)):
        if clip.get("clip_id") == clip_id:
            return index
    raise ManualEditorError(f"clip not found: {clip_id}")


def _slice_effects(
    effects: Sequence[Mapping[str, Any]],
    keep_start: float,
    keep_end: float,
    *,
    preserve_ids: bool,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw_effect in effects:
        start = float(raw_effect["start_seconds"])
        end = float(raw_effect["end_seconds"])
        overlap_start = max(start, keep_start)
        overlap_end = min(end, keep_end)
        if overlap_end <= overlap_start + EPSILON:
            continue
        effect = copy.deepcopy(dict(raw_effect))
        if not preserve_ids:
            effect["effect_id"] = _new_id("mosaic")
        effect["start_seconds"] = _round_time(overlap_start - keep_start)
        effect["end_seconds"] = _round_time(overlap_end - keep_start)
        result.append(effect)
    return result


def _slice_clip(
    clip: Mapping[str, Any],
    keep_start: float,
    keep_end: float,
    *,
    preserve_id: bool,
) -> dict[str, Any]:
    duration = float(clip["duration_seconds"])
    if keep_start < -EPSILON or keep_end > duration + EPSILON or keep_end <= keep_start + EPSILON:
        raise ManualEditorError("invalid clip slice")
    sliced = copy.deepcopy(dict(clip))
    if not preserve_id:
        sliced["clip_id"] = _new_id("clip")
    sliced["source_in"] = _round_time(float(clip["source_in"]) + keep_start)
    sliced["source_out"] = _round_time(float(clip["source_in"]) + keep_end)
    sliced["effects"] = _slice_effects(
        clip.get("effects") or [], keep_start, keep_end, preserve_ids=preserve_id
    )
    return sliced


def split_clip(project: Mapping[str, Any], clip_id: str, at_seconds: float) -> dict[str, Any]:
    """Split a clip at a clip-local offset in seconds."""

    edited = copy.deepcopy(dict(project))
    validate_project(edited)
    index = _find_clip_index(edited, clip_id)
    clips = _project_clips(edited)
    clip = clips[index]
    at = float(at_seconds)
    duration = float(clip["duration_seconds"])
    if at <= EPSILON or at >= duration - EPSILON:
        raise ManualEditorError("split point must be inside the clip")
    left = _slice_clip(clip, 0.0, at, preserve_id=True)
    right = _slice_clip(clip, at, duration, preserve_id=False)
    clips[index : index + 1] = [left, right]
    return _finish_edit(edited)


def trim_clip(
    project: Mapping[str, Any],
    clip_id: str,
    *,
    source_in: float | None = None,
    source_out: float | None = None,
) -> dict[str, Any]:
    """Shorten a clip by setting new absolute source in/out points."""

    edited = copy.deepcopy(dict(project))
    validate_project(edited)
    index = _find_clip_index(edited, clip_id)
    clip = _project_clips(edited)[index]
    old_in = float(clip["source_in"])
    old_out = float(clip["source_out"])
    new_in = old_in if source_in is None else float(source_in)
    new_out = old_out if source_out is None else float(source_out)
    if new_in < old_in - EPSILON or new_out > old_out + EPSILON:
        raise ManualEditorError("trim_clip cannot expand beyond the current source range")
    if new_out <= new_in + EPSILON:
        raise ManualEditorError("trim result must have positive duration")
    _project_clips(edited)[index] = _slice_clip(
        clip, new_in - old_in, new_out - old_in, preserve_id=True
    )
    return _finish_edit(edited)


def ripple_delete(project: Mapping[str, Any], start_seconds: float, end_seconds: float) -> dict[str, Any]:
    """Delete a timeline interval and ripple all later clips to the left."""

    edited = copy.deepcopy(dict(project))
    validate_project(edited)
    start, end = float(start_seconds), float(end_seconds)
    total = float(edited["timeline"]["duration_seconds"])
    if start < -EPSILON or end <= start + EPSILON or end > total + EPSILON:
        raise ManualEditorError(f"delete range must satisfy 0 <= start < end <= {total:.6f}")
    start, end = max(0.0, start), min(total, end)
    result: list[dict[str, Any]] = []
    for clip in _project_clips(edited):
        clip_start = float(clip["timeline_start"])
        clip_end = float(clip["timeline_end"])
        if clip_end <= start + EPSILON or clip_start >= end - EPSILON:
            result.append(clip)
            continue
        duration = float(clip["duration_seconds"])
        local_delete_start = max(0.0, start - clip_start)
        local_delete_end = min(duration, end - clip_start)
        if local_delete_start > EPSILON:
            result.append(_slice_clip(clip, 0.0, local_delete_start, preserve_id=True))
        if local_delete_end < duration - EPSILON:
            result.append(
                _slice_clip(
                    clip,
                    local_delete_end,
                    duration,
                    preserve_id=local_delete_start <= EPSILON,
                )
            )
    edited["timeline"]["clips"] = result
    return _finish_edit(edited)


def add_asset(
    project: Mapping[str, Any],
    path: str | Path,
    *,
    asset_id: str | None = None,
    source_id: str | None = None,
    display_name: str | None = None,
    duration_seconds: float | None = None,
    probe: bool = False,
    ffprobe: str = "ffprobe",
) -> dict[str, Any]:
    """Add an uploaded/local media asset without changing the timeline."""

    edited = copy.deepcopy(dict(project))
    validate_project(edited)
    media_path = Path(path).expanduser().resolve()
    if not media_path.is_file():
        raise ManualEditorError(f"asset file does not exist: {media_path}")
    new_asset_id = str(asset_id or source_id or _new_id("asset"))
    if new_asset_id in _asset_map(edited):
        raise ManualEditorError(f"asset_id already exists: {new_asset_id}")
    metadata = _probe_media(media_path, ffprobe) if probe or duration_seconds is None else {}
    duration = float(duration_seconds if duration_seconds is not None else metadata.get("duration") or 0)
    if duration <= 0:
        raise ManualEditorError(f"asset has invalid duration: {media_path}")
    edited["assets"].append(
        {
            "asset_id": new_asset_id,
            "source_id": str(source_id or new_asset_id),
            "path": str(media_path),
            "display_name": display_name or media_path.name,
            "duration_seconds": _round_time(duration),
            "origin": "added",
        }
    )
    return _finish_edit(edited)


def insert_clip(
    project: Mapping[str, Any],
    asset_id: str,
    source_in: float,
    source_out: float,
    *,
    index: int | None = None,
    timeline_seconds: float | None = None,
    clip_id: str | None = None,
) -> dict[str, Any]:
    """Insert a source range by index or at a timeline position.

    Insertion inside an existing clip automatically splits that clip.
    """

    if index is not None and timeline_seconds is not None:
        raise ManualEditorError("use either index or timeline_seconds, not both")
    edited = copy.deepcopy(dict(project))
    validate_project(edited)
    assets = _asset_map(edited)
    if asset_id not in assets:
        raise ManualEditorError(f"asset not found: {asset_id}")
    source_in_value, source_out_value = float(source_in), float(source_out)
    if source_in_value < -EPSILON or source_out_value <= source_in_value + EPSILON:
        raise ManualEditorError("inserted source range is invalid")
    asset_duration = assets[asset_id].get("duration_seconds")
    if asset_duration not in (None, "") and source_out_value > float(asset_duration) + 0.05:
        raise ManualEditorError("inserted source range exceeds asset duration")

    clips = _project_clips(edited)
    if timeline_seconds is not None:
        position = float(timeline_seconds)
        total = float(edited["timeline"]["duration_seconds"])
        if position < -EPSILON or position > total + EPSILON:
            raise ManualEditorError(f"timeline insertion point must be between 0 and {total:.6f}")
        position = min(max(position, 0.0), total)
        insert_index = len(clips)
        for current_index, current in enumerate(list(clips)):
            clip_start = float(current["timeline_start"])
            clip_end = float(current["timeline_end"])
            if abs(position - clip_start) <= EPSILON:
                insert_index = current_index
                break
            if clip_start < position < clip_end:
                offset = position - clip_start
                duration = float(current["duration_seconds"])
                left = _slice_clip(current, 0.0, offset, preserve_id=True)
                right = _slice_clip(current, offset, duration, preserve_id=False)
                clips[current_index : current_index + 1] = [left, right]
                insert_index = current_index + 1
                break
            if abs(position - clip_end) <= EPSILON:
                insert_index = current_index + 1
    else:
        insert_index = len(clips) if index is None else int(index)
        if insert_index < 0 or insert_index > len(clips):
            raise ManualEditorError(f"insert index must be between 0 and {len(clips)}")

    asset = assets[asset_id]
    clips.insert(
        insert_index,
        {
            "clip_id": str(clip_id or _new_id("clip")),
            "asset_id": asset_id,
            "source_id": str(asset.get("source_id") or asset_id),
            "source_in": _round_time(source_in_value),
            "source_out": _round_time(source_out_value),
            "duration_seconds": _round_time(source_out_value - source_in_value),
            "timeline_start": 0.0,
            "timeline_end": 0.0,
            "effects": [],
            "origin": "inserted",
            "metadata": {},
        },
    )
    return _finish_edit(edited)


def move_clip(project: Mapping[str, Any], clip_id: str, new_index: int) -> dict[str, Any]:
    edited = copy.deepcopy(dict(project))
    validate_project(edited)
    clips = _project_clips(edited)
    old_index = _find_clip_index(edited, clip_id)
    target = int(new_index)
    if target < 0 or target >= len(clips):
        raise ManualEditorError(f"new_index must be between 0 and {len(clips) - 1}")
    clip = clips.pop(old_index)
    clips.insert(target, clip)
    return _finish_edit(edited)


def reorder_clips(project: Mapping[str, Any], clip_ids: Sequence[str]) -> dict[str, Any]:
    edited = copy.deepcopy(dict(project))
    validate_project(edited)
    clips = _project_clips(edited)
    requested = list(map(str, clip_ids))
    current = [str(clip["clip_id"]) for clip in clips]
    if len(requested) != len(current) or len(set(requested)) != len(requested) or set(requested) != set(current):
        raise ManualEditorError("clip_ids must be a duplicate-free permutation of the current timeline")
    by_id = {str(clip["clip_id"]): clip for clip in clips}
    edited["timeline"]["clips"] = [by_id[clip_id] for clip_id in requested]
    return _finish_edit(edited)


def _mosaic_effect(
    *,
    start_seconds: float,
    end_seconds: float,
    x: float,
    y: float,
    width: float,
    height: float,
    block_size: int,
    effect_id: str | None,
) -> dict[str, Any]:
    return {
        "effect_id": str(effect_id or _new_id("mosaic")),
        "type": "mosaic",
        "start_seconds": _round_time(float(start_seconds)),
        "end_seconds": _round_time(float(end_seconds)),
        "x": round(float(x), 6),
        "y": round(float(y), 6),
        "width": round(float(width), 6),
        "height": round(float(height), 6),
        "coordinate_space": "normalized_output",
        "block_size": int(block_size),
    }


def add_mosaic_effect(
    project: Mapping[str, Any],
    clip_id: str,
    *,
    start_seconds: float,
    end_seconds: float,
    x: float,
    y: float,
    width: float,
    height: float,
    block_size: int = 18,
    effect_id: str | None = None,
) -> dict[str, Any]:
    """Add a fixed normalized rectangle for a clip-local time interval."""

    edited = copy.deepcopy(dict(project))
    validate_project(edited)
    clip = _project_clips(edited)[_find_clip_index(edited, clip_id)]
    effect = _mosaic_effect(
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        x=x,
        y=y,
        width=width,
        height=height,
        block_size=block_size,
        effect_id=effect_id,
    )
    clip.setdefault("effects", []).append(effect)
    return _finish_edit(edited)


def add_timeline_mosaic_effect(
    project: Mapping[str, Any],
    *,
    start_seconds: float,
    end_seconds: float,
    x: float,
    y: float,
    width: float,
    height: float,
    block_size: int = 18,
) -> dict[str, Any]:
    """Add one fixed mosaic selection across every overlapping clip."""

    edited = copy.deepcopy(dict(project))
    validate_project(edited)
    start, end = float(start_seconds), float(end_seconds)
    total = float(edited["timeline"]["duration_seconds"])
    if start < -EPSILON or end <= start + EPSILON or end > total + EPSILON:
        raise ManualEditorError(f"effect range must satisfy 0 <= start < end <= {total:.6f}")
    added = 0
    group_id = _new_id("mosaic_group")
    for clip in _project_clips(edited):
        clip_start = float(clip["timeline_start"])
        clip_end = float(clip["timeline_end"])
        overlap_start = max(start, clip_start)
        overlap_end = min(end, clip_end)
        if overlap_end <= overlap_start + EPSILON:
            continue
        effect = _mosaic_effect(
            start_seconds=overlap_start - clip_start,
            end_seconds=overlap_end - clip_start,
            x=x,
            y=y,
            width=width,
            height=height,
            block_size=block_size,
            effect_id=None,
        )
        effect["group_id"] = group_id
        clip.setdefault("effects", []).append(effect)
        added += 1
    if not added:
        raise ManualEditorError("effect range does not overlap a clip")
    return _finish_edit(edited)


def remove_effect(project: Mapping[str, Any], effect_id: str) -> dict[str, Any]:
    edited = copy.deepcopy(dict(project))
    validate_project(edited)
    removed = False
    for clip in _project_clips(edited):
        effects = clip.get("effects") or []
        kept = [effect for effect in effects if effect.get("effect_id") != effect_id]
        if len(kept) != len(effects):
            clip["effects"] = kept
            removed = True
    if not removed:
        raise ManualEditorError(f"effect not found: {effect_id}")
    return _finish_edit(edited)


def _run_command(command: Sequence[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        list(command),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if process.returncode:
        detail = (process.stderr or process.stdout or "no command output").strip()
        raise ManualEditorError(f"command failed ({process.returncode}): {command[0]}\n{detail}")
    return process


def _probe_media(path: str | Path, ffprobe: str = "ffprobe", *, timeout: float | None = None) -> dict[str, Any]:
    if not shutil.which(ffprobe) and not Path(ffprobe).is_file():
        raise ManualEditorError(f"ffprobe executable not found: {ffprobe}")
    process = _run_command(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=timeout,
    )
    try:
        raw = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ManualEditorError(f"ffprobe returned invalid JSON for {path}") from exc
    streams = raw.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    duration_value = (raw.get("format") or {}).get("duration")
    if duration_value in (None, "") and video:
        duration_value = video.get("duration")
    if duration_value in (None, "") and audio:
        duration_value = audio.get("duration")
    return {
        "duration": float(duration_value or 0),
        "video": video,
        "audio": audio,
        "streams": streams,
        "format": raw.get("format") or {},
        "raw": raw,
    }


def _fraction(value: Any) -> float:
    text = str(value or "0")
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return float(numerator) / float(denominator) if float(denominator) else 0.0
        return float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _even_floor(value: float) -> int:
    integer = max(0, int(math.floor(value)))
    return integer - integer % 2


def _effect_rect_pixels(effect: Mapping[str, Any], width: int, height: int) -> tuple[int, int, int, int]:
    x = _even_floor(float(effect["x"]) * width)
    y = _even_floor(float(effect["y"]) * height)
    rect_width = max(2, _even_floor(float(effect["width"]) * width))
    rect_height = max(2, _even_floor(float(effect["height"]) * height))
    if x + rect_width > width:
        rect_width = max(2, width - x - (width - x) % 2)
    if y + rect_height > height:
        rect_height = max(2, height - y - (height - y) % 2)
    return x, y, rect_width, rect_height


def _video_filter_graph(clip: Mapping[str, Any], settings: Mapping[str, Any]) -> str:
    width = int(settings["width"])
    height = int(settings["height"])
    fps = float(settings["fps"])
    background = str(settings.get("background") or "black").replace("'", "")
    base = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={background},"
        f"setsar=1,fps={fps:.6f},setpts=PTS-STARTPTS"
    )
    parts = [f"[0:v:0]{base}[base0]"]
    current = "base0"
    effects = sorted(clip.get("effects") or [], key=lambda effect: (effect["start_seconds"], effect["effect_id"]))
    for index, effect in enumerate(effects):
        x, y, rect_width, rect_height = _effect_rect_pixels(effect, width, height)
        block_size = int(effect["block_size"])
        down_width = max(2, _even_floor(rect_width / block_size))
        down_height = max(2, _even_floor(rect_height / block_size))
        parts.append(f"[{current}]split=2[bg{index}][cropin{index}]")
        parts.append(
            f"[cropin{index}]crop={rect_width}:{rect_height}:{x}:{y},"
            f"scale={down_width}:{down_height}:flags=area,"
            f"scale={rect_width}:{rect_height}:flags=neighbor[pixel{index}]"
        )
        start = float(effect["start_seconds"])
        end = float(effect["end_seconds"])
        parts.append(
            f"[bg{index}][pixel{index}]overlay={x}:{y}:"
            f"enable='between(t,{start:.6f},{end:.6f})'[effect{index}]"
        )
        current = f"effect{index}"
    parts.append(f"[{current}]format={settings.get('pixel_format') or 'yuv420p'}[vout]")
    return ";".join(parts)


def _cache_key(
    clip: Mapping[str, Any],
    asset: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> str:
    path = Path(str(asset["path"]))
    stat = path.stat()
    return stable_hash(
        {
            "cache_format": CACHE_FORMAT_VERSION,
            "path": str(path.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "source_in": clip["source_in"],
            "source_out": clip["source_out"],
            "effects": clip.get("effects") or [],
            "settings": dict(settings),
        }
    )


def _render_cached_clip(
    clip: Mapping[str, Any],
    asset: Mapping[str, Any],
    cache_path: Path,
    *,
    settings: Mapping[str, Any],
    probe: Mapping[str, Any],
    ffmpeg: str,
    preset: str,
    crf: int,
    timeout: float | None,
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    source_path = Path(str(asset["path"]))
    duration = float(clip["duration_seconds"])
    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{float(clip['source_in']):.6f}",
        "-t",
        f"{duration:.6f}",
        "-i",
        str(source_path),
    ]
    has_audio = bool(probe.get("audio"))
    sample_rate = int(settings["sample_rate"])
    channels = int(settings["audio_channels"])
    channel_layout = "mono" if channels == 1 else "stereo"
    if not has_audio:
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=channel_layout={channel_layout}:sample_rate={sample_rate}",
            ]
        )
    audio_input = "0:a:0" if has_audio else "1:a:0"
    temporary = cache_path.with_name(f".{cache_path.stem}.{uuid.uuid4().hex}.tmp.mp4")
    command.extend(
        [
            "-filter_complex",
            _video_filter_graph(clip, settings),
            "-map",
            "[vout]",
            "-map",
            audio_input,
            "-af",
            (
                f"aresample={sample_rate}:async=1:first_pts=0,"
                f"apad,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS"
            ),
            "-t",
            f"{duration:.6f}",
            "-c:v",
            str(settings.get("video_codec") or "libx264"),
            "-preset",
            preset,
            "-crf",
            str(int(crf)),
            "-pix_fmt",
            str(settings.get("pixel_format") or "yuv420p"),
            "-c:a",
            str(settings.get("audio_codec") or "aac"),
            "-b:a",
            "192k",
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-map_metadata",
            "-1",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
    )
    try:
        _run_command(command, timeout=timeout)
        os.replace(temporary, cache_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _concat_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")


def quality_check_export(
    project: Mapping[str, Any],
    output_path: str | Path,
    *,
    ffprobe: str = "ffprobe",
    timeout: float | None = None,
) -> dict[str, Any]:
    validate_project(project)
    settings = project["settings"]
    probe = _probe_media(output_path, ffprobe, timeout=timeout)
    video = probe.get("video") or {}
    audio = probe.get("audio") or {}
    expected_duration = float(project["timeline"]["duration_seconds"])
    actual_duration = float(probe.get("duration") or 0)
    expected_fps = float(settings["fps"])
    actual_fps = _fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    expected_sar = "1:1"
    errors: list[str] = []
    if not video:
        errors.append("missing video stream")
    if not audio:
        errors.append("missing audio stream")
    if video and (int(video.get("width") or 0), int(video.get("height") or 0)) != (
        int(settings["width"]),
        int(settings["height"]),
    ):
        errors.append("output resolution does not match project settings")
    if video and abs(actual_fps - expected_fps) > 0.02:
        errors.append("output frame rate does not match project settings")
    if video and str(video.get("sample_aspect_ratio") or "") not in (expected_sar, "1/1"):
        errors.append("output sample aspect ratio is not 1:1")
    if video and video.get("pix_fmt") != str(settings.get("pixel_format") or "yuv420p"):
        errors.append("output pixel format does not match project settings")
    if audio and audio.get("codec_name") != "aac":
        errors.append("output audio codec is not AAC")
    if audio and int(audio.get("sample_rate") or 0) != int(settings["sample_rate"]):
        errors.append("output audio sample rate does not match project settings")
    if audio and int(audio.get("channels") or 0) != int(settings["audio_channels"]):
        errors.append("output audio channel count does not match project settings")
    duration_tolerance = max(0.15, 3.0 / expected_fps)
    duration_delta = abs(actual_duration - expected_duration)
    if duration_delta > duration_tolerance:
        errors.append(
            f"output duration differs by {duration_delta:.3f}s (allowed {duration_tolerance:.3f}s)"
        )
    return {
        "status": "ok" if not errors else "mismatch",
        "errors": errors,
        "output_file": str(Path(output_path).resolve()),
        "expected_duration_seconds": _round_time(expected_duration),
        "actual_duration_seconds": _round_time(actual_duration),
        "duration_delta_seconds": _round_time(duration_delta),
        "duration_tolerance_seconds": _round_time(duration_tolerance),
        "has_video": bool(video),
        "has_audio": bool(audio),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(actual_fps, 3),
        "sample_aspect_ratio": video.get("sample_aspect_ratio", ""),
        "pixel_format": video.get("pix_fmt", ""),
        "video_codec": video.get("codec_name", ""),
        "audio_codec": audio.get("codec_name", ""),
        "audio_sample_rate": int(audio.get("sample_rate") or 0),
        "audio_channels": int(audio.get("channels") or 0),
    }


def export_project(
    project: Mapping[str, Any],
    output_path: str | Path,
    *,
    cache_dir: str | Path | None = None,
    work_dir: str | Path | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    preset: str = "veryfast",
    crf: int = 18,
    timeout: float | None = None,
    strict_qc: bool = True,
) -> dict[str, Any]:
    """Render and concatenate all clips, then run strict ffprobe quality control."""

    validate_project(project, check_files=True)
    clips = _project_clips(project)
    if not clips:
        raise ManualEditorError("cannot export an empty timeline")
    if not shutil.which(ffmpeg) and not Path(ffmpeg).is_file():
        raise ManualEditorError(f"ffmpeg executable not found: {ffmpeg}")
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    cache_root = Path(cache_dir).expanduser().resolve() if cache_dir else output.parent / ".manual_editor_cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    assets = _asset_map(project)
    probes: dict[str, dict[str, Any]] = {}
    rendered_count = 0
    cached_count = 0
    clip_files: list[Path] = []
    for clip in clips:
        asset = assets[str(clip["asset_id"])]
        asset_path = Path(str(asset["path"]))
        if str(asset["asset_id"]) not in probes:
            probes[str(asset["asset_id"])] = _probe_media(asset_path, ffprobe, timeout=timeout)
        probe = probes[str(asset["asset_id"])]
        if not probe.get("video"):
            raise ManualEditorError(f"asset has no video stream: {asset_path}")
        if float(clip["source_out"]) > float(probe.get("duration") or 0) + 0.05:
            raise ManualEditorError(f"clip {clip['clip_id']} exceeds actual asset duration")
        cache_path = cache_root / f"{_cache_key(clip, asset, project['settings'])}.mp4"
        if cache_path.is_file() and cache_path.stat().st_size > 0:
            cached_count += 1
        else:
            _render_cached_clip(
                clip,
                asset,
                cache_path,
                settings=project["settings"],
                probe=probe,
                ffmpeg=ffmpeg,
                preset=preset,
                crf=int(crf),
                timeout=timeout,
            )
            rendered_count += 1
        clip_files.append(cache_path)

    work_parent = Path(work_dir).expanduser().resolve() if work_dir else output.parent
    work_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="manual_editor_", dir=work_parent) as temporary_directory:
        temporary_root = Path(temporary_directory)
        concat_file = temporary_root / "concat.txt"
        concat_file.write_text(
            "".join(f"file '{_concat_path(path)}'\n" for path in clip_files), encoding="utf-8"
        )
        temporary_output = temporary_root / "export.mp4"
        _run_command(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(temporary_output),
            ],
            timeout=timeout,
        )
        os.replace(temporary_output, output)

    report = quality_check_export(project, output, ffprobe=ffprobe, timeout=timeout)
    report.update(
        {
            "clip_count": len(clips),
            "rendered_clip_count": rendered_count,
            "cached_clip_count": cached_count,
            "cache_dir": str(cache_root),
        }
    )
    write_json(output.with_suffix(output.suffix + ".qc.json"), report)
    if strict_qc and report["status"] != "ok":
        raise ExportQualityError(report)
    return report


__all__ = [
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "ExportQualityError",
    "ManualEditorError",
    "ProjectConflictError",
    "ProjectValidationError",
    "add_asset",
    "add_mosaic_effect",
    "add_timeline_mosaic_effect",
    "apply_frontend_project",
    "export_project",
    "from_web_project",
    "initialize_project",
    "insert_clip",
    "load_project",
    "move_clip",
    "project_errors",
    "quality_check_export",
    "remove_effect",
    "reorder_clips",
    "ripple_delete",
    "save_project",
    "save_project_revision",
    "split_clip",
    "trim_clip",
    "to_frontend_project",
    "to_web_project",
    "validate_project",
]
