from __future__ import annotations

from newsclip_agent.duration_policy import (
    build_voiceover_timing_contract,
    DurationSettings,
    classify_duration_tier,
    target_char_count,
    voiceover_duration_fit,
)


def test_30_second_target_char_budget() -> None:
    assert target_char_count(30, 4.2) == 126


def test_voiceover_marks_too_long() -> None:
    text = "中" * 160
    result = voiceover_duration_fit(text, target_seconds=30, max_allowed_seconds=35, chars_per_second=4.2)

    assert result["duration_fit"] == "too_long"


def test_voiceover_marks_too_short() -> None:
    result = voiceover_duration_fit("太短。", target_seconds=30, max_allowed_seconds=35, chars_per_second=4.2)

    assert result["duration_fit"] == "too_short"


def test_duration_tiers() -> None:
    settings = DurationSettings()

    assert classify_duration_tier(30, settings) == "quick"
    assert classify_duration_tier(40, settings) == "context"
    assert classify_duration_tier(55, settings) == "complex"
    assert classify_duration_tier(70, settings) == "long"


def test_voiceover_contract_bans_reporter_bylines() -> None:
    contract = build_voiceover_timing_contract({
        "short_video_id": "sv_001",
        "editing_structure": [
            {"source_start": "00:00:00.000", "source_end": "00:00:04.000", "visual": "发布会现场"}
        ],
    })

    assert "凤凰卫视记者" in contract["shots"][0]["must_not_include"]
