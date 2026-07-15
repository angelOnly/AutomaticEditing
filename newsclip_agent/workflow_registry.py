from __future__ import annotations

from typing import Any


LEGACY_SINGLE_COMMON_REUSABLE_STEPS = [
    "source_prepare",
    "metadata",
    "source_quality_check",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "asr_digest",
    "vision",
    "timeline",
    "timeline_digest",
]

UNIFIED_SOURCE_COMMON_PROGRESS_STEPS = [
    "source_prepare",
    "source_analysis",
    "source_aggregate",
]

UNIFIED_SOURCE_COMMON_REUSABLE_STEPS = [
    "source_prepare",
    "source_analysis",
    "source_aggregate",
]


WORKFLOWS: dict[tuple[str, bool], list[str]] = {
    ("ai_voiceover", False): [
        "source_prepare",
        "metadata",
        "source_quality_check",
        "audio_extract",
        "frame_extract",
        "chunk_build",
        "asr",
        "asr_digest",
        "vision",
        "timeline",
        "timeline_digest",
        "asr_micro_segment",
        "content_analysis_preselect",
        "content_analysis",
        "short_video_edit_plan",
        "voiceover_script",
        "voiceover_quality_gate",
        "tts",
        "subtitles",
        "cut_plan",
        "news_quality_gate",
        "news_quality_ai_review",
        "render",
    ],
    ("highlight_reassembly", False): [
        "source_prepare",
        "metadata",
        "source_quality_check",
        "audio_extract",
        "frame_extract",
        "chunk_build",
        "asr",
        "asr_digest",
        "vision",
        "timeline",
        "timeline_digest",
        "video_understanding",
        "asr_event_candidate",
        "candidate_filter",
        "highlight_reassembly_plan",
        "reassembly_cut_plan",
        "news_quality_gate",
        "news_quality_ai_review",
        "reassembly_render",
        "reassembly_commentary",
    ],
    ("ai_voiceover", True): [
        "source_prepare",
        "source_analysis",
        "source_aggregate",
        "asr_micro_segment",
        "content_analysis_preselect",
        "content_analysis",
        "ai_voiceover_candidate_materialize",
        "short_video_edit_plan",
        "voiceover_script",
        "voiceover_quality_gate",
        "tts",
        "subtitles",
        "cut_plan",
        "news_quality_gate",
        "news_quality_ai_review",
        "render",
    ],
    ("highlight_reassembly", True): [
        "source_prepare",
        "source_analysis",
        "source_aggregate",
        "video_understanding",
        "asr_event_candidate",
        "candidate_filter",
        "highlight_reassembly_plan",
        "reassembly_cut_plan",
        "news_quality_gate",
        "news_quality_ai_review",
        "reassembly_render",
        "reassembly_commentary",
    ],
    ("full_concat", False): [
        "source_prepare",
        "metadata",
        "source_quality_check",
        "audio_extract",
        "frame_extract",
        "chunk_build",
        "asr",
        "asr_digest",
        "vision",
        "timeline",
        "timeline_digest",
        "ad_detection",
        "full_concat_boundary_refine",
        "full_concat_plan",
        "full_concat_plan_qc",
        "full_concat_render",
        "full_concat_output_qc",
    ],
    ("full_concat", True): [
        "source_prepare",
        "source_analysis",
        "source_aggregate",
        "ad_detection",
        "full_concat_boundary_refine",
        "full_concat_plan",
        "full_concat_plan_qc",
        "full_concat_render",
        "full_concat_output_qc",
    ],
}


DEPENDENCIES: dict[str, list[str]] = {
    "source_prepare": [],
    "source_analysis": [],
    "source_quality_check": ["source_analysis", "metadata"],
    "source_aggregate": ["source_analysis"],

    "metadata": [],
    "audio_extract": ["source_quality_check"],
    "frame_extract": ["source_quality_check"],
    "chunk_build": ["metadata", "frame_extract"],
    "asr": ["audio_extract"],
    "asr_digest": ["asr", "chunk_build"],
    "vision": ["frame_extract", "chunk_build", "asr", "asr_digest"],
    "timeline": ["asr", "asr_digest", "vision"],
    "timeline_digest": ["timeline", "asr_digest"],
    "asr_micro_segment": ["timeline_digest", "source_aggregate"],

    "content_analysis_preselect": ["asr_micro_segment", "timeline_digest", "source_aggregate"],
    "content_analysis": ["content_analysis_preselect", "asr_micro_segment", "timeline_digest", "source_aggregate"],
    "ai_voiceover_candidate_materialize": ["content_analysis", "asr_micro_segment"],
    "short_video_edit_plan": ["ai_voiceover_candidate_materialize"],
    "voiceover_script": ["short_video_edit_plan"],
    "voiceover_quality_gate": ["voiceover_script", "short_video_edit_plan"],
    "tts": ["voiceover_script", "voiceover_quality_gate"],
    "subtitles": ["voiceover_script", "tts"],
    "cut_plan": ["short_video_edit_plan", "voiceover_script", "subtitles", "tts"],
    "news_quality_gate": ["cut_plan", "reassembly_cut_plan"],
    "news_quality_ai_review": ["news_quality_gate"],

    "video_understanding": ["timeline_digest", "source_aggregate"],
    "highlight_detection": ["timeline_digest", "source_aggregate", "video_understanding"],
    "asr_event_candidate": ["timeline_digest", "source_aggregate", "video_understanding"],
    "candidate_filter": ["asr_event_candidate"],
    "highlight_reassembly_plan": ["candidate_filter", "video_understanding"],
    "reassembly_cut_plan": ["highlight_reassembly_plan", "candidate_filter"],
    "render": ["cut_plan", "news_quality_gate", "news_quality_ai_review"],
    "reassembly_render": ["reassembly_cut_plan", "news_quality_gate", "news_quality_ai_review"],
    "reassembly_commentary": ["reassembly_render"],

    "ad_detection": ["timeline_digest", "source_aggregate"],
    "full_concat_boundary_refine": ["ad_detection"],
    "full_concat_plan": ["full_concat_boundary_refine"],
    "full_concat_plan_qc": ["full_concat_plan", "full_concat_boundary_refine"],
    "full_concat_render": ["full_concat_plan", "full_concat_plan_qc"],
    "full_concat_output_qc": ["full_concat_render", "full_concat_plan"],
}


def use_unified_source_pipeline(config: Any, source_count: int = 1) -> bool:
    workflow = config.raw.get("workflow", {}) if hasattr(config, "raw") else {}
    return bool(workflow.get("use_unified_source_pipeline", True)) or source_count > 1


def step_order(production_mode: str, *, unified: bool) -> list[str]:
    return list(WORKFLOWS.get((production_mode, unified)) or WORKFLOWS[("ai_voiceover", unified)])


def common_reusable_steps(*, unified: bool) -> list[str]:
    return list(
        UNIFIED_SOURCE_COMMON_REUSABLE_STEPS
        if unified
        else LEGACY_SINGLE_COMMON_REUSABLE_STEPS
    )


def common_progress_steps(*, unified: bool) -> list[str]:
    return list(
        UNIFIED_SOURCE_COMMON_PROGRESS_STEPS
        if unified
        else LEGACY_SINGLE_COMMON_REUSABLE_STEPS
    )
