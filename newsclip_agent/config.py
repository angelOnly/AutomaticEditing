from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import tomllib


@dataclass(frozen=True)
class ProjectConfig:
    root_dir: Path
    raw: dict[str, Any]

    @property
    def llm(self) -> dict[str, Any]:
        return self.raw.get("llm", {})

    @property
    def omnivoice(self) -> dict[str, Any]:
        return self.raw.get("omnivoice", {})

    @property
    def funasr(self) -> dict[str, Any]:
        return self.raw.get("funasr", {})

    @property
    def workflow(self) -> dict[str, Any]:
        return self.raw.get("workflow", {})

    @property
    def short_video(self) -> dict[str, Any]:
        return self.raw.get("short_video", {})

    @property
    def voiceover(self) -> dict[str, Any]:
        return self.raw.get("voiceover", {})

    @property
    def subtitle(self) -> dict[str, Any]:
        return self.raw.get("subtitle", {})

    def resolve_path(self, value: str | None, fallback: str | Path | None = None) -> Path | None:
        if value:
            path = Path(value)
            if not path.is_absolute():
                path = self.root_dir / path
            if path.exists():
                return path
        if fallback is None:
            return None
        fallback_path = Path(fallback)
        if not fallback_path.is_absolute():
            fallback_path = self.root_dir / fallback_path
        return fallback_path


def load_config(config_path: str | Path = "config.toml") -> ProjectConfig:
    config_file = Path(config_path).resolve()
    with config_file.open("rb") as f:
        raw = tomllib.load(f)
    return ProjectConfig(root_dir=config_file.parent, raw=raw)
