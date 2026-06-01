from __future__ import annotations

from newsclip_agent.duration_policy import (
    DurationSettings,
    duration_mismatch_block_reason,
    normalize_audio_mode_for_policy,
    original_audio_volume_for_clip,
    should_build_evidence_audio_windows,
    validate_audio_policy,
)


def test_ai_voiceover_defaults_original_audio_to_zero() -> None:
    volume = original_audio_volume_for_clip(
        {"audio_mode": "ai_voiceover"},
        audio_policy="ai_voiceover",
        settings=DurationSettings(),
    )

    assert volume == 0.0


def test_mixed_evidence_requires_reason() -> None:
    video = {
        "audio_policy": "ai_voiceover_main",
        "allow_original_audio_evidence": True,
        "voiceover": {"enabled": True},
        "clips": [{"audio_mode": "mixed_evidence", "original_audio_volume": 1.0}],
    }

    result = validate_audio_policy(video, require_tts=True)

    assert result["status"] == "blocked"
    assert any("mixed_evidence" in issue for issue in result["issues"])


def test_require_tts_blocks_missing_voiceover() -> None:
    video = {
        "audio_policy": "ai_voiceover_main",
        "voiceover": {"enabled": False},
        "clips": [],
    }

    result = validate_audio_policy(video, require_tts=True)

    assert result["status"] == "blocked"


def test_original_audio_policy_allows_original_volume() -> None:
    volume = original_audio_volume_for_clip(
        {"audio_mode": "ai_voiceover"},
        audio_policy="original",
        settings=DurationSettings(),
    )

    assert volume == 1.0


def test_ai_voiceover_blocks_long_original_sound() -> None:
    video = {
        "audio_policy": "ai_voiceover_main",
        "allow_original_audio_evidence": True,
        "video_duration_seconds": 30,
        "voiceover_duration_seconds": 28,
        "voiceover": {"enabled": True},
        "clips": [
            {
                "audio_mode": "original_sound",
                "duration_seconds": 20,
                "original_audio_volume": 1.0,
                "original_audio_reason": "key sync sound",
            }
        ],
    }

    result = validate_audio_policy(video, require_tts=True)

    assert result["status"] == "blocked"
    assert any("exceeds" in issue for issue in result["issues"])


def test_ai_voiceover_blocks_high_original_sound_ratio() -> None:
    video = {
        "audio_policy": "ai_voiceover_main",
        "allow_original_audio_evidence": True,
        "video_duration_seconds": 30,
        "voiceover_duration_seconds": 28,
        "voiceover": {"enabled": True},
        "clips": [
            {
                "audio_mode": "mixed_evidence",
                "duration_seconds": 6,
                "original_audio_volume": 1.0,
                "original_audio_reason": "key evidence sound",
                "original_audio_transcript_summary": "完整证据表达",
            },
            {
                "audio_mode": "mixed_evidence",
                "duration_seconds": 6,
                "original_audio_volume": 1.0,
                "original_audio_reason": "key evidence sound",
                "original_audio_transcript_summary": "完整证据表达",
            },
        ],
    }

    result = validate_audio_policy(video, require_tts=True)

    assert result["status"] == "blocked"
    assert any("原声总时长" in issue or "exceeds" in issue for issue in result["issues"])


def test_ai_voiceover_blocks_low_voiceover_ratio() -> None:
    video = {
        "audio_policy": "ai_voiceover_main",
        "video_duration_seconds": 30,
        "voiceover_duration_seconds": 8,
        "voiceover": {"enabled": True},
        "clips": [],
    }

    result = validate_audio_policy(video, require_tts=True)

    assert result["status"] == "blocked"
    assert any("coverage ratio" in issue or "覆盖比例" in issue for issue in result["issues"])


def test_cut_plan_blocks_voiceover_video_duration_mismatch() -> None:
    reason = duration_mismatch_block_reason(
        video_duration=30,
        voiceover_duration=8,
        audio_policy="ai_voiceover",
        tts_success=True,
    )

    assert "超过" in reason


def test_low_value_mixed_evidence_original_audio_is_suppressed() -> None:
    volume = original_audio_volume_for_clip(
        {
            "audio_mode": "mixed_evidence",
            "original_audio_policy": "evidence_window",
            "original_audio_value_score": 3,
        },
        audio_policy="ai_voiceover",
        settings=DurationSettings(),
    )

    assert volume == 0.0


def test_evidence_quote_requires_semantic_duration_and_transcript() -> None:
    video = {
        "audio_policy": "ai_voiceover_main",
        "allow_original_audio_evidence": True,
        "video_duration_seconds": 45,
        "voiceover_duration_seconds": 40,
        "voiceover": {"enabled": True},
        "clips": [
            {
                "audio_mode": "mixed_evidence",
                "duration_seconds": 3,
                "original_audio_volume": 1.0,
                "original_audio_reason": "关键人物原声",
                "original_audio_type": "evidence_quote",
                "original_audio_value_score": 8,
            },
        ],
    }

    result = validate_audio_policy(video, require_tts=True)

    assert result["status"] == "blocked"
    assert any("semantic minimum" in issue for issue in result["issues"])
    assert any("transcript summary" in issue for issue in result["issues"])


def test_core_quote_allows_longer_complete_original_audio() -> None:
    video = {
        "audio_policy": "ai_voiceover_main",
        "allow_original_audio_evidence": True,
        "video_duration_seconds": 60,
        "voiceover_duration_seconds": 50,
        "voiceover": {"enabled": True},
        "clips": [
            {
                "audio_mode": "mixed_evidence",
                "duration_seconds": 10,
                "original_audio_volume": 1.0,
                "original_audio_reason": "完整关键表态",
                "original_audio_policy": "core_quote",
                "original_audio_type": "authority_quote",
                "original_audio_value_score": 9,
                "original_audio_complete_unit": True,
                "original_audio_transcript_summary": "发言人完整说明核心立场",
            },
        ],
    }

    result = validate_audio_policy(video, require_tts=True)

    assert result["status"] == "ok"


def test_ai_voiceover_disables_model_requested_original_audio() -> None:
    volume = original_audio_volume_for_clip(
        {
            "audio_mode": "mixed_evidence",
            "original_audio_policy": "core_quote",
            "original_audio_value_score": 9,
        },
        audio_policy="ai_voiceover",
        settings=DurationSettings(
            ai_voiceover_disable_original_audio=True,
            allow_original_audio_evidence=False,
        ),
    )

    assert volume == 0.0


def test_ai_voiceover_normalizes_model_requested_original_audio() -> None:
    clip = normalize_audio_mode_for_policy(
        {
            "audio_mode": "mixed_evidence",
            "original_audio_policy": "core_quote",
        },
        audio_policy="ai_voiceover",
        settings=DurationSettings(ai_voiceover_disable_original_audio=True),
    )

    assert clip["audio_mode"] == "ai_voiceover"
    assert clip["keep_original_audio"] is False
    assert clip["original_audio_volume"] == 0.0
    assert clip["original_audio_policy"] == "omit"
    assert clip["audio_mode_original"] == "mixed_evidence"


def test_ai_voiceover_does_not_build_evidence_windows_by_default() -> None:
    assert not should_build_evidence_audio_windows(
        audio_policy="ai_voiceover",
        settings=DurationSettings(ai_voiceover_disable_original_audio=True),
        allow_original_audio_evidence=False,
    )


def test_mixed_audio_builds_evidence_windows() -> None:
    assert should_build_evidence_audio_windows(
        audio_policy="mixed",
        settings=DurationSettings(),
        allow_original_audio_evidence=True,
    )


def test_short_original_audio_does_not_block_ai_voiceover_when_original_disabled() -> None:
    video = {
        "audio_policy": "ai_voiceover_main",
        "video_duration_seconds": 45,
        "voiceover_duration_seconds": 44,
        "voiceover": {"enabled": True},
        "clips": [
            {
                "audio_mode": "mixed_evidence",
                "duration_seconds": 5,
                "original_audio_volume": 0.0,
                "keep_original_audio": False,
                "original_audio_type": "authority_quote",
                "original_audio_policy": "core_quote",
                "original_audio_value_score": 9,
            }
        ],
    }

    result = validate_audio_policy(
        video,
        require_tts=True,
        settings=DurationSettings(ai_voiceover_disable_original_audio=True),
    )

    assert result["status"] == "ok"


def test_ai_voiceover_overlap_check_skipped_when_original_disabled() -> None:
    video = {
        "audio_policy": "ai_voiceover_main",
        "video_duration_seconds": 45,
        "voiceover_duration_seconds": 44,
        "voiceover": {"enabled": True},
        "evidence_audio_windows": [],
        "voiceover_original_overlap_seconds": 2.0,
        "clips": [],
    }

    result = validate_audio_policy(
        video,
        require_tts=True,
        settings=DurationSettings(ai_voiceover_disable_original_audio=True),
    )

    assert result["status"] == "ok"
