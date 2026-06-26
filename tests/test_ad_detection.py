"""ad_detection（按栏目过滤版）离线单测，无 GPU/网络。

覆盖：栏目名匹配、时间戳解析、排期推断、目标栏目解析、detect 逐段判定（目标/其他栏目/广告/bridge兜底）、
mock LLM、VIP 检测、build_plan 时序/合并/安全余量/最短片段、源时间戳排序、VIP 打底名单。
"""

from __future__ import annotations

from newsclip_agent import ad_detection as ad

CFG = ad.load_cfg({})
FC = ad.load_full_concat_cfg({})

# 测试排期：各档都设为每天，避免依赖具体星期
SCHEDULE = ad.load_schedule({
    "enabled": True,
    "entries": [
        {"column": "纪录大时代", "time": "16:30", "days": [0, 1, 2, 3, 4, 5, 6]},
        {"column": "凤凰大视野", "time": "20:00", "days": [0, 1, 2, 3, 4, 5, 6]},
        {"column": "凤凰全球连线", "time": "21:00", "days": [0, 1, 2, 3, 4, 5, 6]},
    ],
})


def _chunk(cid, src, start, end, speech="", bug="", scene="", screen=None, footage=None):
    c = {
        "segment_id": cid,
        "source_id": src,
        "source_index": 1,
        "local_start_seconds": start,
        "local_end_seconds": end,
        "speech": speech,
        "column_bug": bug,
        "scene": scene,
    }
    if screen is not None:
        c["screen_text"] = screen
    if footage is not None:
        c["footage_types"] = footage
    return c


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def test_column_match():
    assert ad.column_match("纪录大时代", "纪录大时代")
    assert ad.column_match("现在播出 纪录大时代 第4集", "纪录大时代")
    assert not ad.column_match("凤凰大视野", "纪录大时代")


def test_column_match_traditional_to_simplified():
    # 凤凰画面是繁体，目标是简体，应繁简不敏感匹配（需 opencc）
    assert ad.column_match("鳳凰聚焦", "凤凰聚焦")
    assert ad.column_match("鳳凰全球連線", "凤凰全球连线")


def test_parse_source_timestamp():
    assert ad.parse_source_timestamp("20260603_173438") == "20260603173438"
    assert ad.parse_source_timestamp("clip_20260603_173438_high.mp4") == "20260603173438"
    assert ad.parse_source_timestamp("noidea") == ""


def test_infer_column_from_schedule():
    # 17:00 → 最近一档是 16:30 纪录大时代
    assert ad.infer_column_from_schedule(SCHEDULE, "20260603_170000") == "纪录大时代"
    # 20:30 → 最近一档是 20:00 凤凰大视野
    assert ad.infer_column_from_schedule(SCHEDULE, "20260603_203000") == "凤凰大视野"
    # 21:00 → 最近一档是 21:00 凤凰全球连线
    assert ad.infer_column_from_schedule(SCHEDULE, "20260603_210000") == "凤凰全球连线"


# --------------------------------------------------------------------------- #
# 目标栏目解析
# --------------------------------------------------------------------------- #
def test_resolve_target_override():
    t, src = ad.resolve_target_column(chunks=[], schedule=SCHEDULE, source_items=[], override="纪录大时代", mode="auto")
    assert t == "纪录大时代" and src == "manual"


def test_resolve_target_majority_bug():
    chunks = [_chunk("a", "s", 0, 10, bug="纪录大时代"), _chunk("b", "s", 10, 20, bug="纪录大时代"), _chunk("c", "s", 20, 30, bug="凤凰大视野")]
    t, src = ad.resolve_target_column(chunks=chunks, schedule=SCHEDULE, source_items=[], override=None, mode="auto")
    assert t == "纪录大时代" and src == "column_bug"


def test_resolve_target_schedule_fallback():
    items = [{"source_id": "s1", "display_name": "20260603_163500"}]
    t, src = ad.resolve_target_column(chunks=[], schedule=SCHEDULE, source_items=items, override=None, mode="auto")
    assert t == "纪录大时代" and src == "schedule"


# --------------------------------------------------------------------------- #
# detect 逐段判定
# --------------------------------------------------------------------------- #
def test_detect_keeps_target_drops_other_and_ad():
    chunks = [
        _chunk("a", "s", 0, 30, speech="本台记者现场报道", bug="纪录大时代"),
        _chunk("b", "s", 30, 70, speech="拨打订购热线扫码抢购仅售99元"),       # 广告
        _chunk("c", "s", 70, 100, speech="敬请收看下节目", bug="凤凰大视野"),     # 其他栏目
        _chunk("d", "s", 100, 130, speech="继续报道", bug="纪录大时代"),
    ]
    res = ad.detect(chunks, cfg=CFG, fc_cfg=FC, schedule=SCHEDULE, source_items=[], override_column="纪录大时代")
    keep = {s["segment_id"]: s["keep"] for s in res["segments"]}
    assert keep["a"] is True and keep["d"] is True
    assert keep["b"] is False and keep["c"] is False
    assert res["target_column"] == "纪录大时代"
    assert res["stats"]["removed_seconds"] == 70  # b(40)+c(30)


def test_detect_traditional_bug_kept_as_target():
    # bug 是繁体"鳳凰聚焦"，目标简体"凤凰聚焦"，应判 target 保留（修复繁简误删）
    chunks = [_chunk("a", "s", 0, 30, speech="英国首相辞职相关报道", bug="鳳凰聚焦")]
    res = ad.detect(chunks, cfg={**CFG, "use_llm": False}, fc_cfg=FC, schedule=SCHEDULE, source_items=[], override_column="凤凰聚焦")
    a = res["segments"][0]
    assert a["label"] == "target" and a["keep"] is True


def test_detect_pure_sponsor_card_dropped():
    # 基本是纯赞助卡（没什么新闻）→ 删
    chunks = [_chunk("a", "s", 0, 10, speech="本节目由华润集团赞助播出", bug="凤凰聚焦")]
    res = ad.detect(chunks, cfg={**CFG, "use_llm": False}, fc_cfg=FC, schedule=SCHEDULE, source_items=[], override_column="凤凰聚焦")
    a = res["segments"][0]
    assert a["label"] == "sponsor" and a["keep"] is False


def test_detect_sponsor_with_news_kept():
    # 保新闻为先（修复开头丢失）：赞助语和大段新闻混在一起 → 保留新闻，不当赞助删
    chunks = [_chunk("a", "s", 0, 60, bug="凤凰聚焦",
                     speech="凤凰卫视《凤凰聚焦》节目由华润集团赞助播出，斯塔默宣布辞职，这是英国十年里第七次更换首相，英国政坛陷入动荡，工党面临危机")]
    res = ad.detect(chunks, cfg={**CFG, "use_llm": False}, fc_cfg=FC, schedule=SCHEDULE, source_items=[], override_column="凤凰聚焦")
    a = res["segments"][0]
    assert a["keep"] is True and a["label"] == "target"


def test_detect_promo_other_program_dropped_with_target_bug():
    # 挂着目标角标，但内容是预告其他节目（凤凰全球连线/每日七点）→ 删
    chunks = [_chunk("a", "s", 0, 30, speech="《凤凰全球连线》每日七点播出，敬请收看", bug="凤凰聚焦")]
    res = ad.detect(chunks, cfg={**CFG, "use_llm": False}, fc_cfg=FC, schedule=SCHEDULE, source_items=[], override_column="凤凰聚焦")
    a = res["segments"][0]
    assert a["label"] == "promo" and a["keep"] is False


def test_detect_packaging_footage_dropped():
    # 片头包装/台标卡（footage_types 含片头包装），非目标 → 删
    chunks = [_chunk("a", "s", 0, 8, speech="", bug="", footage=["片头包装"])]
    res = ad.detect(chunks, cfg={**CFG, "use_llm": False}, fc_cfg=FC, schedule=SCHEDULE, source_items=[], override_column="凤凰聚焦")
    a = res["segments"][0]
    assert a["keep"] is False and a["label"] == "packaging_other"


def test_detect_silent_no_bug_dropped():
    chunks = [_chunk("a", "s", 0, 6, speech="", bug="")]
    res = ad.detect(chunks, cfg={**CFG, "use_llm": False}, fc_cfg=FC, schedule=SCHEDULE, source_items=[], override_column="凤凰聚焦")
    assert res["segments"][0]["keep"] is False


def test_detect_program_open_with_target_kept():
    # 节目自己的开场（提到目标栏目、无其他节目）不应被预告规则误删
    chunks = [_chunk("a", "s", 0, 30, speech="欢迎收看本期《凤凰聚焦》，本期关注英国政坛", bug="凤凰聚焦")]
    res = ad.detect(chunks, cfg={**CFG, "use_llm": False}, fc_cfg=FC, schedule=SCHEDULE, source_items=[], override_column="凤凰聚焦")
    assert res["segments"][0]["keep"] is True


def test_detect_bridge_keeps_inner_unknown():
    chunks = [
        _chunk("a", "s", 0, 30, bug="纪录大时代"),
        _chunk("b", "s", 30, 60, speech="一段没有角标的空镜风景"),  # 模糊
        _chunk("c", "s", 60, 90, bug="纪录大时代"),
    ]
    res = ad.detect(chunks, cfg={**CFG, "use_llm": False}, fc_cfg=FC, schedule=SCHEDULE, source_items=[], override_column="纪录大时代")
    b = next(s for s in res["segments"] if s["segment_id"] == "b")
    assert b["keep"] is True and b["label"] == "bridge"


def test_detect_trailing_edge_kept():
    # 结尾紧邻目标段、角标已撤的正片片段 → 向后生长保留（修复结尾丢失）
    chunks = [
        _chunk("a", "s", 0, 30, bug="纪录大时代"),
        _chunk("b", "s", 30, 60, speech="结尾角标已撤但仍是本栏目的收尾报道内容"),
    ]
    res = ad.detect(chunks, cfg={**CFG, "use_llm": False}, fc_cfg=FC, schedule=SCHEDULE, source_items=[], override_column="纪录大时代")
    b = next(s for s in res["segments"] if s["segment_id"] == "b")
    assert b["keep"] is True and b["label"] == "bridge"


def test_detect_undecided_isolated_by_junk_dropped():
    # 模糊段被杂质段（赞助）和目标段隔开、且自身不挨目标 → 不生长，丢弃
    chunks = [
        _chunk("a", "s", 0, 30, bug="纪录大时代"),
        _chunk("x", "s", 30, 40, speech="本节目由华润集团赞助播出"),  # 纯赞助→drop，成为边界
        _chunk("b", "s", 40, 70, speech="一段无角标且与目标段被赞助卡隔开的内容"),
    ]
    res = ad.detect(chunks, cfg={**CFG, "use_llm": False}, fc_cfg=FC, schedule=SCHEDULE, source_items=[], override_column="纪录大时代")
    b = next(s for s in res["segments"] if s["segment_id"] == "b")
    assert b["keep"] is False


def test_detect_llm_flips_unknown():
    chunks = [_chunk("a", "s", 0, 30, speech="模糊内容，无角标")]

    def fake_llm(payload):
        assert payload["target_column"] == "纪录大时代"
        return [{"segment_id": p["segment_id"], "label": "ad", "reason": "购物"} for p in payload["segments"]]

    res = ad.detect(chunks, cfg=CFG, fc_cfg=FC, schedule=SCHEDULE, source_items=[], override_column="纪录大时代", llm_call=fake_llm)
    a = res["segments"][0]
    assert a["label"] == "ad" and a["keep"] is False
    assert res["stats"]["llm_used"] is True


def test_detect_llm_failure_bridge_or_drop_not_crash():
    chunks = [_chunk("a", "s", 0, 30, speech="模糊")]

    def boom(payload):
        raise RuntimeError("down")

    res = ad.detect(chunks, cfg=CFG, fc_cfg=FC, schedule=SCHEDULE, source_items=[], override_column="纪录大时代", llm_call=boom)
    # 单段、无目标邻居 → bridge 不成立 → 删；但不应崩
    assert res["segments"][0]["keep"] in (True, False)


# --------------------------------------------------------------------------- #
# VIP 检测
# --------------------------------------------------------------------------- #
def test_detect_vip_present():
    chunks = [
        _chunk("a", "s", 0, 30, speech="习近平主席今日会见外宾", bug="纪录大时代"),
        _chunk("b", "s", 30, 60, speech="扫码抢购"),  # 广告，删
    ]
    res = ad.detect(chunks, cfg=CFG, fc_cfg=FC, schedule=SCHEDULE, source_items=[], override_column="纪录大时代",
                    vip_names=ad.DEFAULT_VIP_NAMES)
    assert res["vip"]["present"] is True
    assert "习近平" in res["vip"]["names"]


def test_detect_vip_absent():
    chunks = [_chunk("a", "s", 0, 30, speech="普通新闻报道", bug="纪录大时代")]
    res = ad.detect(chunks, cfg=CFG, fc_cfg=FC, schedule=SCHEDULE, source_items=[], override_column="纪录大时代",
                    vip_names=ad.DEFAULT_VIP_NAMES)
    assert res["vip"]["present"] is False


# --------------------------------------------------------------------------- #
# build_plan
# --------------------------------------------------------------------------- #
def test_build_plan_chronological_across_sources():
    segments = [
        {"segment_id": "x", "source_id": "src_b", "source_index": 2, "start_seconds": 0, "end_seconds": 20,
         "duration_seconds": 20, "label": "target", "keep": True},
        {"segment_id": "y", "source_id": "src_a", "source_index": 1, "start_seconds": 0, "end_seconds": 15,
         "duration_seconds": 15, "label": "target", "keep": True},
    ]
    plan = ad.build_plan(segments, source_order=["src_a", "src_b"], cfg=CFG)
    assert [c["source_id"] for c in plan["clips"]] == ["src_a", "src_b"]
    assert plan["clips"][0]["target_start_seconds"] == 0
    assert plan["clips"][1]["target_start_seconds"] == 15
    assert plan["video_duration_seconds"] == 35


def test_build_plan_safety_margin():
    cfg = {"safety_margin_seconds": 0.5, "merge_gap_seconds": 0.0, "min_clip_seconds": 1.0}
    segments = [
        {"segment_id": "ad", "source_id": "s", "start_seconds": 0, "end_seconds": 10, "duration_seconds": 10,
         "label": "ad", "keep": False},
        {"segment_id": "t", "source_id": "s", "start_seconds": 10, "end_seconds": 30, "duration_seconds": 20,
         "label": "target", "keep": True},
    ]
    plan = ad.build_plan(segments, source_order=["s"], cfg=cfg)
    assert plan["clips"][0]["local_start_seconds"] == 10.5
    assert plan["clips"][0]["local_end_seconds"] == 30


def test_build_plan_drops_too_short():
    cfg = {"safety_margin_seconds": 0.0, "merge_gap_seconds": 0.0, "min_clip_seconds": 5.0}
    segments = [{"segment_id": "t", "source_id": "s", "start_seconds": 0, "end_seconds": 2, "duration_seconds": 2,
                 "label": "target", "keep": True}]
    plan = ad.build_plan(segments, source_order=["s"], cfg=cfg)
    assert plan["clips"] == [] and plan["duration_status"] == "blocked"


def test_order_sources_by_timestamp():
    items = [
        {"source_id": "b", "display_name": "20260603_173438"},
        {"source_id": "a", "display_name": "20260603_170438"},
        {"source_id": "c", "display_name": "20260603_171438"},
    ]
    assert ad.order_sources_by_timestamp(items) == ["a", "c", "b"]


def test_load_vip_names_baseline():
    names = ad.load_vip_names({"names": ["甲", "乙"]})
    assert names == ["甲", "乙"]
    # 空配置回退默认打底
    assert ad.load_vip_names({}) == ad.DEFAULT_VIP_NAMES
