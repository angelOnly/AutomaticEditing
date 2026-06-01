from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pytest


os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "outputs" / "omnivoice_tts_smoke" / "omnivoice_smoke.wav"
DEFAULT_TEXT = (
    "这是一段 OmniVoice 语音合成测试。"
    "如果你能听到这句话，说明参考音频克隆和文本转语音流程已经正常完成。"
)


def _load_project_config():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from newsclip_agent.config import load_config

    return load_config(ROOT / "config.toml")


def generate_omnivoice_sample(
    text: str = DEFAULT_TEXT,
    output_path: Path = DEFAULT_OUTPUT,
    reference_audio: Path | None = None,
    reference_text: str | None = None,
    model_path: Path | None = None,
) -> Path:
    config = _load_project_config()
    from newsclip_agent.tts_omnivoice import generate_omnivoice_audio

    model_path = model_path or config.resolve_path(
        config.omnivoice.get("model_path"),
        "models/OmniVoice",
    )
    reference_audio = reference_audio or config.resolve_path(
        config.omnivoice.get("reference_audio"),
        "tts_ref/664925840_0_13s.mp3",
    )
    reference_text = reference_text if reference_text is not None else config.omnivoice.get("reference_text", "")

    if model_path is None or not model_path.exists():
        raise FileNotFoundError(f"OmniVoice model path not found: {model_path}")
    if reference_audio is None or not reference_audio.exists():
        raise FileNotFoundError(f"Reference audio not found: {reference_audio}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    result = generate_omnivoice_audio(
        text=text,
        output_path=output_path,
        model_path=model_path,
        reference_audio=reference_audio,
        reference_text=reference_text,
    )
    if result.status != "success":
        raise RuntimeError(result.error or "OmniVoice generation failed")

    return output_path


@pytest.mark.skipif(
    os.environ.get("RUN_OMNIVOICE_TTS_TEST") != "1",
    reason="Set RUN_OMNIVOICE_TTS_TEST=1 to run the heavy OmniVoice smoke test.",
)
def test_omnivoice_generates_audio_with_reference(tmp_path: Path) -> None:
    output_path = tmp_path / "omnivoice_smoke.wav"

    generated = generate_omnivoice_sample(output_path=output_path)

    assert generated.exists()
    assert generated.stat().st_size > 1024
    from newsclip_agent.tts_omnivoice import audio_metadata

    meta = audio_metadata(generated)
    assert meta["actual_duration_seconds"] > 0
    assert meta["sample_rate"] == 24000


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a small OmniVoice TTS smoke-test wav.")
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--ref-audio", type=Path, default=None)
    parser.add_argument("--ref-text", default=None)
    parser.add_argument("--model-path", type=Path, default=None)
    args = parser.parse_args()

    output_path = generate_omnivoice_sample(
        text=args.text,
        output_path=args.output,
        reference_audio=args.ref_audio,
        reference_text=args.ref_text,
        model_path=args.model_path,
    )
    print(f"Generated: {output_path}")
    print(f"Size: {output_path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
