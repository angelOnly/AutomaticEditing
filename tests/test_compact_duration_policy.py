from __future__ import annotations

import pytest

from newsclip_agent.duration_policy import DurationSettings
from newsclip_agent.pipeline import PipelineRunner


def _runner() -> PipelineRunner:
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.duration_settings = DurationSettings(
        compact_min_target_ratio=0.85,
        compact_duration_tolerance_seconds=1.0,
        compact_severe_short_ratio=0.75,
    )
    return runner


def _voiceover_output(char_count: int, *, duration_fit: str = "ok") -> dict:
    return {
        "scripts": [
            {
                "short_video_id": "v_001",
                "target_duration_seconds": 55,
                "duration_fit": duration_fit,
                "narration_text": "a" * char_count,
                "narrative_sections": {
                    "opening": "ok",
                    "background": "ok",
                    "core_fact": "ok",
                    "analysis_or_conflict": "ok",
                    "ending": "ok",
                },
            }
        ]
    }


def test_compact_duration_slightly_below_min_should_warn_not_raise() -> None:
    runner = _runner()
    block_reason, warning = runner._compact_duration_block_reason(
        video_duration=46.5,
        target_duration=55.0,
        min_compact_duration=55.0 * 0.85,
    )

    assert block_reason is None
    assert warning is not None
    assert "实际时长 46.5 秒" in warning
    assert "允许继续渲染" in warning


def test_compact_duration_below_min_beyond_tolerance_should_raise() -> None:
    runner = _runner()
    block_reason, warning = runner._compact_duration_block_reason(
        video_duration=42.0,
        target_duration=55.0,
        min_compact_duration=55.0 * 0.85,
    )

    assert block_reason is not None
    assert warning == block_reason
    assert "超过容错范围" in block_reason


def test_compact_duration_severely_short_should_raise() -> None:
    runner = _runner()
    block_reason, warning = runner._compact_duration_block_reason(
        video_duration=30.0,
        target_duration=55.0,
        min_compact_duration=55.0 * 0.85,
    )

    assert block_reason is not None
    assert warning == block_reason
    assert "低于目标时长的 75%" in block_reason


def test_compact_duration_416_seconds_passes_with_65_percent_min_ratio() -> None:
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.duration_settings = DurationSettings(
        compact_min_target_ratio=0.65,
        compact_duration_tolerance_seconds=1.0,
        compact_severe_short_ratio=0.65,
    )

    block_reason, warning = runner._compact_duration_block_reason(
        video_duration=41.6,
        target_duration=55.0,
        min_compact_duration=55.0 * 0.65,
    )

    assert block_reason is None
    assert warning is None


def test_compact_duration_36_seconds_passes_with_65_percent_effective_floor() -> None:
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.duration_settings = DurationSettings(
        compact_min_target_ratio=0.65,
        compact_duration_tolerance_seconds=1.0,
        compact_severe_short_ratio=0.65,
    )

    block_reason, warning = runner._compact_duration_block_reason(
        video_duration=36.0,
        target_duration=55.0,
        min_compact_duration=55.0 * 0.65,
    )

    assert block_reason is None
    assert warning is None


def test_voiceover_chars_severely_short_should_raise() -> None:
    runner = _runner()

    with pytest.raises(RuntimeError, match="severely too short"):
        runner._validate_voiceover_script_duration_or_raise(_voiceover_output(130))


def test_voiceover_chars_slightly_short_should_warn_not_raise(capsys: pytest.CaptureFixture[str]) -> None:
    runner = _runner()

    runner._validate_voiceover_script_duration_or_raise(_voiceover_output(155))

    captured = capsys.readouterr()
    assert "Voiceover duration soft warnings" in captured.out
    assert "slightly short" in captured.out


def test_voiceover_chars_ok_should_pass(capsys: pytest.CaptureFixture[str]) -> None:
    runner = _runner()

    runner._validate_voiceover_script_duration_or_raise(_voiceover_output(170))

    captured = capsys.readouterr()
    assert captured.out == ""


def test_voiceover_duration_fit_too_short_still_raises() -> None:
    runner = _runner()

    with pytest.raises(RuntimeError, match="duration_fit=too_short"):
        runner._validate_voiceover_script_duration_or_raise(_voiceover_output(170, duration_fit="too_short"))
