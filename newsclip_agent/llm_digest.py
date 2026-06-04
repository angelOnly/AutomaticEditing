from __future__ import annotations

import json
import re
from typing import Any


NEWS_KEYWORDS: list[str] = []


def compact_text(text: str, max_chars: int = 320) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_chars:
        return text

    parts = re.split(r"(?<=[銆傦紒锛燂紱?!;])", text)
    selected: list[str] = []
    total = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if total + len(part) > max_chars:
            break
        selected.append(part)
        total += len(part)

    if selected:
        return "".join(selected).strip()
    return text[:max_chars].strip() + "..."


def compact_asr_for_llm(text: str, max_chars: int = 320) -> str:
    return compact_text(text, max_chars=max_chars)



def compact_list(items: list[Any], max_items: int = 5, max_chars_each: int = 30) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        if isinstance(item, dict):
            text = item.get("text") or item.get("name") or item.get("value") or json.dumps(item, ensure_ascii=False)
        else:
            text = str(item)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        text = text[:max_chars_each]
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= max_items:
            break
    return out


def build_chunk_flags(item: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    scene = str(item.get("scene_type") or item.get("scene") or "").lower()
    if "press" in scene or "conference" in scene:
        flags.append("press_conference")
    if "chart" in scene or "data" in scene:
        flags.append("data_visual")
    if item.get("is_archive_footage"):
        flags.append("archive_footage")
    if item.get("is_live_scene"):
        flags.append("live_scene")
    try:
        if float(item.get("hook_score") or 0) >= 7:
            flags.append("good_opening")
    except (ValueError, TypeError):
        pass
    
    try:
        if float(item.get("visual_value_score") or item.get("visual_score") or 0) >= 7:
            flags.append("strong_visual")
    except (ValueError, TypeError):
        pass
        
    if item.get("visible_people") or item.get("people"):
        flags.append("people_visible")
    return flags


def select_frames_for_vision(frame_paths: list[str], max_frames: int = 6) -> list[str]:
    if len(frame_paths) <= max_frames:
        return frame_paths
    if max_frames <= 1:
        return [frame_paths[0]]

    n = len(frame_paths)
    indexes = [round(i * (n - 1) / (max_frames - 1)) for i in range(max_frames)]
    selected: list[str] = []
    seen: set[int] = set()
    for idx in indexes:
        if idx not in seen:
            seen.add(idx)
            selected.append(frame_paths[idx])
    return selected


def compact_chunk_for_vision(chunk: dict[str, Any]) -> dict[str, Any]:
    res = {
        "chunk_id": chunk.get("chunk_id", ""),
        "start": chunk.get("start", 0),
        "end": chunk.get("end", 0),
        "time_range": chunk.get("time_range", ""),
        "frame_count": len(chunk.get("frames", []) or []),
    }
    if "source_id" in chunk:
        res["source_id"] = chunk["source_id"]
        res["local_time_range"] = f"{chunk.get('local_start_seconds', 0):.1f}-{chunk.get('local_end_seconds', 0):.1f}"
    return res
