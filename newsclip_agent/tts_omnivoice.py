from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import importlib.metadata as metadata

from .utils import ensure_dir


SAMPLE_RATE = 24000
_MODEL_CACHE: dict[tuple[str, str], Any] = {}
REQUIRED_TRANSFORMERS_VERSION = "5.3.0"


@dataclass
class TTSResult:
    file: str
    status: str
    error: str = ""
    text_length: int = 0
    estimated_duration_seconds: float = 0.0
    actual_duration_seconds: float = 0.0
    sample_rate: int = SAMPLE_RATE
    channels: int = 1
    file_size: int = 0
    voice_file_exists: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def audio_metadata(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"actual_duration_seconds": 0.0, "sample_rate": 0, "channels": 0, "file_size": 0, "voice_file_exists": False}
    try:
        import soundfile as sf

        info = sf.info(str(p))
        duration = float(info.frames) / max(float(info.samplerate), 1.0)
        return {
            "actual_duration_seconds": round(duration, 3),
            "sample_rate": int(info.samplerate),
            "channels": int(info.channels),
            "file_size": p.stat().st_size,
            "voice_file_exists": True,
        }
    except Exception:
        return {"actual_duration_seconds": 0.0, "sample_rate": 0, "channels": 0, "file_size": p.stat().st_size, "voice_file_exists": True}


def check_omnivoice_runtime() -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "omnivoice_version": "",
        "transformers_version": "",
        "required_transformers_version": f">={REQUIRED_TRANSFORMERS_VERSION}",
        "has_higgs_audio_tokenizer": False,
        "error": "",
    }
    try:
        from packaging.version import Version
        import transformers

        result["transformers_version"] = getattr(transformers, "__version__", "")
        result["has_higgs_audio_tokenizer"] = hasattr(transformers, "HiggsAudioV2TokenizerModel")
        try:
            result["omnivoice_version"] = metadata.version("omnivoice")
        except Exception:
            result["omnivoice_version"] = "unknown"
        version_ok = Version(result["transformers_version"]) >= Version(REQUIRED_TRANSFORMERS_VERSION)
        if not version_ok:
            result["error"] = (
                f"OmniVoice 0.1.3 需要 transformers>={REQUIRED_TRANSFORMERS_VERSION}，"
                f"当前是 {result['transformers_version']}。请在当前 conda 环境升级 transformers。"
            )
            return result
        if not result["has_higgs_audio_tokenizer"]:
            result["error"] = "当前 transformers 未暴露 HiggsAudioV2TokenizerModel，OmniVoice 无法加载 audio_tokenizer。"
            return result
        from omnivoice import OmniVoice  # noqa: F401

        result["ok"] = True
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def generate_omnivoice_audio(
    *,
    text: str,
    output_path: str | Path,
    model_path: str | Path,
    reference_audio: str | Path,
    reference_text: str | None = None,
    device: str | None = None,
    dtype: Any | None = None,
    keep_model_loaded: bool = False,
    speed: float | None = None,
) -> TTSResult:
    output = Path(output_path)
    ensure_dir(output.parent)
    text = text or ""
    if not text.strip():
        return TTSResult(file=str(output), status="skipped", text_length=0)
    try:
        runtime = check_omnivoice_runtime()
        if not runtime["ok"]:
            raise RuntimeError(runtime["error"])
        import torch
        import soundfile as sf
        from omnivoice import OmniVoice

        model_path = Path(model_path)
        reference_audio = Path(reference_audio)
        if not model_path.exists():
            raise FileNotFoundError(f"OmniVoice model path not found: {model_path}")
        if not reference_audio.exists():
            raise FileNotFoundError(f"Reference audio not found: {reference_audio}")

        device = device or ("cuda:0" if torch.cuda.is_available() else "cpu")
        dtype = dtype or (torch.float16 if torch.cuda.is_available() else torch.float32)
        cache_key = (str(model_path.resolve()), device)
        model = _MODEL_CACHE.get(cache_key)
        if model is None:
            model = OmniVoice.from_pretrained(str(model_path), device_map=device, dtype=dtype)
            if keep_model_loaded:
                _MODEL_CACHE[cache_key] = model

        kwargs = {}
        if speed is not None:
            kwargs["speed"] = speed
        audio = model.generate(text=text, ref_audio=str(reference_audio), ref_text=reference_text or None, **kwargs)
        audio_numpy = audio[0].detach().cpu().numpy()
        if audio_numpy.ndim > 1:
            audio_numpy = audio_numpy.T
        sf.write(str(output), audio_numpy, SAMPLE_RATE)

        if not keep_model_loaded:
            del model
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        meta = audio_metadata(output)
        return TTSResult(
            file=str(output),
            status="success" if meta["voice_file_exists"] else "failed",
            text_length=len(text),
            estimated_duration_seconds=round(len(text) / 4.2, 3),
            actual_duration_seconds=meta["actual_duration_seconds"],
            sample_rate=meta["sample_rate"],
            channels=meta["channels"],
            file_size=meta["file_size"],
            voice_file_exists=meta["voice_file_exists"],
        )
    except Exception as exc:
        return TTSResult(file=str(output), status="failed", error=str(exc), text_length=len(text))


def release_omnivoice_models() -> None:
    try:
        import torch
    except Exception:
        torch = None
    _MODEL_CACHE.clear()
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()
