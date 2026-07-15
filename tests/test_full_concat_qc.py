from pathlib import Path

from newsclip_agent.full_concat_qc import plan_qc, refine_detection_boundaries


def test_plan_qc_blocks_explicit_drop_reintroduced_by_merge() -> None:
    detection = {
        "target_column": "凤凰聚焦",
        "segments": [
            {"source_id": "s", "start_seconds": 0, "end_seconds": 10, "keep": True, "label": "target"},
            {"source_id": "s", "start_seconds": 10, "end_seconds": 11, "keep": False, "label": "sponsor"},
            {"source_id": "s", "start_seconds": 11, "end_seconds": 20, "keep": True, "label": "target"},
        ],
    }
    plan = {"output_videos": [{"clips": [{"clip_id": "c", "source_id": "s", "local_start_seconds": 0, "local_end_seconds": 20}]}]}
    report = plan_qc(plan, detection)
    assert report["status"] == "fail"
    assert report["issues"][0]["code"] == "explicit_drop_reintroduced_by_merge"


def test_boundary_report_is_auditable() -> None:
    detection = {
        "target_column": "凤凰聚焦",
        "segments": [
            {"source_id": "s", "start_seconds": 0, "end_seconds": 10, "keep": False, "label": "promo"},
            {"source_id": "s", "start_seconds": 10, "end_seconds": 20, "keep": True, "label": "target"},
        ],
    }
    report = refine_detection_boundaries(detection)
    assert report["boundaries"][0]["kind"] == "drop_to_keep"
    assert report["boundaries"][0]["coarse_time_seconds"] == 10
