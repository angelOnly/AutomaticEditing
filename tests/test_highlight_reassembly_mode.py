from __future__ import annotations

from pathlib import Path

from newsclip_agent.pipeline import PipelineRunner, RunOptions, parse_args
from newsclip_agent.utils import read_json


def test_highlight_reassembly_parse_forces_original_audio() -> None:
    options = parse_args([
        "--input",
        "videos/example.mp4",
        "--production-mode",
        "highlight_reassembly",
        "--require-tts",
        "--audio-policy",
        "ai_voiceover",
    ])

    assert options.production_mode == "highlight_reassembly"
    assert options.audio_policy == "original"
    assert options.skip_tts is True
    assert options.require_tts is False
    assert options.allow_original_audio_evidence is True


def test_reassembly_step_order_is_separate_from_ai_voiceover() -> None:
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.options = RunOptions(production_mode="highlight_reassembly")

    steps = runner._active_step_order()

    assert "highlight_reassembly_plan" in steps
    assert "reassembly_cut_plan" in steps
    assert "reassembly_render" in steps
    assert "voiceover_script" not in steps
    assert "tts" not in steps
    assert "cut_plan" not in steps


def test_reassembly_cut_plan_builds_original_audio_clips(tmp_path: Path) -> None:
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.options = RunOptions(
        production_mode="highlight_reassembly",
        reassembly_target_seconds=90,
        reassembly_min_clip_seconds=5.0,
        reassembly_max_clip_seconds=45.0,
        reassembly_max_clip_count=8,
    )
    runner.task_id = "task_test"
    runner.task_dir = tmp_path
    runner.manifest_path = tmp_path / "manifest.json"
    runner.manifest = {
        "task_id": "task_test",
        "source_video": "input/source.mp4",
        "current_versions": {"highlight_reassembly_plan": "v1"},
        "steps": {},
    }

    plan = {
        "output_videos": [
            {
                "reassembly_id": "hr_001",
                "title": "test",
                "selected_clips": [
                    {
                        "source_clip_id": "clip_001",
                        "adjusted_start": "00:00:10.000",
                        "adjusted_end": "00:00:20.000",
                        "role": "opening",
                    },
                    {
                        "source_clip_id": "clip_bad",
                        "adjusted_start": "00:00:30.000",
                        "adjusted_end": "00:00:31.000",
                    },
                ],
            }
        ]
    }
    runner._load_step_json = lambda step: plan

    runner.step_reassembly_cut_plan()

    output = read_json(tmp_path / runner.manifest["steps"]["reassembly_cut_plan"]["output"])
    video = output["output_videos"][0]
    assert video["duration_status"] == "ok"
    assert video["audio_policy"] == "original_audio"
    assert len(video["clips"]) == 1
    assert video["clips"][0]["keep_original_audio"] is True
    assert video["clips"][0]["original_audio_volume"] == 1.0
    assert video["clips"][0]["target_start"] == "00:00:00.000"
    assert video["warnings"]
