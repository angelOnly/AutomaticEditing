from __future__ import annotations

from newsclip_agent.duration_policy import editing_structure_duration, validate_editing_duration


def test_editing_structure_duration_from_timecodes() -> None:
    total = editing_structure_duration([
        {"source_start": "00:00:01.000", "source_end": "00:00:04.500"},
        {"source_start": "00:00:10.000", "source_end": "00:00:15.000"},
    ])

    assert total == 8.5


def test_blocks_when_over_max_allowed_seconds() -> None:
    script = {
        "target_duration_seconds": 30,
        "max_allowed_seconds": 35,
        "editing_structure": [{"duration_seconds": 40}],
    }

    result = validate_editing_duration(script, allow_long_video=False)

    assert result["duration_status"] == "blocked"
    assert "超过允许上限" in result["blocked_reasons"][0]


def test_blocks_over_60_without_confirmation() -> None:
    script = {
        "target_duration_seconds": 60,
        "max_allowed_seconds": 90,
        "editing_structure": [{"duration_seconds": 65}],
    }

    result = validate_editing_duration(script, allow_long_video=False)

    assert result["duration_status"] == "blocked"
    assert any("需确认长版" in reason for reason in result["blocked_reasons"])


def test_allows_over_60_with_confirmation_when_within_max() -> None:
    script = {
        "target_duration_seconds": 65,
        "max_allowed_seconds": 90,
        "editing_structure": [{"duration_seconds": 65}],
    }

    result = validate_editing_duration(script, allow_long_video=True)

    assert result["duration_status"] == "ok"
