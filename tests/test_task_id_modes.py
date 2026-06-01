from __future__ import annotations

import json

import web_app
from web_app import (
    RunRequest,
    _prepare_mode_switched_run,
    _resolve_run_task_id,
    _seed_reusable_outputs,
    _with_mode_suffix,
)


def test_new_video_runs_get_mode_specific_task_ids() -> None:
    voiceover = _resolve_run_task_id(
        RunRequest(
            input_video="videos/example.mp4",
            task_id="example_20260527_1615",
            production_mode="ai_voiceover",
        )
    )
    reassembly = _resolve_run_task_id(
        RunRequest(
            input_video="videos/example.mp4",
            task_id="example_20260527_1615",
            production_mode="highlight_reassembly",
        )
    )

    assert voiceover.startswith("example_AI配音解说_20260527_1615")
    assert len(voiceover.rsplit("_", 1)[1]) == 6
    assert reassembly.startswith("example_视频重组_20260527_1615")
    assert len(reassembly.rsplit("_", 1)[1]) == 6


def test_existing_mode_specific_task_id_is_not_rewritten() -> None:
    task_id = "example_ai_voiceover_20260527_161530"

    assert _with_mode_suffix(task_id, "ai_voiceover") == task_id


def test_existing_task_reruns_keep_selected_task_id() -> None:
    req = RunRequest(task_id="example_20260527_1615", production_mode="highlight_reassembly")

    assert _resolve_run_task_id(req) == "example_20260527_1615"


def test_mode_specific_task_id_is_replaced_when_mode_changes() -> None:
    task_id = "example_AI配音解说_20260527_161530"

    assert _with_mode_suffix(task_id, "highlight_reassembly") == "example_视频重组_20260527_161530"


def test_existing_task_full_run_forks_when_production_mode_changes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", tmp_path)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake video")
    task_dir = tmp_path / "example_AI配音解说_20260527_161530"
    task_dir.mkdir()
    (task_dir / "task.json").write_text(
        json.dumps({"source_video_original": str(source)}, ensure_ascii=False),
        encoding="utf-8",
    )
    (task_dir / "manifest.json").write_text(
        json.dumps({"last_web_run_options": {"production_mode": "ai_voiceover"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    req = RunRequest(task_id=task_dir.name, production_mode="highlight_reassembly")

    _prepare_mode_switched_run(req)

    assert req.input_video == str(source.resolve())
    assert req.task_id == "example_视频重组_20260527_161530"


def test_mode_switched_task_seeds_reusable_outputs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", tmp_path)
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"fake video")
    source_task = tmp_path / "example_AI配音解说_20260527_161530"
    source_task.mkdir()
    output_file = source_task / "vision" / "v1" / "visual_analysis.json"
    output_file.parent.mkdir(parents=True)
    output_file.write_text('{"ok": true}', encoding="utf-8")
    (output_file.parent / "step_status.json").write_text("{}", encoding="utf-8")
    (source_task / "manifest.json").write_text(
        json.dumps(
            {
                "source_video": str(source_video),
                "steps": {
                    "vision": {
                        "step_name": "vision",
                        "version": "v1",
                        "status": "success",
                        "output": "vision/v1/visual_analysis.json",
                    },
                    "voiceover_script": {
                        "step_name": "voiceover_script",
                        "version": "v1",
                        "status": "success",
                        "output": "agents/voiceover_script/v1/voiceover_script.json",
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    target_task = tmp_path / "example_视频重组_20260527_161530"
    target_task.mkdir()
    req = RunRequest(
        task_id=target_task.name,
        input_video=str(source_video),
        production_mode="highlight_reassembly",
        reuse_from_task_id=source_task.name,
    )

    _seed_reusable_outputs(req, target_task.name, target_task)

    target_manifest = json.loads((target_task / "manifest.json").read_text(encoding="utf-8"))
    assert target_manifest["reused_from_task_id"] == source_task.name
    assert list(target_manifest["steps"]) == ["vision"]
    assert (target_task / "vision" / "v1" / "visual_analysis.json").exists()
    assert not (target_task / "agents" / "voiceover_script").exists()


def test_mode_switch_prefers_task_id_when_manifest_was_already_overwritten(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", tmp_path)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake video")
    task_dir = tmp_path / "example_AI配音解说_20260527_161530"
    task_dir.mkdir()
    (task_dir / "task.json").write_text(
        json.dumps({"source_video_original": str(source)}, ensure_ascii=False),
        encoding="utf-8",
    )
    (task_dir / "manifest.json").write_text(
        json.dumps({"last_web_run_options": {"production_mode": "highlight_reassembly"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    req = RunRequest(task_id=task_dir.name, production_mode="highlight_reassembly")

    _prepare_mode_switched_run(req)

    assert req.input_video == str(source.resolve())
    assert req.task_id == "example_视频重组_20260527_161530"
