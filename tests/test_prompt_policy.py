from __future__ import annotations

from newsclip_agent import prompts


def test_allow_long_video_softens_default_30_seconds() -> None:
    assert "target_duration_seconds=30 只表示默认参考，不是硬限制" in prompts.SHORT_VIDEO_DURATION_POLICY
    assert "目标是在 60 秒以内讲清楚" in prompts.SHORT_VIDEO_DURATION_POLICY


def test_voiceover_requires_narrative_completion_for_complex_news() -> None:
    assert "起、承、转、合" in prompts.VOICEOVER_PROMPT
    assert "target_duration_seconds >= 45" in prompts.VOICEOVER_PROMPT
    assert "不得只写 40-60 字的摘要式文案" in prompts.VOICEOVER_PROMPT
