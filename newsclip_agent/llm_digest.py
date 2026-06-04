from __future__ import annotations

import json
import re
from typing import Any


NEWS_KEYWORDS = [
    "宣布",
    "表示",
    "指出",
    "强调",
    "回应",
    "发布",
    "数据",
    "同比",
    "增长",
    "一季度",
    "工信部",
    "算力",
    "人工智能",
    "工业",
    "制造业",
    "政策",
    "冲突",
    "袭击",
    "外交",
    "会谈",
    "制裁",
    "事故",
    "伤亡",
]


def compact_text(text: str, max_chars: int = 320) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_chars:
        return text

    parts = re.split(r"(?<=[。！？；?!;])", text)
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
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_chars:
        return text

    sentences = [x.strip() for x in re.split(r"(?<=[。！？；?!;])", text) if x.strip()]
    if not sentences:
        return text[:max_chars].strip() + "..."

    scored: list[tuple[int, int, str]] = []
    for idx, sentence in enumerate(sentences):
        score = sum(2 for keyword in NEWS_KEYWORDS if keyword in sentence)
        if idx <= 1:
            score += 1
        if idx >= len(sentences) - 2:
            score += 1
        scored.append((score, idx, sentence))

    picked: list[tuple[int, str]] = []
    total = 0
    for _score, idx, sentence in sorted(scored, key=lambda x: (-x[0], x[1])):
        if total + len(sentence) <= max_chars:
            picked.append((idx, sentence))
            total += len(sentence)

    picked.sort(key=lambda x: x[0])
    result = "".join(sentence for _idx, sentence in picked).strip()
    return result or text[:max_chars].strip() + "..."


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
    scene = item.get("scene_type") or item.get("scene") or ""
    if "发布会" in scene:
        flags.append("press_conference")
    if "图表" in scene or "数据" in scene:
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
    return {
        "chunk_id": chunk.get("chunk_id", ""),
        "start": chunk.get("start", 0),
        "end": chunk.get("end", 0),
        "time_range": chunk.get("time_range", ""),
        "frame_count": len(chunk.get("frames", []) or []),
    }
