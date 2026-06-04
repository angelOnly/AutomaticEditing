from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from newsclip_agent.utils import read_json


def build_source_quality_report(sources_metadata: list[dict[str, Any]]) -> dict[str, Any]:
    """Check the initial quality of the source media files.
    Returns:
        dict: {"is_pass": bool, "reason": str}
    """
    if not sources_metadata:
        return {"is_pass": False, "reason": "No sources provided for quality check."}
        
    for index, metadata in enumerate(sources_metadata):
        if not metadata:
            continue
            
        has_video = metadata.get("has_video", False)
        has_audio = metadata.get("has_audio", False)
        duration = metadata.get("duration", 0)
        
        # Check basic streams
        if not has_video and not has_audio:
            return {"is_pass": False, "reason": f"Source {index} has neither video nor audio streams."}
            
        if not has_audio:
            return {"is_pass": False, "reason": f"Source {index} has no audio stream. Audio is required for analysis."}
            
        # Check duration
        if duration < 5:
            return {"is_pass": False, "reason": f"Source {index} is too short ({duration:.1f}s). Minimum required duration is 5 seconds."}
            
        # Optional: check resolution or other metadata
        width = metadata.get("width", 0)
        height = metadata.get("height", 0)
        if has_video and width and height:
            if width < 320 or height < 240:
                return {"is_pass": False, "reason": f"Source {index} resolution is too low ({width}x{height})."}

    return {"is_pass": True, "reason": ""}
