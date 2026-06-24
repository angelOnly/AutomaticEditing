"""图文解说（commentary）：用重组成片的逐段语音文本生成新闻图文解说，并把成片单点嵌入正文。

设计要点：
- 纯文本链路（默认走 DeepSeek），不做多模态识图，输入全部来自已有产物。
- 流水线步骤 step_reassembly_commentary 与 web 接口共用本模块，逻辑只此一份。
- 产物落在 edit/reassembly_commentary/v{N}/，每个 rid 一个 commentary_{rid}.json + commentary_outputs.json 索引。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .llm import OpenAICompatibleClient
from .utils import ensure_dir, now_iso, read_json, stable_hash, write_json

PROMPT_DIR = Path(__file__).resolve().parent / "prompt_texts"
COMMENTARY_BASE = "edit/reassembly_commentary"
COMMENTARY_VERSION = "commentary_v1"


class CommentaryError(RuntimeError):
    """图文解说生成相关的可预期错误（缺配置、缺素材等）。"""


def _load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8").strip() + "\n"


SCRIPT_PROMPT = _load_prompt("commentary_script.txt")
LAYOUT_PROMPT = _load_prompt("commentary_layout.txt")


# --------------------------------------------------------------------------- #
# 产物定位与读取
# --------------------------------------------------------------------------- #
def _latest_glob(task_dir: Path, patterns: list[str]) -> Path | None:
    candidates: list[Path] = []
    for pat in patterns:
        candidates.extend(p for p in task_dir.glob(pat) if p.is_file())
    if not candidates:
        return None

    def ver_key(p: Path) -> tuple[int, float]:
        m = re.search(r"/v(\d+)/", p.as_posix())
        return (int(m.group(1)) if m else 0, p.stat().st_mtime)

    return max(candidates, key=ver_key)


def _load_step_doc(
    task_dir: Path,
    manifest: dict[str, Any],
    step: str,
    glob_patterns: list[str],
) -> dict[str, Any] | None:
    """优先按 manifest 记录的最新版本路径读取，缺失时回退按版本号 glob。"""
    out = (manifest.get("steps", {}).get(step, {}) or {}).get("output")
    if out:
        p = task_dir / out
        if p.exists():
            data = read_json(p, None)
            if isinstance(data, dict):
                return data
    best = _latest_glob(task_dir, glob_patterns)
    if best:
        data = read_json(best, None)
        if isinstance(data, dict):
            return data
    return None


def _load_render_outputs(task_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    return _load_step_doc(
        task_dir,
        manifest,
        "reassembly_render",
        ["edit/reassembly_drafts/v*/reassembly_render_outputs.json"],
    ) or {}


def _first_list(doc: dict[str, Any], keys: list[str]) -> list[dict[str, Any]]:
    for key in keys:
        value = doc.get(key)
        if isinstance(value, list) and value:
            return [c for c in value if isinstance(c, dict)]
    return []


def _build_speech_map(task_dir: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """clip_id -> 候选片段（含 speech/asr_text/summary）。asr_event_candidate 优先，candidate_filter 补缺。"""
    out: dict[str, dict[str, Any]] = {}
    sources = [
        ("asr_event_candidate", ["agents/asr_event_candidate/v*/asr_event_candidate.json"],
         ["candidate_clip_pool", "candidate_clips"]),
        ("candidate_filter", ["agents/candidate_filter/v*/candidate_filter.json"],
         ["candidate_clip_pool_filtered", "filtered_candidate_clips"]),
    ]
    for step, patterns, keys in sources:
        doc = _load_step_doc(task_dir, manifest, step, patterns) or {}
        for clip in _first_list(doc, keys):
            cid = str(clip.get("clip_id") or "").strip()
            if cid and cid not in out:
                out[cid] = clip
    return out


def renderable_rids(task_dir: str | Path, manifest: dict[str, Any] | None = None) -> list[str]:
    task_dir = Path(task_dir)
    if manifest is None:
        manifest = read_json(task_dir / "manifest.json", {})
    render = _load_render_outputs(task_dir, manifest)
    rids: list[str] = []
    for item in render.get("outputs", []) or []:
        rid = item.get("reassembly_id")
        if rid and item.get("file") and rid not in rids:
            rids.append(str(rid))
    return rids


def _truncate(text: Any, max_chars: int) -> str:
    text = str(text or "").strip()
    if max_chars and len(text) > max_chars:
        return text[:max_chars].rstrip() + "…"
    return text


def build_commentary_material(
    task_dir: str | Path,
    reassembly_id: str | None = None,
) -> dict[str, Any] | None:
    """组装喂给提示词的素材：整体理解 + 按播放顺序排好的分镜（含逐段 speech）。

    无可渲染成片 / 找不到该 rid / 缺剪辑计划时返回 None。
    """
    task_dir = Path(task_dir)
    manifest = read_json(task_dir / "manifest.json", {})

    cut_plan = _load_step_doc(
        task_dir, manifest, "reassembly_cut_plan",
        ["edit/reassembly_cut_plan/v*/reassembly_cut_plan.json"],
    )
    render = _load_render_outputs(task_dir, manifest)
    if not cut_plan or not render:
        return None

    rid_to_file = {
        str(o.get("reassembly_id")): o.get("file")
        for o in render.get("outputs", []) or []
        if o.get("reassembly_id") and o.get("file")
    }
    if not rid_to_file:
        return None
    target_rid = reassembly_id or next(iter(rid_to_file))
    if target_rid not in rid_to_file:
        return None

    video_obj = next(
        (v for v in cut_plan.get("output_videos", []) or []
         if str(v.get("reassembly_id")) == target_rid),
        None,
    )
    if not video_obj:
        return None

    speech_map = _build_speech_map(task_dir, manifest)
    understanding = _load_step_doc(
        task_dir, manifest, "video_understanding",
        ["agents/video_understanding/v*/video_analysis.json"],
    ) or {}

    clips: list[dict[str, Any]] = []
    for index, clip in enumerate(video_obj.get("clips", []) or [], start=1):
        scid = str(clip.get("source_clip_id") or "")
        pool = speech_map.get(scid, {})
        speech = (
            pool.get("speech")
            or pool.get("asr_text")
            or clip.get("selection_reason")
            or pool.get("summary")
            or ""
        )
        summary = pool.get("summary") or clip.get("selection_reason") or pool.get("event_summary") or ""
        clips.append({
            "order": index,
            "role": clip.get("role") or pool.get("event_type") or pool.get("type") or "",
            "speech": str(speech).strip(),
            "summary": str(summary).strip(),
            "duration": round(float(clip.get("duration_seconds") or 0), 1),
        })

    material = {
        "reassembly_id": target_rid,
        "video_file": rid_to_file[target_rid],
        "title_hint": str(video_obj.get("title") or "").strip(),
        "topic": str(understanding.get("main_topic") or understanding.get("topic") or "").strip(),
        "summary": str(understanding.get("summary") or understanding.get("understanding") or "").strip(),
        "key_facts": understanding.get("key_facts") or understanding.get("facts") or [],
        "clips": clips,
    }
    material["content_hash"] = stable_hash({
        "clips": clips,
        "topic": material["topic"],
        "summary": material["summary"],
        "video_file": material["video_file"],
    })
    return material


# --------------------------------------------------------------------------- #
# LLM 调用与文案/排版生成
# --------------------------------------------------------------------------- #
def _commentary_cfg(config: Any) -> dict[str, Any]:
    return (config.raw.get("commentary", {}) if hasattr(config, "raw") else {}) or {}


def _make_commentary_client(config: Any) -> tuple[OpenAICompatibleClient, str, list[str], str]:
    llm = config.llm if hasattr(config, "llm") else config.raw.get("llm", {})
    cmt = _commentary_cfg(config)

    def resolve(provider: str) -> tuple[str | None, str | None]:
        return llm.get(f"text_{provider}_api_key"), llm.get(f"text_{provider}_base_url")

    provider = cmt.get("provider") or llm.get("text_llm_provider", "openai")
    api_key, base_url = resolve(provider)
    if not api_key or not base_url:
        # 回退到全局文本 provider
        provider = llm.get("text_llm_provider", "openai")
        api_key, base_url = resolve(provider)
    if not api_key or not base_url:
        raise CommentaryError("缺少文本大模型配置（api_key/base_url），无法生成图文解说")

    client = OpenAICompatibleClient(
        api_key=api_key,
        base_url=base_url,
        timeout=int(llm.get("llm_text_timeout", 180) or 180),
        max_retries=int(llm.get("llm_max_retries", 3) or 3),
    )
    model = (
        cmt.get("model")
        or llm.get(f"text_{provider}_model_name")
        or llm.get("text_llm_model_name")
    )
    if not model:
        raise CommentaryError(f"缺少 text_{provider}_model_name 配置")
    fallbacks = [m for m in (llm.get(f"text_{provider}_fallback_models", []) or []) if m and m != model]
    return client, str(model), fallbacks, str(provider)


def _build_script_input(material: dict[str, Any], cmt: dict[str, Any]) -> dict[str, Any]:
    max_speech = int(cmt.get("max_speech_chars_per_clip", 300) or 300)
    segments = [
        {
            "order": clip["order"],
            "role": clip["role"],
            "speech": _truncate(clip["speech"], max_speech),
            "summary": _truncate(clip["summary"], 200),
        }
        for clip in material["clips"]
    ]
    return {
        "视频理解": {
            "主题": material["topic"],
            "摘要": material["summary"],
            "关键事实": material["key_facts"],
        },
        "重组视频片段": segments,
    }


def _video_speech_blob(material: dict[str, Any], cmt: dict[str, Any]) -> str:
    parts = [clip.get("speech") or clip.get("summary") or "" for clip in material["clips"]]
    blob = "　".join(p.strip() for p in parts if p and p.strip())
    return _truncate(blob, int(cmt.get("max_total_input_chars", 6000) or 6000))


def _coerce_dict(parsed: Any) -> dict[str, Any]:
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        return next((x for x in parsed if isinstance(x, dict)), {})
    return {}


def _normalize_script(parsed: Any, cmt: dict[str, Any], material: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    warnings: list[str] = []
    data = _coerce_dict(parsed)

    raw_paragraphs = data.get("paragraphs") or data.get("body") or []
    if isinstance(raw_paragraphs, str):
        raw_paragraphs = [p for p in re.split(r"\n{2,}", raw_paragraphs) if p.strip()]
    paragraphs = [str(p).strip() for p in (raw_paragraphs if isinstance(raw_paragraphs, list) else []) if str(p).strip()]

    max_p = int(cmt.get("max_paragraphs", 5) or 5)
    if max_p > 0 and len(paragraphs) > max_p:
        paragraphs = paragraphs[:max_p]
    min_p = int(cmt.get("min_paragraphs", 3) or 3)
    if len(paragraphs) < min_p:
        warnings.append(f"paragraphs_below_min:{len(paragraphs)}")
    if not paragraphs:
        warnings.append("no_paragraphs")

    title = str(data.get("title") or "").replace("\n", " ").strip()
    if not title:
        title = (material.get("title_hint") or material.get("topic") or "图文解说").strip()
        warnings.append("title_fallback")
    title_max = int(cmt.get("title_max_chars", 22) or 22)
    if title_max > 0 and len(title) > title_max + 8:
        title = title[:title_max].rstrip()
        warnings.append("title_truncated")

    return title, paragraphs, warnings


def _normalize_layout(parsed: Any, paragraph_count: int) -> tuple[int, str]:
    data = _coerce_dict(parsed)
    raw = data.get("insert_after_index")
    if raw is None:
        raw = data.get("index")
    try:
        idx = int(raw)
    except (TypeError, ValueError):
        idx = 0
    idx = max(0, min(idx, max(0, paragraph_count - 1)))
    reason = str(data.get("reason") or "").strip()
    return idx, reason


def _assemble_markdown(title: str, paragraphs: list[str], insert_idx: int, video_name: str) -> str:
    video_tag = f'<video controls src="{video_name}"></video>'
    parts: list[str] = []
    if title:
        parts.append(f"# {title}")
    if not paragraphs:
        parts.append(video_tag)
        return "\n\n".join(parts).strip() + "\n"
    for i, paragraph in enumerate(paragraphs):
        parts.append(paragraph)
        if i == insert_idx:
            parts.append(video_tag)
    return "\n\n".join(parts).strip() + "\n"


def _assemble_artifact(
    material: dict[str, Any],
    title: str,
    paragraphs: list[str],
    insert_idx: int,
    layout_reason: str,
    model: str,
    provider: str,
    warnings: list[str],
) -> dict[str, Any]:
    video_file = material["video_file"]
    return {
        "version": COMMENTARY_VERSION,
        "reassembly_id": material["reassembly_id"],
        "title": title,
        "paragraphs": paragraphs,
        "insert_after_index": insert_idx,
        "layout_reason": layout_reason,
        "video": {"file": video_file},
        "markdown": _assemble_markdown(title, paragraphs, insert_idx, Path(video_file).name),
        "source": {
            "provider": provider,
            "model": model,
            "clip_count": len(material["clips"]),
            "content_hash": material.get("content_hash", ""),
            "generated_at": now_iso(),
        },
        "warnings": warnings,
        "status": "ok",
    }


def generate_one(config: Any, material: dict[str, Any]) -> dict[str, Any]:
    """对单条成片素材跑两次 LLM：① 文案 ② 排版位置，返回 commentary_v1 工件。"""
    cmt = _commentary_cfg(config)
    client, model, fallbacks, provider = _make_commentary_client(config)

    script_res = client.call_json(
        model=model,
        fallback_models=fallbacks,
        prompt=SCRIPT_PROMPT,
        input_data=_build_script_input(material, cmt),
        temperature=float(cmt.get("temperature", 0.3) or 0.3),
        max_tokens=int(cmt.get("max_tokens", 1200) or 1200),
    )
    title, paragraphs, warnings = _normalize_script(script_res.parsed, cmt, material)

    insert_idx, reason = 0, ""
    if len(paragraphs) > 1:
        try:
            layout_res = client.call_json(
                model=model,
                fallback_models=fallbacks,
                prompt=LAYOUT_PROMPT,
                input_data={"paragraphs": paragraphs, "video_speech": _video_speech_blob(material, cmt)},
                temperature=float(cmt.get("layout_temperature", 0.0) or 0.0),
                max_tokens=int(cmt.get("layout_max_tokens", 4000) or 4000),
            )
            insert_idx, reason = _normalize_layout(layout_res.parsed, len(paragraphs))
        except Exception as exc:  # noqa: BLE001 - 排版失败不致命，回退插在首段后
            warnings.append(f"layout_failed:{exc}")
            insert_idx, reason = 0, ""

    return _assemble_artifact(material, title, paragraphs, insert_idx, reason, model, provider, warnings)


# --------------------------------------------------------------------------- #
# 版本目录、生成入口与缓存读取
# --------------------------------------------------------------------------- #
def _version_dirs(task_dir: Path) -> list[Path]:
    base = task_dir / COMMENTARY_BASE
    if not base.exists():
        return []
    dirs = [p for p in base.iterdir() if p.is_dir() and re.fullmatch(r"v\d+", p.name)]
    return sorted(dirs, key=lambda p: int(p.name[1:]), reverse=True)


def _new_version_dir(task_dir: Path) -> tuple[str, Path]:
    base = task_dir / COMMENTARY_BASE
    next_num = 1
    if base.exists():
        nums = [int(p.name[1:]) for p in base.iterdir() if p.is_dir() and re.fullmatch(r"v\d+", p.name)]
        if nums:
            next_num = max(nums) + 1
    version = f"v{next_num}"
    return version, base / version


def generate_for_task(
    config: Any,
    task_dir: str | Path,
    *,
    reassembly_id: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """对全部（或指定）可渲染成片生成图文解说，写入新版本目录，返回 (索引路径, 索引 dict)。"""
    task_dir = Path(task_dir)
    manifest = read_json(task_dir / "manifest.json", {})
    rids = renderable_rids(task_dir, manifest)
    if reassembly_id:
        rids = [reassembly_id] if reassembly_id in rids else []

    outputs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    artifacts: dict[str, dict[str, Any]] = {}
    for rid in rids:
        material = build_commentary_material(task_dir, rid)
        if not material:
            skipped.append({"reassembly_id": rid, "reason": "material_unavailable"})
            continue
        try:
            artifact = generate_one(config, material)
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它成片
            skipped.append({"reassembly_id": rid, "reason": f"generate_failed:{exc}"})
            continue
        artifacts[rid] = artifact
        outputs.append({"reassembly_id": rid, "title": artifact["title"], "status": artifact["status"]})

    version, vdir = _new_version_dir(task_dir)
    ensure_dir(vdir)
    for rid, artifact in artifacts.items():
        write_json(vdir / f"commentary_{rid}.json", artifact)
    index = {
        "version": version,
        "outputs": outputs,
        "skipped": skipped,
        "generated_at": now_iso(),
    }
    index_path = write_json(vdir / "commentary_outputs.json", index)
    return index_path, index


def load_latest_commentary(
    task_dir: str | Path,
    reassembly_id: str | None = None,
) -> dict[str, Any] | None:
    """跨版本（新→旧）查找指定 rid 的最新工件；rid 缺省取最新索引的第一条。"""
    task_dir = Path(task_dir)
    for vdir in _version_dirs(task_dir):
        index = read_json(vdir / "commentary_outputs.json", {})
        out_rids = [o.get("reassembly_id") for o in index.get("outputs", []) if o.get("reassembly_id")]
        rid = reassembly_id or (out_rids[0] if out_rids else None)
        if not rid:
            continue
        artifact_path = vdir / f"commentary_{rid}.json"
        if artifact_path.exists():
            data = read_json(artifact_path, None)
            if isinstance(data, dict):
                return data
    return None
