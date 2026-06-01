from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import ensure_dir, ffprobe_json, relpath, run_cmd, seconds_to_timecode, write_json, write_text


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v", ".avi"}


@dataclass
class MultiSourceItem:
    source_id: str
    source_type: str
    path: Path
    display_name: str = ""
    remote: dict[str, Any] | None = None


def build_multi_source_video(
    *,
    sources: list[MultiSourceItem],
    task_dir: Path,
    root_dir: Path,
    aspect_ratio: str = "16:9",
    fps: int = 25,
    sample_rate: int = 48000,
) -> dict[str, Any]:
    if len(sources) < 2:
        raise ValueError("build_multi_source_video requires at least 2 sources")

    input_dir = ensure_dir(task_dir / "input")
    normalized_dir = ensure_dir(input_dir / "normalized_sources")
    output_video = input_dir / "source_multi.mp4"
    concat_file = input_dir / "concat_sources.txt"
    manifest_file = input_dir / "source_manifest.json"

    timeline_sources: list[dict[str, Any]] = []
    normalized_files: list[Path] = []
    cursor = 0.0

    for index, item in enumerate(sources, start=1):
        source_path = item.path.resolve()
        if not source_path.exists() or not source_path.is_file():
            raise FileNotFoundError(f"source not found: {source_path}")
        if source_path.suffix.lower() not in VIDEO_SUFFIXES:
            raise ValueError(f"unsupported video type: {source_path.name}")

        metadata = ffprobe_json(source_path)
        duration = float(metadata.get("duration") or 0)
        if duration <= 0:
            raise ValueError(f"invalid duration: {source_path.name}")

        normalized = normalized_dir / f"source_{index:03d}.mp4"
        cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source_path),
        ]
        has_audio = bool(metadata.get("audio_codec"))
        if not has_audio:
            cmd.extend(
                [
                    "-f",
                    "lavfi",
                    "-t",
                    str(duration),
                    "-i",
                    f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}",
                ]
            )
        cmd.extend(
            [
                "-vf",
                _normalize_video_filter(aspect_ratio),
                "-r",
                str(fps),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0" if has_audio else "1:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                str(sample_rate),
                "-ac",
                "2",
                "-shortest",
                "-movflags",
                "+faststart",
                str(normalized),
            ]
        )
        run_cmd(cmd)
        normalized_files.append(normalized)

        normalized_meta = ffprobe_json(normalized)
        normalized_duration = float(normalized_meta.get("duration") or duration)
        timeline_sources.append(
            {
                "source_id": item.source_id or f"src_{index:03d}",
                "source_index": index,
                "source_type": item.source_type,
                "display_name": item.display_name or source_path.name,
                "original_path": str(source_path),
                "normalized_file": relpath(normalized, task_dir),
                "original_duration_seconds": round(duration, 3),
                "duration_seconds": round(normalized_duration, 3),
                "virtual_start_seconds": round(cursor, 3),
                "virtual_end_seconds": round(cursor + normalized_duration, 3),
                "virtual_start": seconds_to_timecode(cursor, ms=True),
                "virtual_end": seconds_to_timecode(cursor + normalized_duration, ms=True),
                "remote": item.remote or {},
            }
        )
        cursor += normalized_duration

    write_text(concat_file, "".join(f"file '{_concat_path(path)}'\n" for path in normalized_files))
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(output_video),
        ]
    )

    output_meta = ffprobe_json(output_video)
    manifest = {
        "source_mode": "multi_source_concat_proxy",
        "source_count": len(sources),
        "source_video": relpath(output_video, task_dir),
        "total_duration_seconds": round(float(output_meta.get("duration") or cursor), 3),
        "aspect_ratio": aspect_ratio,
        "fps": fps,
        "sample_rate": sample_rate,
        "sources": timeline_sources,
    }
    write_json(manifest_file, manifest)
    return {
        "input_video": output_video,
        "source_manifest": manifest_file,
        "manifest": manifest,
    }


def _normalize_video_filter(aspect_ratio: str) -> str:
    if aspect_ratio == "9:16":
        return "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    return "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,setsar=1"


def _concat_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")
