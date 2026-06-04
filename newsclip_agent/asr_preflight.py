from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any


def _check_cmd(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    if not path:
        return {"ok": False, "name": name, "error": f"{name} not found in PATH"}
    try:
        proc = subprocess.run(
            [name, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        first_line = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
        return {"ok": proc.returncode == 0, "name": name, "path": path, "version": first_line}
    except Exception as exc:
        return {"ok": False, "name": name, "path": path, "error": str(exc)}


def _check_python_imports() -> list[dict[str, Any]]:
    results = []
    for module in ["torch", "funasr"]:
        try:
            __import__(module)
            results.append({"ok": True, "module": module})
        except Exception as exc:
            results.append({"ok": False, "module": module, "error": str(exc)})
    return results


def _check_cuda() -> dict[str, Any]:
    try:
        import torch

        available = bool(torch.cuda.is_available())
        return {
            "ok": available,
            "available": available,
            "device_count": torch.cuda.device_count() if available else 0,
            "device_name": torch.cuda.get_device_name(0) if available else "",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _required_model_dirs(model_base: Path) -> dict[str, Path]:
    return {
        "SenseVoiceSmall": model_base / "SenseVoiceSmall",
        "vad": model_base / "speech_fsmn_vad_zh-cn-16k-common-pytorch",
        "punc": model_base / "punc_ct-transformer_cn-en-common-vocab471067-large",
    }


def _check_model_dirs(model_base: Path) -> list[dict[str, Any]]:
    results = []
    for name, path in _required_model_dirs(model_base).items():
        ok = path.exists() and path.is_dir()
        results.append({
            "ok": ok,
            "name": name,
            "path": str(path),
            "error": "missing model directory" if not ok else "",
        })
    return results


def _make_silent_wav(path: Path, seconds: float = 1.0, sample_rate: int = 16000) -> None:
    frame_count = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * frame_count)


def run_asr_preflight(config, *, smoke_test: bool = False) -> dict[str, Any]:
    root = config.root_dir
    funasr_cfg = config.funasr
    model_base = config.resolve_path(funasr_cfg.get("asr_model_path"), "models/iic")
    device = str(funasr_cfg.get("device", "cuda:0"))

    report: dict[str, Any] = {
        "ok": True,
        "ffmpeg": _check_cmd("ffmpeg"),
        "ffprobe": _check_cmd("ffprobe"),
        "imports": _check_python_imports(),
        "cuda": _check_cuda(),
        "model_base": str(model_base),
        "models": _check_model_dirs(model_base),
        "model_load": None,
        "smoke_test": None,
    }

    checks = [report["ffmpeg"], report["ffprobe"], *report["imports"], *report["models"]]
    if any(not item.get("ok") for item in checks):
        report["ok"] = False
        return report

    try:
        from services.asr.FunASREngine import FunASREngine

        engine = FunASREngine(
            model_name=str(model_base / "SenseVoiceSmall"),
            vad_model=str(model_base / "speech_fsmn_vad_zh-cn-16k-common-pytorch"),
            punc_model=str(model_base / "punc_ct-transformer_cn-en-common-vocab471067-large"),
            device=device,
        )
        engine._load_model(device)
        report["model_load"] = {"ok": True, "device": device}

        if smoke_test:
            with tempfile.TemporaryDirectory() as td:
                wav_path = Path(td) / "silent.wav"
                _make_silent_wav(wav_path, seconds=float(funasr_cfg.get("smoke_test_seconds", 3)))
                result = engine.transcribe(str(wav_path))
                report["smoke_test"] = {
                    "ok": bool(result.get("success")),
                    "elapsed": result.get("elapsed"),
                    "error": result.get("error"),
                }
                if not result.get("success"):
                    report["ok"] = False

        engine.release()
    except Exception as exc:
        report["model_load"] = {"ok": False, "error": str(exc)}
        report["ok"] = False

    return report
