from __future__ import annotations

from typing import Any

from .asr_cleaning import clean_asr_text
from .llm_digest import compact_list, compact_text
from .utils import seconds_to_timecode


FORBIDDEN_LLM_FIELDS = {
    "start",
    "end",
    "start_seconds",
    "end_seconds",
    "source_start",
    "source_end",
    "source_start_seconds",
    "source_end_seconds",
    "global_start",
    "global_end",
    "global_start_seconds",
    "global_end_seconds",
    "virtual_start",
    "virtual_end",
    "virtual_start_seconds",
    "virtual_end_seconds",
    "source_path",
    "file_path",
    "manifest_path",
    "cache_path",
    "work_task_dir",
    "source_manifest",
    "normalized_file",
    "original_path",
}


def local_time_range(item: dict[str, Any]) -> str:
    start = item.get("local_start") or seconds_to_timecode(
        float(item.get("local_start_seconds") or item.get("start_seconds") or item.get("start") or 0),
        ms=True,
    )
    end = item.get("local_end") or seconds_to_timecode(
        float(item.get("local_end_seconds") or item.get("end_seconds") or item.get("end") or 0),
        ms=True,
    )
    return f"{start}-{end}"


def sanitize_llm_text(text: str, *, max_chars: int) -> str:
    return compact_text(clean_asr_text(str(text or ""))["clean_text"], max_chars=max_chars)


def compact_sources_for_llm(sources: list[dict[str, Any]], *, max_sources: int = 20) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in sources[:max_sources]:
        if not isinstance(item, dict):
            continue
        out.append({
            "source_id": item.get("source_id", ""),
            "source_index": item.get("source_index", ""),
            "display_name": item.get("display_name", ""),
            "duration_seconds": item.get("duration_seconds", 0),
            "summary": sanitize_llm_text(str(item.get("summary") or ""), max_chars=160),
            "key_facts": compact_list(item.get("key_facts") or [], max_items=2, max_chars_each=70),
        })
    return out


def compact_chunks_for_llm(chunks: list[dict[str, Any]], *, max_chunks: int | None = None) -> list[dict[str, Any]]:
    selected = chunks[:max_chunks] if max_chunks is not None else chunks
    out: list[dict[str, Any]] = []
    for chunk in selected:
        if not isinstance(chunk, dict):
            continue
        out.append({
            "chunk_id": chunk.get("chunk_id") or chunk.get("global_chunk_id") or "",
            "source_id": chunk.get("source_id", ""),
            "source_index": chunk.get("source_index", ""),
            "time_range": local_time_range(chunk),
            "duration_seconds": chunk.get("duration_seconds", 0),
            "speech": sanitize_llm_text(str(chunk.get("speech") or chunk.get("asr_text") or ""), max_chars=70),
            "visual": compact_text(str(chunk.get("visual") or chunk.get("visual_summary") or ""), max_chars=50),
            "visual_score": chunk.get("visual_score", chunk.get("visual_value_score", 0)),
            "hook_score": chunk.get("hook_score", 0),
            "flags": compact_list(chunk.get("flags") or chunk.get("risk_tags") or [], max_items=2, max_chars_each=18),
        })
    return out


def validate_no_forbidden_llm_fields(obj: Any, *, path: str = "") -> None:
    if isinstance(obj, dict):
        found = FORBIDDEN_LLM_FIELDS.intersection(obj.keys())
        if found:
            raise ValueError(f"forbidden LLM fields at {path or '<root>'}: {', '.join(sorted(found))}")
        for key, value in obj.items():
            validate_no_forbidden_llm_fields(value, path=f"{path}.{key}" if path else str(key))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            validate_no_forbidden_llm_fields(value, path=f"{path}[{index}]")
