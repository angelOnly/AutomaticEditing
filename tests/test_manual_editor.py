from __future__ import annotations

import copy
import shutil
import subprocess
from pathlib import Path

import pytest

from newsclip_agent.manual_editor import (
    ManualEditorError,
    ProjectConflictError,
    ProjectValidationError,
    add_mosaic_effect,
    add_timeline_mosaic_effect,
    apply_frontend_project,
    export_project,
    from_web_project,
    initialize_project,
    insert_clip,
    move_clip,
    project_errors,
    remove_effect,
    reorder_clips,
    ripple_delete,
    save_project_revision,
    split_clip,
    to_frontend_project,
    to_web_project,
    trim_clip,
    validate_project,
)


def _raw_clip(clip_id: str, source_id: str, start: float, end: float) -> dict:
    return {
        "clip_id": clip_id,
        "source_id": source_id,
        "local_start_seconds": start,
        "local_end_seconds": end,
        "duration_seconds": end - start,
    }


def _plan(*clips: dict) -> dict:
    return {
        "project_id": "task_001",
        "production_mode": "full_concat",
        "source_video": "",
        "output_videos": [
            {
                "reassembly_id": "fc_001",
                "aspect_ratio": "16:9",
                "target_resolution": "320x180",
                "clips": list(clips),
            }
        ],
    }


@pytest.fixture
def project(tmp_path: Path) -> dict:
    return initialize_project(
        _plan(
            _raw_clip("c1", "s1", 0, 10),
            _raw_clip("c2", "s1", 20, 30),
            _raw_clip("c3", "s2", 5, 15),
        ),
        source_paths={"s1": tmp_path / "one.mp4", "s2": tmp_path / "two.mp4"},
        settings={"fps": 10},
    )


def _clips(project: dict) -> list[dict]:
    return project["timeline"]["clips"]


def test_initialize_project_is_single_track_and_non_destructive(tmp_path: Path):
    original = _plan(_raw_clip("c1", "source_a", 1.5, 4.5))
    untouched = copy.deepcopy(original)
    result = initialize_project(original, source_paths={"source_a": tmp_path / "source.mp4"})

    assert original == untouched
    assert result["schema"] == "newsclip.manual_editor"
    assert result["timeline"]["mode"] == "single_track"
    assert result["timeline"]["duration_seconds"] == 3.0
    assert _clips(result)[0]["source_in"] == 1.5
    assert _clips(result)[0]["timeline_start"] == 0.0
    assert result["source_plan"]["content_hash"]
    validate_project(result)


def test_initialize_resolves_multi_source_manifest(tmp_path: Path):
    source_a = tmp_path / "a.mp4"
    source_b = tmp_path / "b.mp4"
    source_a.touch()
    source_b.touch()
    manifest = {
        "source_mode": "multi_source_pool",
        "sources": [
            {"source_id": "a", "original_path": str(source_a), "duration_seconds": 10},
            {"source_id": "b", "original_path": str(source_b), "duration_seconds": 20},
        ],
    }
    result = initialize_project(
        _plan(_raw_clip("a1", "a", 0, 2), _raw_clip("b1", "b", 3, 5)),
        source_manifest=manifest,
    )
    assert [asset["path"] for asset in result["assets"]] == [str(source_a), str(source_b)]
    assert [asset["duration_seconds"] for asset in result["assets"]] == [10.0, 20.0]


def test_ripple_delete_spans_clip_boundaries(project: dict):
    result = ripple_delete(project, 5, 25)
    clips = _clips(result)

    assert project["timeline"]["duration_seconds"] == 30.0
    assert [(clip["source_in"], clip["source_out"]) for clip in clips] == [(0.0, 5.0), (10.0, 15.0)]
    assert [(clip["timeline_start"], clip["timeline_end"]) for clip in clips] == [(0.0, 5.0), (5.0, 10.0)]
    assert result["revision"] == 1


def test_ripple_delete_inside_clip_splits_and_remaps_mosaic(project: dict):
    with_effect = add_mosaic_effect(
        project,
        "c1",
        start_seconds=1,
        end_seconds=8,
        x=0.1,
        y=0.2,
        width=0.3,
        height=0.4,
    )
    result = ripple_delete(with_effect, 2, 4)
    left, right = _clips(result)[:2]

    assert (left["source_in"], left["source_out"]) == (0.0, 2.0)
    assert (right["source_in"], right["source_out"]) == (4.0, 10.0)
    assert (left["effects"][0]["start_seconds"], left["effects"][0]["end_seconds"]) == (1.0, 2.0)
    assert (right["effects"][0]["start_seconds"], right["effects"][0]["end_seconds"]) == (0.0, 4.0)
    assert left["effects"][0]["effect_id"] != right["effects"][0]["effect_id"]


def test_split_and_trim_clip_remap_effects(project: dict):
    with_effect = add_mosaic_effect(
        project,
        "c1",
        start_seconds=1,
        end_seconds=8,
        x=0,
        y=0,
        width=0.25,
        height=0.25,
    )
    split = split_clip(with_effect, "c1", 3)
    left, right = _clips(split)[:2]
    assert (left["source_in"], left["source_out"]) == (0.0, 3.0)
    assert (right["source_in"], right["source_out"]) == (3.0, 10.0)
    assert (right["effects"][0]["start_seconds"], right["effects"][0]["end_seconds"]) == (0.0, 5.0)

    trimmed = trim_clip(with_effect, "c1", source_in=2, source_out=9)
    trimmed_clip = _clips(trimmed)[0]
    assert trimmed_clip["duration_seconds"] == 7.0
    assert (trimmed_clip["effects"][0]["start_seconds"], trimmed_clip["effects"][0]["end_seconds"]) == (
        0.0,
        6.0,
    )
    with pytest.raises(ManualEditorError, match="cannot expand"):
        trim_clip(trimmed, "c1", source_in=1)


def test_insert_inside_clip_then_move_and_reorder(project: dict):
    inserted = insert_clip(project, "s2", 0, 2, timeline_seconds=5, clip_id="inserted")
    clips = _clips(inserted)
    assert [clip["clip_id"] for clip in clips[:3]][:2] == ["c1", "inserted"]
    assert clips[0]["source_out"] == 5.0
    assert clips[2]["source_in"] == 5.0
    assert inserted["timeline"]["duration_seconds"] == 32.0

    moved = move_clip(inserted, "inserted", len(clips) - 1)
    assert _clips(moved)[-1]["clip_id"] == "inserted"
    ids = [clip["clip_id"] for clip in _clips(moved)]
    reversed_project = reorder_clips(moved, list(reversed(ids)))
    assert [clip["clip_id"] for clip in _clips(reversed_project)] == list(reversed(ids))
    with pytest.raises(ManualEditorError, match="permutation"):
        reorder_clips(moved, ids[:-1])


def test_timeline_mosaic_splits_across_clips_and_can_be_removed(project: dict):
    result = add_timeline_mosaic_effect(
        project,
        start_seconds=8,
        end_seconds=12,
        x=0.25,
        y=0.25,
        width=0.5,
        height=0.5,
        block_size=12,
    )
    first, second = _clips(result)[:2]
    assert (first["effects"][0]["start_seconds"], first["effects"][0]["end_seconds"]) == (8.0, 10.0)
    assert (second["effects"][0]["start_seconds"], second["effects"][0]["end_seconds"]) == (0.0, 2.0)
    assert first["effects"][0]["group_id"] == second["effects"][0]["group_id"]

    removed = remove_effect(result, first["effects"][0]["effect_id"])
    assert not _clips(removed)[0]["effects"]
    assert _clips(removed)[1]["effects"]


def test_validation_reports_bad_geometry_and_timeline(project: dict):
    broken = copy.deepcopy(project)
    broken["timeline"]["clips"][1]["timeline_start"] = 99
    errors = project_errors(broken)
    assert any("ripple-contiguous" in error for error in errors)
    with pytest.raises(ProjectValidationError):
        validate_project(broken)
    with pytest.raises(ProjectValidationError, match="normalized"):
        add_mosaic_effect(
            project,
            "c1",
            start_seconds=0,
            end_seconds=1,
            x=0.9,
            y=0,
            width=0.2,
            height=0.2,
        )


def test_save_project_revision_refuses_overwrite(project: dict, tmp_path: Path):
    paths = save_project_revision(project, tmp_path / "editor")
    assert paths["project"].is_file()
    assert paths["revision"].name == "v0000.json"
    changed = copy.deepcopy(project)
    changed["updated_at"] = "different"
    with pytest.raises(ManualEditorError, match="different content"):
        save_project_revision(changed, tmp_path / "editor")


def test_frontend_bridge_flattens_effects_hides_paths_and_checks_revision(project: dict):
    with_effect = add_mosaic_effect(
        project,
        "c1",
        start_seconds=1,
        end_seconds=2,
        x=0.1,
        y=0.2,
        width=0.3,
        height=0.4,
        block_size=14,
    )
    with_effect["source_plan"]["path"] = "C:/server/private/full_concat_cut_plan.json"
    frontend = to_web_project(with_effect, asset_urls={"s1": "/api/files/s1"})
    assert "path" not in frontend["assets"][0]
    assert "path" not in frontend["source_plan"]
    assert frontend["assets"][0]["preview_url"] == "/api/files/s1"
    assert "effects" not in frontend["clips"][0]
    assert frontend["effects"][0]["rect"] == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    assert frontend["effects"][0]["strength"] == 14

    trusted_asset_path = with_effect["assets"][0]["path"]
    frontend["assets"][0]["path"] = "C:/untrusted/injected.mp4"
    frontend["settings"]["background"] = "black;movie=C:/untrusted/file.mp4"
    frontend["clips"][0]["source_in"] = 0.5
    frontend["clips"][0]["metadata"] = {"path": "C:/untrusted/metadata.mp4"}
    frontend["clips"].reverse()
    saved = from_web_project(frontend, with_effect)
    assert saved["revision"] == with_effect["revision"] + 1
    assert saved["assets"][0]["path"] == trusted_asset_path
    assert saved["settings"] == with_effect["settings"]
    assert "path" not in _clips(saved)[-1]["metadata"]
    assert _clips(saved)[-1]["source_in"] == 0.5
    assert _clips(saved)[-1]["effects"][0]["start_seconds"] == 1.0
    with pytest.raises(ProjectConflictError, match="revision conflict"):
        from_web_project(frontend, saved)
    assert to_frontend_project(with_effect) == to_web_project(with_effect)
    frontend["revision"] = with_effect["revision"]
    assert apply_frontend_project(with_effect, frontend)["revision"] == with_effect["revision"] + 1


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg is unavailable")
def test_export_normalizes_silent_audio_mosaic_and_uses_cache(tmp_path: Path):
    with_audio = tmp_path / "with_audio.mp4"
    silent = tmp_path / "silent.mp4"
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
            "color=c=red:s=160x120:r=10:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=2",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(with_audio),
        ],
        check=True,
    )
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
            "color=c=blue:s=120x160:r=15:d=1",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(silent),
        ],
        check=True,
    )

    result = initialize_project(
        _plan(_raw_clip("audio", "a", 0.2, 1.0), _raw_clip("silent", "b", 0, 0.7)),
        source_paths={"a": with_audio, "b": silent},
        settings={"width": 320, "height": 180, "fps": 10, "sample_rate": 48000},
    )
    result = add_mosaic_effect(
        result,
        "audio",
        start_seconds=0,
        end_seconds=0.8,
        x=0.25,
        y=0.25,
        width=0.5,
        height=0.5,
        block_size=10,
    )
    output = tmp_path / "export.mp4"
    cache = tmp_path / "cache"
    report = export_project(result, output, cache_dir=cache)

    assert report["status"] == "ok"
    assert report["rendered_clip_count"] == 2
    assert report["cached_clip_count"] == 0
    assert report["width"] == 320 and report["height"] == 180
    assert report["fps"] == 10
    assert report["sample_aspect_ratio"] == "1:1"
    assert report["pixel_format"] == "yuv420p"
    assert report["audio_codec"] == "aac"
    assert report["audio_sample_rate"] == 48000
    assert output.with_suffix(".mp4.qc.json").is_file()

    second_report = export_project(result, tmp_path / "export_again.mp4", cache_dir=cache)
    assert second_report["status"] == "ok"
    assert second_report["cached_clip_count"] == 2
    assert second_report["rendered_clip_count"] == 0
