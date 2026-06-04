from __future__ import annotations

from pathlib import Path


PROMPT_DIR = Path(__file__).resolve().parent / "prompt_texts"


def _load_prompt(name: str) -> str:
    path = PROMPT_DIR / name
    return path.read_text(encoding="utf-8").strip() + "\n"


ASR_DIGEST_PROMPT = _load_prompt("asr_digest.txt")
VISION_CHUNK_PROMPT = _load_prompt("vision_chunk.txt")
VIDEO_UNDERSTANDING_PROMPT = _load_prompt("video_understanding.txt")
CONTENT_ANALYSIS_PROMPT = _load_prompt("content_analysis.txt")
HIGHLIGHT_DETECTION_PROMPT = _load_prompt("highlight_detection.txt")
SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT = _load_prompt("short_video_edit_plan_text.txt")
VOICEOVER_SCRIPT_TEXT_PROMPT = _load_prompt("voiceover_script_text.txt")
HIGHLIGHT_REASSEMBLY_TEXT_PROMPT = _load_prompt("highlight_reassembly_text.txt")
