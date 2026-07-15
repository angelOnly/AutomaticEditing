from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from .utils import ensure_dir, extract_json_object, read_json, write_json


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_CONFIG_PATH = _PROJECT_ROOT / "config.toml"


def _load_project_cover_defaults() -> dict[str, Any]:
    """Load compatibility defaults from the checked-in config file.

    Runtime values still come from the ``ProjectConfig`` passed to the service;
    this import-time snapshot only keeps the public module aliases backwards
    compatible for callers that imported them before cover settings were moved
    into ``config.toml``.
    """
    try:
        with _PROJECT_CONFIG_PATH.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    cover = raw.get("cover_generation", {})
    return dict(cover) if isinstance(cover, dict) else {}


_PROJECT_COVER_DEFAULTS = _load_project_cover_defaults()
# Backwards-compatible aliases. Their values are read from config.toml rather
# than duplicated configuration literals in Python code.
SEEDREAM_SKILL_ID = str(_PROJECT_COVER_DEFAULTS.get("skill_id") or "")
SEEDREAM_SKILL_VERSION = str(_PROJECT_COVER_DEFAULTS.get("skill_version") or "")
PROMPT_SKILL_PATH = Path(str(_PROJECT_COVER_DEFAULTS.get("skill_path") or _PROJECT_ROOT))
if not PROMPT_SKILL_PATH.is_absolute():
    PROMPT_SKILL_PATH = _PROJECT_ROOT / PROMPT_SKILL_PATH


class CoverServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CoverProviderConfig:
    api_key: str
    base_url: str
    chat_model: str
    image_model: str
    skill_id: str
    skill_version: str
    skill_path: Path
    timeout_seconds: int
    allowed_image_hosts: tuple[str, ...]
    max_image_bytes: int
    max_image_pixels: int
    allowed_ratios: tuple[str, ...]
    allowed_sizes: tuple[str, ...]
    allowed_safe_areas: tuple[str, ...]


def provider_config(project_config: Any) -> CoverProviderConfig:
    raw = getattr(project_config, "raw", {}) or {}
    cover = raw.get("cover_generation", {}) or {}
    llm = raw.get("llm", {}) or {}
    defaults = _PROJECT_COVER_DEFAULTS
    root_dir = Path(getattr(project_config, "root_dir", None) or _PROJECT_ROOT)

    def setting(name: str, *fallbacks: Any) -> Any:
        for source in (cover, defaults):
            value = source.get(name) if isinstance(source, dict) else None
            if value is not None and value != "":
                return value
        for value in fallbacks:
            if value is not None and value != "":
                return value
        return None

    skill_path_value = setting("skill_path")
    skill_path = Path(str(skill_path_value)) if skill_path_value else PROMPT_SKILL_PATH
    if not skill_path.is_absolute():
        skill_path = root_dir / skill_path
    allowed_hosts = tuple(str(value) for value in (setting("allowed_image_hosts") or []))
    allowed_ratios = tuple(str(value) for value in (setting("supported_ratios") or []))
    allowed_sizes = tuple(str(value).upper() for value in (setting("supported_sizes") or []))
    allowed_safe_areas = tuple(str(value) for value in (setting("safe_areas") or []))
    api_key = str(
        os.environ.get("ARK_API_KEY")
        or cover.get("api_key")
        or llm.get("text_doubao_api_key")
        or llm.get("vision_doubao_api_key")
        or ""
    ).strip()
    return CoverProviderConfig(
        api_key=api_key,
        base_url=str(setting("base_url", llm.get("text_doubao_base_url")) or "").rstrip("/"),
        chat_model=str(setting("chat_model") or ""),
        image_model=str(setting("image_model") or ""),
        skill_id=str(setting("skill_id") or ""),
        skill_version=str(setting("skill_version") or ""),
        skill_path=skill_path,
        timeout_seconds=max(15, int(setting("timeout_seconds") or 15)),
        allowed_image_hosts=allowed_hosts,
        max_image_bytes=max(1, int(setting("max_image_bytes") or 1)),
        max_image_pixels=max(1, int(setting("max_image_pixels") or 1)),
        allowed_ratios=allowed_ratios,
        allowed_sizes=allowed_sizes,
        allowed_safe_areas=allowed_safe_areas,
    )


def public_provider_config(project_config: Any) -> dict[str, Any]:
    cfg = provider_config(project_config)
    return {
        "configured": bool(cfg.api_key),
        "chat_model": cfg.chat_model,
        "image_model": cfg.image_model,
        "skill_id": cfg.skill_id,
        "skill_version": cfg.skill_version,
        "supported_ratios": sorted(cfg.allowed_ratios),
        "supported_sizes": list(cfg.allowed_sizes),
        "safe_areas": list(cfg.allowed_safe_areas),
    }


class CoverService:
    def __init__(self, project_config: Any, *, client: Any | None = None):
        self.config = provider_config(project_config)
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self.config.api_key:
                raise CoverServiceError("未配置 ARK_API_KEY，无法调用豆包或 Seedream")
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url,
                timeout=self.config.timeout_seconds,
            )
        return self._client

    def _load_seedream_skill(self) -> str:
        try:
            skill_text = self.config.skill_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CoverServiceError(
                f"项目内 Seedream Skill 不存在：{self.config.skill_path}，请确认代码工程已完整部署到 Docker 的 /workspace"
            ) from exc
        if not skill_text:
            raise CoverServiceError("项目内 Seedream Skill 为空")
        return skill_text

    def create_seedream_prompt(
        self,
        *,
        title: str,
        summary: str = "",
        requirements: str = "",
        aspect_ratio: str | None = None,
        size: str | None = None,
        safe_area: str | None = None,
    ) -> dict[str, Any]:
        ratio = aspect_ratio if aspect_ratio in self.config.allowed_ratios else self.config.allowed_ratios[0]
        normalized_size = (size or "").upper()
        image_size = normalized_size if normalized_size in self.config.allowed_sizes else self.config.allowed_sizes[0]
        safe = safe_area if safe_area in self.config.allowed_safe_areas else self.config.allowed_safe_areas[0]
        skill_text = self._load_seedream_skill()
        skill_hash = hashlib.sha256(skill_text.encode("utf-8")).hexdigest()[:16]
        user_payload = {
            "title": _clean_text(title, 240),
            "summary": _clean_text(summary, 2000),
            "requirements": _clean_text(requirements, 1200),
            "aspect_ratio": ratio,
            "size": image_size,
            "safe_area": safe,
        }
        response = self.client.chat.completions.create(
            model=self.config.chat_model,
            messages=[
                {"role": "system", "content": skill_text},
                {
                    "role": "user",
                    "content": "请根据下面的新闻资料生成 Seedream 提示词，只返回 JSON：\n"
                    + json.dumps(user_payload, ensure_ascii=False),
                },
            ],
            temperature=0.35,
            max_tokens=1200,
        )
        raw_text = response.choices[0].message.content or ""
        try:
            parsed = extract_json_object(raw_text)
        except Exception as exc:
            raise CoverServiceError(f"豆包返回的生图提示词不是有效 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise CoverServiceError("豆包返回的生图提示词格式不正确")
        prompt = _clean_text(str(parsed.get("prompt") or ""), 1800)
        if not prompt:
            raise CoverServiceError("豆包没有返回可用的 Seedream 提示词")
        if not re.search(r"(?:不出现|不要|无).*(?:文字|Logo|水印)", prompt, re.IGNORECASE):
            prompt += "，画面中不出现文字、字母、数字、Logo、水印"
        return {
            "prompt": prompt,
            "aspect_ratio": parsed.get("aspect_ratio") if parsed.get("aspect_ratio") in self.config.allowed_ratios else ratio,
            "size": str(parsed.get("size") or image_size).upper() if str(parsed.get("size") or image_size).upper() in self.config.allowed_sizes else image_size,
            "safe_area": parsed.get("safe_area") if parsed.get("safe_area") in self.config.allowed_safe_areas else safe,
            "style_tags": [str(x)[:60] for x in (parsed.get("style_tags") or [])[:8]],
            "skill_hash": skill_hash,
            "skill_id": self.config.skill_id,
            "skill_version": self.config.skill_version,
            "chat_model": self.config.chat_model,
            "raw_text": raw_text,
            "input": user_payload,
        }

    def generate_images(
        self,
        *,
        task_dir: Path,
        prompt_doc: dict[str, Any],
        max_images: int = 1,
        watermark: bool = False,
        reference_image: Path | None = None,
        edit_instruction: str = "",
    ) -> dict[str, Any]:
        count = max(1, min(int(max_images or 1), 4))
        prompt = _clean_text(str(prompt_doc.get("prompt") or ""), 1800)
        if edit_instruction:
            prompt = f"{prompt}。{_clean_text(edit_instruction, 600)}"
        if not prompt:
            raise CoverServiceError("Seedream prompt 不能为空")
        extra_body: dict[str, Any] = {"watermark": bool(watermark)}
        if count > 1:
            extra_body.update(
                {
                    "sequential_image_generation": "auto",
                    "sequential_image_generation_options": {"max_images": count},
                }
            )
        if reference_image is not None:
            extra_body["image"] = _image_data_url(reference_image, max_bytes=self.config.max_image_bytes)

        response = self.client.images.generate(
            model=self.config.image_model,
            prompt=prompt,
            size=str(prompt_doc.get("size") or self.config.allowed_sizes[0]),
            response_format="url",
            extra_body=extra_body,
        )
        cover_id = f"cover_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        cover_dir = ensure_dir(task_dir / "edit" / "manual_editor" / "covers" / cover_id)
        files: list[dict[str, Any]] = []
        for index, item in enumerate(getattr(response, "data", []) or [], start=1):
            raw = _download_or_decode_image(
                url=getattr(item, "url", None),
                b64_json=getattr(item, "b64_json", None),
                timeout=self.config.timeout_seconds,
                allowed_hosts=self.config.allowed_image_hosts,
                max_bytes=self.config.max_image_bytes,
            )
            suffix, normalized = _normalize_image_bytes(raw, max_pixels=self.config.max_image_pixels)
            file_path = cover_dir / f"candidate_{index:02d}{suffix}"
            file_path.write_bytes(normalized)
            files.append(
                {
                    "index": index,
                    "file": str(file_path.relative_to(task_dir)).replace("\\", "/"),
                    "width": _image_dimensions(normalized)[0],
                    "height": _image_dimensions(normalized)[1],
                }
            )
        if not files:
            raise CoverServiceError("Seedream 未返回图片")
        metadata = {
            "cover_id": cover_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "chat_model": prompt_doc.get("chat_model") or self.config.chat_model,
            "image_model": self.config.image_model,
            "skill_hash": prompt_doc.get("skill_hash", ""),
            "skill_id": prompt_doc.get("skill_id", self.config.skill_id),
            "skill_version": prompt_doc.get("skill_version", self.config.skill_version),
            "prompt": prompt,
            "size": prompt_doc.get("size") or self.config.allowed_sizes[0],
            "aspect_ratio": prompt_doc.get("aspect_ratio") or self.config.allowed_ratios[0],
            "safe_area": prompt_doc.get("safe_area") or self.config.allowed_safe_areas[0],
            "watermark": bool(watermark),
            "reference_image": str(reference_image) if reference_image else "",
            "files": files,
            "usage": _model_dump(getattr(response, "usage", None)),
        }
        write_json(cover_dir / "generation.json", metadata)
        return metadata


def compose_cover(
    *,
    source_path: Path,
    output_path: Path,
    width: int,
    height: int,
    crop: dict[str, Any] | None = None,
    text_layers: list[dict[str, Any]] | None = None,
    enhance: str = "standard",
    fonts_dir: Path | None = None,
) -> dict[str, Any]:
    width = max(320, min(int(width), 7680))
    height = max(320, min(int(height), 7680))
    with Image.open(source_path) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
    image = _apply_crop(image, crop or {})
    image = ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS)
    if enhance == "standard":
        image = ImageOps.autocontrast(image, cutoff=0.4)
        image = ImageEnhance.Contrast(image).enhance(1.04)
        image = ImageEnhance.Color(image).enhance(1.03)
        image = image.filter(ImageFilter.UnsharpMask(radius=1.4, percent=115, threshold=3))
    elif enhance == "light":
        image = image.filter(ImageFilter.UnsharpMask(radius=1.0, percent=75, threshold=4))

    draw = ImageDraw.Draw(image, "RGBA")
    for layer in text_layers or []:
        _draw_text_layer(draw, image.size, layer, fonts_dir)

    ensure_dir(output_path.parent)
    suffix = output_path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        image.save(output_path, format="JPEG", quality=95, optimize=True, subsampling=0)
    else:
        image.save(output_path, format="PNG", optimize=True)
    return {
        "file": str(output_path),
        "width": width,
        "height": height,
        "enhance": enhance,
        "text_layer_count": len(text_layers or []),
    }


def validate_font_file(path: Path) -> dict[str, Any]:
    if path.suffix.lower() not in {".ttf", ".otf"}:
        raise CoverServiceError("仅支持 TTF/OTF 字体")
    if not path.exists() or path.stat().st_size > 20 * 1024 * 1024:
        raise CoverServiceError("字体文件不存在或超过 20MB")
    try:
        font = ImageFont.truetype(str(path), size=32)
        bbox = font.getbbox("凤凰智剪 Abc 123")
    except Exception as exc:
        raise CoverServiceError(f"字体文件无效：{exc}") from exc
    return {"name": path.name, "size_bytes": path.stat().st_size, "sample_bbox": list(bbox)}


def _apply_crop(image: Image.Image, crop: dict[str, Any]) -> Image.Image:
    x = _bounded_float(crop.get("x"), 0.0)
    y = _bounded_float(crop.get("y"), 0.0)
    w = _bounded_float(crop.get("w"), 1.0)
    h = _bounded_float(crop.get("h"), 1.0)
    if w <= 0.001 or h <= 0.001:
        raise CoverServiceError("裁剪区域无效")
    x2 = min(1.0, x + w)
    y2 = min(1.0, y + h)
    if x2 <= x or y2 <= y:
        raise CoverServiceError("裁剪区域超出图片")
    left = int(round(x * image.width))
    top = int(round(y * image.height))
    right = max(left + 1, int(round(x2 * image.width)))
    bottom = max(top + 1, int(round(y2 * image.height)))
    return image.crop((left, top, right, bottom))


def _draw_text_layer(
    draw: ImageDraw.ImageDraw,
    canvas: tuple[int, int],
    layer: dict[str, Any],
    fonts_dir: Path | None,
) -> None:
    text = _clean_text(str(layer.get("text") or ""), 500)
    if not text:
        return
    width, height = canvas
    font_size = max(12, min(int(layer.get("font_size") or round(height * 0.07)), max(width, height)))
    font = _load_font(str(layer.get("font") or ""), font_size, fonts_dir)
    max_width = max(40, int(_bounded_float(layer.get("max_width"), 0.8) * width))
    lines = _wrap_text(draw, text, font, max_width)
    rendered = "\n".join(lines)
    x = int(_bounded_float(layer.get("x"), 0.08) * width)
    y = int(_bounded_float(layer.get("y"), 0.65) * height)
    spacing = max(2, int(font_size * float(layer.get("line_spacing") or 0.25)))
    stroke_width = max(0, min(int(layer.get("stroke_width") or 2), 20))
    fill = _parse_color(layer.get("color"), (255, 255, 255, 255))
    stroke_fill = _parse_color(layer.get("stroke_color"), (0, 0, 0, 220))
    bbox = draw.multiline_textbbox((x, y), rendered, font=font, spacing=spacing, stroke_width=stroke_width)
    background = layer.get("background")
    if background:
        pad = max(4, int(font_size * 0.18))
        bg = _parse_color(background, (0, 0, 0, 128))
        draw.rounded_rectangle(
            (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad),
            radius=max(2, pad // 2),
            fill=bg,
        )
    draw.multiline_text(
        (x, y),
        rendered,
        font=font,
        fill=fill,
        spacing=spacing,
        align=str(layer.get("align") or "left"),
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
    )


def _load_font(name: str, size: int, fonts_dir: Path | None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates: list[Path] = []
    if fonts_dir and name:
        safe_name = Path(name).name
        candidates.append(fonts_dir / safe_name)
    if os.name == "nt":
        windir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        candidates.extend([windir / "msyh.ttc", windir / "msyhbd.ttc", windir / "simhei.ttf"])
    else:
        candidates.extend(
            [
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            ]
        )
    for candidate in candidates:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: Any, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and draw.textlength(candidate, font=font) > max_width:
                lines.append(current)
                current = char
            else:
                current = candidate
        lines.append(current)
    return [line for line in lines if line] or [""]


def _download_or_decode_image(
    *,
    url: str | None,
    b64_json: str | None,
    timeout: int,
    allowed_hosts: tuple[str, ...],
    max_bytes: int,
) -> bytes:
    if b64_json:
        try:
            raw = base64.b64decode(b64_json, validate=True)
        except Exception as exc:
            raise CoverServiceError("Seedream 返回了无效的 Base64 图片") from exc
        if len(raw) > max_bytes:
            raise CoverServiceError("Seedream 图片超过大小限制")
        return raw
    if not url:
        raise CoverServiceError("Seedream 图片没有 URL 或 Base64 数据")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in allowed_hosts):
        raise CoverServiceError("Seedream 返回了不受信任的图片地址")
    response = requests.get(url, timeout=timeout, stream=True)
    response.raise_for_status()
    raw = bytearray()
    for chunk in response.iter_content(1024 * 1024):
        raw.extend(chunk)
        if len(raw) > max_bytes:
            raise CoverServiceError("Seedream 图片超过大小限制")
    return bytes(raw)


def _normalize_image_bytes(raw: bytes, *, max_pixels: int) -> tuple[str, bytes]:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            Image.MAX_IMAGE_PIXELS = max_pixels
            image.load()
            if image.width * image.height > max_pixels:
                raise CoverServiceError("Seedream 图片像素过大")
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            out = io.BytesIO()
            normalized.save(out, format="JPEG", quality=96, optimize=True, subsampling=0)
            return ".jpg", out.getvalue()
    except CoverServiceError:
        raise
    except Exception as exc:
        raise CoverServiceError(f"Seedream 返回的不是有效图片：{exc}") from exc


def _image_data_url(path: Path, *, max_bytes: int) -> str:
    if not path.exists() or not path.is_file():
        raise CoverServiceError("参考图片不存在")
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise CoverServiceError("参考图片超过大小限制")
    suffix = path.suffix.lower()
    mime = {
        ".png": "image/png",
        ".webp": "image/webp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(suffix)
    if not mime:
        raise CoverServiceError("参考图片格式不支持")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _image_dimensions(raw: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(raw)) as image:
        return image.size


def _model_dump(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    return {}


def _clean_text(value: str, limit: int) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value or "").strip()[:limit]


def _bounded_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(number, 1.0))


def _parse_color(value: Any, default: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    if isinstance(value, (list, tuple)) and len(value) in {3, 4}:
        parts = [max(0, min(int(x), 255)) for x in value]
        if len(parts) == 3:
            parts.append(255)
        return tuple(parts)  # type: ignore[return-value]
    text = str(value or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", text):
        parts = [int(text[i : i + 2], 16) for i in range(0, len(text), 2)]
        if len(parts) == 3:
            parts.append(255)
        return tuple(parts)  # type: ignore[return-value]
    return default


def load_cover_metadata(task_dir: Path, cover_id: str) -> dict[str, Any]:
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", cover_id)
    if safe_id != cover_id:
        raise CoverServiceError("封面 ID 无效")
    return read_json(task_dir / "edit" / "manual_editor" / "covers" / cover_id / "generation.json", {})
