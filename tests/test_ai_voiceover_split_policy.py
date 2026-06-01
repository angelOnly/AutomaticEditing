from __future__ import annotations

import pytest

from newsclip_agent.pipeline import PipelineRunner


def test_same_news_chain_scripts_are_merged() -> None:
    runner = PipelineRunner.__new__(PipelineRunner)
    edit_plan = {
        "recommended_video_count": 2,
        "scripts": [
            {
                "short_video_id": "v_001",
                "topic": "美伊停火谈判核心分歧",
                "news_angle": "核问题是谈判障碍",
                "target_duration_seconds": 45,
                "max_allowed_seconds": 50,
                "source_clip_ids": ["clip_003"],
                "must_keep_fact_points": ["伊朗核问题是核心分歧"],
                "editing_structure": [],
            },
            {
                "short_video_id": "v_002",
                "topic": "美伊边谈边打",
                "news_angle": "特朗普推动停火协议",
                "target_duration_seconds": 50,
                "max_allowed_seconds": 55,
                "source_clip_ids": ["clip_001", "clip_004"],
                "must_keep_fact_points": ["美伊边谈边打", "特朗普政府存在政治考量"],
                "editing_structure": [],
            },
        ],
    }

    result = runner._normalize_short_video_split_decision(edit_plan)

    assert result["recommended_video_count"] == 1
    assert len(result["scripts"]) == 1
    assert set(result["scripts"][0]["source_clip_ids"]) == {"clip_001", "clip_003", "clip_004"}
    assert result["auto_merge_applied"] is True


def test_voiceover_script_too_short_is_blocked() -> None:
    runner = PipelineRunner.__new__(PipelineRunner)
    voiceover_output = {
        "scripts": [
            {
                "short_video_id": "v_001",
                "target_duration_seconds": 45,
                "duration_fit": "ok",
                "narration_text": "美伊谈判仍在继续，但核问题仍是核心分歧。",
                "narrative_sections": {
                    "opening": "美伊谈判仍在继续。",
                    "background": "",
                    "core_fact": "核问题是核心分歧。",
                    "analysis_or_conflict": "",
                    "ending": "",
                },
            }
        ]
    }

    with pytest.raises(RuntimeError, match="too short"):
        runner._validate_voiceover_script_duration_or_raise(voiceover_output)


def test_different_news_events_are_not_merged() -> None:
    runner = PipelineRunner.__new__(PipelineRunner)
    edit_plan = {
        "recommended_video_count": 2,
        "scripts": [
            {
                "short_video_id": "v_001",
                "topic": "美伊停火谈判",
                "news_angle": "核问题是核心分歧",
                "target_duration_seconds": 45,
                "source_clip_ids": ["clip_001"],
                "must_keep_fact_points": ["美伊谈判仍在继续"],
                "editing_structure": [],
            },
            {
                "short_video_id": "v_002",
                "topic": "欧洲央行降息",
                "news_angle": "市场关注后续货币政策",
                "target_duration_seconds": 45,
                "source_clip_ids": ["clip_002"],
                "must_keep_fact_points": ["欧洲央行宣布降息"],
                "editing_structure": [],
            },
        ],
    }

    result = runner._normalize_short_video_split_decision(edit_plan)

    assert result["recommended_video_count"] == 2
    assert len(result["scripts"]) == 2
