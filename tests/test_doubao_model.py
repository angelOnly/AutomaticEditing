from __future__ import annotations

import sys
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from newsclip_agent.config import load_config
from newsclip_agent.llm import OpenAICompatibleClient


def _doubao_llm_config() -> tuple[str, str, str]:
    config = load_config(ROOT / "config.toml")
    llm = config.llm
    provider = llm.get("vision_llm_provider")
    assert provider == "doubao"
    model_id = llm.get("vision_doubao_model_name")
    api_key = llm.get("vision_doubao_api_key")
    base_url = llm.get("vision_doubao_base_url")
    assert model_id and api_key and base_url
    return api_key, base_url, model_id


def test_doubao_multimodal_connectivity(tmp_path: Path) -> None:
    api_key, base_url, model_id = _doubao_llm_config()
    client = OpenAICompatibleClient(api_key=api_key, base_url=base_url)

    img = Image.new("RGB", (100, 100), color="blue")
    img_path = tmp_path / "test_image.jpg"
    img.save(img_path, format="JPEG")

    prompt = """
    Analyze the image and return a JSON object with the following fields:
    - color: the primary color of the image (it should be blue)
    - width: the width of the image
    - description: a short description of the image

    Ensure your response is valid JSON enclosed in a code block.
    """
    input_data = {"test": True}

    result = client.call_json(
        model=model_id,
        prompt=prompt,
        input_data=input_data,
        image_paths=[str(img_path)],
    )

    assert result is not None
    assert result.parsed is not None
    assert isinstance(result.parsed, dict)
    assert "color" in result.parsed
    assert "blue" in result.parsed["color"].lower()
    print("\n--- Doubao Multimodal Connectivity Test Passed! ---")
    print(f"Model used: {result.model}")
    print(f"Latency: {result.latency_ms} ms")
    print(f"Parsed response: {result.parsed}")


def test_doubao_config_integration(tmp_path: Path) -> None:
    from newsclip_agent.pipeline import PipelineRunner, RunOptions
    from unittest.mock import patch

    config = load_config(ROOT / "config.toml")
    api_key, _, model_id = _doubao_llm_config()
    assert config.llm.get("vision_llm_provider") == "doubao"
    assert config.llm.get("text_llm_provider") == "doubao"
    assert config.llm.get("vision_doubao_model_name") == model_id
    assert config.llm.get("text_doubao_model_name") == model_id

    options = RunOptions(
        config=str(ROOT / "config.toml"),
        task_id="test_doubao_run",
        input=str(ROOT / "compare_versions.py"),
        outputs_dir=str(tmp_path / "outputs"),
    )
    
    with patch("newsclip_agent.pipeline.copy_or_link"), patch("newsclip_agent.pipeline.write_json"), patch("newsclip_agent.pipeline.write_text"):
        runner = PipelineRunner(options)
        
    assert runner.llm_vision is not None
    assert runner.llm_text is not None
    assert runner.llm_vision.client.api_key == api_key
    assert runner.llm_text.client.api_key == api_key
    
    img = Image.new("RGB", (100, 100), color="red")
    img_path = tmp_path / "red_test.jpg"
    img.save(img_path, format="JPEG")
    
    model_name = config.llm.get("vision_doubao_model_name")
    result = runner.llm_vision.call_json(
        model=model_name,
        prompt="Analyze the image and return a JSON object with a single field: 'color' which must be the main color.",
        input_data={"test": True},
        image_paths=[str(img_path)],
    )
    assert result is not None
    assert isinstance(result.parsed, dict)
    assert "color" in result.parsed
    assert "red" in result.parsed["color"].lower()
    print("\n--- Doubao Config Integration Test Passed! ---")
    print(f"Model used: {result.model}")
    print(f"Latency: {result.latency_ms} ms")
    print(f"Parsed response: {result.parsed}")
