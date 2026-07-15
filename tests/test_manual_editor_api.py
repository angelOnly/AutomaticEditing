from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import web_app
from newsclip_agent.job_store import JobStore
from newsclip_agent.utils import read_json, write_json


pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg is unavailable",
)


def _make_video(path: Path, *, color: str = "navy", seconds: float = 2.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=160x90:r=10:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=channel_layout=stereo:sample_rate=48000:d={seconds}",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


def _make_full_concat_task(outputs_dir: Path) -> tuple[str, Path]:
    task_id = "sample_full_concat"
    task_dir = outputs_dir / task_id
    source = task_dir / "input" / "source.mp4"
    _make_video(source)
    plan_path = task_dir / "edit" / "full_concat_cut_plan" / "v0001" / "full_concat_cut_plan.json"
    write_json(
        plan_path,
        {
            "project_id": task_id,
            "production_mode": "full_concat",
            "source_video": "input/source.mp4",
            "target_column": "测试新闻",
            "output_videos": [
                {
                    "reassembly_id": "fc_001",
                    "aspect_ratio": "16:9",
                    "target_resolution": "320x180",
                    "clips": [
                        {
                            "clip_id": "clip_001",
                            "source_id": "source_1",
                            "local_start_seconds": 0,
                            "local_end_seconds": 1.5,
                        }
                    ],
                }
            ],
        },
    )
    write_json(
        task_dir / "manifest.json",
        {
            "task_id": task_id,
            "production_mode": "full_concat",
            "source_video": "input/source.mp4",
            "steps": {
                "full_concat_plan": {
                    "status": "success",
                    "version": "v0001",
                    "output": "edit/full_concat_cut_plan/v0001/full_concat_cut_plan.json",
                }
            },
            "current_versions": {"full_concat_plan": "v0001"},
        },
    )
    return task_id, task_dir


def test_manual_editor_api_initializes_saves_uploads_and_queues_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs_dir = tmp_path / "outputs"
    task_id, task_dir = _make_full_concat_task(outputs_dir)
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", outputs_dir)
    monkeypatch.setattr(web_app, "JOB_STORE", JobStore(outputs_dir / ".jobs"))
    monkeypatch.setattr(web_app, "_schedule_jobs", lambda: None)
    web_app.JOBS.clear()
    web_app.PENDING_JOB_IDS.clear()

    with TestClient(web_app.app) as client:
        response = client.get(f"/api/editor/tasks/{task_id}")
        assert response.status_code == 200, response.text
        bootstrap = response.json()
        project = bootstrap["project"]
        assert bootstrap["title"] == "测试新闻"
        assert project["revision"] == 0
        assert project["clips"][0]["source_out"] == 1.5
        assert "path" not in project["assets"][0]
        assert "path" not in project["source_plan"]
        assert (task_dir / "edit" / "manual_editor" / "revisions" / "v0000.json").is_file()

        preview = client.get(project["assets"][0]["preview_url"])
        assert preview.status_code == 200
        assert preview.headers["content-type"].startswith("video/")

        project["clips"][0]["source_out"] = 1.0
        saved_response = client.put(f"/api/editor/tasks/{task_id}/project", json=project)
        assert saved_response.status_code == 200, saved_response.text
        saved = saved_response.json()["project"]
        assert saved["revision"] == 1
        assert saved["duration_seconds"] == 1.0

        stale_response = client.put(f"/api/editor/tasks/{task_id}/project", json=project)
        assert stale_response.status_code == 409

        source = task_dir / "input" / "source.mp4"
        with source.open("rb") as upload:
            uploaded_response = client.post(
                f"/api/editor/tasks/{task_id}/assets",
                files={"file": ("extra.mp4", upload, "video/mp4")},
            )
        assert uploaded_response.status_code == 200, uploaded_response.text
        uploaded = uploaded_response.json()
        assert uploaded["project"]["revision"] == 2
        assert len(uploaded["project"]["assets"]) == 2
        assert "path" not in uploaded["asset"]

        export_response = client.post(f"/api/editor/tasks/{task_id}/export", json={})
        assert export_response.status_code == 200, export_response.text
        export_job = export_response.json()
        assert export_job["job_type"] == "manual_editor_export"
        assert export_job["project_revision"] == 2
        assert Path(export_job["cmd"][4]).name == "v0002.json"
        assert export_job["job_id"] in web_app.PENDING_JOB_IDS

    web_app.JOBS.clear()
    web_app.PENDING_JOB_IDS.clear()


def test_latest_draft_prefers_newer_manual_editor_export(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    automatic = task_dir / "edit" / "full_concat_drafts" / "v0001" / "fc_001" / "automatic.mp4"
    manual = task_dir / "edit" / "manual_editor" / "exports" / "manual.mp4"
    automatic.parent.mkdir(parents=True, exist_ok=True)
    manual.parent.mkdir(parents=True, exist_ok=True)
    automatic.write_bytes(b"automatic")
    manual.write_bytes(b"manual")
    write_json(
        task_dir / "edit" / "full_concat_drafts" / "v0001" / "full_concat_render_outputs.json",
        {"outputs": [{"file": "edit/full_concat_drafts/v0001/fc_001/automatic.mp4"}]},
    )
    os.utime(automatic, (1_000_000, 1_000_000))
    os.utime(manual, (2_000_000, 2_000_000))

    drafts = web_app._list_draft_videos(task_dir)

    assert drafts == [manual.resolve(), automatic.resolve()]


def test_manual_editor_adds_remote_asset_from_shared_cache_without_leaking_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs_dir = tmp_path / "outputs"
    task_id, task_dir = _make_full_concat_task(outputs_dir)
    shared_cache = tmp_path / "shared_remote_cache"
    remote_config = replace(
        web_app.REMOTE_UCMS_CONFIG,
        enabled=True,
        cache_dir=shared_cache,
    )
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", outputs_dir)
    monkeypatch.setattr(web_app, "REMOTE_UCMS_CONFIG", remote_config)

    source = tmp_path / "provider_source.mp4"
    _make_video(source, color="purple", seconds=1.25)
    download_calls: list[bool] = []

    def fake_download(config, remote_video, *, root_dir, force=False):
        assert config is remote_config
        assert root_dir == web_app.ROOT
        download_calls.append(bool(force))
        target = web_app.remote_cache_path(config, remote_video, create_dir=True)
        shutil.copyfile(source, target)
        return {
            "downloaded": True,
            # The editor endpoint must ignore this provider-returned path.
            "absolute_path": "C:/provider/untrusted-result.mp4",
            "metadata": {"remote_video": remote_video, "local_path": str(target)},
        }

    monkeypatch.setattr(web_app, "download_ucms_video", fake_download)
    remote_video = {
        "source_type": "remote_ucms",
        "remote_id": "remote-007",
        "id": 7,
        "guid": "guid-007",
        "name": "远程测试素材",
        "display_name": "远程测试素材 - 凤凰卫视",
        "record_station": "凤凰卫视",
        "create_time": "2026-07-15 10:30:00",
        "duration": 1.25,
        "download_url": "https://media.example/first-signed-url.mp4",
        "path": "C:/browser/forged-local.mp4",
        "absolute_path": "C:/browser/forged-absolute.mp4",
    }

    with TestClient(web_app.app) as client:
        added_response = client.post(
            f"/api/editor/tasks/{task_id}/remote-assets",
            json={"remote_video": remote_video, "force_remote_download": False},
        )
        assert added_response.status_code == 200, added_response.text
        added = added_response.json()
        assert added["duplicate"] is False
        assert added["downloaded"] is True
        assert added["project"]["revision"] == 1
        assert len(added["project"]["assets"]) == 2
        assert added["asset"]["source_type"] == "remote_ucms"
        assert added["asset"]["remote_key"].startswith("ucms_")
        assert added["asset"]["preview_url"].endswith("/preview")
        assert "path" not in added["asset"]
        assert str(shared_cache.resolve()) not in added_response.text
        assert "C:/browser" not in added_response.text
        assert "C:/provider" not in added_response.text
        assert "first-signed-url" not in added_response.text

        preview = client.get(added["asset"]["preview_url"])
        assert preview.status_code == 200
        assert preview.headers["content-type"].startswith("video/")

        changed_signed_url = {**remote_video, "download_url": "https://media.example/renewed.mp4"}
        duplicate_response = client.post(
            f"/api/editor/tasks/{task_id}/remote-assets",
            json={"remote_video": changed_signed_url},
        )
        assert duplicate_response.status_code == 200, duplicate_response.text
        duplicate = duplicate_response.json()
        assert duplicate["duplicate"] is True
        assert duplicate["project"]["revision"] == 1
        assert len(duplicate["project"]["assets"]) == 2

    assert download_calls == [False]
    canonical = read_json(task_dir / "edit" / "manual_editor" / "project.json", {})
    remote_asset = next(asset for asset in canonical["assets"] if asset.get("source_type") == "remote_ucms")
    expected_cache = web_app.remote_cache_path(remote_config, remote_video).resolve()
    assert Path(remote_asset["path"]).resolve() == expected_cache
    assert remote_asset["metadata"]["remote_ucms"].get("download_url") is None
    assert (task_dir / "edit" / "manual_editor" / "revisions" / "v0001.json").is_file()


def test_manual_editor_remote_download_failure_is_502_and_does_not_change_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs_dir = tmp_path / "outputs"
    task_id, task_dir = _make_full_concat_task(outputs_dir)
    remote_config = replace(
        web_app.REMOTE_UCMS_CONFIG,
        enabled=True,
        cache_dir=tmp_path / "shared_remote_cache",
    )
    monkeypatch.setattr(web_app, "OUTPUTS_DIR", outputs_dir)
    monkeypatch.setattr(web_app, "REMOTE_UCMS_CONFIG", remote_config)

    def fail_download(*_args, **_kwargs):
        raise RuntimeError("secret C:/server/cache/provider-token")

    monkeypatch.setattr(web_app, "download_ucms_video", fail_download)
    remote_video = {
        "remote_id": "remote-error",
        "name": "失败素材",
        "record_station": "凤凰卫视",
        "download_url": "https://media.example/failure.mp4",
    }

    with TestClient(web_app.app) as client:
        bootstrap = client.get(f"/api/editor/tasks/{task_id}")
        assert bootstrap.status_code == 200
        failed = client.post(
            f"/api/editor/tasks/{task_id}/remote-assets",
            json={"remote_video": remote_video},
        )
        assert failed.status_code == 502
        assert "secret" not in failed.text
        assert "C:/server" not in failed.text

    canonical = read_json(task_dir / "edit" / "manual_editor" / "project.json", {})
    assert canonical["revision"] == 0
    assert len(canonical["assets"]) == 1
    assert not (task_dir / "edit" / "manual_editor" / "revisions" / "v0001.json").exists()
