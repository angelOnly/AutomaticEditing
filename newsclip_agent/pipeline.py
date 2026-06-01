from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
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
from .text_policy import sanitize_reporter_bylines
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


AI_VOICEOVER_STEP_ORDER = [
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "vision",
    "timeline",
    "timeline_digest",
    "content_analysis",
    "short_video_edit_plan",
    "voiceover_script",
    "tts",
    "subtitles",
    "cut_plan",
    "render",
]

HIGHLIGHT_REASSEMBLY_STEP_ORDER = [
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "vision",
    "timeline",
    "video_understanding",
    "highlight_detection",
    "highlight_reassembly_plan",
    "reassembly_cut_plan",
    "reassembly_render",
]

STEP_ORDER = AI_VOICEOVER_STEP_ORDER
ALL_STEP_ORDER = list(dict.fromkeys(AI_VOICEOVER_STEP_ORDER + HIGHLIGHT_REASSEMBLY_STEP_ORDER))


DEPENDENCIES = {
    "metadata": [],
    "audio_extract": ["metadata"],
    "frame_extract": ["metadata"],
    "chunk_build": ["metadata", "frame_extract"],
    "asr": ["audio_extract"],
    "vision": ["frame_extract", "chunk_build", "asr"],
    "timeline": ["asr", "vision"],
    "timeline_digest": ["timeline"],
    "content_analysis": ["timeline_digest"],
    "short_video_edit_plan": ["content_analysis", "timeline_digest"],
    "video_understanding": ["timeline"],
    "highlight_detection": ["timeline", "video_understanding"],
    "short_video_planning": ["highlight_detection", "video_understanding"],
    "editing_script": ["short_video_planning", "highlight_detection", "timeline"],
    "voiceover_script": ["short_video_edit_plan"],
    "risk_review": ["editing_script", "voiceover_script", "video_understanding"],
    "tts": ["voiceover_script"],
    "subtitles": ["voiceover_script", "tts"],
    "cut_plan": ["short_video_edit_plan", "voiceover_script", "subtitles", "tts"],
    "render": ["cut_plan"],
    "highlight_reassembly_plan": ["highlight_detection", "video_understanding", "timeline"],
    "reassembly_cut_plan": ["highlight_reassembly_plan"],
    "reassembly_render": ["reassembly_cut_plan"],
}


AGENT_INFO = {
    "video_understanding": ("agents/video_understanding", "video_analysis.json", prompts.VIDEO_UNDERSTANDING_PROMPT, "video_understanding_v1"),
    "highlight_detection": ("agents/highlight_detection", "candidate_clips.json", prompts.HIGHLIGHT_DETECTION_PROMPT, "highlight_detection_v1"),
    "short_video_planning": ("agents/short_video_planning", "short_video_plan.json", prompts.SHORT_VIDEO_PLANNER_PROMPT, "short_video_planning_v1"),
    "editing_script": ("agents/editing_script", "editing_script.json", prompts.EDITING_DIRECTOR_PROMPT, "editing_script_v1"),
    "content_analysis": ("agents/content_analysis", "content_analysis.json", prompts.CONTENT_ANALYSIS_PROMPT, "content_analysis_v1"),
    "short_video_edit_plan": ("agents/short_video_edit_plan", "short_video_edit_plan.json", prompts.SHORT_VIDEO_EDIT_PLAN_PROMPT, "short_video_edit_plan_v1"),
    "voiceover_script": ("agents/voiceover_script", "voiceover_script.json", prompts.VOICEOVER_LIGHT_PROMPT, "voiceover_script_light_v1"),
    "risk_review": ("agents/risk_review", "review_report.json", prompts.RISK_REVIEW_PROMPT, "risk_review_v1"),
    "highlight_reassembly_plan": ("agents/highlight_reassembly", "highlight_reassembly_plan.json", prompts.HIGHLIGHT_REASSEMBLY_PROMPT, "highlight_reassembly_v1"),
}


@dataclass
class RunOptions:
    input: str | None = None
    task_id: str | None = None
    config: str = "config.toml"
    outputs_dir: str = "outputs"
    resume: bool = True
    rerun: str | None = None
    rerun_from: str | None = None
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
    target_duration_seconds: int | None = None
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
                handler = getattr(self, f"step_{step}")
                started = time.perf_counter()
                try:
                    handler()
                except Exception as exc:
                    self._mark_failed(step, self._user_facing_error_message(exc), elapsed_seconds=round(time.perf_counter() - started, 3))
                    raise
                self.manifest.setdefault("steps", {}).setdefault(step, {})["elapsed_seconds"] = round(time.perf_counter() - started, 3)
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
            return
        if not self.options.input:
            raise ValueError("首次运行必须提供 --input，断点续跑可只提供 --task-id")
        source = Path(self.options.input).resolve()
        input_dst = self.task_dir / "input" / ("source" + source.suffix.lower())
        copy_or_link(source, input_dst)
        task = {
            "task_id": self.task_id,
            "created_at": now_iso(),
            "source_video_original": str(source),
            "source_video": relpath(input_dst, self.task_dir),
        }
        write_json(self.task_path, task)
        self.manifest = {
            "task_id": self.task_id,
            "created_at": task["created_at"],
            "updated_at": now_iso(),
            "source_video": task["source_video"],
            "current_versions": {},
            "steps": {},
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
        if self.options.rerun and self.options.rerun_from:
            raise ValueError("--rerun 与 --rerun-from 不能同时使用")
        if self.options.rerun:
            if self.options.rerun not in step_order:
                raise ValueError(f"未知步骤: {self.options.rerun}")
            return [self.options.rerun]
        if self.options.rerun_from:
            if self.options.rerun_from not in step_order:
                raise ValueError(f"未知步骤: {self.options.rerun_from}")
            return step_order[step_order.index(self.options.rerun_from) :]
        return step_order

    def _active_step_order(self) -> list[str]:
        if self.options.production_mode == "highlight_reassembly":
            return HIGHLIGHT_REASSEMBLY_STEP_ORDER
        return AI_VOICEOVER_STEP_ORDER

    def _source_video(self) -> Path:
        source = self.manifest["source_video"]
        p = Path(source)
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
        return self.options.resume

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
    ) -> None:
        data = {
            "step_name": step,
            "version": version,
            "status": status,
            "updated_at": now_iso(),
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
        return str(exc).strip() or exc.__class__.__name__

    def _mark_downstream_stale(self, step: str) -> None:
        if self.options.rerun != step and not self.options.rerun_from:
            return
        step_order = self._active_step_order()
        idx = step_order.index(step)
        for downstream in step_order[idx + 1 :]:
            entry = self.manifest.setdefault("steps", {}).get(downstream)
            if entry and entry.get("status") == "success":
                entry["status"] = "stale"
                entry["reason"] = f"upstream {step} updated"

    def step_metadata(self) -> None:
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
        source = self._source_video()
        input_hash = stable_hash({"source": self._step_output("metadata"), "audio": "mono_16k_wav"})
        if self._can_reuse("audio_extract", input_hash):
            print("复用缓存: audio_extract")
            return
        version, vdir = self._version_dir("audio_extract", "preprocess/audio")
        ensure_dir(vdir)
        out = vdir / "audio.wav"
        run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", str(out)])
        self._write_status(vdir, self._base_status("audio_extract", version, input_hash, [out]))
        self._record_step(step="audio_extract", version=version, status="success", output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out])
        print("完成: audio_extract")

    def step_frame_extract(self) -> None:
        source = self._source_video()
        interval = 1 if self.options.mode == "dense" else self.options.frame_interval
        input_hash = stable_hash({"source": self._step_output("metadata"), "interval": interval})
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
        input_hash = stable_hash({"metadata": metadata.get("duration"), "frames": self._step_version("frame_extract"), "chunk_seconds": chunk_seconds})
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
        input_hash = stable_hash({"audio": self._step_output("audio_extract"), "funasr": self.config.funasr})
        if self._can_reuse("asr", input_hash):
            print("复用缓存: asr")
            return
        version, vdir = self._version_dir("asr", "asr")
        ensure_dir(vdir)
        write_json(vdir / "input.json", {"audio": relpath(audio, self.task_dir), "config": self.config.funasr})
        try:
            asr_slots = int(self.config.raw.get("gpu_limits", {}).get("asr_slots", 1))
            with file_slot_lock("asr", slots=asr_slots):
                engine = self._get_asr_engine()
                result = engine.transcribe(str(audio))
            if not result.get("success"):
                raise RuntimeError(result.get("error") or "FunASR failed")
            segments = self._normalize_asr_segments(result.get("segments", []), result.get("text", ""))
            asr = {"language": "zh", "segments": segments, "full_text": result.get("text", ""), "raw_result": result}
            out = write_json(vdir / "asr_segments.json", asr)
            write_text(vdir / "full_text.txt", asr["full_text"])
            status = "success"
            err = ""
        except Exception as exc:
            asr = {"language": "zh", "segments": [], "full_text": "", "error": str(exc)}
            out = write_json(vdir / "asr_segments.json", asr)
            write_text(vdir / "full_text.txt", "")
            status = "failed"
            err = str(exc)
        step_status = self._base_status("asr", version, input_hash, [out])
        if err:
            step_status["error"] = err
        self._write_status(vdir, step_status)
        self._record_step(step="asr", version=version, status=status, output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out])
        if status != "success":
            raise RuntimeError(f"ASR 失败: {err}")
        print("完成: asr")

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

    def step_vision(self) -> None:
        if self.llm_vision is None:
            raise RuntimeError("缺少 vision LLM 配置")
        chunks_doc = self._load_step_json("chunk_build")
        asr = self._load_step_json("asr")
        llm_cfg = self.config.llm
        provider = llm_cfg.get("vision_llm_provider", "openai")
        model = self.options.model or llm_cfg.get(f"vision_{provider}_model_name")
        fallback = llm_cfg.get(f"vision_{provider}_fallback_models", [])
        prompt_version = self.options.prompt_version or "vision_chunk_v1"
        base_input_hash = stable_hash({
            "chunks": self._step_version("chunk_build"),
            "asr": self._step_version("asr"),
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
                "chunk": compact_chunk_for_vision(chunk),
                "asr_text": compact_asr_for_llm(" ".join(s.get("text", "") for s in asr_segments), max_chars=500),
                "prompt_version": prompt_version,
                "model": model,
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
                if isinstance(parsed, dict):
                    parsed.setdefault("chunk_id", cid)
                    parsed.setdefault("time_range", chunk["time_range"])
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
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_one_chunk, chunk) for chunk in chunks_to_process]
            for future in as_completed(futures):
                record = future.result()
                chunk_records.append(record)
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
        vision = self._load_step_json("vision")
        chunks = self._load_step_json("chunk_build")
        input_hash = stable_hash({"asr": self._step_version("asr"), "vision": self._step_version("vision"), "chunks": self._step_version("chunk_build")})
        if self._can_reuse("timeline", input_hash):
            print("复用缓存: timeline")
            return
        version, vdir = self._version_dir("timeline", "timeline")
        ensure_dir(vdir)
        visual_by_id = {r.get("chunk_id"): r for r in vision.get("results", []) if isinstance(r, dict)}
        timeline = []
        for chunk in chunks.get("chunks", []):
            vr = self._apply_manual_override(vdir, visual_by_id.get(chunk["chunk_id"], {}), "vision", chunk["chunk_id"])
            segs = self._segments_in_range(asr.get("segments", []), chunk["start"], chunk["end"])
            timeline.append({
                "chunk_id": chunk["chunk_id"],
                "start": seconds_to_timecode(chunk["start"], ms=True),
                "end": seconds_to_timecode(chunk["end"], ms=True),
                "start_seconds": chunk["start"],
                "end_seconds": chunk["end"],
                "asr_text": " ".join(s.get("text", "") for s in segs).strip(),
                "asr_segments": segs,
                "scene_type": vr.get("scene_type", ""),
                "visual_summary": vr.get("visual_summary", ""),
                "screen_text": vr.get("screen_text", []),
                "visible_people": vr.get("visible_people", []),
                "is_live_scene": vr.get("is_live_scene", False),
                "is_archive_footage": vr.get("is_archive_footage", False),
                "visual_value_score": vr.get("visual_value_score", 0),
                "hook_score": vr.get("hook_score", 0),
                "risk_tags": vr.get("risk_tags", []),
                "notes": vr.get("notes", ""),
            })
        out = write_json(vdir / "merged_timeline.json", {"timeline": timeline, "source_versions": {"asr": self._step_version("asr"), "vision": self._step_version("vision")}})
        self._write_status(vdir, self._base_status("timeline", version, input_hash, [out]))
        self._record_step(step="timeline", version=version, status="success", output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out])
        print("完成: timeline")

    def step_timeline_digest(self) -> None:
        timeline_data = self._load_step_json("timeline")
        timeline = timeline_data.get("timeline", [])
        llm_input_cfg = self.config.raw.get("llm_input", {})
        digest_version = "llm_timeline_digest_v1"
        input_hash = stable_hash({"timeline": self._step_version("timeline"), "digest_version": digest_version, "llm_input": llm_input_cfg})
        if self._can_reuse("timeline_digest", input_hash):
            print("澶嶇敤缂撳瓨: timeline_digest")
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
                "speech": compact_asr_for_llm(item.get("asr_text", ""), max_chars=int(llm_input_cfg.get("max_asr_chars_per_chunk", 320))),
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
            "source_versions": {"timeline": self._step_version("timeline")},
            "video_duration_seconds": max((float(x.get("end_seconds") or 0) for x in chunks), default=0.0),
            "chunks": chunks,
        }
        out = write_json(vdir / "llm_timeline_digest.json", output)
        self._write_status(vdir, self._base_status("timeline_digest", version, input_hash, [out]))
        self._record_step(step="timeline_digest", version=version, status="success", output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out], extra={"summary": {"chunks": len(chunks)}})
        print("瀹屾垚: timeline_digest")

    def step_content_analysis(self) -> None:
        result = self._run_text_agent("content_analysis", {
            "timeline_digest": self._load_step_json("timeline_digest"),
            "run_options": self._run_options_payload_minimal(),
        })
        self._write_compat_content_analysis(result)

    def step_video_understanding(self) -> None:
        self._run_text_agent("video_understanding", {
            "merged_timeline": self._load_step_json("timeline"),
        })

    def step_highlight_detection(self) -> None:
        self._run_text_agent("highlight_detection", {
            "merged_timeline": self._load_step_json("timeline"),
            "video_analysis": self._load_step_json("video_understanding"),
        })

    def step_highlight_reassembly_plan(self) -> None:
        self._run_text_agent("highlight_reassembly_plan", {
            "run_options": self._run_options_payload(),
            "reassembly_options": self._reassembly_options_payload(),
            "timeline": self._load_step_json("timeline"),
            "video_analysis": self._load_step_json("video_understanding"),
            "candidate_clips": self._load_step_json("highlight_detection"),
        })

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

    def step_short_video_edit_plan(self) -> None:
        result = self._run_text_agent("short_video_edit_plan", {
            "content_analysis": self._load_step_json("content_analysis"),
            "timeline_digest": self._load_step_json("timeline_digest"),
            "duration_strategy": self._duration_strategy_payload(),
            "run_options": self._run_options_payload(),
        })
        result = self._normalize_short_video_split_decision(result)
        write_json(self.task_dir / self._step_output("short_video_edit_plan"), result)
        self._write_compat_short_video_plan_and_editing_script(result)

    def step_voiceover_script(self) -> None:
        edit_plan = self._load_step_json("short_video_edit_plan")
        editing = self._load_step_json("editing_script")
        plan = self._load_step_json("short_video_planning")
        plan_by_id = {x.get("short_video_id"): x for x in plan.get("short_videos", []) if isinstance(x, dict)}
        contracts = [
            build_voiceover_timing_contract(
                script,
                plan_by_id.get(script.get("short_video_id"), {}),
                chars_per_second=self.duration_settings.chars_per_second,
            )
            for script in editing.get("scripts", [])
        ]
        final_output = self._run_text_agent("voiceover_script", {
            "short_video_edit_plan": edit_plan,
            "editing_script": {"scripts": editing.get("scripts", [])},
            "voiceover_timing_contracts": contracts,
            "duration_strategy": self._duration_strategy_payload(),
            "run_options": self._run_options_payload(),
        })
        self._normalize_voiceover_scripts(final_output)
        self._validate_voiceover_script_duration_or_raise(final_output)
        self._enforce_long_video_confirmation_after_voiceover(final_output)

    def step_risk_review(self) -> None:
        self._run_text_agent("risk_review", {
            "editing_script": self._load_step_json("editing_script"),
            "voiceover_script": self._load_step_json("voiceover_script"),
            "video_analysis": self._load_step_json("video_understanding"),
            "duration_strategy": self._duration_strategy_payload(),
            "run_options": self._run_options_payload(),
        })

    def _write_compat_agent_output(self, step: str, output: dict[str, Any], source_step: str) -> None:
        base, out_name, _prompt, prompt_version = AGENT_INFO[step]
        input_hash = stable_hash({"compat_source_step": source_step, "compat_source_version": self._step_version(source_step), "output": output})
        version, vdir = self._version_dir(step, base)
        ensure_dir(vdir)
        write_json(vdir / "model_output.json", output)
        write_json(vdir / "final_output.json", output)
        out = write_json(vdir / out_name, output)
        status = self._base_status(step, version, input_hash, [out])
        status.update({"compat_source_step": source_step, "prompt_version": prompt_version})
        self._write_status(vdir, status)
        self._record_step(step=step, version=version, status="success", output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out], extra={"compat_source_step": source_step, "prompt_version": prompt_version})

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
                editing_structure.append({
                    "order": shot.get("order", idx + 1),
                    "shot_id": shot.get("shot_id", f"{script.get('short_video_id', 'sv')}_s{idx + 1:02d}"),
                    "target_timeline": shot.get("target_timeline", ""),
                    "source_start": shot.get("source_start", ""),
                    "source_end": shot.get("source_end", ""),
                    "duration_seconds": shot.get("duration_seconds", 0),
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
                })
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

    def _log_llm_input_size(self, step: str, input_data: dict[str, Any]) -> None:
        text = json.dumps(input_data, ensure_ascii=False)
        print(f"LLM input size [{step}]: {len(text)} chars")
        if len(text) > 10000:
            print(f"warning: {step} input exceeds 10000 chars; check digest compaction")

    def _run_text_agent(self, step: str, input_data: dict[str, Any]) -> dict[str, Any]:
        if self.llm_text is None:
            raise RuntimeError("缺少 text LLM 配置")
        base, out_name, prompt, default_prompt_version = AGENT_INFO[step]
        llm_cfg = self.config.llm
        provider = llm_cfg.get("text_llm_provider", "openai")
        model = self.options.model or llm_cfg.get(f"text_{provider}_model_name")
        fallback = llm_cfg.get(f"text_{provider}_fallback_models", [])
        max_tokens = int(llm_cfg.get("text_llm_max_tokens", 16000) or 16000)
        prompt_version = self.options.prompt_version or default_prompt_version
        input_hash = stable_hash({"input": input_data, "model": model, "fallback": fallback, "prompt": prompt, "prompt_version": prompt_version})
        if self._can_reuse(step, input_hash):
            print(f"复用缓存: {step}")
            return self._load_step_json(step)
        version, vdir = self._version_dir(step, base)
        ensure_dir(vdir)
        self._log_llm_input_size(step, input_data)
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
        }

    def _run_options_payload_minimal(self) -> dict[str, Any]:
        return {
            "target_duration_seconds": self.options.target_duration_seconds or self.duration_settings.default_target_seconds,
            "allow_long_video": self.options.allow_long_video,
            "audio_policy": self.options.audio_policy,
            "voice_id": self.options.voice_id,
            "production_mode": self.options.production_mode,
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
        return {
            "output_mode": self.options.reassembly_output_mode,
            "sort_mode": self.options.reassembly_sort_mode,
            "target_seconds": self.options.reassembly_target_seconds,
            "min_clip_seconds": self.options.reassembly_min_clip_seconds,
            "max_clip_seconds": self.options.reassembly_max_clip_seconds,
            "max_clip_count": self.options.reassembly_max_clip_count,
            "export_individual_clips": self.options.reassembly_export_individual_clips,
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
        editing_by_id = {
            item.get("short_video_id"): item
            for item in editing.get("scripts", [])
            if isinstance(item, dict)
        }
        voice_config = self._resolve_voice_config()
        input_hash = stable_hash({
            "voiceover": self._step_version("voiceover_script"),
            "editing": self._step_version("editing_script"),
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
        failed = sum(1 for item in outputs if item.get("status") == "failed")
        success = sum(1 for item in outputs if item.get("status") == "success")
        tts_is_hard_required = self.options.require_tts and self.options.audio_policy in {"ai_voiceover", "mixed"}
        overall_status = "success" if failed == 0 else ("failed" if tts_is_hard_required else "partial_success")
        self._record_step(
            step="tts",
            version=version,
            status=overall_status,
            output=relpath(out_index, self.task_dir),
            input_hash=input_hash,
            output_files=[out_index],
            extra={"summary": {"success": success, "failed": failed, "total": len(outputs)}},
        )
        print(f"完成: tts ({overall_status})")
        if overall_status == "failed":
            raise RuntimeError("TTS 失败且 require_tts=true，已阻断后续 cut_plan/render")

    def _voiceover_segments_for_tts(self, script: dict[str, Any]) -> list[dict[str, Any]]:
        raw_segments = script.get("narration_segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            return []
        segments: list[dict[str, Any]] = []
        for index, segment in enumerate(raw_segments, start=1):
            if not isinstance(segment, dict):
                continue
            start = timecode_to_seconds(segment.get("target_start"))
            end = timecode_to_seconds(segment.get("target_end"))
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
            "voiceover": self._step_version("voiceover_script"),
            "tts": self._step_version("tts"),
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

    def step_cut_plan(self) -> None:
        editing = self._load_step_json("editing_script")
        voiceover_script = self._load_step_json("voiceover_script")
        tts = self._load_optional_step_json("tts", {"outputs": []})
        subtitles = self._load_optional_step_json("subtitles", {"outputs": []})
        input_hash = stable_hash({
            "editing": self._step_version("editing_script"),
            "tts": self._step_version("tts"),
            "subtitles": self._step_version("subtitles"),
            "aspect": self.options.aspect_ratio,
            "target_duration_seconds": self.options.target_duration_seconds,
            "allow_long_video": self.options.allow_long_video,
            "require_tts": self.options.require_tts,
            "voice_id": self.options.voice_id,
            "audio_policy": self.options.audio_policy,
            "allow_original_audio_evidence": self.options.allow_original_audio_evidence,
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
        try:
            source_duration = float(ffprobe_json(self._source_video()).get("duration") or 0)
        except Exception:
            source_duration = 0.0
        for script in editing.get("scripts", []):
            sid = script.get("short_video_id") or f"SV{len(output_videos)+1:03d}"
            clips = []
            target = 0.0
            validation = validate_editing_duration(script, allow_long_video=self.options.allow_long_video, settings=self.duration_settings)
            tts_item = tts_by_id.get(sid, {})
            voice_item = voice_by_id.get(sid, {})
            tts_success = tts_item.get("status") == "success" and bool(tts_item.get("voice_file_exists", True)) and bool(tts_item.get("file"))
            blocked_reasons = list(validation.get("blocked_reasons", []))
            tts_is_hard_required = self.options.require_tts and self.options.audio_policy in {"ai_voiceover", "mixed"}
            if tts_is_hard_required and not tts_success:
                blocked_reasons.append("require_tts=true 且音频策略需要 AI 配音，但 AI 配音未成功生成")
            warnings: list[str] = []
            repair_reasons: list[str] = []
            voiceover_duration = float(tts_item.get("actual_duration_seconds") or script.get("estimated_total_duration_seconds") or validation["video_duration_seconds"])
            for seg in script.get("editing_structure", []):
                start = timecode_to_seconds(seg.get("source_start"))
                end = timecode_to_seconds(seg.get("source_end"))
                if end <= start:
                    continue
                clip_duration = end - start
                audio_mode = seg.get("audio_mode") or ("mixed_evidence" if seg.get("original_audio_required") else "ai_voiceover")
                raw_clip = {
                    "source_start": seconds_to_timecode(start, ms=True),
                    "source_end": seconds_to_timecode(end, ms=True),
                    "target_start": seconds_to_timecode(target, ms=True),
                    "duration_seconds": round(clip_duration, 3),
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
                clip = normalize_audio_mode_for_policy(
                    raw_clip,
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
                target += clip_duration
            video_duration = round(target, 3)
            tts_segments = tts_item.get("segments", []) if isinstance(tts_item.get("segments"), list) else []
            if tts_success and tts_item.get("timeline_mode") == "compact_segmented" and tts_segments:
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
            )
            if mismatch_reason:
                blocked_reasons.append(mismatch_reason)
            duration_status = "blocked" if blocked_reasons else ("mismatch" if duration_delta > 1.0 else "ok")
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

    def _compact_clips_to_voiceover_segments(
        self,
        clips: list[dict[str, Any]],
        tts_segments: list[dict[str, Any]],
        source_duration: float = 0.0,
    ) -> list[dict[str, Any]]:
        active_segments = [
            segment for segment in tts_segments
            if segment.get("status") == "success" and float(segment.get("actual_duration_seconds") or 0) > 0
        ]
        if not clips or not active_segments:
            return clips
        compacted: list[dict[str, Any]] = []
        for index, (clip, segment) in enumerate(zip(clips, active_segments)):
            start = float(segment.get("target_start_seconds") or 0)
            end = float(segment.get("target_end_seconds") or 0)
            if index + 1 < len(active_segments):
                next_start = float(active_segments[index + 1].get("target_start_seconds") or end)
                duration = max(1.0, next_start - start)
            else:
                duration = max(1.0, end - start)
            source_start = timecode_to_seconds(clip.get("source_start"))
            source_end = source_start + duration
            if source_duration > 0:
                source_end = min(source_end, source_duration)
                duration = max(0.1, source_end - source_start)
            new_clip = dict(clip)
            new_clip["target_start"] = seconds_to_timecode(start, ms=True)
            new_clip["target_start_seconds"] = round(start, 3)
            new_clip["source_end"] = seconds_to_timecode(source_end, ms=True)
            new_clip["duration_seconds"] = round(duration, 3)
            new_clip["compact_voiceover_timed"] = True
            compacted.append(new_clip)
        return compacted

    def step_reassembly_cut_plan(self) -> None:
        plan = self._load_step_json("highlight_reassembly_plan")
        input_hash = stable_hash({
            "highlight_reassembly_plan": self._step_version("highlight_reassembly_plan"),
            "aspect": self.options.aspect_ratio,
            "reassembly_options": self._reassembly_options_payload(),
        })
        if self._can_reuse("reassembly_cut_plan", input_hash):
            print("reuse cache: reassembly_cut_plan")
            return
        version, vdir = self._version_dir("reassembly_cut_plan", "edit/reassembly_cut_plan")
        ensure_dir(vdir)
        output_videos = []
        for item in plan.get("output_videos", []):
            if not isinstance(item, dict):
                continue
            rid = item.get("reassembly_id") or f"hr_{len(output_videos) + 1:03d}"
            clips = []
            warnings: list[str] = []
            blocked_reasons: list[str] = []
            target = 0.0
            for idx, clip in enumerate(item.get("selected_clips", []), start=1):
                if not isinstance(clip, dict):
                    continue
                start = self._clip_time_seconds(clip, "adjusted_start", "source_start", "start", "start_time")
                end = self._clip_time_seconds(clip, "adjusted_end", "source_end", "end", "end_time")
                if end <= start:
                    warnings.append(f"{clip.get('source_clip_id') or clip.get('clip_id') or idx}: invalid time range")
                    continue
                duration = end - start
                if duration < self.options.reassembly_min_clip_seconds:
                    warnings.append(f"{clip.get('source_clip_id') or clip.get('clip_id') or idx}: shorter than min clip seconds")
                    continue
                if duration > self.options.reassembly_max_clip_seconds:
                    end = start + self.options.reassembly_max_clip_seconds
                    duration = self.options.reassembly_max_clip_seconds
                    warnings.append(f"{clip.get('source_clip_id') or clip.get('clip_id') or idx}: truncated to max clip seconds")
                clips.append({
                    "clip_id": f"{rid}_{len(clips) + 1:03d}",
                    "source_clip_id": clip.get("source_clip_id") or clip.get("clip_id") or clip.get("id") or "",
                    "source_start": seconds_to_timecode(start, ms=True),
                    "source_end": seconds_to_timecode(end, ms=True),
                    "target_start": seconds_to_timecode(target, ms=True),
                    "duration_seconds": round(duration, 3),
                    "original_audio_volume": 1.0,
                    "keep_original_audio": True,
                    "role": clip.get("role", ""),
                    "selection_reason": clip.get("selection_reason", ""),
                    "boundary_reason": clip.get("boundary_reason", ""),
                    "risk_level": clip.get("risk_level", ""),
                    "risk_notes": clip.get("risk_notes", ""),
                    "transition_after": clip.get("transition_after", "hard_cut"),
                    "crop_mode": "fit_blur" if self.options.aspect_ratio == "9:16" else "original",
                })
                target += duration
                if len(clips) >= self.options.reassembly_max_clip_count:
                    break
            if not clips:
                blocked_reasons.append("no valid clips for reassembly")
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
        })
        renderable_count = sum(1 for video in output_videos if video.get("duration_status") != "blocked")
        blocked_count = len(output_videos) - renderable_count
        status = "success" if blocked_count == 0 else ("partial_success" if renderable_count else "failed")
        self._write_status(vdir, self._base_status("reassembly_cut_plan", version, input_hash, [out]))
        self._record_step(step="reassembly_cut_plan", version=version, status=status, output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out])
        print(f"completed: reassembly_cut_plan ({status})")
        if status == "failed":
            raise RuntimeError("reassembly_cut_plan has no available clips")

    def step_reassembly_render(self) -> None:
        render_slots = int(self.config.raw.get("gpu_limits", {}).get("render_slots", 1))
        with file_slot_lock("render", slots=render_slots):
            self._step_reassembly_render_impl()

    def _step_reassembly_render_impl(self) -> None:
        plan = self._load_step_json("reassembly_cut_plan")
        input_hash = stable_hash({"reassembly_cut_plan": self._step_version("reassembly_cut_plan")})
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

    def _render_reassembly_one(self, video: dict[str, Any], output_path: Path) -> None:
        source = self._source_video()
        temp_dir = ensure_dir(output_path.parent / "_clips")
        clip_files = []
        for idx, clip in enumerate(video.get("clips", []), start=1):
            clip_file = temp_dir / f"{video['reassembly_id']}_{idx:03d}.mp4"
            run_cmd([
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                clip["source_start"],
                "-to",
                clip["source_end"],
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
        input_hash = stable_hash({"cut_plan": self._step_version("cut_plan")})
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

    def _render_one(self, video: dict[str, Any], output_path: Path) -> None:
        source = self._source_video()
        temp_dir = ensure_dir(output_path.parent / "_clips")
        clip_files = []
        voice = video.get("voiceover", {})
        voiceover_enabled = bool(voice.get("enabled") and voice.get("file"))
        for idx, clip in enumerate(video.get("clips", []), start=1):
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
                clip["source_start"],
                "-to",
                clip["source_end"],
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
        subtitle = video.get("subtitles", {})
        if subtitle.get("burn_in") and subtitle.get("file"):
            sub = str(self.task_dir / subtitle["file"]).replace("\\", "/").replace(":", "\\:")
            style = self._subtitle_force_style()
            run_cmd(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(current), "-vf", f"subtitles='{sub}':force_style='{style}'", "-c:a", "copy", str(output_path)])
        else:
            shutil.copy2(current, output_path)

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
        font_name = str(cfg.get("font_name", "Microsoft YaHei"))
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
                "text": re.sub(r"<\s*\|\s*.*?\s*\|>", "", seg.get("text", "")).strip(),
            })
        if not out and full_text:
            out.append({"id": 1, "start": 0, "end": 0, "start_time": "00:00:00.000", "end_time": "00:00:00.000", "speaker": "", "text": full_text})
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
        override_path = self.task_dir / step / self._step_version(step) / target_id / "manual_override.json"
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
    parser.add_argument("--only-analysis", action="store_true", help="只跑到风险审核，不生成配音和视频")
    parser.add_argument("--target-duration", type=int, default=int(default_config.short_video.get("default_target_seconds", 30)), dest="target_duration_seconds")
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
    if args.production_mode == "highlight_reassembly":
        args.audio_policy = "original"
        args.skip_tts = True
        args.require_tts = False
        args.allow_original_audio_evidence = True
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
