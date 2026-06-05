from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .text_policy import reporter_byline_must_not_include
from .utils import seconds_to_timecode, timecode_to_seconds


@dataclass(frozen=True)
class DurationSettings:
    default_target_seconds: int = 30
    quick_news_min_seconds: int = 20
    quick_news_max_seconds: int = 30
    normal_min_seconds: int = 28
    normal_max_seconds: int = 35
    context_max_seconds: int = 45
    complex_max_seconds: int = 60
    hard_max_without_confirmation: int = 60
    allow_long_video_default: bool = False
    max_long_video_seconds: int = 90
    chars_per_second: float = 4.2
    default_original_audio_volume: float = 0.0
    evidence_original_audio_volume: float = 1.0
    background_original_audio_volume: float = 0.08
    voiceover_volume: float = 1.0
    tts_required_by_default: bool = True
    ai_voiceover_min_ratio: float = 0.80
    duration_mismatch_block_threshold_seconds: float = 1.0
    ai_voiceover_compact_to_tts: bool = True
    ai_voiceover_mismatch_policy: str = "block"
    ai_voiceover_original_audio_max_seconds: float = 6.0
    ai_voiceover_original_audio_max_ratio: float = 0.15
    ai_voiceover_original_audio_single_max_seconds: float = 4.0
    original_audio_value_threshold: float = 7.0
    ambience_original_audio_min_seconds: float = 2.0
    ambience_original_audio_max_seconds: float = 3.0
    evidence_original_audio_min_seconds: float = 6.0
    evidence_original_audio_max_seconds: float = 8.0
    core_quote_original_audio_min_seconds: float = 8.0
    core_quote_original_audio_max_seconds: float = 12.0
    core_quote_original_audio_max_ratio: float = 0.25
    suppress_unplanned_original_audio: bool = True
    compact_voiceover_default: bool = True
    segment_aligned_only_for_evidence: bool = True
    inter_sentence_gap_seconds: float = 0.28
    max_inter_sentence_gap_seconds: float = 0.8
    max_total_silence_ratio: float = 0.15
    subtitle_follow_actual_tts: bool = True
    tts_actual_chars_per_second: float = 5.2
    ai_voiceover_disable_original_audio: bool = True
    ai_voiceover_allow_model_original_audio: bool = False
    allow_original_audio_evidence: bool = False
    compact_min_target_ratio: float = 0.65
    compact_duration_tolerance_seconds: float = 1.0
    compact_severe_short_ratio: float = 0.65
    long_source_threshold_seconds: float = 600.0
    long_source_min_total_output_seconds: float = 90.0
    long_source_preferred_total_output_seconds: float = 150.0
    long_source_min_video_count: int = 2


def load_duration_settings(config: Any) -> DurationSettings:
    short_video = getattr(config, "short_video", {}) or {}
    voiceover = getattr(config, "voiceover", {}) or {}
    return DurationSettings(
        default_target_seconds=int(short_video.get("default_target_seconds", 30)),
        quick_news_min_seconds=int(short_video.get("quick_news_min_seconds", 20)),
        quick_news_max_seconds=int(short_video.get("quick_news_max_seconds", 30)),
        normal_min_seconds=int(short_video.get("normal_min_seconds", 28)),
        normal_max_seconds=int(short_video.get("normal_max_seconds", 35)),
        context_max_seconds=int(short_video.get("context_max_seconds", 45)),
        complex_max_seconds=int(short_video.get("complex_max_seconds", 60)),
        hard_max_without_confirmation=int(short_video.get("hard_max_without_confirmation", 60)),
        allow_long_video_default=bool(short_video.get("allow_long_video_default", False)),
        max_long_video_seconds=int(short_video.get("max_long_video_seconds", 90)),
        chars_per_second=float(voiceover.get("chars_per_second", 4.2)),
        default_original_audio_volume=float(voiceover.get("default_original_audio_volume", 0.0)),
        evidence_original_audio_volume=float(voiceover.get("evidence_original_audio_volume", 1.0)),
        background_original_audio_volume=float(voiceover.get("background_original_audio_volume", 0.08)),
        voiceover_volume=float(voiceover.get("voiceover_volume", 1.0)),
        tts_required_by_default=bool(voiceover.get("tts_required_by_default", True)),
        ai_voiceover_min_ratio=float(voiceover.get("ai_voiceover_min_ratio", 0.80)),
        duration_mismatch_block_threshold_seconds=float(voiceover.get("duration_mismatch_block_threshold_seconds", 1.0)),
        ai_voiceover_compact_to_tts=bool(voiceover.get("ai_voiceover_compact_to_tts", True)),
        ai_voiceover_mismatch_policy=str(voiceover.get("ai_voiceover_mismatch_policy", "block")),
        ai_voiceover_original_audio_max_seconds=float(voiceover.get("ai_voiceover_original_audio_max_seconds", 6.0)),
        ai_voiceover_original_audio_max_ratio=float(voiceover.get("ai_voiceover_original_audio_max_ratio", 0.15)),
        ai_voiceover_original_audio_single_max_seconds=float(voiceover.get("ai_voiceover_original_audio_single_max_seconds", 4.0)),
        original_audio_value_threshold=float(voiceover.get("original_audio_value_threshold", 7.0)),
        ambience_original_audio_min_seconds=float(voiceover.get("ambience_original_audio_min_seconds", 2.0)),
        ambience_original_audio_max_seconds=float(voiceover.get("ambience_original_audio_max_seconds", 3.0)),
        evidence_original_audio_min_seconds=float(voiceover.get("evidence_original_audio_min_seconds", 6.0)),
        evidence_original_audio_max_seconds=float(voiceover.get("evidence_original_audio_max_seconds", 8.0)),
        core_quote_original_audio_min_seconds=float(voiceover.get("core_quote_original_audio_min_seconds", 8.0)),
        core_quote_original_audio_max_seconds=float(voiceover.get("core_quote_original_audio_max_seconds", 12.0)),
        core_quote_original_audio_max_ratio=float(voiceover.get("core_quote_original_audio_max_ratio", 0.25)),
        suppress_unplanned_original_audio=bool(voiceover.get("suppress_unplanned_original_audio", True)),
        compact_voiceover_default=bool(voiceover.get("compact_voiceover_default", True)),
        segment_aligned_only_for_evidence=bool(voiceover.get("segment_aligned_only_for_evidence", True)),
        inter_sentence_gap_seconds=float(voiceover.get("inter_sentence_gap_seconds", 0.28)),
        max_inter_sentence_gap_seconds=float(voiceover.get("max_inter_sentence_gap_seconds", 0.8)),
        max_total_silence_ratio=float(voiceover.get("max_total_silence_ratio", 0.15)),
        subtitle_follow_actual_tts=bool(voiceover.get("subtitle_follow_actual_tts", True)),
        tts_actual_chars_per_second=float(voiceover.get("tts_actual_chars_per_second", 5.2)),
        ai_voiceover_disable_original_audio=bool(voiceover.get("ai_voiceover_disable_original_audio", True)),
        ai_voiceover_allow_model_original_audio=bool(voiceover.get("ai_voiceover_allow_model_original_audio", False)),
        allow_original_audio_evidence=bool(voiceover.get("allow_original_audio_evidence", False)),
        compact_min_target_ratio=float(voiceover.get("compact_min_target_ratio", 0.65)),
        compact_duration_tolerance_seconds=float(voiceover.get("compact_duration_tolerance_seconds", 1.0)),
        compact_severe_short_ratio=float(voiceover.get("compact_severe_short_ratio", 0.65)),
        long_source_threshold_seconds=float(voiceover.get("long_source_threshold_seconds", 600.0)),
        long_source_min_total_output_seconds=float(voiceover.get("long_source_min_total_output_seconds", 90.0)),
        long_source_preferred_total_output_seconds=float(voiceover.get("long_source_preferred_total_output_seconds", 150.0)),
        long_source_min_video_count=int(voiceover.get("long_source_min_video_count", 2)),
    )


def clean_voiceover_text(text: str | None) -> str:
    value = text or ""
    value = re.sub(r"【停顿[0-9.]+秒】", "，", value)
    value = re.sub(r"\[[^\]]*pause[^\]]*\]", "，", value, flags=re.I)
    return re.sub(r"\s+", "", value).strip()


def count_cjkish_chars(text: str | None) -> int:
    return len(clean_voiceover_text(text))


def target_char_count(seconds: float, chars_per_second: float = 4.2) -> int:
    return max(1, int(round(float(seconds or 0) * chars_per_second)))


def estimate_text_duration_seconds(text: str | None, chars_per_second: float = 4.2) -> float:
    return round(count_cjkish_chars(text) / max(chars_per_second, 0.1), 2)


def classify_duration_tier(seconds: float, settings: DurationSettings | None = None) -> str:
    settings = settings or DurationSettings()
    value = float(seconds or 0)
    if settings.quick_news_min_seconds <= value <= settings.quick_news_max_seconds:
        return "quick"
    if value <= settings.normal_max_seconds:
        return "normal"
    if value <= settings.context_max_seconds:
        return "context"
    if value <= settings.complex_max_seconds:
        return "complex"
    return "long"


def voiceover_duration_fit(
    text: str | None,
    target_seconds: float,
    max_allowed_seconds: float | None = None,
    chars_per_second: float = 4.2,
) -> dict[str, Any]:
    char_count = count_cjkish_chars(text)
    target_chars = target_char_count(target_seconds, chars_per_second)
    max_chars = target_char_count(max_allowed_seconds or target_seconds, chars_per_second)
    min_chars = max(1, int(round(target_chars * 0.72)))
    estimated = estimate_text_duration_seconds(text, chars_per_second)
    if char_count > max_chars:
        fit = "too_long"
    elif char_count < min_chars:
        fit = "too_short"
    else:
        fit = "ok"
    return {
        "actual_char_count": char_count,
        "target_char_count": target_chars,
        "min_char_count": min_chars,
        "max_char_count": max_chars,
        "estimated_duration_seconds": estimated,
        "duration_fit": fit,
    }


def editing_structure_duration(editing_structure: list[dict[str, Any]]) -> float:
    total = 0.0
    for seg in editing_structure:
        duration = seg.get("duration_seconds")
        if duration is None:
            start = timecode_to_seconds(seg.get("source_start"))
            end = timecode_to_seconds(seg.get("source_end"))
            duration = max(0.0, end - start)
        total += max(0.0, float(duration or 0))
    return round(total, 3)


def validate_editing_duration(
    script: dict[str, Any],
    *,
    allow_long_video: bool,
    settings: DurationSettings | None = None,
) -> dict[str, Any]:
    settings = settings or DurationSettings()
    video_seconds = editing_structure_duration(script.get("editing_structure", []))
    target = float(script.get("target_duration_seconds") or script.get("recommended_duration_seconds") or settings.default_target_seconds)
    max_allowed = float(script.get("max_allowed_seconds") or max(target, settings.normal_max_seconds))
    blocked_reasons: list[str] = []
    if video_seconds > max_allowed + 0.01:
        blocked_reasons.append(f"画面总时长 {video_seconds:.1f}s 超过允许上限 {max_allowed:.1f}s")
    if video_seconds > settings.hard_max_without_confirmation and not allow_long_video:
        blocked_reasons.append(f"画面总时长 {video_seconds:.1f}s 超过 {settings.hard_max_without_confirmation}s，需确认长版")
    return {
        "target_duration_seconds": target,
        "max_allowed_seconds": max_allowed,
        "video_duration_seconds": video_seconds,
        "duration_tier": classify_duration_tier(video_seconds, settings),
        "duration_status": "blocked" if blocked_reasons else "ok",
        "blocked_reasons": blocked_reasons,
    }


def should_require_long_video_confirmation(
    seconds: float,
    *,
    allow_long_video: bool,
    settings: DurationSettings | None = None,
) -> bool:
    settings = settings or DurationSettings()
    return float(seconds or 0) > settings.hard_max_without_confirmation and not allow_long_video


def duration_mismatch_block_reason(
    *,
    video_duration: float,
    voiceover_duration: float,
    audio_policy: str,
    tts_success: bool,
    threshold_seconds: float = 1.0,
) -> str:
    if audio_policy == "original" or not tts_success or not voiceover_duration:
        return ""
    delta = abs(float(video_duration or 0) - float(voiceover_duration or 0))
    if delta <= threshold_seconds:
        return ""
    return (
        f"画面总时长 {float(video_duration or 0):.1f}s 与 AI 配音 {float(voiceover_duration or 0):.1f}s "
        f"误差 {delta:.1f}s，超过 {threshold_seconds:.0f} 秒"
    )


def compact_voiceover_segment_times(
    segments: list[dict[str, Any]],
    gap_seconds: float,
) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    cursor = 0.0
    gap = max(0.0, float(gap_seconds or 0))
    active = [
        segment for segment in segments
        if segment.get("status") == "success" and float(segment.get("actual_duration_seconds") or 0) > 0
    ]
    for index, segment in enumerate(active):
        item = dict(segment)
        actual = float(item.get("actual_duration_seconds") or 0)
        original_start = item.get("target_start_seconds")
        original_end = item.get("target_end_seconds")
        item["target_start_seconds_original"] = original_start
        item["target_end_seconds_original"] = original_end
        item["target_start_seconds"] = round(cursor, 3)
        item["target_end_seconds"] = round(cursor + actual, 3)
        item["target_duration_seconds"] = round(actual, 3)
        compacted.append(item)
        cursor += actual
        if index < len(active) - 1:
            cursor += gap
    return compacted


def validate_voiceover_gaps(
    segments: list[dict[str, Any]],
    total_duration: float,
    settings: DurationSettings | None = None,
) -> list[str]:
    settings = settings or DurationSettings()
    issues: list[str] = []
    total_silence = 0.0
    for segment in segments:
        target_duration = float(segment.get("target_duration_seconds") or 0)
        actual_duration = float(segment.get("actual_duration_seconds") or 0)
        if target_duration <= 0 or actual_duration <= 0:
            continue
        gap = max(0.0, target_duration - actual_duration)
        total_silence += gap
        if gap > settings.max_inter_sentence_gap_seconds:
            issues.append(
                f"{segment.get('shot_id', '')} AI voiceover gap {gap:.2f}s exceeds "
                f"{settings.max_inter_sentence_gap_seconds:.2f}s"
            )
    total = float(total_duration or 0)
    if total > 0 and total_silence / total > settings.max_total_silence_ratio:
        issues.append(
            f"AI voiceover total silence {total_silence:.2f}s ratio "
            f"{total_silence / total:.2%} exceeds {settings.max_total_silence_ratio:.2%}"
        )
    return issues


def _window_seconds(item: dict[str, Any]) -> tuple[float, float]:
    start = item.get("target_start_seconds")
    end = item.get("target_end_seconds")
    if start is None:
        start = timecode_to_seconds(item.get("target_start"))
    if end is None:
        end = timecode_to_seconds(item.get("target_end"))
    if not end:
        duration = float(item.get("duration_seconds") or item.get("target_duration_seconds") or 0)
        end = float(start or 0) + duration
    return max(0.0, float(start or 0)), max(0.0, float(end or 0))


def voiceover_active_windows_from_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for index, segment in enumerate(segments, start=1):
        text = clean_voiceover_text(segment.get("text"))
        if not text:
            continue
        start, end = _window_seconds(segment)
        if end <= start:
            continue
        windows.append({
            "shot_id": segment.get("shot_id") or f"shot_{index:03d}",
            "target_start_seconds": round(start, 3),
            "target_end_seconds": round(end, 3),
            "text": text,
        })
    return windows


def evidence_audio_windows_from_clips(clips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for index, clip in enumerate(clips, start=1):
        mode = clip.get("audio_mode")
        volume = float(clip.get("original_audio_volume") or 0)
        if mode not in {"mixed_evidence", "original_sound"} or not clip.get("keep_original_audio") or volume <= 0:
            continue
        start, end = _window_seconds(clip)
        if end <= start:
            continue
        windows.append({
            "clip_index": index,
            "audio_mode": mode,
            "target_start_seconds": round(start, 3),
            "target_end_seconds": round(end, 3),
            "original_audio_volume": volume,
            "reason": clip.get("original_audio_reason") or clip.get("reason") or "",
        })
        for key in (
            "original_audio_type",
            "original_audio_policy",
            "original_audio_value_score",
            "original_audio_complete_unit",
            "original_audio_transcript_summary",
        ):
            if key in clip:
                windows[-1][key] = clip.get(key)
    return windows


def is_ai_voiceover_policy(audio_policy: str | None) -> bool:
    return audio_policy in {"ai_voiceover", "ai_voiceover_main"}


def _original_audio_evidence_allowed(
    *,
    audio_policy: str | None,
    settings: DurationSettings,
    allow_original_audio_evidence: bool | None = None,
) -> bool:
    explicit_allow = bool(allow_original_audio_evidence or settings.allow_original_audio_evidence)
    if is_ai_voiceover_policy(audio_policy):
        if not settings.ai_voiceover_disable_original_audio:
            return True
        return explicit_allow or settings.ai_voiceover_allow_model_original_audio
    return audio_policy in {"mixed", "mixed_ai_voiceover", "original", "original_audio"} or explicit_allow


def should_build_evidence_audio_windows(
    *,
    audio_policy: str,
    settings: DurationSettings,
    allow_original_audio_evidence: bool = False,
) -> bool:
    return _original_audio_evidence_allowed(
        audio_policy=audio_policy,
        settings=settings,
        allow_original_audio_evidence=allow_original_audio_evidence,
    )


def normalize_audio_mode_for_policy(
    clip: dict[str, Any],
    *,
    audio_policy: str,
    settings: DurationSettings,
    allow_original_audio_evidence: bool = False,
) -> dict[str, Any]:
    item = dict(clip)
    if (
        is_ai_voiceover_policy(audio_policy)
        and settings.ai_voiceover_disable_original_audio
        and not _original_audio_evidence_allowed(
            audio_policy=audio_policy,
            settings=settings,
            allow_original_audio_evidence=allow_original_audio_evidence,
        )
        and item.get("audio_mode") in {"mixed_evidence", "original_sound"}
    ):
        item["audio_mode_original"] = item.get("audio_mode")
        item["original_audio_policy_original"] = item.get("original_audio_policy")
        item["audio_mode"] = "ai_voiceover"
        item["keep_original_audio"] = False
        item["original_audio_volume"] = 0.0
        item["original_audio_policy"] = "omit"
        item["original_audio_downgraded_reason"] = "AI voiceover mode disables original audio by default"
    return item


def _original_audio_value_score(clip: dict[str, Any]) -> float | None:
    value = clip.get("original_audio_value_score")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _original_audio_type(clip: dict[str, Any]) -> str:
    value = str(clip.get("original_audio_type") or clip.get("audio_evidence_type") or "").strip()
    return value or "evidence_quote"


def _original_audio_policy(clip: dict[str, Any]) -> str:
    value = str(clip.get("original_audio_policy") or "").strip()
    return value or "evidence_window"


def original_audio_duration_bounds(
    clip: dict[str, Any],
    settings: DurationSettings | None = None,
) -> tuple[float, float]:
    settings = settings or DurationSettings()
    audio_type = _original_audio_type(clip)
    policy = _original_audio_policy(clip)
    if audio_type in {"ambience", "ambient", "environment"} or policy == "ambience_only":
        return settings.ambience_original_audio_min_seconds, settings.ambience_original_audio_max_seconds
    if audio_type in {"core_quote", "authority_quote"} or policy == "core_quote":
        return settings.core_quote_original_audio_min_seconds, settings.core_quote_original_audio_max_seconds
    return settings.evidence_original_audio_min_seconds, settings.evidence_original_audio_max_seconds


def window_overlap_seconds(a: dict[str, Any], b: dict[str, Any]) -> float:
    a_start, a_end = _window_seconds(a)
    b_start, b_end = _window_seconds(b)
    return round(max(0.0, min(a_end, b_end) - max(a_start, b_start)), 3)


def voiceover_original_overlap_seconds(
    voiceover_windows: list[dict[str, Any]],
    evidence_windows: list[dict[str, Any]],
) -> float:
    total = 0.0
    for voice_window in voiceover_windows:
        for evidence_window in evidence_windows:
            total += window_overlap_seconds(voice_window, evidence_window)
    return round(total, 3)


def validate_voiceover_silence_for_evidence(
    voiceover_segments: list[dict[str, Any]],
    evidence_windows: list[dict[str, Any]],
) -> list[str]:
    issues: list[str] = []
    active_windows = voiceover_active_windows_from_segments(voiceover_segments)
    for voice_window in active_windows:
        for evidence_window in evidence_windows:
            overlap = window_overlap_seconds(voice_window, evidence_window)
            if overlap > 0:
                issues.append(
                    f"证据原声窗口 {evidence_window['target_start_seconds']:.1f}-"
                    f"{evidence_window['target_end_seconds']:.1f}s 内存在 AI 配音 "
                    f"{voice_window.get('shot_id', '')}，重叠 {overlap:.1f}s"
                )
    return issues


def build_voiceover_timing_contract(
    script: dict[str, Any],
    plan_item: dict[str, Any] | None = None,
    *,
    chars_per_second: float = 4.2,
) -> dict[str, Any]:
    plan_item = plan_item or {}
    target = float(script.get("target_duration_seconds") or script.get("recommended_duration_seconds") or plan_item.get("target_duration_seconds") or 30)
    shots = []
    cursor = 0.0
    for idx, seg in enumerate(script.get("editing_structure", []), start=1):
        start = timecode_to_seconds(seg.get("source_start"))
        end = timecode_to_seconds(seg.get("source_end"))
        duration = float(seg.get("duration_seconds") or max(0.0, end - start))
        if duration <= 0:
            continue
        char_max = max(4, target_char_count(duration, chars_per_second))
        char_min = max(0, int(round(char_max * 0.65)))
        must_not_include = list(plan_item.get("must_not_include", []))
        must_not_include.extend(reporter_byline_must_not_include())
        shots.append({
            "shot_id": f"shot_{idx:03d}",
            "order": int(seg.get("order") or idx),
            "target_start": seconds_to_timecode(cursor, ms=True),
            "target_end": seconds_to_timecode(cursor + duration, ms=True),
            "target_duration_seconds": round(duration, 3),
            "source_start": seconds_to_timecode(start, ms=True),
            "source_end": seconds_to_timecode(end, ms=True),
            "visual_summary": seg.get("visual") or seg.get("visual_selection_reason") or seg.get("subtitle") or "",
            "news_fact_to_explain": seg.get("purpose") or seg.get("editing_note") or "",
            "visual_evidence_type": seg.get("visual_evidence_type", ""),
            "audio_mode": seg.get("audio_mode") or ("original_sound" if seg.get("original_audio_required") else "ai_voiceover"),
            "original_audio_required": bool(seg.get("original_audio_required")),
            "target_char_min": char_min,
            "target_char_max": char_max,
            "must_include": plan_item.get("must_keep_fact_points", []),
            "must_not_include": must_not_include,
        })
        cursor += duration
    return {
        "short_video_id": script.get("short_video_id", ""),
        "target_duration_seconds": target,
        "estimated_chars_per_second": chars_per_second,
        "news_angle": plan_item.get("news_angle", ""),
        "must_keep_fact_points": plan_item.get("must_keep_fact_points", []),
        "shots": shots,
    }


def original_audio_volume_for_clip(
    clip: dict[str, Any],
    *,
    audio_policy: str,
    settings: DurationSettings | None = None,
    allow_original_audio_evidence: bool | None = None,
) -> float:
    settings = settings or DurationSettings()
    if audio_policy in {"original", "original_audio"}:
        return 1.0
    if is_ai_voiceover_policy(audio_policy) and not _original_audio_evidence_allowed(
        audio_policy=audio_policy,
        settings=settings,
        allow_original_audio_evidence=allow_original_audio_evidence,
    ):
        return 0.0
    mode = clip.get("audio_mode") or ("mixed_evidence" if clip.get("original_audio_required") else "ai_voiceover")
    if mode not in {"original_sound", "mixed_evidence"}:
        if audio_policy == "mixed":
            return settings.background_original_audio_volume
        return settings.default_original_audio_volume
    if _original_audio_policy(clip) == "omit":
        return 0.0
    score = _original_audio_value_score(clip)
    if score is not None and score < settings.original_audio_value_threshold:
        return 0.0
    if mode in {"original_sound", "mixed_evidence"}:
        return settings.evidence_original_audio_volume
    if audio_policy == "mixed":
        return settings.background_original_audio_volume
    return settings.default_original_audio_volume


def validate_audio_policy(
    video: dict[str, Any],
    *,
    require_tts: bool = True,
    settings: DurationSettings | None = None,
) -> dict[str, Any]:
    settings = settings or DurationSettings()
    issues: list[str] = []
    voiceover = video.get("voiceover", {})
    if require_tts and not voiceover.get("enabled"):
        issues.append("require_tts=true 但 voiceover 未启用")
    audio_policy = video.get("audio_policy")
    original_audio_checks_enabled = _original_audio_evidence_allowed(
        audio_policy=audio_policy,
        settings=settings,
        allow_original_audio_evidence=video.get("allow_original_audio_evidence"),
    )
    video_duration = float(video.get("video_duration_seconds") or 0)
    voiceover_duration = float(video.get("voiceover_duration_seconds") or voiceover.get("actual_duration_seconds") or 0)
    evidence_total = 0.0
    for clip in video.get("clips", []):
        mode = clip.get("audio_mode")
        volume = float(clip.get("original_audio_volume") or 0)
        reason = clip.get("original_audio_reason") or clip.get("reason") or clip.get("visual_selection_reason") or ""
        duration = float(clip.get("duration_seconds") or 0)
        if original_audio_checks_enabled and mode == "mixed_evidence" and not reason:
            issues.append("mixed_evidence 片段必须提供 reason")
        if is_ai_voiceover_policy(audio_policy) and mode not in {"mixed_evidence", "original_sound"} and volume > 0:
            issues.append("ai_voiceover 策略下普通片段原声应为 0")
        if original_audio_checks_enabled and mode in {"mixed_evidence", "original_sound"}:
            score = _original_audio_value_score(clip)
            audio_type = _original_audio_type(clip)
            policy = _original_audio_policy(clip)
            if volume > 0:
                evidence_total += duration
            if policy == "omit" and volume > 0:
                issues.append("original_audio_policy=omit must not keep original audio")
            if score is not None and score < settings.original_audio_value_threshold:
                issues.append(
                    f"original audio value score {score:.1f} is below "
                    f"{settings.original_audio_value_threshold:.1f}"
                )
            if not reason:
                issues.append(f"{mode} 片段必须提供 original_audio_reason")
            min_seconds, max_seconds = original_audio_duration_bounds(clip, settings)
            if volume > 0 and duration < min_seconds - 0.01 and not clip.get("allow_short_original_audio"):
                issues.append(f"{audio_type} original audio {duration:.1f}s is shorter than {min_seconds:.1f}s semantic minimum")
            if duration > max_seconds + 0.01:
                issues.append(f"{audio_type} original audio {duration:.1f}s exceeds {max_seconds:.1f}s limit")
            if audio_type in {"evidence_quote", "reporter_standup", "authority_quote", "core_quote"}:
                if not clip.get("original_audio_transcript_summary"):
                    issues.append(f"{audio_type} original audio must include transcript summary")
                if clip.get("original_audio_complete_unit") is False:
                    issues.append(f"{audio_type} original audio must be a complete semantic unit")
            if False and duration > settings.ai_voiceover_original_audio_single_max_seconds + 0.01:
                issues.append(
                    f"{mode} 单段原声 {duration:.1f}s 超过 "
                    f"{settings.ai_voiceover_original_audio_single_max_seconds:.1f}s 上限"
                )
    if is_ai_voiceover_policy(audio_policy) and original_audio_checks_enabled and video_duration > 0:
        has_core_quote = any(
            _original_audio_type(clip) in {"core_quote", "authority_quote"} or _original_audio_policy(clip) == "core_quote"
            for clip in video.get("clips", [])
        )
        max_seconds = settings.core_quote_original_audio_max_seconds if has_core_quote else settings.ai_voiceover_original_audio_max_seconds
        max_ratio = settings.core_quote_original_audio_max_ratio if has_core_quote else settings.ai_voiceover_original_audio_max_ratio
        max_original = min(max_seconds, video_duration * max_ratio)
        if evidence_total > max_original + 0.01:
            issues.append(f"AI 配音策略下原声总时长 {evidence_total:.1f}s 超过 {max_original:.1f}s 上限")
        if voiceover_duration > 0:
            ratio = voiceover_duration / video_duration
            if ratio + 0.001 < settings.ai_voiceover_min_ratio:
                issues.append(f"AI 配音覆盖比例 {ratio:.2f} 低于 {settings.ai_voiceover_min_ratio:.2f}")
    if is_ai_voiceover_policy(audio_policy) and not original_audio_checks_enabled and video_duration > 0 and voiceover_duration > 0:
        ratio = voiceover_duration / video_duration
        if ratio + 0.001 < settings.ai_voiceover_min_ratio:
            issues.append(f"AI voiceover coverage ratio {ratio:.2f} is below {settings.ai_voiceover_min_ratio:.2f}")
    evidence_windows = (
        video.get("evidence_audio_windows")
        or (evidence_audio_windows_from_clips(video.get("clips", [])) if original_audio_checks_enabled else [])
    )
    voiceover_segments = video.get("voiceover_segments") or voiceover.get("segments") or []
    if is_ai_voiceover_policy(audio_policy) and original_audio_checks_enabled and evidence_windows and voiceover_segments:
        issues.extend(validate_voiceover_silence_for_evidence(voiceover_segments, evidence_windows))
    if is_ai_voiceover_policy(audio_policy) and original_audio_checks_enabled and float(video.get("voiceover_original_overlap_seconds") or 0) > 0:
        issues.append("AI 配音与证据原声存在重叠")
    return {"status": "blocked" if issues else "ok", "issues": issues}
