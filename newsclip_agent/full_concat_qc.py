"""Boundary and semantic QC helpers for full-concat jobs.

The helpers are deliberately deterministic and dependency free.  They inspect
the already materialised detector/plan artifacts and provide a second safety
bar before a rendered MP4 is published.  A future vision rescanner can enrich
the evidence without changing the public schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .analysis_contracts import FULL_CONCAT_OUTPUT_QC_VERSION, FULL_CONCAT_PLAN_QC_VERSION
from .utils import ffprobe_json, now_iso, stable_hash

HARD_DROP_LABELS = {
    "ad", "sponsor", "sponsor_card", "promo", "other_column", "other_program",
    "station_promo", "packaging_other", "unknown_drop", "trailer_other",
}


def refine_detection_boundaries(detection: dict[str, Any], *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Materialise a boundary-refinement report from detector segments.

    The coarse detector already has the strongest evidence.  This pass records
    every keep/drop transition and intentionally does not invent a fixed margin
    when no transition evidence exists.  That makes the result auditable and
    gives a later frame rescanner a stable place to plug in.
    """

    config = config or {}
    segments = [s for s in detection.get("segments", []) if isinstance(s, dict)]
    ordered = sorted(segments, key=lambda s: (str(s.get("source_id", "")), float(s.get("start_seconds", 0))))
    boundaries: list[dict[str, Any]] = []
    previous = None
    for current in ordered:
        if previous is not None and str(previous.get("source_id")) == str(current.get("source_id")):
            prev_keep = bool(previous.get("keep"))
            cur_keep = bool(current.get("keep"))
            if prev_keep != cur_keep:
                boundaries.append({
                    "boundary_id": f"{current.get('source_id', 'source')}_{len(boundaries)+1:04d}",
                    "source_id": current.get("source_id", ""),
                    "kind": "drop_to_keep" if cur_keep else "keep_to_drop",
                    "coarse_time_seconds": round(float(current.get("start_seconds", 0)), 3),
                    "refined_time_seconds": round(float(current.get("start_seconds", 0)), 3),
                    "adjustment_seconds": 0.0,
                    "method": "coarse_detector_transition",
                    "confidence": 0.75,
                    "decision": "keep" if cur_keep else "drop",
                    "before_state": previous.get("label", "unknown"),
                    "after_state": current.get("label", "unknown"),
                    "fallback_reason": "frame_rescan_not_available",
                })
        previous = current
    return {
        "schema_version": "full_concat_boundary_refinement_v1",
        "algorithm_version": "boundary_refiner_v1",
        "created_at": now_iso(),
        "target_column": detection.get("target_column", ""),
        "inputs": {"ad_detection_hash": stable_hash(detection)},
        "config": config,
        "boundaries": boundaries,
        "segments": segments,
        "stats": {
            "risk_window_count": len(boundaries),
            "refined_boundary_count": len(boundaries),
            "fallback_count": len(boundaries),
            "whole_keep_runs_dropped": 0,
            "added_drop_seconds": 0.0,
            "restored_target_seconds": 0.0,
        },
    }


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return min(a_end, b_end) - max(a_start, b_start) > 0.001


def plan_qc(plan: dict[str, Any], detection: dict[str, Any], *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check that a cut plan never reintroduces explicit hard-drop ranges."""

    config = config or {}
    drops = [s for s in detection.get("segments", []) if isinstance(s, dict) and not s.get("keep", True)]
    issues: list[dict[str, Any]] = []
    checked = 0
    for video in plan.get("output_videos", []) or []:
        for clip in video.get("clips", []) or []:
            checked += 1
            sid = str(clip.get("source_id", ""))
            cs, ce = float(clip.get("local_start_seconds", 0)), float(clip.get("local_end_seconds", 0))
            if ce <= cs:
                issues.append({"code": "clip_overlap_or_reverse_time", "severity": "block", "clip_id": clip.get("clip_id", "")})
                continue
            for drop in drops:
                if sid != str(drop.get("source_id", "")):
                    continue
                ds, de = float(drop.get("start_seconds", 0)), float(drop.get("end_seconds", 0))
                if _overlap(cs, ce, ds, de):
                    issues.append({
                        "code": "explicit_drop_reintroduced_by_merge",
                        "severity": "block",
                        "clip_id": clip.get("clip_id", ""),
                        "source_id": sid,
                        "source_start_seconds": ds,
                        "source_end_seconds": de,
                        "reason": drop.get("reason", "explicit drop"),
                    })
    status = "fail" if issues else "pass"
    return {
        "schema_version": FULL_CONCAT_PLAN_QC_VERSION,
        "status": status,
        "publishable": status == "pass",
        "target_column": detection.get("target_column", ""),
        "plan_hash": stable_hash(plan),
        "issues": issues,
        "metrics": {"checked_clip_count": checked, "blocked_issue_count": len(issues), "warning_count": 0},
        "config": config,
        "created_at": now_iso(),
    }


def output_qc(render_index: dict[str, Any], plan_qc_doc: dict[str, Any], task_dir: Path) -> dict[str, Any]:
    """Run technical checks on every rendered output and inherit plan blocks."""

    issues: list[dict[str, Any]] = list(plan_qc_doc.get("issues", []) or [])
    outputs: list[dict[str, Any]] = []
    for item in render_index.get("outputs", []) or []:
        rel = str(item.get("file") or "")
        path = task_dir / rel
        if not path.exists():
            issues.append({"code": "render_output_missing", "severity": "block", "file": rel})
            continue
        try:
            probe = ffprobe_json(path)
            duration = float(probe.get("duration") or 0)
            has_video = bool(probe.get("has_video") or probe.get("video_streams") or probe.get("video_codec") or probe.get("width"))
            has_audio = bool(probe.get("has_audio") or probe.get("audio_streams") or probe.get("audio_codec"))
            if duration <= 0 or not has_video:
                issues.append({"code": "technical_qc_failed", "severity": "block", "file": rel, "duration": duration})
            outputs.append({"file": rel, "duration_seconds": duration, "has_video": has_video, "has_audio": has_audio})
        except Exception as exc:
            issues.append({"code": "technical_qc_failed", "severity": "block", "file": rel, "reason": str(exc)})
    status = "fail" if issues else "pass"
    return {
        "schema_version": FULL_CONCAT_OUTPUT_QC_VERSION,
        "status": status,
        "publishable": status == "pass",
        "quarantined": status != "pass",
        "issues": issues,
        "outputs": outputs,
        "created_at": now_iso(),
    }
