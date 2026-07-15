from __future__ import annotations

import json
from collections import deque
from types import SimpleNamespace

import run_multisource_pipeline
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


def test_new_multisource_request_with_task_id_does_not_switch_mode() -> None:
    req = RunRequest(
        task_id="new_multisource_task",
        source_items=[
            {"source_type": "local", "path": "videos/a.mp4"},
            {"source_type": "local", "path": "videos/b.mp4"},
        ],
        production_mode="highlight_reassembly",
    )

    _prepare_mode_switched_run(req)

    assert req.task_id == "new_multisource_task"
    assert req.input_video is None


def test_new_multisource_task_id_mode_suffix_is_normalized() -> None:
    task_id = web_app._normalize_new_task_id(
        "多源剪辑_2段_AI配音解说_20260602_115300",
        "多源剪辑（2段）",
        "highlight_reassembly",
        "abcdef1234567890",
    )

    assert "视频重组" in task_id
    assert "AI配音解说" not in task_id


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


def test_mode_switch_to_full_concat_overrides_old_source_request_granularity(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", tmp_path)
    web_app.PROJECT_CONFIG.raw.setdefault("full_concat", {})["chunk_seconds"] = 10
    web_app.PROJECT_CONFIG.raw.setdefault("full_concat", {})["frame_interval"] = 5
    task_dir = tmp_path / "example_AI配音解说_20260527_161530"
    (task_dir / "input").mkdir(parents=True)
    (task_dir / "manifest.json").write_text(
        json.dumps({"last_web_run_options": {"production_mode": "ai_voiceover"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (task_dir / "input" / "source_request.json").write_text(
        json.dumps(
            {
                "source_items": [
                    {"source_type": "remote_ucms", "id": 1, "name": "20260625_215934"},
                    {"source_type": "remote_ucms", "id": 2, "name": "20260625_220434"},
                ],
                "aspect_ratio": "16:9",
                "chunk_seconds": 60,
                "frame_interval": 10,
                "mode": "normal",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    req = RunRequest(task_id=task_dir.name, production_mode="full_concat")

    _prepare_mode_switched_run(req)

    assert req.task_id == "example_完整版_20260527_161530"
    assert req.chunk_seconds == 10
    assert req.frame_interval == 5
    assert len(req.source_items) == 2


def test_multisource_runner_rekeys_stale_full_concat_source_request(monkeypatch) -> None:
    monkeypatch.setattr(
        run_multisource_pipeline,
        "load_config",
        lambda _path: SimpleNamespace(raw={"full_concat": {"chunk_seconds": 10, "frame_interval": 5}}),
    )
    request = {
        "production_mode": "full_concat",
        "source_items": [{"source_type": "remote_ucms", "id": 1}],
        "aspect_ratio": "16:9",
        "chunk_seconds": 60,
        "frame_interval": 10,
        "mode": "normal",
        "common_source_key": "old60",
        "common_task_id": "common_old60",
    }

    changed = run_multisource_pipeline._normalize_full_concat_request(request)

    assert changed is True
    assert request["chunk_seconds"] == 10
    assert request["frame_interval"] == 5
    assert request["common_source_key"] != "old60"
    assert request["common_task_id"] == f"common_{request['common_source_key']}"


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


def test_delete_task_removes_directory_and_job_records(tmp_path, monkeypatch) -> None:
    task_dir = tmp_path / "task_done"
    task_dir.mkdir()
    (task_dir / "manifest.json").write_text("{}", encoding="utf-8")
    deleted_jobs: list[str] = []
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(web_app, "JOBS", {
        "job_001": {
            "job_id": "job_001",
            "task_id": "task_done",
            "status": "failed",
            "log_path": str(tmp_path / "missing.log"),
        }
    })
    monkeypatch.setattr(web_app, "PENDING_JOB_IDS", deque(["job_001"]))
    monkeypatch.setattr(web_app, "JOB_STORE", SimpleNamespace(delete_job=lambda task_id: deleted_jobs.append(task_id)))

    result = web_app.delete_task("task_done")

    assert result == {"ok": True, "deleted": "task_done"}
    assert not task_dir.exists()
    assert web_app.JOBS == {}
    assert list(web_app.PENDING_JOB_IDS) == []
    assert deleted_jobs == ["task_done"]


def test_refresh_job_marks_orphaned_running_job_failed(tmp_path, monkeypatch) -> None:
    task_dir = tmp_path / "task_orphan"
    task_dir.mkdir()
    saved_jobs: list[dict] = []
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(web_app, "JOB_STORE", SimpleNamespace(save_job=lambda _task_id, job: saved_jobs.append(dict(job))))
    monkeypatch.setattr(web_app, "_pid_exists", lambda _pid: False)
    monkeypatch.setattr(web_app, "JOBS", {
        "job_orphan": {
            "job_id": "job_orphan",
            "task_id": "task_orphan",
            "status": "running",
            "pid": 999999,
            "log_path": str(tmp_path / "missing.log"),
        }
    })

    public = web_app._refresh_job("job_orphan")

    assert public["status"] == "failed"
    assert public["returncode"] == -9
    assert "进程已不存在" in public["user_message"]
    assert saved_jobs[-1]["status"] == "failed"


def test_refresh_job_keeps_queued_pending_job_pending(tmp_path, monkeypatch) -> None:
    task_dir = tmp_path / "task_pending"
    task_dir.mkdir()
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(web_app, "PENDING_JOB_IDS", deque(["job_pending"]))
    monkeypatch.setattr(web_app, "_pid_exists", lambda _pid: False)
    monkeypatch.setattr(web_app, "JOBS", {
        "job_pending": {
            "job_id": "job_pending",
            "task_id": "task_pending",
            "status": "pending",
            "pid": None,
            "log_path": str(tmp_path / "missing.log"),
        }
    })

    public = web_app._refresh_job("job_pending")

    assert public["status"] == "pending"
    assert web_app.JOBS["job_pending"]["status"] == "pending"


def test_cancel_job_clears_orphaned_running_job(tmp_path, monkeypatch) -> None:
    task_dir = tmp_path / "task_orphan"
    task_dir.mkdir()
    saved_jobs: list[dict] = []
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(web_app, "JOB_STORE", SimpleNamespace(save_job=lambda _task_id, job: saved_jobs.append(dict(job))))
    monkeypatch.setattr(web_app, "_pid_exists", lambda _pid: False)
    monkeypatch.setattr(web_app, "JOBS", {
        "job_orphan": {
            "job_id": "job_orphan",
            "task_id": "task_orphan",
            "status": "running",
            "pid": 999999,
            "log_path": str(tmp_path / "missing.log"),
        }
    })

    public = web_app.cancel_job("job_orphan")

    assert public["status"] == "cancelled"
    assert public["returncode"] == -9
    assert "进程已不存在" in public["user_message"]
    assert saved_jobs[-1]["status"] == "cancelled"
