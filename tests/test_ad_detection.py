"""ad_detection 离线单测（无 GPU / 无网络）。

覆盖：规则层命中与新闻保护、detect 端到端 + mock LLM、build_plan 时序/合并/安全余量/最短片段、
保守保留 suspected、孤立超短广告块保护、文件名时间戳排序。
"""

from __future__ import annotations

from newsclip_agent import ad_detection as ad


# load_cfg 接受 ProjectConfig（有 .raw）或直接传 ad_detection 段 dict（测试用）
CFG = ad.load_cfg({})


def _seg(seg_id, src, start, end, speech="", scene="", screen=None):
    chunk = {
        "segment_id": seg_id,
        "source_id": src,
        "source_index": 1,
        "local_start_seconds": start,
        "local_end_seconds": end,
        "speech": speech,
        "scene": scene,
    }
    if screen is not None:
        chunk["screen_text"] = screen
    return chunk


# --------------------------------------------------------------------------- #
# 规则层
# --------------------------------------------------------------------------- #
def test_rule_ad_action_keyword():
    v = ad.classify_by_rules(speech="现在拨打热线即可享受限时优惠", screen_text="", scene="", cfg=CFG)
    assert v is not None and v[0] == "ad" and v[1] == "high"


def test_rule_sponsor():
    v = ad.classify_by_rules(speech="本节目由康师傅赞助播出", screen_text="", scene="", cfg=CFG)
    assert v is not None and v[0] == "sponsor"


def test_rule_promo():
    v = ad.classify_by_rules(speech="精彩继续，敬请收看", screen_text="", scene="", cfg=CFG)
    assert v is not None and v[0] == "promo"


def test_rule_packaging_scene_trailer():
    v = ad.classify_by_rules(speech="", screen_text="凤凰资讯", scene="片头包装", cfg=CFG)
    assert v is not None and v[0] == "trailer"


def test_rule_station_id_when_silent_blank():
    v = ad.classify_by_rules(speech="", screen_text="", scene="其他B-roll", cfg=CFG)
    assert v is not None and v[0] == "station_id"


def test_rule_news_protection_finance():
    # 财经新闻提到公司但无购买行动号召 → 不应判广告
    v = ad.classify_by_rules(
        speech="据悉某科技公司今日股价上涨，记者从发布会了解到", screen_text="", scene="演播室", cfg=CFG
    )
    assert v is not None and v[0] == "news"


def test_rule_ambiguous_returns_none():
    v = ad.classify_by_rules(speech="今天天气不错大家心情都很好", screen_text="", scene="", cfg=CFG)
    assert v is None


# --------------------------------------------------------------------------- #
# detect 端到端
# --------------------------------------------------------------------------- #
def test_detect_default_keeps_ambiguous_as_news():
    chunks = [_seg("s1", "source_1", 0, 10, speech="今天大家都很开心")]
    result = ad.detect(chunks, cfg=CFG, llm_call=None)
    seg = result["segments"][0]
    assert seg["label"] == "news" and seg["keep"] is True
    assert result["stats"]["removed_seconds"] == 0


def test_detect_llm_flips_ambiguous_to_ad():
    chunks = [_seg("s1", "source_1", 0, 10, speech="买它买它超值套餐")]

    def fake_llm(payload):
        return [{"segment_id": p["segment_id"], "label": "ad", "confidence": "high", "reason": "购物话术"} for p in payload]

    result = ad.detect(chunks, cfg=CFG, llm_call=fake_llm)
    seg = result["segments"][0]
    assert seg["label"] == "ad" and seg["keep"] is False
    assert result["stats"]["llm_used"] is True


def test_detect_llm_failure_keeps_conservative():
    chunks = [_seg("s1", "source_1", 0, 10, speech="模糊内容")]

    def boom(payload):
        raise RuntimeError("network down")

    result = ad.detect(chunks, cfg=CFG, llm_call=boom)
    assert result["segments"][0]["keep"] is True  # LLM 挂了不误删


def test_detect_drops_strong_ad_block():
    chunks = [
        _seg("a", "source_1", 0, 30, speech="据悉发布会现场记者报道"),
        _seg("b", "source_1", 30, 70, speech="拨打订购热线扫码抢购仅售99元"),
        _seg("c", "source_1", 70, 100, speech="本台消息外交部声明"),
    ]
    result = ad.detect(chunks, cfg=CFG, llm_call=None)
    labels = {s["segment_id"]: s["keep"] for s in result["segments"]}
    assert labels["a"] is True and labels["c"] is True and labels["b"] is False
    assert result["stats"]["removed_seconds"] == 40


# --------------------------------------------------------------------------- #
# 孤立超短广告块保护
# --------------------------------------------------------------------------- #
def test_isolated_short_ad_block_kept():
    cfg = ad.load_cfg({"min_isolated_ad_seconds": 5.0})
    chunks = [
        _seg("a", "source_1", 0, 30, speech="记者现场报道"),
        _seg("b", "source_1", 30, 32, speech="扫码抢购"),  # 2s 广告，短于 5s
        _seg("c", "source_1", 32, 60, speech="本台消息"),
    ]
    result = ad.detect(chunks, cfg=cfg, llm_call=None)
    b = next(s for s in result["segments"] if s["segment_id"] == "b")
    assert b["keep"] is True and b.get("suspected") is True


# --------------------------------------------------------------------------- #
# build_plan
# --------------------------------------------------------------------------- #
def test_build_plan_chronological_across_sources():
    segments = [
        {"segment_id": "x", "source_id": "src_b", "source_index": 2, "start_seconds": 0, "end_seconds": 20,
         "duration_seconds": 20, "label": "news", "keep": True},
        {"segment_id": "y", "source_id": "src_a", "source_index": 1, "start_seconds": 0, "end_seconds": 15,
         "duration_seconds": 15, "label": "news", "keep": True},
    ]
    plan = ad.build_plan(segments, source_order=["src_a", "src_b"], cfg=CFG)
    assert [c["source_id"] for c in plan["clips"]] == ["src_a", "src_b"]
    # target_start 累计：第一段 0，第二段从 15 开始
    assert plan["clips"][0]["target_start_seconds"] == 0
    assert plan["clips"][1]["target_start_seconds"] == 15
    assert plan["video_duration_seconds"] == 35


def test_build_plan_merges_adjacent_kept():
    segments = [
        {"segment_id": "a", "source_id": "s", "start_seconds": 0, "end_seconds": 10, "duration_seconds": 10,
         "label": "news", "keep": True},
        {"segment_id": "b", "source_id": "s", "start_seconds": 10, "end_seconds": 20, "duration_seconds": 10,
         "label": "news", "keep": True},
    ]
    plan = ad.build_plan(segments, source_order=["s"], cfg={"merge_gap_seconds": 1.5, "min_clip_seconds": 3})
    assert len(plan["clips"]) == 1
    assert plan["clips"][0]["duration_seconds"] == 20


def test_build_plan_safety_margin_trims_boundary_touching_ad():
    cfg = {"safety_margin_seconds": 0.5, "merge_gap_seconds": 0.0, "min_clip_seconds": 1.0}
    segments = [
        {"segment_id": "ad", "source_id": "s", "start_seconds": 0, "end_seconds": 10, "duration_seconds": 10,
         "label": "ad", "keep": False},
        {"segment_id": "news", "source_id": "s", "start_seconds": 10, "end_seconds": 30, "duration_seconds": 20,
         "label": "news", "keep": True},
    ]
    plan = ad.build_plan(segments, source_order=["s"], cfg=cfg)
    clip = plan["clips"][0]
    # 起点紧贴广告块结束(10s) → 内缩 0.5s
    assert clip["local_start_seconds"] == 10.5
    assert clip["local_end_seconds"] == 30  # 末尾不贴广告，不缩


def test_build_plan_drops_too_short_clip():
    cfg = {"safety_margin_seconds": 0.0, "merge_gap_seconds": 0.0, "min_clip_seconds": 5.0}
    segments = [
        {"segment_id": "n", "source_id": "s", "start_seconds": 0, "end_seconds": 2, "duration_seconds": 2,
         "label": "news", "keep": True},
    ]
    plan = ad.build_plan(segments, source_order=["s"], cfg=cfg)
    assert plan["clips"] == []
    assert plan["duration_status"] == "blocked"


# --------------------------------------------------------------------------- #
# 源时间戳排序
# --------------------------------------------------------------------------- #
def test_parse_source_timestamp():
    assert ad.parse_source_timestamp("20260603_173438") == "20260603173438"
    assert ad.parse_source_timestamp("clip_20260603_173438_high.mp4") == "20260603173438"
    assert ad.parse_source_timestamp("noidea") == ""


def test_order_sources_by_timestamp():
    items = [
        {"source_id": "b", "display_name": "20260603_173438"},
        {"source_id": "a", "display_name": "20260603_170438"},
        {"source_id": "c", "display_name": "20260603_171438"},
    ]
    assert ad.order_sources_by_timestamp(items) == ["a", "c", "b"]
