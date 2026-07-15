from __future__ import annotations

import pytest

from newsclip_agent.workflow_registry import DEPENDENCIES, step_order


FULL_CONCAT_TAIL = [
    "ad_detection",
    "full_concat_boundary_refine",
    "full_concat_plan",
    "full_concat_plan_qc",
    "full_concat_render",
    "full_concat_output_qc",
]


@pytest.mark.parametrize("unified", [False, True])
def test_full_concat_runs_boundary_refinement_and_quality_gates_in_order(unified: bool) -> None:
    steps = step_order("full_concat", unified=unified)

    assert steps[-len(FULL_CONCAT_TAIL) :] == FULL_CONCAT_TAIL
    assert len(steps) == len(set(steps))


def test_full_concat_quality_step_dependencies_form_render_gate() -> None:
    assert DEPENDENCIES["full_concat_boundary_refine"] == ["ad_detection"]
    assert DEPENDENCIES["full_concat_plan"] == ["full_concat_boundary_refine"]
    assert DEPENDENCIES["full_concat_plan_qc"] == [
        "full_concat_plan",
        "full_concat_boundary_refine",
    ]
    assert DEPENDENCIES["full_concat_render"] == [
        "full_concat_plan",
        "full_concat_plan_qc",
    ]
    assert DEPENDENCIES["full_concat_output_qc"] == [
        "full_concat_render",
        "full_concat_plan",
    ]


@pytest.mark.parametrize("production_mode", ["ai_voiceover", "highlight_reassembly"])
@pytest.mark.parametrize("unified", [False, True])
def test_full_concat_quality_steps_do_not_leak_into_other_workflows(
    production_mode: str,
    unified: bool,
) -> None:
    steps = step_order(production_mode, unified=unified)

    assert not set(FULL_CONCAT_TAIL).intersection(steps)
