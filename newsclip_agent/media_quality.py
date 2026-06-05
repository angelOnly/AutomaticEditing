from typing import Any


def _first_stream(metadata: dict[str, Any], stream_type: str) -> dict[str, Any]:
    streams = metadata.get("streams") or []
    if not isinstance(streams, list):
        return {}
    return next(
        (
            stream for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == stream_type
        ),
        {},
    )


def _has_stream(metadata: dict[str, Any], stream_type: str) -> bool:
    return bool(_first_stream(metadata, stream_type))


def _duration_seconds(metadata: dict[str, Any]) -> float:
    for key in ("duration_seconds", "duration"):
        try:
            value = float(metadata.get(key) or 0)
            if value > 0:
                return value
        except Exception:
            pass

    fmt = metadata.get("format") or {}
    if isinstance(fmt, dict):
        try:
            return float(fmt.get("duration") or 0)
        except Exception:
            return 0.0

    return 0.0


def build_source_quality_report(sources_metadata: list[dict[str, Any]]) -> dict[str, Any]:
    if not sources_metadata:
        return {"is_pass": False, "reason": "No sources provided for quality check."}

    checked_sources: list[dict[str, Any]] = []

    for index, metadata in enumerate(sources_metadata, start=1):
        if not isinstance(metadata, dict) or not metadata:
            return {"is_pass": False, "reason": f"Source {index} metadata is empty."}

        if metadata.get("probe_error"):
            return {
                "is_pass": False,
                "reason": f"Source {index} ffprobe failed: {metadata.get('probe_error')}",
            }

        has_video = bool(
            metadata.get("has_video")
            or metadata.get("video_codec")
            or _has_stream(metadata, "video")
        )
        has_audio = bool(
            metadata.get("has_audio")
            or metadata.get("audio_codec")
            or _has_stream(metadata, "audio")
        )
        duration = _duration_seconds(metadata)

        if not has_video and not has_audio:
            return {"is_pass": False, "reason": f"Source {index} has neither video nor audio streams."}

        if not has_video:
            return {"is_pass": False, "reason": f"Source {index} has no video stream."}

        if not has_audio:
            return {
                "is_pass": False,
                "reason": f"Source {index} has no audio stream. Audio is required for ASR analysis.",
            }

        if duration < 5:
            return {
                "is_pass": False,
                "reason": f"Source {index} is too short ({duration:.1f}s). Minimum required duration is 5 seconds.",
            }

        video_stream = _first_stream(metadata, "video")
        audio_stream = _first_stream(metadata, "audio")
        width = int(metadata.get("width") or video_stream.get("width") or 0)
        height = int(metadata.get("height") or video_stream.get("height") or 0)

        if width and height and (width < 320 or height < 240):
            return {
                "is_pass": False,
                "reason": f"Source {index} resolution is too low ({width}x{height}).",
            }

        checked_sources.append({
            "source_index": index,
            "source_id": metadata.get("source_id", ""),
            "display_name": metadata.get("display_name", ""),
            "has_video": has_video,
            "has_audio": has_audio,
            "duration_seconds": round(duration, 3),
            "width": width,
            "height": height,
            "video_codec": metadata.get("video_codec", "") or video_stream.get("codec_name", ""),
            "audio_codec": metadata.get("audio_codec", "") or audio_stream.get("codec_name", ""),
        })

    return {
        "is_pass": True,
        "reason": "",
        "checked_sources": checked_sources,
    }
