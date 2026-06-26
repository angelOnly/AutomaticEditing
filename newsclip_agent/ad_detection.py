"""完整版·去广告（full_concat）的栏目过滤核心。

模型（v2，按栏目过滤）：
- 选中的素材 = 某个“目标栏目”的一段连续拉片（如纪录大时代）。
- 保留：属于目标栏目的内容，含它自己的片头/片尾；删除：其他栏目、其他栏目的预告/播报、广告。
- 根本依据：画面里出现的栏目名（角标 column_bug / 片头大标题 screen_text）== 目标栏目 → 留；是别的栏目或广告 → 删。
- 节目内没角标的模糊镜头（空镜/B-roll）夹在目标栏目段落中间则保留（bridge），保证连续。
- 额外：保留内容里命中重点人物名单 → 报告旁标 🔥。

纯函数 + 步骤共用：pipeline 步骤负责读产物/写产物，本模块负责判定。
信号来自已有 timeline digest（全覆盖、source-aware，含 speech/screen_text/column_bug/scene/时间码/source_id）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from .llm import OpenAICompatibleClient
from .utils import read_json, seconds_to_timecode

PROMPT_DIR = Path(__file__).resolve().parent / "prompt_texts"
PROMPT_VERSION = "ad_detection_v4_news_first"

# 保留 / 删除标签
LABEL_TARGET = "target"
KEEP_LABELS = {LABEL_TARGET, "target_intro_outro", "bridge"}

# 广告/促销行动号召（面向观众的购买引导）
AD_ACTION_KEYWORDS = [
    "拨打热线", "订购电话", "咨询热线", "扫码", "扫描二维码", "二维码", "登录官网", "官方网站",
    "限时", "抢购", "特价", "钜惠", "优惠价", "仅售", "只要", "买一送一", "加盟", "招商", "代理",
    "办卡", "会员卡", "全国包邮", "货到付款", "厂家直销", "正品保障",
]
SHOPPING_KEYWORDS = ["调理", "养生", "根治", "无副作用", "疗效", "包治", "特效", "祖传秘方", "强身健体"]
# 频道宣传 / 节目预告（通常预告“其他”栏目，即使画面挂着目标角标也要删）
PROMO_KEYWORDS = [
    "敬请收看", "敬请期待", "即将播出", "稍后播出", "稍后为您播出", "精彩继续", "不要走开",
    "不要离开", "锁定本台", "锁定资讯台", "锁定凤凰", "下节目", "更多精彩节目",
    "节目预告", "片花", "明天此时", "节目宣传", "每[日晚天周].{0,4}[点時點]播出",
    "每[日晚天].{0,4}[点時點]", "于.{0,8}播出", "敬请关注",
]
TRAILER_KEYWORDS = ["感谢收看", "下期再见", "下次再见", "节目到此结束"]
# 赞助/冠名（含节目自己的赞助卡，按用户要求也删）
SPONSOR_KEYWORDS = [
    "赞助播出", "特约播出", "独家冠名", "鸣谢", "冠名播出", "由.{0,12}赞助", "由.{0,12}特约",
    "由.{0,12}冠名", ".{0,12}冠名",
]
PACKAGING_SCENES = {"片头包装", "片头", "片尾", "包装"}
PACKAGING_FOOTAGE = {"片头包装", "片头", "片尾", "包装", "台标"}

# 赞助/预告套话词，算“残余新闻量”时连同节目名一起剔除，只数真正的新闻文字
_BOILERPLATE = [
    "凤凰卫视", "凤凰", "资讯台", "中文台", "卫视", "节目", "播出", "正在", "本次", "本期", "主题",
    "相关", "内容", "提及", "为您", "由", "的", "集团", "保险", "冠名", "赞助", "特约",
]

DEFAULT_VIP_NAMES = ["习近平", "李强", "赵乐际", "王沪宁", "蔡奇", "丁薛祥", "李希"]

_SPONSOR_RE = re.compile("|".join(SPONSOR_KEYWORDS))
# 文件名时间戳 YYYYMMDD_HHMMSS
_TS_RE = re.compile(r"(\d{4})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def _section(config: Any, name: str) -> dict[str, Any]:
    if hasattr(config, "raw"):
        return (config.raw.get(name, {}) or {})
    if isinstance(config, dict):
        # 测试可直接传该段 dict（无包裹）或带 raw 的整配置
        if "raw" in config and isinstance(config["raw"], dict):
            return config["raw"].get(name, {}) or {}
        return config
    return {}


def load_cfg(config: Any) -> dict[str, Any]:
    """[ad_detection] 段参数。"""
    raw = _section(config, "ad_detection")
    return {
        "enabled": bool(raw.get("enabled", True)),
        "use_llm": bool(raw.get("use_llm", True)),
        "conservative": bool(raw.get("conservative", True)),
        "batch_size": int(raw.get("batch_size", 30) or 30),
        "temperature": float(raw.get("temperature", 0.0) or 0.0),
        "max_tokens": int(raw.get("max_tokens", 2000) or 2000),
        "provider": raw.get("provider") or "",
        "model": raw.get("model") or "",
        "safety_margin_seconds": float(raw.get("safety_margin_seconds", 0.4) or 0.0),
        "merge_gap_seconds": float(raw.get("merge_gap_seconds", 1.5) or 0.0),
        "min_clip_seconds": float(raw.get("min_clip_seconds", 3.0) or 0.0),
        # 保新闻为先：赞助/预告/广告话术命中后，仅当“去掉话术+节目名+套话后剩余的新闻文字”
        # 少于此字数（即基本是纯杂质）才删；和大段新闻混在一起时保留新闻。
        "min_news_chars": int(raw.get("min_news_chars", 12) or 12),
        "extra_ad_keywords": [str(x) for x in raw.get("extra_ad_keywords", []) or []],
        "extra_promo_keywords": [str(x) for x in raw.get("extra_promo_keywords", []) or []],
    }


def load_full_concat_cfg(config: Any) -> dict[str, Any]:
    raw = _section(config, "full_concat")
    return {
        "target_column_mode": str(raw.get("target_column_mode", "auto") or "auto"),
        "bridge_keep_within_run": bool(raw.get("bridge_keep_within_run", True)),
        "bridge_max_gap_seconds": float(raw.get("bridge_max_gap_seconds", 90.0) or 0.0),
    }


def load_schedule(config: Any) -> list[dict[str, Any]]:
    raw = _section(config, "program_schedule")
    if not raw.get("enabled", True):
        return []
    entries = []
    for e in raw.get("entries", []) or []:
        if not isinstance(e, dict) or not e.get("column"):
            continue
        entries.append({
            "column": str(e["column"]).strip(),
            "minutes": _hhmm_to_minutes(str(e.get("time", "00:00"))),
            "days": [int(d) for d in (e.get("days") or []) if str(d).strip().lstrip("-").isdigit()],
        })
    return entries


def schedule_columns(schedule: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for e in schedule:
        if e["column"] not in seen:
            seen.append(e["column"])
    return seen


def load_vip_names(config: Any) -> list[str]:
    """重点人物名单：优先读 store_file（前端可在线编辑），否则用 config 打底名单。"""
    raw = _section(config, "vip_persons")
    baseline = [str(x).strip() for x in (raw.get("names") or DEFAULT_VIP_NAMES) if str(x).strip()]
    store_file = raw.get("store_file")
    root = getattr(config, "root_dir", None) if hasattr(config, "root_dir") else None
    if store_file and root is not None:
        p = Path(store_file)
        if not p.is_absolute():
            p = Path(root) / p
        data = read_json(p, None)
        if isinstance(data, dict):
            names = [str(x).strip() for x in (data.get("names") or []) if str(x).strip()]
            if names:
                return names
    return baseline


# --------------------------------------------------------------------------- #
# 片段时间 / 文本 / 时间戳
# --------------------------------------------------------------------------- #
def seg_time(chunk: dict[str, Any]) -> tuple[str, float, float]:
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


def _column_bug(chunk: dict[str, Any]) -> str:
    return str(chunk.get("column_bug") or "").strip()


_T2S_CONVERTER = None
_T2S_TRIED = False


def _t2s(text: str) -> str:
    """繁体→简体归一（凤凰画面是繁体，需与简体排期/目标比对）。opencc 缺失则原样返回。"""
    global _T2S_CONVERTER, _T2S_TRIED
    if not text:
        return text
    if _T2S_CONVERTER is None and not _T2S_TRIED:
        _T2S_TRIED = True
        try:
            import opencc  # type: ignore
            _T2S_CONVERTER = opencc.OpenCC("t2s")
        except Exception:
            _T2S_CONVERTER = None
    if _T2S_CONVERTER is None:
        return text
    try:
        return _T2S_CONVERTER.convert(text)
    except Exception:
        return text


def _norm(text: str) -> str:
    return _t2s(re.sub(r"\s+", "", str(text or "")))


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


def column_match(text: str, column: str) -> bool:
    """文本里是否出现某栏目名（双向包含，忽略空白）。"""
    if not text or not column:
        return False
    t, c = _norm(text), _norm(column)
    return bool(c) and (c in t or (len(c) >= 4 and t in c))


# --------------------------------------------------------------------------- #
# 时间戳 → 排期推断
# --------------------------------------------------------------------------- #
def parse_source_timestamp(name: str | None) -> str:
    if not name:
        return ""
    m = _TS_RE.search(str(name))
    return f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}{m.group(5)}{m.group(6)}" if m else ""


def _parse_datetime(name: str | None) -> tuple[int, int] | None:
    """返回 (weekday 0-6, 分钟数 of day)。解析不到返回 None。"""
    if not name:
        return None
    m = _TS_RE.search(str(name))
    if not m:
        return None
    import datetime as _dt
    try:
        d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    minutes = int(m.group(4)) * 60 + int(m.group(5))
    return d.weekday(), minutes


def _hhmm_to_minutes(text: str) -> int:
    parts = str(text).split(":")
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return 0


def infer_column_from_schedule(schedule: list[dict[str, Any]], name: str | None) -> str:
    """按文件名时间戳查排期：取当天 start<=该时刻 的最近一档栏目。"""
    dt = _parse_datetime(name)
    if not dt or not schedule:
        return ""
    weekday, minutes = dt
    same_day = [e for e in schedule if weekday in e["days"]]
    before = [e for e in same_day if e["minutes"] <= minutes]
    pool = before or same_day
    if not pool:
        return ""
    pick = max(before, key=lambda e: e["minutes"]) if before else min(same_day, key=lambda e: abs(e["minutes"] - minutes))
    return pick["column"]


# --------------------------------------------------------------------------- #
# 目标栏目解析
# --------------------------------------------------------------------------- #
def resolve_target_column(
    *,
    chunks: list[dict[str, Any]],
    schedule: list[dict[str, Any]],
    source_items: list[dict[str, Any]] | None,
    override: str | None,
    mode: str = "auto",
) -> tuple[str, str]:
    """返回 (target_column, source)。source ∈ manual/column_bug/schedule/unknown。"""
    override = (override or "").strip()
    if override or mode == "manual":
        return override, "manual"

    # 排期推断（各源时间戳投票）
    sched_guess = ""
    if schedule and source_items:
        votes: dict[str, int] = {}
        for item in source_items:
            remote = item.get("remote") or {}
            name = item.get("display_name") or remote.get("name") or item.get("source_id") or ""
            col = infer_column_from_schedule(schedule, name)
            if col:
                votes[col] = votes.get(col, 0) + 1
        if votes:
            sched_guess = max(votes, key=lambda k: votes[k])

    if mode == "schedule":
        return sched_guess, "schedule" if sched_guess else "unknown"

    # auto：角标多数票为准（繁简归一，简繁合并计票）
    bug_votes: dict[str, int] = {}
    for chunk in chunks:
        bug = _t2s(_column_bug(chunk))
        if bug:
            bug_votes[bug] = bug_votes.get(bug, 0) + 1
    if bug_votes:
        return max(bug_votes, key=lambda k: bug_votes[k]), "column_bug"
    if sched_guess:
        return sched_guess, "schedule"
    return "", "unknown"


# --------------------------------------------------------------------------- #
# 逐段特征 + 标签
# --------------------------------------------------------------------------- #
def _residual_news_len(text: str, target: str, known_columns: list[str]) -> int:
    """去掉赞助/预告/广告话术 + 节目名 + 套话 + 非中文后，剩下的真正新闻文字字数。

    用来判断一段是“纯杂质（剩很少）”还是“新闻里夹了一句杂质（剩很多）”。
    """
    t = re.sub(r"《[^》]{0,15}》", "", text or "")
    terms = (
        AD_ACTION_KEYWORDS + SHOPPING_KEYWORDS + PROMO_KEYWORDS + TRAILER_KEYWORDS
        + SPONSOR_KEYWORDS + _BOILERPLATE + [target] + list(known_columns)
    )
    for term in terms:
        if term and not any(ch in ".^$*+?()[]{}|\\" for ch in term):
            t = t.replace(term, "")
    # 只数中文字符，剔除标点/数字/英文/空白
    return len(re.sub(r"[^一-鿿]", "", t))


def _classify_segment(
    *,
    speech: str,
    screen_text: str,
    bug: str,
    scene: str,
    footage_types: list[str],
    has_speech: bool,
    target: str,
    known_columns: list[str],
    cfg: dict[str, Any],
) -> tuple[str | None, str, str]:
    """返回 (label, keep_state, reason)。keep_state ∈ keep/drop/undecided。

    策略（保新闻为先）：杂质话术（赞助/冠名、预告、广告）只在“基本是纯杂质、没什么新闻”
    时才删；和大段新闻混在一起就保留新闻。其他栏目（异角标）一律删。
    """
    text = _t2s(f"{speech} {screen_text}".strip())
    is_pkg = (bool(scene) and any(p in scene for p in PACKAGING_SCENES)) or any(
        f in PACKAGING_FOOTAGE for f in (footage_types or [])
    )

    target_by_bug = bool(bug and target and column_match(bug, target))
    target_in_text = bool(target and column_match(text, target))
    detected_target = target_by_bug or target_in_text

    detected_other = ""
    if bug and target and not column_match(bug, target):
        detected_other = _t2s(bug)
    if not detected_other:
        for col in known_columns:
            if col != target and column_match(text, col):
                detected_other = col
                break
    mentions_other = bool(detected_other)
    # 《节目名》书名号：出现非目标栏目的《X》，是“在预告/提及别的节目”的强信号
    #（不依赖排期表是否收录该节目，凤凰全球连线/凤凰早班车等中文台节目也能识别）
    bracket_titles = [t for t in re.findall(r"《([^》]{2,15})》", text) if not column_match(t, target)]
    other_title = bracket_titles[0] if bracket_titles else ""

    # 保新闻为先：这段除去杂质话术/节目名/套话后，还剩多少真正的新闻文字
    residual = _residual_news_len(text, target, known_columns)
    news_substantial = residual >= int(cfg.get("min_news_chars", 12) or 12)

    # 1) 赞助/冠名：基本是纯赞助卡才删；夹着大段新闻（如开头“由华润赞助…斯塔默辞职…”）保留新闻
    sponsor_m = _SPONSOR_RE.search(text)
    if sponsor_m and not news_substantial:
        return "sponsor", "drop", f"赞助/冠名卡：{sponsor_m.group(0)[:16]}"

    # 2) 预告/宣传：命中预告话术，且（提到别的节目 / 非目标《节目名》/ 非目标正片），且基本无新闻实质 → 删
    promo_kw = _hit(text, PROMO_KEYWORDS + cfg.get("extra_promo_keywords", []))
    if promo_kw and (mentions_other or other_title or not detected_target) and not news_substantial:
        reason_extra = f"《{other_title}》" if other_title else (f"其他栏目:{detected_other}" if mentions_other else "")
        return "promo", "drop", f"预告/宣传卡：{promo_kw} {reason_extra}".strip()
    if _hit(text, TRAILER_KEYWORDS) and not detected_target and not news_substantial:
        return "trailer_other", "drop", "片尾结束语（非目标栏目）"

    # 3) 商业广告：行动号召/购物话术，且基本无新闻实质 → 删（新闻里偶提“扫码/赞助”不误删）
    ad_kw = _hit(text, AD_ACTION_KEYWORDS + cfg.get("extra_ad_keywords", []))
    if ad_kw and not news_substantial:
        return "ad", "drop", f"广告卡：{ad_kw}"
    if _hit(text, SHOPPING_KEYWORDS) and not detected_target and not news_substantial:
        return "ad", "drop", "保健品/夸大话术"

    # 4) 目标栏目正片（角标或片头大标题命中目标）
    if detected_target:
        if is_pkg:
            return "target_intro_outro", "keep", "目标栏目片头/片尾"
        return LABEL_TARGET, "keep", f"目标栏目角标/标题：{_t2s(bug) or target}"

    # 5) 其他栏目
    if mentions_other:
        return "other_column", "drop", f"其他栏目：{detected_other}"

    # 6) 包装/台标卡（认不出目标名）
    if is_pkg:
        return "packaging_other", "drop", f"包装/台标镜头（非目标）：{scene or '/'.join(footage_types or [])}"

    # 7) 静音卡/无实质（无角标且无语音）→ 多半是垫片/台标/包装
    if not has_speech:
        return "station_id", "drop", "无角标、无语音（疑似台标/垫片）"

    # 8) 无角标但有连续解说的“正片样”片段 → 交 LLM / bridge 兜底（倾向保留，避免精编误删）
    return None, "undecided", "无角标、有解说，待判（疑似正片）"


def detect(
    chunks: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    fc_cfg: dict[str, Any] | None = None,
    schedule: list[dict[str, Any]] | None = None,
    source_items: list[dict[str, Any]] | None = None,
    override_column: str | None = None,
    vip_names: list[str] | None = None,
    llm_call: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    fc_cfg = fc_cfg or load_full_concat_cfg({})
    schedule = schedule or []
    vip_names = vip_names or DEFAULT_VIP_NAMES
    known_columns = schedule_columns(schedule)

    target, target_source = resolve_target_column(
        chunks=chunks,
        schedule=schedule,
        source_items=source_items,
        override=override_column,
        mode=fc_cfg.get("target_column_mode", "auto"),
    )

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
        bug = _column_bug(chunk)
        scene = _scene(chunk)
        footage_types = chunk.get("footage_types") or []
        segment_id = str(chunk.get("segment_id") or chunk.get("chunk_id") or f"{source_id}_seg_{index:04d}")
        label, keep_state, reason = _classify_segment(
            speech=speech, screen_text=screen_text, bug=bug, scene=scene,
            footage_types=footage_types, has_speech=bool(speech.strip()),
            target=target, known_columns=known_columns, cfg=cfg,
        )
        rec: dict[str, Any] = {
            "segment_id": segment_id,
            "source_id": source_id,
            "source_index": chunk.get("source_index"),
            "start_seconds": start,
            "end_seconds": end,
            "duration_seconds": round(end - start, 3),
            "speech": speech[:200],
            "screen_text": screen_text[:200],
            "column_bug": bug,
            "scene": scene,
            "label": label or "unknown",
            "keep_state": keep_state,
            "reason": reason,
            "decided_by": "rule" if label is not None else "pending",
        }
        segments.append(rec)
        if keep_state == "undecided":
            ambiguous.append(rec)

    llm_used = False
    if llm_call and cfg.get("use_llm", True) and target and ambiguous:
        by_id = {r["segment_id"]: r for r in ambiguous}
        for batch in _batched(ambiguous, int(cfg.get("batch_size", 30) or 30)):
            payload = {
                "target_column": target,
                "segments": [
                    {
                        "segment_id": r["segment_id"],
                        "speech": r["speech"],
                        "screen_text": r["screen_text"],
                        "column_bug": r["column_bug"],
                        "scene": r["scene"],
                    }
                    for r in batch
                ],
            }
            try:
                results = llm_call(payload) or []
                llm_used = True
            except Exception as exc:  # noqa: BLE001 - LLM 失败不致命
                for r in batch:
                    r["reason"] = f"{r.get('reason', '')}; LLM未判定({exc})"
                continue
            for item in results:
                if not isinstance(item, dict):
                    continue
                rec = by_id.get(str(item.get("segment_id")))
                if not rec:
                    continue
                llm_label = str(item.get("label") or "").strip()
                rec["decided_by"] = "llm"
                rec["reason"] = str(item.get("reason") or "").strip() or rec.get("reason", "")
                if llm_label == "target":
                    rec["label"], rec["keep_state"] = LABEL_TARGET, "keep"
                elif llm_label in ("other", "other_column"):
                    rec["label"], rec["keep_state"] = "other_column", "drop"
                elif llm_label == "ad":
                    rec["label"], rec["keep_state"] = "ad", "drop"
                elif llm_label == "promo":
                    rec["label"], rec["keep_state"] = "promo", "drop"
                # 其它/无效返回：保持 undecided 交给 bridge

    _resolve_keep(segments, fc_cfg)
    vip = detect_vip(segments, vip_names)
    blocks = _merge_drop_blocks(segments)
    stats = _build_stats(segments, blocks, llm_used=llm_used, target=target)
    return {
        "segments": segments,
        "blocks": blocks,
        "stats": stats,
        "target_column": target,
        "target_column_source": target_source,
        "vip": vip,
    }


def _resolve_keep(segments: list[dict[str, Any]], fc_cfg: dict[str, Any]) -> None:
    """先按 keep_state 定 keep；再让目标栏目段“向两侧生长”，把紧邻的、像正片的无角标段纳入。

    与旧版“必须前后两侧都有目标”不同：这里从目标段沿相邻的 undecided 段向前/向后蔓延，
    一直到碰见杂质(drop)段为止。这样开头第一句、结尾最后一句（角标还没出现/已撤）也能保住，
    而插播的广告/赞助/片头(都是 drop)会成为边界挡住蔓延、不会被带进来。
    """
    bridge = bool(fc_cfg.get("bridge_keep_within_run", True))
    max_gap = float(fc_cfg.get("bridge_max_gap_seconds", 90.0) or 0.0)
    for rec in segments:
        if rec["keep_state"] == "keep":
            rec["keep"] = True
        elif rec["keep_state"] == "drop":
            rec["keep"] = False
        else:
            rec["keep"] = None  # undecided：待 bridge 蔓延决定

    if not bridge:
        for rec in segments:
            if rec["keep"] is None:
                rec["keep"] = False
                rec["label"] = "unknown_drop"
        return

    by_source: dict[str, list[dict[str, Any]]] = {}
    for rec in segments:
        by_source.setdefault(str(rec["source_id"]), []).append(rec)
    for recs in by_source.values():
        recs.sort(key=lambda r: float(r["start_seconds"]))
        n = len(recs)
        # 迭代蔓延：undecided 段若与一个“已保留”相邻（间隔 <= max_gap）则纳入；
        # drop 段 keep=False 不会蔓延，自然成为边界。直到不再变化。
        changed = True
        while changed:
            changed = False
            for i, rec in enumerate(recs):
                if rec.get("keep") is not None:
                    continue
                left = recs[i - 1] if i > 0 else None
                right = recs[i + 1] if i < n - 1 else None
                attach_left = bool(left and left.get("keep") is True and float(rec["start_seconds"]) - float(left["end_seconds"]) <= max_gap)
                attach_right = bool(right and right.get("keep") is True and float(right["start_seconds"]) - float(rec["end_seconds"]) <= max_gap)
                if attach_left or attach_right:
                    rec["keep"] = True
                    rec["label"] = "bridge"
                    rec["reason"] = f"{rec.get('reason', '')}; 紧邻目标栏目正片，保留连续".strip("; ")
                    changed = True
        for rec in recs:
            if rec.get("keep") is None:
                rec["keep"] = False
                rec["label"] = "unknown_drop"


def detect_vip(segments: list[dict[str, Any]], vip_names: list[str]) -> dict[str, Any]:
    """在保留内容里命中重点人物。"""
    hits: list[dict[str, Any]] = []
    found: list[str] = []
    for rec in segments:
        if not rec.get("keep"):
            continue
        text = f"{rec.get('speech', '')} {rec.get('screen_text', '')}"
        for name in vip_names:
            if name and name in text:
                if name not in found:
                    found.append(name)
                hits.append({
                    "name": name,
                    "segment_id": rec["segment_id"],
                    "time": seconds_to_timecode(float(rec["start_seconds"])),
                })
    return {"present": bool(found), "names": found, "hits": hits[:200]}


def _merge_drop_blocks(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    ordered = sorted(
        (r for r in segments if not r.get("keep", True)),
        key=lambda r: (str(r.get("source_id")), float(r.get("start_seconds", 0))),
    )
    for rec in ordered:
        sid = str(rec.get("source_id"))
        if current and current["source_id"] == sid and float(rec["start_seconds"]) - current["end_seconds"] <= 0.05:
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
    labels = sorted(l for l in block["labels"] if l)
    return {
        "source_id": block["source_id"],
        "start_seconds": round(block["start_seconds"], 3),
        "end_seconds": round(block["end_seconds"], 3),
        "start": seconds_to_timecode(block["start_seconds"], ms=True),
        "end": seconds_to_timecode(block["end_seconds"], ms=True),
        "duration_seconds": duration,
        "label": labels[0] if labels else "other_column",
        "labels": labels,
        "segment_ids": block["segment_ids"],
        "reason": block.get("reason", ""),
    }


def _build_stats(segments: list[dict[str, Any]], blocks: list[dict[str, Any]], *, llm_used: bool, target: str) -> dict[str, Any]:
    total = sum(float(r.get("duration_seconds", 0)) for r in segments)
    removed = sum(float(b.get("duration_seconds", 0)) for b in blocks)
    label_counts: dict[str, int] = {}
    for rec in segments:
        label_counts[rec.get("label", "unknown")] = label_counts.get(rec.get("label", "unknown"), 0) + 1
    return {
        "target_column": target,
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
                warnings.append(f"{source_id} {seconds_to_timecode(s)}-{seconds_to_timecode(e)}: 安全余量后过短，丢弃")
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
                "role": "target_column",
                "selection_reason": f"保留目标栏目段（合并 {len(seg_ids)} 段）",
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


def _apply_safety_margin(start: float, end: float, drops: list[tuple[float, float]], margin: float) -> tuple[float, float]:
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


def order_sources_by_timestamp(source_items: list[dict[str, Any]]) -> list[str]:
    def sort_key(item: dict[str, Any]) -> tuple[str, str, float]:
        remote = item.get("remote") or {}
        name = item.get("display_name") or remote.get("name") or item.get("source_id") or ""
        ts = parse_source_timestamp(name)
        create_time = str(remote.get("create_time") or remote.get("createTime") or "")
        try:
            idx = float(item.get("source_index") or 0)
        except (TypeError, ValueError):
            idx = 0.0
        return (ts or "99999999999999", create_time or "9999", idx)

    return [str(item.get("source_id") or f"source_{i + 1}") for i, item in enumerate(sorted(source_items, key=sort_key))]


def _batched(items: list[Any], size: int) -> list[list[Any]]:
    size = max(1, int(size))
    return [items[i : i + size] for i in range(0, len(items), size)]


# --------------------------------------------------------------------------- #
# LLM 兜底调用（默认 DeepSeek，纯文本）
# --------------------------------------------------------------------------- #
def build_llm_call(config: Any, cfg: dict[str, Any]) -> Callable[[dict[str, Any]], list[dict[str, Any]]] | None:
    if not cfg.get("use_llm", True):
        return None
    llm = config.llm if hasattr(config, "llm") else (config.get("llm", {}) if isinstance(config, dict) else {})
    ad_cfg = _section(config, "ad_detection")

    provider = cfg.get("provider") or ad_cfg.get("provider") or llm.get("text_llm_provider", "openai")
    api_key = llm.get(f"text_{provider}_api_key")
    base_url = llm.get(f"text_{provider}_base_url")
    if not api_key or not base_url:
        provider = llm.get("text_llm_provider", "openai")
        api_key = llm.get(f"text_{provider}_api_key")
        base_url = llm.get(f"text_{provider}_base_url")
    if not api_key or not base_url:
        return None
    model = cfg.get("model") or ad_cfg.get("model") or llm.get(f"text_{provider}_model_name") or llm.get("text_llm_model_name")
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
    temperature = float(cfg.get("temperature", 0.0) or 0.0)
    max_tokens = int(cfg.get("max_tokens", 2000) or 2000)

    def llm_call(input_data: dict[str, Any]) -> list[dict[str, Any]]:
        res = client.call_json(
            model=str(model),
            fallback_models=fallbacks,
            prompt=prompt,
            input_data=input_data,
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
