from __future__ import annotations

from newsclip_agent.duration_policy import (
    DurationSettings,
    compact_voiceover_segment_times,
    evidence_audio_windows_from_clips,
    validate_audio_policy,
    validate_voiceover_gaps,
    validate_voiceover_silence_for_evidence,
    voiceover_active_windows_from_segments,
    voiceover_original_overlap_seconds,
    window_overlap_seconds,
)
from newsclip_agent.pipeline import PipelineRunner


def test_evidence_windows_from_mixed_evidence_clips() -> None:
    windows = evidence_audio_windows_from_clips([
        {
            "target_start": "00:00:21.000",
            "duration_seconds": 4,
            "audio_mode": "mixed_evidence",
            "keep_original_audio": True,
            "original_audio_volume": 1.0,
        },
        {
            "target_start": "00:00:25.000",
            "duration_seconds": 8,
            "audio_mode": "ai_voiceover",
            "keep_original_audio": False,
            "original_audio_volume": 0.0,
        },
    ])

    assert windows == [
        {
            "clip_index": 1,
            "audio_mode": "mixed_evidence",
            "target_start_seconds": 21.0,
            "target_end_seconds": 25.0,
            "original_audio_volume": 1.0,
            "reason": "",
        }
    ]


def test_empty_narration_segment_creates_silence_window() -> None:
    windows = voiceover_active_windows_from_segments([
        {"shot_id": "shot_004", "target_start": "00:00:16.000", "target_end": "00:00:21.000", "text": "AI 解说"},
        {"shot_id": "shot_005", "target_start": "00:00:21.000", "target_end": "00:00:25.000", "text": ""},
        {"shot_id": "shot_006", "target_start": "00:00:25.000", "target_end": "00:00:33.000", "text": "AI 接回"},
    ])

    assert [item["shot_id"] for item in windows] == ["shot_004", "shot_006"]


def test_blocks_ai_voiceover_text_inside_evidence_window() -> None:
    issues = validate_voiceover_silence_for_evidence(
        [{"shot_id": "shot_005", "target_start": "00:00:21.000", "target_end": "00:00:25.000", "text": "不该出现的 AI"}],
        [{"target_start_seconds": 21.0, "target_end_seconds": 25.0}],
    )

    assert issues
    assert "重叠" in issues[0]


def test_allows_ai_voiceover_before_and_after_evidence_window() -> None:
    voice_windows = voiceover_active_windows_from_segments([
        {"shot_id": "shot_004", "target_start": "00:00:16.000", "target_end": "00:00:21.000", "text": "AI 解说"},
        {"shot_id": "shot_006", "target_start": "00:00:25.000", "target_end": "00:00:33.000", "text": "AI 接回"},
    ])
    evidence = [{"target_start_seconds": 21.0, "target_end_seconds": 25.0}]

    assert window_overlap_seconds(voice_windows[0], evidence[0]) == 0.0
    assert voiceover_original_overlap_seconds(voice_windows, evidence) == 0.0


def test_audio_policy_blocks_overlap_seconds() -> None:
    result = validate_audio_policy(
        {
            "audio_policy": "ai_voiceover_main",
            "allow_original_audio_evidence": True,
            "voiceover": {"enabled": True},
            "video_duration_seconds": 30,
            "voiceover_duration_seconds": 28,
            "voiceover_original_overlap_seconds": 1.2,
            "clips": [],
        },
        require_tts=True,
    )

    assert result["status"] == "blocked"
    assert any("重叠" in issue for issue in result["issues"])


def test_audio_policy_skips_overlap_when_ai_voiceover_original_disabled() -> None:
    result = validate_audio_policy(
        {
            "audio_policy": "ai_voiceover_main",
            "voiceover": {"enabled": True},
            "video_duration_seconds": 30,
            "voiceover_duration_seconds": 28,
            "voiceover_original_overlap_seconds": 1.2,
            "clips": [],
        },
        require_tts=True,
        settings=DurationSettings(ai_voiceover_disable_original_audio=True),
    )

    assert result["status"] == "ok"


def test_segments_to_srt_skips_empty_evidence_segment() -> None:
    runner = PipelineRunner.__new__(PipelineRunner)

    srt = runner._segments_to_srt([
        {"target_start": "00:00:16.000", "target_end": "00:00:21.000", "text": "AI 解说"},
        {"target_start": "00:00:21.000", "target_end": "00:00:25.000", "text": ""},
        {"target_start": "00:00:25.000", "target_end": "00:00:33.000", "text": "AI 接回"},
    ])

    assert "AI 解说" in srt
    assert "AI 接回" in srt
    assert "00:00:21,000 --> 00:00:25,000" not in srt


def test_compact_voiceover_segment_times_uses_actual_audio_and_gap() -> None:
    compacted = compact_voiceover_segment_times(
        [
            {
                "shot_id": "shot_001",
                "status": "success",
                "target_start_seconds": 0.0,
                "target_end_seconds": 8.0,
                "actual_duration_seconds": 6.32,
            },
            {
                "shot_id": "shot_002",
                "status": "success",
                "target_start_seconds": 8.0,
                "target_end_seconds": 18.0,
                "actual_duration_seconds": 5.92,
            },
        ],
        0.28,
    )

    assert compacted[0]["target_start_seconds"] == 0.0
    assert compacted[0]["target_end_seconds"] == 6.32
    assert compacted[1]["target_start_seconds"] == 6.6
    assert compacted[1]["target_end_seconds"] == 12.52
    assert compacted[1]["target_start_seconds_original"] == 8.0


def test_validate_voiceover_gaps_blocks_segment_aligned_dead_air() -> None:
    issues = validate_voiceover_gaps(
        [
            {"shot_id": "shot_001", "target_duration_seconds": 8, "actual_duration_seconds": 6.32},
            {"shot_id": "shot_002", "target_duration_seconds": 10, "actual_duration_seconds": 5.92},
        ],
        18,
        DurationSettings(max_inter_sentence_gap_seconds=0.8, max_total_silence_ratio=0.15),
    )

    assert issues
    assert any("shot_002" in issue for issue in issues)


def test_tts_segments_to_srt_uses_actual_segment_times() -> None:
    runner = PipelineRunner.__new__(PipelineRunner)

    srt = runner._tts_segments_to_srt([
        {"target_start_seconds": 6.6, "target_end_seconds": 12.52, "text": "compact subtitle"}
    ])

    assert "00:00:06,600 --> 00:00:12,520" in srt


def test_attach_voiceover_segment_timing_from_editing_structure(tmp_path) -> None:
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.task_dir = tmp_path
    runner._step_output = lambda step: "voiceover_script.json"
    voiceover = {
        "scripts": [
            {
                "short_video_id": "v_001",
                "narration_segments": [
                    {"shot_id": "v_001_s01", "text": "one"},
                    {"shot_id": "v_001_s02", "text": "two"},
                ],
            }
        ]
    }
    edit_plan = {
        "scripts": [
            {
                "short_video_id": "v_001",
                "editing_structure": [
                    {"shot_id": "v_001_s01", "duration_seconds": 4},
                    {"shot_id": "v_001_s02", "duration_seconds": 6},
                ],
            }
        ]
    }

    runner._attach_voiceover_script_timings(voiceover, edit_plan)

    segments = voiceover["scripts"][0]["narration_segments"]
    assert segments[0]["target_start"] == "00:00:00.000"
    assert segments[0]["target_end"] == "00:00:04.000"
    assert segments[1]["target_start_seconds"] == 4.0
    assert segments[1]["target_duration_seconds"] == 6.0


def test_compact_clips_to_voiceover_segments_aligns_by_shot_id() -> None:
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.duration_settings = DurationSettings(inter_sentence_gap_seconds=0.25)

    clips = [
        {"shot_id": "shot_002", "source_start": "00:00:20.000", "source_end": "00:00:30.000", "duration_seconds": 10},
        {"shot_id": "shot_001", "source_start": "00:00:00.000", "source_end": "00:00:10.000", "duration_seconds": 10},
    ]
    segments = [
        {"shot_id": "shot_001", "status": "success", "actual_duration_seconds": 3.0},
        {"shot_id": "shot_002", "status": "success", "actual_duration_seconds": 4.0},
    ]

    compacted = runner._compact_clips_to_voiceover_segments(clips, segments)

    assert [clip["shot_id"] for clip in compacted] == ["shot_002", "shot_001"]
    assert compacted[0]["source_end"] == "00:00:24.000"
    assert compacted[0]["target_start_seconds"] == 0.0
    assert compacted[1]["target_start_seconds"] == 4.25
