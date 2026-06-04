from __future__ import annotations

from newsclip_agent import prompts


def test_allow_long_video_softens_default_30_seconds() -> None:
    assert "不要输出目标时长" in prompts.SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT
    assert "代码计算实际时长" in prompts.SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT


def test_voiceover_requires_narrative_completion_for_complex_news() -> None:
    assert "40 秒以上视频应包含：开头、必要背景、核心事实、影响或冲突、当前进展" in prompts.VOICEOVER_SCRIPT_TEXT_PROMPT
    assert "visual_total_seconds" in prompts.VOICEOVER_SCRIPT_TEXT_PROMPT
    assert "shots" in prompts.VOICEOVER_SCRIPT_TEXT_PROMPT
    assert '"narration_segments"' in prompts.VOICEOVER_SCRIPT_TEXT_PROMPT
    assert "不使用“震惊”“万万没想到”“全网炸锅”" in prompts.VOICEOVER_SCRIPT_TEXT_PROMPT


def test_model_outputs_minimal_editing_schema() -> None:
    assert '"clip_ids"' in prompts.SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT
    assert prompts.SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT.strip().endswith("]")
    assert '"clip_ids"' in prompts.HIGHLIGHT_REASSEMBLY_TEXT_PROMPT
    assert prompts.HIGHLIGHT_REASSEMBLY_TEXT_PROMPT.strip().endswith("]")


def test_video_understanding_outputs_only_understanding() -> None:
    assert '"understanding": ""' in prompts.VIDEO_UNDERSTANDING_PROMPT
    assert '"main_topic"' not in prompts.VIDEO_UNDERSTANDING_PROMPT
    assert '"key_facts"' not in prompts.VIDEO_UNDERSTANDING_PROMPT
    assert "只输出 JSON，不要输出 Markdown，不要解释，不要新增字段" in prompts.VIDEO_UNDERSTANDING_PROMPT


def test_vision_chunk_outputs_only_visual() -> None:
    assert '"visual": ""' in prompts.VISION_CHUNK_PROMPT
    assert '"visual_summary"' not in prompts.VISION_CHUNK_PROMPT
    assert '"scene_type"' not in prompts.VISION_CHUNK_PROMPT
    assert "不要把 ASR 内容当成画面事实" in prompts.VISION_CHUNK_PROMPT


def test_multi_source_boundary_policy_is_embedded_without_extra_fields() -> None:
    assert "本规则只影响 clip_ids 的选择和排序，不要求输出任何额外字段" in prompts.SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT
    assert "只输出 JSON，不要解释，不要新增字段" in prompts.HIGHLIGHT_REASSEMBLY_TEXT_PROMPT
    assert "资料画面被误当现场" in prompts.SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT
    assert "避免把两个不同 source 的半句话拼成连续原声" in prompts.HIGHLIGHT_REASSEMBLY_TEXT_PROMPT


def test_asr_digest_only_outputs_speech() -> None:
    assert "asr_text：当前 chunk 的原始 ASR 文本" in prompts.ASR_DIGEST_PROMPT
    assert "只输出 JSON，不要输出 Markdown，不要解释，不要新增字段" in prompts.ASR_DIGEST_PROMPT
    assert '"speech": ""' in prompts.ASR_DIGEST_PROMPT
    assert "不输出人物列表、地点列表、事实列表" in prompts.ASR_DIGEST_PROMPT


def test_content_analysis_outputs_only_candidate_chunks() -> None:
    assert '"chunk_ids"' in prompts.CONTENT_ANALYSIS_PROMPT
    assert '"summary"' in prompts.CONTENT_ANALYSIS_PROMPT
    assert "不要输出 start、end、duration_seconds、clip_id、source_id、type" in prompts.CONTENT_ANALYSIS_PROMPT
    assert "不要新增字段" in prompts.CONTENT_ANALYSIS_PROMPT
    assert prompts.CONTENT_ANALYSIS_PROMPT.strip().endswith("]")


def test_highlight_detection_outputs_only_candidate_chunks() -> None:
    assert '"chunk_ids"' in prompts.HIGHLIGHT_DETECTION_PROMPT
    assert '"summary"' in prompts.HIGHLIGHT_DETECTION_PROMPT
    assert "不要输出 start、end、duration_seconds、clip_id、source_id、type" in prompts.HIGHLIGHT_DETECTION_PROMPT
    assert "优先选择声音信息完整、句子自然、画面能支撑新闻事实的片段" in prompts.HIGHLIGHT_DETECTION_PROMPT
    assert prompts.HIGHLIGHT_DETECTION_PROMPT.strip().endswith("]")
