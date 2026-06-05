from __future__ import annotations

import re
from typing import Any


ASR_SPECIAL_TOKEN_PATTERNS = [
    r"<\s*\|\s*zh\s*\|\s*>",
    r"<\s*\|\s*en\s*\|\s*>",
    r"<\s*\|\s*NEUTRAL\s*\|\s*>",
    r"<\s*\|\s*Speech\s*\|\s*>",
    r"\[Music\]",
    r"\[Applause\]",
]


def clean_asr_text(text: str) -> dict[str, Any]:
    raw = text or ""
    removed: list[str] = []
    clean = raw

    for pattern in ASR_SPECIAL_TOKEN_PATTERNS:
        matches = re.findall(pattern, clean, flags=re.IGNORECASE)
        if matches:
            removed.extend(str(match) for match in matches)
            clean = re.sub(pattern, "", clean, flags=re.IGNORECASE)

    clean = re.sub(r"\s+", " ", clean).strip()
    return {
        "raw_text": raw,
        "clean_text": clean,
        "removed_tokens": removed,
    }
