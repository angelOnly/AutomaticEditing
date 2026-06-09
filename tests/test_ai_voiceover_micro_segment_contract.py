from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from newsclip_agent.pipeline import PipelineRunner, UserFacingPipelineError, read_json, write_json


def _runner(tmp_path: Path | None = None) -> PipelineRunner:
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.options = SimpleNamespace(
        production_mode="ai_voiceover",
        target_duration_seconds=None,
        max_output_video_seconds=None,
    )
    runner.config = SimpleNamespace(raw={})
    runner.manifest = {"steps": {}}
    if tmp_path is not None:
        runner.task_dir = tmp_path
    return runner


def test_asr_micro_segment_does_not_fallback_missing_sentences_by_default() -> None:
    runner = _runner()
    sentences = [
        {"sentence_id": "s0001", "start_seconds": 0.0},
        {"sentence_id": "s0002", "start_seconds": 2.0},
    ]

    groups, diagnostics = runner._dedupe_and_validate_sentence_groups(
        [{"sentence_ids": ["s0001"]}],
        sentences,
        allow_sentence_fallback=False,
    )

    assert [group["sentence_ids"] for group in groups] == [["s0001"]]
    assert diagnostics["missing_sentence_ids"] == ["s0002"]
    assert diagnostics["fallback_groups_added"] == 0


def test_content_analysis_legacy_segment_id_is_diagnostic_only() -> None:
    runner = _runner()
    runner._load_step_json = lambda step: {
        "segments": [{"micro_segment_id": "ms_001", "summary": "fact"}]
    }

    output = runner._normalize_content_analysis_selected_segments(
        {"selected_segments": [{"segment_id": "ms_001", "summary": "picked"}]}
    )

    assert output["selected_segments"] == [
        {"micro_segment_id": "ms_001", "summary": "picked", "role": "fact"}
    ]
    assert output["diagnostics"]["legacy_segment_id_used"] == 1


def test_content_analysis_groups_sources_in_stable_order() -> None:
    runner = _runner()
    runner.config.raw = {
        "content_analysis": {
            "group_by_source": True,
            "group_when_source_count_gt": 3,
            "max_sources_per_group": 3,
            "max_segments_per_group": 40,
        },
        "asr_micro_segment": {"max_segments_for_content_analysis": 120},
    }
    segments = [
        {"micro_segment_id": f"source_{i:03d}_ms_0001", "source_id": f"source_{i:03d}"}
        for i in range(1, 9)
    ]

    groups = runner._group_segments_for_content_analysis(segments, runner._content_analysis_cfg())

    assert [group["source_ids"] for group in groups] == [
        ["source_001", "source_002", "source_003"],
        ["source_004", "source_005", "source_006"],
        ["source_007", "source_008"],
    ]
    assert [group["group_id"] for group in groups] == ["group_001", "group_002", "group_003"]


def test_content_analysis_prompt_respects_char_budget() -> None:
    runner = _runner()
    runner.config.raw = {
        "content_analysis": {
            "max_input_chars_per_group": 1000,
            "max_selected_segments_per_group": 6,
        },
        "asr_micro_segment": {"max_segments_for_content_analysis": 120},
    }
    segments = [
        {
            "micro_segment_id": f"ms_{i:03d}",
            "source_id": "source_001",
            "duration_seconds": 12,
            "summary": "summary " * 20,
            "asr_text": "speech " * 80,
            "visual_summary": "visual " * 40,
        }
        for i in range(20)
    ]

    text = runner._build_content_analysis_micro_segment_text(
        segments,
        group_id="group_001",
        group_index=1,
        group_count=1,
        source_ids=["source_001"],
        max_input_chars=1000,
    )

    assert len(text) <= 1000
    assert "本次实际提供 micro_segment 数" in text
    assert "不能选择未出现在上文的 micro_segment_id" in text


def test_merge_grouped_content_analysis_results_limits_and_dedupes() -> None:
    runner = _runner()
    cfg = {
        "max_selected_segments_per_group": 2,
        "max_final_selected_segments": 3,
    }

    output = runner._merge_grouped_content_analysis_results(
        group_results=[
            {
                "group_id": "group_002",
                "group_index": 2,
                "selected_segments": [
                    {"micro_segment_id": "ms_003", "summary": "third", "role": "fact"},
                    {"micro_segment_id": "ms_004", "summary": "fourth", "role": "fact"},
                ],
            },
            {
                "group_id": "group_001",
                "group_index": 1,
                "selected_segments": [
                    {"micro_segment_id": "ms_001", "summary": "first", "role": "fact"},
                    {"micro_segment_id": "ms_002", "summary": "second", "role": "fact"},
                    {"micro_segment_id": "ms_003", "summary": "duplicate", "role": "fact"},
                ],
            },
        ],
        group_errors=[],
        cfg=cfg,
    )

    assert [item["micro_segment_id"] for item in output["selected_segments"]] == ["ms_001", "ms_002", "ms_003"]
    assert output["selected_segments"][0]["source_group_id"] == "group_001"
    assert output["diagnostics"]["selected_count"] == 3


def test_short_video_edit_plan_reports_invalid_clip_ids() -> None:
    runner = _runner()
    runner._candidate_clips_by_id = lambda: {"clip_001": {"clip_id": "clip_001"}}

    diagnostics = runner._short_video_edit_plan_semantic_diagnostics(
        [{"clip_ids": ["clip_001", "fake_clip"]}]
    )

    assert diagnostics["ok"] is False
    assert diagnostics["reason"] == "invalid_clip_ids"
    assert diagnostics["invalid_clip_ids"] == ["fake_clip"]


def test_candidate_materialize_records_short_clip_without_extending(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    runner.config.raw = {
        "ai_voiceover_candidate": {
            "padding_before_seconds": 0,
            "padding_after_seconds": 0,
            "min_clip_seconds": 3.0,
            "max_clip_seconds": 30.0,
            "fail_on_empty": True,
            "truncate_overlong": False,
        }
    }
    runner._load_step_json = lambda step: {
        "content_analysis": {
            "selected_segments": [{"micro_segment_id": "ms_001", "summary": "fact"}]
        },
        "asr_micro_segment": {
            "segments": [
                {
                    "micro_segment_id": "ms_001",
                    "source_id": "source_001",
                    "source_start_seconds": 1.0,
                    "source_end_seconds": 2.0,
                    "asr_text": "fact",
                }
            ]
        },
    }[step]
    runner._step_content_hash = lambda step: f"{step}_hash"
    runner._can_reuse = lambda step, input_hash: False
    runner._version_dir = lambda step, base: ("v1", tmp_path / base / "v1")
    runner._source_boundaries = lambda: [{"source_id": "source_001", "duration_seconds": 10.0}]
    runner._base_status = lambda step, version, input_hash, files: {
        "step": step,
        "version": version,
        "input_hash": input_hash,
        "output_files": [str(path) for path in files],
    }
    runner._write_status = lambda vdir, status: None
    runner._record_step = lambda **kwargs: None

    runner.step_ai_voiceover_candidate_materialize()

    output = read_json(tmp_path / "ai_voiceover_candidates" / "v1" / "candidate_clips.json")
    debug = read_json(tmp_path / "ai_voiceover_candidates" / "v1" / "materialize_debug.json")
    clip = output["candidate_clips"][0]
    assert clip["source_start_seconds"] == 1.0
    assert clip["source_end_seconds"] == 2.0
    assert clip["duration_seconds"] == 1.0
    assert debug["diagnostics"]["short_clips"][0]["micro_segment_id"] == "ms_001"


def test_asr_sentence_units_loads_standalone_raw_index(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    raw_index = {
        "sources": [
            {
                "source_id": "source_001",
                "source_index": 1,
                "asr_segments": [
                    {
                        "asr_segment_id": "a1",
                        "start_seconds": 1.0,
                        "end_seconds": 3.0,
                        "normalized_text": "standalone raw asr",
                    }
                ],
            }
        ]
    }
    raw_path = tmp_path / "source_aggregate" / "v1" / "source_raw_asr_index.json"
    write_json(raw_path, raw_index)
    runner.manifest = {
        "source_mode": "multi_source_pool",
        "source_videos": [{"source_id": "source_001"}],
        "source_manifest": "input/source_manifest.json",
        "steps": {"source_aggregate": {"output": "source_aggregate/v1/source_aggregate.json"}},
    }
    runner.config.raw = {"asr_micro_segment": {"max_sentence_chars": 120}}
    runner._load_optional_step_json = lambda step, default=None: {
        "raw_asr_index_file": "source_aggregate/v1/source_raw_asr_index.json",
        "raw_asr_index": {"sources": []},
    }

    sentences = runner._build_asr_sentence_units()

    assert len(sentences) == 1
    assert sentences[0]["text"] == "standalone raw asr"


def test_asr_sentence_units_missing_raw_index_fails_fast(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    runner.manifest = {
        "source_mode": "multi_source_pool",
        "source_videos": [{"source_id": "source_001"}],
        "source_manifest": "input/source_manifest.json",
        "steps": {"source_aggregate": {"output": "source_aggregate/v1/source_aggregate.json"}},
    }
    runner.config.raw = {"asr_micro_segment": {"max_sentence_chars": 120}}
    runner._load_optional_step_json = lambda step, default=None: {"raw_asr_index_file": "source_aggregate/v1/missing.json"}

    with pytest.raises(UserFacingPipelineError, match="source_raw_asr_index_missing"):
        runner._build_asr_sentence_units()


def test_tts_missing_segment_keys_are_detected(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    runner._load_step_json = lambda step: {
        "scripts": [
            {
                "short_video_id": "v_001",
                "narration_segments": [{"shot_id": "v_001_s01", "text": "hello"}],
            }
        ]
    }

    expected = runner._expected_tts_segment_keys()

    assert expected == {("v_001", "v_001_s01")}
    actual: set[tuple[str, str]] = set()
    assert sorted(expected - actual) == [("v_001", "v_001_s01")]


def test_cut_plan_empty_clips_fails_fast(tmp_path: Path) -> None:
    runner = _runner(tmp_path)

    with pytest.raises(UserFacingPipelineError, match="cut_plan_clips_invalid"):
        runner._validate_cut_plan_not_empty_or_raise({
            "output_videos": [{"short_video_id": "v_001", "clips": []}]
        })
