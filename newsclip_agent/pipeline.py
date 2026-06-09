from __future__ import annotations

import argparse
import json
import math
import copy
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import ProjectConfig, load_config
from .llm import OpenAICompatibleClient
from . import prompts
from .duration_policy import (
    build_voiceover_timing_contract,
    clean_voiceover_text,
    compact_voiceover_segment_times,
    editing_structure_duration,
    duration_mismatch_block_reason,
    evidence_audio_windows_from_clips,
    load_duration_settings,
    normalize_audio_mode_for_policy,
    original_audio_volume_for_clip,
    should_build_evidence_audio_windows,
    should_require_long_video_confirmation,
    validate_audio_policy,
    validate_voiceover_silence_for_evidence,
    validate_voiceover_gaps,
    voiceover_active_windows_from_segments,
    validate_editing_duration,
    voiceover_original_overlap_seconds,
    voiceover_duration_fit,
)
from .llm_digest import (
    build_chunk_flags,
    compact_asr_for_llm,
    compact_chunk_for_vision,
    compact_list,
    compact_text,
    select_frames_for_vision,
)
from .asr_cleaning import clean_asr_text
from .llm_materials import (
    FORBIDDEN_LLM_FIELDS,
    compact_chunks_for_llm,
    compact_sources_for_llm,
    local_time_range,
    sanitize_llm_text,
)
from .text_policy import sanitize_reporter_bylines
from .media_quality import build_source_quality_report
from .tts_omnivoice import audio_metadata, generate_omnivoice_audio, release_omnivoice_models
from .resource_locks import file_slot_lock
from .utils import (
    copy_or_link,
    ensure_dir,
    ffprobe_json,
    now_iso,
    output_hash,
    read_json,
    relpath,
    run_cmd,
    seconds_to_timecode,
    srt_time,
    stable_hash,
    timecode_to_seconds,
    write_json,
    write_text,
)


from .workflow_registry import DEPENDENCIES, step_order, WORKFLOWS

ALL_STEP_ORDER = list(dict.fromkeys(
    step for workflow in WORKFLOWS.values() for step in workflow
))


AGENT_INFO = {
    "video_understanding": ("agents/video_understanding", "video_analysis.json", prompts.VIDEO_UNDERSTANDING_PROMPT, "video_understanding_v1"),
    "highlight_detection": ("agents/highlight_detection", "candidate_clips.json", prompts.HIGHLIGHT_DETECTION_PROMPT, "highlight_detection_v1"),
    "asr_event_candidate": ("agents/asr_event_candidate", "asr_event_candidate.json", prompts.ASR_EVENT_CANDIDATE_PROMPT, "asr_event_candidate_v1"),
    "candidate_filter": ("agents/candidate_filter", "candidate_filter.json", prompts.CANDIDATE_FILTER_PROMPT, "candidate_filter_v1"),
    "short_video_planning": ("agents/short_video_planning", "short_video_plan.json", prompts.SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT, "short_video_planning_compat_v2"),
    "editing_script": ("agents/editing_script", "editing_script.json", prompts.SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT, "editing_script_compat_v2"),
    "asr_micro_segment": ("agents/asr_micro_segment", "micro_segments.json", prompts.ASR_MICRO_SEGMENT_PROMPT, "asr_micro_segment_v1"),
    "content_analysis_preselect": ("agents/content_analysis_preselect", "content_analysis_preselect.json", prompts.CONTENT_ANALYSIS_PRESELECT_PROMPT, "content_analysis_preselect_v1"),
    "content_analysis": ("agents/content_analysis", "content_analysis.json", prompts.CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT, "content_analysis_micro_segment_v1"),
    "short_video_edit_plan": ("agents/short_video_edit_plan", "short_video_edit_plan.json", prompts.SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT, "short_video_edit_plan_text_v1"),
    "voiceover_script": ("agents/voiceover_script", "voiceover_script.json", prompts.VOICEOVER_SCRIPT_TEXT_PROMPT, "voiceover_script_text_v1"),
    "highlight_reassembly_plan": ("agents/highlight_reassembly", "highlight_reassembly_plan.json", prompts.HIGHLIGHT_REASSEMBLY_TEXT_PROMPT, "highlight_reassembly_text_v1"),
    "news_quality_ai_review": ("agents/news_quality_ai_review", "news_quality_ai_review.json", prompts.NEWS_QUALITY_AI_REVIEW_PROMPT, "news_quality_ai_review_v1"),
}

LEGACY_TIME_FIELDS = {
    "virtual_start_seconds",
    "virtual_end_seconds",
    "virtual_start",
    "virtual_end",
    "global_start_seconds",
    "global_end_seconds",
    "global_start",
    "global_end",
}


class UserFacingPipelineError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        user_message: str,
        suggestions: list[str] | None = None,
        technical_detail: dict | None = None,
    ):
        super().__init__(message)
        self.user_message = user_message
        self.suggestions = suggestions or []
        self.technical_detail = technical_detail or {}


@dataclass
class RunOptions:
    input: str | None = None
    source_manifest: str | None = None
    task_id: str | None = None
    config: str = "config.toml"
    outputs_dir: str = "outputs"
    resume: bool = True
    rerun: str | None = None
    rerun_from: str | None = None
    stop_after: str | None = None
    chunk: str | None = None
    failed_only: bool = False
    mode: str = "normal"
    model: str | None = None
    prompt_version: str | None = None
    use_version: list[str] | None = None
    chunk_seconds: int = 60
    frame_interval: int = 10
    vision_max_workers: int = 3
    vision_max_frames_per_chunk: int = 6
    release_asr_after_task: bool = False
    aspect_ratio: str = "16:9"
    skip_tts: bool = False
    skip_render: bool = False
    only_analysis: bool = False
    common_only: bool = False
    target_duration_seconds: int | None = None
    target_duration_mode: str = "soft"
    output_mode: str = "single"
    max_output_videos: int = 1
    min_output_video_seconds: int = 30
    max_output_video_seconds: int = 90
    allow_long_video: bool = False
    require_tts: bool = True
    voice_id: str | None = None
    audio_policy: str = "ai_voiceover"
    allow_original_audio_evidence: bool = False
    production_mode: str = "ai_voiceover"
    reassembly_output_mode: str = "single"
    reassembly_sort_mode: str = "editorial"
    reassembly_target_seconds: int | None = None
    reassembly_min_clip_seconds: float = 5.0
    reassembly_max_clip_seconds: float = 45.0
    reassembly_max_clip_count: int = 8
    reassembly_export_individual_clips: bool = False


class PipelineRunner:
    def __init__(self, options: RunOptions):
        self.options = options
        self.config: ProjectConfig = load_config(options.config)
        self.duration_settings = load_duration_settings(self.config)
        if self.options.target_duration_seconds is None:
            self.options.target_duration_seconds = self.duration_settings.default_target_seconds
        if self.options.reassembly_target_seconds is None:
            self.options.reassembly_target_seconds = int(self.config.raw.get("reassembly", {}).get("default_target_seconds", 90))
        if self.options.audio_policy == "original":
            self.options.require_tts = False
        if self.options.common_only:
            self.options.production_mode = "ai_voiceover"
        self.root = self.config.root_dir
        self.outputs_dir = (self.root / options.outputs_dir).resolve()
        self.task_id = options.task_id or self._new_task_id()
        self.task_dir = self.outputs_dir / self.task_id
        ensure_dir(self.task_dir)
        self.manifest_path = self.task_dir / "manifest.json"
        self.task_path = self.task_dir / "task.json"
        self.manifest = read_json(self.manifest_path, None)
        self._init_task()
        self.apply_use_versions(options.use_version or [])
        self.llm_text = self._make_llm("text")
        self.llm_vision = self._make_llm("vision")
        self._asr_engine = None

    def run(self) -> dict[str, Any]:
        selected = self._resolve_selected_steps()
        self._record_run_options(selected)
        self._write_effective_runtime_flags(selected)
        print(f"任务目录: {self.task_dir}")
        print(f"执行步骤: {', '.join(selected)}")
        try:
            for step in selected:
                if self.options.only_analysis and step in {"tts", "subtitles", "cut_plan", "render"}:
                    self._mark_skipped(step, "only_analysis")
                    continue
                if self.options.only_analysis and step in {"reassembly_cut_plan", "reassembly_render"}:
                    self._mark_skipped(step, "only_analysis")
                    continue
                if self.options.skip_tts and step == "tts":
                    self._mark_skipped(step, "skip_tts")
                    continue
                if self.options.skip_render and step == "render":
                    self._mark_skipped(step, "skip_render")
                    continue
                if self.options.skip_render and step == "reassembly_render":
                    self._mark_skipped(step, "skip_render")
                    continue
                handler_name = f"step_{step}"
                handler = getattr(self, handler_name, None)
                if handler is None:
                    raise RuntimeError(
                        f"workflow step {step} has no handler {handler_name}; check workflow_registry.py"
                    )
                started = time.perf_counter()
                self._mark_running(step)
                try:
                    handler()
                except Exception as exc:
                    self._mark_failed(step, self._user_facing_error_message(exc), elapsed_seconds=round(time.perf_counter() - started, 3))
                    raise
                self.manifest.setdefault("steps", {}).setdefault(step, {})["elapsed_seconds"] = round(time.perf_counter() - started, 3)
                self._save_manifest()
            if self.manifest.get("status") not in {"failed", "action_required"}:
                self.manifest["status"] = "success"
                if getattr(self.options, "common_only", False):
                    self.manifest["common_only_completed"] = True
                    self.manifest["common_only_completed_at"] = now_iso()
                self._save_manifest()
        finally:
            if self.options.release_asr_after_task and self._asr_engine is not None:
                self._asr_engine.release()
                self._asr_engine = None
        return self.manifest

    def _init_task(self) -> None:
        if self.manifest:
            if self.options.input:
                source = Path(self.options.input).resolve()
                self.manifest["source_video"] = relpath(source, self.task_dir)
            if self.options.source_manifest:
                source_manifest = read_json(Path(self.options.source_manifest), {})
                self.manifest["source_mode"] = source_manifest.get("source_mode", self.manifest.get("source_mode", "single_source"))
                self.manifest["source_videos"] = source_manifest.get("sources", self.manifest.get("source_videos", []))
                self.manifest["source_manifest"] = relpath(Path(self.options.source_manifest), self.task_dir)
                self._save_manifest()
            elif self.manifest.get("source_mode", "single_source") == "single_source":
                self.manifest.setdefault("steps", {}).setdefault("source_prepare", {"status": "skipped", "reason": "single_source"})
                self._save_manifest()
            return
        source_manifest = read_json(Path(self.options.source_manifest), {}) if self.options.source_manifest else {}
        is_virtual = source_manifest.get("source_mode") in {"multi_source_pool", "multi_source_virtual"}
        
        if not self.options.input and not is_virtual:
            raise ValueError("首次运行必须提供 --input，断点续跑可只提供 --task-id")
            
        task = {
            "task_id": self.task_id,
            "created_at": now_iso(),
            "source_mode": source_manifest.get("source_mode", "single_source"),
            "source_manifest": relpath(Path(self.options.source_manifest), self.task_dir) if self.options.source_manifest else "",
            "source_videos": source_manifest.get("sources", []),
        }

        if self.options.input:
            source = Path(self.options.input).resolve()
            input_dst = self.task_dir / "input" / ("source" + source.suffix.lower())
            copy_or_link(source, input_dst)
            task["source_video_original"] = str(source)
            task["source_video"] = relpath(input_dst, self.task_dir)
        else:
            task["source_video_original"] = ""
            task["source_video"] = ""

        if self.options.source_manifest:
            sm_path = Path(self.options.source_manifest).resolve()
            sm_dst = self.task_dir / "input" / "source_manifest.json"
            if sm_path != sm_dst.resolve():
                copy_or_link(sm_path, sm_dst)
            task["source_manifest"] = relpath(sm_dst, self.task_dir)
        write_json(self.task_path, task)
        self.manifest = {
            "task_id": self.task_id,
            "created_at": task["created_at"],
            "updated_at": now_iso(),
            "source_video": task["source_video"],
            "source_mode": task["source_mode"],
            "source_manifest": task["source_manifest"],
            "source_videos": task["source_videos"],
            "current_versions": {},
            "steps": {
                "source_prepare": {"status": "skipped", "reason": "single_source"}
            } if not self.options.source_manifest else {},
        }
        self._save_manifest()

    def apply_use_versions(self, specs: list[str]) -> None:
        for spec in specs:
            if "=" not in spec:
                raise ValueError("--use-version 格式应为 step=v2")
            step, version = spec.split("=", 1)
            self.manifest.setdefault("current_versions", {})[step] = version
            self.manifest.setdefault("steps", {}).setdefault(step, {})["version"] = version
        if specs:
            self._save_manifest()

    def _record_run_options(self, selected_steps: list[str]) -> None:
        run_options = {
            "selected_steps": selected_steps,
            "rerun": self.options.rerun,
            "rerun_from": self.options.rerun_from,
            "chunk": self.options.chunk,
            "failed_only": self.options.failed_only,
            "source_manifest": self.options.source_manifest,
            "mode": self.options.mode,
            "chunk_seconds": self.options.chunk_seconds,
            "frame_interval": self.options.frame_interval,
            "vision_max_workers": self.options.vision_max_workers,
            "vision_max_frames_per_chunk": self.options.vision_max_frames_per_chunk,
            "aspect_ratio": self.options.aspect_ratio,
            "only_analysis": self.options.only_analysis,
            "skip_tts": self.options.skip_tts,
            "skip_render": self.options.skip_render,
            "target_duration_seconds": self.options.target_duration_seconds,
            "output_mode": self.options.output_mode,
            "max_output_videos": self.options.max_output_videos,
            "min_output_video_seconds": self.options.min_output_video_seconds,
            "max_output_video_seconds": self.options.max_output_video_seconds,
            "allow_long_video": self.options.allow_long_video,
            "require_tts": self.options.require_tts,
            "voice_id": self.options.voice_id,
            "audio_policy": self.options.audio_policy,
            "allow_original_audio_evidence": self.options.allow_original_audio_evidence,
            "production_mode": self.options.production_mode,
            "reassembly_output_mode": self.options.reassembly_output_mode,
            "reassembly_sort_mode": self.options.reassembly_sort_mode,
            "reassembly_target_seconds": self.options.reassembly_target_seconds,
            "reassembly_max_clip_count": self.options.reassembly_max_clip_count,
            "reassembly_export_individual_clips": self.options.reassembly_export_individual_clips,
            "updated_at": now_iso(),
        }
        if self.options.allow_long_video and self.manifest.get("action_required", {}).get("type") == "confirm_long_video":
            self.manifest.pop("action_required", None)
            if self.manifest.get("status") == "action_required":
                self.manifest["status"] = "running"
        self.manifest["last_run_options"] = run_options
        self.manifest.setdefault("run_history", []).append(run_options)
        self.manifest["run_history"] = self.manifest["run_history"][-20:]
        self._save_manifest()

    def _write_effective_runtime_flags(self, selected_steps: list[str]) -> None:
        unified = self._is_virtual_source_manifest()
        workflow_key = f"{self.options.production_mode}:{'unified' if unified else 'legacy'}"
        source_manifest = str(self.options.source_manifest or self.manifest.get("source_manifest") or "")
        doc = {
            "version": "effective_runtime_flags_v1",
            "created_at": now_iso(),
            "production_mode": self.options.production_mode,
            "audio_policy": self.options.audio_policy,
            "require_tts": bool(self.options.require_tts),
            "skip_tts": bool(self.options.skip_tts),
            "skip_render": bool(self.options.skip_render),
            "only_analysis": bool(self.options.only_analysis),
            "common_only": bool(self.options.common_only),
            "source_manifest": source_manifest,
            "source_mode": "unified" if unified else "legacy",
            "resolved_workflow_key": workflow_key,
            "resolved_workflow_steps": self._active_step_order(),
            "selected_steps": selected_steps,
        }
        out = write_json(self.task_dir / "effective_runtime_flags.json", doc)
        self.manifest["effective_runtime_flags"] = relpath(out, self.task_dir)
        self._save_manifest()

    def _make_llm(self, kind: str) -> OpenAICompatibleClient | None:
        llm = self.config.llm
        provider = llm.get(f"{kind}_llm_provider", "openai")
        if provider not in ("openai", "doubao"):
            return None
        key = llm.get(f"{kind}_{provider}_api_key")
        base_url = llm.get(f"{kind}_{provider}_base_url")
        if not key or not base_url:
            return None
        return OpenAICompatibleClient(
            api_key=key,
            base_url=base_url,
            timeout=int(llm.get(f"llm_{kind}_timeout", llm.get("llm_text_timeout", 180))),
            max_retries=int(llm.get("llm_max_retries", 3)),
        )

    def _new_task_id(self) -> str:
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"task_{stamp}"

    def _save_manifest(self) -> None:
        self.manifest["updated_at"] = now_iso()
        write_json(self.manifest_path, self.manifest)

    def _resolve_selected_steps(self) -> list[str]:
        step_order = self._active_step_order()
        
        if self.options.common_only:
            if self._is_virtual_source_manifest():
                # In unified virtual-source common_only mode, source_prepare is
                # handled by run_multisource_pipeline.py before PipelineRunner
                # starts. PipelineRunner only produces reusable common outputs.
                common_steps = [
                    "source_analysis",
                    "source_aggregate",
                ]
            else:
                common_steps = [
                    "metadata",
                    "audio_extract",
                    "frame_extract",
                    "chunk_build",
                    "asr",
                    "asr_digest",
                    "vision",
                    "timeline",
                    "timeline_digest",
                ]
            step_order = [step for step in common_steps if step in step_order]

        if self.options.rerun and self.options.rerun_from:
            raise ValueError("--rerun 与 --rerun-from 不能同时使用")
        if self.options.rerun:
            if self.options.rerun not in step_order:
                raise ValueError(f"未知步骤: {self.options.rerun}")
            selected = [self.options.rerun]
        elif self.options.rerun_from:
            if self.options.rerun_from not in step_order:
                raise ValueError(f"未知步骤: {self.options.rerun_from}")
            selected = step_order[step_order.index(self.options.rerun_from) :]
        else:
            selected = step_order
            reused_common_steps = set(self.manifest.get("reused_common_steps") or [])
            if reused_common_steps:
                selected = [step for step in selected if step not in reused_common_steps]

        if self.options.stop_after:
            if self.options.stop_after not in selected:
                raise ValueError(f"未知 stop_after 步骤或不在本次执行范围内: {self.options.stop_after}")
            selected = selected[: selected.index(self.options.stop_after) + 1]
        return selected

    def _active_step_order(self) -> list[str]:
        return step_order(
            self.options.production_mode,
            unified=self._is_virtual_source_manifest(),
        )

    def _source_video(self) -> Path:
        source = self.manifest["source_video"]
        if not source:
            raise RuntimeError("source_video not found (are you calling this in virtual multi-source mode?)")
        p = Path(source)
        return p if p.is_absolute() else self.task_dir / p

    def _is_virtual_multi_source(self) -> bool:
        return self._is_multi_source_pool()

    def _is_virtual_source_manifest(self) -> bool:
        return self._is_multi_source_pool()

    def _is_multi_source_pool(self) -> bool:
        manifest = getattr(self, "manifest", {}) or {}
        source_videos = manifest.get("source_videos") or []
        if manifest.get("source_mode") in {"multi_source_pool", "multi_source_virtual"} and source_videos:
            return True
        if not manifest.get("source_manifest"):
            return False
        try:
            source_manifest = read_json(self._source_manifest_path(), {})
        except Exception:
            return False
        return bool(source_manifest.get("sources"))

    def _source_manifest_path(self) -> Path:
        manifest_path = self.manifest.get("source_manifest")
        if not manifest_path:
            raise RuntimeError("missing source_manifest")
        p = Path(manifest_path)
        return p if p.is_absolute() else self.task_dir / p

    def _iter_source_items(self) -> list[dict[str, Any]]:
        return self.manifest.get("source_videos", [])

    def _resolve_source_item_path(self, item: dict[str, Any]) -> Path:
        path = item.get("original_path")
        if not path:
            raise RuntimeError(f"missing original_path in source item: {item.get('source_id')}")
        p = Path(path)
        return p if p.is_absolute() else self.task_dir / p

    def _version_dir(self, step: str, base: str) -> tuple[str, Path]:
        next_num = 1
        base_dir = self.task_dir / base
        if base_dir.exists():
            nums = [int(p.name[1:]) for p in base_dir.iterdir() if p.is_dir() and re.fullmatch(r"v\d+", p.name)]
            if nums:
                next_num = max(nums) + 1
        version = f"v{next_num}"
        return version, base_dir / version

    def _can_reuse(self, step: str, input_hash: str | None = None) -> bool:
        step_order = self._active_step_order()
        if self.options.rerun == step or (self.options.rerun_from and step_order.index(step) >= step_order.index(self.options.rerun_from)):
            return False
        status = self.manifest.get("steps", {}).get(step, {})
        if status.get("status") != "success":
            return False
        if input_hash and status.get("input_hash") != input_hash:
            return False
        output = status.get("output")
        if output and not (self.task_dir / output).exists():
            return False
        if output and str(output).endswith(".json"):
            try:
                self._assert_no_legacy_virtual_time(read_json(self.task_dir / output, {}))
            except Exception:
                return False
        return self.options.resume

    def _assert_no_legacy_virtual_time(self, obj: Any, *, path: str = "") -> None:
        if isinstance(obj, dict):
            found = LEGACY_TIME_FIELDS.intersection(obj.keys())
            if found:
                raise UserFacingPipelineError(
                    "legacy virtual time fields detected",
                    user_message=(
                        "检测到旧架构产物，包含虚拟/全局时间字段："
                        + ", ".join(sorted(found))
                        + "。本次架构升级后不兼容旧任务产物，请重新运行任务。"
                    ),
                    technical_detail={"path": path, "fields": sorted(found)},
                )
            for key, value in obj.items():
                self._assert_no_legacy_virtual_time(value, path=f"{path}.{key}" if path else str(key))
        elif isinstance(obj, list):
            for index, value in enumerate(obj):
                self._assert_no_legacy_virtual_time(value, path=f"{path}[{index}]")

    def _record_step(
        self,
        *,
        step: str,
        version: str,
        status: str,
        output: str | None,
        input_hash: str,
        output_files: list[Path] | None = None,
        extra: dict[str, Any] | None = None,
        mark_downstream_stale: bool = True,
    ) -> None:
        previous = self.manifest.setdefault("steps", {}).get(step, {})
        finished_at = now_iso()
        data = {
            "step_name": step,
            "version": version,
            "status": status,
            "started_at": previous.get("started_at", ""),
            "finished_at": finished_at,
            "updated_at": finished_at,
            "input_hash": input_hash,
            "output_hash": output_hash(output_files or []) if output_files else "",
            "depends_on": DEPENDENCIES.get(step, []),
            "output": output,
            "can_rerun": True,
        }
        if extra:
            data.update(extra)
        self.manifest.setdefault("steps", {})[step] = data
        if status in {"success", "partial_success"}:
            self.manifest.setdefault("current_versions", {})[step] = version
            if mark_downstream_stale:
                self._mark_downstream_stale(step)
        self._save_manifest()

    def _write_status(self, version_dir: Path, status: dict[str, Any]) -> None:
        write_json(version_dir / "step_status.json", status)

    def _mark_skipped(self, step: str, reason: str) -> None:
        self.manifest.setdefault("steps", {})[step] = {
            "step_name": step,
            "status": "skipped",
            "reason": reason,
            "updated_at": now_iso(),
            "can_rerun": True,
        }
        self._save_manifest()

    def _mark_running(self, step: str) -> None:
        self.manifest["status"] = "running"
        previous = self.manifest.setdefault("steps", {}).get(step, {})
        started_at = previous.get("started_at") or now_iso()
        self.manifest.setdefault("steps", {})[step] = {
            "step_name": step,
            "status": "running",
            "started_at": started_at,
            "updated_at": now_iso(),
            "can_rerun": False,
        }
        self._save_manifest()

    def _mark_failed(self, step: str, error: str, elapsed_seconds: float | None = None) -> None:
        self.manifest["status"] = "failed"
        self.manifest["user_message"] = error
        data = {
            "step_name": step,
            "status": "failed",
            "error": error,
            "updated_at": now_iso(),
            "can_rerun": True,
        }
        if elapsed_seconds is not None:
            data["elapsed_seconds"] = elapsed_seconds
        self.manifest.setdefault("steps", {})[step] = data
        self._save_manifest()

    def _user_facing_error_message(self, exc: Exception) -> str:
        text = str(exc).strip() or exc.__class__.__name__
        if text.startswith("视频预处理失败："):
            return text
        if "ffmpeg" in text.lower() or "ffprobe" in text.lower():
            return "\n".join([
                "视频处理失败：系统调用 ffmpeg/ffprobe 时出错。",
                "如果源视频在播放器里也无法正常播放，请重新导出或重新上传视频后再试。",
                "如果源视频能正常播放，这更可能是系统处理或临时读取问题，请先重试；重试仍失败时再查看下面的原始错误。",
                "原始错误：",
                text,
            ])
        return text

    def _source_media_error_message(
        self,
        source: Path,
        metadata: dict[str, Any],
        exc: Exception,
        retry_exc: Exception | None = None,
    ) -> str:
        duration = float(metadata.get("duration") or 0)
        width = int(metadata.get("width") or 0)
        height = int(metadata.get("height") or 0)
        video_codec = str(metadata.get("video_codec") or "")
        audio_codec = str(metadata.get("audio_codec") or "")
        source_size = source.stat().st_size if source.exists() else 0
        hints = [
            "视频预处理失败：系统无法从源视频中提取可用音频。",
            f"源文件：{source}",
            f"检测结果：大小 {source_size} 字节，时长 {duration:.3f}s，画面 {width}x{height}，视频编码 {video_codec or '未检测到'}，音频编码 {audio_codec or '未检测到'}。",
        ]
        if source_size < 1024 or duration <= 0 or not video_codec:
            hints.extend([
                "判断：源视频本身不像一个可用的视频文件，或者系统拿到了占位/损坏文件。",
                "处理办法：请重新上传或重新导出视频；如果这是多源任务，请直接重试任务，让系统重新准备源文件。",
            ])
        else:
            hints.extend([
                "判断：视频有基本画面信息，但 ffmpeg 抽音频失败，可能是封装/音轨损坏或临时读取问题。",
                "处理办法：可以先重试；如果仍失败，请用剪映/播放器/ffmpeg 重新导出为 H.264 + AAC 的 mp4 后再跑。",
            ])
        hints.append("原始错误：")
        hints.append(str(exc).strip())
        if retry_exc is not None:
            hints.append("容错重试仍失败：")
            hints.append(str(retry_exc).strip())
        return "\n".join(hints)

    def _mark_downstream_stale(self, step: str) -> None:
        if self.options.rerun != step and not self.options.rerun_from:
            return
        step_order = self._active_step_order()
        if step not in step_order:
            return
        idx = step_order.index(step)
        for downstream in step_order[idx + 1 :]:
            entry = self.manifest.setdefault("steps", {}).get(downstream)
            if entry and entry.get("status") == "success":
                entry["status"] = "stale"
                entry["reason"] = f"upstream {step} updated"

    def _update_step_progress(self, step: str, message: str, extra: dict[str, Any] | None = None) -> None:
        data = {
            "step_name": step,
            "status": "running",
            "updated_at": now_iso(),
            "can_rerun": False,
            "message": message,
        }
        if extra:
            data.update(extra)
        self.manifest.setdefault("steps", {}).setdefault(step, {}).update(data)
        self._save_manifest()

    def _cut_audio_segment(self, audio: Path, out: Path, start: float, end: float) -> None:
        ensure_dir(out.parent)
        run_cmd([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(max(0.0, start)),
            "-to", str(max(start, end)),
            "-i", str(audio),
            "-ac", "1",
            "-ar", "16000",
            str(out),
        ])

    def _asr_windows_from_chunks(self, audio_duration: float | None = None) -> list[dict[str, Any]]:
        funasr_cfg = self.config.funasr
        segment_seconds = float(funasr_cfg.get("segment_seconds", 60))
        overlap = float(funasr_cfg.get("segment_overlap_seconds", 0))
        max_segment_seconds = float(funasr_cfg.get("max_segment_seconds", 90))
        segment_seconds = min(segment_seconds, max_segment_seconds)

        try:
            chunks = self._load_step_json("chunk_build").get("chunks", [])
        except Exception:
            chunks = []

        if chunks:
            windows = []
            for chunk in chunks:
                start = float(chunk.get("local_start_seconds", chunk.get("start", 0)) or 0)
                end = float(chunk.get("local_end_seconds", chunk.get("end", start)) or start)
                if end <= start:
                    continue
                w = {
                    "id": chunk.get("chunk_id") or f"asr_{len(windows)+1:04d}",
                    "start": max(0.0, start - overlap),
                    "end": end + overlap,
                    "chunk_id": chunk.get("chunk_id", ""),
                }
                if "source_id" in chunk:
                    w["source_id"] = chunk["source_id"]
                    local_start = float(chunk.get("local_start_seconds", chunk.get("local_start", 0)))
                    local_end = float(chunk.get("local_end_seconds", chunk.get("local_end", local_start)))
                    w["local_start"] = max(0.0, local_start - overlap)
                    w["local_end"] = local_end + overlap
                windows.append(w)
            return windows

        if not audio_duration:
            return [{"id": "asr_0001", "start": 0.0, "end": 0.0, "chunk_id": ""}]

        windows = []
        t = 0.0
        while t < audio_duration:
            end = min(audio_duration, t + segment_seconds)
            windows.append({"id": f"asr_{len(windows)+1:04d}", "start": t, "end": end, "chunk_id": ""})
            t = max(end - overlap, end)
        return windows

    def _quality_check_source_item(
        self,
        item: dict[str, Any],
        source_path: Path,
    ) -> dict[str, Any]:
        try:
            metadata = ffprobe_json(source_path)
        except Exception as exc:
            metadata = {"probe_error": str(exc)}

        if item.get("duration_seconds") is not None:
            metadata.setdefault("duration", item.get("duration_seconds"))
            metadata.setdefault("duration_seconds", item.get("duration_seconds"))

        metadata.setdefault("source_id", item.get("source_id", ""))
        metadata.setdefault("source_index", item.get("source_index"))
        metadata.setdefault("display_name", item.get("display_name", ""))
        metadata.setdefault("original_path", item.get("original_path", ""))

        report = build_source_quality_report([metadata])

        return {
            "version": "source_quality_check_internal_v1",
            "source_id": item.get("source_id", ""),
            "source_index": item.get("source_index"),
            "display_name": item.get("display_name", ""),
            "original_path": item.get("original_path", ""),
            "metadata": metadata,
            "report": report,
            "is_pass": bool(report.get("is_pass", False)),
            "reason": report.get("reason", ""),
            "checked_at": now_iso(),
        }

    def _source_stage_message(self, step: str, summary: dict[str, Any]) -> str:
        if step == "vision":
            total = int(summary.get("total_chunks") or 0)
            processed = int(summary.get("processed_chunks") or 0)
            failed = int(summary.get("failed_chunks") or 0)
            if total:
                return f"画面识别 {processed}/{total}" + (f"，失败 {failed}" if failed else "")

        if step == "asr":
            total = int(summary.get("total_chunks") or summary.get("total_segments") or 0)
            processed = int(summary.get("processed_chunks") or summary.get("processed_segments") or 0)
            if total:
                return f"语音转文字 {processed}/{total}"

        return step

    def _step_label(self, step: str) -> str:
        return {
            "source_quality_check": "素材质量检查",
            "metadata": "读取视频信息",
            "audio_extract": "提取音频",
            "frame_extract": "抽取关键帧",
            "chunk_build": "切分分析片段",
            "asr": "语音转文字",
            "asr_digest": "压缩语音摘要",
            "vision": "画面识别",
            "timeline": "合并时间线",
            "timeline_digest": "压缩分析时间线",
        }.get(step, step)

    def step_source_analysis(self) -> None:
        step_started = time.perf_counter()
        sources = self._iter_source_items()
        if not sources:
            raise RuntimeError("source_analysis requires a virtual source manifest with at least 1 source")
        input_hash = stable_hash({
            "source_manifest": self._step_content_hash("source_prepare") or self.manifest.get("source_manifest", ""),
            "sources": [
                {
                    "source_id": item.get("source_id"),
                    "path": str(self._resolve_source_item_path(item)),
                    "mtime": self._resolve_source_item_path(item).stat().st_mtime if self._resolve_source_item_path(item).exists() else 0,
                    "duration": item.get("duration_seconds"),
                }
                for item in sources
            ],
            "chunk_seconds": self.options.chunk_seconds,
            "frame_interval": self.options.frame_interval,
            "mode": self.options.mode,
            "aspect_ratio": self.options.aspect_ratio,
        })
        if self._can_reuse("source_analysis", input_hash):
            print("复用缓存: source_analysis")
            return

        version, base_dir = self._version_dir("source_analysis", "source_analysis")
        ensure_dir(base_dir)
        cfg = self.config.raw.get("multi_source_analysis", {})
        max_workers = max(1, int(cfg.get("source_max_workers", 3) or 3))
        active_workers = min(max_workers, len(sources))
        per_source_vision_workers = max(1, int(cfg.get("per_source_vision_max_workers", self.options.vision_max_workers) or self.options.vision_max_workers))
        fail_policy = str(cfg.get("fail_policy") or "partial_success")
        progress_enabled = bool(cfg.get("progress_enabled", True))
        verbose_source_logs = bool(cfg.get("verbose_source_logs", True))
        config_path = Path(self.options.config)
        if not config_path.is_absolute():
            config_path = self.root / config_path

        total_sources = len(sources)
        source_items_by_identity = {id(item): idx for idx, item in enumerate(sources, start=1)}

        def source_id_for(item: dict[str, Any]) -> str:
            idx = source_items_by_identity.get(id(item), 0)
            return str(item.get("source_id") or f"source_{idx:03d}")

        source_positions = {
            source_id_for(item): idx
            for idx, item in enumerate(sources, start=1)
        }
        source_progress: dict[str, dict[str, Any]] = {}
        for idx, item in enumerate(sources, start=1):
            source_id = source_id_for(item)
            display_name = str(item.get("display_name") or Path(str(item.get("original_path") or "")).name or source_id)
            source_progress[source_id] = {
                "source_id": source_id,
                "source_index": item.get("source_index") or idx,
                "display_name": display_name,
                "status": "pending",
                "updated_at": now_iso(),
            }

        if verbose_source_logs:
            print(
                f"[source_analysis] queue total={total_sources} "
                f"source_max_workers={active_workers} "
                f"per_source_vision_max_workers={per_source_vision_workers}",
                flush=True,
            )

        def source_progress_extra() -> dict[str, Any]:
            return {
                "completed_sources": sum(
                    1 for value in source_progress.values()
                    if value.get("status") in {"success", "failed"}
                ),
                "total_sources": total_sources,
                "success_count": sum(1 for value in source_progress.values() if value.get("status") == "success"),
                "failed_count": sum(1 for value in source_progress.values() if value.get("status") == "failed"),
                "running_count": sum(1 for value in source_progress.values() if value.get("status") == "running"),
                "source_progress": list(source_progress.values()),
                "source_max_workers": active_workers,
                "per_source_vision_max_workers": per_source_vision_workers,
            }

        def analyze_one(item: dict[str, Any]) -> dict[str, Any]:
            source_id = source_id_for(item)
            source_pos = source_positions.get(source_id, 0)
            source_dir = ensure_dir(base_dir / source_id)
            work_root = ensure_dir(source_dir / "_work")
            source_path = self._resolve_source_item_path(item)
            started_at = time.perf_counter()
            summary_path = source_dir / "source_summary.json"
            source_status_path = source_dir / "source_status.json"
            source_status = read_json(source_status_path, {})

            source_progress[source_id].update({
                "status": "running",
                "current_stage": "source_quality_check",
                "current_stage_label": "素材质量检查",
                "message": "素材质量检查中",
                "updated_at": now_iso(),
            })
            if progress_enabled:
                self._update_step_progress(
                    "source_analysis",
                    f"source {source_pos}/{total_sources}: source_quality_check",
                    source_progress_extra(),
                )

            if (
                self.options.resume
                and not self.options.rerun
                and summary_path.exists()
                and source_status.get("status") == "success"
            ):
                summary_doc = read_json(summary_path, {})
                digest = summary_doc.get("timeline_digest") or {}
                chunk_count = len(digest.get("chunks") or [])
                if verbose_source_logs:
                    print(
                        f"[source_analysis] reuse {source_id} {source_pos}/{total_sources} "
                        f"chunks={chunk_count} video={source_path.name}",
                        flush=True,
                    )
                return {
                    "source_id": source_id,
                    "status": "success",
                    "display_name": item.get("display_name") or source_path.name,
                    "summary": relpath(summary_path, self.task_dir),
                    "work_task_dir": summary_doc.get("work_task_dir", ""),
                    "chunk_count": chunk_count,
                    "elapsed_seconds": 0,
                    "reused": True,
                }

            quality_doc = self._quality_check_source_item(item, source_path)
            quality_path = write_json(source_dir / "source_quality_check.json", quality_doc)

            if not quality_doc.get("is_pass"):
                elapsed = round(time.perf_counter() - started_at, 3)
                reason = quality_doc.get("reason") or "source quality check failed"

                write_json(source_status_path, {
                    "source_id": source_id,
                    "status": "failed",
                    "failed_stage": "source_quality_check",
                    "error": reason,
                    "quality_check": relpath(quality_path, self.task_dir),
                    "updated_at": now_iso(),
                    "elapsed_seconds": elapsed,
                })

                source_progress[source_id].update({
                    "status": "failed",
                    "current_stage": "source_quality_check",
                    "error": reason,
                    "quality_check": relpath(quality_path, self.task_dir),
                    "updated_at": now_iso(),
                })
                if progress_enabled:
                    self._update_step_progress(
                        "source_analysis",
                        f"source {source_pos}/{total_sources}: source_quality_check failed",
                        source_progress_extra(),
                    )

                raise RuntimeError(f"source quality check failed for {source_id}: {reason}")

            if verbose_source_logs:
                print(
                    f"[source_analysis] start {source_id} {source_pos}/{total_sources} "
                    f"video={source_path.name}",
                    flush=True,
                )

            try:
                child_options = RunOptions(
                    input=str(source_path),
                    task_id=source_id,
                    config=str(config_path),
                    outputs_dir=str(work_root),
                    resume=self.options.resume,
                    rerun_from="metadata" if self.options.rerun == "source_analysis" else None,
                    chunk_seconds=self.options.chunk_seconds,
                    frame_interval=self.options.frame_interval,
                    vision_max_workers=per_source_vision_workers,
                    vision_max_frames_per_chunk=self.options.vision_max_frames_per_chunk,
                    release_asr_after_task=self.options.release_asr_after_task,
                    aspect_ratio=self.options.aspect_ratio,
                    mode=self.options.mode,
                    common_only=True,
                    production_mode="ai_voiceover",
                )
                child_runner = PipelineRunner(child_options)
                stop_child_sync = threading.Event()

                def sync_child_progress() -> None:
                    while not stop_child_sync.is_set():
                        try:
                            child_manifest_data = read_json(child_runner.manifest_path, {})
                            child_steps = child_manifest_data.get("steps", {}) or {}

                            running_step = ""
                            running_item: dict[str, Any] = {}
                            for name, value in child_steps.items():
                                if isinstance(value, dict) and value.get("status") == "running":
                                    running_step = name
                                    running_item = value
                                    break

                            if running_step:
                                summary = running_item.get("summary") or {}
                                message = self._source_stage_message(running_step, summary)
                                if message == running_step:
                                    message = str(running_item.get("message") or running_step)
                                source_progress[source_id].update({
                                    "status": "running",
                                    "current_stage": running_step,
                                    "current_stage_label": self._step_label(running_step),
                                    "stage_summary": summary,
                                    "message": message,
                                    "updated_at": now_iso(),
                                })

                                self._update_step_progress(
                                    "source_analysis",
                                    f"source {source_pos}/{total_sources}: {running_step}",
                                    source_progress_extra(),
                                )
                        except Exception:
                            pass

                        stop_child_sync.wait(2.0)

                sync_thread = threading.Thread(target=sync_child_progress, daemon=True)
                sync_thread.start()
                try:
                    child_manifest = child_runner.run()
                finally:
                    stop_child_sync.set()
                    sync_thread.join(timeout=1.0)
                digest = child_runner._load_step_json("timeline_digest")
                summary = self._build_source_summary(item, child_runner.task_dir, child_manifest, digest)
                summary_path = write_json(summary_path, summary)
                elapsed = round(time.perf_counter() - started_at, 3)
                chunk_count = len(digest.get("chunks") or [])
                write_json(source_status_path, {
                    "source_id": source_id,
                    "status": "success",
                    "summary": relpath(summary_path, self.task_dir),
                    "quality_check": relpath(quality_path, self.task_dir),
                    "updated_at": now_iso(),
                    "elapsed_seconds": elapsed,
                    "chunk_count": chunk_count,
                })
                if verbose_source_logs:
                    print(
                        f"[source_analysis] done {source_id} {source_pos}/{total_sources} "
                        f"elapsed={elapsed}s chunks={chunk_count} video={source_path.name}",
                        flush=True,
                    )
                return {
                    "source_id": source_id,
                    "status": "success",
                    "display_name": item.get("display_name") or source_path.name,
                    "summary": relpath(summary_path, self.task_dir),
                    "quality_check": relpath(quality_path, self.task_dir),
                    "work_task_dir": relpath(child_runner.task_dir, self.task_dir),
                    "chunk_count": chunk_count,
                    "elapsed_seconds": elapsed,
                }
            except Exception as exc:
                elapsed = round(time.perf_counter() - started_at, 3)
                write_json(source_status_path, {
                    "source_id": source_id,
                    "status": "failed",
                    "failed_stage": source_progress.get(source_id, {}).get("current_stage", ""),
                    "error": str(exc),
                    "updated_at": now_iso(),
                    "elapsed_seconds": elapsed,
                })
                if verbose_source_logs:
                    print(
                        f"[source_analysis] failed {source_id} {source_pos}/{total_sources} "
                        f"elapsed={elapsed}s video={source_path.name} error={exc}",
                        flush=True,
                    )
                raise

        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        if progress_enabled:
            self._update_step_progress(
                "source_analysis",
                f"multi-source analysis started: 0/{total_sources} sources completed",
                {
                    "completed_sources": 0,
                    "total_sources": total_sources,
                    "success_count": 0,
                    "failed_count": 0,
                    "running_count": 0,
                    "source_progress": list(source_progress.values()),
                    "source_max_workers": active_workers,
                    "per_source_vision_max_workers": per_source_vision_workers,
                },
            )

        with ThreadPoolExecutor(max_workers=active_workers) as executor:
            future_map = {}
            for idx, item in enumerate(sources, start=1):
                source_id = source_id_for(item)
                source_progress[source_id].update({"status": "pending", "updated_at": now_iso()})
                future_map[executor.submit(analyze_one, item)] = item

            completed = 0
            for future in as_completed(future_map):
                item = future_map[future]
                source_id = source_id_for(item)
                try:
                    result = future.result()
                    results.append(result)
                    source_progress.setdefault(source_id, {"source_id": source_id}).update({
                        "status": "success",
                        "elapsed_seconds": result.get("elapsed_seconds", 0),
                        "chunk_count": result.get("chunk_count", 0),
                        "summary": result.get("summary", ""),
                        "reused": bool(result.get("reused")),
                        "updated_at": now_iso(),
                    })
                except Exception as exc:
                    failure = {
                        "source_id": source_id,
                        "display_name": item.get("display_name") or "",
                        "status": "failed",
                        "error": str(exc),
                    }
                    failures.append(failure)
                    source_progress.setdefault(source_id, {"source_id": source_id}).update({
                        "status": "failed",
                        "error": str(exc),
                        "updated_at": now_iso(),
                    })
                    if fail_policy == "fail_fast":
                        raise
                finally:
                    completed += 1
                    running_count = sum(1 for value in source_progress.values() if value.get("status") == "running")
                    if progress_enabled:
                        self._update_step_progress(
                            "source_analysis",
                            f"multi-source analysis running: {completed}/{total_sources} sources completed",
                            {
                                "completed_sources": completed,
                                "total_sources": total_sources,
                                "success_count": len(results),
                                "failed_count": len(failures),
                                "running_count": running_count,
                                "source_progress": list(source_progress.values()),
                                "source_max_workers": active_workers,
                                "per_source_vision_max_workers": per_source_vision_workers,
                            },
                        )
                    if verbose_source_logs:
                        print(
                            f"[source_analysis] progress {completed}/{total_sources} "
                            f"success={len(results)} failed={len(failures)} running={running_count}",
                            flush=True,
                        )

        results.sort(key=lambda item: str(item.get("source_id") or ""))
        failures.sort(key=lambda item: str(item.get("source_id") or ""))
        if not results:
            raise RuntimeError("source_analysis failed for all sources")

        output_doc = {
            "version": "source_analysis_v1",
            "source_count": len(sources),
            "success_count": len(results),
            "failed_count": len(failures),
            "sources": results,
            "failed_sources": failures,
            "fail_policy": fail_policy,
        }
        if failures and results:
            output_doc["partial_success_notice"] = (
                f"{len(failures)} source video(s) failed; downstream output is based on "
                f"{len(results)} successful source video(s)."
            )
        out = write_json(base_dir / "source_analysis.json", output_doc)
        status = "partial_success" if failures else "success"
        status_doc = self._base_status("source_analysis", version, input_hash, [out])
        status_doc["status"] = status
        self._write_status(base_dir, status_doc)
        extra = {
            "completed_sources": len(results) + len(failures),
            "total_sources": len(sources),
            "success_count": len(results),
            "failed_count": len(failures),
            "source_progress": list(source_progress.values()),
            "elapsed_seconds_total": round(time.perf_counter() - step_started, 3),
            "source_max_workers": active_workers,
            "per_source_vision_max_workers": per_source_vision_workers,
            "vision_global_max_workers": int(cfg.get("vision_global_max_workers", 0) or 0),
            "text_global_max_workers": int(cfg.get("text_global_max_workers", 0) or 0),
        }
        if failures:
            extra["failed_sources"] = failures
        self._record_step(
            step="source_analysis",
            version=version,
            status=status,
            output=relpath(out, self.task_dir),
            input_hash=input_hash,
            output_files=[out] + [self.task_dir / item["summary"] for item in results if item.get("summary")],
            extra=extra,
        )
        if verbose_source_logs:
            print(
                f"[source_analysis] final status={status} success={len(results)} "
                f"failed={len(failures)} elapsed={extra['elapsed_seconds_total']}s",
                flush=True,
            )
        print(f"完成: source_analysis ({status})")

    def _build_source_summary(
        self,
        item: dict[str, Any],
        child_task_dir: Path,
        child_manifest: dict[str, Any],
        digest: dict[str, Any],
    ) -> dict[str, Any]:
        chunks = [chunk for chunk in digest.get("chunks", []) if isinstance(chunk, dict)]
        speech_parts = [str(chunk.get("speech") or "").strip() for chunk in chunks if str(chunk.get("speech") or "").strip()]
        visual_parts = [str(chunk.get("visual") or "").strip() for chunk in chunks if str(chunk.get("visual") or "").strip()]
        key_facts = compact_list(speech_parts + visual_parts, max_items=8, max_chars_each=160)
        summary_text = compact_text(" ".join(speech_parts[:6] + visual_parts[:4]), max_chars=800)
        return {
            "version": "source_summary_v1",
            "source_id": item.get("source_id"),
            "source_index": item.get("source_index"),
            "display_name": item.get("display_name") or Path(str(item.get("original_path") or "")).name,
            "source_type": item.get("source_type", ""),
            "original_path": item.get("original_path", ""),
            "duration_seconds": item.get("duration_seconds"),
            "summary": summary_text,
            "key_facts": key_facts,
            "timeline_digest": digest,
            "work_task_dir": relpath(child_task_dir, self.task_dir),
            "work_manifest": relpath(child_task_dir / "manifest.json", self.task_dir),
            "work_current_versions": child_manifest.get("current_versions", {}),
        }

    def _source_analysis_child_asr_hashes(self, analysis: dict[str, Any]) -> list[dict[str, Any]]:
        hashes: list[dict[str, Any]] = []
        for source_result in analysis.get("sources") or []:
            if not isinstance(source_result, dict) or source_result.get("status") != "success":
                continue
            summary_rel = str(source_result.get("summary") or "")
            summary_doc = read_json(self.task_dir / summary_rel, {}) if summary_rel else {}
            work_task_dir = str(summary_doc.get("work_task_dir") or "").strip()
            if not work_task_dir:
                continue
            child_manifest = read_json(self.task_dir / work_task_dir / "manifest.json", {})
            asr_step = (child_manifest.get("steps") or {}).get("asr") or {}
            asr_output = str(asr_step.get("output") or "")
            asr_output_hash = str(asr_step.get("output_hash") or "")
            if asr_output and not asr_output_hash:
                asr_path = self.task_dir / work_task_dir / asr_output
                if asr_path.exists():
                    asr_output_hash = output_hash([asr_path])
            hashes.append({
                "source_id": summary_doc.get("source_id") or source_result.get("source_id") or "",
                "asr_output": asr_output,
                "asr_output_hash": asr_output_hash,
            })
        return hashes

    def _source_chunk_ids_for_time_range(
        self,
        *,
        source_id: str,
        start: float,
        end: float,
        chunks: list[dict[str, Any]],
    ) -> tuple[str, list[str], dict[str, Any]]:
        overlaps: list[tuple[float, str]] = []
        nearest_chunk_id = ""
        nearest_distance: float | None = None
        for chunk in chunks:
            if str(chunk.get("source_id") or "") != source_id:
                continue
            chunk_start = self._time_value_seconds(chunk.get("local_start_seconds") or chunk.get("start_seconds"))
            chunk_end = self._time_value_seconds(chunk.get("local_end_seconds") or chunk.get("end_seconds"))
            if chunk_start is None or chunk_end is None or chunk_end <= chunk_start:
                continue
            overlap = max(0.0, min(end, chunk_end) - max(start, chunk_start))
            chunk_id = str(chunk.get("chunk_id") or chunk.get("global_chunk_id") or "").strip()
            if overlap > 0 and chunk_id:
                overlaps.append((overlap, chunk_id))
            if overlap <= 0 and chunk_id:
                distance = min(abs(start - chunk_end), abs(end - chunk_start))
                if nearest_distance is None or distance < nearest_distance:
                    nearest_distance = distance
                    nearest_chunk_id = chunk_id
        overlaps.sort(key=lambda item: item[0], reverse=True)
        source_chunk_ids = [chunk_id for _overlap, chunk_id in overlaps]
        diagnostics = {
            "nearest_chunk_id": nearest_chunk_id,
            "nearest_distance_seconds": round(nearest_distance, 3) if nearest_distance is not None else None,
        }
        return (source_chunk_ids[0] if source_chunk_ids else ""), source_chunk_ids, diagnostics

    def _load_child_source_asr_segments(
        self,
        *,
        source_id: str,
        source_index: int | str | None,
        summary_doc: dict[str, Any],
        chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        work_task_dir = str(summary_doc.get("work_task_dir") or "").strip()
        if not work_task_dir:
            return []
        child_dir = self.task_dir / work_task_dir
        child_manifest = read_json(child_dir / "manifest.json", {})
        asr_step = (child_manifest.get("steps") or {}).get("asr") or {}
        asr_rel = str(asr_step.get("output") or "").strip()
        if not asr_rel:
            return []
        asr_doc = read_json(child_dir / asr_rel, {})
        raw_segments = asr_doc.get("segments") or []
        if not isinstance(raw_segments, list):
            return []

        out: list[dict[str, Any]] = []
        for idx, seg in enumerate(raw_segments, start=1):
            if not isinstance(seg, dict):
                continue
            start = self._time_value_seconds(seg.get("start"))
            end = self._time_value_seconds(seg.get("end"))
            if start is None or end is None or end <= start:
                continue
            raw_text = str(seg.get("raw_text") or "").strip()
            text = raw_text or str(seg.get("text") or "").strip()
            if not text:
                continue
            primary_chunk_id, source_chunk_ids, chunk_match_diagnostics = self._source_chunk_ids_for_time_range(
                source_id=source_id,
                start=float(start),
                end=float(end),
                chunks=chunks,
            )
            chunk_diagnostics = []
            if not source_chunk_ids:
                chunk_diagnostics.append({
                    "reason": "asr_chunk_no_overlap",
                    "source_id": source_id,
                    "asr_segment_id": f"{source_id}_asr_{idx:06d}",
                    "start_seconds": round(float(start), 3),
                    "end_seconds": round(float(end), 3),
                    "source_chunk_id_hint": seg.get("chunk_id", ""),
                    **chunk_match_diagnostics,
                })
            out.append({
                "asr_segment_id": f"{source_id}_asr_{idx:06d}",
                "source_raw_asr_segment_id": seg.get("asr_segment_id") or seg.get("id") or "",
                "source_id": source_id,
                "source_index": source_index,
                "start_seconds": round(float(start), 3),
                "end_seconds": round(float(end), 3),
                "duration_seconds": round(float(end - start), 3),
                "text": text,
                "raw_text": raw_text,
                "normalized_text": re.sub(r"\s+", " ", text).strip(),
                "primary_chunk_id": primary_chunk_id,
                "source_chunk_ids": source_chunk_ids,
                "source_chunk_id_hint": seg.get("chunk_id", ""),
                "diagnostics": chunk_diagnostics,
                "removed_tokens": seg.get("removed_tokens", []),
            })
        return out

    def step_source_aggregate(self) -> None:
        analysis = self._load_step_json("source_analysis")
        input_hash = stable_hash({
            "source_analysis": self._step_content_hash("source_analysis"),
            "child_asr_outputs": self._source_analysis_child_asr_hashes(analysis),
            "limits": self.config.raw.get("multi_source_analysis", {}),
        })
        if self._can_reuse("source_aggregate", input_hash):
            print("复用缓存: source_aggregate")
            return

        version, vdir = self._version_dir("source_aggregate", "source_aggregate")
        ensure_dir(vdir)
        summaries: list[dict[str, Any]] = []
        chunks: list[dict[str, Any]] = []
        summary_docs: list[dict[str, Any]] = []
        source_items_by_id = {str(item.get("source_id") or ""): item for item in self._iter_source_items()}

        for source_result in analysis.get("sources") or []:
            if not isinstance(source_result, dict) or source_result.get("status") != "success":
                continue
            summary_rel = str(source_result.get("summary") or "")
            summary_doc = read_json(self.task_dir / summary_rel, {}) if summary_rel else {}
            summary_docs.append(summary_doc)
            source_id = str(summary_doc.get("source_id") or source_result.get("source_id") or "")
            source_item = source_items_by_id.get(source_id, {})
            source_summary = {
                "source_id": source_id,
                "source_index": source_item.get("source_index") or summary_doc.get("source_index"),
                "display_name": summary_doc.get("display_name") or source_result.get("display_name") or "",
                "source_type": summary_doc.get("source_type") or source_item.get("source_type") or "",
                "duration_seconds": summary_doc.get("duration_seconds") or source_item.get("duration_seconds"),
                "summary": summary_doc.get("summary", ""),
                "key_facts": summary_doc.get("key_facts", []),
            }
            summaries.append(source_summary)

            digest = summary_doc.get("timeline_digest") or {}
            for index, chunk in enumerate(digest.get("chunks") or [], start=1):
                if not isinstance(chunk, dict):
                    continue
                local_start = float(chunk.get("local_start_seconds", chunk.get("start_seconds", chunk.get("start", 0))) or 0)
                local_end = float(chunk.get("local_end_seconds", chunk.get("end_seconds", chunk.get("end", local_start))) or local_start)
                chunk_id = str(chunk.get("chunk_id") or f"chunk_{index:04d}")
                global_chunk_id = chunk_id if chunk_id.startswith(f"{source_id}_") else f"{source_id}_{chunk_id}"
                clean_chunk = {
                    key: value
                    for key, value in chunk.items()
                    if key not in FORBIDDEN_LLM_FIELDS
                    and key not in {"global_chunk_id", "local_chunk_id"}
                }
                chunks.append({
                    **clean_chunk,
                    "chunk_id": global_chunk_id,
                    "global_chunk_id": global_chunk_id,
                    "local_chunk_id": chunk_id,
                    "source_id": source_id,
                    "source_index": source_summary.get("source_index"),
                    "local_start_seconds": round(local_start, 3),
                    "local_end_seconds": round(local_end, 3),
                    "local_start": seconds_to_timecode(local_start, ms=True),
                    "local_end": seconds_to_timecode(local_end, ms=True),
                    "duration_seconds": round(max(0.0, local_end - local_start), 3),
                })

        chunks.sort(key=lambda item: (
            int(item.get("source_index") or 0),
            float(item.get("local_start_seconds") or 0),
            str(item.get("chunk_id") or ""),
        ))
        raw_sources: list[dict[str, Any]] = []
        raw_segments_all: list[dict[str, Any]] = []
        raw_asr_diagnostics: list[dict[str, Any]] = []
        for summary_doc in summary_docs:
            source_id = str(summary_doc.get("source_id") or "").strip()
            if not source_id:
                continue
            source_index = summary_doc.get("source_index")
            asr_segments = self._load_child_source_asr_segments(
                source_id=source_id,
                source_index=source_index,
                summary_doc=summary_doc,
                chunks=chunks,
            )
            raw_segments_all.extend(asr_segments)
            for seg in asr_segments:
                for item in seg.get("diagnostics") or []:
                    if isinstance(item, dict):
                        raw_asr_diagnostics.append(item)
            raw_sources.append({
                "source_id": source_id,
                "source_index": source_index,
                "duration_seconds": summary_doc.get("duration_seconds"),
                "asr_segment_count": len(asr_segments),
                "asr_segments": asr_segments,
            })
        total_source_duration = sum(float(item.get("duration_seconds") or 0) for item in summaries)
        raw_asr_index = {
            "version": "source_raw_asr_index_v1",
            "raw_asr_segment_count": len(raw_segments_all),
            "diagnostics": raw_asr_diagnostics,
            "sources": raw_sources,
        }
        aggregate = {
            "version": "source_aggregate_local_time_v1",
            "source_count": len(self._iter_source_items()),
            "successful_source_count": len(summaries),
            "failed_sources": analysis.get("failed_sources", []),
            "sources": summaries,
            "timeline_digest": {
                "version": "multi_source_local_timeline_digest_v1",
                "timeline_mode": "source_pool_local_time",
                "total_source_duration_seconds": round(total_source_duration, 3),
                "chunks": chunks,
            },
            "raw_asr_index": raw_asr_index,
            "raw_asr_segment_count": len(raw_segments_all),
        }
        raw_asr_out = write_json(vdir / "source_raw_asr_index.json", raw_asr_index)
        aggregate["raw_asr_index_file"] = relpath(raw_asr_out, self.task_dir)
        out = write_json(vdir / "source_aggregate.json", aggregate)
        self._write_status(vdir, self._base_status("source_aggregate", version, input_hash, [out, raw_asr_out]))
        self._record_step(
            step="source_aggregate",
            version=version,
            status="success",
            output=relpath(out, self.task_dir),
            input_hash=input_hash,
            output_files=[out, raw_asr_out],
            extra={"summary": {"chunks": len(chunks), "sources": len(summaries), "raw_asr_segments": len(raw_segments_all)}},
        )
        print("完成: source_aggregate")

    def _load_current_timeline_digest(self) -> dict[str, Any]:
        if self._is_virtual_source_manifest():
            aggregate = self._load_step_json("source_aggregate")
            digest = aggregate.get("timeline_digest") if isinstance(aggregate, dict) else {}
            return digest if isinstance(digest, dict) else {"chunks": []}
        return self._load_step_json("timeline_digest")

    def _load_current_timeline_digest_for_llm(self, step: str) -> dict[str, Any]:
        digest = self._load_current_timeline_digest()
        chunks = digest.get("chunks", []) if isinstance(digest, dict) else []
        compact_chunks = compact_chunks_for_llm([chunk for chunk in chunks if isinstance(chunk, dict)])
        return {
            "version": str(digest.get("version") or "timeline_digest_llm_compact_v1"),
            "source": "source_aggregate" if self._is_virtual_source_manifest() else "timeline_digest",
            "timeline_mode": digest.get("timeline_mode", "source_pool_local_time" if self._is_virtual_source_manifest() else ""),
            "total_source_duration_seconds": digest.get("total_source_duration_seconds", digest.get("video_duration_seconds", 0)),
            "chunk_count": len(compact_chunks),
            "chunks": compact_chunks,
        }

    def _load_compact_source_aggregate_for_llm(self) -> dict[str, Any]:
        if not self._is_virtual_source_manifest():
            return {}
        aggregate = self._load_optional_step_json("source_aggregate", {})
        sources = compact_sources_for_llm(
            [item for item in aggregate.get("sources") or [] if isinstance(item, dict)],
            max_sources=int(self.config.raw.get("llm_input", {}).get("max_llm_sources", 20) or 20),
        )
        return {
            "version": "source_aggregate_llm_compact_v1",
            "source_count": aggregate.get("source_count", len(sources)),
            "successful_source_count": aggregate.get("successful_source_count", len(sources)),
            "failed_sources": aggregate.get("failed_sources", []),
            "sources": sources,
        }

    def _current_timeline_digest_hash(self) -> str:
        if self._is_virtual_source_manifest():
            return self._step_content_hash("source_aggregate")
        return self._step_content_hash("timeline_digest")

    def _load_source_raw_asr_index(self) -> dict[str, Any]:
        aggregate = self._load_optional_step_json("source_aggregate", {})
        rel = str(aggregate.get("raw_asr_index_file") or "").strip() if isinstance(aggregate, dict) else ""
        if rel:
            path = self.task_dir / rel
            doc = read_json(path, {})
            if isinstance(doc, dict) and isinstance(doc.get("sources"), list):
                return doc

        source_aggregate_output = self._step_output("source_aggregate")
        if source_aggregate_output:
            candidate = (self.task_dir / source_aggregate_output).parent / "source_raw_asr_index.json"
            doc = read_json(candidate, {})
            if isinstance(doc, dict) and isinstance(doc.get("sources"), list):
                return doc

        raise UserFacingPipelineError(
            "source_raw_asr_index_missing",
            user_message="AI 配音微片段生成失败：source_raw_asr_index.json 不存在或格式无效。",
            suggestions=[
                "请从 source_aggregate 重新运行。",
                "检查 source_aggregate/v*/source_raw_asr_index.json 是否生成。",
                "确认当前任务走的是统一 source_prepare -> source_analysis -> source_aggregate 链路。",
            ],
            technical_detail={
                "source_aggregate_output": source_aggregate_output,
                "raw_asr_index_file": rel,
            },
        )

    def _source_raw_asr_index_hash(self) -> str:
        aggregate = self._load_optional_step_json("source_aggregate", {})
        rel = str(aggregate.get("raw_asr_index_file") or "").strip() if isinstance(aggregate, dict) else ""
        if rel:
            path = self.task_dir / rel
            if path.exists():
                return stable_hash(read_json(path, {}))

        source_aggregate_output = self._step_output("source_aggregate")
        if source_aggregate_output:
            candidate = (self.task_dir / source_aggregate_output).parent / "source_raw_asr_index.json"
            if candidate.exists():
                return stable_hash(read_json(candidate, {}))
        return ""

    def step_source_quality_check(self) -> None:
        if self._is_virtual_multi_source():
            sources_metadata: list[dict[str, Any]] = []
            source_hash_items: list[dict[str, Any]] = []
            for item in self._iter_source_items():
                meta: dict[str, Any] = {}
                try:
                    source_path = self._resolve_source_item_path(item)
                    if source_path.exists():
                        meta = ffprobe_json(source_path)
                except Exception as exc:
                    meta = {"probe_error": str(exc)}

                if item.get("duration_seconds") is not None:
                    meta.setdefault("duration", item.get("duration_seconds"))
                    meta.setdefault("duration_seconds", item.get("duration_seconds"))
                meta.setdefault("source_id", item.get("source_id"))
                meta.setdefault("source_index", item.get("source_index"))
                meta.setdefault("display_name", item.get("display_name", ""))
                meta.setdefault("original_path", item.get("original_path", ""))
                sources_metadata.append(meta)
                source_hash_items.append({
                    "source_id": item.get("source_id"),
                    "source_index": item.get("source_index"),
                    "duration_seconds": item.get("duration_seconds"),
                    "original_path": item.get("original_path"),
                })
            input_hash = stable_hash({
                "source_analysis": self._step_content_hash("source_analysis"),
                "sources": source_hash_items,
            })
        else:
            metadata_info = self._load_step_json("metadata")
            if metadata_info and isinstance(metadata_info.get("sources"), list):
                sources_metadata = [
                    s.get("metadata", {})
                    for s in metadata_info.get("sources", [])
                    if isinstance(s, dict)
                ]
            elif metadata_info:
                sources_metadata = [metadata_info]
            else:
                sources_metadata = []
            input_hash = self._step_content_hash("metadata")

        if self._can_reuse("source_quality_check", input_hash):
            print("复用缓存: source_quality_check")
            return
            
        version, vdir = self._version_dir("source_quality_check", "source_quality_check")
        ensure_dir(vdir)
        
        report = build_source_quality_report(sources_metadata)
        
        result = {
            "version": "source_quality_check_v1",
            "report": report,
            "source_count": len(sources_metadata),
        }
        out = write_json(vdir / "source_quality_check.json", result)
        
        status = "success" if report.get("is_pass", True) else "failed"
        status_doc = self._base_status("source_quality_check", version, input_hash, [out])
        status_doc["status"] = status
        self._write_status(vdir, status_doc)
        self._record_step(
            step="source_quality_check",
            version=version,
            status=status,
            output=relpath(out, self.task_dir),
            input_hash=input_hash,
            output_files=[out],
            extra={
                "summary": {
                    "is_pass": report.get("is_pass", True),
                    "reason": report.get("reason", ""),
                    "source_count": len(sources_metadata),
                }
            },
        )

        if not report.get("is_pass", True):
            raise RuntimeError(f"Source quality check failed: {report.get('reason')}")

        print("瀹屾垚: source_quality_check")

    def step_metadata(self) -> None:
        if self._is_virtual_multi_source():
            sources = self._iter_source_items()
            input_hash = stable_hash({"sources": [{"id": s.get("source_id"), "mtime": self._resolve_source_item_path(s).stat().st_mtime if self._resolve_source_item_path(s).exists() else 0} for s in sources]})
            if self._can_reuse("metadata", input_hash):
                print("复用缓存: metadata")
                return
            version, vdir = self._version_dir("metadata", "metadata")
            ensure_dir(vdir)
            metadata_list = []
            for item in sources:
                source = self._resolve_source_item_path(item)
                meta = ffprobe_json(source)
                metadata_list.append({
                    "source_id": item.get("source_id"),
                    "source_index": item.get("source_index"),
                    "duration_seconds": item.get("duration_seconds"),
                    "metadata": meta,
                })
            metadata = {
                "source_mode": "multi_source_pool",
                "timeline_mode": "source_pool_local_time",
                "duration": sum(m.get("duration_seconds", 0) for m in sources),
                "sources": metadata_list,
            }
            out = write_json(vdir / "video_metadata.json", metadata)
            status = self._base_status("metadata", version, input_hash, [out])
            self._write_status(vdir, status)
            self._record_step(step="metadata", version=version, status="success", output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out])
            print("完成: metadata")
            return

        source = self._source_video()
        input_hash = stable_hash({"source": str(source), "size": source.stat().st_size, "mtime": source.stat().st_mtime})
        if self._can_reuse("metadata", input_hash):
            print("复用缓存: metadata")
            return
        version, vdir = self._version_dir("metadata", "metadata")
        ensure_dir(vdir)
        metadata = ffprobe_json(source)
        metadata["source_video"] = relpath(source, self.task_dir)
        out = write_json(vdir / "video_metadata.json", metadata)
        status = self._base_status("metadata", version, input_hash, [out])
        self._write_status(vdir, status)
        self._record_step(step="metadata", version=version, status="success", output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out])
        print("完成: metadata")

    def step_audio_extract(self) -> None:
        if self._is_virtual_multi_source():
            input_hash = stable_hash({"source": self._step_content_hash("metadata"), "audio": "mono_16k_wav"})
            if self._can_reuse("audio_extract", input_hash):
                print("复用缓存: audio_extract")
                return
            version, vdir = self._version_dir("audio_extract", "preprocess/audio")
            ensure_dir(vdir)
            metadata = self._load_step_json("metadata")
            sources_meta = {m["source_id"]: m["metadata"] for m in metadata.get("sources", [])}
            manifest_items = []
            output_files = []
            for item in self._iter_source_items():
                source_id = item["source_id"]
                source = self._resolve_source_item_path(item)
                source_dir = ensure_dir(vdir / source_id)
                out = source_dir / "audio.wav"
                meta = sources_meta.get(source_id, {})
                try:
                    run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", str(out)])
                except Exception as exc:
                    duration = float(meta.get("duration") or 0)
                    has_video = bool(meta.get("width") or meta.get("height") or meta.get("video_codec"))
                    has_audio = bool(meta.get("audio_codec"))
                    if duration > 0 and has_video and has_audio:
                        try:
                            run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts", "-err_detect", "ignore_err", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", str(out)])
                        except Exception as retry_exc:
                            raise RuntimeError(self._source_media_error_message(source, meta, exc, retry_exc)) from retry_exc
                    elif duration > 0 and has_video and not has_audio:
                        run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=16000", "-t", str(duration), str(out)])
                    else:
                        raise RuntimeError(self._source_media_error_message(source, meta, exc)) from exc
                manifest_items.append({"source_id": source_id, "audio": relpath(out, self.task_dir)})
                output_files.append(out)
            
            manifest_out = write_json(vdir / "audio_manifest.json", {"sources": manifest_items})
            output_files.append(manifest_out)
            status = self._base_status("audio_extract", version, input_hash, output_files)
            self._write_status(vdir, status)
            self._record_step(step="audio_extract", version=version, status="success", output=relpath(manifest_out, self.task_dir), input_hash=input_hash, output_files=output_files)
            print("完成: audio_extract")
            return

        source = self._source_video()
        input_hash = stable_hash({"source": self._step_content_hash("metadata"), "audio": "mono_16k_wav"})
        if self._can_reuse("audio_extract", input_hash):
            print("复用缓存: audio_extract")
            return
        version, vdir = self._version_dir("audio_extract", "preprocess/audio")
        ensure_dir(vdir)
        out = vdir / "audio.wav"
        metadata = self._load_step_json("metadata")
        extra: dict[str, Any] = {}
        try:
            run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", str(out)])
        except Exception as exc:
            duration = float(metadata.get("duration") or 0)
            has_video = bool(metadata.get("width") or metadata.get("height") or metadata.get("video_codec"))
            has_audio = bool(metadata.get("audio_codec"))
            if duration > 0 and has_video and has_audio:
                try:
                    run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts", "-err_detect", "ignore_err", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", str(out)])
                    extra = {"degraded": True, "warning": "audio_extract first attempt failed; retry with tolerant ffmpeg flags succeeded"}
                except Exception as retry_exc:
                    raise RuntimeError(self._source_media_error_message(source, metadata, exc, retry_exc)) from retry_exc
            elif duration > 0 and has_video and not has_audio:
                run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=16000", "-t", str(duration), str(out)])
                extra = {"degraded": True, "warning": f"source has no audio track; generated {duration:.3f}s silent audio fallback"}
            else:
                raise RuntimeError(self._source_media_error_message(source, metadata, exc)) from exc
        status = self._base_status("audio_extract", version, input_hash, [out])
        status.update(extra)
        self._write_status(vdir, status)
        self._record_step(step="audio_extract", version=version, status="success", output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out], extra=extra)
        print("完成: audio_extract")

    def step_frame_extract(self) -> None:
        interval = 1 if self.options.mode == "dense" else self.options.frame_interval

        if self._is_virtual_multi_source():
            input_hash = stable_hash({"source": self._step_content_hash("metadata"), "interval": interval})
            if self._can_reuse("frame_extract", input_hash):
                print("复用缓存: frame_extract")
                return
            version, vdir = self._version_dir("frame_extract", "preprocess/frames")
            ensure_dir(vdir)
            all_frames = []
            output_files = []
            
            for item in self._iter_source_items():
                source_id = item["source_id"]
                source = self._resolve_source_item_path(item)
                source_dir = ensure_dir(vdir / source_id)
                pattern = str(source_dir / "frame_%06d.jpg")
                run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-vf", f"fps=1/{interval}", "-q:v", "3", pattern])
                frames = sorted(source_dir.glob("*.jpg"))
                
                for i, p in enumerate(frames):
                    local_seconds = i * interval
                    all_frames.append({
                        "source_id": source_id,
                        "index": i + 1,
                        "time": seconds_to_timecode(local_seconds, ms=True),
                        "seconds": local_seconds,
                        "file": relpath(p, self.task_dir)
                    })
                output_files.extend(frames[:5])

            manifest = {
                "frame_interval_seconds": interval,
                "frames": all_frames,
            }
            out = write_json(vdir / "frames.json", manifest)
            output_files.insert(0, out)
            self._write_status(vdir, self._base_status("frame_extract", version, input_hash, output_files))
            self._record_step(step="frame_extract", version=version, status="success", output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out])
            print(f"完成: frame_extract，共 {len(all_frames)} 帧")
            return

        source = self._source_video()
        interval = 1 if self.options.mode == "dense" else self.options.frame_interval
        input_hash = stable_hash({"source": self._step_content_hash("metadata"), "interval": interval})
        if self._can_reuse("frame_extract", input_hash):
            print("复用缓存: frame_extract")
            return
        version, vdir = self._version_dir("frame_extract", "preprocess/frames")
        frame_dir = ensure_dir(vdir / "frames")
        pattern = str(frame_dir / "frame_%06d.jpg")
        run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-vf", f"fps=1/{interval}", "-q:v", "3", pattern])
        frames = sorted(frame_dir.glob("*.jpg"))
        manifest = {
            "frame_interval_seconds": interval,
            "frames": [
                {"index": i + 1, "time": seconds_to_timecode(i * interval, ms=True), "seconds": i * interval, "file": relpath(p, self.task_dir)}
                for i, p in enumerate(frames)
            ],
        }
        out = write_json(vdir / "frames.json", manifest)
        self._write_status(vdir, self._base_status("frame_extract", version, input_hash, [out] + frames[:20]))
        self._record_step(step="frame_extract", version=version, status="success", output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out])
        print(f"完成: frame_extract，共 {len(frames)} 帧")

    def step_chunk_build(self) -> None:
        metadata = self._load_step_json("metadata")
        frames = self._load_step_json("frame_extract")
        chunk_seconds = self.options.chunk_seconds
        
        if self._is_virtual_multi_source():
            input_hash = stable_hash({"metadata": self._step_content_hash("metadata"), "frames": self._step_content_hash("frame_extract"), "chunk_seconds": chunk_seconds})
            if self._can_reuse("chunk_build", input_hash):
                print("复用缓存: chunk_build")
                return
            version, vdir = self._version_dir("chunk_build", "preprocess/chunks")
            ensure_dir(vdir)
            chunks = []
            frame_items = frames.get("frames", [])
            for s_idx, source in enumerate(metadata.get("sources", [])):
                source_id = source.get("source_id")
                duration = float(source.get("metadata", {}).get("duration") or 0)
                source_frames = [f for f in frame_items if f.get("source_id") == source_id]
                for idx in range(max(1, math.ceil(duration / chunk_seconds))):
                    local_start = idx * chunk_seconds
                    local_end = min(duration, (idx + 1) * chunk_seconds)
                    chunk_frames = [f for f in source_frames if local_start <= float(f.get("seconds", 0)) < local_end]
                    chunk_id = f"chunk_{s_idx + 1:03d}_{idx + 1:04d}"
                    chunks.append({
                        "source_id": source_id,
                        "chunk_id": chunk_id,
                        "local_start_seconds": local_start,
                        "local_end_seconds": local_end,
                        "local_start": seconds_to_timecode(local_start, ms=True),
                        "local_end": seconds_to_timecode(local_end, ms=True),
                        "time_range": f"{seconds_to_timecode(local_start, ms=True)}-{seconds_to_timecode(local_end, ms=True)}",
                        "frames": [f["file"] for f in chunk_frames],
                    })
            out = write_json(vdir / "chunks.json", {"chunks": chunks})
            self._write_status(vdir, self._base_status("chunk_build", version, input_hash, [out]))
            self._record_step(step="chunk_build", version=version, status="success", output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out])
            print(f"完成: chunk_build，共 {len(chunks)} 个片段")
            return

        input_hash = stable_hash({"metadata": metadata.get("duration"), "frames": self._step_content_hash("frame_extract"), "chunk_seconds": chunk_seconds})
        if self._can_reuse("chunk_build", input_hash):
            print("复用缓存: chunk_build")
            return
        version, vdir = self._version_dir("chunk_build", "preprocess/chunks")
        ensure_dir(vdir)
        chunks = []
        duration = float(metadata.get("duration") or 0)
        frame_items = frames.get("frames", [])
        for idx in range(max(1, math.ceil(duration / chunk_seconds))):
            start = idx * chunk_seconds
            end = min(duration, (idx + 1) * chunk_seconds)
            chunk_frames = [f for f in frame_items if start <= float(f.get("seconds", 0)) < end]
            chunks.append({
                "chunk_id": f"chunk_{idx + 1:04d}",
                "start": start,
                "end": end,
                "time_range": f"{seconds_to_timecode(start)}-{seconds_to_timecode(end)}",
                "frames": [f["file"] for f in chunk_frames],
            })
        out = write_json(vdir / "chunks.json", {"chunk_seconds": chunk_seconds, "chunks": chunks})
        self._write_status(vdir, self._base_status("chunk_build", version, input_hash, [out]))
        self._record_step(step="chunk_build", version=version, status="success", output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out])
        print(f"完成: chunk_build，共 {len(chunks)} 段")

    def step_asr(self) -> None:
        audio = self.task_dir / self._step_output("audio_extract")
        input_hash = stable_hash({
            "audio": self._step_content_hash("audio_extract"),
            "funasr": self.config.funasr,
            "mode": "segmented_asr_v1",
            "windows": self._asr_windows_from_chunks(),
        })
        if self._can_reuse("asr", input_hash):
            print("复用缓存: asr")
            return

        version, vdir = self._version_dir("asr", "asr")
        ensure_dir(vdir)
        write_json(vdir / "input.json", {"audio": relpath(audio, self.task_dir), "config": self.config.funasr})

        all_segments: list[dict[str, Any]] = []
        full_text_parts: list[str] = []
        raw_full_text_parts: list[str] = []
        raw_results: list[dict[str, Any]] = []
        err = ""
        out = vdir / "asr_segments.json"

        try:
            asr_slots = int(self.config.raw.get("gpu_limits", {}).get("asr_slots", 1))
            windows = self._asr_windows_from_chunks()

            self._update_step_progress("asr", "等待 ASR GPU 锁", {
                "total_segments": len(windows),
                "processed_segments": 0,
            })
            print("ASR: waiting gpu slot...", flush=True)

            with file_slot_lock("asr", slots=asr_slots):
                self._update_step_progress("asr", "加载 FunASR 模型", {
                    "total_segments": len(windows),
                    "processed_segments": 0,
                })
                print("ASR: loading FunASR model...", flush=True)
                engine = self._get_asr_engine()

                for index, window in enumerate(windows, start=1):
                    wid = str(window["id"])
                    start = float(window["start"])
                    end = float(window["end"])
                    seg_audio = vdir / "segments_audio" / f"{wid}.wav"

                    source_id = window.get("source_id")
                    if source_id:
                        audio_manifest = self._load_step_json("audio_extract")
                        source_audio = next((Path(self.task_dir) / s["audio"] for s in audio_manifest.get("sources", []) if s["source_id"] == source_id), audio)
                        local_start = float(window["local_start"])
                        local_end = float(window["local_end"])
                        self._update_step_progress("asr", f"切分音频 {index}/{len(windows)}", {
                            "current_segment": wid,
                            "processed_segments": index - 1,
                            "total_segments": len(windows),
                            "current_range": [start, end],
                        })
                        self._cut_audio_segment(source_audio, seg_audio, local_start, local_end)
                    else:
                        self._update_step_progress("asr", f"切分音频 {index}/{len(windows)}", {
                            "current_segment": wid,
                            "processed_segments": index - 1,
                            "total_segments": len(windows),
                            "current_range": [start, end],
                        })
                        self._cut_audio_segment(audio, seg_audio, start, end)

                    self._update_step_progress("asr", f"FunASR 转写中 {index}/{len(windows)}", {
                        "current_segment": wid,
                        "processed_segments": index - 1,
                        "total_segments": len(windows),
                        "current_range": [start, end],
                    })
                    print(f"ASR: transcribing {wid} {index}/{len(windows)} {start:.1f}-{end:.1f}s", flush=True)

                    result = engine.transcribe(str(seg_audio))
                    raw_results.append({"id": wid, "range": [start, end], "result": result})

                    if not result.get("success"):
                        raise RuntimeError(f"{wid} ASR failed: {result.get('error')}")

                    raw_text = str(result.get("text", ""))
                    raw_full_text_parts.append(raw_text)
                    full_text_parts.append(clean_asr_text(raw_text)["clean_text"])
                    for seg in result.get("segments", []):
                        local_start = float(seg.get("start") or 0)
                        local_end = float(seg.get("end") or 0)
                        cleaned = clean_asr_text(str(seg.get("text") or ""))
                        text = cleaned["clean_text"]
                        if not text:
                            continue
                        all_segments.append({
                            "start": start + local_start,
                            "end": start + local_end if local_end > 0 else end,
                            "text": text,
                            "raw_text": cleaned["raw_text"],
                            "removed_tokens": cleaned["removed_tokens"],
                            "asr_segment_id": wid,
                            "chunk_id": window.get("chunk_id", ""),
                        })

                    self._update_step_progress("asr", f"FunASR 已完成 {index}/{len(windows)}", {
                        "current_segment": wid,
                        "processed_segments": index,
                        "total_segments": len(windows),
                    })

            full_text = " ".join(x for x in full_text_parts if x).strip()
            segments = self._normalize_asr_segments(all_segments, full_text)
            asr = {
                "language": "zh",
                "segments": segments,
                "full_text": full_text,
                "raw_full_text": " ".join(str(x) for x in raw_full_text_parts if x).strip(),
                "raw_result": {
                    "mode": "segmented_asr_v1",
                    "segment_count": len(windows),
                    "results": raw_results,
                },
            }
            out = write_json(vdir / "asr_segments.json", asr)
            write_text(vdir / "full_text.txt", asr["full_text"])
            status = "success"
        except Exception as exc:
            err = str(exc)
            asr = {"language": "zh", "segments": [], "full_text": "", "error": err, "raw_result": raw_results}
            out = write_json(vdir / "asr_segments.json", asr)
            write_text(vdir / "full_text.txt", "")
            status = "failed"

        step_status = self._base_status("asr", version, input_hash, [out])
        if err:
            step_status["error"] = err
        self._write_status(vdir, step_status)
        self._record_step(step="asr", version=version, status=status, output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out])
        if status != "success":
            raise RuntimeError(f"ASR 失败: {err}")
        print("完成: asr", flush=True)

    def _get_asr_engine(self):
        if self._asr_engine is not None:
            return self._asr_engine

        from services.asr.FunASREngine import FunASREngine

        model_base = self.config.resolve_path(self.config.funasr.get("asr_model_path"), "models/iic")
        self._asr_engine = FunASREngine(
            model_name=str(model_base / "SenseVoiceSmall"),
            vad_model=str(model_base / "speech_fsmn_vad_zh-cn-16k-common-pytorch"),
            punc_model=str(model_base / "punc_ct-transformer_cn-en-common-vocab471067-large"),
            device=self.config.funasr.get("device", "cuda:0"),
        )
        return self._asr_engine

    def step_asr_digest(self) -> None:
        cfg = self.config.raw.get("asr_digest", {})
        chunks_doc = self._load_step_json("chunk_build")
        asr = self._load_step_json("asr")
        chunks = chunks_doc.get("chunks", [])
        model = str(cfg.get("model_name") or self._text_model_name())
        fallback = list(cfg.get("fallback_models") or [])
        max_workers = max(1, int(cfg.get("max_workers", 4) or 4))
        input_hash = stable_hash({
            "chunks": self._step_content_hash("chunk_build"),
            "asr": self._step_content_hash("asr"),
            "model": model,
            "fallback": fallback,
            "cfg": cfg,
        })
        if self._can_reuse("asr_digest", input_hash):
            print("澶嶇敤缂撳瓨: asr_digest")
            return

        version, vdir = self._version_dir("asr_digest", "asr_digest")
        ensure_dir(vdir)
        asr_by_chunk_id: dict[str, str] = {}
        for chunk in chunks:
            chunk_id = str(chunk.get("chunk_id") or "")
            segs = self._segments_in_range(asr.get("segments", []), float(chunk.get("start") or 0), float(chunk.get("end") or 0))
            asr_by_chunk_id[chunk_id] = " ".join(str(s.get("text") or "") for s in segs).strip()

        def process_one(index: int, chunk: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            chunk_id = str(chunk.get("chunk_id") or f"chunk_{index + 1:04d}")
            raw_text = asr_by_chunk_id.get(chunk_id, "")
            ts = self._format_chunk_ts(chunk)
            if not raw_text.strip():
                return index, {"chunk_id": chunk_id, "speech": ""}

            target_chars = int(cfg.get("target_chars_per_chunk", 320) or 320)
            input_data = {
                "chunk_id": chunk_id,
                "time_range": ts,
                "asr_text": raw_text[: int(cfg.get("max_input_chars_per_chunk", 4000) or 4000)],
                "source_id": chunk.get("source_id", ""),
                "language": str(cfg.get("language", "") or "zh"),
            }
            write_json(vdir / chunk_id / "input.json", input_data)
            try:
                if self.llm_text is None:
                    raise RuntimeError("missing text llm")
                result = self.llm_text.call_json(
                    model=model,
                    fallback_models=fallback,
                    prompt=prompts.ASR_DIGEST_PROMPT,
                    input_data=input_data,
                    temperature=float(cfg.get("temperature", 0.0) or 0.0),
                    max_tokens=int(cfg.get("max_tokens", 600) or 600),
                    debug_dir=vdir / "_llm_debug" / chunk_id,
                )
                parsed = result.parsed if isinstance(result.parsed, dict) else {}
                speech = str(parsed.get("speech") or "").strip()
                if not speech:
                    speech = compact_asr_for_llm(raw_text, max_chars=target_chars)
            except Exception as exc:
                write_json(vdir / "_errors" / f"{chunk_id}.json", {"chunk_id": chunk_id, "error": str(exc), "created_at": now_iso()})
                speech = compact_asr_for_llm(raw_text, max_chars=target_chars)
            return index, {"chunk_id": chunk_id, "speech": speech}

        results: list[tuple[int, dict[str, Any]]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_one, index, chunk) for index, chunk in enumerate(chunks)]
            for future in as_completed(futures):
                results.append(future.result())

        results.sort(key=lambda x: x[0])
        output = {
            "version": "asr_digest_v1",
            "model": model,
            "chunks": [item for _index, item in results],
        }
        out = write_json(vdir / "asr_digest.json", output)
        self._write_status(vdir, self._base_status("asr_digest", version, input_hash, [out]))
        self._record_step(
            step="asr_digest",
            version=version,
            status="success",
            output=relpath(out, self.task_dir),
            input_hash=input_hash,
            output_files=[out],
            extra={"summary": {"chunks": len(output["chunks"]), "max_workers": max_workers}, "model": model},
        )
        print("瀹屾垚: asr_digest")

    def step_vision(self) -> None:
        if self.llm_vision is None:
            raise RuntimeError("缺少 vision LLM 配置")
        chunks_doc = self._load_step_json("chunk_build")
        asr = self._load_step_json("asr")
        asr_digest = self._load_optional_step_json("asr_digest", {"chunks": []})
        asr_digest_by_id = {
            str(item.get("chunk_id")): str(item.get("speech") or "")
            for item in asr_digest.get("chunks", [])
            if isinstance(item, dict)
        }
        llm_cfg = self.config.llm
        provider = llm_cfg.get("vision_llm_provider", "openai")
        model = self.options.model or llm_cfg.get(f"vision_{provider}_model_name")
        fallback = llm_cfg.get(f"vision_{provider}_fallback_models", [])
        prompt_version = self.options.prompt_version or "vision_chunk_v1"
        base_input_hash = stable_hash({
            "chunks": self._step_content_hash("chunk_build"),
            "asr": self._step_content_hash("asr"),
            "asr_digest": self._step_content_hash("asr_digest"),
            "model": model,
            "fallback": fallback,
            "prompt_version": prompt_version,
            "mode": self.options.mode,
            "vision_max_frames_per_chunk": self.options.vision_max_frames_per_chunk,
        })
        chunk_filter = self.options.chunk
        if self._can_reuse("vision", base_input_hash) and not chunk_filter and not self.options.failed_only:
            print("复用缓存: vision")
            return
        version, vdir = self._version_dir("vision", "vision")
        ensure_dir(vdir)
        previous_version = self.manifest.get("current_versions", {}).get("vision")
        previous_dir = self.task_dir / "vision" / previous_version if previous_version else None
        chunk_records = []
        chunks_to_process = []
        for chunk in chunks_doc.get("chunks", []):
            cid = chunk["chunk_id"]
            cdir = vdir / cid
            previous_cdir = previous_dir / cid if previous_dir else None
            should_run = (not chunk_filter or cid == chunk_filter)
            if self.options.failed_only and previous_cdir:
                prev_status = read_json(previous_cdir / "step_status.json", {})
                should_run = prev_status.get("status") != "success"
            if not should_run and previous_cdir and previous_cdir.exists():
                if not cdir.exists():
                    shutil.copytree(previous_cdir, cdir)
                parsed = read_json(cdir / "parsed_result.json", {})
                chunk_records.append({"chunk_id": cid, "status": "reused", "result_file": relpath(cdir / "parsed_result.json", self.task_dir), "result": parsed})
                continue
            chunks_to_process.append(chunk)

        def process_one_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
            cid = chunk["chunk_id"]
            cdir = ensure_dir(vdir / cid)
            asr_segments = self._segments_in_range(asr.get("segments", []), chunk["start"], chunk["end"])
            frame_paths = [str(self.task_dir / f) for f in chunk.get("frames", []) if (self.task_dir / f).exists()]
            frame_paths = select_frames_for_vision(frame_paths, max_frames=max(1, int(self.options.vision_max_frames_per_chunk or 1)))
            input_data = {
                "chunk_id": cid,
                "time_range": chunk.get("time_range", ""),
                "source_id": chunk.get("source_id", ""),
                "frames": f"{len(frame_paths)} key frames attached as images",
                "asr_text": asr_digest_by_id.get(cid) or compact_asr_for_llm(" ".join(s.get("text", "") for s in asr_segments), max_chars=500),
            }
            write_json(cdir / "input.json", input_data)
            write_text(cdir / "prompt.txt", prompts.VISION_CHUNK_PROMPT)
            try:
                result = self.llm_vision.call_json(
                    model=model,
                    fallback_models=fallback,
                    prompt=prompts.VISION_CHUNK_PROMPT,
                    input_data=input_data,
                    image_paths=frame_paths,
                    temperature=0.1,
                    debug_dir=cdir / "_llm_debug",
                )
                parsed = result.parsed
                parsed = self._materialize_vision_chunk_result(parsed, chunk_id=cid, time_range=chunk["time_range"])
                raw = {
                    "model": result.model,
                    "created_at": now_iso(),
                    "raw_text": result.raw_text,
                    "usage": result.usage,
                    "finish_reason": result.finish_reason,
                    "latency_ms": result.latency_ms,
                }
                write_json(cdir / "raw_response.json", raw)
                write_json(cdir / "parsed_result.json", parsed)
                status = "success"
                error = ""
            except Exception as exc:
                parsed = {"chunk_id": cid, "time_range": chunk["time_range"], "error": str(exc)}
                write_json(cdir / "raw_response.json", {"error": str(exc), "created_at": now_iso()})
                write_json(cdir / "parsed_result.json", parsed)
                status = "failed"
                error = str(exc)
            cstatus = self._base_status("vision_chunk_analysis", version, stable_hash(input_data), [cdir / "parsed_result.json"])
            cstatus.update({"chunk_id": cid, "status": status, "model": model, "prompt_version": prompt_version, "error": error})
            self._write_status(cdir, cstatus)
            return {"chunk_id": cid, "status": status, "result_file": relpath(cdir / "parsed_result.json", self.task_dir), "result": parsed}

        max_workers = max(1, int(self.options.vision_max_workers or 1))
        total_chunks = len(chunks_to_process) + len(chunk_records)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_one_chunk, chunk) for chunk in chunks_to_process]
            for future in as_completed(futures):
                record = future.result()
                chunk_records.append(record)

                success = sum(1 for c in chunk_records if c["status"] in {"success", "reused", "manual_edited"})
                failed = sum(1 for c in chunk_records if c["status"] == "failed")
                processed = len(chunk_records)

                self.manifest.setdefault("steps", {}).setdefault("vision", {})
                self.manifest["steps"]["vision"].update(
                    {
                        "step_name": "vision",
                        "status": "running",
                        "updated_at": now_iso(),
                        "can_rerun": False,
                        "current_chunk": record.get("chunk_id", ""),
                        "summary": {
                            "total_chunks": total_chunks,
                            "processed_chunks": processed,
                            "success_chunks": success,
                            "failed_chunks": failed,
                        },
                    }
                )
                self._save_manifest()

                print(f"vision {record['chunk_id']}: {record['status']}")

        chunk_records.sort(key=lambda x: x.get("chunk_id", ""))
        success = sum(1 for c in chunk_records if c["status"] in {"success", "reused", "manual_edited"})
        failed = sum(1 for c in chunk_records if c["status"] == "failed")
        analysis = {
            "version": version,
            "chunks": [{k: v for k, v in c.items() if k != "result"} for c in chunk_records],
            "results": [c["result"] for c in chunk_records],
            "summary": {"total_chunks": len(chunk_records), "success_chunks": success, "failed_chunks": failed},
        }
        out = write_json(vdir / "visual_analysis.json", analysis)
        overall_status = "success" if failed == 0 else "partial_success"
        self._write_status(vdir, self._base_status("vision", version, base_input_hash, [out]) | {"status": overall_status})
        self._record_step(step="vision", version=version, status=overall_status, output=relpath(out, self.task_dir), input_hash=base_input_hash, output_files=[out], extra={"summary": analysis["summary"]})
        print("完成: vision")

    def step_timeline(self) -> None:
        asr = self._load_step_json("asr")
        asr_digest = self._load_optional_step_json("asr_digest", {"chunks": []})
        vision = self._load_step_json("vision")
        chunks = self._load_step_json("chunk_build")
        asr_digest_by_id = {
            str(item.get("chunk_id")): str(item.get("speech") or "")
            for item in asr_digest.get("chunks", [])
            if isinstance(item, dict)
        }
        input_hash = stable_hash({
            "asr": self._step_content_hash("asr"),
            "asr_digest": self._step_content_hash("asr_digest"),
            "vision": self._step_content_hash("vision"),
            "chunks": self._step_content_hash("chunk_build"),
        })
        if self._can_reuse("timeline", input_hash):
            print("复用缓存: timeline")
            return
        version, vdir = self._version_dir("timeline", "timeline")
        ensure_dir(vdir)
        visual_by_id = {r.get("chunk_id"): r for r in vision.get("results", []) if isinstance(r, dict)}
        timeline = []
        for chunk in chunks.get("chunks", []):
            vr = self._apply_manual_override(vdir, visual_by_id.get(chunk["chunk_id"], {}), "vision", chunk["chunk_id"])
            chunk_start = float(chunk.get("local_start_seconds", chunk.get("start", 0)) or 0)
            chunk_end = float(chunk.get("local_end_seconds", chunk.get("end", chunk_start)) or chunk_start)
            segs = self._segments_in_range(asr.get("segments", []), chunk_start, chunk_end)
            t_entry = {
                "chunk_id": chunk["chunk_id"],
                "asr_text": clean_asr_text(" ".join(str(s.get("clean_text") or s.get("text") or "") for s in segs))["clean_text"],
                "asr_digest": asr_digest_by_id.get(str(chunk["chunk_id"]), ""),
                "asr_segments": segs,
                "scene_type": vr.get("scene_type", ""),
                "visual_summary": vr.get("visual_summary") or vr.get("visual", ""),
                "screen_text": vr.get("screen_text", []),
                "visible_people": vr.get("visible_people", []),
                "is_live_scene": vr.get("is_live_scene", False),
                "is_archive_footage": vr.get("is_archive_footage", False),
                "visual_value_score": vr.get("visual_value_score", 0),
                "hook_score": vr.get("hook_score", 0),
                "risk_tags": vr.get("risk_tags", []),
                "notes": vr.get("notes", ""),
            }
            if "source_id" in chunk:
                t_entry["source_id"] = chunk["source_id"]
                t_entry["local_start_seconds"] = chunk_start
                t_entry["local_end_seconds"] = chunk_end
                t_entry["local_start"] = seconds_to_timecode(chunk_start, ms=True)
                t_entry["local_end"] = seconds_to_timecode(chunk_end, ms=True)
            else:
                t_entry["start"] = seconds_to_timecode(chunk_start, ms=True)
                t_entry["end"] = seconds_to_timecode(chunk_end, ms=True)
                t_entry["start_seconds"] = chunk_start
                t_entry["end_seconds"] = chunk_end
            timeline.append(t_entry)
        out = write_json(vdir / "merged_timeline.json", {"timeline": timeline, "source_versions": {"asr": self._step_content_hash("asr"), "asr_digest": self._step_content_hash("asr_digest"), "vision": self._step_content_hash("vision")}})
        self._write_status(vdir, self._base_status("timeline", version, input_hash, [out]))
        self._record_step(step="timeline", version=version, status="success", output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out])
        print("完成: timeline")

    def step_timeline_digest(self) -> None:
        timeline_data = self._load_step_json("timeline")
        timeline = timeline_data.get("timeline", [])
        llm_input_cfg = self.config.raw.get("llm_input", {})
        digest_version = "llm_timeline_digest_v2_asr_digest"
        input_hash = stable_hash({
            "timeline": self._step_content_hash("timeline"),
            "digest_version": digest_version,
            "llm_input": llm_input_cfg,
        })
        if self._can_reuse("timeline_digest", input_hash):
            print("复用缓存: timeline_digest")
            return
        version, vdir = self._version_dir("timeline_digest", "timeline")
        ensure_dir(vdir)
        chunks = []
        for item in timeline:
            chunks.append({
                "chunk_id": item.get("chunk_id", ""),
                "time": f"{item.get('start', '')}-{item.get('end', '')}",
                "start_seconds": item.get("start_seconds", 0),
                "end_seconds": item.get("end_seconds", 0),
                "speech": compact_asr_for_llm(item.get("asr_digest") or item.get("asr_text", ""), max_chars=int(llm_input_cfg.get("max_asr_chars_per_chunk", 320))),
                "visual": compact_text(item.get("visual_summary", ""), max_chars=int(llm_input_cfg.get("max_visual_chars_per_chunk", 160))),
                "screen_text": compact_list(item.get("screen_text", []), max_items=int(llm_input_cfg.get("max_screen_text_items", 5))),
                "people": compact_list(item.get("visible_people", []), max_items=int(llm_input_cfg.get("max_people_items", 5))),
                "scene": item.get("scene_type", ""),
                "visual_score": item.get("visual_value_score", 0),
                "hook_score": item.get("hook_score", 0),
                "flags": build_chunk_flags(item),
            })
        output = {
            "version": digest_version,
            "source_versions": {"timeline": self._step_content_hash("timeline")},
            "video_duration_seconds": max((float(x.get("end_seconds") or 0) for x in chunks), default=0.0),
            "chunks": chunks,
        }
        out = write_json(vdir / "llm_timeline_digest.json", output)
        self._write_status(vdir, self._base_status("timeline_digest", version, input_hash, [out]))
        self._record_step(step="timeline_digest", version=version, status="success", output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out], extra={"summary": {"chunks": len(chunks)}})
        print("完成: timeline_digest")

    def _format_source_aggregate_text(self, aggregate: dict[str, Any]) -> str:
        if not aggregate or not aggregate.get("sources"):
            return ""
        lines = ["素材概览："]
        for src in aggregate.get("sources", []):
            idx = src.get("source_index", "")
            name = src.get("display_name", "")
            duration = float(src.get("duration_seconds", 0))
            summary = src.get("summary", "")
            facts = " ".join(src.get("key_facts", []))
            dur_str = f"{int(duration//60)}分{int(duration%60)}秒"
            
            lines.append(f"素材{idx}《{name}》，时长{dur_str}。")
            if summary:
                lines.append(f"摘要：{summary}")
            if facts:
                lines.append(f"要点：{facts}")
            lines.append("")
        return "\n".join(lines).strip()

    def _format_timeline_chunks_text(self, digest: dict[str, Any], max_chunks: int | None = None) -> str:
        chunks = digest.get("chunks", [])
        if max_chunks is not None:
            chunks = chunks[:max_chunks]
        
        lines = [
            "格式说明：每个片段都包含 chunk_id、source_id、时间段，随后两行依次为声音内容、画面内容。",
            "",
            "片段列表："
        ]
        for c in chunks:
            chunk_id = c.get("chunk_id", "")
            source_id = c.get("source_id") or "source_1"
            source_index = c.get("source_index", "")
            time_range = c.get("time_range") or local_time_range(c)
            speech = sanitize_llm_text(str(c.get("speech") or ""), max_chars=500) or "无"
            visual = str(c.get("visual") or "").strip() or "无"
            
            lines.append(f"[{chunk_id}] source_id={source_id} source_index={source_index} time={time_range}")
            lines.append(speech)
            lines.append(visual)
            lines.append("")
        return "\n".join(lines).strip()

    def _build_content_analysis_brief_text(self) -> str:
        digest = self._load_current_timeline_digest_for_llm("content_analysis")
        aggregate = self._load_compact_source_aggregate_for_llm()
        opts = self._run_options_payload_minimal()
        
        lines = [
            "任务：请判断这些素材的新闻主题、素材关系，并提取适合 AI 配音解说的候选片段。",
            ""
        ]
        if opts:
            lines.append("运行参数：")
            lines.append(f"- output_mode：{opts.get('output_mode')}")
            lines.append(f"- max_output_videos：{opts.get('max_output_videos')}")
            lines.append("")
            
        src_text = self._format_source_aggregate_text(aggregate)
        if src_text:
            lines.append(src_text)
            lines.append("")
            
        lines.append(self._format_timeline_chunks_text(digest, max_chunks=20))
        return "\n".join(lines).strip()

    def _build_video_understanding_brief_text(self) -> str:
        digest = self._load_current_timeline_digest_for_llm("video_understanding")
        aggregate = self._load_compact_source_aggregate_for_llm()
        
        lines = [
            "任务：请理解这些素材的新闻内容、叙事结构、人物/地点/事件关系，并判断哪些部分适合做原声高光重组。",
            ""
        ]
        src_text = self._format_source_aggregate_text(aggregate)
        if src_text:
            lines.append(src_text)
            lines.append("")
            
        lines.append(self._format_timeline_chunks_text(digest))
        return "\n".join(lines).strip()

    def _build_highlight_detection_brief_text(self) -> str:
        digest = self._load_current_timeline_digest_for_llm("highlight_detection")
        video_analysis = self._load_step_json("video_understanding")
        
        lines = [
            "任务：请从以下片段中找适合原声重组的高光片段。",
            ""
        ]
        if video_analysis:
            lines.append("整体理解：")
            lines.append(json.dumps({
                "main_topic": video_analysis.get("main_topic", ""),
                "video_type": video_analysis.get("video_type", ""),
                "summary": video_analysis.get("summary") or video_analysis.get("understanding", ""),
                "key_facts": video_analysis.get("key_facts", []),
            }, ensure_ascii=False))
            lines.append("")
            
        lines.append(self._format_timeline_chunks_text(digest))
        return "\n".join(lines).strip()

    def _asr_event_segments(self) -> list[dict[str, Any]]:
        digest = self._load_current_timeline_digest()
        chunks = digest.get("chunks", []) if isinstance(digest, dict) else []
        segments: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks, start=1):
            if not isinstance(chunk, dict):
                continue
            speech = clean_asr_text(str(chunk.get("speech") or chunk.get("asr_text") or chunk.get("asr_digest") or ""))["clean_text"]
            if not speech:
                continue
            source_id = str(chunk.get("source_id") or "source_1")
            local_start = float(chunk.get("local_start_seconds", chunk.get("start_seconds", chunk.get("start", 0))) or 0)
            local_end = float(chunk.get("local_end_seconds", chunk.get("end_seconds", chunk.get("end", local_start))) or local_start)
            if local_end <= local_start and chunk.get("duration_seconds"):
                local_end = local_start + float(chunk.get("duration_seconds") or 0)
            if local_end <= local_start:
                continue
            source_index = chunk.get("source_index", 1 if source_id == "source_1" else "")
            segment_id = str(chunk.get("segment_id") or f"{source_id}_seg_{index:04d}")
            segments.append({
                "segment_id": segment_id,
                "source_id": source_id,
                "source_index": source_index,
                "source_start_seconds": round(local_start, 3),
                "source_end_seconds": round(local_end, 3),
                "time_range_for_reference": f"{seconds_to_timecode(local_start, ms=True)}-{seconds_to_timecode(local_end, ms=True)}",
                "speech": compact_text(speech, max_chars=160),
                "nearby_context": "",
                "visual_summary": compact_text(str(chunk.get("visual") or chunk.get("visual_summary") or ""), max_chars=80),
                "source_chunk_ids": [str(chunk.get("chunk_id") or chunk.get("global_chunk_id") or f"chunk_{index:04d}")],
            })
        return segments

    def _format_asr_event_segment_for_llm(self, segment: dict[str, Any]) -> str:
        segment_id = str(segment.get("segment_id") or "").strip()
        source_id = str(segment.get("source_id") or "").strip()
        time_range = str(segment.get("time_range_for_reference") or "").strip()
        speech = compact_text(str(segment.get("speech") or ""), max_chars=160)
        visual = compact_text(str(segment.get("visual_summary") or ""), max_chars=80)

        lines = [f"[{segment_id}] src={source_id} t={time_range}"]
        if speech:
            lines.append(f"声：{speech}")
        if visual:
            lines.append(f"画：{visual}")
        return "\n".join(lines)

    def _build_asr_event_candidate_text(self) -> str:
        video = self._load_optional_step_json("video_understanding", {})
        segments = self._asr_event_segments()

        llm_cfg = self.config.raw.get("llm_input", {})
        limit = int(
            llm_cfg.get(
                "max_asr_event_candidate_input_chars",
                llm_cfg.get("max_text_agent_input_chars", 20000),
            )
            or 20000
        )
        budget = max(6000, int(limit * 0.75))

        main_topic = compact_text(str(video.get("main_topic") or video.get("topic") or ""), max_chars=120)
        summary = compact_text(str(video.get("summary") or video.get("understanding") or ""), max_chars=240)

        lines = [
            "任务：请从下面的 ASR 片段中选择适合原声高光重组的新闻事件候选。",
            "要求：",
            "1. 只选择信息密度高、能独立表达新闻事件的片段。",
            "2. 半句话、弱背景、重复信息、无明确事件的片段不要选。",
            "3. 同一 source 内连续相关片段可以合并。",
            "4. 不要跨 source 合并，因为不同 source 是独立素材。",
            "",
            "视频理解：",
            f"主题：{main_topic}",
            f"摘要：{summary}",
            "",
            "候选片段：",
        ]

        included = 0
        for segment in segments:
            block = self._format_asr_event_segment_for_llm(segment)
            candidate_size = len("\n".join(lines)) + len(block) + 2
            if candidate_size > budget:
                remaining = len(segments) - included
                lines.append(f"【输入已压缩】后续 {remaining} 个片段未展开；请只基于已给片段选择。")
                break
            lines.append(block)
            lines.append("")
            included += 1

        lines.append('输出 JSON 只允许：{"keep":["segment_id"],"merge":[["segment_id1","segment_id2"]]}')
        lines.append("不要输出时间码，不要解释。")
        return "\n".join(lines).strip()

    def _build_candidate_filter_text(self) -> str:
        video = self._load_optional_step_json("video_understanding", {})
        pool = self._load_candidate_clip_pool(filtered=False)
        llm_cfg = self.config.raw.get("llm_input", {})
        max_clips = int(llm_cfg.get("max_llm_candidate_clips", llm_cfg.get("max_candidate_clips_for_edit_plan", 18)) or 18)
        payload = {
            "production_mode": self.options.production_mode,
            "main_topic": video.get("main_topic") or video.get("topic") or "",
            "summary": video.get("summary") or video.get("understanding") or "",
            "candidate_clips": [
                {
                    "clip_id": clip.get("clip_id"),
                    "source_id": clip.get("source_id"),
                    "source_index": clip.get("source_index"),
                    "duration_seconds": clip.get("duration_seconds"),
                    "speech": compact_text(str(clip.get("speech") or clip.get("asr_text") or ""), max_chars=500),
                    "visual": compact_text(str(clip.get("visual") or clip.get("visual_summary") or ""), max_chars=260),
                    "summary": compact_text(str(clip.get("summary") or clip.get("event_summary") or ""), max_chars=320),
                    "event_type": clip.get("event_type", ""),
                    "visual_support": clip.get("visual_support", "unknown"),
                    "independent": clip.get("independent", True),
                    "needs_context": clip.get("needs_context", False),
                }
                for clip in pool[:max_clips]
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)


    def step_asr_micro_segment(self) -> None:
        cfg = self.config.raw.get("asr_micro_segment", {})
        if not cfg.get("enabled", True):
            self._mark_skipped("asr_micro_segment", "disabled in config")
            return

        input_hash = stable_hash({
            "source_raw_asr_index": self._source_raw_asr_index_hash() if self._is_virtual_source_manifest() else "",
            "source_aggregate": self._step_content_hash("source_aggregate") if "source_aggregate" in self.manifest.get("steps", {}) else None,
            "timeline_digest_visual_context": self._current_timeline_digest_hash(),
            "asr_micro_segment_cfg": cfg,
            "prompt_version": AGENT_INFO["asr_micro_segment"][3],
            "cache_version": "raw_asr_index_file_v3",
        })
        if self._can_reuse("asr_micro_segment", input_hash):
            print("复用缓存: asr_micro_segment")
            return

        version, vdir = self._version_dir("asr_micro_segment", "asr_micro_segment")
        ensure_dir(vdir)

        sentences = self._build_asr_sentence_units()
        if not sentences and cfg.get("fail_on_empty", True):
            raise UserFacingPipelineError(
                "asr_micro_segment_no_raw_asr",
                user_message="ASR 微分段失败：source_raw_asr_index 为空，无法从真实 ASR 时间生成 micro_segment。",
                suggestions=["回查 source_aggregate/v*/source_raw_asr_index.json。", "确认每个 source_analysis 子任务已经生成 asr_segments.json。"],
            )
        if cfg.get("include_visual_summary", True):
            sentences = self._attach_visual_to_asr_sentences(sentences)
            
        write_json(vdir / "asr_sentences.json", sentences)

        from collections import defaultdict
        source_groups = defaultdict(list)
        for s in sentences:
            source_groups[s.get("source_id", "source_1")].append(s)

        groups = []
        raw_responses = []
        inputs = []
        for source_id, source_sentences in source_groups.items():
            windows = self._window_sentences(
                source_sentences,
                window_size=cfg.get("window_sentence_count", 40),
                overlap=cfg.get("window_overlap_sentence_count", 5),
            )
            for window in windows:
                text_input = self._build_asr_micro_segment_text(window, source_id)
                inputs.append(text_input)
                result = self._run_text_agent("asr_micro_segment", text_input)
                raw_responses.append(result)
                groups.extend(result.get("groups", []))

        write_json(vdir / "raw_response.json", raw_responses)
        write_text(vdir / "input.txt", "\n\n---\n\n".join(inputs))

        groups, group_diagnostics = self._dedupe_and_validate_sentence_groups(
            groups,
            sentences,
            allow_sentence_fallback=bool(cfg.get("allow_sentence_fallback", False)),
        )
        group_diagnostics_out = write_json(vdir / "group_diagnostics.json", group_diagnostics)
        segments = self._materialize_micro_segments_from_sentence_groups(groups, sentences)
        segments = self._enforce_micro_segment_limits(segments, cfg, sentences)
        fail_on_empty_groups = bool(cfg.get("fail_on_empty_groups", cfg.get("fail_on_empty", True)))
        if not groups and fail_on_empty_groups:
            raise UserFacingPipelineError(
                "asr_micro_segment_empty_groups",
                user_message="ASR 微分段失败：模型没有返回任何可用 sentence group。",
                suggestions=["回查 agents/asr_micro_segment/v*/raw_response.json。", "检查模型输出 sentence_ids 是否来自输入。"],
                technical_detail=group_diagnostics,
            )
        if not segments and cfg.get("fail_on_empty", True):
            raise UserFacingPipelineError(
                "asr_micro_segment_empty",
                user_message="ASR 微分段失败：没有生成可用 micro_segment。",
                suggestions=["回查 agents/asr_micro_segment/v*/asr_sentences.json。", "检查 ASR 文本是否为空或时间是否无效。"],
            )

        output = {
            "version": "asr_micro_segment_raw_asr_v2",
            "segments": segments,
            "diagnostics": group_diagnostics,
        }
        out = write_json(vdir / "micro_segments.json", output)
        
        self._write_status(vdir, self._base_status("asr_micro_segment", version, input_hash, [out, group_diagnostics_out]))
        self._record_step(step="asr_micro_segment", version=version, status="success", output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out, group_diagnostics_out], extra={"summary": {"segments": len(segments), "groups": len(groups), "missing_sentences": len(group_diagnostics.get("missing_sentence_ids", []))}})
        print("完成: asr_micro_segment")

    def _build_asr_sentence_units(self) -> list[dict[str, Any]]:
        if not self._is_virtual_source_manifest():
            raise UserFacingPipelineError(
                "asr_micro_segment_requires_unified_source_pipeline",
                user_message="ASR 微分段失败：AI 配音主链路必须走统一 source_aggregate，不能从 timeline_digest 反推时间。",
                suggestions=[
                    "请从 Web 入口或 run_multisource_pipeline.py 运行。",
                    "如需支持单视频，请先包装为 source_prepare/source_analysis/source_aggregate 链路。",
                ],
                technical_detail={
                    "source_mode": self.manifest.get("source_mode"),
                    "source_manifest": self.manifest.get("source_manifest"),
                },
            )

        raw_index = self._load_source_raw_asr_index()
        raw_sources = raw_index.get("sources") if isinstance(raw_index, dict) else []
        if not isinstance(raw_sources, list):
            raw_sources = []

        sentences: list[dict[str, Any]] = []
        cfg = self.config.raw.get("asr_micro_segment", {})
        max_chars = int(cfg.get("max_sentence_chars", 120) or 120)
        for source in raw_sources:
            if not isinstance(source, dict):
                continue
            source_id = str(source.get("source_id") or "source_1").strip() or "source_1"
            source_index = source.get("source_index") or 1
            raw_segments = [seg for seg in source.get("asr_segments") or [] if isinstance(seg, dict)]
            raw_segments.sort(key=lambda seg: float(seg.get("start_seconds") or 0))
            for seg in raw_segments:
                if not isinstance(seg, dict):
                    continue
                start = self._time_value_seconds(seg.get("start_seconds"))
                end = self._time_value_seconds(seg.get("end_seconds"))
                if start is None or end is None or end <= start:
                    continue
                text = str(seg.get("normalized_text") or seg.get("text") or seg.get("raw_text") or "").strip()
                if not text:
                    continue
                parts = self._split_long_asr_text_for_sentence_units(text, max_chars=max_chars)
                total_chars = sum(len(part) for part in parts) or len(text)
                cursor = float(start)
                for part in parts:
                    part = str(part or "").strip()
                    if not part:
                        continue
                    part_duration = (float(end) - float(start)) * (len(part) / total_chars) if total_chars else 0
                    part_start = cursor
                    part_end = min(float(end), cursor + part_duration)
                    cursor = part_end
                    sentences.append({
                        "sentence_id": f"s{len(sentences) + 1:04d}",
                        "asr_segment_id": seg.get("asr_segment_id", ""),
                        "source_raw_asr_segment_id": seg.get("source_raw_asr_segment_id", ""),
                        "source_id": source_id,
                        "source_index": seg.get("source_index", source_index),
                        "start_seconds": round(part_start, 3),
                        "end_seconds": round(part_end, 3),
                        "text": part,
                        "raw_text": seg.get("raw_text", ""),
                        "source_chunk_ids": seg.get("source_chunk_ids", []),
                        "primary_chunk_id": seg.get("primary_chunk_id", ""),
                    })

        sentences.sort(key=lambda item: (
            str(item.get("source_id") or ""),
            float(item.get("start_seconds") or 0),
            float(item.get("end_seconds") or 0),
        ))
        for index, sentence in enumerate(sentences, start=1):
            sentence["sentence_id"] = f"s{index:04d}"
        return sentences

    def _split_long_asr_text_for_sentence_units(self, text: str, *, max_chars: int) -> list[str]:
        text = str(text or "").strip()
        if not text:
            return []
        max_chars = max(1, int(max_chars or 120))
        if len(text) <= max_chars:
            return [text]

        parts = re.split(r"([。！？；.!?;])", text)
        units: list[str] = []
        current = ""
        for i in range(0, len(parts), 2):
            frag = parts[i]
            punct = parts[i + 1] if i + 1 < len(parts) else ""
            candidate = (current + frag + punct).strip()
            if current and len(candidate) > max_chars:
                units.append(current.strip())
                current = (frag + punct).strip()
            else:
                current = candidate
        if current:
            units.append(current.strip())

        final: list[str] = []
        for unit in units or [text]:
            if len(unit) <= max_chars:
                final.append(unit)
            else:
                for start in range(0, len(unit), max_chars):
                    piece = unit[start:start + max_chars].strip()
                    if piece:
                        final.append(piece)
        return [item for item in final if item]

    def _build_asr_sentence_units_legacy_from_timeline_digest(self) -> list[dict[str, Any]]:
        raise UserFacingPipelineError(
            "legacy_asr_micro_segment_timeline_digest_disabled",
            user_message="ASR 微分段旧 timeline_digest fallback 已禁用。",
            suggestions=["请改用统一 source_aggregate 生成 source_raw_asr_index.json。"],
        )
        digest = self._load_current_timeline_digest()
        timeline = digest.get("chunks", [])
        cfg = self.config.raw.get("asr_micro_segment", {})
        max_chars = cfg.get("max_sentence_chars", 120)
        
        sentences = []
        for item in timeline:
            text = str(item.get("speech") or item.get("asr_text") or "").strip()
            if not text:
                continue
            
            parts = re.split(r'([。！？；.!?;\n])', text)
            sub_texts = []
            current = ""
            for i in range(0, len(parts), 2):
                frag = parts[i]
                punct = parts[i+1] if i+1 < len(parts) else ""
                if current and len(current) + len(frag) > max_chars:
                    sub_texts.append(current)
                    current = frag + punct
                else:
                    current += frag + punct
            if current:
                sub_texts.append(current)
            
            start = float(item.get("local_start_seconds", item.get("start_seconds", 0)))
            end = float(item.get("local_end_seconds", item.get("end_seconds", 0)))
            duration = end - start
            total_chars = sum(len(t) for t in sub_texts)
            
            curr_start = start
            for sub_text in sub_texts:
                if not sub_text.strip():
                    continue
                sub_duration = duration * (len(sub_text) / total_chars) if total_chars else 0
                sentences.append({
                    "sentence_id": f"s{len(sentences)+1:04d}",
                    "source_id": item.get("source_id", "source_1"),
                    "source_index": item.get("source_index", 0),
                    "start_seconds": round(curr_start, 3),
                    "end_seconds": round(curr_start + sub_duration, 3),
                    "text": sub_text.strip()
                })
                curr_start += sub_duration
                
        return sentences

    def _attach_visual_to_asr_sentences(self, sentences: list[dict[str, Any]]) -> list[dict[str, Any]]:
        digest = self._load_current_timeline_digest()
        timeline = digest.get("chunks", [])
        cfg = self.config.raw.get("asr_micro_segment", {})
        visual_chars = cfg.get("visual_summary_chars", 80)
        
        for s in sentences:
            s_start = s["start_seconds"]
            s_end = s["end_seconds"]
            visuals = []
            for item in timeline:
                if item.get("source_id", "source_1") != s.get("source_id", "source_1"):
                    continue
                t_start = float(item.get("local_start_seconds", item.get("start_seconds", 0)))
                t_end = float(item.get("local_end_seconds", item.get("end_seconds", 0)))
                if max(s_start, t_start) < min(s_end, t_end):
                    v = str(item.get("visual") or item.get("visual_summary") or "").strip()
                    if v and v not in visuals:
                        visuals.append(v)
            s["visual_summary"] = compact_text("，".join(visuals), max_chars=visual_chars)
        return sentences

    def _window_sentences(self, sentences: list[dict[str, Any]], window_size: int, overlap: int) -> list[list[dict[str, Any]]]:
        windows = []
        if not sentences:
            return windows
        i = 0
        while i < len(sentences):
            windows.append(sentences[i:i+window_size])
            if i + window_size >= len(sentences):
                break
            i += (window_size - overlap)
        return windows

    def _build_asr_micro_segment_text(self, sentences: list[dict[str, Any]], source_id: str) -> str:
        lines = []
        for s in sentences:
            sid = s["sentence_id"]
            lines.append(f"[{sid}]")
            lines.append(f"{s['text']}")
            lines.append("")
        return "\n".join(lines).strip()

    def _dedupe_and_validate_sentence_groups(
        self,
        groups: list[Any],
        sentences: list[dict[str, Any]],
        *,
        allow_sentence_fallback: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        valid_groups = []
        used_sids = set()
        smap = {s["sentence_id"]: s for s in sentences}
        diagnostics: dict[str, Any] = {
            "input_group_count": len(groups) if isinstance(groups, list) else 0,
            "valid_group_count": 0,
            "invalid_groups": [],
            "duplicate_sentence_ids": [],
            "missing_sentence_ids": [],
            "fallback_groups_added": 0,
            "allow_sentence_fallback": allow_sentence_fallback,
        }
        
        for group_index, g in enumerate(groups):
            if isinstance(g, list):
                sids = g
                group_data = {}
            elif isinstance(g, dict):
                sids = g.get("sentence_ids", [])
                group_data = g
            else:
                diagnostics["invalid_groups"].append({"index": group_index, "reason": "group_not_object_or_list"})
                continue
                
            if not isinstance(sids, list):
                diagnostics["invalid_groups"].append({"index": group_index, "reason": "sentence_ids_not_list", "value": sids})
                continue
            
            clean_sids = []
            for sid in sids:
                sid = str(sid).strip()
                if not sid or sid not in smap:
                    diagnostics["invalid_groups"].append({"index": group_index, "reason": "unknown_sentence_id", "sentence_id": sid})
                    continue
                if sid in used_sids:
                    diagnostics["duplicate_sentence_ids"].append(sid)
                    continue
                clean_sids.append(sid)
                used_sids.add(sid)
                    
            if not clean_sids:
                diagnostics["invalid_groups"].append({"index": group_index, "reason": "empty_after_validation"})
                continue
                
            valid_group = dict(group_data) if isinstance(g, dict) else {}
            valid_group["sentence_ids"] = clean_sids
            valid_groups.append(valid_group)
            
        missing_sentences = [s for s in sentences if s["sentence_id"] not in used_sids]
        diagnostics["missing_sentence_ids"] = [s["sentence_id"] for s in missing_sentences]
        if allow_sentence_fallback:
            for s in missing_sentences:
                valid_groups.append({
                    "group_id": f"fallback_{s['sentence_id']}",
                    "sentence_ids": [s["sentence_id"]],
                    "summary": s["text"][:20],
                    "segment_type": "fact",
                    "keep_candidate": 1,
                })
            diagnostics["fallback_groups_added"] = len(missing_sentences)
            
        valid_groups.sort(key=lambda g: smap[g["sentence_ids"][0]]["start_seconds"])
        diagnostics["valid_group_count"] = len(valid_groups)
        diagnostics["covered_sentence_count"] = len(used_sids)
        diagnostics["sentence_count"] = len(sentences)
        return valid_groups, diagnostics

    def _materialize_micro_segments_from_sentence_groups(self, groups: list[dict[str, Any]], sentences: list[dict[str, Any]]) -> list[dict[str, Any]]:
        smap = {s["sentence_id"]: s for s in sentences}
        segments = []
        for i, g in enumerate(groups):
            sids = g["sentence_ids"]
            s_list = [smap[sid] for sid in sids]
            
            start = s_list[0]["start_seconds"]
            end = s_list[-1]["end_seconds"]
            source_id = s_list[0].get("source_id", "source_1")
            source_index = s_list[0].get("source_index", 0)
            
            asr_text = "".join(s["text"] for s in s_list)
            visuals = []
            asr_segment_ids = []
            source_chunk_ids = []
            for s in s_list:
                v = s.get("visual_summary")
                if v and v not in visuals:
                    visuals.append(v)
                asr_id = str(s.get("asr_segment_id") or "").strip()
                if asr_id and asr_id not in asr_segment_ids:
                    asr_segment_ids.append(asr_id)
                for chunk_id in s.get("source_chunk_ids") or []:
                    chunk_id = str(chunk_id).strip()
                    if chunk_id and chunk_id not in source_chunk_ids:
                        source_chunk_ids.append(chunk_id)
            micro_segment_id = f"{source_id}_ms_{i+1:04d}"
                    
            segments.append({
                "micro_segment_id": micro_segment_id,
                "segment_id": micro_segment_id,
                "source_id": source_id,
                "source_index": source_index,
                "sentence_ids": sids,
                "asr_segment_ids": asr_segment_ids,
                "source_chunk_ids": source_chunk_ids,
                "source_start_seconds": round(start, 3),
                "source_end_seconds": round(end, 3),
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "duration_seconds": round(end - start, 3),
                "source_start": seconds_to_timecode(start, ms=True),
                "source_end": seconds_to_timecode(end, ms=True),
                "asr_text": asr_text,
                "summary": g.get("summary", ""),
                "visual_context": " ".join(visuals),
                "visual_summary": "，".join(visuals),
                "segment_type": g.get("segment_type", "fact"),
                "keep_candidate": g.get("keep_candidate", 1)
            })
        return segments

    def _enforce_micro_segment_limits(self, segments: list[dict[str, Any]], cfg: dict[str, Any], sentences: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hard_max = float(cfg.get("hard_max_segment_seconds", 35))
        min_seg = float(cfg.get("min_segment_seconds", 4))
        
        fixed = []
        for seg in segments:
            if seg["duration_seconds"] > hard_max and cfg.get("split_overlong_group", True):
                fixed.extend(self._split_segment_by_sentence_boundary(seg, hard_max, sentences))
            else:
                fixed.append(seg)
                
        fixed = self._merge_too_short_adjacent_segments(fixed, min_seg, hard_max)
        
        final_fixed = []
        for seg in fixed:
            if seg["duration_seconds"] > hard_max and cfg.get("split_overlong_group", True):
                final_fixed.extend(self._split_segment_by_sentence_boundary(seg, hard_max, sentences))
            else:
                final_fixed.append(seg)
        return final_fixed
        
    def _split_segment_by_sentence_boundary(self, segment: dict[str, Any], hard_max: float, sentences: list[dict[str, Any]]) -> list[dict[str, Any]]:
        smap = {s["sentence_id"]: s for s in sentences}
        sids = segment.get("sentence_ids", [])
        if len(sids) <= 1:
            return [segment]
            
        parts = []
        current_sids = []
        current_dur = 0.0
        
        for sid in sids:
            s = smap[sid]
            dur = s["end_seconds"] - s["start_seconds"]
            if current_dur + dur > hard_max and current_sids:
                parts.append(current_sids)
                current_sids = [sid]
                current_dur = dur
            else:
                current_sids.append(sid)
                current_dur += dur
        if current_sids:
            parts.append(current_sids)
            
        result = []
        for i, part_sids in enumerate(parts):
            sub_seg = dict(segment)
            sub_seg["segment_id"] = f"{segment['segment_id']}_{i+1}"
            sub_seg["micro_segment_id"] = sub_seg["segment_id"]
            sub_seg["sentence_ids"] = part_sids
            
            s_list = [smap[sid] for sid in part_sids]
            start = s_list[0]["start_seconds"]
            end = s_list[-1]["end_seconds"]
            asr_segment_ids = []
            source_chunk_ids = []
            for s in s_list:
                asr_id = str(s.get("asr_segment_id") or "").strip()
                if asr_id and asr_id not in asr_segment_ids:
                    asr_segment_ids.append(asr_id)
                for chunk_id in s.get("source_chunk_ids") or []:
                    chunk_id = str(chunk_id).strip()
                    if chunk_id and chunk_id not in source_chunk_ids:
                        source_chunk_ids.append(chunk_id)
            sub_seg["asr_segment_ids"] = asr_segment_ids
            sub_seg["source_chunk_ids"] = source_chunk_ids
            sub_seg["source_start_seconds"] = round(start, 3)
            sub_seg["source_end_seconds"] = round(end, 3)
            sub_seg["start_seconds"] = round(start, 3)
            sub_seg["end_seconds"] = round(end, 3)
            sub_seg["duration_seconds"] = round(end - start, 3)
            sub_seg["source_start"] = seconds_to_timecode(start, ms=True)
            sub_seg["source_end"] = seconds_to_timecode(end, ms=True)
            sub_seg["asr_text"] = "".join(s["text"] for s in s_list)
            result.append(sub_seg)
            
        return result

    def _merge_too_short_adjacent_segments(self, segments: list[dict[str, Any]], min_seg: float, hard_max: float) -> list[dict[str, Any]]:
        if not segments:
            return segments
            
        merged = []
        current = segments[0]
        
        for i in range(1, len(segments)):
            nxt = segments[i]
            merged_duration = float(nxt.get("end_seconds") or 0) - float(current.get("start_seconds") or 0)
            can_merge = (
                current["duration_seconds"] < min_seg
                and current["source_id"] == nxt["source_id"]
                and (hard_max <= 0 or merged_duration <= hard_max)
            )
            if can_merge:
                # merge into nxt
                nxt["start_seconds"] = current["start_seconds"]
                nxt["source_start_seconds"] = current.get("source_start_seconds", current["start_seconds"])
                nxt["source_start"] = current["source_start"]
                nxt["duration_seconds"] = round(nxt["end_seconds"] - nxt["start_seconds"], 3)
                nxt["asr_text"] = current["asr_text"] + nxt["asr_text"]
                nxt["sentence_ids"] = current["sentence_ids"] + nxt["sentence_ids"]
                nxt["asr_segment_ids"] = list(dict.fromkeys((current.get("asr_segment_ids") or []) + (nxt.get("asr_segment_ids") or [])))
                nxt["source_chunk_ids"] = list(dict.fromkeys((current.get("source_chunk_ids") or []) + (nxt.get("source_chunk_ids") or [])))
                current = nxt
            else:
                merged.append(current)
                current = nxt
        merged.append(current)
        
        # also check if the last one is still too short, merge backwards if possible
        if len(merged) > 1:
            last = merged[-1]
            prev = merged[-2]
            merged_duration_back = float(last.get("end_seconds") or 0) - float(prev.get("start_seconds") or 0)
            can_merge_back = (
                last["duration_seconds"] < min_seg
                and last["source_id"] == prev["source_id"]
                and (hard_max <= 0 or merged_duration_back <= hard_max)
            )
            if can_merge_back:
                merged.pop()
                prev["end_seconds"] = last["end_seconds"]
                prev["source_end_seconds"] = last.get("source_end_seconds", last["end_seconds"])
                prev["source_end"] = last["source_end"]
                prev["duration_seconds"] = round(prev["end_seconds"] - prev["start_seconds"], 3)
                prev["asr_text"] = prev["asr_text"] + last["asr_text"]
                prev["sentence_ids"] = prev["sentence_ids"] + last["sentence_ids"]
                prev["asr_segment_ids"] = list(dict.fromkeys((prev.get("asr_segment_ids") or []) + (last.get("asr_segment_ids") or [])))
                prev["source_chunk_ids"] = list(dict.fromkeys((prev.get("source_chunk_ids") or []) + (last.get("source_chunk_ids") or [])))
            
        return merged

    def _load_segments_for_content_analysis(self) -> list[dict[str, Any]]:
        micro_doc = self._load_step_json("asr_micro_segment")
        all_segments = [x for x in micro_doc.get("segments", []) if isinstance(x, dict)]
        by_id = {
            str(seg.get("micro_segment_id") or seg.get("id") or "").strip(): seg
            for seg in all_segments
            if str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
        }

        try:
            preselect_doc = self._load_step_json("content_analysis_preselect")
            kept = preselect_doc.get("kept_segments") if isinstance(preselect_doc, dict) else None
            if isinstance(kept, list) and kept:
                selected = []
                for item in kept:
                    if not isinstance(item, dict):
                        continue
                    msid = str(item.get("micro_segment_id") or "").strip()
                    if msid in by_id:
                        seg = dict(by_id[msid])
                        seg["preselect_summary"] = item.get("summary") or ""
                        selected.append(seg)
                if selected:
                    return selected
        except Exception:
            pass

        return all_segments

    def _content_analysis_cfg(self) -> dict[str, Any]:
        raw = self.config.raw.get("content_analysis", {}) or {}
        llm_input_cfg = self.config.raw.get("llm_input", {}) or {}
        asr_micro_cfg = self.config.raw.get("asr_micro_segment", {}) or {}

        return {
            "group_by_source": bool(raw.get("group_by_source", True)),
            "group_when_source_count_gt": max(1, int(raw.get("group_when_source_count_gt", 3) or 3)),
            "max_sources_per_group": max(1, int(raw.get("max_sources_per_group", 3) or 3)),
            "max_segments_per_group": max(1, int(raw.get("max_segments_per_group", 40) or 40)),
            "max_input_chars_per_group": max(
                1000,
                int(
                    raw.get(
                        "max_input_chars_per_group",
                        min(10000, int(llm_input_cfg.get("max_content_analysis_input_chars", 12000) or 12000)),
                    )
                    or 10000
                ),
            ),
            "max_selected_segments_per_group": max(1, int(raw.get("max_selected_segments_per_group", 6) or 6)),
            "max_final_selected_segments": max(1, int(raw.get("max_final_selected_segments", 20) or 20)),
            "allow_partial_group_success": bool(raw.get("allow_partial_group_success", True)),
            "fallback_to_single_pass_when_empty": bool(raw.get("fallback_to_single_pass_when_empty", False)),
            "legacy_max_segments": max(1, int(asr_micro_cfg.get("max_segments_for_content_analysis", 120) or 120)),
        }

    def _source_order_from_segments(self, segments: list[dict[str, Any]]) -> list[str]:
        source_order: list[str] = []
        seen: set[str] = set()
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            source_id = str(seg.get("source_id") or "source_1").strip() or "source_1"
            if source_id not in seen:
                seen.add(source_id)
                source_order.append(source_id)
        return source_order

    def _group_segments_for_content_analysis(
        self,
        segments: list[dict[str, Any]],
        cfg: dict[str, Any],
    ) -> list[dict[str, Any]]:
        source_order = self._source_order_from_segments(segments)

        if not cfg["group_by_source"] or len(source_order) <= cfg["group_when_source_count_gt"]:
            return [{
                "group_id": "all",
                "group_index": 1,
                "group_count": 1,
                "source_ids": source_order,
                "segments": segments[: cfg["legacy_max_segments"]],
                "grouped": False,
            }]

        by_source: dict[str, list[dict[str, Any]]] = {source_id: [] for source_id in source_order}
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            source_id = str(seg.get("source_id") or "source_1").strip() or "source_1"
            by_source.setdefault(source_id, []).append(seg)

        source_groups: list[list[str]] = []
        max_sources = cfg["max_sources_per_group"]
        for start in range(0, len(source_order), max_sources):
            source_groups.append(source_order[start:start + max_sources])

        groups: list[dict[str, Any]] = []
        for index, source_ids in enumerate(source_groups, start=1):
            group_segments: list[dict[str, Any]] = []
            for source_id in source_ids:
                group_segments.extend(by_source.get(source_id, []))

            groups.append({
                "group_id": f"group_{index:03d}",
                "group_index": index,
                "group_count": len(source_groups),
                "source_ids": source_ids,
                "segments": group_segments[: cfg["max_segments_per_group"]],
                "grouped": True,
            })

        return [group for group in groups if group.get("segments")]

    def _build_content_analysis_micro_segment_text(
        self,
        micro_segments: list[dict[str, Any]] | None = None,
        *,
        group_id: str = "all",
        group_index: int = 1,
        group_count: int = 1,
        source_ids: list[str] | None = None,
        max_selected_segments: int | None = None,
        max_input_chars: int | None = None,
    ) -> str:
        if micro_segments is None:
            micro_segments = self._load_segments_for_content_analysis()

        cfg = self._content_analysis_cfg()
        max_segments = cfg["legacy_max_segments"]
        if max_input_chars is None:
            max_input_chars = cfg["max_input_chars_per_group"]
        if max_selected_segments is None:
            max_selected_segments = cfg["max_selected_segments_per_group"]
        source_ids = source_ids or self._source_order_from_segments(micro_segments)

        lines = [
            "你将看到一组已经按 ASR 语义切好的 micro_segments。",
            "每个 micro_segment_id 是唯一标识。",
            "任务：只从这些 micro_segment_id 中选择适合 AI 配音短视频的事实段。",
            "不要合并多个 micro_segment。",
            "不要输出新的时间码。",
            "输出 JSON 格式：{\"selected_segments\":[{\"micro_segment_id\":\"...\",\"summary\":\"...\",\"role\":\"fact\"}]}",
            "",
            f"当前分组：{group_id}，第 {group_index}/{group_count} 组。",
            "当前组 source_ids：" + ", ".join(source_ids),
            f"当前组最多选择 {max_selected_segments} 个 micro_segment。",
            "",
            "选择偏好：",
            "优先选择 10～35 秒内的 micro_segment。",
            "22～35 秒属于偏长片段，只有在完整表态、连续现场动作、不可拆事实时才选择。",
            "不要因为片段长就自动丢弃，但不要选择多个信息混杂的长片段。",
            "同一事实重复出现时，优先选择画面更清楚、信息更完整的一段。",
            "",
        ]

        included = 0
        truncated = False
        for seg in micro_segments[:max_segments]:
            msid = str(seg.get("micro_segment_id") or "").strip()
            if not msid:
                continue

            block_lines = [
                f"[{msid}]",
                f"source={seg.get('source_id', '')}",
                f"duration={seg.get('duration_seconds', '')}",
            ]
            if seg.get("preselect_summary"):
                block_lines.append(f"预选摘要：{seg['preselect_summary']}")
            else:
                summary = str(seg.get("summary") or "").strip()
                if summary:
                    block_lines.append(f"摘要：{summary}")
            
            speech = sanitize_llm_text(str(seg.get("asr_text") or seg.get("speech") or ""), max_chars=160)
            if speech:
                block_lines.append(f"短ASR：{speech}")
            
            visual = compact_text(str(seg.get("visual_summary") or seg.get("visual") or ""), max_chars=80)
            if visual:
                block_lines.append(f"短画面：{visual}")

            block = "\n".join(block_lines).strip()
            candidate_text = "\n".join(lines + [block, ""]).strip()
            if len(candidate_text) > max_input_chars:
                truncated = True
                break

            lines.append(block)
            lines.append("")
            included += 1

        if truncated:
            lines.append("")
            lines.append("注意：后续 micro_segments 已因输入长度预算被截断，不能选择未出现在上文的 micro_segment_id。")

        lines.append("")
        lines.append(f"本次实际提供 micro_segment 数：{included}")
        return "\n".join(lines).strip()

    def _normalize_content_analysis_selected_segments(self, result: Any) -> dict[str, Any]:
        raw_items: Any
        if isinstance(result, list):
            raw_items = result
            base_result: dict[str, Any] = {}
        elif isinstance(result, dict):
            raw_items = result.get("selected_segments")
            if raw_items is None:
                raw_items = result.get("segments", [])
            base_result = dict(result)
        else:
            raw_items = []
            base_result = {}
        if isinstance(raw_items, dict):
            raw_items = raw_items.get("selected_segments", [])
        if not isinstance(raw_items, list):
            raw_items = []

        micro_segments = self._load_step_json("asr_micro_segment").get("segments", [])
        seg_map = {
            str(s.get("micro_segment_id") or s.get("segment_id") or ""): s
            for s in micro_segments
            if isinstance(s, dict)
        }
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        diagnostics: dict[str, Any] = {
            "legacy_segment_id_used": 0,
            "invalid_items": [],
            "selected_count": 0,
        }
        for index, raw in enumerate(raw_items):
            if isinstance(raw, str):
                micro_segment_id = raw.strip()
                summary = ""
                role = ""
            elif isinstance(raw, dict):
                raw_micro_id = raw.get("micro_segment_id")
                raw_legacy_id = raw.get("segment_id") or raw.get("id")
                if raw_micro_id:
                    micro_segment_id = str(raw_micro_id).strip()
                else:
                    micro_segment_id = str(raw_legacy_id or "").strip()
                    if micro_segment_id:
                        diagnostics["legacy_segment_id_used"] += 1
                summary = str(raw.get("summary") or "").strip()
                role = str(raw.get("role") or raw.get("segment_type") or "fact").strip()
            else:
                diagnostics["invalid_items"].append({"index": index, "reason": "item_not_string_or_object"})
                continue
            if not micro_segment_id or micro_segment_id in seen or micro_segment_id not in seg_map:
                diagnostics["invalid_items"].append({
                    "index": index,
                    "reason": "missing_duplicate_or_unknown_micro_segment_id",
                    "micro_segment_id": micro_segment_id,
                })
                continue
            seg = seg_map[micro_segment_id]
            selected.append({
                "micro_segment_id": micro_segment_id,
                "summary": summary or str(seg.get("summary") or seg.get("asr_text") or "").strip(),
                "role": role or "fact",
            })
            seen.add(micro_segment_id)
        diagnostics["selected_count"] = len(selected)

        return {
            "version": "ai_voiceover_content_analysis_v2",
            "input_unit": "micro_segment",
            "selected_segments": selected,
            "diagnostics": diagnostics,
            "materialized_by_code": True,
            "raw_model_plan": result,
            **({
                "model_fields": {
                    key: value
                    for key, value in base_result.items()
                    if key not in {"selected_segments", "segments", "candidate_clips", "candidate_segments"}
                }
            } if base_result else {}),
        }

    def _merge_grouped_content_analysis_results(
        self,
        *,
        group_results: list[dict[str, Any]],
        group_errors: list[dict[str, Any]],
        cfg: dict[str, Any],
    ) -> dict[str, Any]:
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()

        for group in sorted(group_results, key=lambda x: int(x.get("group_index") or 0)):
            count_in_group = 0
            for item in group.get("selected_segments") or []:
                if not isinstance(item, dict):
                    continue
                msid = str(item.get("micro_segment_id") or "").strip()
                if not msid or msid in seen:
                    continue

                clean_item = {
                    "micro_segment_id": msid,
                    "summary": str(item.get("summary") or "").strip(),
                    "role": str(item.get("role") or "fact").strip() or "fact",
                    "source_group_id": group.get("group_id", ""),
                }
                selected.append(clean_item)
                seen.add(msid)
                count_in_group += 1

                if count_in_group >= cfg["max_selected_segments_per_group"]:
                    break

            if len(selected) >= cfg["max_final_selected_segments"]:
                break

        selected = selected[: cfg["max_final_selected_segments"]]

        return {
            "version": "ai_voiceover_content_analysis_v2_grouped",
            "input_unit": "micro_segment",
            "selected_segments": selected,
            "diagnostics": {
                "selected_count": len(selected),
                "group_count": len(group_results),
                "group_error_count": len(group_errors),
                "group_errors": group_errors,
                "max_selected_segments_per_group": cfg["max_selected_segments_per_group"],
                "max_final_selected_segments": cfg["max_final_selected_segments"],
            },
            "materialized_by_code": True,
            "grouped_content_analysis": True,
            "group_results": group_results,
        }

    def _materialize_content_analysis_candidates_from_segments(self, result: Any) -> dict[str, Any]:
        raise UserFacingPipelineError(
            "legacy_content_analysis_candidate_materialize_disabled",
            user_message="旧 content_analysis -> candidate_clips 物化路径已禁用。",
            suggestions=["AI 配音候选片段必须由 ai_voiceover_candidate_materialize 生成。"],
        )
        if isinstance(result, list):
            raw_clips = result
            base_result = {}
        elif isinstance(result, dict):
            raw_clips = result.get("candidate_segments", result.get("candidate_clips", []))
            base_result = dict(result)
        else:
            raw_clips = []
            base_result = {}
            
        if isinstance(raw_clips, dict):
            raw_clips = raw_clips.get("candidate_segments", raw_clips.get("candidate_clips", []))
            
        micro_segments = self._load_step_json("asr_micro_segment").get("segments", [])
        seg_map = {s["segment_id"]: s for s in micro_segments}
        
        materialized = []
        for i, raw in enumerate(raw_clips):
            if not isinstance(raw, dict):
                continue
                
            seg_id = raw.get("segment_id") or raw.get("id") or raw.get("clip_id")
            if not seg_id or seg_id not in seg_map:
                continue
                
            seg = seg_map[seg_id]
            clip_id = f"clip_{i+1:03d}"
            
            materialized.append({
                "clip_id": clip_id,
                "source_id": seg["source_id"],
                "source_index": seg["source_index"],
                "source_start": seg["source_start"],
                "source_end": seg["source_end"],
                "source_start_seconds": seg["start_seconds"],
                "source_end_seconds": seg["end_seconds"],
                "duration_seconds": seg["duration_seconds"],
                "start": seg["source_start"],
                "end": seg["source_end"],
                "start_seconds": seg["start_seconds"],
                "end_seconds": seg["end_seconds"],
                "summary": str(raw.get("summary") or raw.get("reason") or seg.get("summary") or "").strip(),
                "speech": seg["asr_text"],
                "visual": seg.get("visual_summary", ""),
                "micro_segment_id": seg_id,
                "sentence_ids": seg.get("sentence_ids", []),
                "type": "fact",
            })
            
        base_result["candidate_clips"] = materialized
        return base_result



    def step_content_analysis_preselect(self) -> None:
        source_doc = self._load_step_json("asr_micro_segment")
        segments = [x for x in source_doc.get("segments", []) if isinstance(x, dict)]

        cfg = self.config.raw.get("content_analysis_preselect", {})
        enabled = bool(cfg.get("enabled", True))

        strategy = self._resolve_content_analysis_preselect_strategy(len(segments), cfg)
        input_hash = stable_hash({
            "asr_micro_segment": self._step_content_hash("asr_micro_segment"),
            "strategy": strategy,
            "enabled": enabled,
            "prompt": prompts.CONTENT_ANALYSIS_PRESELECT_PROMPT,
        })

        if self._can_reuse("content_analysis_preselect", input_hash):
            print("复用缓存: content_analysis_preselect")
            return

        version, vdir = self._version_dir("content_analysis_preselect", "agents/content_analysis_preselect")
        ensure_dir(vdir)

        if not enabled or len(segments) <= strategy["direct_threshold"]:
            output = self._build_content_analysis_preselect_passthrough(segments, strategy, enabled)
            self._write_content_analysis_preselect_output(output, input_hash, version, vdir)
            return

        batches = self._split_micro_segments_for_preselect(segments, strategy["batch_size"])
        results = self._run_content_analysis_preselect_batches(batches, strategy, vdir)

        output = self._merge_content_analysis_preselect_results(
            segments=segments,
            batch_results=results,
            strategy=strategy,
        )
        self._write_content_analysis_preselect_output(output, input_hash, version, vdir)

    def _resolve_content_analysis_preselect_strategy(self, total_segments: int, cfg: dict) -> dict:
        direct_threshold = int(cfg.get("direct_threshold", 10) or 10)
        small_total_threshold = int(cfg.get("small_total_threshold", 30) or 30)

        if total_segments <= small_total_threshold:
            batch_size = int(cfg.get("small_batch_size", 10) or 10)
            max_keep = int(cfg.get("small_max_keep_per_batch", 5) or 5)
            max_final = int(cfg.get("small_max_final_candidates", 20) or 20)
        else:
            batch_size = int(cfg.get("large_batch_size", 10) or 10)
            max_keep = int(cfg.get("large_max_keep_per_batch", 3) or 3)
            max_final = int(cfg.get("large_max_final_candidates", 30) or 30)

        return {
            "direct_threshold": max(0, direct_threshold),
            "small_total_threshold": max(1, small_total_threshold),
            "batch_size": max(1, batch_size),
            "max_keep_per_batch": max(1, max_keep),
            "max_final_candidates": max(1, max_final),
            "max_workers": max(1, int(cfg.get("max_workers", 4) or 4)),
            "max_asr_chars_per_segment": max(40, int(cfg.get("max_asr_chars_per_segment", 220) or 220)),
            "max_visual_chars_per_segment": max(20, int(cfg.get("max_visual_chars_per_segment", 100) or 100)),
            "max_summary_chars_per_segment": max(20, int(cfg.get("max_summary_chars_per_segment", 120) or 120)),
            "max_input_chars_per_batch": max(1000, int(cfg.get("max_input_chars_per_batch", 8000) or 8000)),
        }

    def _build_content_analysis_preselect_passthrough(self, segments: list[dict], strategy: dict, enabled: bool) -> dict:
        kept = []
        for seg in segments:
            msid = str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
            if not msid: continue
            kept.append({
                "micro_segment_id": msid,
                "batch_id": "passthrough",
                "summary": compact_text(str(seg.get("summary") or ""), max_chars=strategy["max_summary_chars_per_segment"]),
            })
        return {
            "version": "content_analysis_preselect_v1",
            "enabled": enabled,
            "source": "asr_micro_segment",
            "strategy": strategy,
            "batch_count": 0,
            "input_segment_count": len(segments),
            "kept_segment_count": len(kept),
            "kept_segments": kept,
            "batches": [],
            "warnings": [],
        }

    def _split_micro_segments_for_preselect(self, segments: list[dict], batch_size: int) -> list[dict]:
        batches = []
        for start in range(0, len(segments), batch_size):
            end = min(start + batch_size, len(segments))
            batches.append({
                "batch_id": f"batch_{len(batches) + 1:03d}",
                "start_index": start,
                "end_index": end,
                "segments": segments[start:end],
            })
        return batches

    def _shrink_preselect_batch_text(self, batch: dict, strategy: dict) -> str:
        s2 = dict(strategy)
        s2["max_asr_chars_per_segment"] = max(20, strategy["max_asr_chars_per_segment"] // 2)
        s2["max_visual_chars_per_segment"] = max(10, strategy["max_visual_chars_per_segment"] // 2)
        s2["max_summary_chars_per_segment"] = max(10, strategy["max_summary_chars_per_segment"] // 2)
        text = self._build_content_analysis_preselect_batch_text(batch, s2, shrink_fallback=False)
        if len(text) > strategy["max_input_chars_per_batch"]:
            hint = "请降低 batch_size 或减少每个 segment 的 ASR/画面字段长度。"
            raise RuntimeError(f"LLM input size [content_analysis_preselect] exceeds limit={strategy['max_input_chars_per_batch']}; {hint}")
        return text

    def _build_content_analysis_preselect_batch_text(self, batch: dict, strategy: dict, shrink_fallback: bool = True) -> str:
        lines = [
            "任务：从当前 batch 的 micro_segments 中预选 AI 配音候选片段。",
            f"batch_id: {batch['batch_id']}",
            f"max_keep_per_batch: {strategy['max_keep_per_batch']}",
            "",
            "规则：",
            "- 只能选择下面出现过的 micro_segment_id。",
            f"- 最多选择 {strategy['max_keep_per_batch']} 个。",
            "- 可以一个都不选。",
            "- 不要输出时间码。",
            "- 不要改写 micro_segment_id。",
            "",
            "micro_segments:",
            "",
        ]

        for offset, seg in enumerate(batch["segments"], start=1):
            msid = str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
            if not msid:
                continue
            source_id = str(seg.get("source_id") or "").strip()
            duration = seg.get("duration_seconds", "")
            summary = compact_text(str(seg.get("summary") or ""), max_chars=strategy["max_summary_chars_per_segment"])
            speech = sanitize_llm_text(str(seg.get("asr_text") or seg.get("speech") or ""), max_chars=strategy["max_asr_chars_per_segment"])
            visual = compact_text(str(seg.get("visual_summary") or seg.get("visual") or ""), max_chars=strategy["max_visual_chars_per_segment"])

            lines.append(f"[{msid}]")
            lines.append(f"source={source_id}")
            lines.append(f"index={batch['start_index'] + offset}")
            lines.append(f"duration={duration}")
            if summary: lines.append(f"摘要：{summary}")
            if speech: lines.append(f"声音：{speech}")
            if visual: lines.append(f"画面：{visual}")
            lines.append("")

        text = "\n".join(lines).strip()
        if shrink_fallback and len(text) > strategy["max_input_chars_per_batch"]:
            text = self._shrink_preselect_batch_text(batch, strategy)
        return text

    def _run_content_analysis_preselect_batches(self, batches: list[dict], strategy: dict, vdir: Path) -> list[dict]:
        max_workers = min(strategy["max_workers"], len(batches)) or 1
        results = []

        def run_one(batch: dict) -> dict:
            text_input = self._build_content_analysis_preselect_batch_text(batch, strategy)
            parsed = self._run_text_agent_for_preselect_batch(batch, text_input, vdir)
            return self._normalize_content_analysis_preselect_batch_result(batch, parsed, strategy)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(run_one, batch): batch for batch in batches}
            for future in as_completed(future_map):
                batch = future_map[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({
                        "batch_id": batch["batch_id"],
                        "status": "failed",
                        "error": str(exc),
                        "kept_segments": [],
                        "input_count": len(batch.get("segments") or []),
                    })

        return sorted(results, key=lambda x: x.get("batch_id", ""))

    def _run_text_agent_for_preselect_batch(self, batch: dict, input_data: str, vdir: Path) -> Any:
        if self.llm_text is None:
            raise RuntimeError("缺少 text LLM 配置")

        llm_cfg = self.config.llm
        provider = llm_cfg.get("text_llm_provider", "openai")
        model = self.options.model or llm_cfg.get("text_llm_model_name") or llm_cfg.get(f"text_{provider}_model_name")
        fallback = llm_cfg.get(f"text_{provider}_fallback_models", [])
        max_tokens = int(llm_cfg.get("text_llm_max_tokens", 16000) or 16000)

        batch_dir = vdir / "batches" / batch["batch_id"]
        ensure_dir(batch_dir)

        self._log_llm_input_size("content_analysis_preselect", input_data)
        write_text(batch_dir / "input.txt", input_data)
        write_json(batch_dir / "input_meta.json", {"type": "text", "chars": len(input_data)})
        write_text(batch_dir / "prompt.txt", prompts.CONTENT_ANALYSIS_PRESELECT_PROMPT)

        result = self.llm_text.call_json(
            model=model,
            fallback_models=fallback,
            prompt=prompts.CONTENT_ANALYSIS_PRESELECT_PROMPT,
            input_data=input_data,
            temperature=0.2,
            debug_dir=batch_dir / "_llm_debug",
            max_tokens=max_tokens,
        )

        write_json(batch_dir / "raw_response.json", {
            "model": result.model,
            "created_at": now_iso(),
            "raw_text": result.raw_text,
            "usage": result.usage,
            "finish_reason": result.finish_reason,
            "latency_ms": result.latency_ms,
        })
        write_json(batch_dir / "model_output.json", result.parsed)
        return result.parsed

    def _normalize_content_analysis_preselect_batch_result(self, batch: dict, parsed: Any, strategy: dict) -> dict:
        allowed_ids = {
            str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
            for seg in batch.get("segments", [])
            if str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
        }

        raw_items = []
        if isinstance(parsed, dict) and isinstance(parsed.get("kept_segments"), list):
            raw_items = parsed.get("kept_segments")
        elif isinstance(parsed, list):
            raw_items = parsed

        kept = []
        seen = set()
        for item in raw_items:
            if not isinstance(item, dict): continue
            msid = str(item.get("micro_segment_id") or item.get("id") or "").strip()
            if not msid or msid not in allowed_ids or msid in seen:
                continue
            seen.add(msid)
            kept.append({
                "micro_segment_id": msid,
                "batch_id": batch["batch_id"],
                "summary": compact_text(str(item.get("summary") or ""), max_chars=160),
            })
            if len(kept) >= strategy["max_keep_per_batch"]:
                break

        return {
            "batch_id": batch["batch_id"],
            "start_index": batch["start_index"],
            "end_index": batch["end_index"],
            "input_count": len(batch.get("segments") or []),
            "kept_count": len(kept),
            "status": "success",
            "kept_segments": kept,
        }

    def _merge_content_analysis_preselect_results(self, segments: list[dict], batch_results: list[dict], strategy: dict) -> dict:
        by_id = {
            str(seg.get("micro_segment_id") or seg.get("id") or "").strip(): seg
            for seg in segments
            if str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
        }

        items = []
        warnings = []
        for result in batch_results:
            if result.get("status") != "success":
                warnings.append({
                    "batch_id": result.get("batch_id"),
                    "error": result.get("error"),
                })
                continue
            for item in result.get("kept_segments") or []:
                msid = item.get("micro_segment_id")
                if msid in by_id:
                    source = by_id[msid]
                    enriched = dict(item)
                    enriched["source_id"] = source.get("source_id", "")
                    enriched["duration_seconds"] = source.get("duration_seconds", "")
                    items.append(enriched)

        # 去重，保留第一次出现
        dedup = {}
        for item in items:
            msid = item["micro_segment_id"]
            if msid not in dedup:
                dedup[msid] = item

        kept = list(dedup.values())
        kept = kept[: strategy["max_final_candidates"]]

        if not kept and segments:
            # fallback: fallback to top N items
            fallback_count = min(10, len(segments))
            warnings.append({"type": "empty_preselect_fallback"})
            for i in range(fallback_count):
                seg = segments[i]
                msid = str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
                if not msid: continue
                kept.append({
                    "micro_segment_id": msid,
                    "batch_id": "fallback",
                    "summary": compact_text(str(seg.get("summary") or ""), max_chars=strategy["max_summary_chars_per_segment"]),
                    "source_id": seg.get("source_id", ""),
                    "duration_seconds": seg.get("duration_seconds", ""),
                })

        return {
            "version": "content_analysis_preselect_v1",
            "enabled": True,
            "source": "asr_micro_segment",
            "strategy": strategy,
            "batch_count": len(batch_results),
            "input_segment_count": len(segments),
            "kept_segment_count": len(kept),
            "kept_segments": kept,
            "batches": [
                {k: v for k, v in result.items() if k != "kept_segments"}
                for result in batch_results
            ],
            "warnings": warnings,
        }

    def _write_content_analysis_preselect_output(self, output: dict, input_hash: str, version: str, vdir: Path) -> None:
        out = write_json(vdir / "content_analysis_preselect.json", output)
        status = self._base_status("content_analysis_preselect", version, input_hash, [out])
        self._write_status(vdir, status)
        self._record_step(
            step="content_analysis_preselect",
            version=version,
            status="success",
            output=relpath(out, self.task_dir),
            input_hash=input_hash,
            output_files=[out],
            extra={
                "summary": {
                    "input_segments": output.get("input_segment_count", 0),
                    "kept_segments": output.get("kept_segment_count", 0),
                    "batch_count": output.get("batch_count", 0),
                }
            },
        )
        print("完成: content_analysis_preselect")

    def step_content_analysis(self) -> None:
        cfg = self.config.raw.get("asr_micro_segment", {})
        use_micro_segments = cfg.get("enabled", True)
        if not use_micro_segments or self.manifest.get("steps", {}).get("asr_micro_segment", {}).get("status") != "success":
            raise UserFacingPipelineError(
                "content_analysis_requires_micro_segments",
                user_message="内容分析失败：AI 配音主链路必须先生成 micro_segments，不能回退到 chunk 分析。",
                suggestions=["回查 agents/asr_micro_segment/v*/micro_segments.json。", "从 asr_micro_segment 重新运行。"],
            )
        _base, _out_name, bound_prompt, prompt_version = AGENT_INFO["content_analysis"]
        if bound_prompt != prompts.CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT or prompt_version != "content_analysis_micro_segment_v1":
            raise UserFacingPipelineError(
                "content_analysis_prompt_binding_invalid",
                user_message="content_analysis 提示词绑定无效：AI 配音 micro_segment 链路不能使用旧 chunk/content_analysis prompt。",
                suggestions=["确认 AGENT_INFO['content_analysis'] 绑定 CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT。"],
                technical_detail={"prompt_version": prompt_version},
            )

        ca_cfg = self._content_analysis_cfg()
        all_segments = self._load_segments_for_content_analysis()
        groups = self._group_segments_for_content_analysis(all_segments, ca_cfg)
        if not groups:
            raise UserFacingPipelineError(
                "content_analysis_no_input_segments",
                user_message="内容分析失败：没有可用于 AI 配音终选的 micro_segment。",
                suggestions=["回查 agents/asr_micro_segment/v*/micro_segments.json。", "回查 agents/content_analysis_preselect/v*/content_analysis_preselect.json。"],
            )

        group_results: list[dict[str, Any]] = []
        group_errors: list[dict[str, Any]] = []

        for group in groups:
            try:
                text_input = self._build_content_analysis_micro_segment_text(
                    group["segments"],
                    group_id=group["group_id"],
                    group_index=group["group_index"],
                    group_count=group["group_count"],
                    source_ids=group["source_ids"],
                    max_selected_segments=ca_cfg["max_selected_segments_per_group"],
                    max_input_chars=ca_cfg["max_input_chars_per_group"],
                )
                print(
                    f"LLM grouped input size [content_analysis][{group['group_id']}]: "
                    f"{len(text_input)} chars, sources={group['source_ids']}, segments={len(group['segments'])}",
                    flush=True,
                )

                legacy_segment_hint = "只输出 segment_id" in text_input or "[segment_id]" in text_input
                if legacy_segment_hint or "\ntime=" in text_input:
                    raise UserFacingPipelineError(
                        "content_analysis_prompt_binding_invalid",
                        user_message="content_analysis 输入协议无效：不得向模型提示 segment_id 或 time 时间码。",
                        suggestions=["检查 _build_content_analysis_micro_segment_text() 是否只输出 micro_segment_id。"],
                        technical_detail={
                            "legacy_segment_hint": legacy_segment_hint,
                            "contains_time": "\ntime=" in text_input,
                            "group_id": group["group_id"],
                        },
                    )

                model_result = self._run_text_agent("content_analysis", text_input, allow_reuse=False)
                normalized = self._normalize_content_analysis_selected_segments(model_result)
                allowed_ids = {
                    str(seg.get("micro_segment_id") or "").strip()
                    for seg in group["segments"]
                    if isinstance(seg, dict) and str(seg.get("micro_segment_id") or "").strip()
                }
                selected = [
                    item for item in normalized.get("selected_segments", [])
                    if str(item.get("micro_segment_id") or "").strip() in allowed_ids
                ]
                invalid_cross_group = [
                    item for item in normalized.get("selected_segments", [])
                    if str(item.get("micro_segment_id") or "").strip() not in allowed_ids
                ]
                diagnostics = dict(normalized.get("diagnostics", {}))
                if invalid_cross_group:
                    diagnostics["invalid_cross_group_items"] = invalid_cross_group

                group_results.append({
                    "group_id": group["group_id"],
                    "group_index": group["group_index"],
                    "source_ids": group["source_ids"],
                    "input_segment_count": len(group["segments"]),
                    "selected_segments": selected,
                    "diagnostics": diagnostics,
                    "raw_model_plan": normalized.get("raw_model_plan"),
                })
            except Exception as exc:
                group_errors.append({
                    "group_id": group.get("group_id"),
                    "group_index": group.get("group_index"),
                    "source_ids": group.get("source_ids", []),
                    "error": str(exc),
                })
                if not ca_cfg["allow_partial_group_success"] or len(groups) == 1:
                    raise

        result = self._merge_grouped_content_analysis_results(
            group_results=group_results,
            group_errors=group_errors,
            cfg=ca_cfg,
        )

        if not result.get("selected_segments"):
            if ca_cfg["fallback_to_single_pass_when_empty"] and len(groups) > 1:
                text_input = self._build_content_analysis_micro_segment_text(
                    all_segments,
                    group_id="fallback_all",
                    group_index=1,
                    group_count=1,
                    source_ids=self._source_order_from_segments(all_segments),
                    max_selected_segments=ca_cfg["max_final_selected_segments"],
                    max_input_chars=int(self.config.raw.get("llm_input", {}).get("max_content_analysis_input_chars", 12000) or 12000),
                )
                fallback_result = self._run_text_agent("content_analysis", text_input, allow_reuse=False)
                result = self._normalize_content_analysis_selected_segments(fallback_result)

        if not result.get("selected_segments"):
            raise UserFacingPipelineError(
                "content_analysis_selected_segments_empty",
                user_message="内容分析失败：模型没有选出任何可用于 AI 配音的 micro_segment。",
                suggestions=[
                    "回查 agents/content_analysis/v*/model_output.json。",
                    "检查 asr_micro_segment 的 ASR 文本和画面摘要是否有效。",
                    "如果是多视频任务，请检查 content_analysis.json 里的 group_results 和 group_errors。",
                ],
                technical_detail={
                    "group_results": group_results,
                    "group_errors": group_errors,
                    "raw_model_plan": result.get("raw_model_plan"),
                },
            )
        self._overwrite_step_json("content_analysis", result, materialized=True)
        self._write_compat_content_analysis(result)

    def step_ai_voiceover_candidate_materialize(self) -> None:
        content = self._load_step_json("content_analysis")
        micro_doc = self._load_step_json("asr_micro_segment")
        cfg = self.config.raw.get("ai_voiceover_candidate", {})
        input_hash = stable_hash({
            "content_analysis": self._step_content_hash("content_analysis"),
            "asr_micro_segment": self._step_content_hash("asr_micro_segment"),
            "cfg": cfg,
        })
        if self._can_reuse("ai_voiceover_candidate_materialize", input_hash):
            print("reuse cache: ai_voiceover_candidate_materialize")
            return

        version, vdir = self._version_dir("ai_voiceover_candidate_materialize", "ai_voiceover_candidates")
        ensure_dir(vdir)
        padding_before = float(cfg.get("padding_before_seconds", 0.5) or 0.0)
        padding_after = float(cfg.get("padding_after_seconds", 0.8) or 0.0)
        min_clip_seconds = float(cfg.get("min_clip_seconds", 5.0) or 0.0)
        preferred_max_clip_seconds = float(cfg.get("preferred_max_clip_seconds", 45.0) or 0.0)
        max_clip_seconds = float(cfg.get("max_clip_seconds", 60.0) or 0.0)
        fail_on_empty = bool(cfg.get("fail_on_empty", True))
        truncate_overlong = bool(cfg.get("truncate_overlong", False))
        fail_on_overlong = bool(cfg.get("fail_on_overlong", False))
        fail_on_short = bool(cfg.get("fail_on_short", False))

        micro_segments = [
            seg for seg in micro_doc.get("segments", [])
            if isinstance(seg, dict)
        ]
        seg_map = {
            str(seg.get("micro_segment_id") or seg.get("segment_id") or ""): seg
            for seg in micro_segments
        }
        source_bounds: dict[str, float] = {}
        for item in self._source_boundaries():
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("source_id") or "").strip()
            if not source_id:
                continue
            duration = self._time_value_seconds(item.get("duration_seconds"))
            end_value = self._time_value_seconds(item.get("local_end") or item.get("end"))
            source_bounds[source_id] = float(duration or end_value or 0.0)

        candidate_clips: list[dict[str, Any]] = []
        missing_segment_ids: list[str] = []
        diagnostics: dict[str, Any] = {
            "missing_segment_ids": missing_segment_ids,
            "invalid_time_segments": [],
            "clamped_clips": [],
            "short_clips": [],
            "long_but_allowed_clips": [],
            "overlong_clips": [],
        }
        for selected in content.get("selected_segments") or []:
            if not isinstance(selected, dict):
                continue
            micro_segment_id = str(
                selected.get("micro_segment_id")
                or selected.get("segment_id")
                or selected.get("id")
                or ""
            ).strip()
            seg = seg_map.get(micro_segment_id)
            if not seg:
                if micro_segment_id:
                    missing_segment_ids.append(micro_segment_id)
                continue
            start = self._time_value_seconds(seg.get("source_start_seconds") or seg.get("start_seconds"))
            end = self._time_value_seconds(seg.get("source_end_seconds") or seg.get("end_seconds"))
            if start is None or end is None or end <= start:
                diagnostics["invalid_time_segments"].append({
                    "micro_segment_id": micro_segment_id,
                    "start_seconds": start,
                    "end_seconds": end,
                })
                continue
            source_id = str(seg.get("source_id") or "").strip()
            source_duration = source_bounds.get(source_id, 0.0)
            requested_start = float(start) - padding_before
            requested_end = float(end) + padding_after
            padded_start = max(0.0, requested_start)
            padded_end = float(end) + padding_after
            if source_duration > 0:
                padded_end = min(source_duration, padded_end)
            if padded_start != requested_start or padded_end != requested_end:
                diagnostics["clamped_clips"].append({
                    "micro_segment_id": micro_segment_id,
                    "source_id": source_id,
                    "requested_start_seconds": round(requested_start, 3),
                    "requested_end_seconds": round(requested_end, 3),
                    "clamped_start_seconds": round(padded_start, 3),
                    "clamped_end_seconds": round(padded_end, 3),
                    "source_duration_seconds": round(source_duration, 3) if source_duration else None,
                })
            raw_duration = padded_end - padded_start
            
            if max_clip_seconds > 0 and raw_duration > max_clip_seconds:
                diagnostics["overlong_clips"].append({
                    "micro_segment_id": micro_segment_id,
                    "source_id": source_id,
                    "duration_seconds": round(raw_duration, 3),
                    "max_clip_seconds": max_clip_seconds,
                    "truncated": truncate_overlong,
                })
                if truncate_overlong:
                    padded_end = padded_start + max_clip_seconds
            elif preferred_max_clip_seconds > 0 and raw_duration > preferred_max_clip_seconds:
                diagnostics["long_but_allowed_clips"].append({
                    "micro_segment_id": micro_segment_id,
                    "source_id": source_id,
                    "duration_seconds": round(raw_duration, 3),
                    "preferred_max_clip_seconds": preferred_max_clip_seconds,
                    "max_clip_seconds": max_clip_seconds,
                })
            duration = round(max(0.0, padded_end - padded_start), 3)
            if min_clip_seconds > 0 and duration < min_clip_seconds:
                diagnostics["short_clips"].append({
                    "micro_segment_id": micro_segment_id,
                    "source_id": source_id,
                    "duration_seconds": duration,
                    "min_clip_seconds": min_clip_seconds,
                })
            if duration <= 0:
                diagnostics["invalid_time_segments"].append({
                    "micro_segment_id": micro_segment_id,
                    "reason": "non_positive_duration_after_padding",
                    "padded_start_seconds": round(padded_start, 3),
                    "padded_end_seconds": round(padded_end, 3),
                })
                continue
            clip_id = f"av_clip_{len(candidate_clips) + 1:04d}"
            candidate_clips.append({
                "clip_id": clip_id,
                "candidate_type": "ai_voiceover",
                "derived_from": "micro_segment",
                "source_id": source_id,
                "source_index": seg.get("source_index"),
                "source_start_seconds": round(padded_start, 3),
                "source_end_seconds": round(padded_end, 3),
                "source_start": seconds_to_timecode(padded_start, ms=True),
                "source_end": seconds_to_timecode(padded_end, ms=True),
                "local_start_seconds": round(padded_start, 3),
                "local_end_seconds": round(padded_end, 3),
                "local_start": seconds_to_timecode(padded_start, ms=True),
                "local_end": seconds_to_timecode(padded_end, ms=True),
                "duration_seconds": duration,
                "micro_segment_ids": [micro_segment_id],
                "micro_segment_id": micro_segment_id,
                "asr_segment_ids": seg.get("asr_segment_ids", []),
                "source_chunk_ids": seg.get("source_chunk_ids", []),
                "asr_text": seg.get("asr_text", ""),
                "speech": seg.get("asr_text", ""),
                "visual_context": seg.get("visual_context") or seg.get("visual_summary", ""),
                "visual": seg.get("visual_context") or seg.get("visual_summary", ""),
                "summary": selected.get("summary") or seg.get("summary") or seg.get("asr_text", ""),
                "role": selected.get("role") or seg.get("segment_type") or "fact",
                "padding_before_seconds": padding_before,
                "padding_after_seconds": padding_after,
            })

        output = {
            "version": "ai_voiceover_candidate_clips_v1",
            "candidate_type": "ai_voiceover",
            "source": "asr_micro_segment",
            "candidate_clips": candidate_clips,
        }
        durations = [
            float(clip.get("duration_seconds") or 0)
            for clip in candidate_clips
            if float(clip.get("duration_seconds") or 0) > 0
        ]
        
        duration_stats = {
            "min": round(min(durations), 3) if durations else 0.0,
            "max": round(max(durations), 3) if durations else 0.0,
            "avg": round(sum(durations) / len(durations), 3) if durations else 0.0,
            "count": len(durations),
            "over_preferred_count": len(diagnostics["long_but_allowed_clips"]),
            "over_hard_count": len(diagnostics["overlong_clips"]),
        }

        debug = {
            "selected_segment_count": len(content.get("selected_segments") or []),
            "micro_segment_count": len(micro_segments),
            "candidate_clip_count": len(candidate_clips),
            "missing_segment_ids": missing_segment_ids,
            "diagnostics": diagnostics,
            "duration_stats": duration_stats,
            "config": {
                "padding_before_seconds": padding_before,
                "padding_after_seconds": padding_after,
                "min_clip_seconds": min_clip_seconds,
                "preferred_max_clip_seconds": preferred_max_clip_seconds,
                "max_clip_seconds": max_clip_seconds,
                "truncate_overlong": truncate_overlong,
                "fail_on_overlong": fail_on_overlong,
                "fail_on_short": fail_on_short,
            },
        }
        fail_reasons: list[str] = []
        if fail_on_empty and not candidate_clips:
            fail_reasons.append("empty")
        if fail_on_overlong and diagnostics["overlong_clips"]:
            fail_reasons.append("overlong")
        if fail_on_short and diagnostics["short_clips"]:
            fail_reasons.append("short")
        status = "failed" if fail_reasons else "success"
        debug["fail_reasons"] = fail_reasons
        out = write_json(vdir / "candidate_clips.json", output)
        debug_out = write_json(vdir / "materialize_debug.json", debug)
        status_doc = self._base_status("ai_voiceover_candidate_materialize", version, input_hash, [out, debug_out])
        status_doc["status"] = status
        if status == "failed":
            status_doc["technical_error"] = debug
        self._write_status(vdir, status_doc)
        self._record_step(
            step="ai_voiceover_candidate_materialize",
            version=version,
            status=status,
            output=relpath(out, self.task_dir),
            input_hash=input_hash,
            output_files=[out, debug_out],
            extra={"summary": {"candidate_clips": len(candidate_clips), "fail_reasons": fail_reasons, "duration_stats": duration_stats}},
        )
        if fail_on_empty and not candidate_clips:
            raise UserFacingPipelineError(
                "ai_voiceover_candidate_clips_empty",
                user_message="AI 配音候选片段为空：content_analysis 没有形成可物化的 micro_segment。",
                suggestions=["回查 agents/content_analysis/v*/content_analysis.json。", "回查 agents/asr_micro_segment/v*/micro_segments.json。"],
                technical_detail=debug,
            )
        if fail_on_overlong and diagnostics["overlong_clips"]:
            raise UserFacingPipelineError(
                "ai_voiceover_candidate_overlong",
                user_message="AI 配音候选片段超过硬上限，请拆分超长 micro_segment 或调大 ai_voiceover_candidate.max_clip_seconds。",
                suggestions=[
                    "查看 ai_voiceover_candidates/*/materialize_debug.json 中的 diagnostics.overlong_clips。",
                    "如果片段在 45～60 秒之间，应只作为 warning，不应失败。",
                    "如果片段超过 60 秒，建议回到 asr_micro_segment 阶段拆分。",
                ],
                technical_detail=debug,
            )
        if fail_on_short and diagnostics["short_clips"]:
            raise UserFacingPipelineError(
                "ai_voiceover_candidate_too_short",
                user_message="AI 配音候选片段存在短于 min_clip_seconds 的片段。",
                suggestions=["回查 ai_voiceover_candidates/v*/materialize_debug.json。", "调小 min_clip_seconds 或重新选择更完整的 micro_segment。"],
                technical_detail=debug,
            )

    def _expected_tts_segment_keys(self) -> set[tuple[str, str]]:
        voiceover = self._load_step_json("voiceover_script")
        expected: set[tuple[str, str]] = set()
        for script in voiceover.get("scripts") or []:
            if not isinstance(script, dict):
                continue
            vid = str(script.get("short_video_id") or "").strip()
            for seg in script.get("narration_segments") or []:
                if not isinstance(seg, dict):
                    continue
                shot_id = str(seg.get("shot_id") or "").strip()
                text = str(seg.get("text") or "").strip()
                if vid and shot_id and text:
                    expected.add((vid, shot_id))
        return expected

    def step_video_understanding(self) -> None:
        text_input = self._build_video_understanding_brief_text()
        result = self._run_text_agent("video_understanding", text_input)
        result = self._materialize_video_understanding(result)
        self._overwrite_step_json("video_understanding", result, materialized=True)

    def step_highlight_detection(self) -> None:
        text_input = self._build_highlight_detection_brief_text()
        result = self._run_text_agent("highlight_detection", text_input)
        result = self._materialize_candidate_clips_from_chunks(result, source_step="highlight_detection")
        self._overwrite_step_json("highlight_detection", result, materialized=True)

    def step_asr_event_candidate(self) -> None:
        result = self._run_text_agent("asr_event_candidate", self._build_asr_event_candidate_text())
        result = self._materialize_asr_event_candidate(result)
        self._overwrite_step_json("asr_event_candidate", result, materialized=True)

    def step_candidate_filter(self) -> None:
        pool = self._load_candidate_clip_pool(filtered=False)
        if not pool:
            self._write_candidate_filter_outputs(
                parsed={"keep_clip_ids": [], "merge_groups": []},
                filtered_pool=[],
                diagnostics=self._reassembly_error(
                    "candidate_pool_empty",
                    reason="ASR 事件候选生成没有找到可用原声片段",
                    suggestion="降低候选事件阈值，或检查 ASR 文本质量",
                    source_chunk_count=len(self._asr_event_segments()),
                ),
                status="failed",
            )
            raise UserFacingPipelineError(
                "candidate_pool_empty",
                user_message="候选池为空：ASR 事件候选生成没有找到可用原声片段。",
                suggestions=["检查 ASR 文本质量。", "从 ASR 事件候选步骤重跑，并放宽候选规则。"],
                technical_detail={"error_type": "candidate_pool_empty"},
            )
        result = self._run_text_agent("candidate_filter", self._build_candidate_filter_text())
        llm_input_hash = self.manifest.get("steps", {}).get("candidate_filter", {}).get("input_hash")
        normalized = self._normalize_candidate_filter_result(result, pool)
        filtered_pool = self._build_filtered_candidate_pool(pool, normalized)
        diagnostics = {}
        status = "success"
        if not filtered_pool:
            diagnostics = self._reassembly_error(
                "candidate_filter_empty",
                reason="候选复核后没有保留任何 clip",
                suggestion="放宽 candidate_filter，或允许保留 medium 质量片段",
                candidate_pool_count=len(pool),
                keep_clip_ids=normalized.get("keep_clip_ids", []),
                main_drop_reasons=normalized.get("diagnostics", {}).get("main_drop_reasons", []),
            )
            status = "failed"
        self._write_candidate_filter_outputs(normalized, filtered_pool, diagnostics=diagnostics, status=status, input_hash=llm_input_hash)
        if status == "failed":
            raise UserFacingPipelineError(
                "candidate_filter_empty",
                user_message="候选复核后为空：candidate_filter 没有保留任何原声片段。",
                suggestions=["放宽 candidate_filter 规则后重跑。", "检查候选片段是否都是半句话、弱背景或空镜。"],
                technical_detail=diagnostics,
            )

    def step_candidate_refine(self) -> None:
        self._mark_skipped("candidate_refine", "已删除：候选筛选前移到 content_analysis/highlight_detection，并由代码 materialize。")

    def step_highlight_reassembly_plan(self) -> None:
        result = self._run_text_agent("highlight_reassembly_plan", self._build_highlight_reassembly_plan_text())
        result = self._materialize_highlight_reassembly_plan(result)
        self._overwrite_step_json("highlight_reassembly_plan", result, materialized=True)

    def step_short_video_planning(self) -> None:
        self._run_text_agent("short_video_planning", {
            "candidate_clips": self._load_step_json("highlight_detection"),
            "video_analysis": self._load_step_json("video_understanding"),
            "duration_strategy": self._duration_strategy_payload(),
            "run_options": self._run_options_payload(),
        })

    def step_editing_script(self) -> None:
        self._run_text_agent("editing_script", {
            "short_video_plan": self._load_step_json("short_video_planning"),
            "candidate_clips": self._load_step_json("highlight_detection"),
            "merged_timeline": self._load_step_json("timeline"),
            "duration_strategy": self._duration_strategy_payload(),
            "run_options": self._run_options_payload(),
        })

    def _validate_ai_voiceover_editing_structure_or_raise(self, edit_plan: dict[str, Any]) -> None:
        if self.options.production_mode != "ai_voiceover":
            return

        issues: list[str] = []
        scripts = edit_plan.get("scripts", []) if isinstance(edit_plan, dict) else []
        if not isinstance(scripts, list) or not scripts:
            issues.append("short_video_edit_plan.scripts is empty")

        for script in scripts:
            sid = str(script.get("short_video_id") or "unknown")
            shots = script.get("editing_structure", [])
            if not isinstance(shots, list) or not shots:
                issues.append(f"{sid}: editing_structure 为空")
                continue

            for idx, shot in enumerate(shots, start=1):
                if not isinstance(shot, dict):
                    issues.append(f"{sid}: shot {idx} 不是对象")
                    continue

                shot_id = str(shot.get("shot_id") or f"shot_{idx}")
                source_id = str(shot.get("source_id") or "").strip()
                start, end = self._segment_start_end_seconds(shot)

                if self._is_virtual_source_manifest() and not source_id:
                    issues.append(f"{sid}/{shot_id}: 多源 AI 配音 shot 缺少 source_id")

                if start is None or end is None:
                    issues.append(f"{sid}/{shot_id}: 缺少 source_start/source_end")
                elif end <= start:
                    issues.append(f"{sid}/{shot_id}: source_end <= source_start ({start}-{end})")

        if issues:
            raise UserFacingPipelineError(
                "invalid_ai_voiceover_edit_plan",
                user_message="AI 配音剪辑规划无效：存在无法定位原视频的画面片段。",
                suggestions=[
                    "请从 content_analysis 或 short_video_edit_plan 重跑。",
                    "如果是多源任务，请检查候选 clip 是否都带有 source_id。",
                ],
                technical_detail={
                    "error_type": "invalid_ai_voiceover_edit_plan",
                    "issues": issues[:50],
                },
            )

    def _soft_target_warning(self, script: dict[str, Any]) -> None:
        visual_seconds = float(script.get("visual_total_seconds") or 0.0)
        target_seconds = float(script.get("target_duration_seconds") or 0.0)
        mode = str(script.get("target_duration_mode") or "soft")
        sid = str(script.get("short_video_id") or "unknown")

        if mode == "soft" and target_seconds > 0:
            if visual_seconds > target_seconds * 1.5:
                print(
                    f"[Soft Target] Video {sid} visual duration {visual_seconds:.1f}s is significantly longer than "
                    f"soft target {target_seconds:.1f}s, but within max limits."
                )

    def _validate_ai_voiceover_plan_duration_or_raise(self, edit_plan: dict[str, Any]) -> None:
        if self.options.production_mode != "ai_voiceover":
            return

        configured_hard_max = self._configured_ai_voiceover_max_seconds()
        effective_max = configured_hard_max
        allow_long = bool(self.options.allow_long_video)
        issues: list[str] = []

        for script in edit_plan.get("scripts") or []:
            if not isinstance(script, dict):
                continue
            sid = str(script.get("short_video_id") or "unknown")
            visual_seconds = editing_structure_duration(script.get("editing_structure") or [])

            self._soft_target_warning(script)

            if visual_seconds <= 0:
                issues.append(f"{sid}: visual duration is 0")
                continue

            if visual_seconds > effective_max + 0.01:
                issues.append(
                    f"{sid}: visual duration {visual_seconds:.1f}s exceeds AI voiceover hard max {effective_max:.1f}s"
                )

            if visual_seconds > self.duration_settings.hard_max_without_confirmation and not allow_long:
                issues.append(
                    f"{sid}: visual duration {visual_seconds:.1f}s exceeds hard max without long-video confirmation"
                )

        if issues:
            raise UserFacingPipelineError(
                "ai_voiceover_plan_duration_invalid",
                user_message="AI 配音选片规划失败：规划画面时长超过当前任务允许范围。",
                suggestions=[
                    "如果内容确实需要长版，请把 max_output_video_seconds 调大到 180，并允许 long video。",
                    "如果只想要短版，请从 short_video_edit_plan 重跑，并要求模型减少 clip_ids 或缩短候选片段。",
                ],
                technical_detail={"issues": issues[:50], "effective_max_output_video_seconds": effective_max},
            )

    def step_short_video_edit_plan(self) -> None:
        if self.options.production_mode == "ai_voiceover" and not self._load_candidate_clips_for_current_mode():
            raise UserFacingPipelineError(
                "short_video_edit_plan_candidate_clips_empty",
                user_message="选片规划失败：AI 配音候选片段为空。",
                suggestions=["回查 ai_voiceover_candidates/v*/candidate_clips.json。", "从 content_analysis 重新运行。"],
            )
        text_input = self._build_short_video_edit_plan_text()
        raw_result = self._run_text_agent("short_video_edit_plan", text_input)
        first_diagnostics = self._short_video_edit_plan_semantic_diagnostics(raw_result)
        retry_diagnostics: dict[str, Any] | None = None
        if self.options.production_mode == "ai_voiceover" and not first_diagnostics.get("ok"):
            first_out = self.task_dir / self._step_output("short_video_edit_plan")
            if first_out.exists():
                write_json(first_out.parent / "semantic_retry_first_output.json", raw_result)
                write_json(first_out.parent / "semantic_retry_first_diagnostics.json", first_diagnostics)
            retry_input = (
                text_input
                + "\n\n---\n"
                + "上一次输出未通过业务校验，请只使用输入中存在的 clip_id 重新输出 JSON。"
                + f"\n失败原因：{first_diagnostics.get('reason')}"
                + f"\n无效 clip_id：{first_diagnostics.get('invalid_clip_ids', [])}"
                + f"\n可用 clip_id：{first_diagnostics.get('candidate_clip_ids', [])}"
            )
            raw_result = self._run_text_agent("short_video_edit_plan", retry_input)
            retry_diagnostics = self._short_video_edit_plan_semantic_diagnostics(raw_result)
            retry_out = self.task_dir / self._step_output("short_video_edit_plan")
            if retry_out.exists():
                write_json(retry_out.parent / "semantic_retry_response.json", raw_result)
                write_json(retry_out.parent / "semantic_retry_diagnostics.json", {
                    "first": first_diagnostics,
                    "retry": retry_diagnostics,
                })
            if not retry_diagnostics.get("ok"):
                raise UserFacingPipelineError(
                    "short_video_edit_plan_semantic_retry_failed",
                    user_message="AI 配音选片规划失败：模型重试后仍未返回可用 clip_id。",
                    suggestions=["回查 agents/short_video_edit_plan/v*/semantic_retry_diagnostics.json。", "检查 candidate_clips 是否为空或 clip_id 是否过长难以复制。"],
                    technical_detail={"first": first_diagnostics, "retry": retry_diagnostics},
                )
        result = self._materialize_short_video_edit_plan(raw_result)
        if retry_diagnostics is not None:
            result["semantic_retry"] = {"first": first_diagnostics, "retry": retry_diagnostics}
        else:
            result["semantic_retry"] = {"first": first_diagnostics}
        self._validate_ai_voiceover_editing_structure_or_raise(result)
        self._validate_ai_voiceover_plan_duration_or_raise(result)
        self._overwrite_step_json("short_video_edit_plan", result, materialized=True)
        self._write_compat_short_video_plan_and_editing_script(result)

    def step_merge_decision(self) -> None:
        self._mark_skipped("merge_decision", "已删除：不要乱拆的规则已前移到 short_video_edit_plan。")

    def step_voiceover_script(self) -> None:
        edit_plan = self._load_step_json("short_video_edit_plan")
        scripts = [x for x in edit_plan.get("scripts", []) if isinstance(x, dict)]
        if not scripts:
            raise UserFacingPipelineError(
                "voiceover_script_no_edit_plan_scripts",
                user_message="配音文案生成失败：short_video_edit_plan.scripts 为空。",
                suggestions=["回查 agents/short_video_edit_plan/v*/short_video_edit_plan.json。"],
            )
        cfg = self.config.raw.get("voiceover_script", {})
        max_workers = max(1, int(cfg.get("max_workers", 2) or 2))
        model = str(cfg.get("model_name") or self._text_model_name())
        fallback = list(cfg.get("fallback_models") or self._text_fallback_models())
        input_hash = stable_hash({
            "short_video_edit_plan": self._step_content_hash("short_video_edit_plan"),
            "model": model,
            "fallback": fallback,
            "cfg": cfg,
        })
        if self._can_reuse("voiceover_script", input_hash):
            print("澶嶇敤缂撳瓨: voiceover_script")
            final_output = self._load_step_json("voiceover_script")
            self._finalize_voiceover_script_with_repair(
                final_output,
                edit_plan,
                vdir=Path(self.task_dir / self._step_output("voiceover_script")).parent,
                input_hash=input_hash,
                model=model,
                fallback=fallback,
            )
            return
        version, vdir = self._version_dir("voiceover_script", "agents/voiceover_script")
        ensure_dir(vdir)

        def generate_one(index: int, script: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
            short_video_id = str(script.get("short_video_id") or f"v_{index + 1:03d}")
            input_text = self._build_voiceover_script_text_input(script)
            write_text(vdir / short_video_id / "input.txt", input_text)
            try:
                if self.llm_text is None:
                    raise RuntimeError("missing text llm")
                result = self.llm_text.call_json(
                    model=model,
                    fallback_models=fallback,
                    prompt=prompts.VOICEOVER_SCRIPT_TEXT_PROMPT,
                    input_data=input_text,
                    temperature=float(cfg.get("temperature", 0.2) or 0.2),
                    max_tokens=int(cfg.get("max_tokens", 1200) or 1200),
                    debug_dir=vdir / "_llm_debug" / short_video_id,
                )
                parsed = result.parsed if isinstance(result.parsed, dict) else {}
                items = [x for x in parsed.get("scripts", []) if isinstance(x, dict)]
                if not items and parsed.get("short_video_id"):
                    items = [parsed]
                if not items:
                    raise UserFacingPipelineError(
                        "voiceover_script_model_empty",
                        user_message="配音文案生成失败：模型没有返回可用 scripts。",
                        suggestions=["回查 agents/voiceover_script/v*/_llm_debug。", "检查 voiceover_script 提示词输出格式。"],
                        technical_detail={"short_video_id": short_video_id, "parsed": parsed},
                    )
            except Exception as exc:
                write_json(vdir / "_errors" / f"{short_video_id}.json", {"short_video_id": short_video_id, "error": str(exc), "created_at": now_iso()})
                raise
            return index, items

        results: list[tuple[int, list[dict[str, Any]]]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(generate_one, index, script) for index, script in enumerate(scripts)]
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda x: x[0])
        final_output = {"scripts": [item for _index, items in results for item in items]}
        if not final_output["scripts"]:
            raise UserFacingPipelineError(
                "voiceover_script_empty",
                user_message="配音文案生成失败：没有生成任何可用文案。",
                suggestions=["回查 agents/voiceover_script/v*/_errors。", "回查 short_video_edit_plan 的 editing_structure。"],
            )
        out = write_json(vdir / "voiceover_script.json", final_output)
        self._write_status(vdir, self._base_status("voiceover_script", version, input_hash, [out]) | {"model": model})
        self._record_step(
            step="voiceover_script",
            version=version,
            status="success",
            output=relpath(out, self.task_dir),
            input_hash=input_hash,
            output_files=[out],
            extra={"model": model, "summary": {"scripts": len(final_output["scripts"]), "max_workers": max_workers}},
        )
        self._finalize_voiceover_script_with_repair(
            final_output,
            edit_plan,
            vdir=vdir,
            input_hash=input_hash,
            model=model,
            fallback=fallback,
        )

    def step_voiceover_quality_check(self) -> None:
        self._mark_skipped("voiceover_quality_check", "已删除：配音文案质量改由代码规则和时长校验处理。")

    def step_voiceover_quality_gate(self) -> None:
        self._mark_skipped("voiceover_quality_gate", "已删除：配音文案质量改由代码规则、TTS时长和成片时长校验处理。")

    def step_risk_review(self) -> None:
        self._run_text_agent("risk_review", {
            "editing_script": self._load_step_json("editing_script"),
            "voiceover_script": self._load_step_json("voiceover_script"),
            "video_analysis": self._load_step_json("video_understanding"),
            "duration_strategy": self._duration_strategy_payload(),
            "run_options": self._run_options_payload(),
        })

    def _overwrite_step_json(self, step: str, output: dict[str, Any], *, materialized: bool = False) -> None:
        out_path = self.task_dir / self._step_output(step)
        write_json(out_path, output)
        version_dir = out_path.parent
        write_json(version_dir / "final_output.json", output)
        if materialized:
            write_json(version_dir / "materialized_output.json", output)

        output_files = [out_path]
        new_hash = output_hash(output_files)
        step_status = self.manifest.setdefault("steps", {}).setdefault(step, {})
        step_status["output_hash"] = new_hash
        if materialized:
            step_status["materialized_output"] = True
        self._save_manifest()

        status_path = version_dir / "step_status.json"
        status_doc = read_json(status_path, {})
        if isinstance(status_doc, dict):
            status_doc["output_hash"] = new_hash
            if materialized:
                status_doc["materialized_output"] = True
            write_json(status_path, status_doc)

    def _timeline_chunks_by_id(self) -> dict[str, dict[str, Any]]:
        digest = self._load_current_timeline_digest()
        chunks = digest.get("chunks", []) if isinstance(digest, dict) else []
        out: dict[str, dict[str, Any]] = {}
        for index, chunk in enumerate(chunks, start=1):
            if not isinstance(chunk, dict):
                continue
            chunk_id = str(chunk.get("chunk_id") or chunk.get("global_chunk_id") or f"chunk_{index:04d}")
            out[chunk_id] = chunk
        return out

    def _coerce_id_list(self, value: Any) -> list[str]:
        if isinstance(value, str):
            parts = re.split(r"[,，\s]+", value)
            return [part.strip() for part in parts if part.strip()]
        if isinstance(value, list):
            out: list[str] = []
            for item in value:
                text = str(item).strip()
                if text:
                    out.append(text)
            return out
        return []

    def _chunk_start_end(self, chunk: dict[str, Any]) -> tuple[float, float]:
        start = float(chunk.get("local_start_seconds", chunk.get("start_seconds", chunk.get("start", 0))) or 0)
        end = float(chunk.get("local_end_seconds", chunk.get("end_seconds", chunk.get("end", start))) or start)
        if end <= start and chunk.get("duration_seconds"):
            end = start + float(chunk.get("duration_seconds") or 0)
        return start, end

    def _materialize_video_understanding(self, result: Any) -> dict[str, Any]:
        raw = result if isinstance(result, dict) else {}
        understanding = str(
            raw.get("understanding")
            or raw.get("summary")
            or raw.get("overall_understanding")
            or ""
        ).strip()
        key_facts = raw.get("key_facts")
        if not isinstance(key_facts, list):
            key_facts = []
        people = raw.get("people")
        if not isinstance(people, list):
            people = []
        locations = raw.get("locations")
        if not isinstance(locations, list):
            locations = []
        return {
            "version": "video_understanding_materialized_v2",
            "understanding": understanding,
            "main_topic": str(raw.get("main_topic") or raw.get("topic") or "").strip(),
            "video_type": str(raw.get("video_type") or ""),
            "summary": str(raw.get("summary") or understanding).strip(),
            "key_facts": key_facts,
            "people": people,
            "locations": locations,
            "materialized_by_code": True,
            "raw_model_plan": result,
        }

    def _materialize_vision_chunk_result(self, result: Any, *, chunk_id: str, time_range: str) -> dict[str, Any]:
        raw = result if isinstance(result, dict) else {}
        visual = str(raw.get("visual") or raw.get("visual_summary") or "").strip()
        screen_text = raw.get("screen_text")
        if not isinstance(screen_text, list):
            screen_text = []
        visible_people = raw.get("visible_people")
        if not isinstance(visible_people, list):
            visible_people = []
        location_clues = raw.get("location_clues")
        if not isinstance(location_clues, list):
            location_clues = []
        warnings = raw.get("warnings")
        if not isinstance(warnings, list):
            warnings = []
        return {
            "chunk_id": chunk_id,
            "time_range": time_range,
            "visual": visual,
            "visual_summary": visual,
            "scene_type": str(raw.get("scene_type") or ""),
            "screen_text": screen_text,
            "visible_people": visible_people,
            "location_clues": location_clues,
            "footage_type": str(raw.get("footage_type") or ""),
            "asr_visual_consistency": str(raw.get("asr_visual_consistency") or ""),
            "warnings": warnings,
            "materialized_by_code": True,
            "raw_model_plan": result,
        }

    def _group_chunks_by_source_and_contiguity(
        self,
        chunks: list[dict[str, Any]],
        *,
        max_gap_seconds: float = 3.0,
    ) -> list[list[dict[str, Any]]]:
        by_source: dict[str, list[dict[str, Any]]] = {}

        for chunk in chunks:
            source_id = str(chunk.get("source_id") or "source_1").strip()
            if not source_id:
                continue
            by_source.setdefault(source_id, []).append(chunk)

        groups: list[list[dict[str, Any]]] = []

        for _source_id, source_chunks in by_source.items():
            items = sorted(source_chunks, key=lambda c: self._chunk_start_end(c)[0])
            current: list[dict[str, Any]] = []
            current_end: float | None = None

            for chunk in items:
                start, end = self._chunk_start_end(chunk)
                if end <= start:
                    continue

                if current and current_end is not None and start > current_end + max_gap_seconds:
                    groups.append(current)
                    current = []

                current.append(chunk)
                current_end = max(current_end or end, end)

            if current:
                groups.append(current)

        return groups

    def _source_safe_clip_id(
        self,
        *,
        raw_clip_id: str,
        source_id: str,
        index: int,
    ) -> str:
        source_slug = re.sub(r"[^0-9A-Za-z_]+", "_", source_id).strip("_")
        if not source_slug:
            source_slug = "source"
        return f"{source_slug}_clip_{index:03d}"

    def _materialize_clip_from_chunk_group(
        self,
        *,
        raw: dict[str, Any],
        chunks: list[dict[str, Any]],
        clip_id: str,
    ) -> dict[str, Any] | None:
        if not chunks:
            return None

        starts_ends = [self._chunk_start_end(chunk) for chunk in chunks]
        starts_ends = [(s, e) for s, e in starts_ends if e > s]
        if not starts_ends:
            return None

        start = min(s for s, _e in starts_ends)
        end = max(e for _s, e in starts_ends)
        if end <= start:
            return None

        source_ids = [str(chunk.get("source_id") or "source_1") for chunk in chunks]
        unique_source_ids = list(dict.fromkeys(source_ids))
        if len(unique_source_ids) != 1:
            return None

        source_id = unique_source_ids[0]
        source_index = chunks[0].get("source_index", "")

        chunk_ids = [
            str(chunk.get("chunk_id") or chunk.get("global_chunk_id") or "")
            for chunk in chunks
            if str(chunk.get("chunk_id") or chunk.get("global_chunk_id") or "").strip()
        ]

        speech = " ".join(
            compact_text(str(chunk.get("speech") or chunk.get("asr_digest") or ""), max_chars=260)
            for chunk in chunks
            if str(chunk.get("speech") or chunk.get("asr_digest") or "").strip()
        ).strip()

        visual = " ".join(
            compact_text(str(chunk.get("visual") or chunk.get("visual_summary") or ""), max_chars=180)
            for chunk in chunks
            if str(chunk.get("visual") or chunk.get("visual_summary") or "").strip()
        ).strip()

        summary = str(raw.get("summary") or raw.get("reason") or speech or visual).strip()
        clip_type = str(raw.get("type") or raw.get("clip_type") or raw.get("candidate_type") or "").strip()

        return {
            "clip_id": clip_id,
            "raw_clip_id": str(raw.get("clip_id") or raw.get("id") or ""),
            "chunk_ids": chunk_ids,
            "source_chunk_ids": chunk_ids,

            "source_id": source_id,
            "source_index": source_index,
            "source_ids": [source_id],

            "local_start": seconds_to_timecode(start, ms=True),
            "local_end": seconds_to_timecode(end, ms=True),
            "local_start_seconds": round(start, 3),
            "local_end_seconds": round(end, 3),

            "source_start": seconds_to_timecode(start, ms=True),
            "source_end": seconds_to_timecode(end, ms=True),
            "source_start_seconds": round(start, 3),
            "source_end_seconds": round(end, 3),

            "start": seconds_to_timecode(start, ms=True),
            "end": seconds_to_timecode(end, ms=True),
            "start_seconds": round(start, 3),
            "end_seconds": round(end, 3),
            "duration_seconds": round(end - start, 3),

            "speech": speech,
            "visual": visual,
            "summary": summary,
            "type": clip_type,
            "clip_type": clip_type,
        }

    def _materialize_candidate_clips_from_chunks(self, result: Any, *, source_step: str) -> dict[str, Any]:
        if isinstance(result, list):
            raw_clips = result
            base_result: dict[str, Any] = {}
        elif isinstance(result, dict):
            raw_clips = result.get("candidate_clips", [])
            base_result = dict(result)
        else:
            raw_clips = []
            base_result = {}
        chunk_by_id = self._timeline_chunks_by_id()
        if isinstance(raw_clips, dict):
            raw_clips = raw_clips.get("candidate_clips", [])
        if not isinstance(raw_clips, list):
            raw_clips = []

        materialized: list[dict[str, Any]] = []
        per_source_clip_count: dict[str, int] = {}
        for index, raw in enumerate(raw_clips, start=1):
            if not isinstance(raw, dict):
                continue
            chunk_ids = self._coerce_id_list(
                raw.get("chunk_ids")
                or raw.get("source_chunk_ids")
                or raw.get("chunks")
                or raw.get("chunk_id")
            )
            chunks = [chunk_by_id[cid] for cid in chunk_ids if cid in chunk_by_id]
            if chunks:
                chunk_groups = self._group_chunks_by_source_and_contiguity(
                    chunks,
                    max_gap_seconds=float(
                        self.config.raw.get("ai_voiceover", {}).get("candidate_chunk_max_gap_seconds", 3.0)
                    ),
                )
                for group in chunk_groups:
                    source_id = str(group[0].get("source_id") or "source_1").strip()
                    per_source_clip_count[source_id] = per_source_clip_count.get(source_id, 0) + 1

                    raw_clip_id = str(raw.get("clip_id") or raw.get("id") or f"clip_{index:03d}")
                    new_clip_id = self._source_safe_clip_id(
                        raw_clip_id=raw_clip_id,
                        source_id=source_id,
                        index=per_source_clip_count[source_id],
                    )

                    item = self._materialize_clip_from_chunk_group(
                        raw=raw,
                        chunks=group,
                        clip_id=new_clip_id,
                    )
                    if item:
                        materialized.append(item)
                continue
            else:
                start = self._clip_time_seconds(raw, "start", "source_start")
                end = self._clip_time_seconds(raw, "end", "source_end")
                if end <= start and raw.get("duration_seconds"):
                    end = start + float(raw.get("duration_seconds") or 0)
                source_id = str(raw.get("source_id") or "source_1").strip()
                if self._is_virtual_source_manifest() and not source_id:
                    continue
                unique_source_ids = [source_id] if source_id else []
                source_index = raw.get("source_index", "")
                speech = str(raw.get("speech") or raw.get("asr_text") or "").strip()
                visual = str(raw.get("visual") or raw.get("visual_summary") or "").strip()
                if not chunk_ids and raw.get("source_chunk_ids"):
                    chunk_ids = self._coerce_id_list(raw.get("source_chunk_ids"))
            if end <= start:
                continue
            
            per_source_clip_count[source_id] = per_source_clip_count.get(source_id, 0) + 1
            raw_clip_id = str(raw.get("clip_id") or raw.get("id") or f"clip_{index:03d}")
            new_clip_id = self._source_safe_clip_id(
                raw_clip_id=raw_clip_id,
                source_id=source_id,
                index=per_source_clip_count[source_id],
            )
            summary = str(raw.get("summary") or raw.get("reason") or speech or visual).strip()
            clip_type = str(raw.get("type") or raw.get("clip_type") or raw.get("candidate_type") or "").strip()
            materialized.append({
                "clip_id": new_clip_id,
                "raw_clip_id": raw_clip_id,
                "chunk_ids": chunk_ids,
                "source_chunk_ids": chunk_ids,
                "local_start": seconds_to_timecode(start, ms=True),
                "local_end": seconds_to_timecode(end, ms=True),
                "local_start_seconds": round(start, 3),
                "local_end_seconds": round(end, 3),
                "start": seconds_to_timecode(start, ms=True),
                "end": seconds_to_timecode(end, ms=True),
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "duration_seconds": round(end - start, 3),
                "source_id": source_id,
                "source_index": source_index,
                "source_ids": unique_source_ids,
                "speech": speech,
                "visual": visual,
                "summary": summary,
                "type": clip_type,
                "clip_type": clip_type,
            })

        normalized = base_result
        normalized["candidate_clips"] = materialized
        if source_step == "content_analysis":
            candidate_summaries = [
                str(item.get("summary") or "").strip()
                for item in materialized
                if str(item.get("summary") or "").strip()
            ]
            if not str(normalized.get("summary") or "").strip() and candidate_summaries:
                normalized["summary"] = compact_text(" ".join(candidate_summaries[:6]), max_chars=800)
            if not normalized.get("key_facts") and candidate_summaries:
                normalized["key_facts"] = candidate_summaries[:8]
        normalized.setdefault("version", f"{source_step}_materialized_v2")
        normalized["materialized_by_code"] = True
        return normalized

    def _normalize_asr_event_result(self, result: Any) -> dict[str, Any]:
        raw = result if isinstance(result, dict) else {}
        keep = self._coerce_id_list(raw.get("keep") or raw.get("keep_segment_ids") or raw.get("segment_ids"))
        merge_raw = raw.get("merge") or raw.get("merge_groups") or []
        merge_groups: list[list[str]] = []
        if isinstance(merge_raw, list):
            for group in merge_raw:
                ids = self._coerce_id_list(group)
                if len(ids) >= 2:
                    merge_groups.append(ids)
                    for sid in ids:
                        if sid not in keep:
                            keep.append(sid)
        return {"keep": keep, "merge": merge_groups, "raw_model_plan": result}

    def _materialize_asr_event_candidate(self, result: Any) -> dict[str, Any]:
        normalized = self._normalize_asr_event_result(result)
        segments = self._asr_event_segments()
        by_id = {str(seg.get("segment_id")): seg for seg in segments}
        keep_ids = [sid for sid in normalized["keep"] if sid in by_id]
        if not keep_ids:
            keep_ids = [str(seg.get("segment_id")) for seg in segments]

        used: set[str] = set()
        groups: list[list[str]] = []
        for group in normalized["merge"]:
            valid = [sid for sid in group if sid in by_id and sid in keep_ids]
            if len(valid) < 2:
                continue
            source_ids = {str(by_id[sid].get("source_id") or "") for sid in valid}
            if len(source_ids) != 1:
                continue
            groups.append(valid)
            used.update(valid)
        for sid in keep_ids:
            if sid not in used:
                groups.append([sid])

        per_source_count: dict[str, int] = {}
        candidate_clips: list[dict[str, Any]] = []
        for group in groups:
            group_segments = [by_id[sid] for sid in group if sid in by_id]
            if not group_segments:
                continue
            source_id = str(group_segments[0].get("source_id") or "source_1")
            source_ids = {str(seg.get("source_id") or "source_1") for seg in group_segments}
            if len(source_ids) != 1:
                continue
            
            ranges = []
            for seg in group_segments:
                seg_start, seg_end = self._segment_start_end_seconds(seg)
                if seg_start is None or seg_end is None or seg_end <= seg_start:
                    continue
                ranges.append((seg_start, seg_end))

            if not ranges:
                continue

            start = min(item[0] for item in ranges)
            end = max(item[1] for item in ranges)

            per_source_count[source_id] = per_source_count.get(source_id, 0) + 1
            source_digits = re.sub(r"\D+", "", source_id) or str(group_segments[0].get("source_index") or 1)
            clip_id = f"src{int(source_digits):02d}_evt_{per_source_count[source_id]:03d}" if source_digits.isdigit() else f"{source_id}_evt_{per_source_count[source_id]:03d}"
            speech = " ".join(str(seg.get("speech") or "").strip() for seg in group_segments if str(seg.get("speech") or "").strip())
            visual = " ".join(str(seg.get("visual_summary") or "").strip() for seg in group_segments if str(seg.get("visual_summary") or "").strip())
            chunk_ids: list[str] = []
            for seg in group_segments:
                for chunk_id in seg.get("source_chunk_ids") or []:
                    if chunk_id not in chunk_ids:
                        chunk_ids.append(str(chunk_id))
            candidate_clips.append({
                "clip_id": clip_id,
                "source_id": source_id,
                "source_index": group_segments[0].get("source_index", ""),
                "source_start_seconds": round(start, 3),
                "source_end_seconds": round(end, 3),
                "source_start": seconds_to_timecode(start, ms=True),
                "source_end": seconds_to_timecode(end, ms=True),
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "start": seconds_to_timecode(start, ms=True),
                "end": seconds_to_timecode(end, ms=True),
                "duration_seconds": round(end - start, 3),
                "segment_ids": group,
                "asr_text": speech,
                "speech": speech,
                "event_summary": compact_text(speech, max_chars=220),
                "summary": compact_text(speech, max_chars=220),
                "event_type": "",
                "news_value": "medium",
                "independent": True,
                "needs_context": False,
                "context_reason": "",
                "source_chunk_ids": chunk_ids,
                "chunk_ids": chunk_ids,
                "visual_summary": compact_text(visual, max_chars=240),
                "visual": compact_text(visual, max_chars=240),
                "visual_support": "unknown",
            })

        return {
            "version": "asr_event_candidate_materialized_v1",
            "keep_segment_ids": keep_ids,
            "merge_groups": groups,
            "segments": segments,
            "candidate_clips": candidate_clips,
            "candidate_clip_pool": candidate_clips,
            "materialized_by_code": True,
            "raw_model_plan": result,
        }

    def _load_candidate_clip_pool(self, *, filtered: bool) -> list[dict[str, Any]]:
        if filtered:
            doc = self._load_optional_step_json("candidate_filter", {})
            clips = doc.get("candidate_clip_pool_filtered") or doc.get("filtered_candidate_clips") or []
            return [clip for clip in clips if isinstance(clip, dict)]
        if self.options.production_mode == "highlight_reassembly":
            doc = self._load_optional_step_json("asr_event_candidate", {})
            clips = doc.get("candidate_clip_pool") or doc.get("candidate_clips") or []
            if clips:
                return [clip for clip in clips if isinstance(clip, dict)]
            doc = self._load_optional_step_json("highlight_detection", {})
            clips = doc.get("candidate_clips", [])
            if isinstance(clips, dict):
                clips = clips.get("candidate_clips", [])
            return [clip for clip in clips if isinstance(clip, dict)]
        return self._load_candidate_clips_for_current_mode()

    def _normalize_candidate_filter_result(self, result: Any, pool: list[dict[str, Any]]) -> dict[str, Any]:
        raw = result if isinstance(result, dict) else {}
        pool_ids = [str(clip.get("clip_id") or "") for clip in pool if clip.get("clip_id")]
        keep = self._coerce_id_list(raw.get("keep_clip_ids") or raw.get("keep") or raw.get("clip_ids"))
        keep = [cid for cid in keep if cid in pool_ids]
        if not keep:
            keep = pool_ids
        merge_groups: list[list[str]] = []
        for group in raw.get("merge_groups") or raw.get("merge") or []:
            ids = self._coerce_id_list(group.get("clip_ids") if isinstance(group, dict) else group)
            ids = [cid for cid in ids if cid in pool_ids]
            if len(ids) >= 2:
                merge_groups.append(ids)
        ranked = [cid for cid in self._coerce_id_list(raw.get("ranked_clip_ids")) if cid in keep]
        diagnostics = raw.get("diagnostics") if isinstance(raw.get("diagnostics"), dict) else {}
        return {
            "version": "candidate_filter_materialized_v1",
            "keep_clip_ids": keep,
            "drop_clip_ids": [cid for cid in pool_ids if cid not in keep],
            "merge_groups": merge_groups,
            "ranked_clip_ids": ranked,
            "diagnostics": diagnostics,
            "materialized_by_code": True,
            "raw_model_plan": result,
        }

    def _normalize_clip_time_fields(self, clip: dict[str, Any]) -> dict[str, Any]:
        item = dict(clip)
        start, end = self._clip_start_end(item)
        duration = max(0.0, end - start)

        item["local_start_seconds"] = round(start, 3)
        item["local_end_seconds"] = round(end, 3)
        item["source_start_seconds"] = round(start, 3)
        item["source_end_seconds"] = round(end, 3)
        item["start_seconds"] = round(start, 3)
        item["end_seconds"] = round(end, 3)

        item["local_start"] = seconds_to_timecode(start, ms=True)
        item["local_end"] = seconds_to_timecode(end, ms=True)
        item["source_start"] = seconds_to_timecode(start, ms=True)
        item["source_end"] = seconds_to_timecode(end, ms=True)
        item["start"] = seconds_to_timecode(start, ms=True)
        item["end"] = seconds_to_timecode(end, ms=True)

        item["duration_seconds"] = round(duration, 3)
        return item

    def _build_filtered_candidate_pool(self, pool: list[dict[str, Any]], filter_doc: dict[str, Any]) -> list[dict[str, Any]]:
        by_id = {str(clip.get("clip_id")): clip for clip in pool if clip.get("clip_id")}
        keep_ids = filter_doc.get("ranked_clip_ids") or filter_doc.get("keep_clip_ids") or []
        group_by_clip: dict[str, str] = {}
        for group_index, group in enumerate(filter_doc.get("merge_groups") or [], start=1):
            gid = f"story_{group_index:03d}"
            for cid in group:
                group_by_clip[str(cid)] = gid
        filtered: list[dict[str, Any]] = []
        for cid in keep_ids:
            if cid not in by_id:
                continue
            clip = self._normalize_clip_time_fields(by_id[cid])
            if cid in group_by_clip:
                clip["story_group_id"] = group_by_clip[cid]
                clip["merge_group_id"] = group_by_clip[cid]
            filtered.append(clip)
        return filtered

    def _write_candidate_filter_outputs(
        self,
        parsed: dict[str, Any],
        filtered_pool: list[dict[str, Any]],
        *,
        diagnostics: dict[str, Any] | None = None,
        status: str = "success",
        input_hash: str | None = None,
    ) -> None:
        effective_input_hash = input_hash or stable_hash({
            "candidate_filter": self._step_content_hash("asr_event_candidate"),
            "video_understanding": self._step_content_hash("video_understanding"),
            "parsed": parsed,
        })
        version, vdir = self._version_dir("candidate_filter", "agents/candidate_filter")
        ensure_dir(vdir)
        output = dict(parsed)
        output["candidate_clip_pool_filtered"] = filtered_pool
        output["filtered_candidate_clips"] = filtered_pool
        output["diagnostic_error"] = diagnostics or {}
        out = write_json(vdir / "candidate_filter.json", output)
        write_json(vdir / "candidate_clip_pool_filtered.json", {"candidate_clips": filtered_pool})
        status_doc = self._base_status("candidate_filter", version, effective_input_hash, [out])
        status_doc["status"] = status
        if diagnostics:
            status_doc["technical_error"] = diagnostics
        self._write_status(vdir, status_doc)
        self._record_step(
            step="candidate_filter",
            version=version,
            status=status,
            output=relpath(out, self.task_dir),
            input_hash=effective_input_hash,
            output_files=[out],
            extra={"summary": {"filtered_clips": len(filtered_pool)}, "technical_error": diagnostics or {}},
        )

    def _candidate_clips_by_id(self) -> dict[str, dict[str, Any]]:
        clips = (
            self._load_candidate_clip_pool(filtered=True)
            if self.options.production_mode == "highlight_reassembly"
            else self._load_candidate_clips_for_current_mode()
        )

        by_id: dict[str, dict[str, Any]] = {}
        duplicate_raw_ids: dict[str, list[dict[str, Any]]] = {}

        for index, clip in enumerate(clips):
            clip_id = self._clip_id(clip, index)
            if clip_id:
                by_id[clip_id] = clip

            source_id = str(clip.get("source_id") or "").strip()
            if source_id and clip_id:
                by_id[f"{source_id}:{clip_id}"] = clip

            raw_clip_id = str(clip.get("raw_clip_id") or "").strip()
            if raw_clip_id:
                duplicate_raw_ids.setdefault(raw_clip_id, []).append(clip)

        # 只有 raw_clip_id 唯一时才兼容旧 ID
        for raw_clip_id, items in duplicate_raw_ids.items():
            if len(items) == 1:
                by_id.setdefault(raw_clip_id, items[0])

        return by_id

    def _stringify_fact_points(self, values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        out: list[str] = []
        for item in values:
            if isinstance(item, str):
                text = item.strip()
            else:
                text = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            if text and text not in out:
                out.append(text)
        return out

    def _materialize_short_video_edit_plan(self, result: Any) -> dict[str, Any]:
        if isinstance(result, list):
            raw_videos = result
        elif isinstance(result, dict):
            raw_videos = result.get("videos")
            if not isinstance(raw_videos, list):
                raw_videos = result.get("scripts") if isinstance(result.get("scripts"), list) else []
        else:
            raw_videos = []
        content = self._load_optional_step_json("content_analysis", {})
        clips_by_id = self._candidate_clips_by_id()

        scripts: list[dict[str, Any]] = []
        used_clip_ids: set[str] = set()
        topic = str(content.get("main_topic") or content.get("topic") or "")
        default_facts = self._stringify_fact_points(content.get("key_facts") or [])
        for video_index, video in enumerate(raw_videos, start=1):
            if not isinstance(video, dict):
                continue
            selected_clip_meta: dict[str, dict[str, Any]] = {}
            selected_clips_raw = video.get("selected_clips")
            if isinstance(selected_clips_raw, list) and selected_clips_raw:
                clip_ids = []
                for item in selected_clips_raw:
                    if not isinstance(item, dict):
                        continue
                    clip_id = str(item.get("clip_id") or item.get("source_clip_id") or "").strip()
                    if clip_id:
                        clip_ids.append(clip_id)
                        selected_clip_meta[clip_id] = item
            else:
                clip_ids = self._coerce_id_list(video.get("clip_ids") or video.get("source_clip_ids"))
            selected = [clips_by_id[cid] for cid in clip_ids if cid in clips_by_id]
            if not selected:
                continue
            short_video_id = f"v_{len(scripts) + 1:03d}"
            source_clip_ids = [self._clip_id(clip, idx) for idx, clip in enumerate(selected)]
            used_clip_ids.update(source_clip_ids)
            must_keep = self._stringify_fact_points(video.get("必须讲到") or video.get("must_keep_fact_points") or [])
            if not must_keep:
                must_keep = default_facts
            news_angle = str(video.get("讲述重点") or video.get("news_angle") or topic or "").strip()
            editing_structure: list[dict[str, Any]] = []
            for shot_index, clip in enumerate(selected, start=1):
                start, end = self._clip_start_end(clip)
                duration = max(0.0, end - start)
                source_clip_id = self._clip_id(clip, shot_index - 1)
                local_start = seconds_to_timecode(start, ms=True)
                local_end = seconds_to_timecode(end, ms=True)
                visual = str(clip.get("visual") or clip.get("visual_summary") or "").strip()
                fact = str(clip.get("summary") or clip.get("speech") or clip.get("clip_type") or "").strip()
                clip_meta = selected_clip_meta.get(source_clip_id, {})
                section = str(clip_meta.get("section") or clip.get("section") or ("hook" if shot_index == 1 else "evidence")).strip()
                narration_intent = str(
                    clip_meta.get("narration_intent")
                    or clip.get("narration_intent")
                    or clip.get("role")
                    or fact
                    or visual
                ).strip()
                editing_structure.append({
                    "order": shot_index,
                    "shot_id": f"{short_video_id}_s{shot_index:02d}",
                    "source_clip_id": source_clip_id,
                    "clip_id": source_clip_id,
                    "source_id": clip.get("source_id", ""),
                    "source_index": clip.get("source_index", ""),
                    "local_start": local_start,
                    "local_end": local_end,
                    "local_start_seconds": round(start, 3),
                    "local_end_seconds": round(end, 3),
                    "source_start": local_start,
                    "source_end": local_end,
                    "source_start_seconds": round(start, 3),
                    "source_end_seconds": round(end, 3),
                    "duration_seconds": round(duration, 3),
                    "section": section,
                    "visual": visual,
                    "visual_summary": visual,
                    "fact": fact,
                    "news_fact_to_explain": fact,
                    "narration_intent": narration_intent,
                    "purpose": "evidence_visual" if shot_index > 1 else "opening_hook",
                    "must_say_facts": must_keep if shot_index == 1 else [],
                })
            visual_total = editing_structure_duration(editing_structure)
            requested_target = float(getattr(self.options, "target_duration_seconds", 30) or 30)
            max_allowed = float(getattr(self.options, "max_output_video_seconds", 180) or 180)

            effective_target = self._resolve_ai_voiceover_effective_target_seconds(
                visual_total_seconds=visual_total,
                requested_target_seconds=requested_target,
                max_allowed_seconds=max_allowed,
            )

            editing_structure = self._assign_shot_duration_budget(editing_structure, effective_target)
            editing_structure = self._attach_voiceover_char_budget(editing_structure)
            editing_structure = [
                self._normalize_voiceover_budget_fields(shot)
                for shot in editing_structure
                if isinstance(shot, dict)
            ]
            scripts.append({
                "short_video_id": short_video_id,
                "topic": topic,
                "news_angle": news_angle,
                "video_type": "解说型",
                "requested_target_duration_seconds": round(requested_target, 3),
                "target_duration_seconds": round(effective_target, 3),
                "effective_target_duration_seconds": round(effective_target, 3),
                "max_allowed_seconds": round(max_allowed, 3),
                "visual_total_seconds": round(visual_total, 3),
                "target_duration_mode": str(getattr(self.options, "target_duration_mode", "soft") or "soft"),
                "source_clip_ids": source_clip_ids,
                "must_keep_fact_points": must_keep,
                "voiceover_brief": news_angle,
                "editing_structure": editing_structure,
            })

        discarded = [cid for cid in clips_by_id if cid not in used_clip_ids]
        return {
            "version": "short_video_edit_plan_materialized_v2",
            "recommended_video_count": len(scripts),
            "overall_reason": "模型输出 clip_ids，代码已补全 short_video_id、shot_id、时间码、时长和旧工程结构。",
            "scripts": scripts,
            "discarded_clip_ids": discarded,
            "materialized_by_code": True,
            "raw_model_plan": result,
        }

    def _short_video_edit_plan_semantic_diagnostics(self, result: Any) -> dict[str, Any]:
        if isinstance(result, list):
            raw_videos = result
        elif isinstance(result, dict):
            raw_videos = result.get("videos")
            if not isinstance(raw_videos, list):
                raw_videos = result.get("scripts") if isinstance(result.get("scripts"), list) else []
        else:
            raw_videos = []

        clips_by_id = self._candidate_clips_by_id()
        invalid_clip_ids: list[str] = []
        empty_items: list[int] = []
        valid_clip_ids: list[str] = []
        if not isinstance(raw_videos, list) or not raw_videos:
            return {
                "ok": False,
                "reason": "empty_business_result",
                "raw_video_count": 0,
                "candidate_clip_ids": list(clips_by_id),
            }

        for index, video in enumerate(raw_videos):
            if not isinstance(video, dict):
                empty_items.append(index)
                continue
            clip_ids = self._coerce_id_list(video.get("clip_ids") or video.get("source_clip_ids"))
            selected_clips = video.get("selected_clips")
            if not clip_ids and isinstance(selected_clips, list):
                clip_ids = self._coerce_id_list([
                    item.get("clip_id") or item.get("source_clip_id") or ""
                    for item in selected_clips
                    if isinstance(item, dict)
                ])
            if not clip_ids:
                empty_items.append(index)
                continue
            for clip_id in clip_ids:
                if clip_id in clips_by_id:
                    valid_clip_ids.append(clip_id)
                else:
                    invalid_clip_ids.append(clip_id)

        reason = ""
        if invalid_clip_ids:
            reason = "invalid_clip_ids"
        elif not valid_clip_ids:
            reason = "empty_business_result"
        return {
            "ok": not reason,
            "reason": reason,
            "raw_video_count": len(raw_videos),
            "valid_clip_ids": list(dict.fromkeys(valid_clip_ids)),
            "invalid_clip_ids": list(dict.fromkeys(invalid_clip_ids)),
            "empty_items": empty_items,
            "candidate_clip_ids": list(clips_by_id),
        }

    def _first_number(self, *values: Any) -> float | None:
        for value in values:
            if value in (None, ""):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                return number
        return None

    def _safe_positive_float(self, value: Any, *, default: float = 0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(number) or number <= 0:
            return default
        return round(number, 3)

    def _safe_float(self, value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(number):
            return default
        return number

    def _resolve_ai_voiceover_effective_target_seconds(
        self,
        *,
        visual_total_seconds: float,
        requested_target_seconds: float | None = None,
        max_allowed_seconds: float | None = None,
    ) -> float:
        requested = self._safe_float(
            requested_target_seconds or getattr(self.options, "target_duration_seconds", None),
            float(getattr(self.duration_settings, "default_target_seconds", 30) or 30),
        )

        max_allowed = self._safe_float(
            max_allowed_seconds or getattr(self.options, "max_output_video_seconds", None),
            float(getattr(self.duration_settings, "max_long_video_seconds", 180) or 180),
        )

        if max_allowed <= 0:
            max_allowed = 180.0

        visual_total = self._safe_float(visual_total_seconds, 0.0)
        mode = str(getattr(self.options, "target_duration_mode", "soft") or "soft").lower()

        if mode == "fixed":
            return round(min(max(requested, 1.0), max_allowed), 3)

        if visual_total <= 0:
            return round(min(max(requested, 1.0), max_allowed), 3)

        return round(min(max(visual_total, 1.0), max_allowed), 3)

    def _normalize_voiceover_budget_fields(self, shot: dict[str, Any]) -> dict[str, Any]:
        item = dict(shot)

        min_chars = self._first_number(item.get("narration_min_chars"))
        target_chars = self._first_number(item.get("narration_target_chars"))
        max_chars = self._first_number(item.get("narration_max_chars"))

        voice_cfg = self.config.raw.get("voiceover", {}) if hasattr(self, "config") else {}
        cps = float(voice_cfg.get("voiceover_chars_per_second") or 4.8)

        target_duration = self._first_number(item.get("target_duration_seconds"))
        if target_duration is None:
            target_duration = self._first_number(item.get("duration_seconds"))

        # 如果完全没有预算，则按目标时长估算。
        if target_duration and target_duration > 0:
            estimated_target = max(1, int(round(target_duration * cps)))
            if target_chars is None or target_chars <= 0:
                target_chars = estimated_target
            if min_chars is None or min_chars <= 0:
                min_chars = int(round(target_chars * 0.75))
            if max_chars is None or max_chars <= 0:
                max_chars = int(round(target_chars * 1.25))

        # 如果仍然没有预算，直接返回，避免误伤旧产物。
        if min_chars is None and target_chars is None and max_chars is None:
            return item

        # 补齐缺失值。
        if target_chars is None:
            if min_chars is not None and max_chars is not None:
                target_chars = int(round((min_chars + max_chars) / 2))
            elif min_chars is not None:
                target_chars = min_chars
            elif max_chars is not None:
                target_chars = max_chars

        if min_chars is None:
            min_chars = int(round(float(target_chars) * 0.75))
        if max_chars is None:
            max_chars = int(round(float(target_chars) * 1.25))

        min_chars = int(max(0, round(float(min_chars))))
        target_chars = int(max(0, round(float(target_chars))))
        max_chars = int(max(0, round(float(max_chars))))

        # 核心修复：不能出现 min > target > max 这种矛盾。
        if min_chars > target_chars:
            min_chars = target_chars
        if max_chars < target_chars:
            max_chars = target_chars
        if min_chars > max_chars:
            min_chars = max_chars

        # 对很短 shot 做保护，避免 0 字预算。
        if target_duration and target_duration > 0:
            min_floor = 6 if target_duration < 3 else 10
            target_chars = max(target_chars, min_floor)
            max_chars = max(max_chars, target_chars)
            min_chars = min(max(min_chars, min_floor), target_chars)

        item["narration_min_chars"] = min_chars
        item["narration_target_chars"] = target_chars
        item["narration_max_chars"] = max_chars
        return item

    def _resolve_tts_check_target_duration(self, shot: dict[str, Any]) -> tuple[float, float, float]:
        target = self._safe_positive_float(shot.get("target_duration_seconds"), default=0.0)
        source_duration = self._safe_positive_float(shot.get("duration_seconds"), default=0.0)

        # AI 配音模式：优先使用配音目标时长。
        if target <= 0 and self.options.production_mode == "ai_voiceover":
            target = source_duration

        # 非 AI 配音或旧产物：保留旧行为，兼容 duration_seconds。
        if target <= 0:
            target = source_duration

        min_d = self._safe_positive_float(shot.get("min_duration_seconds"), default=0.0)
        max_d = self._safe_positive_float(shot.get("max_duration_seconds"), default=0.0)

        if target > 0:
            if min_d <= 0:
                min_d = round(target * 0.65, 3)
            if max_d <= 0:
                max_d = round(target * 1.35, 3)

        if max_d > 0 and min_d > max_d:
            min_d = round(max_d * 0.75, 3)
        if target > 0:
            if min_d > target:
                min_d = round(target * 0.75, 3)
            if max_d < target:
                max_d = round(target * 1.25, 3)

        return target, min_d, max_d

    def _time_value_seconds(self, value: Any) -> float | None:
        if value in (None, ""):
            return None

        if isinstance(value, (int, float)):
            number = float(value)
            return number if math.isfinite(number) else None

        text = str(value).strip()
        if not text:
            return None

        try:
            number = float(text)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            pass

        if ":" in text:
            try:
                seconds = float(timecode_to_seconds(text))
                return seconds if math.isfinite(seconds) else None
            except Exception:
                return None

        return None

    def _timecode_field_seconds(self, seg: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            value = seg.get(key)
            if value in (None, ""):
                continue
            try:
                seconds = timecode_to_seconds(value)
            except Exception:
                continue
            if seconds > 0 or str(value).strip() in {"0", "0.0", "00:00:00", "00:00:00.000"}:
                return float(seconds)
        return None

    def _segment_start_end_seconds(self, seg: dict[str, Any]) -> tuple[float | None, float | None]:
        start = self._time_value_seconds(seg.get("source_start_seconds"))
        if start is None:
            start = self._time_value_seconds(seg.get("local_start_seconds"))
        if start is None:
            start = self._time_value_seconds(seg.get("start_seconds"))
        if start is None:
            start = self._time_value_seconds(seg.get("source_start"))
        if start is None:
            start = self._time_value_seconds(seg.get("local_start"))
        if start is None:
            start = self._time_value_seconds(seg.get("start"))

        end = self._time_value_seconds(seg.get("source_end_seconds"))
        if end is None:
            end = self._time_value_seconds(seg.get("local_end_seconds"))
        if end is None:
            end = self._time_value_seconds(seg.get("end_seconds"))
        if end is None:
            end = self._time_value_seconds(seg.get("source_end"))
        if end is None:
            end = self._time_value_seconds(seg.get("local_end"))
        if end is None:
            end = self._time_value_seconds(seg.get("end"))

        duration = self._time_value_seconds(seg.get("duration_seconds"))

        if start is not None and (end is None or end <= start) and duration is not None and duration > 0:
            end = start + duration

        if end is not None and start is None and duration is not None and duration > 0:
            start = max(0.0, end - duration)

        return start, end

    def _resolve_voiceover_target_duration(self, visual_total: float, plan: dict[str, Any] | None = None) -> float:
        plan = plan or {}
        requested_target = plan.get("requested_target_duration_seconds") or plan.get("target_duration_seconds")
        max_allowed = plan.get("max_allowed_seconds") or getattr(self.options, "max_output_video_seconds", None)

        return self._resolve_ai_voiceover_effective_target_seconds(
            visual_total_seconds=visual_total,
            requested_target_seconds=requested_target,
            max_allowed_seconds=max_allowed,
        )

    def _assign_shot_duration_budget(self, editing_structure: list[dict[str, Any]], target_duration: float) -> list[dict[str, Any]]:
        if not editing_structure:
            return []
        items: list[dict[str, Any]] = []
        total_available = 0.0
        for seg in editing_structure:
            item = dict(seg)
            start, end = self._segment_start_end_seconds(item)
            available = max(0.0, float(end - start)) if start is not None and end is not None else 0.0
            if start is not None:
                item["source_start_seconds"] = round(float(start), 3)
                item.setdefault("source_start", seconds_to_timecode(float(start), ms=True))
            if end is not None:
                item["source_end_seconds"] = round(float(end), 3)
                item.setdefault("source_end", seconds_to_timecode(float(end), ms=True))
            item["available_duration_seconds"] = round(available, 3)
            items.append(item)
            total_available += available
        if total_available <= 0:
            return items
        target_duration = max(0.0, float(target_duration or 0.0)) or total_available
        if abs(target_duration - total_available) <= 0.5:
            for item in items:
                available = float(item.get("available_duration_seconds") or 0.0)
                item["target_duration_seconds"] = round(available, 3)
                item["min_duration_seconds"] = round(max(1.0, available * 0.65), 3)
                item["max_duration_seconds"] = round(available * 1.35, 3)
                item.setdefault("allow_trim", True)
                item.setdefault("allow_extend", True)
            return items

        for item in items:
            available = float(item.get("available_duration_seconds") or 0.0)
            if available <= 0:
                continue
            shot_target = min(available, max(3.0, target_duration * (available / total_available)))
            min_d = min(available, max(1.0, shot_target * 0.65))
            max_d = min(available, max(min_d, shot_target * 1.35))
            item["target_duration_seconds"] = round(shot_target, 3)
            item["min_duration_seconds"] = round(min_d, 3)
            item["max_duration_seconds"] = round(max_d, 3)
            item.setdefault("allow_trim", True)
            item.setdefault("allow_extend", True)
        return items

    def _attach_voiceover_char_budget(self, editing_structure: list[dict[str, Any]]) -> list[dict[str, Any]]:
        voice_cfg = self.config.raw.get("voiceover", {}) if hasattr(self, "config") else {}
        cps = float(voice_cfg.get("voiceover_chars_per_second") or voice_cfg.get("chars_per_second") or 4.8)
        min_cps = float(voice_cfg.get("voiceover_min_chars_per_second") or 3.8)
        max_cps = float(voice_cfg.get("voiceover_max_chars_per_second") or 5.8)
        global_min = int(voice_cfg.get("shot_min_text_chars") or 10)
        global_max = int(voice_cfg.get("shot_max_text_chars") or 90)
        result: list[dict[str, Any]] = []
        for seg in editing_structure:
            item = dict(seg)
            target = float(item.get("target_duration_seconds") or item.get("duration_seconds") or 5.0)
            min_chars = max(global_min, int(target * min_cps))
            target_chars = max(global_min, int(target * cps))
            max_chars = min(global_max, max(min_chars + 4, int(target * max_cps)))
            item["narration_min_chars"] = min_chars
            item["narration_target_chars"] = target_chars
            item["narration_max_chars"] = max_chars
            result.append(item)
        return result

    def _materialize_highlight_reassembly_plan(self, result: Any) -> dict[str, Any]:
        if isinstance(result, list):
            raw_videos = result
            base_result: dict[str, Any] = {}
        elif isinstance(result, dict):
            raw_videos = result.get("videos")
            if not isinstance(raw_videos, list):
                raw_videos = result.get("output_videos") if isinstance(result.get("output_videos"), list) else []
            base_result = dict(result)
        else:
            raw_videos = []
            base_result = {}
        clips_by_id = self._candidate_clips_by_id()
        diagnostics = {
            "model_output_empty": not bool(raw_videos),
            "fallback_used": False,
            "candidate_clip_count": len(clips_by_id),
            "invalid_time_clips": [],
        }
        if not raw_videos and clips_by_id:
            raw_videos = [{
                "clip_ids": list(clips_by_id)[: self.options.reassembly_max_clip_count],
                "fallback_reason": "model returned empty plan; selected top filtered clips",
            }]
            diagnostics["fallback_used"] = True

        output_videos: list[dict[str, Any]] = []
        used_clip_ids: set[str] = set()
        for video in raw_videos:
            if not isinstance(video, dict):
                continue
            clip_ids = self._coerce_id_list(video.get("clip_ids") or video.get("source_clip_ids"))
            if not clip_ids and isinstance(video.get("selected_clips"), list):
                clip_ids = self._coerce_id_list([
                    item.get("source_clip_id") or item.get("clip_id")
                    for item in video.get("selected_clips", [])
                    if isinstance(item, dict)
                ])
            selected = [clips_by_id[cid] for cid in clip_ids if cid in clips_by_id]
            if not selected:
                continue
            rid = f"hr_{len(output_videos) + 1:03d}"
            selected_clips: list[dict[str, Any]] = []
            for clip in selected:
                source_clip_id = self._clip_id(clip, len(selected_clips))
                try:
                    clip = self._normalize_clip_time_fields(clip)
                    start, end = self._clip_start_end(clip)
                except Exception as exc:
                    diagnostics["invalid_time_clips"].append({
                        "clip_id": source_clip_id,
                        "error": str(exc),
                        "raw_time_fields": self._clip_time_debug_fields(clip),
                    })
                    continue

                local_start = seconds_to_timecode(start, ms=True)
                local_end = seconds_to_timecode(end, ms=True)
                used_clip_ids.add(source_clip_id)
                selected_clips.append({
                    "clip_id": source_clip_id,
                    "source_clip_id": source_clip_id,
                    "source_id": clip.get("source_id", ""),
                    "local_start": local_start,
                    "local_end": local_end,
                    "local_start_seconds": round(start, 3),
                    "local_end_seconds": round(end, 3),
                    "source_start": local_start,
                    "source_end": local_end,
                    "source_start_seconds": round(start, 3),
                    "source_end_seconds": round(end, 3),
                    "duration_seconds": round(max(0.0, end - start), 3),
                    "role": clip.get("type") or clip.get("clip_type") or "",
                    "transition_after": "hard_cut",
                })
            
            if not selected_clips:
                continue

            output_videos.append({
                "reassembly_id": rid,
                "title": "",
                "selected_clips": selected_clips,
                "excluded_clip_ids": [cid for cid in clips_by_id if cid not in used_clip_ids],
                **({"fallback_reason": video.get("fallback_reason")} if video.get("fallback_reason") else {}),
            })

        if not output_videos:
            raise UserFacingPipelineError(
                "highlight_reassembly_no_valid_clips",
                user_message="重组失败：候选片段时间字段无效，无法生成可信剪辑方案。",
                suggestions=[
                    "请重跑 asr_event_candidate 和 candidate_filter。",
                    "检查 candidate_clip_pool_filtered.json 里的 *_seconds 字段是否是数字。",
                    "确认时间码字符串只出现在 source_start/source_end/local_start/local_end/start/end 字段。",
                ],
                technical_detail={
                    "candidate_clip_count": len(clips_by_id),
                    "invalid_time_clips": diagnostics.get("invalid_time_clips", []),
                },
            )

        return {
            "version": "highlight_reassembly_materialized_v2",
            "output_videos": output_videos,
            "diagnostics": diagnostics,
            "materialized_by_code": True,
            "raw_model_plan": result,
            **({"model_fields": {k: v for k, v in base_result.items() if k not in {"videos", "output_videos"}}} if base_result else {}),
        }

    def _write_compat_agent_output(self, step: str, output: dict[str, Any], source_step: str) -> None:
        base, out_name, _prompt, prompt_version = AGENT_INFO[step]
        input_hash = stable_hash({"compat_source_step": source_step, "compat_source_version": self._step_content_hash(source_step), "output": output})
        version, vdir = self._version_dir(step, base)
        ensure_dir(vdir)
        write_json(vdir / "model_output.json", output)
        write_json(vdir / "final_output.json", output)
        out = write_json(vdir / out_name, output)
        status = self._base_status(step, version, input_hash, [out])
        status.update({"compat_source_step": source_step, "prompt_version": prompt_version, "compat_output": True})
        self._write_status(vdir, status)
        self._record_step(
            step=step,
            version=version,
            status="success",
            output=relpath(out, self.task_dir),
            input_hash=input_hash,
            output_files=[out],
            extra={"compat_source_step": source_step, "prompt_version": prompt_version, "compat_output": True},
            mark_downstream_stale=False,
        )

    def _write_compat_content_analysis(self, result: dict[str, Any]) -> None:
        video_analysis = {
            "main_topic": result.get("main_topic", ""),
            "video_type": result.get("video_news_type", ""),
            "summary": result.get("summary", ""),
            "key_facts": result.get("key_facts", []),
            "people": result.get("people", []),
            "locations": result.get("locations", []),
            "content_structure": result.get("content_structure", []),
            "potential_angles": result.get("potential_angles", []),
        }
        candidate_clips = {
            "video_news_type": result.get("video_news_type", ""),
            "candidate_clips": result.get("candidate_clips", []),
        }
        self._write_compat_agent_output("video_understanding", video_analysis, "content_analysis")
        self._write_compat_agent_output("highlight_detection", candidate_clips, "content_analysis")

    def _write_compat_short_video_plan_and_editing_script(self, edit_plan: dict[str, Any]) -> None:
        scripts = edit_plan.get("scripts", [])
        short_video_plan = {
            "recommended_video_count": edit_plan.get("recommended_video_count", len(scripts)),
            "overall_reason": edit_plan.get("overall_reason", ""),
            "short_videos": [],
            "discarded_clips": edit_plan.get("discarded_clips", []),
        }
        editing_script = {"scripts": []}
        for script in scripts:
            short_video_plan["short_videos"].append({
                "short_video_id": script.get("short_video_id", ""),
                "topic": script.get("topic", ""),
                "news_angle": script.get("news_angle", ""),
                "video_type": script.get("video_type", "解说型"),
                "target_duration_seconds": script.get("target_duration_seconds", 30),
                "max_allowed_seconds": script.get("max_allowed_seconds", 35),
                "source_clip_ids": script.get("source_clip_ids", []),
                "must_keep_fact_points": script.get("must_keep_fact_points", []),
                "visual_selection_strategy": "generated by short_video_edit_plan",
                "audio_strategy": "ai_voiceover_main",
                "priority": script.get("priority", "A"),
                "reason": script.get("voiceover_brief", ""),
                "required_context": "",
            })
            editing_structure = []
            for idx, shot in enumerate(script.get("editing_structure", [])):
                source_duration = self._safe_positive_float(shot.get("duration_seconds"), default=0.0)
                target_duration = self._safe_positive_float(
                    shot.get("target_duration_seconds"),
                    default=source_duration,
                )
                min_duration = self._safe_positive_float(
                    shot.get("min_duration_seconds"),
                    default=0.0,
                )
                max_duration = self._safe_positive_float(
                    shot.get("max_duration_seconds"),
                    default=0.0,
                )

                # 兜底：如果没有 min/max，按 target 给一个合理范围，不再只依赖原始画面 duration。
                if target_duration > 0:
                    if min_duration <= 0:
                        min_duration = round(target_duration * 0.65, 3)
                    if max_duration <= 0:
                        max_duration = round(target_duration * 1.35, 3)

                item = {
                    "order": shot.get("order", idx + 1),
                    "shot_id": shot.get("shot_id", f"{script.get('short_video_id', 'sv')}_s{idx + 1:02d}"),
                    "target_timeline": shot.get("target_timeline", ""),
                    "source_clip_id": shot.get("source_clip_id", ""),
                    "source_id": shot.get("source_id", ""),
                    "source_index": shot.get("source_index", ""),
                    "source_start": shot.get("source_start", ""),
                    "source_end": shot.get("source_end", ""),
                    "source_start_seconds": shot.get("source_start_seconds"),
                    "source_end_seconds": shot.get("source_end_seconds"),
                    "local_start": shot.get("local_start", shot.get("source_start", "")),
                    "local_end": shot.get("local_end", shot.get("source_end", "")),
                    "local_start_seconds": shot.get("local_start_seconds", shot.get("source_start_seconds")),
                    "local_end_seconds": shot.get("local_end_seconds", shot.get("source_end_seconds")),

                    # 原始画面长度仍保留，供 cut_plan/render 使用。
                    "duration_seconds": source_duration,

                    # AI 配音目标长度必须保留，供 voiceover/TTS 校验使用。
                    "target_duration_seconds": target_duration,
                    "min_duration_seconds": min_duration,
                    "max_duration_seconds": max_duration,

                    # 文案预算必须保留，供 voiceover_script 和校验使用。
                    "narration_min_chars": shot.get("narration_min_chars"),
                    "narration_target_chars": shot.get("narration_target_chars"),
                    "narration_max_chars": shot.get("narration_max_chars"),

                    "purpose": shot.get("purpose", ""),
                    "visual": shot.get("visual", ""),
                    "visual_evidence_type": "",
                    "visual_selection_reason": "",
                    "importance": "must_keep",
                    "audio_mode": "ai_voiceover",
                    "original_audio_required": False,
                    "subtitle": shot.get("subtitle_hint", ""),
                    "editing_note": shot.get("news_fact_to_explain", ""),
                    "news_fact_to_explain": shot.get("news_fact_to_explain", ""),
                    "must_say_facts": shot.get("must_say_facts", []),
                }

                editing_structure.append(self._normalize_voiceover_budget_fields(item))
            editing_script["scripts"].append({
                "short_video_id": script.get("short_video_id", ""),
                "title": script.get("title", ""),
                "cover_text": script.get("cover_text", ""),
                "target_duration_seconds": script.get("target_duration_seconds", 30),
                "max_allowed_seconds": script.get("max_allowed_seconds", 35),
                "estimated_total_duration_seconds": sum(float(s.get("duration_seconds") or 0) for s in editing_structure),
                "duration_reason": script.get("voiceover_brief", ""),
                "editing_structure": editing_structure,
                "need_voiceover": True,
                "subtitle_keywords": script.get("subtitle_keywords", []),
            })
        self._write_compat_agent_output("short_video_planning", short_video_plan, "short_video_edit_plan")
        self._write_compat_agent_output("editing_script", editing_script, "short_video_edit_plan")

    def _normalize_short_video_split_decision(self, edit_plan: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(edit_plan, dict):
            return edit_plan
        scripts = [x for x in edit_plan.get("scripts", []) if isinstance(x, dict)]
        if len(scripts) <= 1:
            return edit_plan
        options = getattr(self, "options", None)
        output_mode = getattr(options, "output_mode", "single")
        if output_mode == "multiple":
            normalized = dict(edit_plan)
            max_count = max(2, min(int(getattr(options, "max_output_videos", 5) or 5), 5))
            normalized["scripts"] = scripts[:max_count]
            normalized["recommended_video_count"] = len(normalized["scripts"])
            normalized["auto_merge_applied"] = False
            normalized["output_mode_respected"] = "multiple"
            return normalized
        if not self._should_merge_short_video_scripts(scripts):
            return edit_plan

        normalized = dict(edit_plan)
        normalized["recommended_video_count"] = 1
        normalized["overall_reason"] = (
            "system post-process: multiple planned short videos appear to share one news chain "
            "or include fragments too short for a complete AI voiceover story, so they were merged."
        )
        normalized["scripts"] = [self._merge_short_video_scripts(scripts)]
        normalized["discarded_clips"] = edit_plan.get("discarded_clips", [])
        normalized["auto_merge_applied"] = True
        normalized["auto_merge_reason"] = "same_news_chain_or_too_short"
        return normalized

    def _should_merge_short_video_scripts(self, scripts: list[dict[str, Any]]) -> bool:
        if len(scripts) <= 1:
            return False

        durations = [
            float(script.get("target_duration_seconds") or 0)
            for script in scripts
            if isinstance(script, dict)
        ]
        total_target = sum(durations)
        if any(0 < duration < 30 for duration in durations):
            return True

        merge_duration_ok = total_target <= 120
        all_text = "\n".join(
            "\n".join(
                [
                    str(script.get("topic", "")),
                    str(script.get("news_angle", "")),
                    "\n".join(map(str, script.get("must_keep_fact_points", []) or [])),
                    "\n".join(map(str, script.get("source_clip_ids", []) or [])),
                ]
            )
            for script in scripts
        )

        same_chain_keywords = [
            "谈判", "停火", "冲突", "核问题", "制裁", "白宫", "特朗普",
            "伊朗", "美国", "美方", "伊方", "外交", "协议", "分歧", "政治考量",
            "negotiation", "ceasefire", "conflict", "nuclear", "sanction",
            "white house", "trump", "iran", "us", "diplomacy", "agreement",
        ]
        keyword_hits = sum(1 for keyword in same_chain_keywords if keyword.lower() in all_text.lower())

        shared_entity_groups = [
            ["伊朗", "美伊", "美国", "特朗普"],
            ["俄乌", "俄罗斯", "乌克兰"],
            ["中美", "中国", "美国"],
            ["巴以", "以色列", "哈马斯", "加沙"],
            ["iran", "us", "trump"],
            ["russia", "ukraine"],
            ["china", "us"],
            ["israel", "hamas", "gaza"],
        ]
        has_shared_entity_group = any(
            sum(1 for keyword in group if keyword.lower() in all_text.lower()) >= 2
            for group in shared_entity_groups
        )

        chain_structure_words = [
            "背景", "现状", "表态", "核心", "分歧", "原因", "动机", "风险", "后续",
            "background", "status", "statement", "core", "dispute", "reason", "motive", "risk",
        ]
        structure_hits = sum(1 for keyword in chain_structure_words if keyword.lower() in all_text.lower())

        if merge_duration_ok and has_shared_entity_group and keyword_hits >= 3:
            return True
        if merge_duration_ok and has_shared_entity_group and keyword_hits >= 4 and structure_hits >= 2:
            return True
        return False

    def _merge_short_video_scripts(self, scripts: list[dict[str, Any]]) -> dict[str, Any]:
        first = scripts[0]
        source_clip_ids: list[str] = []
        fact_points: list[str] = []
        editing_structure: list[dict[str, Any]] = []
        subtitle_keywords: list[Any] = []

        for script in scripts:
            for clip_id in script.get("source_clip_ids", []) or []:
                if clip_id and clip_id not in source_clip_ids:
                    source_clip_ids.append(clip_id)
            for fact in script.get("must_keep_fact_points", []) or []:
                fact_text = str(fact).strip()
                if fact_text and fact_text not in fact_points:
                    fact_points.append(fact_text)
            for shot in script.get("editing_structure", []) or []:
                if isinstance(shot, dict):
                    editing_structure.append(dict(shot))
            for keyword in script.get("subtitle_keywords", []) or []:
                if keyword not in subtitle_keywords:
                    subtitle_keywords.append(keyword)

        normalized_structure: list[dict[str, Any]] = []
        for index, shot in enumerate(editing_structure, start=1):
            item = dict(shot)
            item["order"] = index
            item["shot_id"] = f"v_001_s{index:02d}"
            normalized_structure.append(item)

        total_target = sum(float(script.get("target_duration_seconds") or 0) for script in scripts)
        target_duration = min(max(total_target, 45), 60)
        max_allowed = min(max(target_duration + 5, 50), 75)
        topics = [str(script.get("topic", "")).strip() for script in scripts if script.get("topic")]
        angles = [str(script.get("news_angle", "")).strip() for script in scripts if script.get("news_angle")]

        return {
            "short_video_id": "v_001",
            "topic": topics[0] if topics else str(first.get("topic", "")),
            "news_angle": "；".join(angles[:3]) if angles else str(first.get("news_angle", "")),
            "video_type": first.get("video_type", "解说型"),
            "title": first.get("title", ""),
            "cover_text": first.get("cover_text", ""),
            "target_duration_seconds": round(target_duration, 1),
            "max_allowed_seconds": round(max_allowed, 1),
            "source_clip_ids": source_clip_ids,
            "must_keep_fact_points": fact_points,
            "split_reason": "",
            "merge_with_other_clips_reason": "multiple planned clips belong to one news chain and were merged into one complete AI voiceover video",
            "voiceover_brief": (
                "Write one complete AI news explainer: open with the background and current state, "
                "then explain key statements and core disputes, and close with motives, risks, or next steps."
            ),
            "subtitle_keywords": subtitle_keywords,
            "editing_structure": normalized_structure,
        }

    def _text_model_name(self) -> str:
        llm_cfg = self.config.llm
        provider = llm_cfg.get("text_llm_provider", "openai")
        return str(llm_cfg.get("text_llm_model_name") or llm_cfg.get(f"text_{provider}_model_name") or "")

    def _text_fallback_models(self) -> list[str]:
        llm_cfg = self.config.llm
        provider = llm_cfg.get("text_llm_provider", "openai")
        return list(llm_cfg.get(f"text_{provider}_fallback_models", []) or [])

    def _format_chunk_ts(self, chunk: dict[str, Any]) -> str:
        if chunk.get("time_range"):
            return str(chunk.get("time_range"))
        return local_time_range(chunk)

    def _clip_id(self, clip: dict[str, Any], index: int = 0) -> str:
        return str(clip.get("clip_id") or clip.get("id") or clip.get("source_clip_id") or f"clip_{index + 1:03d}")

    def _clip_start_end(self, clip: dict[str, Any]) -> tuple[float, float]:
        start, end = self._segment_start_end_seconds(clip)
        clip_id = clip.get("clip_id") or clip.get("id") or clip.get("source_clip_id") or ""

        if start is None or end is None:
            raise ValueError(
                f"invalid clip time fields: clip_id={clip_id}, "
                f"times={self._clip_time_debug_fields(clip)}"
            )

        if end <= start:
            raise ValueError(
                f"invalid clip time range: clip_id={clip_id}, start={start}, end={end}, "
                f"times={self._clip_time_debug_fields(clip)}"
            )

        return float(start), float(end)

    def _clip_time_debug_fields(self, clip: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "local_start_seconds",
            "local_end_seconds",
            "source_start_seconds",
            "source_end_seconds",
            "start_seconds",
            "end_seconds",
            "local_start",
            "local_end",
            "source_start",
            "source_end",
            "start",
            "end",
            "duration_seconds",
        ]
        return {key: clip.get(key) for key in keys if key in clip}

    def _format_clip_time(self, clip: dict[str, Any]) -> str:
        if clip.get("local_start") and clip.get("local_end"):
            return f"{clip.get('local_start')}-{clip.get('local_end')}"
        return local_time_range(clip)

    def _compact_clip_score(self, clip: dict[str, Any]) -> dict[str, Any]:
        keys = ["news_value_score", "timeliness_score", "information_density_score", "visual_score", "visual_evidence_score", "independence_score", "hook_score"]
        return {key: clip.get(key) for key in keys if key in clip}

    def _candidate_source_hash(self) -> str:
        if self.options.production_mode == "highlight_reassembly":
            return self._step_content_hash("candidate_filter") or self._step_content_hash("asr_event_candidate")
        return self._step_content_hash("ai_voiceover_candidate_materialize")

    def _load_candidate_clips_for_current_mode(self) -> list[dict[str, Any]]:
        if self.options.production_mode == "highlight_reassembly":
            filtered = self._load_candidate_clip_pool(filtered=True)
            if filtered:
                return filtered
            pool = self._load_candidate_clip_pool(filtered=False)
            if pool:
                return pool
        if self.options.production_mode == "ai_voiceover":
            return self._load_ai_voiceover_candidate_clips_or_raise()
        source_step = "highlight_detection" if self.options.production_mode == "highlight_reassembly" else "ai_voiceover_candidate_materialize"
        doc = self._load_optional_step_json(source_step, {})
        clips = doc.get("candidate_clips", [])
        if isinstance(clips, dict):
            clips = clips.get("candidate_clips", [])
        return [clip for clip in clips if isinstance(clip, dict)]

    def _load_ai_voiceover_candidate_clips_or_raise(self) -> list[dict[str, Any]]:
        doc = self._load_optional_step_json("ai_voiceover_candidate_materialize", {})
        clips = doc.get("candidate_clips", []) if isinstance(doc, dict) else []
        if isinstance(clips, dict):
            clips = clips.get("candidate_clips", [])
        clips = [clip for clip in clips if isinstance(clip, dict)]
        if not clips:
            raise UserFacingPipelineError(
                "ai_voiceover_candidate_clips_missing",
                user_message="AI 配音候选片段缺失：short_video_edit_plan 只能读取 ai_voiceover_candidate_materialize 输出。",
                suggestions=["回查 ai_voiceover_candidates/v*/candidate_clips.json。", "从 content_analysis 重新运行。"],
            )
        return clips
        

    def _candidate_refine_by_clip_id(self) -> dict[str, dict[str, Any]]:
        doc = self._load_optional_step_json("candidate_refine", {})
        return {str(item.get("clip_id")): item for item in doc.get("clips", []) if isinstance(item, dict) and item.get("clip_id")}

    def _source_boundaries(self) -> list[dict[str, Any]]:
        boundaries: list[dict[str, Any]] = []
        for item in self.manifest.get("source_videos", []) or []:
            if isinstance(item, dict):
                duration = float(item.get("duration_seconds") or item.get("original_duration_seconds") or 0)
                boundaries.append({
                    "source_id": item.get("source_id", ""),
                    "local_start": 0.0,
                    "local_end": duration,
                })
        if boundaries:
            return boundaries
        metadata = self._load_optional_step_json("metadata", {})
        duration = float(metadata.get("duration") or 0)
        return [{"source_id": "source_1", "start": 0.0, "end": duration}] if duration else []

    def _build_highlight_reassembly_plan_text(self) -> str:
        llm_cfg = self.config.raw.get("llm_input", {})
        max_clips = int(llm_cfg.get("max_llm_candidate_clips", llm_cfg.get("max_candidate_clips_for_edit_plan", 14)) or 14)
        video = self._load_optional_step_json("video_understanding", {})
        clips = self._load_candidate_clips_for_current_mode()[:max_clips]
        if not clips:
            raise UserFacingPipelineError(
                "highlight_reassembly_candidate_clips_missing",
                user_message="视频重组候选片段缺失：candidate_filter / asr_event_candidate 没有生成可用原声片段。",
                suggestions=[
                    "回查 agents/candidate_filter/v*/candidate_filter.json。",
                    "回查 agents/asr_event_candidate/v*/asr_event_candidate.json。",
                    "确认当前任务 production_mode 为 highlight_reassembly。",
                ],
                technical_detail={
                    "production_mode": self.options.production_mode,
                    "candidate_filter": self.manifest.get("steps", {}).get("candidate_filter", {}),
                    "asr_event_candidate": self.manifest.get("steps", {}).get("asr_event_candidate", {}),
                },
            )
        lines = [
            "任务：请规划原声高光重组视频。",
            "规则：不同 source 是独立素材池，不代表连续时间线；只选择并排序 clip_id，不要输出任何时间戳。",
            "",
            "运行参数：",
            f"- target_seconds：{self.options.reassembly_target_seconds}",
            f"- max_clip_count：{self.options.reassembly_max_clip_count}",
            f"- output_mode：{self.options.reassembly_output_mode}",
            "",
            "视频理解：",
            f"主题：{video.get('main_topic') or video.get('topic') or ''}",
            f"摘要：{video.get('summary') or video.get('understanding') or ''}",
            "",
            "候选 clips：",
        ]
        for index, clip in enumerate(clips):
            cid = self._clip_id(clip, index)
            lines.append(f"[{cid}] time={self._format_clip_time(clip)} duration={clip.get('duration_seconds', '')} source_id={clip.get('source_id', 'source_1')} type={clip.get('type') or clip.get('clip_type') or ''}")
            lines.append(f"摘要：{compact_text(str(clip.get('summary') or ''), max_chars=400)}")
            lines.append(f"声音：{sanitize_llm_text(str(clip.get('speech') or clip.get('asr_text') or clip.get('original_audio_transcript_summary') or ''), max_chars=400)}")
            lines.append(f"画面：{compact_text(str(clip.get('visual') or clip.get('visual_summary') or clip.get('why_this_visual_matters') or ''), max_chars=400)}")
            lines.append("")
        lines.append("")
        lines.append('输出 JSON 只允许包含 clip_id，例如 {"output_videos":[{"reassembly_id":"hr_001","title":"","clip_ids":["source_001_clip_0001"]}]}')
        return "\n".join(lines)

    def _configured_ai_voiceover_max_seconds(self) -> float:
        short_cfg = self.config.raw.get("short_video", {}) if hasattr(self, "config") else {}
        values = [
            short_cfg.get("ai_voiceover_max_output_seconds"),
            short_cfg.get("max_long_video_seconds"),
            getattr(self.options, "max_output_video_seconds", None),
        ]
        for value in values:
            try:
                number = float(value or 0)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number > 0:
                return max(30.0, min(number, 180.0))
        return 180.0

    def _effective_ai_voiceover_max_seconds(self) -> float:
        if self.options.production_mode != "ai_voiceover":
            return float(self.options.max_output_video_seconds or self.duration_settings.normal_max_seconds)
        return self._configured_ai_voiceover_max_seconds()

    def _build_short_video_edit_plan_text(self) -> str:
        llm_cfg = self.config.raw.get("llm_input", {})
        max_clips = int(llm_cfg.get("max_llm_candidate_clips", llm_cfg.get("max_candidate_clips_for_edit_plan", 14)) or 14)
        content = self._load_optional_step_json("content_analysis", {})
        clips = self._load_candidate_clips_for_current_mode()[:max_clips]
        effective_max = self._effective_ai_voiceover_max_seconds()
        lines = [
            "任务：请生成 AI 配音短视频选片与讲述规划。",
            "",
            "运行参数：",
            f"- output_mode：{self.options.output_mode}",
            f"- max_output_videos：{self.options.max_output_videos}",
            f"- min_output_video_seconds：{self.options.min_output_video_seconds}",
            f"- target_duration_seconds：{self.options.target_duration_seconds}",
            f"- effective_max_output_video_seconds：{effective_max}",
            f"- raw_max_output_video_seconds：{self.options.max_output_video_seconds}",
            f"- allow_long_video：{self.options.allow_long_video}",
            "- 重要：AI 配音视频优先接近 target_duration_seconds；除非明确允许长版，否则不要贴着 max_output_video_seconds 输出。",
            "target_duration_seconds 是推荐目标，不是硬上限。",
            "如果新闻信息量较大，可以超过 target_duration_seconds，但必须控制在 effective_max_output_video_seconds 以内。",
            "AI 配音模式下，允许 60-180 秒长版解说；长版必须保证每个 shot 都有足够 narration_intent，便于后续 voiceover_script 生成足够长文案。",
            "",
            "新闻概览：",
            f"主题：{content.get('main_topic') or content.get('topic') or ''}",
            f"摘要：{content.get('summary') or ''}",
            "关键事实：",
        ]
        for fact in (content.get("key_facts") or [])[: int(llm_cfg.get("max_llm_key_facts", 8) or 8)]:
            lines.append(f"- {fact if isinstance(fact, str) else json.dumps(fact, ensure_ascii=False)}")
        lines.append("")
        lines.append("候选 clips：")
        for index, clip in enumerate(clips):
            cid = self._clip_id(clip, index)
            lines.append(f"[{cid}] duration={clip.get('duration_seconds', '')} role={clip.get('role', 'fact')}")
            lines.append(f"摘要：{compact_text(str(clip.get('summary') or ''), max_chars=240)}")
            lines.append(f"声音：{sanitize_llm_text(str(clip.get('speech') or clip.get('asr_text') or ''), max_chars=360)}")
            lines.append(f"画面：{compact_text(str(clip.get('visual') or clip.get('visual_context') or ''), max_chars=220)}")
            lines.append("")
        lines.append('输出 JSON：只返回数组，例如 [{"clip_ids":["source_001_clip_0001"]}]。不要输出时间戳，不要输出额外字段。')
        return "\n".join(lines)

    def _build_merge_decision_text(self, edit_plan: dict[str, Any]) -> str:
        lines = ["Task: decide whether planned short videos should be merged.", ""]
        for index, script in enumerate(edit_plan.get("scripts", []) or []):
            if not isinstance(script, dict):
                continue
            sid = str(script.get("short_video_id") or f"v_{index + 1:03d}")
            lines.append(f"[{sid}]")
            lines.append(f"topic: {script.get('topic') or ''}")
            lines.append(f"angle: {script.get('news_angle') or ''}")
            lines.append(f"target: {script.get('target_duration_seconds') or ''}s")
            lines.append("facts: " + "; ".join(map(str, script.get("must_keep_fact_points") or [])))
            lines.append("clips: " + ", ".join(map(str, script.get("source_clip_ids") or [])))
            lines.append("")
        lines.append('Return JSON like {"m":1,"groups":[["v_001","v_002"]]}.')
        return "\n".join(lines)

    def _normalize_merge_decision(self, parsed: Any) -> dict[str, Any]:
        parsed = parsed if isinstance(parsed, dict) else {}
        groups = parsed.get("groups") if isinstance(parsed.get("groups"), list) else []
        clean_groups = [[str(x) for x in group if str(x).strip()] for group in groups if isinstance(group, list)]
        return {"version": "merge_decision_v1", "m": 1 if int(parsed.get("m", 0) or 0) else 0, "groups": clean_groups}

    def _apply_merge_decision_to_edit_plan(self, edit_plan: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        scripts = [x for x in edit_plan.get("scripts", []) if isinstance(x, dict)]
        if len(scripts) <= 1 or int(decision.get("m", 0) or 0) != 1:
            return edit_plan
        by_id = {str(script.get("short_video_id") or f"v_{index + 1:03d}"): script for index, script in enumerate(scripts)}
        groups = decision.get("groups") or [[sid for sid in by_id]]
        consumed: set[str] = set()
        merged_scripts: list[dict[str, Any]] = []
        for group in groups:
            group_scripts = [by_id[sid] for sid in group if sid in by_id]
            if len(group_scripts) >= 2:
                merged_scripts.append(self._merge_short_video_scripts(group_scripts))
                consumed.update(str(script.get("short_video_id") or "") for script in group_scripts)
        for sid, script in by_id.items():
            if sid not in consumed:
                merged_scripts.append(script)
        if not merged_scripts:
            return edit_plan
        normalized = dict(edit_plan)
        normalized["scripts"] = merged_scripts
        normalized["recommended_video_count"] = len(merged_scripts)
        normalized["model_merge_applied"] = True
        return normalized

    def _build_voiceover_script_text_input(self, script: dict[str, Any]) -> str:
        lines = [
            "任务：请为一条短视频写最终 AI 配音文案。",
            "",
            "视频信息：",
            f"- short_video_id：{script.get('short_video_id') or ''}",
            f"- 主题：{script.get('topic') or ''}",
            f"- 讲述重点：{script.get('news_angle') or ''}",
            f"- visual_total_seconds：{script.get('visual_total_seconds') or editing_structure_duration(script.get('editing_structure') or [])}",
            "",
            "必须保留事实：",
        ]
        for fact in script.get("must_keep_fact_points") or []:
            lines.append(f"- {fact}")
        lines.append("")
        lines.append("shots：")
        for shot in script.get("editing_structure") or []:
            if isinstance(shot, dict):
                lines.append(
                    f"- shot_id={shot.get('shot_id') or ''} "
                    f"target={shot.get('target_duration_seconds') or shot.get('duration_seconds') or ''}s "
                    f"chars={shot.get('narration_min_chars') or ''}/{shot.get('narration_target_chars') or ''}/{shot.get('narration_max_chars') or ''} "
                    f"section={shot.get('section') or ''} "
                    f"visual={shot.get('visual_summary') or shot.get('visual') or ''} "
                    f"fact={shot.get('fact') or shot.get('news_fact_to_explain') or shot.get('editing_note') or ''} "
                    f"narration_intent={shot.get('narration_intent') or ''}"
                )
        return "\n".join(lines)

    def _fallback_voiceover_script(self, script: dict[str, Any], short_video_id: str) -> dict[str, Any]:
        facts = [str(x).strip() for x in script.get("must_keep_fact_points") or [] if str(x).strip()]
        shots = [x for x in script.get("editing_structure") or [] if isinstance(x, dict)]
        narration = " ".join(facts or [str(script.get("topic") or script.get("title") or "")]).strip()
        segments = []
        for index, shot in enumerate(shots, start=1):
            text = str(shot.get("fact") or shot.get("news_fact_to_explain") or shot.get("visual") or narration).strip()
            segments.append({"shot_id": shot.get("shot_id") or f"{short_video_id}_s{index:02d}", "text": text})
        return {"short_video_id": short_video_id, "narration_text": narration, "narration_segments": segments}

    def _resolve_script_visual_target_duration(self, edit_script: dict[str, Any], voice_script: dict[str, Any]) -> float:
        editing_structure = edit_script.get("editing_structure", [])
        total_target = sum(float(shot.get("target_duration_seconds", 0) or 0) for shot in editing_structure if isinstance(shot, dict) and shot.get("target_duration_seconds"))
        if total_target > 0 and math.isfinite(total_target):
            return total_target
        
        fallback_vals = [
            editing_structure_duration(editing_structure),
            edit_script.get("estimated_total_duration_seconds"),
            voice_script.get("target_duration_seconds"),
            self.options.target_duration_seconds,
            (self.config.raw.get("voiceover", {}) if hasattr(self, "config") else {}).get("default_target_duration_seconds")
        ]
        for val in fallback_vals:
            try:
                v = float(val or 0)
                if v > 0 and math.isfinite(v):
                    return v
            except (ValueError, TypeError):
                continue
        return 0.0

    def _voiceover_text_chars(self, text: str) -> int:
        clean = clean_voiceover_text(str(text or ""))
        return len(re.sub(r"\s+", "", clean))

    def _attach_voiceover_script_timings(self, voiceover_output: dict[str, Any], edit_plan: dict[str, Any]) -> None:
        scripts = voiceover_output.get("scripts", []) if isinstance(voiceover_output, dict) else []
        edit_scripts = edit_plan.get("scripts", []) if isinstance(edit_plan, dict) else []
        if not isinstance(scripts, list) or not isinstance(edit_scripts, list):
            return
        edit_by_id = {
            script.get("short_video_id"): script
            for script in edit_scripts
            if isinstance(script, dict) and script.get("short_video_id")
        }
        changed = False
        for index, voice_script in enumerate(scripts, start=1):
            if not isinstance(voice_script, dict):
                continue
            sid = voice_script.get("short_video_id") or f"v_{index:03d}"
            edit_script = edit_by_id.get(sid)
            if not edit_script and index <= len(edit_scripts) and isinstance(edit_scripts[index - 1], dict):
                edit_script = edit_scripts[index - 1]
            if edit_script and self._attach_voiceover_segment_timing(voice_script, edit_script):
                changed = True
        if changed:
            out = self.task_dir / self._step_output("voiceover_script")
            write_json(out, voiceover_output)

    def _attach_voiceover_segment_timing(self, voice_script: dict[str, Any], edit_script: dict[str, Any]) -> bool:
        segments = voice_script.get("narration_segments")
        editing_structure = edit_script.get("editing_structure")
        if not isinstance(segments, list) or not isinstance(editing_structure, list):
            return False
        shot_time_map: dict[str, dict[str, Any]] = {}
        ordered_times: list[dict[str, Any]] = []
        cursor = 0.0
        for index, shot in enumerate(editing_structure, start=1):
            if not isinstance(shot, dict):
                continue
            duration = shot.get("target_duration_seconds")
            if duration is None:
                duration = shot.get("duration_seconds")
            if duration is None:
                start = timecode_to_seconds(shot.get("source_start"))
                end = timecode_to_seconds(shot.get("source_end"))
                duration = max(0.0, end - start)
            duration = max(0.0, float(duration or 0))
            if duration <= 0:
                continue
            shot_id = str(shot.get("shot_id") or f"{voice_script.get('short_video_id', 'v')}_s{index:02d}")
            timing = {
                "shot_id": shot_id,
                "target_start": seconds_to_timecode(cursor, ms=True),
                "target_end": seconds_to_timecode(cursor + duration, ms=True),
                "target_start_seconds": round(cursor, 3),
                "target_end_seconds": round(cursor + duration, 3),
                "target_duration_seconds": round(duration, 3),
            }
            shot_time_map[shot_id] = timing
            ordered_times.append(timing)
            cursor += duration
        if not ordered_times:
            return False
        changed = False
        for index, segment in enumerate(segments, start=1):
            if not isinstance(segment, dict):
                continue
            shot_id = str(segment.get("shot_id") or "")
            timing = shot_time_map.get(shot_id)
            if not timing and index <= len(ordered_times):
                timing = ordered_times[index - 1]
                if not shot_id:
                    segment["shot_id"] = timing["shot_id"]
                    changed = True
            if not timing:
                continue
            for key in ("target_start", "target_end", "target_start_seconds", "target_end_seconds", "target_duration_seconds"):
                if segment.get(key) in (None, ""):
                    segment[key] = timing[key]
                    changed = True
        return changed

    def _sync_voiceover_narration_text_from_segments(self, voiceover_output: dict[str, Any]) -> None:
        voice_scripts = voiceover_output.get("scripts", []) if isinstance(voiceover_output, dict) else []
        for script in voice_scripts:
            if not isinstance(script, dict):
                continue
            segments = script.get("narration_segments", [])
            if not isinstance(segments, list):
                continue
            
            clean_texts = []
            for seg in segments:
                if isinstance(seg, dict) and seg.get("text"):
                    clean = clean_voiceover_text(str(seg["text"]))
                    if clean:
                        clean_texts.append(clean)
            
            if clean_texts:
                script["narration_text"] = " ".join(clean_texts)

    def _collect_voiceover_segment_duration_issues(self, voiceover_output: dict[str, Any]) -> list[dict[str, Any]]:
        if self.options.production_mode != "ai_voiceover":
            return []
        voice_cfg = self.config.raw.get("voiceover", {}) if hasattr(self, "config") else {}
        min_ratio = float(voice_cfg.get("pre_tts_segment_min_ratio", 0.65))
        cps = float(voice_cfg.get("voiceover_chars_per_second", 4.8))
        
        issues = []
        scripts = voiceover_output.get("scripts", []) if isinstance(voiceover_output, dict) else []
        for script in scripts:
            if not isinstance(script, dict):
                continue
            sid = str(script.get("short_video_id") or "")
            for seg in script.get("narration_segments", []):
                if not isinstance(seg, dict):
                    continue
                target = float(seg.get("target_duration_seconds") or 0)
                if target <= 0:
                    continue
                text = str(seg.get("text") or "")
                chars = self._voiceover_text_chars(text)
                estimated = chars / max(cps, 1.0)
                required = target * min_ratio
                
                if estimated < required:
                    issues.append({
                        "short_video_id": sid,
                        "shot_id": str(seg.get("shot_id") or ""),
                        "type": "segment_duration_too_short",
                        "source": "pre_tts_segment_duration",
                        "target_duration_seconds": target,
                        "estimated_text_duration_seconds": round(estimated, 2),
                        "required_estimated_seconds": round(required, 2),
                        "chars": chars,
                        "required_chars": int(required * cps),
                        "under_chars": max(0, int(required * cps) - chars),
                        "severity": "hard"
                    })
        return issues

    def _collect_voiceover_script_duration_issues(self, voiceover_output: dict[str, Any], edit_plan: dict[str, Any]) -> list[dict[str, Any]]:
        if self.options.production_mode != "ai_voiceover":
            return []
        script_cfg = self.config.raw.get("voiceover_script", {}) if hasattr(self, "config") else {}
        voice_cfg = self.config.raw.get("voiceover", {}) if hasattr(self, "config") else {}
        soft_min_ratio = float(script_cfg.get("script_estimated_soft_min_ratio", 0.85))
        hard_min_ratio = float(script_cfg.get("script_estimated_hard_min_ratio", 0.65))
        cps = float(voice_cfg.get("voiceover_chars_per_second", 4.8))
        
        issues = []
        voice_scripts = voiceover_output.get("scripts", []) if isinstance(voiceover_output, dict) else []
        edit_scripts = edit_plan.get("scripts", []) if isinstance(edit_plan, dict) else []
        edit_by_id = {str(script.get("short_video_id")): script for script in edit_scripts if isinstance(script, dict) and script.get("short_video_id")}
        
        for index, voice_script in enumerate(voice_scripts):
            if not isinstance(voice_script, dict):
                continue
            sid = str(voice_script.get("short_video_id") or f"v_{index + 1:03d}")
            edit_script = edit_by_id.get(sid)
            if not edit_script and index < len(edit_scripts):
                edit_script = edit_scripts[index]
            if not edit_script:
                continue
                
            target = self._resolve_script_visual_target_duration(edit_script, voice_script)
            if target <= 0:
                continue
                
            text = voice_script.get("narration_text")
            if not text:
                text = " ".join(str(seg.get("text", "")) for seg in voice_script.get("narration_segments", []) if isinstance(seg, dict))
            
            chars = self._voiceover_text_chars(text)
            estimated = chars / max(cps, 1.0)
            ratio = estimated / target if target > 0 else 1.0
            
            severity = None
            if ratio < hard_min_ratio:
                severity = "hard"
            elif ratio < soft_min_ratio:
                severity = "warning"
                
            if severity:
                required = target * soft_min_ratio
                issues.append({
                    "short_video_id": sid,
                    "shot_id": None,
                    "type": "script_duration_too_short",
                    "source": "pre_tts_script_duration",
                    "script_target_duration_seconds": target,
                    "script_estimated_duration_seconds": round(estimated, 2),
                    "script_required_duration_seconds": round(required, 2),
                    "script_missing_chars": max(0, int((required - estimated) * cps)),
                    "severity": severity
                })
        return issues

    def _expand_script_duration_issue_to_segment_issues(
        self,
        script_issues: list[dict[str, Any]],
        voiceover_output: dict[str, Any],
        edit_plan: dict[str, Any],
        existing_segment_issues: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        expanded = []
        voice_scripts = voiceover_output.get("scripts", []) if isinstance(voiceover_output, dict) else []
        voice_by_id = {str(s.get("short_video_id")): s for s in voice_scripts if isinstance(s, dict) and s.get("short_video_id")}
        
        for issue in script_issues:
            sid = issue.get("short_video_id")
            script = voice_by_id.get(sid)
            if not script:
                continue
            segments = [seg for seg in script.get("narration_segments", []) if isinstance(seg, dict)]
            if not segments:
                continue
                
            missing_chars = issue.get("script_missing_chars", 0)
            if missing_chars <= 0:
                continue
                
            # Find segments that already have issues
            seg_issues_for_sid = [si for si in existing_segment_issues if si.get("short_video_id") == sid and si.get("shot_id")]
            target_segments = []
            
            if seg_issues_for_sid:
                target_shot_ids = {si["shot_id"] for si in seg_issues_for_sid}
                target_segments = [seg for seg in segments if str(seg.get("shot_id")) in target_shot_ids]
            
            if not target_segments:
                target_segments = segments
                
            total_target_dur = sum(float(seg.get("target_duration_seconds") or 0) for seg in target_segments)
            if total_target_dur <= 0:
                # Fallback to equal distribution
                chars_per_seg = max(8, missing_chars // len(target_segments))
                for seg in target_segments:
                    expanded.append({
                        "short_video_id": sid,
                        "shot_id": str(seg.get("shot_id") or ""),
                        "type": "script_duration_too_short",
                        "source": "pre_tts_script_duration",
                        "suggested_add_chars": chars_per_seg,
                        "severity": issue.get("severity", "warning")
                    })
            else:
                for seg in target_segments:
                    dur = float(seg.get("target_duration_seconds") or 0)
                    if dur > 0:
                        suggested = max(8, int(missing_chars * (dur / total_target_dur)))
                        expanded.append({
                            "short_video_id": sid,
                            "shot_id": str(seg.get("shot_id") or ""),
                            "type": "script_duration_too_short",
                            "source": "pre_tts_script_duration",
                            "suggested_add_chars": suggested,
                            "severity": issue.get("severity", "warning")
                        })
        return expanded

    def _merge_voiceover_duration_issues_into_alignment(
        self,
        alignment: dict[str, Any],
        segment_issues: list[dict[str, Any]],
        expanded_script_issues: list[dict[str, Any]]
    ) -> dict[str, Any]:
        merged = copy.deepcopy(alignment)
        all_issues = segment_issues + expanded_script_issues
        
        for check in merged.get("checks", []):
            sid = check.get("short_video_id")
            issues_for_sid = [i for i in all_issues if i.get("short_video_id") == sid]
            
            if issues_for_sid:
                if "char_budget_issues" not in check:
                    check["char_budget_issues"] = []
                check["char_budget_issues"].extend(issues_for_sid)
                
                # Check if any of these are 'hard' severity
                has_hard = any(i.get("severity") == "hard" for i in issues_for_sid)
                if has_hard:
                    check["ok"] = False
                    
        merged["ok"] = all(check.get("ok", True) for check in merged.get("checks", []))
        return merged

    def _check_is_text_patch_repairable(self, check: dict[str, Any], *, repair_on_warnings: bool = False) -> bool:
        if check.get("missing_shot_ids") or check.get("extra_shot_ids"):
            return False
        if check.get("char_budget_issues") or check.get("empty_text_shot_ids"):
            return True
        if repair_on_warnings and check.get("char_budget_warnings"):
            return True
        return False

    def _build_voiceover_repair_text_input(
        self,
        *,
        edit_script: dict[str, Any],
        voice_script: dict[str, Any],
        alignment_check: dict[str, Any],
    ) -> str:
        lines = [
            "Task: Repair AI voiceover script to strictly fit character budgets.",
            "",
            f"short_video_id: {edit_script.get('short_video_id') or ''}",
            f"topic: {edit_script.get('topic') or ''}",
            f"news_angle: {edit_script.get('news_angle') or ''}",
            "",
            "must_keep_fact_points:"
        ]
        for fact in edit_script.get("must_keep_fact_points", []):
            lines.append(f"- {fact}")
        lines.append("")
        lines.append("alignment_check:")
        lines.append(json.dumps({
            "missing_shot_ids": alignment_check.get("missing_shot_ids"),
            "extra_shot_ids": alignment_check.get("extra_shot_ids"),
            "empty_text_shot_ids": alignment_check.get("empty_text_shot_ids"),
            "char_budget_issues": alignment_check.get("char_budget_issues"),
        }, ensure_ascii=False, indent=2))
        lines.append("")
        
        problem_segments = []
        shots = {str(shot.get("shot_id")): shot for shot in edit_script.get("editing_structure", []) if isinstance(shot, dict)}
        segments = voice_script.get("narration_segments", [])
        
        issue_map = {}
        issues_by_shot = {}
        for issue in alignment_check.get("char_budget_issues", []) + alignment_check.get("char_budget_warnings", []):
            sid = str(issue.get("shot_id") or "")
            if sid:
                issue_map[sid] = issue.get("type", "unknown")
                if sid not in issues_by_shot:
                    issues_by_shot[sid] = []
                issues_by_shot[sid].append(issue)
        for empty_id in alignment_check.get("empty_text_shot_ids", []):
            issue_map[str(empty_id)] = "empty"
            
        for index, seg in enumerate(segments):
            shot_id = str(seg.get("shot_id"))
            if shot_id not in issue_map:
                continue
            shot = shots.get(shot_id, {})
            prev_text = segments[index-1].get("text", "") if index > 0 else ""
            next_text = segments[index+1].get("text", "") if index < len(segments) - 1 else ""
            
            problem_segments.append({
                "shot_id": shot_id,
                "issue_type": issue_map[shot_id],
                "issues": issues_by_shot.get(shot_id, []),
                "current_chars": len(re.sub(r"\s+", "", str(seg.get("text", "")))),
                "min_chars": self._first_number(shot.get("narration_min_chars")),
                "target_chars": self._first_number(shot.get("narration_target_chars")),
                "max_chars": self._first_number(shot.get("narration_max_chars")),
                "current_text": str(seg.get("text", "")),
                "previous_text": str(prev_text),
                "next_text": str(next_text),
                "section": str(shot.get("section", "")),
                "target_duration_seconds": self._first_number(shot.get("target_duration_seconds")),
                "visual_summary": str(shot.get("visual_summary") or shot.get("visual", "")),
                "fact": str(shot.get("fact") or shot.get("news_fact_to_explain", "")),
                "narration_intent": str(shot.get("narration_intent", "")),
            })
            
        lines.append("problem_segments:")
        lines.append(json.dumps(problem_segments, ensure_ascii=False, indent=2))
        lines.append("")
        lines.append("Output JSON format:")
        lines.append(json.dumps({
            "short_video_id": edit_script.get("short_video_id"),
            "repaired_segments": [
                {"shot_id": "example_shot_id", "new_text": "repaired text goes here"}
            ]
        }, ensure_ascii=False, indent=2))
        return "\n".join(lines)

    def _normalize_voiceover_repair_patch(self, parsed: Any, *, expected_sid: str) -> dict[str, Any]:
        if not isinstance(parsed, dict):
            return {"short_video_id": expected_sid, "repaired_segments": []}
        
        repaired = parsed.get("repaired_segments")
        if not isinstance(repaired, list):
            if isinstance(parsed, list):
                repaired = parsed
            else:
                repaired = []
                
        segments = []
        for seg in repaired:
            if isinstance(seg, dict):
                shot_id = str(seg.get("shot_id") or "")
                new_text = clean_voiceover_text(str(seg.get("new_text") or seg.get("text") or ""))
                if shot_id and new_text:
                    segments.append({"shot_id": shot_id, "new_text": new_text})
                    
        return {
            "short_video_id": str(parsed.get("short_video_id") or expected_sid),
            "repaired_segments": segments
        }

    def _repair_one_voiceover_script_patch(
        self,
        *,
        edit_script: dict[str, Any],
        voice_script: dict[str, Any],
        alignment_check: dict[str, Any],
        model: str,
        fallback: list[str],
        vdir: Path,
        round_index: int,
    ) -> dict[str, Any]:
        sid = str(edit_script.get("short_video_id") or "v_001")
        input_text = self._build_voiceover_repair_text_input(
            edit_script=edit_script,
            voice_script=voice_script,
            alignment_check=alignment_check,
        )
        rdir = vdir / sid / f"repair_round_{round_index}"
        ensure_dir(rdir)
        write_text(rdir / "input.txt", input_text)
        
        cfg = self.config.raw.get("voiceover_script", {})
        temperature = float(cfg.get("repair_temperature", 0.1))
        max_tokens = int(cfg.get("repair_max_tokens", 1000))
        
        if self.llm_text is None:
            raise RuntimeError("missing text llm")
            
        result = self.llm_text.call_json(
            model=model,
            fallback_models=fallback,
            prompt=prompts.VOICEOVER_REPAIR_TEXT_PROMPT,
            input_data=input_text,
            temperature=temperature,
            max_tokens=max_tokens,
            debug_dir=rdir / "_llm_debug",
        )
        parsed = result.parsed if isinstance(result.parsed, dict) else {}
        write_json(rdir / "model_output.json", parsed)
        return self._normalize_voiceover_repair_patch(parsed, expected_sid=sid)

    def _apply_voiceover_repair_patch(
        self,
        voice_script: dict[str, Any],
        patch: dict[str, Any],
    ) -> bool:
        if str(voice_script.get("short_video_id", "")) != str(patch.get("short_video_id", "")):
            return False
            
        repaired = patch.get("repaired_segments", [])
        if not repaired:
            return False
            
        patch_map = {str(seg["shot_id"]): str(seg["new_text"]) for seg in repaired if seg.get("shot_id") and seg.get("new_text")}
        segments = voice_script.get("narration_segments", [])
        applied = False
        
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            shot_id = str(seg.get("shot_id", ""))
            if shot_id in patch_map:
                seg["text"] = patch_map[shot_id]
                applied = True
                
        return applied

    def _finalize_voiceover_script_with_repair(
        self,
        final_output: dict[str, Any],
        edit_plan: dict[str, Any],
        *,
        vdir: Path,
        input_hash: str | None = None,
        model: str,
        fallback: list[str],
    ) -> None:
        self._normalize_voiceover_scripts(final_output)
        self._sync_voiceover_narration_text_from_segments(final_output)
        self._attach_voiceover_script_timings(final_output, edit_plan)
        
        alignment = self._validate_voiceover_script_against_editing_structure(
            final_output,
            edit_plan,
            raise_on_error=False,
        )
        
        segment_issues = self._collect_voiceover_segment_duration_issues(final_output)
        script_issues = self._collect_voiceover_script_duration_issues(final_output, edit_plan)
        expanded_script_issues = self._expand_script_duration_issue_to_segment_issues(script_issues, final_output, edit_plan, segment_issues)
        merged_alignment = self._merge_voiceover_duration_issues_into_alignment(alignment, segment_issues, expanded_script_issues)
        
        cfg = self.config.raw.get("voiceover_script", {})
        repair_enabled = bool(cfg.get("repair_enabled", True))
        max_repair_rounds = int(cfg.get("max_repair_rounds", 2))
        repair_on_warnings = bool(cfg.get("repair_on_warnings", False))
        
        voice_scripts = final_output.get("scripts", []) if isinstance(final_output, dict) else []
        edit_scripts = edit_plan.get("scripts", []) if isinstance(edit_plan, dict) else []
        edit_by_id = {str(script.get("short_video_id")): script for script in edit_scripts if isinstance(script, dict) and script.get("short_video_id")}
        voice_by_id = {str(script.get("short_video_id") or f"v_{index + 1:03d}"): script for index, script in enumerate(voice_scripts) if isinstance(script, dict)}
        
        repair_summary = {
            "enabled": repair_enabled,
            "rounds_used": 0,
            "status": "ok",
            "repair_type": "none",
            "details": []
        }
        
        if not merged_alignment.get("ok") and repair_enabled:
            for round_index in range(1, max_repair_rounds + 1):
                round_applied = False
                for check in merged_alignment.get("checks", []):
                    if check.get("ok"):
                        continue
                    if not self._check_is_text_patch_repairable(check, repair_on_warnings=repair_on_warnings):
                        continue

                    sid = str(check.get("short_video_id"))
                    edit_script = edit_by_id.get(sid)
                    voice_script = voice_by_id.get(sid)
                    if not edit_script or not voice_script:
                        continue
                        
                    patch = self._repair_one_voiceover_script_patch(
                        edit_script=edit_script,
                        voice_script=voice_script,
                        alignment_check=check,
                        model=model,
                        fallback=fallback,
                        vdir=vdir,
                        round_index=round_index,
                    )
                    applied = self._apply_voiceover_repair_patch(voice_script, patch)
                    if applied:
                        round_applied = True
                        repair_summary["details"].append({"round": round_index, "short_video_id": sid, "patch": patch})

                if not round_applied:
                    break
                    
                repair_summary["rounds_used"] = round_index
                repair_summary["status"] = "repaired"
                repair_summary["repair_type"] = "text_patch"

                self._normalize_voiceover_scripts(final_output)
                self._sync_voiceover_narration_text_from_segments(final_output)
                self._attach_voiceover_script_timings(final_output, edit_plan)
                alignment = self._validate_voiceover_script_against_editing_structure(
                    final_output,
                    edit_plan,
                    raise_on_error=False,
                )
                segment_issues = self._collect_voiceover_segment_duration_issues(final_output)
                script_issues = self._collect_voiceover_script_duration_issues(final_output, edit_plan)
                expanded_script_issues = self._expand_script_duration_issue_to_segment_issues(script_issues, final_output, edit_plan, segment_issues)
                merged_alignment = self._merge_voiceover_duration_issues_into_alignment(alignment, segment_issues, expanded_script_issues)
                
                ensure_dir(vdir / f"repair_round_{round_index}")
                write_json(vdir / f"repair_round_{round_index}" / "alignment_after_repair.json", merged_alignment)
                if merged_alignment.get("ok"):
                    break
                    
            write_json(vdir / "voiceover_repair_summary.json", repair_summary)
            # 覆盖原 voiceover_script.json
            write_json(vdir / "voiceover_script.json", final_output)

        if not merged_alignment.get("ok"):
            self._validate_voiceover_script_against_editing_structure(
                final_output,
                edit_plan,
                raise_on_error=True,
            )

        self._validate_voiceover_segment_text_duration_or_raise(final_output)
        self._validate_voiceover_script_duration_or_raise(final_output)
        self._enforce_long_video_confirmation_after_voiceover(final_output)

    def _validate_voiceover_script_against_editing_structure(
        self,
        voiceover_output: dict[str, Any],
        edit_plan: dict[str, Any],
        *,
        raise_on_error: bool = True,
    ) -> dict[str, Any]:
        voice_scripts = voiceover_output.get("scripts", []) if isinstance(voiceover_output, dict) else []
        edit_scripts = edit_plan.get("scripts", []) if isinstance(edit_plan, dict) else []
        edit_by_id = {
            str(script.get("short_video_id")): script
            for script in edit_scripts
            if isinstance(script, dict) and script.get("short_video_id")
        }
        checks: list[dict[str, Any]] = []
        hard_issues: list[str] = []
        for index, voice_script in enumerate(voice_scripts):
            if not isinstance(voice_script, dict):
                continue
            sid = str(voice_script.get("short_video_id") or f"v_{index + 1:03d}")
            edit_script = edit_by_id.get(sid)
            if edit_script is None and index < len(edit_scripts) and isinstance(edit_scripts[index], dict):
                edit_script = edit_scripts[index]
            shots = [shot for shot in (edit_script or {}).get("editing_structure", []) if isinstance(shot, dict)]
            segments = [seg for seg in voice_script.get("narration_segments", []) if isinstance(seg, dict)]
            shot_ids = [str(shot.get("shot_id") or "") for shot in shots if shot.get("shot_id")]
            segment_ids = [str(seg.get("shot_id") or "") for seg in segments if seg.get("shot_id")]
            missing = [shot_id for shot_id in shot_ids if shot_id not in segment_ids]
            extra = [shot_id for shot_id in segment_ids if shot_id not in shot_ids]
            empty_text = [str(seg.get("shot_id") or "") for seg in segments if not str(seg.get("text") or "").strip()]
            voice_cfg = self.config.raw.get("voiceover", {}) if hasattr(self, "config") else {}
            soft_over_chars = int(voice_cfg.get("char_budget_soft_tolerance_chars") or 3)
            soft_over_ratio = float(voice_cfg.get("char_budget_soft_tolerance_ratio") or 0.12)
            hard_over_ratio = float(voice_cfg.get("char_budget_hard_tolerance_ratio") or 0.25)
            soft_under_chars = int(voice_cfg.get("char_budget_under_soft_tolerance_chars") or 3)
            soft_under_ratio = float(voice_cfg.get("char_budget_under_soft_tolerance_ratio") or 0.12)
            hard_under_ratio = float(voice_cfg.get("char_budget_under_hard_tolerance_ratio") or 0.25)

            char_budget_issues: list[dict[str, Any]] = []
            char_budget_warnings: list[dict[str, Any]] = []
            shot_by_id = {str(shot.get("shot_id")): shot for shot in shots if shot.get("shot_id")}
            for seg in segments:
                shot_id = str(seg.get("shot_id") or "")
                shot = shot_by_id.get(shot_id)
                if not shot:
                    continue
                text_chars = len(re.sub(r"\s+", "", str(seg.get("text") or "")))
                min_chars = self._first_number(shot.get("narration_min_chars"))
                max_chars = self._first_number(shot.get("narration_max_chars"))
                
                if max_chars is not None and text_chars > max_chars:
                    over = text_chars - int(max_chars)
                    over_ratio = over / max(float(max_chars), 1.0)
                    item = {
                        "shot_id": shot_id,
                        "chars": text_chars,
                        "max_chars": int(max_chars),
                        "over_chars": over,
                        "over_ratio": round(over_ratio, 3),
                        "type": "too_long",
                    }
                    if over <= soft_over_chars or over_ratio <= soft_over_ratio:
                        item["severity"] = "warning"
                        char_budget_warnings.append(item)
                    elif over_ratio >= hard_over_ratio:
                        item["severity"] = "hard"
                        char_budget_issues.append(item)
                    else:
                        item["severity"] = "warning"
                        char_budget_warnings.append(item)

                if min_chars is not None and text_chars < min_chars:
                    under = int(min_chars) - text_chars
                    under_ratio = under / max(float(min_chars), 1.0)
                    item = {
                        "shot_id": shot_id,
                        "chars": text_chars,
                        "min_chars": int(min_chars),
                        "under_chars": under,
                        "under_ratio": round(under_ratio, 3),
                        "type": "too_short",
                    }
                    if under <= soft_under_chars or under_ratio <= soft_under_ratio:
                        item["severity"] = "warning"
                        char_budget_warnings.append(item)
                    elif under_ratio >= hard_under_ratio:
                        item["severity"] = "hard"
                        char_budget_issues.append(item)
                    else:
                        item["severity"] = "warning"
                        char_budget_warnings.append(item)

            ok = not missing and not extra and not empty_text and not char_budget_issues and len(segments) == len(shots)
            check = {
                "short_video_id": sid,
                "ok": ok,
                "shot_count": len(shots),
                "segment_count": len(segments),
                "missing_shot_ids": missing,
                "extra_shot_ids": extra,
                "empty_text_shot_ids": empty_text,
                "char_budget_issues": char_budget_issues,
                "char_budget_warnings": char_budget_warnings,
            }
            checks.append(check)
            if not ok:
                hard_issues.append(
                    f"{sid}: shot/voiceover mismatch "
                    f"(shots={len(shots)}, segments={len(segments)}, missing={missing}, extra={extra}, empty={empty_text}, char_issues={len(char_budget_issues)})"
                )
            if char_budget_warnings:
                print("Voiceover alignment warnings:")
                for w in char_budget_warnings:
                    print(f"- {w}")
        result = {"ok": not hard_issues, "checks": checks, "hard_issues": hard_issues}
        if hasattr(self, "task_dir"):
            write_json(self.task_dir / "voiceover_alignment_check.json", result)
        if raise_on_error and hard_issues:
            raise RuntimeError(
                "AI voiceover script does not match editing_structure; blocked before TTS:\n"
                + "\n".join(f"- {issue}" for issue in hard_issues)
            )
        return result

    def _build_voiceover_quality_check_text(self, script: dict[str, Any], plan_item: dict[str, Any]) -> str:
        lines = [
            "Task: check voiceover script quality.",
            f"short_video_id: {script.get('short_video_id') or ''}",
            f"target_duration_seconds: {plan_item.get('target_duration_seconds') or script.get('target_duration_seconds') or ''}",
            "required facts:",
        ]
        for index, fact in enumerate(plan_item.get("must_keep_fact_points") or [], start=1):
            lines.append(f"{index}. {fact}")
        lines.append("")
        lines.append("script:")
        lines.append(str(script.get("narration_text") or ""))
        return "\n".join(lines)

    def _normalize_voiceover_quality_check(self, short_video_id: str, parsed: dict[str, Any]) -> dict[str, Any]:
        missing = parsed.get("missing") if isinstance(parsed.get("missing"), list) else []
        return {
            "short_video_id": short_video_id,
            "ok": 1 if int(parsed.get("ok", 1) or 0) else 0,
            "rewrite": 1 if int(parsed.get("rewrite", 0) or 0) else 0,
            "missing": missing,
        }

    def _text_agent_input_limit(self, step: str) -> int:
        cfg = self.config.raw.get("llm_input", {})
        return int(cfg.get(f"max_{step}_input_chars", cfg.get("max_text_agent_input_chars", 60000)) or 60000)

    def _log_llm_input_size(self, step: str, input_data: Any) -> None:
        text = input_data if isinstance(input_data, str) else json.dumps(input_data, ensure_ascii=False, separators=(",", ":"), default=str)
        print(f"LLM input size [{step}]: {len(text)} chars")
        limit = self._text_agent_input_limit(step)
        if len(text) > limit:
            if step == "content_analysis":
                hint = "请启用 content_analysis_preselect 或降低 micro_segment 输入字段长度。"
            elif step == "content_analysis_preselect":
                hint = "请降低 batch_size 或减少每个 segment 的 ASR/画面字段长度。"
            else:
                hint = "请检查该步骤输入压缩策略。"
            raise RuntimeError(f"LLM input size [{step}] exceeds limit={limit}; {hint}")

    def _run_text_agent(self, step: str, input_data: Any, *, allow_reuse: bool = True) -> Any:
        if self.llm_text is None:
            raise RuntimeError("缺少 text LLM 配置")
        base, out_name, prompt, default_prompt_version = AGENT_INFO[step]
        llm_cfg = self.config.llm
        provider = llm_cfg.get("text_llm_provider", "openai")
        model = self.options.model or llm_cfg.get("text_llm_model_name") or llm_cfg.get(f"text_{provider}_model_name")
        fallback = llm_cfg.get(f"text_{provider}_fallback_models", [])
        max_tokens = int(llm_cfg.get("text_llm_max_tokens", 16000) or 16000)
        prompt_version = self.options.prompt_version or default_prompt_version
        input_hash = stable_hash({"input": input_data, "model": model, "fallback": fallback, "prompt": prompt, "prompt_version": prompt_version})
        if allow_reuse and self._can_reuse(step, input_hash):
            print(f"复用缓存: {step}")
            return self._load_step_json(step)
        version, vdir = self._version_dir(step, base)
        ensure_dir(vdir)
        self._log_llm_input_size(step, input_data)
        if isinstance(input_data, str):
            write_text(vdir / "input.txt", input_data)
            write_json(vdir / "input_meta.json", {"type": "text", "chars": len(input_data)})
        else:
            write_json(vdir / "input.json", input_data)
        write_text(vdir / "prompt.txt", prompt)
        result = self.llm_text.call_json(
            model=model,
            fallback_models=fallback,
            prompt=prompt,
            input_data=input_data,
            temperature=0.2,
            debug_dir=vdir / "_llm_debug",
            max_tokens=max_tokens,
        )
        raw = {
            "model": result.model,
            "created_at": now_iso(),
            "raw_text": result.raw_text,
            "usage": result.usage,
            "finish_reason": result.finish_reason,
            "latency_ms": result.latency_ms,
        }
        write_json(vdir / "raw_response.json", raw)
        model_output = result.parsed
        write_json(vdir / "model_output.json", model_output)
        manual_path = vdir / "manual_override.json"
        final_output = self._merge_manual(model_output, read_json(manual_path, {}))
        write_json(vdir / "final_output.json", final_output)
        out = write_json(vdir / out_name, final_output)
        status = self._base_status(step, version, input_hash, [out])
        status.update({"model": result.model, "prompt_version": prompt_version, "temperature": 0.2, "manual_edited": manual_path.exists()})
        self._write_status(vdir, status)
        self._record_step(step=step, version=version, status="success", output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out], extra={"model": result.model, "prompt_version": prompt_version})
        print(f"完成: {step}")
        return final_output

    def _duration_strategy_payload(self) -> dict[str, Any]:
        settings = self.duration_settings
        return {
            "default_target_seconds": self.options.target_duration_seconds or settings.default_target_seconds,
            "quick_news_seconds": [settings.quick_news_min_seconds, settings.quick_news_max_seconds],
            "normal_seconds": [settings.normal_min_seconds, settings.normal_max_seconds],
            "context_max_seconds": settings.context_max_seconds,
            "complex_max_seconds": settings.complex_max_seconds,
            "hard_max_without_confirmation": settings.hard_max_without_confirmation,
            "allow_long_video": self.options.allow_long_video,
            "allow_long_video_meaning": "allow_complete_story_within_60_seconds",
            "when_allow_long_video_target_range": [45, 60],
            "target_duration_is_soft_when_allow_long_video": True,
            "chars_per_second": settings.chars_per_second,
            "audio_policy": self.options.audio_policy,
            "require_tts": self.options.require_tts,
            "ai_voiceover_original_audio_max_seconds": settings.ai_voiceover_original_audio_max_seconds,
            "ai_voiceover_original_audio_max_ratio": settings.ai_voiceover_original_audio_max_ratio,
            "ai_voiceover_original_audio_single_max_seconds": settings.ai_voiceover_original_audio_single_max_seconds,
            "ai_voiceover_min_ratio": settings.ai_voiceover_min_ratio,
            "block_on_duration_mismatch": True,
            "compact_voiceover_default": settings.compact_voiceover_default,
            "segment_aligned_only_for_evidence": settings.segment_aligned_only_for_evidence,
            "inter_sentence_gap_seconds": settings.inter_sentence_gap_seconds,
            "max_inter_sentence_gap_seconds": settings.max_inter_sentence_gap_seconds,
            "max_total_silence_ratio": settings.max_total_silence_ratio,
            "tts_actual_chars_per_second": settings.tts_actual_chars_per_second,
        }

    def _run_options_payload(self) -> dict[str, Any]:
        target_seconds = self.options.target_duration_seconds or self.duration_settings.default_target_seconds
        return {
            "target_duration_seconds": target_seconds,
            "target_duration_mode": "auto_within_60" if self.options.allow_long_video and target_seconds == 30 else "fixed",
            "allow_long_video": self.options.allow_long_video,
            "audio_policy": self.options.audio_policy,
            "require_tts": self.options.require_tts,
            "voice_id": self.options.voice_id,
            "production_mode": self.options.production_mode,
            "output_mode": self.options.output_mode,
            "max_output_videos": self.options.max_output_videos,
            "min_output_video_seconds": self.options.min_output_video_seconds,
            "max_output_video_seconds": self.options.max_output_video_seconds,
        }

    def _run_options_payload_minimal(self) -> dict[str, Any]:
        return {
            "target_duration_seconds": self.options.target_duration_seconds or self.duration_settings.default_target_seconds,
            "allow_long_video": self.options.allow_long_video,
            "audio_policy": self.options.audio_policy,
            "voice_id": self.options.voice_id,
            "production_mode": self.options.production_mode,
            "output_mode": self.options.output_mode,
            "max_output_videos": self.options.max_output_videos,
        }

    def _resolve_voice_config(self) -> dict[str, Any]:
        base = dict(self.config.omnivoice)
        voices = self.config.raw.get("voices", {})
        if not isinstance(voices, dict):
            voices = {}

        requested = str(self.options.voice_id or base.get("default_voice_id") or "").strip()
        selected_id = requested
        selected: dict[str, Any] = {}
        if requested and isinstance(voices.get(requested), dict):
            selected = dict(voices[requested])
        elif voices:
            for key, value in voices.items():
                if isinstance(value, dict) and value.get("enabled") is not False:
                    selected_id = str(value.get("id") or key)
                    selected = dict(value)
                    break

        if selected:
            merged = {**base, **selected}
            merged["voice_id"] = str(selected.get("id") or selected_id)
            merged["voice_name"] = str(selected.get("name") or merged["voice_id"])
            return merged

        base["voice_id"] = str(base.get("default_voice_id") or "default")
        base["voice_name"] = "默认音色"
        return base

    def _reassembly_options_payload(self) -> dict[str, Any]:
        reassembly_cfg = self.config.raw.get("reassembly", {}) if hasattr(self, "config") else {}
        return {
            "output_mode": self.options.reassembly_output_mode,
            "requested_output_mode": self.options.output_mode,
            "max_output_videos": self.options.max_output_videos,
            "sort_mode": self.options.reassembly_sort_mode,
            "target_seconds": self.options.reassembly_target_seconds,
            "min_clip_seconds": self.options.reassembly_min_clip_seconds,
            "soft_min_clip_seconds": float(reassembly_cfg.get("soft_min_clip_seconds", 5.0)),
            "max_clip_seconds": self.options.reassembly_max_clip_seconds,
            "max_clip_count": self.options.reassembly_max_clip_count,
            "export_individual_clips": self.options.reassembly_export_individual_clips,
            "recover_missing_clip_ids": bool(reassembly_cfg.get("recover_missing_clip_ids", True)),
            "keep_original_audio": True,
        }

    def _enforce_long_video_confirmation(self, payload: dict[str, Any], *, source_step: str) -> None:
        candidates: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            candidates.extend([x for x in payload.get("short_videos", []) if isinstance(x, dict)])
            candidates.extend([x for x in payload.get("scripts", []) if isinstance(x, dict)])
        for item in candidates:
            seconds = float(
                item.get("estimated_total_duration_seconds")
                or item.get("target_duration_seconds")
                or item.get("recommended_duration_seconds")
                or 0
            )
            requires = bool(item.get("requires_user_long_video_confirmation")) or should_require_long_video_confirmation(
                seconds,
                allow_long_video=self.options.allow_long_video,
                settings=self.duration_settings,
            )
            if requires:
                self.manifest["status"] = "action_required"
                self.manifest["action_required"] = {
                    "type": "confirm_long_video",
                    "source_step": source_step,
                    "short_video_id": item.get("short_video_id", ""),
                    "estimated_duration_seconds": seconds,
                    "reason": item.get("over_60_seconds_reason") or item.get("duration_reason") or item.get("reason") or "规划时长超过 60 秒，需要确认长版后继续。",
                    "continue_command": "--allow-long-video --rerun-from editing_script",
                }
                self._save_manifest()
                raise RuntimeError(f"需要确认长版后继续: {seconds:.1f}s")

    def _enforce_long_video_confirmation_after_voiceover(self, voiceover: dict[str, Any]) -> None:
        if self.options.allow_long_video:
            return
        editing = self._load_step_json("editing_script")
        plan = self._load_step_json("short_video_planning")
        plan_by_id = {x.get("short_video_id"): x for x in plan.get("short_videos", []) if isinstance(x, dict)}
        voice_by_id = {x.get("short_video_id"): x for x in voiceover.get("scripts", []) if isinstance(x, dict)}
        for index, script in enumerate(editing.get("scripts", []), start=1):
            sid = script.get("short_video_id") or f"SV{index:03d}"
            plan_item = plan_by_id.get(sid, {})
            voice_item = voice_by_id.get(sid, {})
            video_seconds = editing_structure_duration(script.get("editing_structure", []))
            voice_seconds = float(
                voice_item.get("estimated_duration_seconds")
                or script.get("estimated_total_duration_seconds")
                or script.get("target_duration_seconds")
                or plan_item.get("target_duration_seconds")
                or 0
            )
            seconds = max(video_seconds, voice_seconds)
            if should_require_long_video_confirmation(seconds, allow_long_video=False, settings=self.duration_settings):
                reason = (
                    voice_item.get("revise_suggestion")
                    or plan_item.get("over_60_seconds_reason")
                    or script.get("duration_reason")
                    or plan_item.get("reason")
                    or "方案超过 60 秒，请审核视频分析、剪辑脚本和 AI 文案后确认是否继续生成长版。"
                )
                self.manifest["status"] = "action_required"
                self.manifest["action_required"] = {
                    "type": "confirm_long_video",
                    "source_step": "voiceover_script",
                    "short_video_id": sid,
                    "estimated_duration_seconds": round(seconds, 3),
                    "video_duration_seconds": round(video_seconds, 3),
                    "voiceover_estimated_duration_seconds": round(voice_seconds, 3),
                    "reason": reason,
                    "review_files": {
                        "video_analysis": self._step_output("video_understanding"),
                        "short_video_plan": self._step_output("short_video_planning"),
                        "editing_script": self._step_output("editing_script"),
                        "voiceover_script": self._step_output("voiceover_script"),
                    },
                    "continue_command": "--allow-long-video --rerun-from tts",
                    "continue_from_step": "tts",
                }
                self._save_manifest()
                raise RuntimeError(f"已生成 AI 文案，需用户确认长版后继续: {seconds:.1f}s")

    def _normalize_voiceover_scripts(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        scripts = payload.get("scripts", [])
        if not isinstance(scripts, list):
            return
        changed = False
        for item in scripts:
            if not isinstance(item, dict):
                continue
            narration = item.get("narration_text") or item.get("script_with_pause_marks") or item.get("formal_script") or item.get("short_video_script") or ""
            sanitized, text_policy_issues = sanitize_reporter_bylines(narration)
            fit = voiceover_duration_fit(
                sanitized,
                float(item.get("target_duration_seconds") or self.options.target_duration_seconds or self.duration_settings.default_target_seconds),
                float(item.get("max_allowed_seconds") or self.duration_settings.normal_max_seconds),
                self.duration_settings.chars_per_second,
            )
            item["narration_text"] = clean_voiceover_text(sanitized)
            if item.get("script_with_pause_marks"):
                cleaned_pause_text, _ = sanitize_reporter_bylines(item.get("script_with_pause_marks"))
                item["script_with_pause_marks"] = cleaned_pause_text
            item.setdefault("target_duration_seconds", self.options.target_duration_seconds or self.duration_settings.default_target_seconds)
            item.setdefault("max_allowed_seconds", self.duration_settings.normal_max_seconds)
            item.update(fit)
            item["text_policy_check"] = {
                "status": "fixed" if text_policy_issues else "ok",
                "issues": text_policy_issues,
                "policy": "no_reporter_byline_or_anchor_cue",
            }
            changed = True
        if changed:
            out = self.task_dir / self._step_output("voiceover_script")
            write_json(out, payload)

    def _validate_voiceover_segment_text_duration_or_raise(self, voiceover_output: dict[str, Any]) -> None:
        if self.options.production_mode != "ai_voiceover":
            return

        voice_cfg = self.config.raw.get("voiceover", {})
        min_ratio = float(voice_cfg.get("pre_tts_segment_min_ratio", 0.65))
        chars_per_second = float(voice_cfg.get("voiceover_chars_per_second", 4.8))

        issues = []
        for script in voiceover_output.get("scripts") or []:
            if not isinstance(script, dict):
                continue
            sid = str(script.get("short_video_id") or "unknown")
            for seg in script.get("narration_segments") or []:
                if not isinstance(seg, dict):
                    continue
                target = float(seg.get("target_duration_seconds") or 0)
                text = re.sub(r"\s+", "", str(seg.get("text") or ""))
                estimated = len(text) / max(chars_per_second, 0.1)
                if target > 0 and estimated < target * min_ratio:
                    issues.append({
                        "short_video_id": sid,
                        "shot_id": seg.get("shot_id"),
                        "target_duration_seconds": round(target, 3),
                        "estimated_text_duration_seconds": round(estimated, 3),
                        "chars": len(text),
                        "min_ratio": min_ratio,
                    })

        if issues:
            raise UserFacingPipelineError(
                "voiceover_script_segment_duration_too_short",
                user_message="配音文案生成失败：部分 shot 的文案明显短于画面目标时长。",
                suggestions=[
                    "从 voiceover_script 重跑，让模型按每个 shot 的 target 秒数补足文案。",
                    "如果希望视频更短，请从 short_video_edit_plan 重跑，减少画面片段。",
                ],
                technical_detail={"issues": issues[:50]},
            )

    def _validate_voiceover_script_duration_or_raise(self, voiceover_output: dict[str, Any]) -> None:
        scripts = voiceover_output.get("scripts", []) if isinstance(voiceover_output, dict) else []
        if not isinstance(scripts, list):
            return

        hard_issues: list[str] = []
        soft_warnings: list[str] = []
        required_sections = ["opening", "background", "core_fact", "analysis_or_conflict", "ending"]
        for script in scripts:
            if not isinstance(script, dict):
                continue
            short_video_id = script.get("short_video_id") or "unknown"
            target = float(script.get("target_duration_seconds") or 0)
            narration_text = str(script.get("narration_text") or "")
            actual_chars = len(re.sub(r"\s+", "", narration_text))
            hard_min_chars = int(target * 2.5)
            soft_min_chars = int(target * 3.0)

            if target >= 40 and actual_chars < hard_min_chars:
                hard_issues.append(
                    f"{short_video_id} AI voiceover narration is severely too short: target {target:.0f}s "
                    f"requires hard minimum {hard_min_chars} chars, got {actual_chars}."
                )
            elif target >= 40 and actual_chars < soft_min_chars:
                soft_warnings.append(
                    f"{short_video_id} AI voiceover narration is slightly short: target {target:.0f}s "
                    f"recommends at least {soft_min_chars} chars, got {actual_chars}."
                )

            if str(script.get("duration_fit") or "") == "too_short":
                hard_issues.append(f"{short_video_id} duration_fit=too_short; rerun with richer narration or merge more source clips.")

            sections = script.get("narrative_sections") or {}
            if target >= 40 and isinstance(sections, dict):
                for key in required_sections:
                    if not str(sections.get(key) or "").strip():
                        hard_issues.append(f"{short_video_id} missing narrative section: {key}")

        if soft_warnings:
            print(
                "Voiceover duration soft warnings:\n"
                + "\n".join(f"- {warning}" for warning in soft_warnings)
            )

        if hard_issues:
            raise RuntimeError(
                "AI voiceover script does not satisfy target duration requirements; blocked before TTS/cut/render:\n"
                + "\n".join(f"- {issue}" for issue in hard_issues)
            )

    def _format_compact_duration_message(
        self,
        *,
        video_duration: float,
        target_duration: float,
        min_compact_duration: float,
        tolerance_seconds: float,
        action: str,
    ) -> str:
        gap = max(0.0, min_compact_duration - video_duration)
        return (
            "成片时长检查："
            f"实际时长 {video_duration:.1f} 秒，"
            f"目标时长 {target_duration:.1f} 秒，"
            f"最低合格线 {min_compact_duration:.1f} 秒，"
            f"差距 {gap:.1f} 秒，"
            f"容错 {tolerance_seconds:.1f} 秒。"
            f"处理方式：{action}。"
        )

    def _compact_duration_block_reason(
        self,
        *,
        video_duration: float,
        target_duration: float,
        min_compact_duration: float,
    ) -> tuple[str | None, str | None]:
        if min_compact_duration <= 0:
            return None, None
        tolerance_seconds = max(0.0, float(self.duration_settings.compact_duration_tolerance_seconds or 0))
        severe_short_ratio = max(0.0, float(self.duration_settings.compact_severe_short_ratio or 0))
        severe_min_duration = target_duration * severe_short_ratio

        if video_duration < severe_min_duration:
            message = self._format_compact_duration_message(
                video_duration=video_duration,
                target_duration=target_duration,
                min_compact_duration=min_compact_duration,
                tolerance_seconds=tolerance_seconds,
                action=(
                    f"阻断：成片时长低于目标时长的 {severe_short_ratio:.0%}；"
                    "建议重新生成更完整的 AI 配音文案，或合并更多素材片段后再剪辑"
                ),
            )
            return message, message

        if video_duration + tolerance_seconds < min_compact_duration:
            message = self._format_compact_duration_message(
                video_duration=video_duration,
                target_duration=target_duration,
                min_compact_duration=min_compact_duration,
                tolerance_seconds=tolerance_seconds,
                action="阻断：成片时长明显低于最低合格线且超过容错范围；建议重新生成更长配音稿，或合并更多素材片段",
            )
            return message, message

        if video_duration < min_compact_duration:
            warning = self._format_compact_duration_message(
                video_duration=video_duration,
                target_duration=target_duration,
                min_compact_duration=min_compact_duration,
                tolerance_seconds=tolerance_seconds,
                action="提醒：成片时长略低于最低合格线，但仍在容错范围内，允许继续渲染",
            )
            return None, warning

        return None, None

    def step_tts(self) -> None:
        tts_slots = int(self.config.raw.get("gpu_limits", {}).get("tts_slots", 1))
        with file_slot_lock("tts", slots=tts_slots):
            self._step_tts_impl()

    def _step_tts_impl(self) -> None:
        voiceover = self._load_step_json("voiceover_script")
        editing = self._load_optional_step_json("editing_script", {"scripts": []})
        scripts = voiceover.get("scripts", []) if isinstance(voiceover, dict) else []
        tts_is_hard_required = self.options.require_tts and self.options.audio_policy in {"ai_voiceover", "mixed"}
        if tts_is_hard_required and not scripts:
            raise UserFacingPipelineError(
                "tts_no_voiceover_scripts",
                user_message="TTS 生成失败：AI 配音文案为空。",
                suggestions=["回查 agents/voiceover_script/v*/voiceover_script.json。", "回查 agents/short_video_edit_plan/v*/short_video_edit_plan.json。"],
            )
        editing_by_id = {
            item.get("short_video_id"): item
            for item in editing.get("scripts", [])
            if isinstance(item, dict)
        }
        voice_config = self._resolve_voice_config()
        input_hash = stable_hash({
            "voiceover": self._step_content_hash("voiceover_script"),
            "editing": self._step_content_hash("editing_script"),
            "voice_config": {
                "voice_id": voice_config.get("voice_id"),
                "reference_audio": voice_config.get("reference_audio"),
                "reference_text": voice_config.get("reference_text"),
                "speed": voice_config.get("speed"),
            },
            "require_tts": self.options.require_tts,
            "audio_policy": self.options.audio_policy,
            "allow_original_audio_evidence": self.options.allow_original_audio_evidence,
        })
        if self._can_reuse("tts", input_hash):
            print("复用缓存: tts")
            return
        version, base_dir = self._version_dir("tts", "tts/omnivoice")
        outputs = []
        if self.options.audio_policy == "original" or self.options.skip_tts:
            out_index = write_json(base_dir / "tts_outputs.json", {
                "version": version,
                "outputs": [],
                "status": "skipped",
                "reason": "audio_policy=original or skip_tts",
                "voice_id": voice_config.get("voice_id", ""),
                "voice_name": voice_config.get("voice_name", ""),
            })
            self._record_step(step="tts", version=version, status="skipped", output=relpath(out_index, self.task_dir), input_hash=input_hash, output_files=[out_index])
            print("跳过: tts")
            return
        for item in scripts:
            sid = item.get("short_video_id") or f"SV{len(outputs)+1:03d}"
            vdir = ensure_dir(base_dir / sid)
            text = item.get("narration_text") or item.get("script_with_pause_marks") or item.get("formal_script") or item.get("short_video_script") or ""
            write_text(vdir / "input_script.txt", text)
            write_json(vdir / "config.json", {
                "omnivoice": voice_config,
                "selected_voice_id": voice_config.get("voice_id", ""),
                "selected_voice_name": voice_config.get("voice_name", ""),
            })
            out = vdir / "voiceover.wav"
            item_status = "skipped"
            error = ""
            result = None
            timeline_segments = self._voiceover_segments_for_tts(item)
            segment_outputs: list[dict[str, Any]] = []
            requires_alignment = self._requires_segment_aligned_voiceover(item, editing_by_id.get(sid, {}))
            timeline_mode = "continuous"
            if timeline_segments:
                if requires_alignment or not self.duration_settings.compact_voiceover_default:
                    timeline_mode = "segment_aligned"
                else:
                    timeline_mode = "compact_segmented"
            if timeline_segments:
                model_path = self.config.resolve_path(voice_config.get("model_path"), "models/OmniVoice")
                ref_audio = self.config.resolve_path(voice_config.get("reference_audio"), "tts_ref/664925840_0_13s.mp3")
                segment_dir = ensure_dir(vdir / "segments")
                for seg in timeline_segments:
                    seg_text = seg.get("text", "")
                    seg_out = segment_dir / f"{seg['shot_id']}.wav"
                    seg_item = {
                        "shot_id": seg["shot_id"],
                        "target_start_seconds": seg["target_start_seconds"],
                        "target_end_seconds": seg["target_end_seconds"],
                        "target_duration_seconds": seg["target_duration_seconds"],
                        "text": seg_text,
                        "file": relpath(seg_out, self.task_dir),
                        "status": "skipped_empty",
                        "actual_duration_seconds": 0.0,
                    }
                    if seg_text:
                        seg_result = generate_omnivoice_audio(
                            text=seg_text,
                            output_path=seg_out,
                            model_path=model_path or "",
                            reference_audio=ref_audio or "",
                            reference_text=voice_config.get("reference_text", ""),
                            keep_model_loaded=True,
                            speed=voice_config.get("speed"),
                        )
                        seg_item.update(seg_result.to_dict())
                        seg_item["file"] = relpath(seg_out, self.task_dir)
                        seg_item["status"] = seg_result.status
                        if seg_result.error:
                            error = seg_result.error
                    segment_outputs.append(seg_item)
                failed_segments = [seg for seg in segment_outputs if seg.get("status") == "failed"]
                if failed_segments:
                    item_status = "failed"
                    if not error:
                        error = "segment TTS failed"
                elif any(seg.get("status") == "success" for seg in segment_outputs):
                    if timeline_mode == "compact_segmented":
                        self._compose_compact_voiceover(segment_outputs, out)
                    else:
                        self._compose_timeline_voiceover(segment_outputs, out)
                    result = audio_metadata(out)
                    item_status = "success"
                if error:
                    write_json(vdir / "error.json", {"short_video_id": sid, "error": error, "created_at": now_iso(), "segments": segment_outputs})
            elif text.strip():
                model_path = self.config.resolve_path(voice_config.get("model_path"), "models/OmniVoice")
                ref_audio = self.config.resolve_path(voice_config.get("reference_audio"), "tts_ref/664925840_0_13s.mp3")
                result = generate_omnivoice_audio(
                    text=text,
                    output_path=out,
                    model_path=model_path or "",
                    reference_audio=ref_audio or "",
                    reference_text=voice_config.get("reference_text", ""),
                    keep_model_loaded=True,
                    speed=voice_config.get("speed"),
                )
                item_status = result.status
                error = result.error
                if error:
                    write_json(vdir / "error.json", {"short_video_id": sid, "error": error, "created_at": now_iso()})
            output_item = {
                "short_video_id": sid,
                "file": relpath(out, self.task_dir),
                "status": item_status,
                "error": error,
                "text_length": len(text),
                "estimated_duration_seconds": round(len(text) / max(self.duration_settings.chars_per_second, 0.1), 3),
                "actual_duration_seconds": 0.0,
                "sample_rate": 24000,
                "voice_file_exists": out.exists(),
                "voice_id": voice_config.get("voice_id", ""),
                "voice_name": voice_config.get("voice_name", ""),
                "timeline_mode": timeline_mode,
                "segments": segment_outputs,
                "timeline_voiceover_file": relpath(out, self.task_dir) if timeline_segments else "",
            }
            if isinstance(result, dict):
                output_item.update(result)
                output_item["file"] = relpath(out, self.task_dir)
            elif result is not None:
                output_item.update(result.to_dict())
                output_item["file"] = relpath(out, self.task_dir)
            outputs.append(output_item)
            step_status = self._base_status("tts", version, stable_hash({"sid": sid, "text": text}), [out] if out.exists() else [])
            step_status.update({"status": item_status, "error": error})
            self._write_status(vdir, step_status)
        release_omnivoice_models()
        out_index = write_json(base_dir / "tts_outputs.json", {
            "version": version,
            "outputs": outputs,
            "voice_id": voice_config.get("voice_id", ""),
            "voice_name": voice_config.get("voice_name", ""),
        })
        reconcile = self._build_tts_duration_reconcile(editing, voiceover, {"outputs": outputs})
        expected_tts_segments = self._expected_tts_segment_keys() if tts_is_hard_required else set()
        fatal_errors = self._collect_tts_fatal_errors({"outputs": outputs}, expected_tts_segments)
        
        success = sum(1 for item in outputs if item.get("status") == "success")
        failed = sum(1 for item in outputs if item.get("status") == "failed")
        
        if tts_is_hard_required and fatal_errors:
            overall_status = "failed"
            hard_error = "TTS fatal failure"
        else:
            if not reconcile.get("ok", True):
                decision = self._decide_post_tts_duration_action(
                    editing_script=editing,
                    voiceover_script=voiceover,
                    tts_outputs={"outputs": outputs},
                    reconcile=reconcile,
                    fatal_errors=fatal_errors,
                )
                write_json(base_dir / "auto_duration_decision.json", decision)
                
                if decision.get("decision") == "shrink_video":
                    adjusted = self._build_adjusted_editing_script_from_tts(
                        editing_script=editing,
                        tts_outputs={"outputs": outputs},
                        reconcile=reconcile,
                        decision=decision,
                    )
                    write_json(base_dir / "adjusted_editing_script.json", adjusted)
                    overall_status = "success"
                    hard_error = ""
                elif decision.get("decision") == "continue":
                    overall_status = "success"
                    hard_error = ""
                else:
                    overall_status = "action_required"
                    hard_error = decision.get("reason", "Action required for duration mismatch")
            else:
                overall_status = "success" if failed == 0 else ("failed" if tts_is_hard_required else "partial_success")
                hard_error = ""
                
        self._record_step(
            step="tts",
            version=version,
            status=overall_status,
            output=relpath(out_index, self.task_dir),
            input_hash=input_hash,
            output_files=[out_index],
            extra={"summary": {"success": success, "failed": failed, "total": len(outputs), "duration_adjusted": overall_status == "success" and not reconcile.get("ok", True)}},
        )
        print(f"完成: tts ({overall_status})")
        
        if overall_status == "failed":
            error_type = "tts_failed"
            user_message = "TTS 生成失败：无法生成可用配音音频或音频严重不匹配。"
            for err in fatal_errors:
                if err.get("type") == "missing_required_tts_segment":
                    error_type = "tts_missing_segments"
                    user_message = "TTS 生成失败：部分 required narration_segments 没有成功生成音频。"
                    break
                elif err.get("type") == "empty_tts_outputs":
                    error_type = "tts_outputs_empty"
                    user_message = "TTS 生成失败：没有生成任何配音输出。"
                    break
                    
            raise UserFacingPipelineError(
                error_type,
                user_message=user_message,
                suggestions=[
                    "优先从 short_video_edit_plan 重新运行，重新生成配音目标时长和文案预算。",
                    "检查 agents/short_video_edit_plan/v*/short_video_edit_plan.json 中 target_duration_seconds 与 narration_*_chars 是否合理。",
                    "检查 agents/voiceover_script/v*/voiceover_script.json 的 narration_text 是否过短。",
                    "检查 tts_duration_reconcile.json 中 target_total_duration_seconds、tts_total_duration_seconds、coverage_ratio。",
                ],
                technical_detail={
                    "success": success,
                    "failed": failed,
                    "total": len(outputs),
                    "error": hard_error,
                    "fatal_errors": fatal_errors,
                    "tts_duration_reconcile": reconcile,
                },
            )
        elif overall_status == "action_required":
            self.manifest["action_required"] = {
                "type": "duration_mismatch_action_required",
                "message": hard_error,
                "reconcile_path": relpath(base_dir / "tts_duration_reconcile.json", self.task_dir),
            }
            self._save_manifest()

    def _collect_tts_fatal_errors(
        self,
        tts_outputs: dict[str, Any],
        required_segment_keys: set[tuple[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        errors = []
        outputs = tts_outputs.get("outputs", [])
        if not outputs:
            errors.append({"type": "empty_tts_outputs"})
            return errors
        
        actual_segments = set()
        for item in outputs:
            vid = str(item.get("short_video_id") or "").strip()
            audio_path = item.get("file")
            duration = float(item.get("actual_duration_seconds") or 0)
            
            if not audio_path:
                errors.append({"type": "missing_audio_path", "short_video_id": vid})
            elif not (self.task_dir / audio_path).exists() and not Path(audio_path).is_absolute():
                errors.append({"type": "audio_file_not_exists", "audio_path": audio_path})
            elif duration <= 0:
                errors.append({"type": "invalid_audio_duration", "audio_path": audio_path, "duration": duration})
            
            for seg in item.get("segments", []):
                if seg.get("status") == "success":
                    shot_id = str(seg.get("shot_id") or "").strip()
                    if vid and shot_id:
                        actual_segments.add((vid, shot_id))
                        
        if required_segment_keys is not None:
            missing = required_segment_keys - actual_segments
            for vid, shot_id in missing:
                errors.append({"type": "missing_required_tts_segment", "short_video_id": vid, "shot_id": shot_id})
                
        return errors

    def _decide_post_tts_duration_action(self, editing_script: dict[str, Any], voiceover_script: dict[str, Any], tts_outputs: dict[str, Any], reconcile: dict[str, Any], fatal_errors: list[dict[str, Any]]) -> dict[str, Any]:
        ratio = float(reconcile.get("coverage_ratio", 1.0))
        target_total = float(reconcile.get("target_total_duration_seconds", 0))
        tts_total = float(reconcile.get("tts_total_duration_seconds", 0))

        if fatal_errors:
            return {
                "schema_version": "auto_duration_decision_v1",
                "decision": "action_required",
                "reason": "TTS has fatal errors, cannot adjust video duration.",
                "fatal_errors": fatal_errors,
                "rebuild_cut_plan": False,
                "rerun_tts": False,
                "repair_voiceover": False,
            }

        voice_cfg = self.config.raw.get("voiceover", {}) if hasattr(self, "config") else {}
        ok_min_ratio = float(voice_cfg.get("post_tts_ok_min_ratio", 0.90))

        if ratio >= ok_min_ratio:
            return {
                "schema_version": "auto_duration_decision_v1",
                "decision": "continue",
                "reason": "TTS duration is close enough to planned video duration.",
                "rebuild_cut_plan": False,
                "rerun_tts": False,
                "repair_voiceover": False,
            }

        if tts_total > 0 and target_total > 0:
            return {
                "schema_version": "auto_duration_decision_v1",
                "decision": "shrink_video",
                "reason": "TTS is shorter than planned video; shrink video in AI voiceover mode.",
                "target_video_seconds_before": target_total,
                "tts_seconds": tts_total,
                "coverage_ratio": ratio,
                "rebuild_cut_plan": True,
                "rerun_tts": False,
                "repair_voiceover": False,
                "use_adjusted_editing_script": True,
            }

        return {
            "schema_version": "auto_duration_decision_v1",
            "decision": "action_required",
            "reason": "Duration mismatch exists but editing script cannot be safely adjusted.",
            "rebuild_cut_plan": False,
            "rerun_tts": False,
            "repair_voiceover": False,
        }

    def _build_adjusted_editing_script_from_tts(self, editing_script: dict[str, Any], tts_outputs: dict[str, Any], reconcile: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        adjusted = copy.deepcopy(editing_script)
        
        tts_by_shot_id = {}
        for item in tts_outputs.get("outputs", []):
            for seg in item.get("segments", []):
                shot_id = seg.get("shot_id")
                if shot_id:
                    tts_by_shot_id[str(shot_id)] = seg
                    
        voice_cfg = self.config.raw.get("voiceover", {}) if hasattr(self, "config") else {}
        tail_padding = float(voice_cfg.get("tts_visual_tail_padding_seconds", 1.0))
        warnings = []
        
        for video in adjusted.get("scripts", []):
            if not isinstance(video, dict):
                continue
            for shot in video.get("editing_structure", []):
                if not isinstance(shot, dict):
                    continue
                shot_id = str(shot.get("shot_id", ""))
                original_duration = float(shot.get("duration_seconds", shot.get("target_duration_seconds", 0)))
                source_start = shot.get("source_start_seconds")
                local_start = shot.get("local_start_seconds")
                
                tts_seg = tts_by_shot_id.get(shot_id)
                if not tts_seg:
                    warnings.append({
                        "type": "missing_tts_segment_for_shot",
                        "shot_id": shot_id,
                        "action": "keep_original_duration"
                    })
                    continue
                    
                tts_duration = float(tts_seg.get("actual_duration_seconds", 0))
                if tts_duration <= 0:
                    warnings.append({
                        "type": "invalid_tts_duration_for_shot",
                        "shot_id": shot_id,
                        "action": "keep_original_duration"
                    })
                    continue
                    
                min_d = float(shot.get("min_duration_seconds", max(3.0, original_duration * 0.60)))
                max_d = float(shot.get("max_duration_seconds", original_duration))
                max_d = min(max_d, original_duration)
                
                raw_new = tts_duration + tail_padding
                new_duration = max(min_d, min(raw_new, max_d))
                
                if new_duration <= 0 or new_duration > original_duration:
                    warnings.append({
                        "type": "adjusted_duration_invalid",
                        "shot_id": shot_id,
                        "raw_new_duration": raw_new,
                        "action": "keep_original_duration"
                    })
                    continue
                    
                shot["original_duration_seconds"] = original_duration
                shot["original_target_duration_seconds"] = shot.get("target_duration_seconds")
                shot["original_source_end_seconds"] = shot.get("source_end_seconds")
                
                new_duration_round = round(new_duration, 3)
                shot["duration_seconds"] = new_duration_round
                shot["target_duration_seconds"] = new_duration_round
                
                if source_start is not None:
                    source_end = float(source_start) + new_duration
                    shot["source_end_seconds"] = round(source_end, 3)
                    shot["source_end"] = seconds_to_timecode(source_end)
                    
                if local_start is not None:
                    local_end = float(local_start) + new_duration
                    shot["local_end_seconds"] = round(local_end, 3)
                    shot["local_end"] = seconds_to_timecode(local_end)
                    
                if "target_end_seconds" in shot:
                    target_start = float(shot.get("target_start_seconds", 0))
                    shot["target_end_seconds"] = round(target_start + new_duration, 3)
                    
                shot["tts_actual_duration_seconds"] = round(tts_duration, 3)
                shot["tts_tail_padding_seconds"] = tail_padding
                shot["duration_adjusted"] = True
                shot["adjust_action"] = "trim_tail_to_tts"
                shot["adjust_reason"] = "tts_segment_shorter_than_planned"

        adjusted.setdefault("meta", {})["schema_version"] = "adjusted_editing_script_v1"
        adjusted["meta"]["duration_adjusted"] = True
        adjusted["meta"]["adjust_reason"] = "tts_duration_mismatch_shrink_video"
        adjusted["meta"]["prevent_double_compact"] = True
        adjusted["meta"]["effective_editing_source"] = "adjusted_editing_script"
        adjusted["meta"]["auto_duration_decision_path"] = decision.get("path", "")
        adjusted["meta"]["warnings"] = warnings

        return adjusted

    def _tts_reconcile_hard_segments(self, reconcile: dict[str, Any]) -> list[dict[str, Any]]:
        hard_segments: list[dict[str, Any]] = []
        for video in reconcile.get("videos") or []:
            if not isinstance(video, dict):
                continue
            sid = str(video.get("short_video_id") or "")
            for seg in video.get("segments") or []:
                if not isinstance(seg, dict):
                    continue
                if seg.get("hard") or seg.get("status") in {"missing_tts", "too_short", "too_long"}:
                    item = dict(seg)
                    item["short_video_id"] = sid
                    hard_segments.append(item)
        return hard_segments

    def _tts_reconcile_hard_videos(self, reconcile: dict[str, Any]) -> list[dict[str, Any]]:
        hard_videos = []
        for video in reconcile.get("videos") or []:
            if not isinstance(video, dict):
                continue
            total_check = video.get("total_check") or {}
            if total_check.get("hard") or total_check.get("status") in {"too_short", "too_long"}:
                hard_videos.append(video)
        return hard_videos

    def _build_tts_duration_reconcile(
        self,
        editing: dict[str, Any],
        voiceover: dict[str, Any],
        tts: dict[str, Any],
    ) -> dict[str, Any]:
        edit_scripts = editing.get("scripts", []) if isinstance(editing, dict) else []
        voice_scripts = voiceover.get("scripts", []) if isinstance(voiceover, dict) else []
        tts_outputs = tts.get("outputs", []) if isinstance(tts, dict) else []
        voice_by_id = {str(item.get("short_video_id")): item for item in voice_scripts if isinstance(item, dict) and item.get("short_video_id")}
        tts_by_id = {str(item.get("short_video_id")): item for item in tts_outputs if isinstance(item, dict) and item.get("short_video_id")}
        voice_cfg = self.config.raw.get("voiceover", {}) if hasattr(self, "config") else {}
        ok_min = float(voice_cfg.get("tts_segment_ok_min_ratio") or 0.75)
        ok_max = float(voice_cfg.get("tts_segment_ok_max_ratio") or 1.25)
        hard_min = float(voice_cfg.get("tts_segment_hard_min_ratio") or 0.55)
        hard_max = float(voice_cfg.get("tts_segment_hard_max_ratio") or 1.60)
        videos: list[dict[str, Any]] = []
        for index, edit_script in enumerate(edit_scripts):
            if not isinstance(edit_script, dict):
                continue
            sid = str(edit_script.get("short_video_id") or f"v_{index + 1:03d}")
            voice_script = voice_by_id.get(sid, {})
            tts_item = tts_by_id.get(sid, {})
            tts_segments = tts_item.get("segments", []) if isinstance(tts_item.get("segments"), list) else []
            tts_by_shot = {str(seg.get("shot_id")): seg for seg in tts_segments if isinstance(seg, dict) and seg.get("shot_id")}
            voice_segments = voice_script.get("narration_segments", []) if isinstance(voice_script.get("narration_segments"), list) else []
            voice_by_shot = {str(seg.get("shot_id")): seg for seg in voice_segments if isinstance(seg, dict) and seg.get("shot_id")}
            segment_checks: list[dict[str, Any]] = []
            for shot in edit_script.get("editing_structure", []) or []:
                if not isinstance(shot, dict):
                    continue
                shot_id = str(shot.get("shot_id") or "")
                target, min_d, max_d = self._resolve_tts_check_target_duration(shot)
                tts_seg = tts_by_shot.get(shot_id)
                actual = float((tts_seg or {}).get("actual_duration_seconds") or 0)
                if not tts_seg or actual <= 0:
                    status = "missing_tts"
                elif min_d and actual < min_d:
                    status = "too_short"
                elif max_d and actual > max_d:
                    status = "too_long"
                elif target and actual / target < ok_min:
                    status = "slightly_short"
                elif target and actual / target > ok_max:
                    status = "slightly_long"
                else:
                    status = "ok"
                hard = bool(target and actual > 0 and (actual / target < hard_min or actual / target > hard_max)) or status == "missing_tts"
                segment_checks.append({
                    "shot_id": shot_id,
                    "status": status,
                    "hard": hard,
                    "fatal": status == "missing_tts",
                    "hard_type": "adjustable_duration_mismatch" if status in {"too_short", "too_long"} and actual > 0 else "missing" if status == "missing_tts" else "",
                    "target_duration_seconds": round(target, 3),
                    "min_duration_seconds": round(min_d, 3),
                    "max_duration_seconds": round(max_d, 3),
                    "tts_actual_duration_seconds": round(actual, 3),
                    "text_chars": len(re.sub(r"\s+", "", str((voice_by_shot.get(shot_id) or {}).get("text") or ""))),
                })
            video_target_total = sum(float(seg.get("target_duration_seconds") or 0) for seg in segment_checks)
            video_tts_total = sum(float(seg.get("tts_actual_duration_seconds") or 0) for seg in segment_checks)
            coverage_ratio = video_tts_total / video_target_total if video_target_total > 0 else 0.0
            
            script_ok_min = float(voice_cfg.get("tts_script_ok_min_ratio") or 0.90)
            script_hard_min = float(voice_cfg.get("tts_script_hard_min_ratio") or 0.75)
            script_ok_max = float(voice_cfg.get("tts_script_ok_max_ratio") or 1.15)
            script_hard_max = float(voice_cfg.get("tts_script_hard_max_ratio") or 1.40)
            
            total_status = "ok"
            total_hard = False
            if video_target_total > 0:
                if coverage_ratio < script_hard_min:
                    total_status = "too_short"
                    total_hard = True
                elif coverage_ratio < script_ok_min:
                    total_status = "slightly_short"
                elif coverage_ratio > script_hard_max:
                    total_status = "too_long"
                    total_hard = True
                elif coverage_ratio > script_ok_max:
                    total_status = "slightly_long"

            hard_count = sum(1 for seg in segment_checks if seg.get("hard"))
            videos.append({
                "short_video_id": sid,
                "ok": all(seg.get("status") in {"ok", "slightly_short", "slightly_long"} for seg in segment_checks) and not total_hard,
                "target_total_duration_seconds": round(video_target_total, 3),
                "tts_total_duration_seconds": round(video_tts_total, 3),
                "coverage_ratio": round(coverage_ratio, 3),
                "total_check": {
                    "ratio": round(coverage_ratio, 3),
                    "status": total_status,
                    "hard": total_hard,
                    "fatal": False,
                    "hard_type": "adjustable_duration_mismatch" if total_hard else ""
                },
                "hard_segment_count": hard_count,
                "segments": segment_checks,
            })
        result = {
            "ok": all(video.get("ok") for video in videos),
            "target_total_duration_seconds": round(sum(v.get("target_total_duration_seconds", 0) for v in videos), 3),
            "tts_total_duration_seconds": round(sum(v.get("tts_total_duration_seconds", 0) for v in videos), 3),
            "videos": videos,
        }
        if hasattr(self, "task_dir"):
            write_json(self.task_dir / "tts_duration_reconcile.json", result)
        return result

    def _voiceover_segments_for_tts(self, script: dict[str, Any]) -> list[dict[str, Any]]:
        raw_segments = script.get("narration_segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            return []
        segments: list[dict[str, Any]] = []
        for index, segment in enumerate(raw_segments, start=1):
            if not isinstance(segment, dict):
                continue
            start_value = segment.get("target_start_seconds")
            end_value = segment.get("target_end_seconds")
            start = float(start_value) if start_value not in (None, "") else timecode_to_seconds(segment.get("target_start"))
            end = float(end_value) if end_value not in (None, "") else timecode_to_seconds(segment.get("target_end"))
            duration = float(segment.get("target_duration_seconds") or 0)
            if end <= start and duration > 0:
                end = start + duration
            if end <= start:
                continue
            text = clean_voiceover_text(segment.get("text"))
            segments.append({
                "shot_id": segment.get("shot_id") or f"shot_{index:03d}",
                "target_start_seconds": round(start, 3),
                "target_end_seconds": round(end, 3),
                "target_duration_seconds": round(end - start, 3),
                "text": text,
            })
        return segments

    def _requires_segment_aligned_voiceover(self, script: dict[str, Any], editing_script: dict[str, Any] | None = None) -> bool:
        if self.options.audio_policy == "original":
            return False
        if (
            self.options.audio_policy == "ai_voiceover"
            and self.duration_settings.ai_voiceover_disable_original_audio
            and not self.options.allow_original_audio_evidence
            and not self.duration_settings.allow_original_audio_evidence
            and not self.duration_settings.ai_voiceover_allow_model_original_audio
        ):
            return False
        if not self.duration_settings.segment_aligned_only_for_evidence:
            return True
        editing_script = editing_script or {}
        for segment in editing_script.get("editing_structure") or []:
            if not isinstance(segment, dict):
                continue
            audio_mode = segment.get("audio_mode") or ("mixed_evidence" if segment.get("original_audio_required") else "")
            if audio_mode in {"mixed_evidence", "original_sound"}:
                original_volume = original_audio_volume_for_clip(
                    {**segment, "audio_mode": audio_mode},
                    audio_policy=self.options.audio_policy,
                    settings=self.duration_settings,
                    allow_original_audio_evidence=self.options.allow_original_audio_evidence,
                )
                if original_volume > 0:
                    return True
        completion = script.get("completion_check") or {}
        if float(completion.get("original_sound_total_seconds") or 0) > 0:
            return True
        for segment in script.get("narration_segments") or []:
            if not isinstance(segment, dict):
                continue
            audio_mode = segment.get("audio_mode")
            if audio_mode in {"mixed_evidence", "original_sound"}:
                return True
            if not clean_voiceover_text(segment.get("text")):
                return True
        return False

    def _compose_timeline_voiceover(self, segments: list[dict[str, Any]], output_path: Path) -> None:
        total_duration = max(float(seg.get("target_end_seconds") or 0) for seg in segments)
        if total_duration <= 0:
            raise RuntimeError("timeline voiceover duration is empty")
        active_segments = [
            seg for seg in segments
            if seg.get("status") == "success" and seg.get("file") and (self.task_dir / seg["file"]).exists()
        ]
        if not active_segments:
            raise RuntimeError("timeline voiceover has no successful segments")
        inputs: list[str] = []
        filter_parts = ["[0:a]volume=0[base]"]
        mix_labels = ["[base]"]
        for input_index, seg in enumerate(active_segments, start=1):
            inputs.extend(["-i", str(self.task_dir / seg["file"])])
            delay_ms = int(round(float(seg.get("target_start_seconds") or 0) * 1000))
            label = f"a{input_index}"
            actual_duration = float(seg.get("actual_duration_seconds") or 0)
            target_duration = float(seg.get("target_duration_seconds") or 0)
            tempo_chain = self._atempo_chain(actual_duration / target_duration) if actual_duration > target_duration + 0.05 and target_duration > 0 else ""
            filters = []
            if tempo_chain:
                filters.append(tempo_chain)
                seg["tempo_adjustment"] = round(actual_duration / target_duration, 3)
            filters.append(f"atrim=0:{target_duration:.3f}")
            filters.append("asetpts=PTS-STARTPTS")
            filters.append(f"adelay={delay_ms}|{delay_ms}")
            filter_parts.append(f"[{input_index}:a]{','.join(filters)}[{label}]")
            mix_labels.append(f"[{label}]")
        filter_parts.append(f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first:normalize=0[aout]")
        run_cmd([
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-t",
            f"{total_duration:.3f}",
            "-i",
            "anullsrc=r=24000:cl=mono",
            *inputs,
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[aout]",
            "-ar",
            "24000",
            str(output_path),
        ])

    def _compose_compact_voiceover(self, segments: list[dict[str, Any]], output_path: Path) -> None:
        compacted = compact_voiceover_segment_times(segments, self.duration_settings.inter_sentence_gap_seconds)
        if not compacted:
            raise RuntimeError("compact voiceover has no successful segments")
        by_shot = {seg.get("shot_id"): seg for seg in compacted}
        for index, segment in enumerate(segments):
            compact = by_shot.get(segment.get("shot_id"))
            if compact:
                segments[index].update(compact)
        concat_file = output_path.with_suffix(".concat.txt")
        lines: list[str] = []
        silence_file: Path | None = None
        if len(compacted) > 1 and self.duration_settings.inter_sentence_gap_seconds > 0:
            silence_file = output_path.with_name(f"{output_path.stem}_gap.wav")
            run_cmd([
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-t",
                f"{self.duration_settings.inter_sentence_gap_seconds:.3f}",
                "-i",
                "anullsrc=r=24000:cl=mono",
                "-ar",
                "24000",
                str(silence_file),
            ])
        for index, segment in enumerate(compacted):
            lines.append(f"file '{str(self.task_dir / segment['file']).replace(chr(92), '/')}'\n")
            if silence_file is not None and index < len(compacted) - 1:
                lines.append(f"file '{str(silence_file).replace(chr(92), '/')}'\n")
        write_text(concat_file, "".join(lines))
        run_cmd([
            "ffmpeg",
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
            "-ar",
            "24000",
            str(output_path),
        ])

    def _atempo_chain(self, ratio: float) -> str:
        parts: list[str] = []
        value = max(float(ratio or 1.0), 0.01)
        while value > 2.0:
            parts.append("atempo=2.0")
            value /= 2.0
        while value < 0.5:
            parts.append("atempo=0.5")
            value /= 0.5
        parts.append(f"atempo={value:.6f}")
        return ",".join(parts)

    def step_subtitles(self) -> None:
        voiceover = self._load_step_json("voiceover_script")
        tts = self._load_optional_step_json("tts", {"outputs": []})
        scripts = voiceover.get("scripts", []) if isinstance(voiceover, dict) else []
        input_hash = stable_hash({
            "voiceover": self._step_content_hash("voiceover_script"),
            "tts": self._step_content_hash("tts"),
            "subtitle_config": self._subtitle_config(),
            "aspect_ratio": self.options.aspect_ratio,
        })
        if self._can_reuse("subtitles", input_hash):
            print("复用缓存: subtitles")
            return
        version, base_dir = self._version_dir("subtitles", "subtitles")
        outputs = []
        tts_by_id = {x["short_video_id"]: x for x in tts.get("outputs", []) if "short_video_id" in x}
        for item in scripts:
            sid = item.get("short_video_id") or f"SV{len(outputs)+1:03d}"
            vdir = ensure_dir(base_dir / sid)
            text = item.get("narration_text") or item.get("script_with_pause_marks") or item.get("formal_script") or item.get("short_video_script") or ""
            tts_item = tts_by_id.get(sid, {})
            duration = float(tts_item.get("actual_duration_seconds") or item.get("estimated_duration_seconds") or 0)
            tts_segments = tts_item.get("segments", []) if isinstance(tts_item.get("segments"), list) else []
            if self.duration_settings.subtitle_follow_actual_tts and tts_segments:
                lines = self._tts_segments_to_srt(tts_segments)
            elif isinstance(item.get("narration_segments"), list) and item.get("narration_segments"):
                lines = self._segments_to_srt(item["narration_segments"])
            else:
                lines = self._script_to_srt(text, total_duration=duration if duration > 0 else None)
            out = write_text(vdir / "subtitle.srt", lines)
            write_json(vdir / "subtitle.json", {"short_video_id": sid, "source": item, "tts": tts_item})
            outputs.append({"short_video_id": sid, "file": relpath(out, self.task_dir)})
            self._write_status(vdir, self._base_status("subtitles", version, stable_hash(item), [out]))
        out_index = write_json(base_dir / "subtitle_outputs.json", {"version": version, "outputs": outputs})
        self._record_step(step="subtitles", version=version, status="success", output=relpath(out_index, self.task_dir), input_hash=input_hash, output_files=[out_index])
        print("完成: subtitles")

    def _compact_debug_segment(self, seg: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "shot_id",
            "source_id",
            "source_start",
            "source_end",
            "source_start_seconds",
            "source_end_seconds",
            "local_start",
            "local_end",
            "local_start_seconds",
            "local_end_seconds",
            "start",
            "end",
            "duration_seconds",
        )
        return {key: seg.get(key) for key in keys if key in seg}

    def _build_clips_from_editing_structure(self, editing_structure: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        clips: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        cursor = 0.0
        for index, seg in enumerate(editing_structure or []):
            if not isinstance(seg, dict):
                invalid.append({"index": index, "reason": "not_object"})
                continue
            start, end = self._segment_start_end_seconds(seg)
            if start is None or end is None:
                invalid.append({
                    "index": index,
                    "shot_id": seg.get("shot_id"),
                    "reason": "missing_start_or_end",
                    "segment": self._compact_debug_segment(seg),
                })
                continue
            duration = float(end - start)
            if duration <= 0:
                invalid.append({
                    "index": index,
                    "shot_id": seg.get("shot_id"),
                    "reason": "end_lte_start",
                    "start": start,
                    "end": end,
                })
                continue
            target_duration = float(seg.get("target_duration_seconds") or seg.get("duration_seconds") or duration)
            target_duration = min(max(0.0, target_duration), duration)
            if target_duration <= 0:
                invalid.append({
                    "index": index,
                    "shot_id": seg.get("shot_id"),
                    "reason": "target_duration_lte_zero",
                    "target_duration_seconds": target_duration,
                })
                continue
            audio_mode = seg.get("audio_mode") or ("mixed_evidence" if seg.get("original_audio_required") else "ai_voiceover")
            clip = {
                "shot_id": seg.get("shot_id", ""),
                "source_id": seg.get("source_id", ""),
                "source_index": seg.get("source_index", ""),
                "source_start": seconds_to_timecode(start, ms=True),
                "source_end": seconds_to_timecode(start + target_duration, ms=True),
                "source_start_seconds": round(start, 3),
                "source_end_seconds": round(start + target_duration, 3),
                "source_max_end_seconds": round(end, 3),
                "target_start": seconds_to_timecode(cursor, ms=True),
                "target_start_seconds": round(cursor, 3),
                "target_end_seconds": round(cursor + target_duration, 3),
                "duration_seconds": round(target_duration, 3),
                "target_duration_seconds": round(target_duration, 3),
                "min_duration_seconds": seg.get("min_duration_seconds"),
                "max_duration_seconds": seg.get("max_duration_seconds"),
                "purpose": seg.get("purpose", ""),
                "audio_mode": audio_mode,
                "original_audio_reason": seg.get("original_audio_reason") or seg.get("visual_selection_reason") or seg.get("editing_note") or "",
                "original_audio_policy": seg.get("original_audio_policy", ""),
                "original_audio_type": seg.get("original_audio_type", ""),
                "original_audio_value_score": seg.get("original_audio_value_score", ""),
                "original_audio_complete_unit": seg.get("original_audio_complete_unit", ""),
                "original_audio_transcript_summary": seg.get("original_audio_transcript_summary", ""),
                "crop_mode": "fit_blur" if self.options.aspect_ratio == "9:16" else "original",
            }
            clips.append(clip)
            cursor += target_duration
        return clips, invalid

    def _validate_ai_voiceover_inputs_before_cut_plan(
        self,
        editing: dict[str, Any],
        voiceover: dict[str, Any],
        tts: dict[str, Any],
    ) -> None:
        edit_scripts = editing.get("scripts") if isinstance(editing, dict) else []
        voice_scripts = voiceover.get("scripts") if isinstance(voiceover, dict) else []
        tts_outputs = tts.get("outputs") if isinstance(tts, dict) else []

        issues: list[str] = []
        if not isinstance(edit_scripts, list) or not edit_scripts:
            issues.append("editing_script.scripts is empty")
        if not isinstance(voice_scripts, list) or not voice_scripts:
            issues.append("voiceover_script.scripts is empty")
        if self.options.require_tts and self.options.audio_policy in {"ai_voiceover", "mixed"}:
            if not isinstance(tts_outputs, list) or not tts_outputs:
                issues.append("tts.outputs is empty")

        for script in edit_scripts or []:
            if not isinstance(script, dict):
                continue
            sid = str(script.get("short_video_id") or "")
            shots = script.get("editing_structure") or []
            if not isinstance(shots, list) or not shots:
                issues.append(f"{sid}: editing_structure is empty")
                continue
            for shot in shots:
                if not isinstance(shot, dict):
                    continue
                shot_id = shot.get("shot_id", "")
                if not shot.get("source_id"):
                    issues.append(f"{sid}/{shot_id}: missing source_id")
                start = self._time_value_seconds(shot.get("source_start_seconds") or shot.get("source_start"))
                end = self._time_value_seconds(shot.get("source_end_seconds") or shot.get("source_end"))
                if start is None or end is None or end <= start:
                    issues.append(f"{sid}/{shot_id}: invalid source time")

        for voice_script in voice_scripts or []:
            if not isinstance(voice_script, dict):
                continue
            sid = str(voice_script.get("short_video_id") or "")
            segments = voice_script.get("narration_segments") or []
            if not isinstance(segments, list) or not segments:
                issues.append(f"{sid}: narration_segments is empty")
                continue
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                if not str(segment.get("shot_id") or "").strip():
                    issues.append(f"{sid}: narration segment missing shot_id")
                if not str(segment.get("text") or "").strip():
                    issues.append(f"{sid}/{segment.get('shot_id', '')}: narration text is empty")

        if issues:
            raise UserFacingPipelineError(
                "cut_plan_ai_voiceover_input_invalid",
                user_message="生成剪辑计划失败：AI 配音上游结构不完整。",
                suggestions=["回查 ai_voiceover_candidates、short_video_edit_plan、voiceover_script、tts。"],
                technical_detail={"issues": issues[:100]},
            )

    def _validate_cut_plan_not_empty_or_raise(self, cut_plan: dict[str, Any]) -> None:
        videos = [item for item in cut_plan.get("output_videos") or [] if isinstance(item, dict)]
        if not videos:
            raise UserFacingPipelineError(
                "cut_plan_output_videos_empty",
                user_message="剪辑计划生成失败：cut_plan.output_videos 为空。",
                suggestions=["回查 short_video_edit_plan / voiceover_script / tts 输出。"],
                technical_detail={"cut_plan": cut_plan},
            )

        errors: list[dict[str, Any]] = []
        for video in videos:
            vid = str(video.get("short_video_id") or video.get("video_id") or "").strip()
            clips = [clip for clip in video.get("clips") or [] if isinstance(clip, dict)]
            if not clips:
                errors.append({"short_video_id": vid, "reason": "clips_empty"})
                continue
            for clip in clips:
                source_id = str(clip.get("source_id") or "").strip()
                start = self._time_value_seconds(clip.get("source_start_seconds") or clip.get("source_start") or clip.get("start_seconds"))
                end = self._time_value_seconds(clip.get("source_end_seconds") or clip.get("source_end") or clip.get("end_seconds"))
                if not source_id or start is None or end is None or end <= start:
                    errors.append({
                        "short_video_id": vid,
                        "reason": "invalid_clip_time_or_source",
                        "clip": clip,
                    })

        if errors:
            raise UserFacingPipelineError(
                "cut_plan_clips_invalid",
                user_message="剪辑计划生成失败：存在空 clips 或无效 source/time。",
                suggestions=[
                    "回查 cut_plan/v*/cut_plan.json。",
                    "回查 short_video_edit_plan 的 editing_structure 是否有 source_start/source_end。",
                    "回查 TTS 是否生成完整。",
                ],
                technical_detail={"errors": errors},
            )

    def _validate_ai_voiceover_coverage_before_render(self, renderable_videos: list[dict[str, Any]]) -> None:
        if self.options.production_mode != "ai_voiceover":
            return
            
        cfg = self.config.raw.get("voiceover", {}) if hasattr(self, "config") else {}
        if not cfg.get("ai_voiceover_block_duration_mismatch", True):
            return
            
        errors = []
        for video in renderable_videos:
            vid = video.get("short_video_id") or ""
            video_dur = float(video.get("video_duration_seconds") or 0)
            voice_dur = float(video.get("voiceover_duration_seconds") or 0)
            
            if video_dur > 0 and voice_dur > 0:
                ratio = voice_dur / video_dur
                hard_min = float(cfg.get("tts_script_hard_min_ratio", 0.75))
                
                if ratio < hard_min:
                    errors.append({
                        "short_video_id": vid,
                        "video_duration_seconds": round(video_dur, 2),
                        "voiceover_duration_seconds": round(voice_dur, 2),
                        "coverage_ratio": round(ratio, 2),
                        "required_min_ratio": hard_min,
                        "reason": f"AI配音总时长占比过低 ({ratio:.1%} < {hard_min:.1%})，导致视频后半段无解说",
                    })

        if errors:
            raise UserFacingPipelineError(
                "ai_voiceover_coverage_too_low",
                user_message="最终剪辑计划中 AI 配音时长未能充分覆盖视频时长，成片后半段可能出现长时间无声。",
                suggestions=[
                    "文案内容过少或目标时长过长，导致画面多而解说少。",
                    "建议：重新调整生成配音文案，增加解说词；或者在「视频时长设定」中调小期望时长。",
                    "或者修改 config.toml 中的 tts_script_hard_min_ratio 降低硬性拦截标准。",
                ],
                technical_detail={"errors": errors},
            )

    def _load_effective_editing_for_cut_plan(self) -> tuple[dict[str, Any], dict[str, Any]]:
        original = self._load_step_json("editing_script")
        voice_cfg = self.config.raw.get("voiceover", {}) if hasattr(self, "config") else {}
        if not voice_cfg.get("use_adjusted_editing_script_for_cut_plan", True):
            return original, {
                "editing_source": "original_editing_script",
                "reason": "use_adjusted_editing_script_for_cut_plan_disabled"
            }
            
        tts_entry = self.manifest.get("steps", {}).get("tts")
        if not tts_entry or tts_entry.get("status") not in {"success", "partial_success"}:
            return original, {"editing_source": "original_editing_script", "reason": "tts_not_success"}
            
        tts_output = tts_entry.get("output")
        if not tts_output:
            return original, {"editing_source": "original_editing_script", "reason": "tts_output_missing"}
            
        tts_dir = (self.task_dir / tts_output).parent
        decision_path = tts_dir / "auto_duration_decision.json"
        adjusted_path = tts_dir / "adjusted_editing_script.json"
        
        decision = read_json(decision_path, None)
        adjusted = read_json(adjusted_path, None)
        
        if decision and adjusted and decision.get("rebuild_cut_plan"):
            if adjusted.get("meta", {}).get("duration_adjusted"):
                return adjusted, {
                    "editing_source": "adjusted_editing_script",
                    "auto_duration_decision": decision,
                    "adjusted_editing_script_path": relpath(adjusted_path, self.task_dir),
                    "prevent_double_compact": True
                }
                
        return original, {
            "editing_source": "original_editing_script",
            "reason": "no_adjusted_editing_script"
        }

    def _build_final_duration_check(self, output_videos: list[dict[str, Any]], effective_meta: dict[str, Any]) -> dict[str, Any]:
        videos_check = []
        all_ok = True
        for video in output_videos:
            tts_total = float(video.get("voiceover_duration_seconds") or 0)
            cut_plan_total = float(video.get("video_duration_seconds") or 0)
            ratio = cut_plan_total / tts_total if tts_total > 0 else 0
            status = "pass"
            
            checks = {
                "has_tts_audio": tts_total > 0,
                "has_valid_cut_plan": cut_plan_total > 0,
                "no_negative_duration": all(float(c.get("duration_seconds") or 0) >= 0 for c in video.get("clips", [])),
            }
            if tts_total > 0 and cut_plan_total > 0:
                if ratio > 1.30:
                    status = "failed"
                    all_ok = False
                elif ratio > 1.20:
                    status = "warning"
                    
            videos_check.append({
                "short_video_id": video.get("short_video_id"),
                "status": status,
                "tts_total_duration_seconds": tts_total,
                "cut_plan_total_duration_seconds": cut_plan_total,
                "video_tts_ratio": round(ratio, 3),
                "checks": checks,
                "warnings": video.get("warnings", []),
            })
            
        return {
            "schema_version": "final_duration_check_v1",
            "ok": all_ok,
            "status": "pass" if all_ok else "failed",
            "effective_editing_source": effective_meta.get("editing_source", ""),
            "videos": videos_check,
        }

    def _trim_ai_voiceover_clips_to_tts_duration(
        self,
        clips: list[dict[str, Any]],
        *,
        voiceover_duration: float,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if (
            self.options.production_mode != "ai_voiceover"
            or not self.duration_settings.ai_voiceover_trim_video_to_tts
            or voiceover_duration <= 0
        ):
            return clips, {"trimmed": False, "reason": "disabled_or_no_voiceover"}

        tolerance = float(self.duration_settings.ai_voiceover_trim_tail_tolerance_seconds or 0.8)
        max_duration = voiceover_duration + tolerance

        trimmed: list[dict[str, Any]] = []
        cursor = 0.0
        cut_tail_seconds = 0.0

        for clip in clips:
            duration = float(clip.get("duration_seconds") or 0)
            if duration <= 0:
                continue

            if cursor >= max_duration:
                cut_tail_seconds += duration
                continue

            remain = max_duration - cursor
            new_clip = dict(clip)

            if duration > remain:
                source_start = timecode_to_seconds(new_clip.get("source_start"))
                new_duration = max(0.1, remain)
                new_clip["source_end"] = seconds_to_timecode(source_start + new_duration, ms=True)
                new_clip["duration_seconds"] = round(new_duration, 3)
                new_clip["target_end_seconds"] = round(cursor + new_duration, 3)
                cut_tail_seconds += duration - new_duration
                duration = new_duration
            else:
                new_clip["target_end_seconds"] = round(cursor + duration, 3)

            new_clip["target_start"] = seconds_to_timecode(cursor, ms=True)
            new_clip["target_start_seconds"] = round(cursor, 3)
            trimmed.append(new_clip)
            cursor += duration

        return trimmed, {
            "trimmed": bool(cut_tail_seconds > 0.001),
            "voiceover_duration_seconds": round(voiceover_duration, 3),
            "max_video_duration_seconds": round(max_duration, 3),
            "final_video_duration_seconds": round(cursor, 3),
            "cut_tail_seconds": round(cut_tail_seconds, 3),
        }

    def step_cut_plan(self) -> None:
        editing, effective_meta = self._load_effective_editing_for_cut_plan()
        voiceover_script = self._load_step_json("voiceover_script")
        tts = self._load_optional_step_json("tts", {"outputs": []})
        subtitles = self._load_optional_step_json("subtitles", {"outputs": []})
        if self.options.production_mode == "ai_voiceover":
            self._validate_ai_voiceover_inputs_before_cut_plan(editing, voiceover_script, tts)
        input_hash = stable_hash({
            "editing": self._step_content_hash("editing_script"),
            "effective_editing_source": effective_meta.get("editing_source"),
            "effective_editing_hash": stable_hash(editing),
            "tts": self._step_content_hash("tts"),
            "subtitles": self._step_content_hash("subtitles"),
            "aspect": self.options.aspect_ratio,
            "target_duration_seconds": self.options.target_duration_seconds,
            "allow_long_video": self.options.allow_long_video,
            "require_tts": self.options.require_tts,
            "voice_id": self.options.voice_id,
            "audio_policy": self.options.audio_policy,
            "allow_original_audio_evidence": self.options.allow_original_audio_evidence,
            "duration_mismatch_block_threshold_seconds": self.duration_settings.duration_mismatch_block_threshold_seconds,
            "ai_voiceover_compact_to_tts": self.duration_settings.ai_voiceover_compact_to_tts,
            "ai_voiceover_mismatch_policy": self.duration_settings.ai_voiceover_mismatch_policy,
            "ai_voiceover_min_ratio": self.duration_settings.ai_voiceover_min_ratio,
            "skip_compact_to_tts": effective_meta.get("prevent_double_compact", False),
        })
        if self._can_reuse("cut_plan", input_hash):
            print("复用缓存: cut_plan")
            return
        version, vdir = self._version_dir("cut_plan", "edit/cut_plan")
        ensure_dir(vdir)
        tts_by_id = {x["short_video_id"]: x for x in tts.get("outputs", [])}
        sub_by_id = {x["short_video_id"]: x for x in subtitles.get("outputs", [])}
        voice_by_id = {x.get("short_video_id"): x for x in voiceover_script.get("scripts", []) if isinstance(x, dict)}
        output_videos = []
        source_duration = self._current_source_duration_seconds()
        for script in editing.get("scripts", []):
            sid = script.get("short_video_id") or f"SV{len(output_videos)+1:03d}"
            clips = []
            target = 0.0
            validation = validate_editing_duration(script, allow_long_video=self.options.allow_long_video, settings=self.duration_settings)
            tts_item = tts_by_id.get(sid, {})
            voice_item = voice_by_id.get(sid, {})
            tts_success = tts_item.get("status") == "success" and bool(tts_item.get("voice_file_exists", True)) and bool(tts_item.get("file"))
            
            raw_blocked_reasons = list(validation.get("blocked_reasons", []))
            blocked_reasons: list[str] = []
            warnings: list[str] = []
            repair_reasons: list[str] = []

            for reason in raw_blocked_reasons:
                reason_text = str(reason)
                if (
                    self.options.production_mode == "ai_voiceover"
                    and "超过允许上限" in reason_text
                ):
                    warnings.append(reason_text + "；AI配音模式不再因最大成片时长阻断。")
                    repair_reasons.append("max_output_duration_warning_only")
                else:
                    blocked_reasons.append(reason_text)

            tts_is_hard_required = self.options.require_tts and self.options.audio_policy in {"ai_voiceover", "mixed"}
            if tts_is_hard_required and not tts_success:
                blocked_reasons.append("require_tts=true 且音频策略需要 AI 配音，但 AI 配音未成功生成")
            voiceover_duration = float(tts_item.get("actual_duration_seconds") or script.get("estimated_total_duration_seconds") or validation["video_duration_seconds"])
            raw_clips, invalid_clips = self._build_clips_from_editing_structure(script.get("editing_structure", []))
            
            if self.options.production_mode == "ai_voiceover" and self._is_virtual_source_manifest():
                missing_source_clips = [
                    clip for clip in raw_clips
                    if not str(clip.get("source_id") or "").strip()
                ]
                if missing_source_clips:
                    blocked_reasons.append(
                        "多源 AI 配音剪辑计划中存在 source_id 为空的画面片段，无法定位原视频；"
                        "请回查 content_analysis/short_video_edit_plan 的候选片段物化。"
                    )
                    cut_plan_debug = {
                        "short_video_id": sid,
                        "missing_source_id_clips": missing_source_clips[:20]
                    }
                    write_json(vdir / f"{sid}_cut_plan_debug.json", cut_plan_debug)

            cut_plan_debug = {
                "short_video_id": sid,
                "editing_structure_count": len(script.get("editing_structure", []) or []),
                "valid_clip_count": len(raw_clips),
                "invalid_clip_count": len(invalid_clips),
                "invalid_clips": invalid_clips[:20],
            }
            if not raw_clips:
                blocked_reasons.append("剪辑计划无有效画面片段：editing_structure 中没有可用 source_start/source_end")
                write_json(vdir / f"{sid}_cut_plan_debug.json", cut_plan_debug)
            for raw_clip in raw_clips:
                start = float(raw_clip.get("source_start_seconds") or timecode_to_seconds(raw_clip.get("source_start")) or 0)
                clip_duration = float(raw_clip.get("duration_seconds") or 0)
                normalized_raw_clips = self._normalize_reassembly_clip_to_source_boundaries(raw_clip, start, start + clip_duration)
                for normalized_raw_clip in normalized_raw_clips:
                    normalized_duration = float(normalized_raw_clip.get("duration_seconds") or clip_duration)
                    normalized_raw_clip["target_start"] = seconds_to_timecode(target, ms=True)
                    clip = normalize_audio_mode_for_policy(
                        normalized_raw_clip,
                        audio_policy=self.options.audio_policy,
                        settings=self.duration_settings,
                        allow_original_audio_evidence=self.options.allow_original_audio_evidence,
                    )
                    original_volume = original_audio_volume_for_clip(
                        clip,
                        audio_policy=self.options.audio_policy,
                        settings=self.duration_settings,
                        allow_original_audio_evidence=self.options.allow_original_audio_evidence,
                    )
                    clip["original_audio_volume"] = original_volume
                    clip["keep_original_audio"] = original_volume > 0
                    clips.append(clip)
                    target += normalized_duration
            video_duration = round(target, 3)
            original_visual_duration = video_duration
            tts_segments = tts_item.get("segments", []) if isinstance(tts_item.get("segments"), list) else []
            skip_compact_to_tts = effective_meta.get("prevent_double_compact", False)
            if (
                self.duration_settings.ai_voiceover_compact_to_tts
                and not skip_compact_to_tts
                and tts_success
                and tts_item.get("timeline_mode") == "compact_segmented"
                and tts_segments
            ):
                clips = self._compact_clips_to_voiceover_segments(clips, tts_segments, source_duration)
                if clips:
                    video_duration = round(
                        max(
                            timecode_to_seconds(clip.get("target_start")) + float(clip.get("duration_seconds") or 0)
                            for clip in clips
                        ),
                        3,
                    )
                    target = video_duration
                    if abs(original_visual_duration - video_duration) > 0.001:
                        repair_reasons.append("auto_compacted_to_tts")
                        warnings.append(
                            f"auto_compacted_to_tts: visual {original_visual_duration:.1f}s -> {video_duration:.1f}s"
                        )
            trim_to_tts_info = {"trimmed": False}
            if (
                self.options.production_mode == "ai_voiceover"
                and tts_success
                and self.duration_settings.ai_voiceover_trim_video_to_tts
            ):
                clips, trim_to_tts_info = self._trim_ai_voiceover_clips_to_tts_duration(
                    clips,
                    voiceover_duration=voiceover_duration,
                )
                if trim_to_tts_info.get("trimmed"):
                    repair_reasons.append("trimmed_video_to_tts_duration")
                    warnings.append(
                        f"trimmed_video_to_tts_duration: "
                        f"video -> {trim_to_tts_info.get('final_video_duration_seconds')}s, "
                        f"tts={trim_to_tts_info.get('voiceover_duration_seconds')}s"
                    )

                video_duration = round(
                    sum(float(clip.get("duration_seconds") or 0) for clip in clips),
                    3,
                )
                target = video_duration

            target_duration = float(validation["target_duration_seconds"] or 0)
            min_compact_duration = target_duration * self.duration_settings.compact_min_target_ratio
            if (
                tts_success
                and tts_item.get("timeline_mode") == "compact_segmented"
                and min_compact_duration > 0
            ):
                block_reason, warning_reason = self._compact_duration_block_reason(
                    video_duration=video_duration,
                    target_duration=target_duration,
                    min_compact_duration=min_compact_duration,
                )
                if warning_reason:
                    repair_reasons.append(warning_reason)
                    warnings.append(warning_reason)
                if block_reason and self.options.audio_policy != "original":
                    raise RuntimeError(
                        "最终成片时长未达到目标要求，已停止渲染。\n"
                        + block_reason
                        + "\n建议：重新运行“生成配音文案”，让 AI 配音稿更完整；如果素材信息不足，请合并更多片段或降低目标时长。"
                    )
            auto_extended_seconds = 0.0
            if self.options.audio_policy != "original" and tts_success and voiceover_duration > video_duration + 1.0:
                needed = round(voiceover_duration - video_duration, 3)
                if needed <= 6.0:
                    for clip in reversed(clips):
                        if clip.get("audio_mode") in {"original_sound", "mixed_evidence"}:
                            continue
                        current_end = timecode_to_seconds(clip.get("source_end"))
                        available = needed
                        if source_duration > 0:
                            available = min(available, max(0.0, source_duration - current_end))
                        if available <= 0:
                            continue
                        clip["source_end"] = seconds_to_timecode(current_end + available, ms=True)
                        clip["duration_seconds"] = round(float(clip.get("duration_seconds") or 0) + available, 3)
                        clip["auto_extended_for_voiceover_seconds"] = round(available, 3)
                        target += available
                        auto_extended_seconds = round(available, 3)
                        break
                    video_duration = round(target, 3)
            evidence_audio_checks_enabled = should_build_evidence_audio_windows(
                audio_policy=self.options.audio_policy,
                settings=self.duration_settings,
                allow_original_audio_evidence=self.options.allow_original_audio_evidence,
            )
            evidence_windows = evidence_audio_windows_from_clips(clips) if evidence_audio_checks_enabled else []
            voiceover_segments = voice_item.get("narration_segments", []) if isinstance(voice_item.get("narration_segments"), list) else []
            voiceover_windows = voiceover_active_windows_from_segments(tts_segments or voiceover_segments)
            overlap_seconds = voiceover_original_overlap_seconds(voiceover_windows, evidence_windows)
            voiceover_timeline_required = self.options.audio_policy != "original" and evidence_audio_checks_enabled and bool(evidence_windows)
            if voiceover_timeline_required and tts_success and tts_item.get("timeline_mode") != "segment_aligned":
                blocked_reasons.append("存在证据原声窗口，必须使用 segment_aligned 时间线 TTS，请从 tts 重跑")
            if evidence_windows and evidence_audio_checks_enabled:
                blocked_reasons.extend(validate_voiceover_silence_for_evidence(voiceover_segments, evidence_windows))
            if tts_item.get("timeline_mode") == "segment_aligned" and tts_segments:
                gap_issues = validate_voiceover_gaps(tts_segments, voiceover_duration, self.duration_settings)
                if voiceover_timeline_required:
                    blocked_reasons.extend(gap_issues)
                elif gap_issues:
                    warnings.extend(gap_issues)
            duration_delta = round(abs(video_duration - voiceover_duration), 3) if voiceover_duration else 0.0
            mismatch_reason = duration_mismatch_block_reason(
                video_duration=video_duration,
                voiceover_duration=voiceover_duration,
                audio_policy=self.options.audio_policy,
                tts_success=tts_success,
                threshold_seconds=self.duration_settings.duration_mismatch_block_threshold_seconds,
            )
            if mismatch_reason:
                block_ai_voiceover_mismatch = bool(
                    self.config.raw.get("voiceover", {}).get("ai_voiceover_block_duration_mismatch", True)
                )
                if self.options.production_mode == "ai_voiceover":
                    if block_ai_voiceover_mismatch:
                        blocked_reasons.append(mismatch_reason)
                    else:
                        warnings.append(mismatch_reason + "；配置允许 AI 配音总时长误差仅警告。")
                        repair_reasons.append("voiceover_duration_mismatch_warning_only")
                else:
                    blocked_reasons.append(mismatch_reason)

            if blocked_reasons:
                duration_status = "blocked"
            elif (
                self.options.production_mode != "ai_voiceover"
                and duration_delta > self.duration_settings.duration_mismatch_block_threshold_seconds
            ):
                duration_status = "mismatch"
            else:
                duration_status = "ok"
            voiceover_enabled = self.options.audio_policy != "original" and tts_success
            voiceover = {
                "enabled": voiceover_enabled,
                "required": self.options.require_tts,
                "file": tts_item.get("file", ""),
                "original_audio_volume": self.duration_settings.default_original_audio_volume,
                "voiceover_volume": self.duration_settings.voiceover_volume,
                "actual_duration_seconds": voiceover_duration if voiceover_enabled else 0.0,
                "timeline_mode": tts_item.get("timeline_mode", ""),
                "segments": tts_segments,
            }
            output_videos.append({
                "short_video_id": sid,
                "output_file": f"short_video_{sid.lower()}_draft.mp4",
                "aspect_ratio": self.options.aspect_ratio,
                "target_resolution": "1080x1920" if self.options.aspect_ratio == "9:16" else "1920x1080",
                "target_duration_seconds": validation["target_duration_seconds"],
                "max_allowed_seconds": validation["max_allowed_seconds"],
                "voiceover_duration_seconds": round(voiceover_duration, 3) if voiceover_enabled else 0.0,
                "video_duration_seconds": video_duration,
                "duration_delta_seconds": duration_delta,
                "duration_status": duration_status,
                "blocked_reasons": blocked_reasons,
                "repair_reasons": repair_reasons,
                "warnings": warnings,
                "auto_compacted_to_tts": bool(
                    self.duration_settings.ai_voiceover_compact_to_tts
                    and tts_success
                    and tts_item.get("timeline_mode") == "compact_segmented"
                    and tts_segments
                    and abs(original_visual_duration - video_duration) > 0.001
                ),
                "trim_to_tts": trim_to_tts_info,
                "original_visual_duration_seconds": round(original_visual_duration, 3),
                "compacted_visual_duration_seconds": video_duration,
                "cut_plan_debug": cut_plan_debug,
                "auto_extended_for_voiceover_seconds": auto_extended_seconds,
                "evidence_audio_windows": evidence_windows,
                "voiceover_timeline_required": voiceover_timeline_required,
                "voiceover_segments": voiceover_segments,
                "voiceover_original_overlap_seconds": overlap_seconds,
                "requires_user_long_video_confirmation": should_require_long_video_confirmation(video_duration, allow_long_video=self.options.allow_long_video, settings=self.duration_settings),
                "audio_policy": self._effective_output_audio_policy(tts_success),
                "allow_original_audio_evidence": self.options.allow_original_audio_evidence,
                "clips": clips,
                "voiceover": voiceover,
                "subtitles": {"file": sub_by_id.get(sid, {}).get("file", ""), "burn_in": bool(sub_by_id.get(sid))},
                "cover": {"main_text": script.get("cover_text", ""), "sub_text": script.get("title", "")},
            })
        plan = {"project_id": self.task_id, "source_video": self.manifest["source_video"], "output_videos": output_videos}
        
        final_duration_check = self._build_final_duration_check(output_videos, effective_meta)
        write_json(vdir / "final_duration_check.json", final_duration_check)
        if not final_duration_check.get("ok"):
            self.manifest["action_required"] = {
                "type": "final_duration_check_failed",
                "message": "Cut plan generated but failed final duration check (e.g. video excessively long relative to TTS).",
                "path": relpath(vdir / "final_duration_check.json", self.task_dir),
            }
            self._save_manifest()
            raise UserFacingPipelineError(
                "final_duration_check_failed",
                user_message="最终检查失败：成片剪辑时长与 AI 配音时长差距过大。",
                suggestions=["查看 final_duration_check.json 分析原因", "检查 adjusted_editing_script 是否正确生成并生效"],
            )
            
        for video in output_videos:
            audio_check = validate_audio_policy(
                video,
                require_tts=self.options.require_tts and self.options.audio_policy in {"ai_voiceover", "mixed"},
                settings=self.duration_settings,
            )
            if audio_check["issues"]:
                video.setdefault("blocked_reasons", []).extend(audio_check["issues"])
                video["duration_status"] = "blocked"
        renderable_videos = [video for video in output_videos if video.get("duration_status") != "blocked"]
        self._validate_ai_voiceover_coverage_before_render(renderable_videos)
        renderable_total_seconds = round(sum(float(video.get("video_duration_seconds") or 0) for video in renderable_videos), 3)
        if (
            source_duration >= self.duration_settings.long_source_threshold_seconds
            and renderable_total_seconds < self.duration_settings.long_source_min_total_output_seconds
        ):
            plan["coverage_warning"] = {
                "source_duration_seconds": round(source_duration, 3),
                "renderable_total_seconds": renderable_total_seconds,
                "expected_min_total_seconds": self.duration_settings.long_source_min_total_output_seconds,
                "preferred_total_seconds": self.duration_settings.long_source_preferred_total_output_seconds,
                "expected_min_video_count": self.duration_settings.long_source_min_video_count,
                "suggestion": "rerun planning/voiceover with more output coverage",
            }
        self._validate_cut_plan_not_empty_or_raise(plan)
        out = write_json(vdir / "cut_plan.json", plan)
        has_blocked = any(v.get("duration_status") == "blocked" for v in output_videos)
        renderable_count = sum(1 for v in output_videos if v.get("duration_status") != "blocked")
        status = "success"
        if has_blocked:
            status = "partial_success" if renderable_count else "failed"
        blocked_summary = []
        for video in output_videos:
            if video.get("duration_status") != "blocked":
                continue
            sid = video.get("short_video_id") or "unknown"
            reasons = [str(reason) for reason in video.get("blocked_reasons", []) if reason]
            if reasons:
                blocked_summary.append(f"{sid}: {' | '.join(reasons[:3])}")
            else:
                blocked_summary.append(f"{sid}: cut_plan blocked without a detailed reason")
        blocked_error = " | ".join(blocked_summary[:5])
        status_doc = self._base_status("cut_plan", version, input_hash, [out])
        status_doc["status"] = status
        if blocked_summary:
            status_doc["error"] = blocked_error
            status_doc["blocked_summary"] = blocked_summary
        extra = {"error": blocked_error, "blocked_summary": blocked_summary} if blocked_summary else None
        self._write_status(vdir, status_doc)
        self._record_step(step="cut_plan", version=version, status=status, output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out], extra=extra)
        print(f"完成: cut_plan ({status})")
        if status == "failed":
            detail = f": {blocked_error}" if blocked_error else ""
            raise RuntimeError(f"cut_plan blocked by duration/TTS/audio policy; check cut_plan.json{detail}")

    def _current_source_duration_seconds(self) -> float:
        if self._is_virtual_source_manifest():
            aggregate = self._load_optional_step_json("source_aggregate", {})
            digest = aggregate.get("timeline_digest", {}) if isinstance(aggregate, dict) else {}
            for key in ("video_duration_seconds", "total_source_duration_seconds", "duration_seconds"):
                duration = float(digest.get(key) or 0)
                if duration > 0:
                    return duration
            try:
                source_manifest = read_json(self._source_manifest_path(), {})
                for key in ("total_duration_seconds", "total_source_duration_seconds", "duration_seconds"):
                    duration = float(source_manifest.get(key) or 0)
                    if duration > 0:
                        return duration
                sources = source_manifest.get("sources") or []
                total = sum(float(item.get("duration_seconds") or 0) for item in sources if isinstance(item, dict))
                if total > 0:
                    return total
                return 0.0
            except Exception:
                return 0.0
        try:
            return float(ffprobe_json(self._source_video()).get("duration") or 0)
        except Exception:
            return 0.0

    def _compact_clips_to_voiceover_segments(
        self,
        clips: list[dict[str, Any]],
        tts_segments: list[dict[str, Any]],
        source_duration: float = 0.0,
    ) -> list[dict[str, Any]]:
        active_by_shot_id = {
            str(segment.get("shot_id")): segment
            for segment in tts_segments
            if segment.get("shot_id")
            and segment.get("status") == "success"
            and float(segment.get("actual_duration_seconds") or 0) > 0
        }
        if not clips or not active_by_shot_id:
            return clips
        compacted: list[dict[str, Any]] = []
        cursor = 0.0
        gap = max(0.0, float(self.duration_settings.inter_sentence_gap_seconds or 0))
        production_mode = getattr(self.options, "production_mode", "normal")
        for clip in clips:
            shot_id = str(clip.get("shot_id") or clip.get("source_shot_id") or "")
            segment = active_by_shot_id.get(shot_id)
            if not segment:
                if (
                    production_mode == "ai_voiceover"
                    and self.duration_settings.ai_voiceover_drop_unmatched_clips
                ):
                    continue
                duration = float(clip.get("duration_seconds") or 0)
                if duration <= 0:
                    continue
                new_clip = dict(clip)
                new_clip["target_start"] = seconds_to_timecode(cursor, ms=True)
                new_clip["target_start_seconds"] = round(cursor, 3)
                new_clip["target_end_seconds"] = round(cursor + duration, 3)
                new_clip["compact_voiceover_timed"] = False
                new_clip["compact_voiceover_unmatched"] = True
                compacted.append(new_clip)
                cursor += duration
                continue
            duration, duration_reason = self._resolve_final_clip_duration(clip, segment)
            if duration <= 0:
                continue
            source_start = timecode_to_seconds(clip.get("source_start"))
            source_end = source_start + duration
            source_max_end = float(clip.get("source_max_end_seconds") or timecode_to_seconds(clip.get("source_end")) or 0)
            if source_max_end > source_start:
                source_end = min(source_end, source_max_end)
                duration = max(0.1, source_end - source_start)
            if source_duration > 0:
                source_end = min(source_end, source_duration)
                duration = max(0.1, source_end - source_start)
            new_clip = dict(clip)
            new_clip["target_start"] = seconds_to_timecode(cursor, ms=True)
            new_clip["target_start_seconds"] = round(cursor, 3)
            new_clip["target_end_seconds"] = round(cursor + duration, 3)
            new_clip["source_end"] = seconds_to_timecode(source_end, ms=True)
            new_clip["duration_seconds"] = round(duration, 3)
            new_clip["compact_voiceover_timed"] = True
            new_clip["compact_voiceover_duration_reason"] = duration_reason
            new_clip["tts_actual_duration_seconds"] = round(float(segment.get("actual_duration_seconds") or 0), 3)
            new_clip["voiceover_segment_shot_id"] = shot_id
            compacted.append(new_clip)
            cursor += duration
            if len(compacted) < len(clips):
                cursor += gap
        return compacted or clips

    def _resolve_final_clip_duration(self, shot: dict[str, Any], tts_seg: dict[str, Any]) -> tuple[float, str]:
        target = float(shot.get("target_duration_seconds") or shot.get("duration_seconds") or 0)
        actual = float(tts_seg.get("actual_duration_seconds") or 0)
        has_contract_bounds = shot.get("min_duration_seconds") not in (None, "") or shot.get("max_duration_seconds") not in (None, "")
        if not has_contract_bounds:
            if actual > 0:
                return actual, "use_tts_duration"
            return max(0.0, target), "fallback_target"
        min_d = float(shot.get("min_duration_seconds") or (target * 0.65 if target else 0))
        max_d = float(shot.get("max_duration_seconds") or (target * 1.35 if target else 0))
        if target <= 0:
            target = actual
        if min_d <= 0:
            min_d = target
        if max_d <= 0:
            max_d = target
        if actual <= 0:
            return max(0.0, target), "fallback_target"
        if min_d <= actual <= max_d:
            return actual, "use_tts_duration"
        if actual < min_d:
            return min_d, "tts_too_short_clamped"
        return max_d, "tts_too_long_clamped"

    def step_reassembly_cut_plan(self) -> None:
        plan = self._load_step_json("highlight_reassembly_plan")
        pool = self._load_candidate_clip_pool(filtered=True)
        input_hash = stable_hash({
            "reassembly_cut_plan": self._step_content_hash("highlight_reassembly_plan"),
            "candidate_filter": self._step_content_hash("candidate_filter"),
            "aspect": self.options.aspect_ratio,
            "reassembly_options": self._reassembly_options_payload(),
        })
        if self._can_reuse("reassembly_cut_plan", input_hash):
            print("reuse cache: reassembly_cut_plan")
            return
        version, vdir = self._version_dir("reassembly_cut_plan", "edit/reassembly_cut_plan")
        ensure_dir(vdir)
        if not pool:
            pool = self._legacy_candidate_pool_from_reassembly_plan(plan)
        pool_by_id = {str(clip.get("clip_id")): clip for clip in pool if clip.get("clip_id")}
        diagnostics: list[dict[str, Any]] = []
        raw_pool: list[dict[str, Any]] | None = None
        reassembly_cfg = self.config.raw.get("reassembly", {}) if hasattr(self, "config") else {}
        recover_missing_clip_ids = bool(reassembly_cfg.get("recover_missing_clip_ids", True))
        soft_min_clip_seconds = float(reassembly_cfg.get("soft_min_clip_seconds", 5.0))
        output_videos = []
        for item in plan.get("output_videos", []):
            if not isinstance(item, dict):
                continue
            rid = (
                item.get("reassembly_id")
                or item.get("video_id")
                or f"hr_{len(output_videos) + 1:03d}"
            )
            clips = []
            warnings: list[str] = []
            blocked_reasons: list[str] = []
            target = 0.0
            planned_clip_ids = self._planned_reassembly_clip_ids(item)
            if not planned_clip_ids:
                blocked_reasons.append("plan_empty")
                diagnostics.append(self._reassembly_error(
                    "plan_empty",
                    reason="高光重组规划没有选择任何 clip_id",
                    suggestion="重跑 highlight_reassembly_plan，或检查规划 prompt 是否过于严格",
                    candidate_pool_count=len(self._load_candidate_clip_pool(filtered=False)),
                    filtered_pool_count=len(pool),
                ))
            missing_clip_ids = [cid for cid in planned_clip_ids if cid not in pool_by_id]
            if missing_clip_ids and recover_missing_clip_ids:
                raw_pool = raw_pool if raw_pool is not None else self._load_candidate_clip_pool(filtered=False)
                raw_by_id = {
                    self._clip_id(clip, index): clip
                    for index, clip in enumerate(raw_pool)
                    if isinstance(clip, dict)
                }
                recovered_clip_ids: list[str] = []
                for cid in missing_clip_ids:
                    candidate = raw_by_id.get(cid)
                    if candidate:
                        pool_by_id[cid] = candidate
                        recovered_clip_ids.append(cid)
                if recovered_clip_ids:
                    warnings.append(f"recovered missing clips from raw candidate pool: {recovered_clip_ids}")
                missing_clip_ids = [cid for cid in missing_clip_ids if cid not in pool_by_id]
            if missing_clip_ids:
                blocked_reasons.append("clip_id_not_found")
                diagnostics.append(self._reassembly_error(
                    "clip_id_not_found",
                    reason="模型选择了不存在的 clip_id",
                    suggestion="重跑高光重组规划，或检查 candidate_filter 输出和候选池映射",
                    candidate_pool_count=len(self._load_candidate_clip_pool(filtered=False)),
                    filtered_pool_count=len(pool),
                    planned_clip_ids=planned_clip_ids,
                    matched_clip_ids=[cid for cid in planned_clip_ids if cid in pool_by_id],
                    missing_clip_ids=missing_clip_ids,
                ))
            for idx, source_clip_id in enumerate(planned_clip_ids, start=1):
                candidate = pool_by_id.get(source_clip_id)
                if not candidate:
                    continue
                start = float(candidate.get("local_start_seconds", candidate.get("source_start_seconds", candidate.get("start_seconds", 0))) or 0)
                end = float(candidate.get("local_end_seconds", candidate.get("source_end_seconds", candidate.get("end_seconds", 0))) or 0)
                if end <= start:
                    warnings.append(f"{source_clip_id}: invalid source time range")
                    diagnostics.append(self._reassembly_error(
                        "clip_time_invalid",
                        reason="候选池里的 clip 时间无效",
                        suggestion="回查 ASR 事件候选构造和 candidate_clip_pool_build",
                        invalid_clips=[{"clip_id": source_clip_id, "reason": "source_end <= source_start"}],
                    ))
                    continue
                duration = end - start
                if duration < self.options.reassembly_min_clip_seconds:
                    warnings.append(f"{source_clip_id}: shorter than hard min clip seconds")
                    continue
                if duration < soft_min_clip_seconds:
                    warnings.append(f"{source_clip_id}: short_original_audio_kept_with_warning")
                if duration > self.options.reassembly_max_clip_seconds:
                    end = start + self.options.reassembly_max_clip_seconds
                    duration = self.options.reassembly_max_clip_seconds
                    warnings.append(f"{source_clip_id}: truncated to max clip seconds")
                clips.append({
                    "clip_id": f"{rid}_{len(clips) + 1:03d}",
                    "source_clip_id": source_clip_id,
                    "source_id": candidate.get("source_id", ""),
                    "source_index": candidate.get("source_index"),
                    "local_start": seconds_to_timecode(start, ms=True),
                    "local_end": seconds_to_timecode(end, ms=True),
                    "local_start_seconds": round(start, 3),
                    "local_end_seconds": round(end, 3),
                    "source_start": seconds_to_timecode(start, ms=True),
                    "source_end": seconds_to_timecode(end, ms=True),
                    "source_start_seconds": round(start, 3),
                    "source_end_seconds": round(end, 3),
                    "target_start": seconds_to_timecode(target, ms=True),
                    "target_start_seconds": round(target, 3),
                    "duration_seconds": round(duration, 3),
                    "original_audio_volume": 1.0,
                    "keep_original_audio": True,
                    "role": candidate.get("event_type") or candidate.get("type") or candidate.get("clip_type") or "",
                    "selection_reason": candidate.get("summary") or candidate.get("event_summary") or "",
                    "boundary_reason": "",
                    "risk_level": candidate.get("risk_level", ""),
                    "risk_notes": candidate.get("risk_notes", ""),
                    "transition_after": "hard_cut",
                    "crop_mode": "fit_blur" if self.options.aspect_ratio == "9:16" else "original",
                })
                target += duration
                if len(clips) >= self.options.reassembly_max_clip_count:
                    break
            if not clips:
                if not blocked_reasons:
                    blocked_reasons.append("all_clips_filtered")
                    diagnostics.append(self._reassembly_error(
                        "all_clips_filtered",
                        reason="所有片段低于最短时长或源文件不可用",
                        suggestion="检查最短时长规则、源文件路径、候选池时间字段",
                        candidate_pool_count=len(pool),
                        planned_clip_ids=planned_clip_ids,
                    ))
            output_videos.append({
                "reassembly_id": rid,
                "output_file": f"highlight_reassembly_{rid}_draft.mp4",
                "title": item.get("title", ""),
                "output_type": item.get("output_type", self.options.reassembly_output_mode),
                "aspect_ratio": self.options.aspect_ratio,
                "target_resolution": "1080x1920" if self.options.aspect_ratio == "9:16" else "1920x1080",
                "audio_policy": "original_audio",
                "target_duration_seconds": item.get("target_duration_seconds") or self.options.reassembly_target_seconds,
                "video_duration_seconds": round(target, 3),
                "duration_status": "blocked" if blocked_reasons else "ok",
                "risk_review_required": bool(item.get("risk_review_required")),
                "context_integrity_check": item.get("context_integrity_check", {}),
                "clips": clips,
                "warnings": warnings,
                "blocked_reasons": blocked_reasons,
            })
        if not output_videos:
            diagnostics.append(self._reassembly_error(
                "plan_empty",
                reason="高光重组规划没有 output_videos",
                suggestion="重跑 highlight_reassembly_plan",
                candidate_pool_count=len(self._load_candidate_clip_pool(filtered=False)),
                filtered_pool_count=len(pool),
            ))
            output_videos.append({
                "reassembly_id": "hr_001",
                "output_file": "highlight_reassembly_hr_001_draft.mp4",
                "title": "",
                "output_type": self.options.reassembly_output_mode,
                "aspect_ratio": self.options.aspect_ratio,
                "target_resolution": "1080x1920" if self.options.aspect_ratio == "9:16" else "1920x1080",
                "audio_policy": "original_audio",
                "target_duration_seconds": self.options.reassembly_target_seconds,
                "video_duration_seconds": 0,
                "duration_status": "blocked",
                "risk_review_required": False,
                "context_integrity_check": {},
                "clips": [],
                "warnings": [],
                "blocked_reasons": ["highlight_reassembly_plan has no output_videos"],
            })
        out = write_json(vdir / "reassembly_cut_plan.json", {
            "project_id": self.task_id,
            "production_mode": "highlight_reassembly",
            "source_video": self.manifest["source_video"],
            "output_videos": output_videos,
            "diagnostics": diagnostics,
        })
        renderable_count = sum(1 for video in output_videos if video.get("duration_status") != "blocked")
        blocked_count = len(output_videos) - renderable_count
        status = "success" if blocked_count == 0 else ("partial_success" if renderable_count else "failed")
        status_doc = self._base_status("reassembly_cut_plan", version, input_hash, [out])
        status_doc["status"] = status
        
        if status == "failed":
            primary_error = diagnostics[0] if diagnostics else self._reassembly_error(
                "all_clips_filtered",
                reason="没有找到可用于剪辑的片段",
                suggestion="从候选生成、候选复核、重组规划依次检查",
            )
            status_doc["user_error"] = {
                "title": "生成重组剪辑计划失败",
                "message": primary_error.get("reason", "没有找到可用于剪辑的片段。"),
                "suggestions": [
                    primary_error.get("suggestion", "从“规划高光重组”重新运行。"),
                ],
            }
            status_doc["technical_error"] = {
                **primary_error,
                "diagnostics": diagnostics,
                "renderable_count": renderable_count,
                "blocked_count": blocked_count,
                "blocked_reasons": [
                    reason
                    for video in output_videos
                    for reason in video.get("blocked_reasons", [])
                ],
                "warnings": [
                    warning
                    for video in output_videos
                    for warning in video.get("warnings", [])
                ],
            }

        self._write_status(vdir, status_doc)
        self._record_step(step="reassembly_cut_plan", version=version, status=status, output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out])
        print(f"completed: reassembly_cut_plan ({status})")
        
        if status == "failed":
            raise UserFacingPipelineError(
                status_doc["technical_error"].get("error_type", "reassembly_cut_plan_failed"),
                user_message=f"生成重组剪辑计划失败：{status_doc['technical_error'].get('reason', '没有找到可用于剪辑的片段。')}",
                suggestions=[
                    status_doc["technical_error"].get("suggestion", "从候选生成、候选复核、重组规划依次检查。"),
                ],
                technical_detail=status_doc["technical_error"],
            )

    def _planned_reassembly_clip_ids(self, item: dict[str, Any]) -> list[str]:
        clip_ids = self._coerce_id_list(item.get("clip_ids") or item.get("source_clip_ids"))
        if clip_ids:
            return clip_ids
        selected = (
            item.get("selected_clips")
            or item.get("clips")
            or item.get("clip_sequence")
            or item.get("segments")
            or []
        )
        out: list[str] = []
        if isinstance(selected, list):
            for clip in selected:
                if isinstance(clip, dict):
                    cid = str(clip.get("source_clip_id") or clip.get("clip_id") or clip.get("id") or "").strip()
                else:
                    cid = str(clip).strip()
                if cid and cid not in out:
                    out.append(cid)
        return out

    def _legacy_candidate_pool_from_reassembly_plan(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        pool: list[dict[str, Any]] = []
        for video in plan.get("output_videos", []) or []:
            if not isinstance(video, dict):
                continue
            selected = (
                video.get("selected_clips")
                or video.get("clips")
                or video.get("clip_sequence")
                or video.get("segments")
                or []
            )
            if not isinstance(selected, list):
                continue
            for index, clip in enumerate(selected, start=1):
                if not isinstance(clip, dict):
                    continue
                cid = str(clip.get("source_clip_id") or clip.get("clip_id") or clip.get("id") or f"legacy_clip_{len(pool) + 1:03d}")
                start = self._clip_time_seconds(clip, "adjusted_start", "source_start", "start", "start_time")
                end = self._clip_time_seconds(clip, "adjusted_end", "source_end", "end", "end_time")
                if end <= start and clip.get("duration_seconds"):
                    end = start + float(clip.get("duration_seconds") or 0)
                pool.append({
                    "clip_id": cid,
                    "source_id": clip.get("source_id", "source_1"),
                    "source_index": clip.get("source_index"),
                    "source_start_seconds": round(start, 3),
                    "source_end_seconds": round(end, 3),
                    "source_start": seconds_to_timecode(start, ms=True),
                    "source_end": seconds_to_timecode(end, ms=True),
                    "duration_seconds": round(max(0.0, end - start), 3),
                    "summary": clip.get("selection_reason") or clip.get("role") or "",
                    "event_type": clip.get("role", ""),
                })
        return pool

    def _reassembly_error(self, error_type: str, **fields: Any) -> dict[str, Any]:
        defaults = {
            "candidate_pool_empty": ("ASR 事件候选池为空", "降低候选事件阈值，或检查 ASR 文本质量"),
            "candidate_filter_empty": ("候选复核后没有保留任何 clip", "放宽 candidate_filter，或允许保留 medium 质量片段"),
            "plan_empty": ("高光重组规划没有选择任何 clip_id", "重跑 highlight_reassembly_plan，或检查规划 prompt 是否过于严格"),
            "clip_id_not_found": ("模型选择了不存在的 clip_id", "重跑高光重组规划，或检查 candidate_filter 输出和候选池映射"),
            "clip_time_invalid": ("候选池里的 clip 时间无效", "回查 ASR 事件候选构造和 candidate_clip_pool_build"),
            "all_clips_filtered": ("所有 clip 被剪辑规则过滤", "检查最短时长规则、源文件路径、候选池时间字段"),
            "quality_gate_failed": ("本地质量检查未通过", "回查 reassembly_cut_plan 或 candidate_clip_pool_filtered"),
        }
        reason, suggestion = defaults.get(error_type, ("重组链路失败", "请检查上游步骤输出"))
        data = {"error_type": error_type, "reason": reason, "suggestion": suggestion}
        data.update({k: v for k, v in fields.items() if v is not None})
        return data

    def step_news_quality_gate(self) -> None:
        source_step = "reassembly_cut_plan" if self.options.production_mode == "highlight_reassembly" else "cut_plan"
        plan = self._load_step_json(source_step)
        input_hash = stable_hash({"source_step": source_step, "source_hash": self._step_content_hash(source_step)})
        if self._can_reuse("news_quality_gate", input_hash):
            print("reuse cache: news_quality_gate")
            return
        version, vdir = self._version_dir("news_quality_gate", "quality/news_quality_gate")
        ensure_dir(vdir)
        output_videos = [v for v in plan.get("output_videos", []) if isinstance(v, dict)]
        issues: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        total_duration = 0.0
        clip_count = 0
        if not output_videos:
            issues.append({"type": "missing_output_videos", "message": "没有 output_videos"})
        seen_source_clip_ids: set[str] = set()
        for video in output_videos:
            clips = [c for c in video.get("clips", []) if isinstance(c, dict)]
            if not clips:
                issues.append({"type": "missing_clips", "video_id": video.get("reassembly_id") or video.get("short_video_id"), "message": "视频没有 clips"})
            for clip in clips:
                clip_count += 1
                cid = str(clip.get("clip_id") or clip.get("source_clip_id") or "")
                source_clip_id = str(clip.get("source_clip_id") or cid)
                if source_clip_id in seen_source_clip_ids:
                    warnings.append({"type": "duplicate_clip", "clip_id": cid, "message": "存在重复使用的 source clip"})
                seen_source_clip_ids.add(source_clip_id)
                for key in ("source_id", "source_start", "source_end"):
                    if not clip.get(key):
                        issues.append({"type": f"missing_{key}", "clip_id": cid, "message": f"clip 缺少 {key}"})
                start = float(clip.get("source_start_seconds", timecode_to_seconds(clip.get("source_start"))) or 0)
                end = float(clip.get("source_end_seconds", timecode_to_seconds(clip.get("source_end"))) or 0)
                duration = float(clip.get("duration_seconds") or max(0.0, end - start))
                total_duration += max(0.0, duration)
                if end <= start:
                    issues.append({"type": "invalid_source_time", "clip_id": cid, "message": "source_end <= source_start"})
                if duration <= 0:
                    issues.append({"type": "invalid_duration", "clip_id": cid, "message": "duration_seconds <= 0"})
                if self.options.production_mode == "highlight_reassembly" and clip.get("keep_original_audio") is not True:
                    issues.append({"type": "missing_original_audio", "clip_id": cid, "message": "原声重组 clip 未明确保留原声"})
        if clip_count and total_duration < max(1.0, float(getattr(self.options, "reassembly_min_clip_seconds", 5.0))):
            warnings.append({"type": "total_duration_short", "message": "总时长过短"})
        ok = not issues
        output = {
            "ok": ok,
            "block_render": not ok,
            "issues": issues,
            "warnings": warnings,
            "stats": {
                "output_video_count": len(output_videos),
                "clip_count": clip_count,
                "total_duration_seconds": round(total_duration, 3),
            },
            "source_step": source_step,
        }

        if not ok:
            output["diagnostic_error"] = self._reassembly_error("quality_gate_failed", issues=issues)
            if self.options.production_mode == "ai_voiceover":
                output["diagnostic_error"]["suggestion"] = "回查 ai_voiceover_candidates、short_video_edit_plan、voiceover_script、cut_plan"
        out = write_json(vdir / "news_quality_gate.json", output)
        status = "success" if ok else "failed"
        status_doc = self._base_status("news_quality_gate", version, input_hash, [out])
        status_doc["status"] = status
        if not ok:
            status_doc["technical_error"] = output["diagnostic_error"]
        self._write_status(vdir, status_doc)
        self._record_step(step="news_quality_gate", version=version, status=status, output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out])
        print(f"completed: news_quality_gate ({status})")
        if not ok:
            suggestions = ["回查 reassembly_cut_plan 或 candidate_clip_pool_filtered。"]
            if self.options.production_mode == "ai_voiceover":
                suggestions = ["回查 ai_voiceover_candidates、short_video_edit_plan、voiceover_script、cut_plan。"]
            raise UserFacingPipelineError(
                "quality_gate_failed",
                user_message="本地质量检查未通过：剪辑计划存在缺字段或无效时间。",
                suggestions=suggestions,
                technical_detail=output["diagnostic_error"],
            )

    def step_news_quality_ai_review(self) -> None:
        gate = self._load_step_json("news_quality_gate")
        source_step = "reassembly_cut_plan" if self.options.production_mode == "highlight_reassembly" else "cut_plan"
        plan = self._load_step_json(source_step)
        payload = {
            "production_mode": self.options.production_mode,
            "quality_gate": gate,
            "plan_summary": {
                "output_video_count": len(plan.get("output_videos", []) or []),
                "output_videos": [
                    {
                        "id": video.get("reassembly_id") or video.get("short_video_id"),
                        "title": video.get("title", ""),
                        "clips": [
                            {
                                "source_clip_id": clip.get("source_clip_id"),
                                "source_id": clip.get("source_id"),
                                "duration_seconds": clip.get("duration_seconds"),
                                "selection_reason": clip.get("selection_reason", ""),
                            }
                            for clip in (video.get("clips") or [])[:10]
                            if isinstance(clip, dict)
                        ],
                    }
                    for video in (plan.get("output_videos") or [])[:5]
                    if isinstance(video, dict)
                ],
            },
        }
        result = self._run_text_agent("news_quality_ai_review", payload)
        if isinstance(result, dict):
            result.setdefault("quality_gate_ok", bool(gate.get("ok")))
            self._overwrite_step_json("news_quality_ai_review", result, materialized=True)

    def step_reassembly_render(self) -> None:
        render_slots = int(self.config.raw.get("gpu_limits", {}).get("render_slots", 1))
        with file_slot_lock("render", slots=render_slots):
            self._step_reassembly_render_impl()

    def _step_reassembly_render_impl(self) -> None:
        plan = self._load_step_json("reassembly_cut_plan")
        input_hash = stable_hash({"reassembly_cut_plan": self._step_content_hash("reassembly_cut_plan")})
        if self._can_reuse("reassembly_render", input_hash):
            print("reuse cache: reassembly_render")
            return
        version, base_dir = self._version_dir("reassembly_render", "edit/reassembly_drafts")
        outputs = []
        skipped_outputs = []
        for video in plan.get("output_videos", []):
            rid = video.get("reassembly_id") or f"hr_{len(outputs) + 1:03d}"
            if video.get("duration_status") == "blocked":
                skipped_outputs.append({
                    "reassembly_id": rid,
                    "status": "skipped",
                    "reason": "reassembly_cut_plan_blocked",
                    "blocked_reasons": video.get("blocked_reasons", []),
                })
                continue
            vdir = ensure_dir(base_dir / rid)
            out = vdir / video.get("output_file", f"highlight_reassembly_{rid}_draft.mp4")
            self._render_reassembly_one(video, out)
            quality_check = self._reassembly_quality_check(video, out)
            write_json(vdir / "final_quality_check.json", quality_check)
            self._write_status(vdir, {**self._base_status("reassembly_render", version, stable_hash(video), [out]), "quality_check": quality_check})
            outputs.append({"reassembly_id": rid, "file": relpath(out, self.task_dir), "quality_check": quality_check})
        out_index = write_json(base_dir / "reassembly_render_outputs.json", {"version": version, "outputs": outputs, "skipped_outputs": skipped_outputs})
        status = "success"
        if skipped_outputs:
            status = "partial_success" if outputs else "failed"
        self._record_step(step="reassembly_render", version=version, status=status, output=relpath(out_index, self.task_dir), input_hash=input_hash, output_files=[out_index], extra={"skipped_outputs": skipped_outputs} if skipped_outputs else None)
        print(f"completed: reassembly_render ({status})")
        if status == "failed":
            raise RuntimeError("reassembly_render has no available videos")

    def _normalize_reassembly_clip_to_source_boundaries(
        self,
        clip: dict[str, Any],
        start: float,
        end: float,
    ) -> list[dict[str, Any]]:
        if not self._is_virtual_source_manifest():
            return [dict(clip, _normalized_start=start, _normalized_end=end)]

        if not clip.get("source_id"):
            return []

        local_start = float(clip.get("local_start_seconds", start) or start)
        local_end = float(clip.get("local_end_seconds", end) or end)
        new_clip = dict(clip)
        new_clip["_normalized_start"] = local_start
        new_clip["_normalized_end"] = local_end
        new_clip["local_start_seconds"] = round(local_start, 3)
        new_clip["local_end_seconds"] = round(local_end, 3)
        new_clip["local_start"] = seconds_to_timecode(local_start, ms=True)
        new_clip["local_end"] = seconds_to_timecode(local_end, ms=True)
        new_clip.setdefault("source_start", new_clip["local_start"])
        new_clip.setdefault("source_end", new_clip["local_end"])
        new_clip["duration_seconds"] = round(max(0.0, local_end - local_start), 3)
        return [new_clip]

    def _resolve_reassembly_clip_source(self, clip: dict[str, Any]) -> tuple[Path, float, float]:
        start = float(clip.get("local_start_seconds") or timecode_to_seconds(clip.get("local_start")) or timecode_to_seconds(clip.get("source_start")) or 0)
        end = float(clip.get("local_end_seconds") or timecode_to_seconds(clip.get("local_end")) or timecode_to_seconds(clip.get("source_end")) or 0)
        if end <= start:
            raise RuntimeError(f"invalid reassembly clip time range: {clip.get('clip_id') or ''}")

        if self._is_virtual_source_manifest():
            source_id = clip.get("source_id")
            for item in self._iter_source_items():
                if source_id and item.get("source_id") != source_id:
                    continue
                duration = float(item.get("duration_seconds") or item.get("original_duration_seconds") or 0)
                if duration and end > duration + 0.05:
                    break
                source_path = self._resolve_source_item_path(item)
                return source_path, start, end
            raise RuntimeError(
                "视频重组渲染失败：片段无效，source_id 不存在，或 local_start/local_end 超出该源视频时长。\n"
                f"片段：{clip.get('clip_id') or ''} {start:.3f}-{end:.3f}\n"
                f"source_id：{clip.get('source_id') or ''}"
            )

        manifest_value = str(self.manifest.get("source_manifest") or "").strip()
        source_manifest_path = Path(manifest_value)
        if not source_manifest_path.is_absolute():
            source_manifest_path = self.task_dir / source_manifest_path
        source_manifest = read_json(source_manifest_path, {})
        if source_manifest.get("source_mode") != "multi_source_concat_proxy":
            return self._source_video(), start, end

        raise RuntimeError(
            "视频重组渲染失败：legacy concat proxy 任务不再支持虚拟时间转换，请重新运行多源任务。\n"
            f"片段：{clip.get('clip_id') or ''} {clip.get('source_start')} - {clip.get('source_end')}"
        )

    def _render_reassembly_one(self, video: dict[str, Any], output_path: Path) -> None:
        temp_dir = ensure_dir(output_path.parent / "_clips")
        clip_files = []
        for idx, clip in enumerate(video.get("clips", []), start=1):
            source, local_start, local_end = self._resolve_reassembly_clip_source(clip)
            clip_file = temp_dir / f"{video['reassembly_id']}_{idx:03d}.mp4"
            run_cmd([
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                seconds_to_timecode(local_start, ms=True),
                "-to",
                seconds_to_timecode(local_end, ms=True),
                "-i",
                str(source),
                "-vf",
                self._video_filter(video),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-af",
                "volume=1.0",
                str(clip_file),
            ])
            clip_files.append(clip_file)
        if not clip_files:
            raise RuntimeError(f"{video['reassembly_id']} has no renderable clips")
        concat_file = temp_dir / f"{video['reassembly_id']}_concat.txt"
        write_text(concat_file, "".join(f"file '{str(p).replace(chr(92), '/')}'\n" for p in clip_files))
        run_cmd([
            "ffmpeg",
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
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            str(output_path),
        ])

    def _reassembly_quality_check(self, video: dict[str, Any], output_path: Path) -> dict[str, Any]:
        metadata = ffprobe_json(output_path)
        final_duration = float(metadata.get("duration") or 0)
        expected_duration = float(video.get("video_duration_seconds") or 0)
        has_audio = any(s.get("codec_type") == "audio" for s in metadata.get("streams", []))
        has_video = any(s.get("codec_type") == "video" for s in metadata.get("streams", []))
        duration_delta = round(abs(final_duration - expected_duration), 3)
        return {
            "final_duration_seconds": round(final_duration, 3),
            "expected_duration_seconds": round(expected_duration, 3),
            "duration_delta_seconds": duration_delta,
            "audio_policy": "original_audio",
            "clip_count": len(video.get("clips", [])),
            "has_audio": has_audio,
            "has_video": has_video,
            "status": "ok" if has_audio and has_video and duration_delta <= 1.0 else "mismatch",
        }

    def _clip_time_seconds(self, clip: dict[str, Any], *keys: str) -> float:
        for key in keys:
            value = clip.get(key)
            if value not in (None, ""):
                return timecode_to_seconds(value)
        return 0.0

    def step_render(self) -> None:
        render_slots = int(self.config.raw.get("gpu_limits", {}).get("render_slots", 1))
        with file_slot_lock("render", slots=render_slots):
            self._step_render_impl()

    def _step_render_impl(self) -> None:
        plan = self._load_step_json("cut_plan")
        input_hash = stable_hash({"cut_plan": self._step_content_hash("cut_plan")})
        if self._can_reuse("render", input_hash):
            print("复用缓存: render")
            return
        version, base_dir = self._version_dir("render", "edit/drafts")
        outputs = []
        skipped_outputs = []
        for video in plan.get("output_videos", []):
            sid = video["short_video_id"]
            if video.get("duration_status") == "blocked":
                skipped_outputs.append({
                    "short_video_id": sid,
                    "status": "skipped",
                    "reason": "cut_plan_blocked",
                    "blocked_reasons": video.get("blocked_reasons", []),
                })
                continue
            vdir = ensure_dir(base_dir / sid)
            out = vdir / video["output_file"]
            self._render_one(video, out)
            try:
                final_duration = float(ffprobe_json(out).get("duration") or 0)
            except Exception:
                final_meta = audio_metadata(out)
                final_duration = float(final_meta.get("actual_duration_seconds") or 0)
            voiceover_duration = float(video.get("voiceover_duration_seconds") or 0)
            duration_delta = round(abs(final_duration - voiceover_duration), 3) if voiceover_duration else 0.0
            quality_check = {
                "final_duration_seconds": final_duration,
                "voiceover_duration_seconds": voiceover_duration,
                "duration_delta_seconds": duration_delta,
                "duration_status": "ok" if not voiceover_duration or duration_delta <= 1.0 else "mismatch",
                "audio_policy": video.get("audio_policy", ""),
                "voiceover_timeline_mode": video.get("voiceover", {}).get("timeline_mode", ""),
                "evidence_audio_window_count": len(video.get("evidence_audio_windows", [])),
                "voiceover_original_overlap_seconds": float(video.get("voiceover_original_overlap_seconds") or 0),
            }
            write_json(vdir / "final_quality_check.json", quality_check)
            outputs.append({"short_video_id": sid, "file": relpath(out, self.task_dir), "quality_check": quality_check})
            status_doc = self._base_status("render", version, stable_hash(video), [out])
            status_doc["quality_check"] = quality_check
            self._write_status(vdir, status_doc)
        out_index = write_json(base_dir / "render_outputs.json", {"version": version, "outputs": outputs, "skipped_outputs": skipped_outputs})
        status = "success"
        if skipped_outputs:
            status = "partial_success" if outputs else "failed"
        extra = {"skipped_outputs": skipped_outputs} if skipped_outputs else None
        self._record_step(step="render", version=version, status=status, output=relpath(out_index, self.task_dir), input_hash=input_hash, output_files=[out_index], extra=extra)
        print(f"completed: render ({status})")
        if status == "failed":
            raise RuntimeError("render has no available videos; all candidates were blocked by cut_plan")

    def _trim_rendered_video_to_duration(
        self,
        input_path: Path,
        output_path: Path,
        duration_seconds: float,
    ) -> None:
        run_cmd([
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-t",
            f"{duration_seconds:.3f}",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            str(output_path),
        ])

    def _render_one(self, video: dict[str, Any], output_path: Path) -> None:
        temp_dir = ensure_dir(output_path.parent / "_clips")
        clip_files = []
        voice = video.get("voiceover", {})
        voiceover_enabled = bool(voice.get("enabled") and voice.get("file"))
        for idx, clip in enumerate(video.get("clips", []), start=1):
            source, local_start, local_end = self._resolve_reassembly_clip_source(clip)
            clip_file = temp_dir / f"{video['short_video_id']}_{idx:03d}.mp4"
            vf = self._video_filter(video)
            vol = float(clip.get("original_audio_volume", 1.0))
            if not clip.get("keep_original_audio", True):
                vol = 0.0
            if voiceover_enabled:
                vol = 1.0 if self.duration_settings.suppress_unplanned_original_audio else vol
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                seconds_to_timecode(local_start, ms=True),
                "-to",
                seconds_to_timecode(local_end, ms=True),
                "-i",
                str(source),
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-af",
                f"volume={vol}",
                str(clip_file),
            ]
            run_cmd(cmd)
            clip_files.append(clip_file)
        if not clip_files:
            raise RuntimeError(f"{video['short_video_id']} 没有可渲染片段")
        concat_file = temp_dir / f"{video['short_video_id']}_concat.txt"
        write_text(concat_file, "".join(f"file '{str(p).replace(chr(92), '/')}'\n" for p in clip_files))
        merged = temp_dir / f"{video['short_video_id']}_merged.mp4"
        run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(merged)])
        current = merged
        if voiceover_enabled:
            mixed = temp_dir / f"{video['short_video_id']}_voiceover.mp4"
            evidence_windows = video.get("evidence_audio_windows", [])
            voice_windows = []
            for segment in voice.get("segments", []):
                start = float(segment.get("target_start_seconds") or 0)
                actual_duration = float(segment.get("actual_duration_seconds") or segment.get("target_duration_seconds") or 0)
                if actual_duration <= 0:
                    continue
                voice_windows.append({
                    "target_start_seconds": start,
                    "target_end_seconds": start + actual_duration,
                })
            voice_volume = float(voice.get("voiceover_volume", 1.0))
            background_volume = float(self.duration_settings.background_original_audio_volume)
            if self.duration_settings.suppress_unplanned_original_audio:
                evidence_exprs = []
                for window in evidence_windows:
                    start = float(window.get("target_start_seconds") or 0)
                    end = float(window.get("target_end_seconds") or 0)
                    if end > start:
                        evidence_exprs.append(f"between(t,{start:.3f},{end:.3f})")
                evidence_expr = "+".join(evidence_exprs) if evidence_exprs else "0"
                base_original_volume = background_volume if evidence_exprs else float(self.duration_settings.default_original_audio_volume)
                original_filter = (
                    f"[0:a]volume='if(gt({evidence_expr},0),"
                    f"{self.duration_settings.evidence_original_audio_volume:.3f},{base_original_volume:.3f})'[a0]"
                )
            else:
                original_filter = "[0:a]volume=1.0"
                for window in voice_windows:
                    start = float(window.get("target_start_seconds") or 0)
                    end = float(window.get("target_end_seconds") or 0)
                    original_filter += f",volume={background_volume}:enable='between(t,{start:.3f},{end:.3f})'"
                original_filter += "[a0]"
            ai_filter = f"[1:a]volume={voice_volume}"
            for window in evidence_windows:
                start = float(window.get("target_start_seconds") or 0)
                end = float(window.get("target_end_seconds") or 0)
                ai_filter += f",volume=0:enable='between(t,{start:.3f},{end:.3f})'"
            ai_filter += "[a1]"
            run_cmd([
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(current),
                "-i",
                str(self.task_dir / voice["file"]),
                "-filter_complex",
                f"{original_filter};{ai_filter};[a0][a1]amix=inputs=2:duration=first:normalize=0[aout]",
                "-map",
                "0:v",
                "-map",
                "[aout]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                str(mixed),
            ])
            current = mixed
        final_current = current

        if voiceover_enabled and self.duration_settings.ai_voiceover_render_trim_to_tts:
            voiceover_duration = float(voice.get("actual_duration_seconds") or 0)
            if voiceover_duration > 0:
                tolerance = float(self.duration_settings.ai_voiceover_trim_tail_tolerance_seconds or 0.8)
                trim_duration = voiceover_duration + tolerance
                trimmed_video = temp_dir / f"{video['short_video_id']}_trimmed_to_tts.mp4"
                self._trim_rendered_video_to_duration(current, trimmed_video, trim_duration)
                final_current = trimmed_video

        subtitle = video.get("subtitles", {})
        if subtitle.get("burn_in") and subtitle.get("file"):
            sub = str(self.task_dir / subtitle["file"]).replace("\\", "/").replace(":", "\\:")
            style = self._subtitle_force_style()
            run_cmd([
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(final_current),
                "-vf",
                f"subtitles='{sub}':force_style='{style}'",
                "-c:a",
                "copy",
                str(output_path),
            ])
        else:
            shutil.copy2(final_current, output_path)

    def _video_filter(self, video: dict[str, Any]) -> str:
        if video.get("aspect_ratio") == "9:16":
            return "scale=1080:-2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
        return "scale=1920:-2"

    def _subtitle_config(self) -> dict[str, Any]:
        config = getattr(self, "config", None)
        return getattr(config, "subtitle", None) or {}

    def _subtitle_mode(self) -> str:
        if not hasattr(self, "config"):
            return "segment"
        return str(self._subtitle_config().get("mode", "sentence")).strip().lower()

    def _subtitle_max_chars_per_line(self) -> int:
        cfg = self._subtitle_config()
        aspect_ratio = getattr(getattr(self, "options", None), "aspect_ratio", "16:9")
        if aspect_ratio == "9:16":
            return int(cfg.get("max_chars_per_line_9_16", 14))
        return int(cfg.get("max_chars_per_line_16_9", 22))

    def _subtitle_min_cue_seconds(self) -> float:
        return float(self._subtitle_config().get("min_cue_seconds", 1.0))

    def _subtitle_max_cue_seconds(self) -> float:
        return float(self._subtitle_config().get("max_cue_seconds", 4.0))

    def _subtitle_force_style(self) -> str:
        cfg = self._subtitle_config()
        aspect_ratio = getattr(getattr(self, "options", None), "aspect_ratio", "16:9")
        if aspect_ratio == "9:16":
            font_size = int(cfg.get("font_size_9_16", 28))
            margin_v = int(cfg.get("margin_v_9_16", 150))
        else:
            font_size = int(cfg.get("font_size_16_9", 30))
            margin_v = int(cfg.get("margin_v_16_9", 52))
        configured_font = str(cfg.get("font_name", "")).strip()
        font_name = configured_font

        import os
        import shutil
        import subprocess

        def _font_available(fname: str) -> bool:
            if not fname:
                return False
            try:
                result = subprocess.run(
                    ["fc-match", fname],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=2,
                )
                return fname.lower() in result.stdout.lower()
            except Exception:
                return False

        if shutil.which("fc-match"):
            candidates = [
                configured_font,
                "Noto Sans CJK SC",
                "WenQuanYi Micro Hei",
                "Microsoft YaHei",
                "SimHei",
                "Arial Unicode MS",
            ]
            for cand in candidates:
                if cand and _font_available(cand):
                    font_name = cand
                    break

        if not font_name:
            font_name = "Microsoft YaHei" if os.name == "nt" else "Noto Sans CJK SC"
        primary = str(cfg.get("primary_colour", "&H00FFFFFF"))
        outline_colour = str(cfg.get("outline_colour", "&H00000000"))
        back_colour = str(cfg.get("back_colour", "&H80000000"))
        border_style = int(cfg.get("border_style", 3))
        outline = int(cfg.get("outline", 1))
        shadow = int(cfg.get("shadow", 0))
        blur = int(cfg.get("blur", 0))
        bold = int(cfg.get("bold", 1))
        return ",".join([
            f"FontName={font_name}",
            f"Fontsize={font_size}",
            f"Bold={bold}",
            f"PrimaryColour={primary}",
            f"OutlineColour={outline_colour}",
            f"BackColour={back_colour}",
            f"BorderStyle={border_style}",
            f"Outline={outline}",
            f"Shadow={shadow}",
            f"Blur={blur}",
            "Alignment=2",
            f"MarginV={margin_v}",
        ])

    def _pad_subtitle_text(self, text: str) -> str:
        if self._subtitle_mode() == "segment":
            return text
        pad = int(self._subtitle_config().get("box_padding_chars", 0))
        if pad <= 0:
            return text
        return ("　" * pad) + text + ("　" * pad)

    def _effective_output_audio_policy(self, tts_success: bool) -> str:
        if self.options.audio_policy == "original":
            return "original_audio"
        if self.options.audio_policy == "mixed":
            return "mixed_ai_voiceover" if tts_success else "mixed_ai_voiceover_missing"
        return "ai_voiceover_main" if tts_success else "ai_voiceover_missing"

    def _generate_tts(self, text: str, output_path: Path) -> None:
        voice_config = self._resolve_voice_config()
        model_path = self.config.resolve_path(voice_config.get("model_path"), "models/OmniVoice")
        ref_audio = self.config.resolve_path(voice_config.get("reference_audio"), "tts_ref/664925840_0_13s.mp3")
        result = generate_omnivoice_audio(
            text=text,
            output_path=output_path,
            model_path=model_path or "",
            reference_audio=ref_audio or "",
            reference_text=voice_config.get("reference_text", ""),
            keep_model_loaded=False,
            speed=voice_config.get("speed"),
        )
        if result.status != "success":
            raise RuntimeError(f"OmniVoice 生成失败: {result.error}")

    def _split_subtitle_text(self, text: str, max_chars: int | None = None) -> list[str]:
        value = clean_voiceover_text(text)
        value = re.sub(r"【停顿[0-9.]+秒】", "，", value)
        value = re.sub(r"\s+", "", value)
        if not value:
            return []

        max_chars = max(1, int(max_chars or self._subtitle_max_chars_per_line()))
        sentence_parts = re.findall(r"[^。！？!?]+[。！？!?]?", value)
        sentence_parts = [part.strip() for part in sentence_parts if part.strip()]

        chunks: list[str] = []
        for sentence in sentence_parts:
            if len(sentence) <= max_chars:
                chunks.append(sentence)
                continue

            clause_parts = re.findall(r"[^，,；;、：:]+[，,；;、：:]?", sentence)
            clause_parts = [part.strip() for part in clause_parts if part.strip()]
            buffer = ""
            for part in clause_parts:
                if not buffer:
                    buffer = part
                    continue
                if len(buffer) + len(part) <= max_chars:
                    buffer += part
                else:
                    chunks.extend(self._hard_split_subtitle_chunk(buffer, max_chars))
                    buffer = part
            if buffer:
                chunks.extend(self._hard_split_subtitle_chunk(buffer, max_chars))

        return [chunk for chunk in chunks if chunk.strip()]

    def _hard_split_subtitle_chunk(self, text: str, max_chars: int) -> list[str]:
        value = (text or "").strip()
        if not value:
            return []
        if len(value) <= max_chars:
            return [value]
        return [value[i:i + max_chars] for i in range(0, len(value), max_chars)]

    def _subtitle_chunks_to_srt_blocks(
        self,
        *,
        chunks: list[str],
        start: float,
        end: float,
        index_start: int,
    ) -> tuple[list[str], int]:
        if not chunks or end <= start:
            return [], index_start

        total_duration = max(0.1, end - start)
        weights = [max(1, len(chunk)) for chunk in chunks]
        weight_total = max(1, sum(weights))
        min_cue = max(0.1, self._subtitle_min_cue_seconds())
        max_cue = max(min_cue, self._subtitle_max_cue_seconds())
        blocks: list[str] = []
        cursor = start
        index = index_start

        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                cue_end = end
            else:
                raw_duration = total_duration * weights[i] / weight_total
                cue_duration = min(max(raw_duration, min_cue), max_cue)
                remaining_chunks = len(chunks) - i - 1
                latest_end = end - remaining_chunks * min_cue
                cue_end = min(cursor + cue_duration, latest_end)
                if cue_end <= cursor:
                    cue_end = cursor + max(0.3, raw_duration)

            cue_end = min(cue_end, end)
            if cue_end <= cursor:
                break
            blocks.append(f"{index}\n{srt_time(cursor)} --> {srt_time(cue_end)}\n{self._pad_subtitle_text(chunk)}\n")
            index += 1
            cursor = cue_end

        return blocks, index

    def _script_to_srt(self, text: str, total_duration: float | None = None) -> str:
        chunks = self._split_subtitle_text(text)
        if not chunks:
            return ""

        if total_duration and total_duration > 0:
            end = float(total_duration)
        else:
            total_chars = sum(max(1, len(chunk)) for chunk in chunks)
            end = max(2.0, total_chars / max(self.duration_settings.chars_per_second, 0.1))
        blocks, _ = self._subtitle_chunks_to_srt_blocks(chunks=chunks, start=0.0, end=end, index_start=1)
        return "\n".join(blocks)

    def _segments_to_srt(self, segments: list[dict[str, Any]]) -> str:
        blocks = []
        index = 1
        for segment in segments:
            text = (segment.get("text") or "").strip()
            if not text:
                continue
            start = timecode_to_seconds(segment.get("target_start"))
            end = timecode_to_seconds(segment.get("target_end"))
            duration = float(segment.get("target_duration_seconds") or 0)
            if end <= start and duration > 0:
                end = start + duration
            if end <= start:
                continue
            if self._subtitle_mode() == "segment":
                blocks.append(f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{self._pad_subtitle_text(text)}\n")
                index += 1
                continue
            chunks = self._split_subtitle_text(text)
            new_blocks, index = self._subtitle_chunks_to_srt_blocks(
                chunks=chunks,
                start=start,
                end=end,
                index_start=index,
            )
            blocks.extend(new_blocks)
        return "\n".join(blocks)

    def _tts_segments_to_srt(self, segments: list[dict[str, Any]]) -> str:
        blocks = []
        index = 1
        for segment in segments:
            text = (segment.get("text") or "").strip()
            if not text:
                continue
            start = float(segment.get("target_start_seconds") or 0)
            end = float(segment.get("target_end_seconds") or 0)
            if end <= start:
                actual = float(segment.get("actual_duration_seconds") or 0)
                end = start + actual
            if end <= start:
                continue
            if self._subtitle_mode() == "segment":
                blocks.append(f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{self._pad_subtitle_text(text)}\n")
                index += 1
                continue
            chunks = self._split_subtitle_text(text)
            new_blocks, index = self._subtitle_chunks_to_srt_blocks(
                chunks=chunks,
                start=start,
                end=end,
                index_start=index,
            )
            blocks.extend(new_blocks)
        return "\n".join(blocks)

    def _base_status(self, step: str, version: str, input_hash: str, files: list[Path]) -> dict[str, Any]:
        return {
            "step_name": step,
            "version": version,
            "status": "success",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "input_hash": input_hash,
            "output_hash": output_hash(files),
            "depends_on": DEPENDENCIES.get(step, []),
            "output_files": [relpath(p, self.task_dir) for p in files],
            "manual_edited": False,
            "notes": "",
        }

    def _step_version(self, step: str) -> str:
        return self.manifest.get("current_versions", {}).get(step, "")

    def _step_content_hash(self, step: str) -> str:
        return self.manifest.get("steps", {}).get(step, {}).get("output_hash", "")

    def _step_output(self, step: str) -> str:
        out = self.manifest.get("steps", {}).get(step, {}).get("output")
        if not out:
            raise RuntimeError(f"步骤 {step} 没有可用输出")
        return out

    def _load_step_json(self, step: str) -> dict[str, Any]:
        return read_json(self.task_dir / self._step_output(step), {})

    def _load_optional_step_json(self, step: str, default: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._load_step_json(step)
        except Exception:
            return default

    def _normalize_asr_segments(self, segments: list[dict[str, Any]], full_text: str) -> list[dict[str, Any]]:
        out = []
        for idx, seg in enumerate(segments, start=1):
            start = float(seg.get("start") or 0)
            end = float(seg.get("end") or 0)
            if end <= start:
                end = start + max(1.0, len(seg.get("text", "")) / 5.0)
            out.append({
                "id": idx,
                "start": start,
                "end": end,
                "start_time": seconds_to_timecode(start, ms=True),
                "end_time": seconds_to_timecode(end, ms=True),
                "speaker": seg.get("speaker", ""),
                "text": clean_asr_text(str(seg.get("text", "")))["clean_text"],
            })
        if not out and full_text:
            out.append({"id": 1, "start": 0, "end": 0, "start_time": "00:00:00.000", "end_time": "00:00:00.000", "speaker": "", "text": clean_asr_text(full_text)["clean_text"]})
        return out

    def _segments_in_range(self, segments: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
        selected = []
        for seg in segments:
            s = float(seg.get("start") or 0)
            e = float(seg.get("end") or s)
            if e >= start and s <= end:
                selected.append(seg)
        return selected

    def _apply_manual_override(self, version_dir: Path, model_output: dict[str, Any], step: str, target_id: str) -> dict[str, Any]:
        override_path = self.task_dir / step / self.manifest.get("current_versions", {}).get(step, "") / target_id / "manual_override.json"
        return self._merge_manual(model_output, read_json(override_path, {}))

    def _merge_manual(self, model_output: Any, override: dict[str, Any]) -> Any:
        if not override:
            return model_output
        if "final_output" in override:
            return override["final_output"]
        fields = override.get("fields_overridden", override)
        if isinstance(model_output, dict) and isinstance(fields, dict):
            merged = dict(model_output)
            merged.update(fields)
            return merged
        return model_output


def parse_args(argv: list[str] | None = None) -> RunOptions:
    parser = argparse.ArgumentParser(description="凤凰新闻视频智能拆条系统")
    parser.add_argument("--input", help="输入视频路径，首次运行必填")
    parser.add_argument("--source-manifest", help="Multi-source mapping JSON generated by the web UI")
    parser.add_argument("--task-id", help="任务 ID，断点续跑或指定任务时使用")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--outputs-dir", default="outputs")
    parser.add_argument("--resume", action="store_true", default=True, help="复用已有成功步骤")
    parser.add_argument("--no-resume", action="store_false", dest="resume")
    parser.add_argument("--rerun", choices=ALL_STEP_ORDER, help="只重跑某一步")
    parser.add_argument("--rerun-from", choices=ALL_STEP_ORDER, help="从某一步开始重跑后续")
    parser.add_argument("--chunk", help="只重跑指定 vision chunk，例如 chunk_0007")
    parser.add_argument("--failed-only", action="store_true", help="vision 只重跑失败 chunk")
    parser.add_argument("--mode", choices=["normal", "dense"], default="normal", help="dense 模式每秒抽帧")
    parser.add_argument("--model", help="本次重跑指定模型")
    parser.add_argument("--prompt-version", help="本次重跑指定 Prompt 版本标记")
    parser.add_argument("--use-version", action="append", help="指定当前使用版本，例如 vision=v2")
    parser.add_argument("--stop-after", choices=ALL_STEP_ORDER, default=None)
    default_config = load_config("config.toml")
    default_workflow = default_config.workflow
    parser.add_argument("--chunk-seconds", type=int, default=int(default_workflow.get("default_chunk_seconds", 60)))
    parser.add_argument("--frame-interval", type=int, default=int(default_workflow.get("default_frame_interval", 10)))
    parser.add_argument("--vision-max-workers", type=int, default=int(default_workflow.get("vision_max_workers", 3)))
    parser.add_argument("--vision-max-frames-per-chunk", type=int, default=int(default_workflow.get("vision_max_frames_per_chunk", 6)))
    parser.add_argument("--release-asr-after-task", action="store_true")
    parser.add_argument("--aspect-ratio", default=default_workflow.get("default_aspect_ratio", "16:9"), choices=["9:16", "16:9"])
    parser.add_argument("--skip-tts", action="store_true")
    parser.add_argument("--skip-render", action="store_true")
    parser.add_argument("--common-only", action="store_true", help="Run only source-independent reusable analysis steps.")
    parser.add_argument("--only-analysis", action="store_true", help="只跑到风险审核，不生成配音和视频")
    parser.add_argument("--target-duration", type=int, default=int(default_config.short_video.get("default_target_seconds", 30)), dest="target_duration_seconds")
    parser.add_argument(
        "--target-duration-mode",
        choices=["fixed", "soft"],
        default=str(default_config.short_video.get("ai_voiceover_target_mode", "soft")),
    )
    parser.add_argument("--output-mode", choices=["single", "multiple"], default="single")
    parser.add_argument("--max-output-videos", type=int, default=1)
    parser.add_argument("--min-output-video-seconds", type=int, default=30)
    parser.add_argument("--max-output-video-seconds", type=int, default=90)
    parser.add_argument("--allow-long-video", action="store_true", default=bool(default_config.short_video.get("allow_long_video_default", False)))
    parser.add_argument("--require-tts", action="store_true", default=bool(default_config.voiceover.get("tts_required_by_default", True)))
    parser.add_argument("--no-require-tts", action="store_false", dest="require_tts")
    parser.add_argument("--voice-id", help="选择服务端内置音色 ID")
    parser.add_argument("--audio-policy", choices=["ai_voiceover", "original", "mixed"], default="ai_voiceover")
    parser.add_argument("--allow-original-audio-evidence", action="store_true")
    parser.add_argument("--production-mode", choices=["ai_voiceover", "highlight_reassembly"], default="ai_voiceover")
    parser.add_argument("--reassembly-output-mode", choices=["single", "multiple", "clips"], default="single")
    parser.add_argument("--reassembly-sort-mode", choices=["editorial", "source_order", "score"], default="editorial")
    parser.add_argument("--reassembly-target-seconds", type=int)
    parser.add_argument("--reassembly-min-clip-seconds", type=float, default=5.0)
    parser.add_argument("--reassembly-max-clip-seconds", type=float, default=45.0)
    parser.add_argument("--reassembly-max-clip-count", type=int, default=8)
    parser.add_argument("--reassembly-export-individual-clips", action="store_true")
    args = parser.parse_args(argv)
    if args.output_mode == "single":
        args.max_output_videos = 1
    else:
        args.max_output_videos = max(2, min(int(args.max_output_videos or 5), 5))
    if args.production_mode == "highlight_reassembly":
        args.audio_policy = "original"
        args.skip_tts = True
        args.require_tts = False
        args.allow_original_audio_evidence = True
        args.reassembly_output_mode = "multiple" if args.output_mode == "multiple" else "single"
    if args.skip_tts or args.audio_policy == "original":
        args.require_tts = False
    return RunOptions(**vars(args))


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    runner = PipelineRunner(options)
    try:
        runner.run()
        return 0
    except Exception as exc:
        message = runner._user_facing_error_message(exc)
        print("\n运行失败：")
        print(message)
        print("\n可在任务表中查看失败步骤，并按提示调整后重跑。")
        return 1
