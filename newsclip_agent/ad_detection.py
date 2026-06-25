"""完整版·去广告（full_concat）的广告检测核心。

设计要点：
- 纯函数 + 步骤共用：检测/排序/合并逻辑只此一份，pipeline 步骤负责读产物、调本模块、写产物。
- 双信号：ASR 文案 + 画面（screen_text/visual/scene），输入来自已有 timeline digest（全覆盖、source-aware）。
- 两层判定：规则层（关键词正则，便宜先跑）→ 拿不准的段批量送 DeepSeek 兜底。
- 保守默认：宁可漏过广告也不误删新闻；低置信非新闻段保留并标 suspected；孤立超短广告块不单删。
- 产出 clip schema 对齐 reassembly_cut_plan，渲染复用 _render_reassembly_one。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from .llm import OpenAICompatibleClient
from .utils import seconds_to_timecode

PROMPT_DIR = Path(__file__).resolve().parent / "prompt_texts"
PROMPT_VERSION = "ad_detection_v1"

# --------------------------------------------------------------------------- #
# 标签与关键词
# --------------------------------------------------------------------------- #
LABEL_NEWS = "news"
NON_NEWS_LABELS = ("ad", "promo", "trailer", "station_id", "sponsor")
KEEP_LABELS = {LABEL_NEWS}

# A 商业硬广：面向观众的购买/促销行动号召
AD_ACTION_KEYWORDS = [
    "拨打热线", "订购电话", "咨询热线", "扫码", "扫描二维码", "二维码", "登录官网", "官方网站",
    "限时", "抢购", "特价", "钜惠", "优惠价", "仅售", "只要", "买一送一", "加盟", "招商", "代理",
    "办卡", "会员卡", "全国包邮", "货到付款", "厂家直销", "正品保障",
]
# A 保健品/医疗夸大话术
SHOPPING_KEYWORDS = [
    "调理", "养生", "根治", "无副作用", "疗效", "包治", "特效", "祖传秘方", "强身健体",
]
# B 频道宣传 / 节目预告
PROMO_KEYWORDS = [
    "敬请收看", "敬请期待", "即将播出", "稍后播出", "稍后为您播出", "精彩继续", "不要走开",
    "不要离开", "锁定本台", "锁定资讯台", "锁定凤凰", "每周", "每晚", "本周", "下节目",
    "精彩节目", "更多精彩", "欢迎收看",
]
# C 片头/片尾
TRAILER_KEYWORDS = [
    "感谢收看", "下期再见", "下次再见", "再会", "节目到此结束", "本期节目",
]
# E 赞助播报
SPONSOR_KEYWORDS = [
    "赞助播出", "特约播出", "独家冠名", "鸣谢", "由.{0,12}赞助", "由.{0,12}特约",
]
# 新闻保护词：命中则倾向 news（避免误删财经/产经报道）
NEWS_KEYWORDS = [
    "记者", "报道", "本台消息", "据悉", "据了解", "新闻", "采访", "现场", "发布会",
    "总统", "总理", "外交", "声明", "会议", "峰会", "股市", "指数", "央行", "经济数据",
]
# 画面包装场景（来自 vision shot_type）
PACKAGING_SCENES = {"片头包装", "片头", "片尾", "包装"}

_SPONSOR_RE = re.compile("|".join(SPONSOR_KEYWORDS))


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_cfg(config: Any) -> dict[str, Any]:
    """从 ProjectConfig 读取 [ad_detection]，套用默认值。"""
    raw = (config.raw.get("ad_detection", {}) if hasattr(config, "raw") else (config or {})) or {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "use_llm": bool(raw.get("use_llm", True)),
        "conservative": bool(raw.get("conservative", True)),
        "batch_size": int(raw.get("batch_size", 30) or 30),
        "safety_margin_seconds": float(raw.get("safety_margin_seconds", 0.4) or 0.0),
        "merge_gap_seconds": float(raw.get("merge_gap_seconds", 1.5) or 0.0),
        "min_clip_seconds": float(raw.get("min_clip_seconds", 3.0) or 0.0),
        "min_isolated_ad_seconds": float(raw.get("min_isolated_ad_seconds", 3.0) or 0.0),
        "extra_ad_keywords": [str(x) for x in raw.get("extra_ad_keywords", []) or []],
        "extra_promo_keywords": [str(x) for x in raw.get("extra_promo_keywords", []) or []],
        "extra_news_keywords": [str(x) for x in raw.get("extra_news_keywords", []) or []],
    }


# --------------------------------------------------------------------------- #
# 片段时间与文本提取
# --------------------------------------------------------------------------- #
def seg_time(chunk: dict[str, Any]) -> tuple[str, float, float]:
    """从 timeline digest chunk 稳健取 (source_id, 本地起, 本地止)。"""
    source_id = str(chunk.get("source_id") or "source_1")
    start = _num(chunk.get("local_start_seconds"), _num(chunk.get("start_seconds"), _num(chunk.get("start"), 0.0)))
    end = _num(chunk.get("local_end_seconds"), _num(chunk.get("end_seconds"), _num(chunk.get("end"), start)))
    if end <= start and chunk.get("duration_seconds"):
        end = start + _num(chunk.get("duration_seconds"), 0.0)
    return source_id, round(start, 3), round(end, 3)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _screen_text(chunk: dict[str, Any]) -> str:
    value = chunk.get("screen_text")
    if isinstance(value, list):
        return " ".join(str(x) for x in value if x)
    return str(value or "")


def _scene(chunk: dict[str, Any]) -> str:
    return str(chunk.get("scene") or chunk.get("scene_type") or "")


def _speech(chunk: dict[str, Any]) -> str:
    return str(chunk.get("speech") or chunk.get("asr_text") or chunk.get("asr_digest") or "").strip()


def _hit(text: str, keywords: list[str]) -> str | None:
    for kw in keywords:
        if not kw:
            continue
        if any(ch in ".^$*+?()[]{}|\\" for ch in kw):
            if re.search(kw, text):
                return kw
        elif kw in text:
            return kw
    return None


# --------------------------------------------------------------------------- #
# 规则层
# --------------------------------------------------------------------------- #
def classify_by_rules(
    *,
    speech: str,
    screen_text: str,
    scene: str,
    cfg: dict[str, Any],
) -> tuple[str, str, str] | None:
    """规则层判定。命中强/中信号返回 (label, confidence, reason)；拿不准返回 None 交给 LLM。"""
    text = f"{speech} {screen_text}".strip()
    has_speech = bool(speech.strip())

    # 新闻保护：命中新闻词且没有强广告行动号召 → 直接判 news（保护财经/产经报道）
    news_kw = _hit(text, NEWS_KEYWORDS + cfg.get("extra_news_keywords", []))
    ad_kw = _hit(text, AD_ACTION_KEYWORDS + cfg.get("extra_ad_keywords", []))

    if ad_kw:
        return "ad", "high", f"命中广告行动号召词：{ad_kw}"
    shop_kw = _hit(text, SHOPPING_KEYWORDS)
    if shop_kw and not news_kw:
        return "ad", "med", f"命中保健品/夸大话术：{shop_kw}"

    sponsor_m = _SPONSOR_RE.search(text)
    if sponsor_m:
        return "sponsor", "high", f"命中赞助播报话术：{sponsor_m.group(0)}"

    promo_kw = _hit(text, PROMO_KEYWORDS + cfg.get("extra_promo_keywords", []))
    if promo_kw and not news_kw:
        return "promo", "high", f"命中频道宣传/预告话术：{promo_kw}"

    trailer_kw = _hit(text, TRAILER_KEYWORDS)
    if trailer_kw and not news_kw:
        return "trailer", "med", f"命中片头片尾话术：{trailer_kw}"

    if scene and any(p in scene for p in PACKAGING_SCENES):
        if not news_kw:
            return "trailer", "med", f"画面为包装镜头：{scene}"

    # 台标卡/垫片：无语音 + 无画面文字 + 非新闻场景
    if not has_speech and not screen_text.strip():
        return "station_id", "med", "无语音、无画面文字（疑似台标卡/垫片）"

    if news_kw:
        return "news", "med", f"命中新闻词：{news_kw}"

    # 有正常语音但无任何广告/宣传信号 → 拿不准，交 LLM（保守默认 news）
    return None


# --------------------------------------------------------------------------- #
# 检测主流程
# --------------------------------------------------------------------------- #
def detect(
    chunks: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    llm_call: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """对全覆盖 chunks 逐段分类，返回 {segments, blocks, stats}。"""
    segments: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []

    for index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            continue
        source_id, start, end = seg_time(chunk)
        if end <= start:
            continue
        speech = _speech(chunk)
        screen_text = _screen_text(chunk)
        scene = _scene(chunk)
        segment_id = str(chunk.get("segment_id") or chunk.get("chunk_id") or f"{source_id}_seg_{index:04d}")
        visual = str(chunk.get("visual") or chunk.get("visual_summary") or "").strip()
        rec: dict[str, Any] = {
            "segment_id": segment_id,
            "source_id": source_id,
            "source_index": chunk.get("source_index"),
            "start_seconds": start,
            "end_seconds": end,
            "duration_seconds": round(end - start, 3),
            "speech": speech[:200],
            "screen_text": screen_text[:200],
            "visual": visual[:200],
            "scene": scene,
        }
        verdict = classify_by_rules(speech=speech, screen_text=screen_text, scene=scene, cfg=cfg)
        if verdict is None:
            rec.update(label=LABEL_NEWS, confidence="low", reason="规则未命中，默认保守保留", decided_by="default")
            ambiguous.append(rec)
        else:
            label, confidence, reason = verdict
            rec.update(label=label, confidence=confidence, reason=reason, decided_by="rule")
        segments.append(rec)

    llm_used = False
    if llm_call and cfg.get("use_llm", True) and ambiguous:
        by_id = {r["segment_id"]: r for r in ambiguous}
        for batch in _batched(ambiguous, int(cfg.get("batch_size", 30) or 30)):
            payload = [
                {
                    "segment_id": r["segment_id"],
                    "speech": r["speech"],
                    "screen_text": r.get("screen_text", ""),
                    "visual": r.get("visual", ""),
                    "scene": r["scene"],
                }
                for r in batch
            ]
            try:
                results = llm_call(payload) or []
                llm_used = True
            except Exception as exc:  # noqa: BLE001 - LLM 失败不致命，保守保留
                for r in batch:
                    r["reason"] = f"{r.get('reason', '')}; LLM未判定({exc})"
                continue
            for item in results:
                if not isinstance(item, dict):
                    continue
                rec = by_id.get(str(item.get("segment_id")))
                if not rec:
                    continue
                label = str(item.get("label") or LABEL_NEWS).strip()
                if label not in (LABEL_NEWS, *NON_NEWS_LABELS):
                    label = LABEL_NEWS
                rec["label"] = label
                rec["confidence"] = str(item.get("confidence") or "low").strip() or "low"
                rec["reason"] = str(item.get("reason") or "").strip() or rec.get("reason", "")
                rec["decided_by"] = "llm"

    _apply_keep_decisions(segments, cfg)
    blocks = _merge_drop_blocks(segments)
    stats = _build_stats(segments, blocks, llm_used=llm_used)
    return {"segments": segments, "blocks": blocks, "stats": stats}


def _apply_keep_decisions(segments: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
    conservative = bool(cfg.get("conservative", True))
    for rec in segments:
        label = rec.get("label", LABEL_NEWS)
        if label in KEEP_LABELS:
            rec["keep"] = True
            continue
        # 非新闻：低置信且保守模式 → 保留并标 suspected
        if conservative and str(rec.get("confidence")) == "low":
            rec["keep"] = True
            rec["suspected"] = True
        else:
            rec["keep"] = False

    # 孤立超短广告块（被新闻包夹）不单独删，避免成片抖动
    min_iso = float(cfg.get("min_isolated_ad_seconds", 0.0) or 0.0)
    if min_iso > 0:
        for block in _merge_drop_blocks(segments):
            if block["duration_seconds"] < min_iso:
                for sid in block["segment_ids"]:
                    rec = next((r for r in segments if r["segment_id"] == sid), None)
                    if rec:
                        rec["keep"] = True
                        rec["suspected"] = True
                        rec["reason"] = f"{rec.get('reason', '')}; 孤立超短广告块保留"


def _merge_drop_blocks(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把相邻、同源、被删的段合并成广告块（用于报告与超短保护）。"""
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    ordered = sorted(
        (r for r in segments if not r.get("keep", True)),
        key=lambda r: (str(r.get("source_id")), float(r.get("start_seconds", 0))),
    )
    for rec in ordered:
        sid = str(rec.get("source_id"))
        if (
            current
            and current["source_id"] == sid
            and float(rec["start_seconds"]) - current["end_seconds"] <= 0.05
        ):
            current["end_seconds"] = float(rec["end_seconds"])
            current["segment_ids"].append(rec["segment_id"])
            current["labels"].add(rec.get("label"))
        else:
            if current:
                blocks.append(_finalize_block(current))
            current = {
                "source_id": sid,
                "start_seconds": float(rec["start_seconds"]),
                "end_seconds": float(rec["end_seconds"]),
                "segment_ids": [rec["segment_id"]],
                "labels": {rec.get("label")},
                "reason": rec.get("reason", ""),
            }
    if current:
        blocks.append(_finalize_block(current))
    return blocks


def _finalize_block(block: dict[str, Any]) -> dict[str, Any]:
    duration = round(block["end_seconds"] - block["start_seconds"], 3)
    return {
        "source_id": block["source_id"],
        "start_seconds": round(block["start_seconds"], 3),
        "end_seconds": round(block["end_seconds"], 3),
        "start": seconds_to_timecode(block["start_seconds"], ms=True),
        "end": seconds_to_timecode(block["end_seconds"], ms=True),
        "duration_seconds": duration,
        "label": sorted(l for l in block["labels"] if l)[0] if block["labels"] else "ad",
        "labels": sorted(l for l in block["labels"] if l),
        "segment_ids": block["segment_ids"],
        "reason": block.get("reason", ""),
    }


def _build_stats(segments: list[dict[str, Any]], blocks: list[dict[str, Any]], *, llm_used: bool) -> dict[str, Any]:
    total = sum(float(r.get("duration_seconds", 0)) for r in segments)
    removed = sum(float(b.get("duration_seconds", 0)) for b in blocks)
    label_counts: dict[str, int] = {}
    for rec in segments:
        label_counts[rec.get("label", LABEL_NEWS)] = label_counts.get(rec.get("label", LABEL_NEWS), 0) + 1
    return {
        "segment_count": len(segments),
        "removed_block_count": len(blocks),
        "total_seconds": round(total, 3),
        "removed_seconds": round(removed, 3),
        "kept_seconds": round(total - removed, 3),
        "label_counts": label_counts,
        "llm_used": llm_used,
    }


# --------------------------------------------------------------------------- #
# 拼接计划：时序排序 + 合并保留段 + 安全余量
# --------------------------------------------------------------------------- #
def build_plan(
    segments: list[dict[str, Any]],
    *,
    source_order: list[str],
    aspect_ratio: str = "16:9",
    cfg: dict[str, Any] | None = None,
    reassembly_id: str = "fc_001",
    title: str = "完整版",
) -> dict[str, Any]:
    """按源时间戳顺序、组内时序合并保留段成 clip，输出对齐 reassembly_cut_plan 的 output_video。"""
    cfg = cfg or {}
    margin = float(cfg.get("safety_margin_seconds", 0.4) or 0.0)
    merge_gap = float(cfg.get("merge_gap_seconds", 1.5) or 0.0)
    min_clip = float(cfg.get("min_clip_seconds", 3.0) or 0.0)

    kept = [r for r in segments if r.get("keep", True)]
    dropped = [r for r in segments if not r.get("keep", True)]
    dropped_by_source: dict[str, list[tuple[float, float]]] = {}
    for r in dropped:
        dropped_by_source.setdefault(str(r.get("source_id")), []).append(
            (float(r["start_seconds"]), float(r["end_seconds"]))
        )

    order_index = {sid: i for i, sid in enumerate(source_order)}

    by_source: dict[str, list[dict[str, Any]]] = {}
    for r in kept:
        by_source.setdefault(str(r.get("source_id")), []).append(r)

    clips: list[dict[str, Any]] = []
    warnings: list[str] = []
    target = 0.0
    crop_mode = "fit_blur" if aspect_ratio == "9:16" else "original"

    for source_id in sorted(by_source, key=lambda s: (order_index.get(s, 10**9), s)):
        recs = sorted(by_source[source_id], key=lambda r: float(r["start_seconds"]))
        source_index = next((r.get("source_index") for r in recs if r.get("source_index") not in (None, "")), None)
        # 合并相邻保留段
        merged: list[list[float]] = []
        reasons: list[list[str]] = []
        for r in recs:
            s, e = float(r["start_seconds"]), float(r["end_seconds"])
            if merged and s - merged[-1][1] <= merge_gap:
                merged[-1][1] = max(merged[-1][1], e)
                reasons[-1].append(str(r.get("segment_id")))
            else:
                merged.append([s, e])
                reasons.append([str(r.get("segment_id"))])

        drops = dropped_by_source.get(source_id, [])
        for (s, e), seg_ids in zip(merged, reasons):
            s, e = _apply_safety_margin(s, e, drops, margin)
            duration = round(e - s, 3)
            if duration < max(0.01, min_clip):
                warnings.append(f"{source_id} {seconds_to_timecode(s)}-{seconds_to_timecode(e)}: 安全余量后短于最短片段，丢弃")
                continue
            clips.append({
                "clip_id": f"{reassembly_id}_{len(clips) + 1:03d}",
                "source_clip_id": seg_ids[0],
                "source_id": source_id,
                "source_index": source_index,
                "local_start": seconds_to_timecode(s, ms=True),
                "local_end": seconds_to_timecode(e, ms=True),
                "local_start_seconds": round(s, 3),
                "local_end_seconds": round(e, 3),
                "source_start": seconds_to_timecode(s, ms=True),
                "source_end": seconds_to_timecode(e, ms=True),
                "source_start_seconds": round(s, 3),
                "source_end_seconds": round(e, 3),
                "target_start": seconds_to_timecode(target, ms=True),
                "target_start_seconds": round(target, 3),
                "duration_seconds": duration,
                "original_audio_volume": 1.0,
                "keep_original_audio": True,
                "role": "news",
                "selection_reason": f"保留新闻段（合并 {len(seg_ids)} 段）",
                "boundary_reason": "",
                "transition_after": "hard_cut",
                "crop_mode": crop_mode,
            })
            target += duration

    blocked_reasons = [] if clips else ["all_clips_filtered"]
    return {
        "reassembly_id": reassembly_id,
        "output_file": f"full_concat_{reassembly_id}_draft.mp4",
        "title": title,
        "output_type": "single",
        "aspect_ratio": aspect_ratio,
        "target_resolution": "1080x1920" if aspect_ratio == "9:16" else "1920x1080",
        "audio_policy": "original_audio",
        "video_duration_seconds": round(target, 3),
        "duration_status": "blocked" if blocked_reasons else "ok",
        "clips": clips,
        "warnings": warnings,
        "blocked_reasons": blocked_reasons,
    }


def _apply_safety_margin(
    start: float,
    end: float,
    drops: list[tuple[float, float]],
    margin: float,
) -> tuple[float, float]:
    """保留段边界若紧贴广告块，则内缩 margin，避免广告帧渗入。"""
    if margin <= 0:
        return start, end
    eps = 0.1
    touches_before = any(abs(d_end - start) <= eps for _, d_end in drops)
    touches_after = any(abs(d_start - end) <= eps for d_start, _ in drops)
    new_start = start + margin if touches_before else start
    new_end = end - margin if touches_after else end
    if new_end <= new_start:
        return start, end
    return new_start, new_end


# --------------------------------------------------------------------------- #
# 源排序：文件名时间戳 YYYYMMDD_HHMMSS
# --------------------------------------------------------------------------- #
_TS_RE = re.compile(r"(\d{8})[_-]?(\d{6})")


def parse_source_timestamp(name: str | None) -> str:
    """从素材名解析 YYYYMMDD_HHMMSS 时间戳作为排序键；解析不到返回空串（排到最后）。"""
    if not name:
        return ""
    m = _TS_RE.search(str(name))
    if m:
        return f"{m.group(1)}{m.group(2)}"
    return ""


def order_sources_by_timestamp(source_items: list[dict[str, Any]]) -> list[str]:
    """按文件名时间戳升序返回 source_id 顺序；解析不到时间戳的回退 create_time / source_index。"""
    def sort_key(item: dict[str, Any]) -> tuple[str, str, float]:
        remote = item.get("remote") or {}
        name = item.get("display_name") or remote.get("name") or item.get("source_id") or ""
        ts = parse_source_timestamp(name)
        create_time = str(remote.get("create_time") or remote.get("createTime") or "")
        try:
            idx = float(item.get("source_index") or 0)
        except (TypeError, ValueError):
            idx = 0.0
        # 有时间戳的排前面（"" 排最后）
        return (ts or "99999999999999", create_time or "9999", idx)

    return [str(item.get("source_id") or f"source_{i + 1}") for i, item in enumerate(sorted(source_items, key=sort_key))]


def _batched(items: list[Any], size: int) -> list[list[Any]]:
    size = max(1, int(size))
    return [items[i : i + size] for i in range(0, len(items), size)]


# --------------------------------------------------------------------------- #
# LLM 兜底调用（默认 DeepSeek，纯文本）
# --------------------------------------------------------------------------- #
def build_llm_call(config: Any, cfg: dict[str, Any]) -> Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None:
    """构造广告分类的 LLM 调用闭包；缺配置或关闭时返回 None（规则层照常出片）。"""
    if not cfg.get("use_llm", True):
        return None
    llm = config.llm if hasattr(config, "llm") else (config.get("llm", {}) if isinstance(config, dict) else {})
    ad_cfg = (config.raw.get("ad_detection", {}) if hasattr(config, "raw") else {}) or {}

    provider = ad_cfg.get("provider") or llm.get("text_llm_provider", "openai")
    api_key = llm.get(f"text_{provider}_api_key")
    base_url = llm.get(f"text_{provider}_base_url")
    if not api_key or not base_url:
        provider = llm.get("text_llm_provider", "openai")
        api_key = llm.get(f"text_{provider}_api_key")
        base_url = llm.get(f"text_{provider}_base_url")
    if not api_key or not base_url:
        return None
    model = ad_cfg.get("model") or llm.get(f"text_{provider}_model_name") or llm.get("text_llm_model_name")
    if not model:
        return None
    fallbacks = [m for m in (llm.get(f"text_{provider}_fallback_models", []) or []) if m and m != model]

    client = OpenAICompatibleClient(
        api_key=api_key,
        base_url=base_url,
        timeout=int(llm.get("llm_text_timeout", 180) or 180),
        max_retries=int(llm.get("llm_max_retries", 3) or 3),
    )
    prompt = (PROMPT_DIR / "ad_detection.txt").read_text(encoding="utf-8").strip() + "\n"
    temperature = float(cfg.get("temperature", ad_cfg.get("temperature", 0.0)) or 0.0)
    max_tokens = int(ad_cfg.get("max_tokens", 2000) or 2000)

    def llm_call(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
        res = client.call_json(
            model=str(model),
            fallback_models=fallbacks,
            prompt=prompt,
            input_data={"segments": payload},
            temperature=temperature,
            max_tokens=max_tokens,
        )
        parsed = res.parsed
        if isinstance(parsed, dict):
            return parsed.get("segments") or []
        if isinstance(parsed, list):
            return parsed
        return []

    return llm_call
