from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str | Path, data: Any) -> Path:
    p = Path(path)
    ensure_dir(p.parent)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return p


def write_text(path: str | Path, text: str) -> Path:
    p = Path(path)
    ensure_dir(p.parent)
    p.write_text(text, encoding="utf-8")
    return p


def read_text(path: str | Path, default: str = "") -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else default


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_hash(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def output_hash(paths: list[str | Path]) -> str:
    data = []
    for path in paths:
        p = Path(path)
        if p.exists() and p.is_file():
            data.append({"path": str(p), "sha256": file_hash(p), "size": p.stat().st_size})
    return stable_hash(data)


def seconds_to_timecode(seconds: float, ms: bool = False) -> str:
    seconds = max(0.0, float(seconds or 0))
    total_ms = int(round(seconds * 1000))
    h = total_ms // 3_600_000
    total_ms %= 3_600_000
    m = total_ms // 60_000
    total_ms %= 60_000
    s = total_ms // 1000
    milli = total_ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}" if ms else f"{h:02d}:{m:02d}:{s:02d}"


def timecode_to_seconds(value: str | float | int | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text:
        return 0.0
    parts = text.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return 0.0


def srt_time(seconds: float) -> str:
    return seconds_to_timecode(seconds, ms=True).replace(".", ",")


def run_cmd(cmd: list[str], cwd: str | Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        timeout=timeout,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def require_exe(name: str) -> str:
    exe = shutil.which(name)
    if not exe:
        raise RuntimeError(f"找不到可执行程序: {name}")
    return exe


def ffprobe_json(video_path: str | Path) -> dict[str, Any]:
    require_exe("ffprobe")
    proc = run_cmd([
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(video_path),
    ])
    data = json.loads(proc.stdout)
    v_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    duration = float(data.get("format", {}).get("duration") or v_stream.get("duration") or 0)
    fps = 0.0
    fps_text = v_stream.get("avg_frame_rate") or v_stream.get("r_frame_rate") or "0/1"
    if "/" in fps_text:
        num, den = fps_text.split("/", 1)
        fps = float(num) / max(1.0, float(den))
    return {
        "duration": duration,
        "duration_timecode": seconds_to_timecode(duration, ms=True),
        "width": int(v_stream.get("width") or 0),
        "height": int(v_stream.get("height") or 0),
        "fps": round(fps, 3),
        "video_codec": v_stream.get("codec_name", ""),
        "audio_codec": a_stream.get("codec_name", ""),
        "format": data.get("format", {}),
        "streams": data.get("streams", []),
    }


def extract_json_object(text: str) -> Any:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S | re.I)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return _loads_json_relaxed(cleaned)
    except Exception:
        pass
    start_candidates = [i for i in [cleaned.find("{"), cleaned.find("[")] if i >= 0]
    if not start_candidates:
        raise ValueError("模型响应中没有 JSON 对象")
    start = min(start_candidates)
    end = max(cleaned.rfind("}"), cleaned.rfind("]"))
    if end < start:
        raise ValueError("模型响应中 JSON 不完整")
    return _loads_json_relaxed(cleaned[start : end + 1])


def _loads_json_relaxed(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import json5  # type: ignore
        except Exception:
            raise
        return json5.loads(text)


def image_to_data_url(path: str | Path) -> str:
    p = Path(path)
    mime = "image/jpeg"
    if p.suffix.lower() == ".png":
        mime = "image/png"
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def relpath(path: str | Path, root: str | Path) -> str:
    try:
        return os.path.relpath(Path(path), Path(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def copy_or_link(src: str | Path, dst: str | Path) -> None:
    src_p = Path(src)
    dst_p = Path(dst)
    ensure_dir(dst_p.parent)
    if dst_p.exists():
        return
    shutil.copy2(src_p, dst_p)
