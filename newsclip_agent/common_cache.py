"""Shared cache-key helpers for multi-source analysis.

The web request builder and the worker must derive the same key.  Keeping the
semantic contract in the key prevents an old common pool (built with a weaker
ASR/visual attribution policy) from being reused after an algorithm upgrade.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .analysis_contracts import analysis_contract_profile
from .utils import stable_hash


def source_fingerprint(item: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    source_type = str(item.get("source_type") or item.get("type") or "local")
    path_value = item.get("path") or item.get("input_video") or item.get("original_path")
    path = Path(str(path_value)) if path_value else None
    if path is not None and not path.is_absolute() and root is not None:
        path = (root / path).resolve()
    if path is not None:
        try:
            stat = path.stat()
            return {
                "source_type": "local",
                "path": str(path.resolve()),
                "size_bytes": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
            }
        except OSError:
            return {"source_type": "local", "path": str(path.resolve())}
    remote = item.get("remote_video") or item.get("remote") or item
    if isinstance(remote, dict):
        return {
            "source_type": source_type,
            "remote_id": remote.get("id") or remote.get("video_id") or remote.get("guid") or "",
            "etag": remote.get("etag") or remote.get("checksum") or remote.get("md5") or "",
            "url": remote.get("url") or remote.get("download_url") or "",
            "name": remote.get("name") or remote.get("title") or "",
        }
    return {"source_type": source_type, "value": str(remote)}


def build_common_analysis_profile(request: dict[str, Any], *, config: Any | None = None) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "contracts": analysis_contract_profile(),
        "production_mode": request.get("production_mode") or "ai_voiceover",
        "aspect_ratio": request.get("aspect_ratio") or "16:9",
        "chunk_seconds": int(request.get("chunk_seconds") or 60),
        "frame_interval": int(request.get("frame_interval") or 10),
        "mode": request.get("mode") or "normal",
    }
    if config is not None:
        raw = getattr(config, "raw", {}) or {}
        fc = raw.get("full_concat", {}) or {}
        llm = getattr(config, "llm", {}) or {}
        profile["full_concat"] = {
            "target_column_mode": fc.get("target_column_mode", "auto"),
            "boundary_contract": analysis_contract_profile()["plan_qc"],
        }
        profile["vision"] = {
            "provider": llm.get("vision_llm_provider", "openai"),
            "model": llm.get(f"vision_{llm.get('vision_llm_provider', 'openai')}_model_name", ""),
            "max_frames": request.get("vision_max_frames_per_chunk") or raw.get("workflow", {}).get("vision_max_frames_per_chunk", 6),
        }
    return profile


def build_common_source_key(source_items: list[dict[str, Any]], profile: dict[str, Any], *, root: Path | None = None) -> str:
    payload = {
        "sources": [source_fingerprint(item, root=root) for item in source_items if isinstance(item, dict)],
        "analysis_profile": profile,
    }
    return stable_hash(payload)[:16]

