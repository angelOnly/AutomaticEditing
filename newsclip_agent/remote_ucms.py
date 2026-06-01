from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .utils import ensure_dir, read_json, relpath, write_json

VIDEO_SUFFIX = ".mp4"


@dataclass(frozen=True)
class RemoteVideoSourceConfig:
    enabled: bool
    api_url: str
    default_record_station: str
    default_page_size: int
    request_timeout_seconds: int
    connect_timeout_seconds: int
    station_count_timeout_seconds: int
    cache_dir: Path
    prefer_quality: str
    record_stations: list[str]
    headers: dict[str, str]


def load_remote_ucms_config(project_config: ProjectConfig) -> RemoteVideoSourceConfig:
    raw = project_config.raw.get("remote_ucms", {})
    cache_dir = Path(str(raw.get("cache_dir", "data/remote_videos")))
    if not cache_dir.is_absolute():
        cache_dir = project_config.root_dir / cache_dir
    return RemoteVideoSourceConfig(
        enabled=bool(raw.get("enabled", False)),
        api_url=str(raw.get("api_url", "https://ucms.ifeng.com/api/ve/list")),
        default_record_station=str(raw.get("default_record_station", "")),
        default_page_size=int(raw.get("default_page_size", 30)),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 20)),
        connect_timeout_seconds=int(raw.get("connect_timeout_seconds", 5)),
        station_count_timeout_seconds=int(raw.get("station_count_timeout_seconds", raw.get("connect_timeout_seconds", 5))),
        cache_dir=cache_dir,
        prefer_quality=str(raw.get("prefer_quality", "high")).lower(),
        record_stations=[str(item) for item in raw.get("record_stations", [])],
        headers={str(key): str(value) for key, value in raw.get("headers", {}).items()},
    )


def public_config(
    cfg: RemoteVideoSourceConfig,
    *,
    station_counts: dict[str, int] | None = None,
    station_count_errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    counts = station_counts or {}
    return {
        "enabled": cfg.enabled,
        "default_record_station": cfg.default_record_station,
        "default_page_size": cfg.default_page_size,
        "prefer_quality": cfg.prefer_quality,
        "record_stations": cfg.record_stations,
        "station_counts": counts,
        "station_count_errors": station_count_errors or {},
        "record_station_options": [
            {
                "value": station,
                "label": f"{station}（{counts[station]}）" if station in counts else station,
                "total": counts.get(station),
            }
            for station in cfg.record_stations
        ],
    }


def list_ucms_videos(
    cfg: RemoteVideoSourceConfig,
    *,
    record_station: str | None = None,
    current: int = 1,
    page_size: int | None = None,
) -> dict[str, Any]:
    size = int(page_size or cfg.default_page_size)
    page = max(1, int(current or 1))
    if not cfg.enabled:
        return _empty_response(page, size, cfg)

    raw = _query_ucms(cfg, record_station or cfg.default_record_station, current=page, page_size=size, timeout=cfg.request_timeout_seconds)
    data = _response_data(raw)
    raw_items = _response_items(data)
    pagination = _response_pagination(data, page, size, len(raw_items))
    return {
        "items": [
            normalize_ucms_item(item, prefer_quality=cfg.prefer_quality)
            for item in raw_items
            if isinstance(item, dict)
        ],
        "pagination": pagination,
        "record_stations": cfg.record_stations,
        "default_record_station": cfg.default_record_station,
    }


def count_ucms_videos(cfg: RemoteVideoSourceConfig, *, record_station: str) -> int:
    if not cfg.enabled:
        return 0
    raw = _query_ucms(cfg, record_station, current=1, page_size=1, timeout=cfg.station_count_timeout_seconds)
    data = _response_data(raw)
    pagination = data.get("pagination") or data.get("page") or {}
    total = pagination.get("total", data.get("total"))
    if total is not None:
        return int(total)
    return len(_response_items(data))


def normalize_ucms_item(item: dict[str, Any], *, prefer_quality: str = "high") -> dict[str, Any]:
    media_high = _loads_json_field(item.get("mediaHigh") or item.get("media_high"))
    media_low = _loads_json_field(item.get("mediaLow") or item.get("media_low"))
    preview = _loads_json_field(item.get("previewImage") or item.get("preview_image"))
    high_url = str(media_high.get("url") or "")
    low_url = str(media_low.get("url") or "")
    selected_url = low_url if prefer_quality == "low" and low_url else high_url or low_url
    preview_paths = []
    if preview.get("path"):
        preview_paths = [part.strip() for part in str(preview.get("path")).split(",") if part.strip()]

    duration = _safe_float(item.get("duration"), 0.0)
    remote_id = str(item.get("id") or item.get("guid") or item.get("name") or "")
    name = str(item.get("name") or remote_id or "remote_video")
    station = str(item.get("recordStation") or item.get("record_station") or "")
    return {
        "source_type": "remote_ucms",
        "remote_id": remote_id,
        "id": item.get("id"),
        "guid": item.get("guid", ""),
        "name": name,
        "display_name": f"{name} - {station}" if station else name,
        "duration": duration,
        "duration_text": _format_duration(duration),
        "create_time": item.get("createTime") or item.get("create_time") or "",
        "record_station": station,
        "tv_station": item.get("tvStation") or item.get("tv_station") or "",
        "mime_type": item.get("mimeType") or item.get("mime_type") or "",
        "aspect": item.get("aspect", ""),
        "media_high_url": high_url,
        "media_low_url": low_url,
        "download_url": selected_url,
        "preview_url": low_url or high_url,
        "preview_images": preview_paths,
        "raw": item,
    }


def download_ucms_video(
    cfg: RemoteVideoSourceConfig,
    remote_video: dict[str, Any],
    *,
    root_dir: Path,
    force: bool = False,
) -> dict[str, Any]:
    normalized = (
        normalize_ucms_item(remote_video, prefer_quality=cfg.prefer_quality)
        if "download_url" not in remote_video
        else remote_video
    )
    url = normalized.get("download_url") or normalized.get("media_high_url") or normalized.get("media_low_url")
    if not url:
        raise RuntimeError("Remote video has no downloadable URL")

    station = _safe_path_part(normalized.get("record_station") or "unknown_station")
    name = _safe_path_part(normalized.get("name") or "remote_video")
    rid = _safe_path_part(normalized.get("remote_id") or normalized.get("id") or normalized.get("guid") or "unknown")
    target_dir = ensure_dir(cfg.cache_dir / station)
    target = target_dir / f"{rid}_{name}{VIDEO_SUFFIX}"
    meta_path = target.with_suffix(".json")

    if target.exists() and target.stat().st_size > 0 and not force:
        return {
            "downloaded": False,
            "path": relpath(target, root_dir),
            "absolute_path": str(target.resolve()),
            "size_mb": round(target.stat().st_size / 1024 / 1024, 2),
            "metadata": read_json(meta_path, {}),
        }

    tmp = target.with_suffix(".downloading")
    _download_file(str(url), tmp, headers=cfg.headers, timeout=cfg.request_timeout_seconds)
    tmp.replace(target)
    meta = {
        "source_type": "remote_ucms",
        "downloaded_at": datetime.now().isoformat(timespec="seconds"),
        "download_url": url,
        "remote_video": normalized,
        "local_path": relpath(target, root_dir),
    }
    write_json(meta_path, meta)
    return {
        "downloaded": True,
        "path": relpath(target, root_dir),
        "absolute_path": str(target.resolve()),
        "size_mb": round(target.stat().st_size / 1024 / 1024, 2),
        "metadata": meta,
    }


def _query_ucms(
    cfg: RemoteVideoSourceConfig,
    record_station: str,
    *,
    current: int,
    page_size: int,
    timeout: int,
) -> dict[str, Any]:
    payload = {
        "body": {
            "query": [
                {"key": "from", "type": "=", "value": "record"},
                {"key": "record_station", "type": "=", "value": record_station},
            ],
            "pagination": {"current": int(current), "pageSize": int(page_size)},
        }
    }
    raw = _post_json(cfg.api_url, payload, headers=cfg.headers, timeout=timeout)
    if not isinstance(raw, dict):
        raise RuntimeError(f"UCMS returned non-object response: {type(raw).__name__}")
    if int(raw.get("code", 0) or 0) not in {0, 200}:
        raise RuntimeError(f"UCMS interface returned an error: {raw.get('message') or raw}")
    return raw


def _response_data(raw: dict[str, Any]) -> dict[str, Any]:
    data = raw.get("data") or {}
    return data if isinstance(data, dict) else {}


def _response_items(data: dict[str, Any]) -> list[Any]:
    items = data.get("list") or data.get("records") or data.get("items") or []
    return items if isinstance(items, list) else []


def _response_pagination(data: dict[str, Any], current: int, page_size: int, item_count: int) -> dict[str, Any]:
    pagination = data.get("pagination") or data.get("page") or {}
    if not isinstance(pagination, dict):
        pagination = {}
    if "total" not in pagination:
        pagination["total"] = data.get("total") or item_count
    pagination.setdefault("current", current)
    pagination.setdefault("pageSize", page_size)
    return pagination


def _empty_response(current: int, page_size: int, cfg: RemoteVideoSourceConfig) -> dict[str, Any]:
    return {
        "items": [],
        "pagination": {"current": current, "pageSize": page_size, "total": 0},
        "record_stations": cfg.record_stations,
        "default_record_station": cfg.default_record_station,
    }


def _post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str], timeout: int) -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return json.loads(resp.read().decode(charset, errors="replace"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"UCMS request failed: {exc}") from exc


def _download_file(url: str, target: Path, *, headers: dict[str, str], timeout: int) -> None:
    ensure_dir(target.parent)
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, target.open("wb") as out:
            for chunk in iter(lambda: resp.read(1024 * 1024), b""):
                out.write(chunk)
    except Exception:
        if target.exists():
            target.unlink(missing_ok=True)
        raise


def _loads_json_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _safe_path_part(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*\s]+', "_", text)
    text = text.strip("._")
    return text[:80] or "unknown"
