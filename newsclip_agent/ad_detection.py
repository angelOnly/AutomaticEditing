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
PROMPT_VERSION = "ad_detection_v6_visual_ownership"

# 保留 / 删除标签
LABEL_TARGET = "target"
KEEP_LABELS = {LABEL_TARGET, "target_intro_outro", "bridge"}

STATE_HARD_DROP = "hard_drop"
STATE_STRONG_KEEP = "strong_keep"
STATE_BOUNDARY_CANDIDATE = "boundary_candidate"
STATE_AMBIGUOUS = "ambiguous"

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
    "每[日晚天].{0,4}[点時點]", "每[日晚天].{0,8}[0-9一二三四五六七八九十:：]{1,8}",
    "于.{0,8}播出", "敬请关注", "宣传内容", "节目宣传内容", "主打.{0,12}要闻",
]
TRAILER_KEYWORDS = ["感谢收看", "下期再见", "下次再见", "节目到此结束"]
# 赞助/冠名（含节目自己的赞助卡，按用户要求也删）
SPONSOR_KEYWORDS = [
    "赞助播出", "特约播出", "独家冠名", "鸣谢", "冠名播出", "由.{0,12}赞助", "由.{0,12}特约",
    "由.{0,12}冠名", ".{0,12}冠名",
]
SPONSOR_CARD_KEYWORDS = ["有华润多美好", "有華潤多美好", "What a Wonderful Life"]
NON_TARGET_PROMO_MARKERS = [
    "TRAVELOGUE", "CGTN", "PHOENIX MORNING EXPRESS", "凤凰早班车", "鳳凰早班車",
    "凤凰全球连线", "鳳凰全球連線", "CHIMELONG RESORT", "长隆度假区", "鳳凰資訊",
    "凤凰资讯", "News that matters",
]
PLATFORM_PROMO_KEYWORDS = [
    "传播矩阵", "传播平台", "平台宣传", "频道宣传", "宣传类片头", "多平台", "社交平台",
    "手机端", "平板电脑", "内容列表", "节目列表", "短视频列表", "帖文界面", "关注我们",
    "下载客户端", "News that matters", "拉近全球华人距离",
    "港人港事港你知",
]
COPYRIGHT_SLATE_KEYWORDS = [
    "版权声明", "版权所有", "是凤凰卫视有限公司的商标", "©2026凤凰卫视", "IFENG.COM",
]
CREDITS_KEYWORDS = [
    "节目片尾职员表", "片尾职员表", "演职人员", "职员表", "制作人", "总制作人", "总策划",
]
HARD_DROP_CONTENT_ROLES = {
    "sponsor_card", "commercial_ad", "other_program_promo", "channel_promo", "platform_promo",
    "station_id", "copyright_slate",
}
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
        "bridge_max_total_seconds": float(raw.get("bridge_max_total_seconds", 30.0) or 0.0),
        "bridge_edge_max_seconds": float(raw.get("bridge_edge_max_seconds", 30.0) or 0.0),
        "boundary_candidate_max_seconds": float(raw.get("boundary_candidate_max_seconds", 30.0) or 0.0),
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


def _speech_context(chunk: dict[str, Any]) -> str:
    return str(chunk.get("speech_context") or "").strip()


def _visual_summary(chunk: dict[str, Any]) -> str:
    return str(chunk.get("visual_summary") or chunk.get("visual") or "").strip()


def _content_role(chunk: dict[str, Any]) -> str:
    return str(chunk.get("content_role") or "").strip().lower()


def _column_bug(chunk: dict[str, Any]) -> str:
    return str(chunk.get("column_bug") or "").strip()


def _expand_visual_subsegments(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把带 frame_shots 的混杂 chunk 拆成视觉子段。

    旧公共池可能仍是 60s，一个 chunk 内会同时出现目标正片、其他栏目宣传、赞助卡。
    frame_shots 已经给出关键帧时间和画面描述，先拆成子段再判定，可以避免单个目标角标污染整段。
    """
    expanded: list[dict[str, Any]] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        source_id, start, end = seg_time(chunk)
        shots = _normalized_frame_shots(chunk, start, end)
        if len(shots) < 2 or not _should_split_visual_chunk(chunk, shots):
            expanded.append(chunk)
            continue

        if chunk.get("segment_id"):
            original_id = str(chunk["segment_id"])
        elif chunk.get("chunk_id"):
            original_id = f"{source_id}_{chunk.get('chunk_id')}"
        else:
            original_id = f"{source_id}_{start:g}_{end:g}"
        raw_bug = _column_bug(chunk)
        parent_speech = _speech(chunk)
        for idx, shot in enumerate(shots):
            seg_start = max(start, float(shot["t"]))
            seg_end = end if idx == len(shots) - 1 else min(end, float(shots[idx + 1]["t"]))
            if seg_end <= seg_start:
                continue
            visual = str(shot.get("visual") or "")
            shot_type = str(shot.get("shot_type") or shot.get("type") or "")
            sub = dict(chunk)
            sub["segment_id"] = f"{original_id}_frame_{idx + 1:02d}"
            sub["parent_segment_id"] = original_id
            sub["local_start_seconds"] = round(seg_start, 3)
            sub["local_end_seconds"] = round(seg_end, 3)
            sub["start_seconds"] = round(seg_start, 3)
            sub["end_seconds"] = round(seg_end, 3)
            sub["duration_seconds"] = round(seg_end - seg_start, 3)
            sub["visual"] = visual
            sub["visual_summary"] = visual
            shot_screen_text = shot.get("screen_text")
            sub["screen_text"] = shot_screen_text if isinstance(shot_screen_text, list) else visual
            sub["scene"] = shot_type
            sub["scene_type"] = shot_type
            sub["footage_types"] = [shot_type] if shot_type else []
            sub["frame_shots"] = [shot]
            sub["content_role"] = str(shot.get("content_role") or "")
            sub["program_identity"] = str(shot.get("program_identity") or "")
            sub["program_mentions"] = shot.get("program_mentions") if isinstance(shot.get("program_mentions"), list) else []
            shot_bug = str(shot.get("column_bug") or "").strip()
            # 父 chunk 的角标不能无条件复制到每个视觉子段。旧 schema 没有逐帧 column_bug 时，
            # 仅在逐帧描述明确写出角落/栏目角标时做兼容回填；菜单/节目列表中的同名不算角标。
            corner_words = ("右下角", "右上角", "左下角", "左上角", "画面角落", "栏目角标")
            legacy_local_bug = bool(
                raw_bug and column_match(visual, raw_bug) and any(word in visual for word in corner_words)
            )
            sub["column_bug"] = shot_bug or (raw_bug if legacy_local_bug else "")
            # frame_shots 只有视觉时间点，父段 ASR 通常跨越整个 chunk，不能复制为每个子段的
            # 本地语音证据。父语音仅作为上下文保存，规则层不会用它证明栏目归属。
            sub["speech_context"] = parent_speech
            precise_asr = chunk.get("asr_segments") if isinstance(chunk.get("asr_segments"), list) else []
            local_speech_parts = []
            for asr_seg in precise_asr:
                if not isinstance(asr_seg, dict):
                    continue
                asr_start = _num(asr_seg.get("start"), _num(asr_seg.get("start_seconds"), seg_start))
                asr_end = _num(asr_seg.get("end"), _num(asr_seg.get("end_seconds"), asr_start))
                if min(asr_end, seg_end) - max(asr_start, seg_start) > 0.001:
                    text_value = str(asr_seg.get("text") or asr_seg.get("clean_text") or "").strip()
                    if text_value:
                        local_speech_parts.append(text_value)
            sub["speech"] = " ".join(local_speech_parts)
            sub["asr_text"] = sub["speech"]
            sub["asr_digest"] = sub["speech"]
            expanded.append(sub)
    return expanded


def _should_split_visual_chunk(chunk: dict[str, Any], shots: list[dict[str, Any]]) -> bool:
    visual_text = _t2s(" ".join(
        [str(chunk.get("visual") or ""), _screen_text(chunk)]
        + [str(shot.get("visual") or "") for shot in shots]
    ))
    if _hit(visual_text, NON_TARGET_PROMO_MARKERS + SPONSOR_CARD_KEYWORDS):
        return True
    if any(term in visual_text for term in ("节目宣传", "宣传画面", "广告画面", "赞助宣传", "传播矩阵")):
        return True
    roles = {str(shot.get("content_role") or "").strip().lower() for shot in shots if shot.get("content_role")}
    identities = {str(shot.get("program_identity") or "").strip() for shot in shots if shot.get("program_identity")}
    # 主持人→现场等普通新闻镜头变化不能触发拆分；旧 frame schema 没有逐帧 speech/bug，
    # 把所有异质新闻镜头都拆开会丢掉语音归属。P0 只拆明确杂质或结构化角色/节目身份变化。
    return len(roles) > 1 or len(identities) > 1


def _normalized_frame_shots(chunk: dict[str, Any], start: float, end: float) -> list[dict[str, Any]]:
    raw_shots = chunk.get("frame_shots") or []
    if not isinstance(raw_shots, list):
        return []
    duration = max(0.0, end - start)
    shots: list[dict[str, Any]] = []
    seen: set[float] = set()
    for raw in raw_shots:
        if not isinstance(raw, dict):
            continue
        t = _num(raw.get("t"), start)
        if t < start - 0.5 and t <= duration + 0.5:
            t = start + t
        t = min(max(t, start), end)
        t = round(t, 3)
        if t in seen:
            continue
        seen.add(t)
        shot = dict(raw)
        shot["t"] = t
        shots.append(shot)
    shots.sort(key=lambda item: float(item.get("t", 0.0)))
    return shots


_T2S_CONVERTER = None
_T2S_TRIED = False
_T2S_FALLBACK = str.maketrans({
    "鳳": "凤",
    "連": "连",
    "線": "线",
    "華": "华",
    "潤": "润",
    "資": "资",
    "訊": "讯",
    "國": "国",
    "換": "换",
    "衛": "卫",
    "視": "视",
    "臺": "台",
    "節": "节",
})


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
        return text.translate(_T2S_FALLBACK)
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
    visual_summary: str,
    bug: str,
    scene: str,
    footage_types: list[str],
    content_role: str,
    program_identity: str,
    program_mentions: list[dict[str, Any]],
    has_speech: bool,
    target: str,
    known_columns: list[str],
    cfg: dict[str, Any],
) -> tuple[str | None, str, str, str]:
    """返回 (label, keep_state, reason, decision_state)。

    目标栏目“归属证据”和“名称提及”严格分开：角标、独占片头标题、结构化
    program_identity 可以证明归属；ASR、手机菜单、传播矩阵里的栏目名只能算提及。
    当前画面的强杂质证据优先于任何栏目名提及。
    """
    speech_text = _t2s(speech)
    # screen_text/frame visual 是画面表层事实；visual_summary 是模型语义说明，可能包含
    # “ASR提及X但画面未出现”这类否定句，不能把摘要中的每个栏目名/预告词都当成OCR命中。
    visual_surface_text = _t2s(f"{screen_text} {scene} {' '.join(footage_types or [])}".strip())
    visual_semantic_text = _t2s(visual_summary)
    visual_text = f"{visual_surface_text} {visual_semantic_text}".strip()
    text = f"{speech_text} {visual_surface_text}".strip()
    role = str(content_role or "").strip().lower()
    identity = _t2s(str(program_identity or "").strip())
    is_pkg = (bool(scene) and any(p in scene for p in PACKAGING_SCENES)) or any(
        f in PACKAGING_FOOTAGE for f in (footage_types or [])
    )

    target_by_bug = bool(bug and target and column_match(bug, target))
    target_in_visual = bool(target and column_match(visual_surface_text, target))
    target_in_speech = bool(target and column_match(speech_text, target))
    target_by_identity = bool(identity and target and column_match(identity, target))

    mention_roles = {
        str(item.get("role") or "").strip().lower()
        for item in (program_mentions or [])
        if isinstance(item, dict) and target and column_match(str(item.get("name") or ""), target)
    }
    weak_mention_roles = {"menu_item", "list_item", "caption", "other", "spoken_reference"}
    platform_kw = _hit(visual_text, PLATFORM_PROMO_KEYWORDS)
    copyright_kw = _hit(visual_text, COPYRIGHT_SLATE_KEYWORDS)
    weak_target_mention = bool(
        target_in_visual
        and (platform_kw or role in {"platform_promo", "channel_promo", "other_program_promo"}
             or bool(mention_roles & weak_mention_roles))
    )
    # 视觉中的目标名只有在不是菜单/列表/平台宣传时才可作为节目自身标题；speech 命中永不单独保留。
    target_visual_ownership = target_by_identity or (target_in_visual and not weak_target_mention)

    detected_other = ""
    visual_other = ""
    if bug and target and not column_match(bug, target):
        detected_other = _t2s(bug)
    if not detected_other:
        for col in known_columns:
            if col == target:
                continue
            if column_match(visual_surface_text, col):
                visual_other = col
                detected_other = col
                break
            if column_match(text, col):
                detected_other = col
                break
    mentions_other = bool(detected_other)
    # 《节目名》书名号：出现非目标栏目的《X》，是“在预告/提及别的节目”的强信号
    #（不依赖排期表是否收录该节目，凤凰全球连线/凤凰早班车等中文台节目也能识别）
    visual_titles = [t for t in re.findall(r"《([^》]{2,15})》", visual_surface_text) if not column_match(t, target)]
    speech_titles = [t for t in re.findall(r"《([^》]{2,15})》", speech_text) if not column_match(t, target)]
    bracket_titles = visual_titles + [t for t in speech_titles if t not in visual_titles]
    other_title = bracket_titles[0] if bracket_titles else ""

    promo_kw = _hit(text, PROMO_KEYWORDS + cfg.get("extra_promo_keywords", []))
    visual_promo_kw = _hit(visual_surface_text, PROMO_KEYWORDS + cfg.get("extra_promo_keywords", []))
    visual_non_target_marker = _hit(visual_surface_text, NON_TARGET_PROMO_MARKERS)
    if visual_non_target_marker and target and column_match(visual_non_target_marker, target):
        visual_non_target_marker = None
    visual_sponsor_card = _hit(visual_surface_text, SPONSOR_CARD_KEYWORDS)
    sponsor_card = visual_sponsor_card or _hit(text, SPONSOR_CARD_KEYWORDS)
    strong_visual_junk = bool(
        visual_other or visual_titles or visual_promo_kw or visual_non_target_marker
        or visual_sponsor_card or platform_kw or copyright_kw or role in HARD_DROP_CONTENT_ROLES
    )

    detected_target = target_by_bug or target_visual_ownership

    # “新闻量”只能来自当前段自己的 speech。视觉描述和父段 speech_context 不能冒充新闻正文。
    residual = _residual_news_len(speech_text, target, known_columns)
    news_substantial = residual >= int(cfg.get("min_news_chars", 12) or 12)

    # 1) 结构化/画面级强杂质。优先级高于目标角标或名称提及。
    if role in {"platform_promo", "channel_promo", "other_program_promo"} or platform_kw:
        return "promo", "drop", f"频道/平台宣传：{platform_kw or role}", STATE_HARD_DROP
    if role == "copyright_slate" or copyright_kw:
        return "packaging_other", "drop", f"频道版权/台标卡：{copyright_kw or role}", STATE_HARD_DROP
    if role in {"sponsor_card", "commercial_ad", "station_id"}:
        label = "sponsor" if role == "sponsor_card" else ("ad" if role == "commercial_ad" else "station_id")
        return label, "drop", f"画面内容角色：{role}", STATE_HARD_DROP
    if sponsor_card and (visual_sponsor_card or not news_substantial):
        return "sponsor", "drop", f"赞助/冠名卡：{sponsor_card[:16]}", STATE_HARD_DROP

    if strong_visual_junk and (visual_non_target_marker or visual_titles or visual_other or visual_promo_kw):
        reason_extra = f"《{visual_titles[0]}》" if visual_titles else (visual_other or visual_non_target_marker or visual_promo_kw)
        return "promo", "drop", f"其他栏目/频道宣传：{reason_extra}".strip(), STATE_HARD_DROP

    # 2) 语音赞助：当前 speech 内确有大量新闻时不直接删正文，但它也不能成为目标栏目证据。
    sponsor_m = _SPONSOR_RE.search(text)
    if sponsor_m and not news_substantial:
        return "sponsor", "drop", f"赞助/冠名卡：{sponsor_m.group(0)[:16]}", STATE_HARD_DROP

    # 3) 预告/宣传。
    if promo_kw and (mentions_other or other_title or not detected_target) and (not news_substantial or not detected_target):
        reason_extra = f"《{other_title}》" if other_title else (f"其他栏目:{detected_other}" if mentions_other else "")
        return "promo", "drop", f"预告/宣传卡：{promo_kw} {reason_extra}".strip(), STATE_HARD_DROP
    if _hit(text, TRAILER_KEYWORDS) and not detected_target and not news_substantial:
        return "trailer_other", "drop", "片尾结束语（非目标栏目）", STATE_HARD_DROP

    # 4) 商业广告。
    ad_kw = _hit(text, AD_ACTION_KEYWORDS + cfg.get("extra_ad_keywords", []))
    if ad_kw and not news_substantial:
        return "ad", "drop", f"广告卡：{ad_kw}", STATE_HARD_DROP
    if _hit(text, SHOPPING_KEYWORDS) and not detected_target and not news_substantial:
        return "ad", "drop", "保健品/夸大话术", STATE_HARD_DROP

    # 5) 目标栏目强归属：本地角标 / program_identity / 非菜单式独占标题。
    if detected_target:
        if is_pkg:
            return "target_intro_outro", "keep", "目标栏目片头/片尾（本地视觉归属）", STATE_STRONG_KEEP
        return LABEL_TARGET, "keep", f"目标栏目角标/标题：{_t2s(bug) or target}", STATE_STRONG_KEEP

    # 6) 其他栏目。
    if mentions_other:
        return "other_column", "drop", f"其他栏目：{detected_other}", STATE_HARD_DROP

    # 7) 片尾职员表没有栏目名时只作为边界候选，需由紧邻强目标段确认，不能自行保留。
    credits_kw = _hit(visual_text, CREDITS_KEYWORDS)
    if credits_kw:
        return "target_intro_outro", "boundary_candidate", f"片尾职员表候选：{credits_kw}", STATE_BOUNDARY_CANDIDATE

    # 8) 通用包装/台标卡，没有节目归属即删。target_in_speech 只记录为提及，不会救回。
    if is_pkg:
        mention = "；语音仅提及目标栏目" if target_in_speech else ""
        return "packaging_other", "drop", f"包装/台标镜头（无目标视觉归属）：{scene or '/'.join(footage_types or [])}{mention}", STATE_HARD_DROP

    # 9) 静音卡/无实质。
    if not has_speech:
        event_types = {"主持人口播", "演播室", "记者连线", "人物讲话", "发布会", "现场画面", "资料画面", "图表地图", "其他B-roll"}
        if scene in event_types or any(f in event_types for f in (footage_types or [])):
            return None, "undecided", "无本地语音但画面像正片，待上下文确认", STATE_AMBIGUOUS
        return "station_id", "drop", "无角标、无语音（疑似台标/垫片）", STATE_HARD_DROP

    # 10) 无角标但有本地连续解说：交 LLM / 有界 bridge。语音提到目标名仍只是辅助。
    reason = "无目标视觉归属、有解说，待判"
    if target_in_speech:
        reason += "（语音提及目标栏目）"
    return None, "undecided", reason, STATE_AMBIGUOUS


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

    # 目标栏目先按父 chunk 的持续角标解析；视觉拆分会把 chunk 角标收窄到本地帧，
    # 若先拆再投票会因子段数/角标清空而改变目标栏目。
    target, target_source = resolve_target_column(
        chunks=chunks,
        schedule=schedule,
        source_items=source_items,
        override=override_column,
        mode=fc_cfg.get("target_column_mode", "auto"),
    )
    input_chunk_count = len(chunks)
    chunks = _expand_visual_subsegments(chunks)

    segments: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            continue
        source_id, start, end = seg_time(chunk)
        if end <= start:
            continue
        speech = _speech(chunk)
        speech_context = _speech_context(chunk)
        screen_text = _screen_text(chunk)
        visual_summary = _visual_summary(chunk)
        bug = _column_bug(chunk)
        scene = _scene(chunk)
        footage_types = chunk.get("footage_types") or []
        content_role = _content_role(chunk)
        program_identity = str(chunk.get("program_identity") or "").strip()
        program_mentions = chunk.get("program_mentions") if isinstance(chunk.get("program_mentions"), list) else []
        segment_id = str(chunk.get("segment_id") or chunk.get("chunk_id") or f"{source_id}_seg_{index:04d}")
        label, keep_state, reason, decision_state = _classify_segment(
            speech=speech, screen_text=screen_text, visual_summary=visual_summary, bug=bug, scene=scene,
            footage_types=footage_types, content_role=content_role, program_identity=program_identity,
            program_mentions=program_mentions, has_speech=bool(speech.strip()),
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
            "speech_context": speech_context[:200],
            "target_mention": bool(target and (column_match(speech, target) or column_match(speech_context, target))),
            "screen_text": screen_text[:200],
            "visual_summary": visual_summary[:400],
            "column_bug": bug,
            "scene": scene,
            "content_role": content_role,
            "program_identity": program_identity,
            "parent_segment_id": chunk.get("parent_segment_id", ""),
            "label": label or "unknown",
            "keep_state": keep_state,
            "decision_state": decision_state,
            "reason": reason,
            "decided_by": "rule" if decision_state in {STATE_HARD_DROP, STATE_STRONG_KEEP} else "pending",
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
                        "speech_context": r.get("speech_context", ""),
                        "screen_text": r["screen_text"],
                        "visual_summary": r.get("visual_summary", ""),
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

    _resolve_boundary_candidates(segments, fc_cfg)
    _resolve_keep(segments, fc_cfg)
    vip = detect_vip(segments, vip_names)
    blocks = _merge_drop_blocks(segments)
    stats = _build_stats(segments, blocks, llm_used=llm_used, target=target)
    stats.update({
        "input_chunk_count": input_chunk_count,
        "expanded_segment_count": len(segments),
        "split_parent_count": len({r.get("parent_segment_id") for r in segments if r.get("parent_segment_id")}),
    })
    return {
        "segments": segments,
        "blocks": blocks,
        "stats": stats,
        "target_column": target,
        "target_column_source": target_source,
        "vip": vip,
    }


def _resolve_boundary_candidates(segments: list[dict[str, Any]], fc_cfg: dict[str, Any]) -> None:
    """用紧邻的强目标段确认无栏目名的片头/职员表候选。

    generic packaging / sponsor / platform promo 已在本地分类阶段 hard-drop，不会进入这里。
    候选链必须同源、连续、总时长有上限，避免“片尾”标签向频道包装无限延伸。
    """
    max_total = float(fc_cfg.get("boundary_candidate_max_seconds", 30.0) or 0.0)
    max_gap = min(float(fc_cfg.get("bridge_max_gap_seconds", 1.0) or 0.0), 1.0)
    by_source: dict[str, list[dict[str, Any]]] = {}
    for rec in segments:
        by_source.setdefault(str(rec["source_id"]), []).append(rec)
    for recs in by_source.values():
        recs.sort(key=lambda r: float(r["start_seconds"]))
        i = 0
        while i < len(recs):
            if recs[i].get("keep_state") != "boundary_candidate":
                i += 1
                continue
            j = i + 1
            while j < len(recs) and recs[j].get("keep_state") == "boundary_candidate":
                if float(recs[j]["start_seconds"]) - float(recs[j - 1]["end_seconds"]) > max_gap:
                    break
                j += 1
            chain = recs[i:j]
            total = sum(float(r.get("duration_seconds", 0)) for r in chain)
            left = recs[i - 1] if i > 0 else None
            right = recs[j] if j < len(recs) else None
            left_anchor = bool(
                left and left.get("decision_state") == STATE_STRONG_KEEP
                and float(chain[0]["start_seconds"]) - float(left["end_seconds"]) <= max_gap
            )
            right_anchor = bool(
                right and right.get("decision_state") == STATE_STRONG_KEEP
                and float(right["start_seconds"]) - float(chain[-1]["end_seconds"]) <= max_gap
            )
            keep_chain = bool((left_anchor or right_anchor) and total <= max_total)
            for rec in chain:
                if keep_chain:
                    rec["keep_state"] = "keep"
                    rec["decision_state"] = STATE_STRONG_KEEP
                    rec["decided_by"] = "context_rule"
                    rec["reason"] = f"{rec.get('reason', '')}; 紧邻目标栏目强归属段，确认为目标片头/片尾".strip("; ")
                else:
                    rec["keep_state"] = "drop"
                    rec["decision_state"] = STATE_HARD_DROP
                    rec["label"] = "packaging_other"
                    rec["decided_by"] = "context_rule"
                    rec["reason"] = f"{rec.get('reason', '')}; 缺少相邻目标归属或候选链过长".strip("; ")
            i = j


def _resolve_keep(segments: list[dict[str, Any]], fc_cfg: dict[str, Any]) -> None:
    """解析有界 bridge。

    undecided 只按“整条候选链”处理：双侧目标 anchor 的内部链可保留；素材边缘允许
    一侧 anchor，但有独立的总时长上限。hard-drop、source boundary、时间断裂都会阻断。
    """
    bridge = bool(fc_cfg.get("bridge_keep_within_run", True))
    max_gap = float(fc_cfg.get("bridge_max_gap_seconds", 90.0) or 0.0)
    max_total = float(fc_cfg.get("bridge_max_total_seconds", 30.0) or 0.0)
    edge_max = float(fc_cfg.get("bridge_edge_max_seconds", 30.0) or 0.0)
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
        i = 0
        while i < len(recs):
            if recs[i].get("keep") is not None:
                i += 1
                continue
            j = i + 1
            while j < len(recs) and recs[j].get("keep") is None:
                if float(recs[j]["start_seconds"]) - float(recs[j - 1]["end_seconds"]) > max_gap:
                    break
                j += 1
            chain = recs[i:j]
            total = sum(float(r.get("duration_seconds", 0)) for r in chain)
            left = recs[i - 1] if i > 0 else None
            right = recs[j] if j < len(recs) else None
            attach_left = bool(
                left and left.get("keep") is True
                and float(chain[0]["start_seconds"]) - float(left["end_seconds"]) <= max_gap
            )
            attach_right = bool(
                right and right.get("keep") is True
                and float(right["start_seconds"]) - float(chain[-1]["end_seconds"]) <= max_gap
            )
            interior = attach_left and attach_right and total <= max_total
            at_source_edge = i == 0 or j == len(recs)
            edge_extension = at_source_edge and (attach_left or attach_right) and total <= edge_max
            # 赞助卡/包装刚结束后，目标角标可能晚几秒出现。允许“整链都有目标语音提示”的
            # 正片样画面向单侧强 anchor 收口，但目标名提及本身仍不能在无 anchor 时直接 keep。
            contextual_one_sided = bool(
                (attach_left ^ attach_right)
                and total <= edge_max
                and all(bool(r.get("target_mention")) for r in chain)
            )
            keep_chain = bool(interior or edge_extension or contextual_one_sided)
            for rec in chain:
                if keep_chain:
                    rec["keep"] = True
                    rec["label"] = "bridge"
                    rec["reason"] = f"{rec.get('reason', '')}; 有界连续段（总长{total:.1f}s）紧邻目标栏目".strip("; ")
                else:
                    rec["keep"] = False
                    rec["label"] = "unknown_drop"
                    if total > (edge_max if at_source_edge else max_total):
                        rec["reason"] = f"{rec.get('reason', '')}; bridge候选链过长({total:.1f}s)".strip("; ")
            i = j


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
        drops = dropped_by_source.get(source_id, [])
        merged: list[list[float]] = []
        reasons: list[list[str]] = []
        for r in recs:
            s, e = float(r["start_seconds"]), float(r["end_seconds"])
            gap_start = merged[-1][1] if merged else s
            explicit_drop_in_gap = any(
                d_start < s - 1e-6 and d_end > gap_start + 1e-6
                for d_start, d_end in drops
            )
            if merged and s - merged[-1][1] <= merge_gap and not explicit_drop_in_gap:
                merged[-1][1] = max(merged[-1][1], e)
                reasons[-1].append(str(r.get("segment_id")))
            else:
                merged.append([s, e])
                reasons.append([str(r.get("segment_id"))])

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
