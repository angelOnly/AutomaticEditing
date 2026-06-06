from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from newsclip_agent.duration_policy import DurationSettings
from newsclip_agent.pipeline import PipelineRunner


def _runner(tmp_path: Path | None = None) -> PipelineRunner:
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.options = SimpleNamespace(
        target_duration_seconds=None,
        max_output_video_seconds=None,
        aspect_ratio="16:9",
    )
    runner.config = SimpleNamespace(raw={"voiceover": {}})
    runner.duration_settings = DurationSettings(default_target_seconds=60, inter_sentence_gap_seconds=0.25)
    if tmp_path is not None:
        runner.task_dir = tmp_path
    return runner


def test_segment_start_end_accepts_numeric_source_fields() -> None:
    runner = _runner()

    start, end = runner._segment_start_end_seconds({
        "source_start_seconds": 12.0,
        "source_end_seconds": 20.0,
    })

    assert start == 12.0
    assert end == 20.0


def test_build_clips_records_invalid_missing_start() -> None:
    runner = _runner()

    clips, invalid = runner._build_clips_from_editing_structure([
        {"shot_id": "v_001_s01", "source_id": "source_1", "duration_seconds": 8}
    ])

    assert clips == []
    assert invalid[0]["reason"] == "missing_start_or_end"


def test_voiceover_alignment_blocks_rewritten_shot_ids(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    edit_plan = {
        "scripts": [
            {
                "short_video_id": "v_001",
                "editing_structure": [
                    {"shot_id": "v_001_s01", "narration_min_chars": 1, "narration_max_chars": 20},
                    {"shot_id": "v_001_s02", "narration_min_chars": 1, "narration_max_chars": 20},
                ],
            }
        ]
    }
    voiceover = {
        "scripts": [
            {
                "short_video_id": "v_001",
                "narration_segments": [
                    {"shot_id": "shot_1", "text": "one"},
                    {"shot_id": "shot_2", "text": "two"},
                ],
            }
        ]
    }

    with pytest.raises(RuntimeError, match="does not match editing_structure"):
        runner._validate_voiceover_script_against_editing_structure(voiceover, edit_plan)


def test_tts_duration_reconcile_marks_too_long(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    editing = {
        "scripts": [
            {
                "short_video_id": "v_001",
                "editing_structure": [
                    {
                        "shot_id": "v_001_s01",
                        "target_duration_seconds": 8,
                        "min_duration_seconds": 5,
                        "max_duration_seconds": 10,
                    }
                ],
            }
        ]
    }
    voiceover = {
        "scripts": [
            {"short_video_id": "v_001", "narration_segments": [{"shot_id": "v_001_s01", "text": "测试文案"}]}
        ]
    }
    tts = {
        "outputs": [
            {
                "short_video_id": "v_001",
                "segments": [{"shot_id": "v_001_s01", "status": "success", "actual_duration_seconds": 15}],
            }
        ]
    }

    result = runner._build_tts_duration_reconcile(editing, voiceover, tts)

    assert result["ok"] is False
    assert result["videos"][0]["segments"][0]["status"] == "too_long"


def test_compact_clips_clamps_and_keeps_unmatched() -> None:
    runner = _runner()
    clips = [
        {
            "shot_id": "shot_001",
            "source_start": "00:00:00.000",
            "source_end": "00:00:12.000",
            "duration_seconds": 8,
            "target_duration_seconds": 8,
            "min_duration_seconds": 5,
            "max_duration_seconds": 10,
        },
        {"shot_id": "shot_002", "source_start": "00:00:20.000", "source_end": "00:00:26.000", "duration_seconds": 6},
    ]
    segments = [{"shot_id": "shot_001", "status": "success", "actual_duration_seconds": 15.0}]

    compacted = runner._compact_clips_to_voiceover_segments(clips, segments)

    assert [clip["shot_id"] for clip in compacted] == ["shot_001", "shot_002"]
    assert compacted[0]["duration_seconds"] == 10.0
    assert compacted[0]["compact_voiceover_duration_reason"] == "tts_too_long_clamped"
    assert compacted[1]["compact_voiceover_unmatched"] is True
