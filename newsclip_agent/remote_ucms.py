from __future__ import annotations

import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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
    search_page_size: int
    max_scan_items: int
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
        default_page_size=int(raw.get("default_page_size", 10)),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 20)),
        connect_timeout_seconds=int(raw.get("connect_timeout_seconds", 5)),
        station_count_timeout_seconds=int(raw.get("station_count_timeout_seconds", raw.get("connect_timeout_seconds", 5))),
        search_page_size=int(raw.get("search_page_size", 100)),
        max_scan_items=int(raw.get("max_scan_items", 3000)),
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
                "label": f"{station} ({counts[station]})" if station in counts else station,
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
    keyword: str | None = None,
    sort_order: str = "desc",
    start_time: str | None = None,
    end_time: str | None = None,
) -> dict[str, Any]:
    size = int(page_size or cfg.default_page_size)
    page = max(1, int(current or 1))
    station = record_station or cfg.default_record_station
    order = _normalize_sort_order(sort_order)
    query = str(keyword or "").strip()
    start = str(start_time or "").strip() or None
    end = str(end_time or "").strip() or None
    if not cfg.enabled:
        return _empty_response(page, size, cfg)

    if query or start or end or order in {"asc", "desc"}:
        return _list_ucms_videos_scanned(
            cfg,
            record_station=station,
            current=page,
            page_size=size,
            keyword=query,
            sort_order=order,
            start_time=start,
            end_time=end,
        )

    raw = _query_ucms(cfg, station, current=page, page_size=size, timeout=cfg.request_timeout_seconds)
    data = _response_data(raw)
    raw_items = _response_items(data)
    pagination = _response_pagination(data, page, size, len(raw_items))
    return _list_response(
        raw_items,
        pagination,
        cfg,
        keyword=query,
        sort_order=order,
        scanned=False,
    )


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


def _normalized_remote_video(remote_video: dict[str, Any], *, prefer_quality: str) -> dict[str, Any]:
    """Return a normalized remote video dict, skipping re-normalization when already normalized."""
    if not isinstance(remote_video, dict):
        raise RuntimeError("remote_video must be a dict")
    if "download_url" in remote_video:
        return remote_video
    return normalize_ucms_item(remote_video, prefer_quality=prefer_quality)


def remote_cache_path(
    cfg: RemoteVideoSourceConfig,
    remote_video: dict[str, Any],
    *,
    create_dir: bool = False,
) -> Path:
    """Deterministic local cache path for a remote video.

    Mirrors the target chosen by :func:`download_ucms_video` so callers can locate
    (or pre-check) the cached file without triggering a download. Used by the web
    layer to serve a downloaded copy for 原片 preview when the remote signed URL
    has expired.
    """
    normalized = _normalized_remote_video(remote_video, prefer_quality=cfg.prefer_quality)
    station = _safe_path_part(normalized.get("record_station") or "unknown_station")
    name = _safe_path_part(normalized.get("name") or "remote_video")
    rid = _safe_path_part(normalized.get("remote_id") or normalized.get("id") or normalized.get("guid") or "unknown")
    target_dir = cfg.cache_dir / station
    if create_dir:
        ensure_dir(target_dir)
    return target_dir / f"{rid}_{name}{VIDEO_SUFFIX}"


def download_ucms_video(
    cfg: RemoteVideoSourceConfig,
    remote_video: dict[str, Any],
    *,
    root_dir: Path,
    force: bool = False,
) -> dict[str, Any]:
    normalized = _normalized_remote_video(remote_video, prefer_quality=cfg.prefer_quality)
    url = normalized.get("download_url") or normalized.get("media_high_url") or normalized.get("media_low_url")
    if not url:
        raise RuntimeError("Remote video has no downloadable URL")

    target = remote_cache_path(cfg, normalized, create_dir=True)
    meta_path = target.with_suffix(".json")

    if target.exists() and target.stat().st_size > 0 and not force:
        return {
            "downloaded": False,
            "path": relpath(target, root_dir),
            "absolute_path": str(target.resolve()),
            "size_mb": round(target.stat().st_size / 1024 / 1024, 2),
            "metadata": read_json(meta_path, {}),
        }

    raw_url = str(url)
    safe_url = _quote_url_for_http(raw_url)

    # 临时文件名按进程+随机串唯一化：Web 进程的预取与 runner 子进程会并发下载同一个远程
    # 视频，若共用确定性的 .downloading 路径，先完成者把它 rename 成 .mp4 后，另一个 replace
    # 时就会 FileNotFoundError。唯一化后各写各的临时文件，replace 原子且后者覆盖（内容相同）。
    tmp = target.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex[:8]}.downloading")
    try:
        _download_file(safe_url, tmp, headers=cfg.headers, timeout=cfg.request_timeout_seconds)
        os.replace(tmp, target)
    except (FileNotFoundError, PermissionError):
        # 并发下载者已生成目标文件（POSIX 下临时文件被先完成者移走，Windows 下 replace 撞锁），
        # 目标已就绪则直接复用，不视为失败；否则是真实错误，照常抛出。
        if not (target.exists() and target.stat().st_size > 0):
            raise
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    meta = {
        "source_type": "remote_ucms",
        "downloaded_at": datetime.now().isoformat(timespec="seconds"),
        "download_url": raw_url,
        "download_url_encoded": safe_url,
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


def _list_ucms_videos_scanned(
    cfg: RemoteVideoSourceConfig,
    *,
    record_station: str,
    current: int,
    page_size: int,
    keyword: str,
    sort_order: str,
    start_time: str | None = None,
    end_time: str | None = None,
) -> dict[str, Any]:
    scan_size = max(1, min(int(cfg.search_page_size), 200))
    max_items = max(scan_size, int(cfg.max_scan_items))
    all_items: list[dict[str, Any]] = []
    total = 0
    page = 1
    while len(all_items) < max_items:
        raw = _query_ucms(
            cfg,
            record_station,
            current=page,
            page_size=scan_size,
            timeout=cfg.request_timeout_seconds,
            start_time=start_time,
            end_time=end_time,
        )
        data = _response_data(raw)
        raw_items = [item for item in _response_items(data) if isinstance(item, dict)]
        if page == 1:
            total = int(_response_pagination(data, page, scan_size, len(raw_items)).get("total") or len(raw_items))
        all_items.extend(raw_items)
        if not raw_items or len(all_items) >= total:
            break
        page += 1

    filtered = [item for item in all_items if _matches_keyword(item, keyword)]
    filtered.sort(key=_item_create_time_sort_key, reverse=sort_order == "desc")
    start = max(0, (current - 1) * page_size)
    page_items = filtered[start : start + page_size]
    return _list_response(
        page_items,
        {
            "current": current,
            "pageSize": page_size,
            "total": len(filtered),
            "sourceTotal": total,
            "scanned": min(len(all_items), max_items),
        },
        cfg,
        keyword=keyword,
        sort_order=sort_order,
        scanned=True,
    )


def _list_response(
    raw_items: list[Any],
    pagination: dict[str, Any],
    cfg: RemoteVideoSourceConfig,
    *,
    keyword: str,
    sort_order: str,
    scanned: bool,
) -> dict[str, Any]:
    return {
        "items": [
            normalize_ucms_item(item, prefer_quality=cfg.prefer_quality)
            for item in raw_items
            if isinstance(item, dict)
        ],
        "pagination": pagination,
        "record_stations": cfg.record_stations,
        "default_record_station": cfg.default_record_station,
        "keyword": keyword,
        "sort_order": sort_order,
        "scanned": scanned,
    }


def _query_ucms(
    cfg: RemoteVideoSourceConfig,
    record_station: str,
    *,
    current: int,
    page_size: int,
    timeout: int,
    start_time: str | None = None,
    end_time: str | None = None,
) -> dict[str, Any]:
    query: list[dict[str, Any]] = [
        {"key": "from", "type": "=", "value": "record"},
        {"key": "record_station", "type": "=", "value": record_station},
    ]
    # 时间区间在服务端按 create_time 过滤（闭区间），避免把整段素材全拉回本地再筛。
    # 注意：查询条件的 key 是下划线 create_time，与返回里的驼峰 createTime 不同。
    if start_time:
        query.append({"key": "create_time", "type": ">=", "value": start_time})
    if end_time:
        query.append({"key": "create_time", "type": "<=", "value": end_time})
    payload = {
        "body": {
            "query": query,
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


def _matches_keyword(item: dict[str, Any], keyword: str) -> bool:
    if not keyword:
        return True
    needle = keyword.casefold()
    fields = [
        item.get("name"),
        item.get("id"),
        item.get("guid"),
        item.get("createTime"),
        item.get("recordStation"),
        item.get("tvStation"),
        item.get("preCutMsg"),
    ]
    return any(needle in str(value or "").casefold() for value in fields)


def _item_create_time_sort_key(item: dict[str, Any]) -> tuple[datetime, str]:
    value = str(item.get("createTime") or "")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        parsed = datetime.min
    return parsed, str(item.get("id") or item.get("guid") or item.get("name") or "")


def _normalize_sort_order(value: str | None) -> str:
    order = str(value or "desc").lower()
    return order if order in {"asc", "desc", "source"} else "desc"


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


# 瞬时网络错误：DNS 抖动（gaierror 经 URLError 抛出）、连接重置、读超时、半截响应等。
# 这些重试一次大概率就过去；4xx/未知异常不在此列，照常直接失败。
_TRANSIENT_DOWNLOAD_ERRORS = (
    urllib.error.URLError,
    http.client.IncompleteRead,
    ConnectionError,
    TimeoutError,
)


def _download_file(
    url: str,
    target: Path,
    *,
    headers: dict[str, str],
    timeout: int,
    retries: int = 3,
    backoff_seconds: float = 1.0,
) -> None:
    ensure_dir(target.parent)

    safe_url = _quote_url_for_http(url)
    safe_headers = _ascii_safe_headers(headers)

    req = urllib.request.Request(safe_url, headers=safe_headers, method="GET")
    for attempt in range(1, max(1, retries) + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp, target.open("wb") as out:
                for chunk in iter(lambda: resp.read(1024 * 1024), b""):
                    out.write(chunk)
            return
        except urllib.error.HTTPError as exc:
            # 4xx 是客户端错误（URL/权限），重试无意义；5xx 才值得退避重试。
            if target.exists():
                target.unlink(missing_ok=True)
            if exc.code < 500 or attempt >= retries:
                raise RuntimeError(f"remote video download failed: HTTP {exc.code}, url={_mask_url(safe_url)}") from exc
            last_exc: Exception = exc
        except _TRANSIENT_DOWNLOAD_ERRORS as exc:
            if target.exists():
                target.unlink(missing_ok=True)
            if attempt >= retries:
                raise RuntimeError(f"remote video download failed: {exc}, url={_mask_url(safe_url)}") from exc
            last_exc = exc
        except Exception:
            if target.exists():
                target.unlink(missing_ok=True)
            raise

        wait = backoff_seconds * (2 ** (attempt - 1))
        print(
            f"[remote_ucms] download attempt {attempt}/{retries} failed ({last_exc}); "
            f"retrying in {wait:.0f}s, url={_mask_url(safe_url)}",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(wait)


def _quote_url_for_http(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        raise RuntimeError("download url is empty")

    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        raise RuntimeError(f"unsupported download url scheme: {parts.scheme or 'empty'}")

    netloc = parts.netloc.encode("idna").decode("ascii")
    path = urllib.parse.quote(parts.path, safe="/:%@&=+$,;~!*'()[]-._%")
    query = urllib.parse.quote(parts.query, safe="=&?/:;+,%@~!*'()[]-._")
    fragment = urllib.parse.quote(parts.fragment, safe="=&?/:;+,%@~!*'()[]-._")

    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, fragment))


def _ascii_safe_headers(headers: dict[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in (headers or {}).items():
        k = str(key)
        v = str(value)
        try:
            k.encode("latin-1")
            v.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise RuntimeError(f"remote download header contains non-latin1 characters: {k}") from exc
        safe[k] = v
    return safe


def _mask_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


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

