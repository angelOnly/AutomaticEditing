from __future__ import annotations

from pathlib import Path


# 提示词正文统一放在 prompt_texts 目录，避免把长提示词堆在 Python 文件里。
PROMPT_DIR = Path(__file__).resolve().parent / "prompt_texts"


def _load_prompt(name: str) -> str:
    # 保留文件末尾换行，方便写入调试 prompt.txt 时阅读。
    path = PROMPT_DIR / name
    return path.read_text(encoding="utf-8").strip() + "\n"


# ASR 文本整理：把单个 chunk 的原始语音转成简短 speech 摘要。
ASR_DIGEST_PROMPT = _load_prompt("asr_digest.txt")
# 画面分析：根据关键帧生成单个 chunk 的 visual 画面摘要。
VISION_CHUNK_PROMPT = _load_prompt("vision_chunk.txt")
# 视频理解：概括整条视频的主编理解，供原声高光链路使用。
VIDEO_UNDERSTANDING_PROMPT = _load_prompt("video_understanding.txt")
# AI 配音候选片段：从时间轴中选择适合解说短视频的 chunk 组合。
CONTENT_ANALYSIS_PROMPT = _load_prompt("content_analysis.txt")
# AI 配音 micro_segment 事实筛选：只输出 selected_segments，不生成 candidate_clips。
CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT = _load_prompt("content_analysis_micro_segment.txt")
# ASR 微片段：根据连续 ASR 句子合并成语义小片段。
ASR_MICRO_SEGMENT_PROMPT = _load_prompt("asr_micro_segment.txt")
# 原声高光候选片段：从时间轴中选择适合原声重组的 chunk 组合。
HIGHLIGHT_DETECTION_PROMPT = _load_prompt("highlight_detection.txt")
# 原声高光 ASR 事件候选：模型只输出 segment_id 保留/合并判断。
ASR_EVENT_CANDIDATE_PROMPT = _load_prompt("asr_event_candidate.txt")
# AI 配音选片规划：从候选 clips 中决定每条短视频使用哪些 clip。
SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT = _load_prompt("short_video_edit_plan_text.txt")
# AI 配音文案：根据短视频 shots 生成 narration_text 和分段文案。
VOICEOVER_SCRIPT_TEXT_PROMPT = _load_prompt("voiceover_script_text.txt")
# AI 配音局部修复：只修复 TTS 时长过长或过短的 narration_segments。
VOICEOVER_REPAIR_TEXT_PROMPT = _load_prompt("voiceover_repair_text.txt")
# 原声高光重组：从候选 clips 中决定每条原声视频的 clip 顺序。
HIGHLIGHT_REASSEMBLY_TEXT_PROMPT = _load_prompt("highlight_reassembly_text.txt")
# 候选片段轻量复核：只判断保留 clip_id 和建议合并组。
CANDIDATE_FILTER_PROMPT = _load_prompt("candidate_filter.txt")
# 渲染前新闻软审校：只判断新闻表达风险等级。
NEWS_QUALITY_AI_REVIEW_PROMPT = _load_prompt("news_quality_ai_review.txt")
