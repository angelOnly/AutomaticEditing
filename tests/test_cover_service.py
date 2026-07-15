from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from newsclip_agent.cover_service import (
    PROMPT_SKILL_PATH,
    SEEDREAM_SKILL_ID,
    SEEDREAM_SKILL_VERSION,
    CoverService,
    compose_cover,
    provider_config,
)


class _ChatCompletions:
    def __init__(self) -> None:
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        content = (
            '{"prompt":"纪实新闻摄影，主体位于画面右侧，左侧留出干净标题区，冷暖对比光，'
            '画面中不出现文字、字母、数字、Logo、水印","aspect_ratio":"16:9",'
            '"size":"2K","safe_area":"left","style_tags":["纪实","新闻"]}'
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class _Images:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.kwargs = None

    def generate(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            data=[SimpleNamespace(url=None, b64_json=base64.b64encode(self.payload).decode("ascii"))],
            usage=SimpleNamespace(model_dump=lambda: {"generated_images": 1}),
        )


class _FakeClient:
    def __init__(self, payload: bytes) -> None:
        self.chat_completions = _ChatCompletions()
        self.chat = SimpleNamespace(completions=self.chat_completions)
        self.image_api = _Images(payload)
        self.images = self.image_api


def _image_bytes(size: tuple[int, int] = (320, 180)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, (30, 80, 130)).save(output, format="PNG")
    return output.getvalue()


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        raw={
            "cover_generation": {
                "api_key": "test-key",
                "chat_model": "chat-endpoint",
                "image_model": "image-endpoint",
            }
        }
    )


def test_provider_config_uses_cover_endpoint_ids() -> None:
    cfg = provider_config(_config())
    assert cfg.chat_model == "chat-endpoint"
    assert cfg.image_model == "image-endpoint"
    assert cfg.api_key == "test-key"


def test_skill_is_injected_as_system_message() -> None:
    client = _FakeClient(_image_bytes())
    service = CoverService(_config(), client=client)
    result = service.create_seedream_prompt(title="测试新闻", summary="事实摘要")

    messages = client.chat_completions.kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert "Seedream" in messages[0]["content"]
    assert f"skill_version: {SEEDREAM_SKILL_VERSION}" in messages[0]["content"]
    assert result["prompt"].endswith("Logo、水印")
    assert result["chat_model"] == "chat-endpoint"
    assert result["skill_id"] == SEEDREAM_SKILL_ID
    assert result["skill_version"] == SEEDREAM_SKILL_VERSION


def test_seedream_skill_is_checked_in_with_the_application_package() -> None:
    package_root = Path(__file__).resolve().parents[1] / "newsclip_agent"
    assert PROMPT_SKILL_PATH.is_file()
    assert PROMPT_SKILL_PATH.resolve().is_relative_to(package_root.resolve())
    assert "skill_version:" in PROMPT_SKILL_PATH.read_text(encoding="utf-8")


def test_generate_images_is_task_scoped_and_persists_metadata(tmp_path: Path) -> None:
    client = _FakeClient(_image_bytes())
    service = CoverService(_config(), client=client)
    result = service.generate_images(
        task_dir=tmp_path,
        prompt_doc={"prompt": "新闻封面，无文字", "size": "2K", "aspect_ratio": "16:9"},
        max_images=1,
    )

    relative = Path(result["files"][0]["file"])
    assert relative.parts[:4] == ("edit", "manual_editor", "covers", result["cover_id"])
    assert (tmp_path / relative).exists()
    assert (tmp_path / relative.parent / "generation.json").exists()
    assert client.image_api.kwargs["model"] == "image-endpoint"


def test_reference_image_is_passed_for_ai_restore(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(_image_bytes())
    client = _FakeClient(_image_bytes())
    service = CoverService(_config(), client=client)
    service.generate_images(
        task_dir=tmp_path,
        prompt_doc={"prompt": "忠实高清修复", "size": "4K"},
        reference_image=source,
    )
    assert client.image_api.kwargs["extra_body"]["image"].startswith("data:image/png;base64,")


def test_webp_reference_image_keeps_its_mime_type(tmp_path: Path) -> None:
    source = tmp_path / "source.webp"
    Image.new("RGB", (64, 64), (20, 40, 60)).save(source, format="WEBP")
    client = _FakeClient(_image_bytes())
    service = CoverService(_config(), client=client)
    service.generate_images(
        task_dir=tmp_path,
        prompt_doc={"prompt": "忠实高清修复", "size": "2K"},
        reference_image=source,
    )
    assert client.image_api.kwargs["extra_body"]["image"].startswith("data:image/webp;base64,")


def test_compose_cover_crops_resizes_and_draws_text(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(_image_bytes((400, 300)))
    output = tmp_path / "final.png"
    result = compose_cover(
        source_path=source,
        output_path=output,
        width=640,
        height=360,
        crop={"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8},
        text_layers=[{"text": "凤凰智剪", "x": 0.1, "y": 0.7, "font_size": 36}],
        enhance="standard",
    )
    with Image.open(output) as image:
        assert image.size == (640, 360)
    assert result["text_layer_count"] == 1
