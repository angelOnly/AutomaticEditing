from __future__ import annotations

from pathlib import Path

from newsclip_agent.pipeline import PipelineRunner, RunOptions
from newsclip_agent.workflow_registry import common_reusable_steps
from run_multisource_pipeline import (
    _common_step_ready,
    _map_common_rerun_step,
    COMMON_REUSABLE_STEPS,
)


def test_unified_common_reusable_steps_include_source_prepare() -> None:
    assert common_reusable_steps(unified=True) == [
        "source_prepare",
        "source_analysis",
        "source_aggregate",
    ]
    assert COMMON_REUSABLE_STEPS == common_reusable_steps(unified=True)


def test_child_pipeline_skips_all_reused_common_steps() -> None:
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.options = RunOptions(production_mode="ai_voiceover")
    runner.manifest = {
        "source_mode": "multi_source_pool",
        "source_videos": [{"source_id": "source_001"}],
        "reused_common_steps": [
            "source_prepare",
            "source_analysis",
            "source_aggregate",
        ],
    }

    selected = runner._resolve_selected_steps()

    assert selected[0] == "content_analysis"
    assert "source_prepare" not in selected
    assert "source_analysis" not in selected
    assert "source_aggregate" not in selected


def test_source_prepare_rerun_maps_to_executable_common_step() -> None:
    assert _map_common_rerun_step("source_prepare") == "source_analysis"


def test_source_prepare_ready_falls_back_to_source_manifest(tmp_path: Path) -> None:
    source_manifest = tmp_path / "input" / "source_manifest.json"
    source_manifest.parent.mkdir(parents=True)
    source_manifest.write_text("{}", encoding="utf-8")

    assert _common_step_ready(tmp_path, "source_prepare") is True
