from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from newsclip_agent.pipeline import PipelineRunner, read_json


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
