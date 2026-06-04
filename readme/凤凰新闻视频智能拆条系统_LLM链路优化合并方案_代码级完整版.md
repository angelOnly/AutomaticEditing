# 凤凰新闻视频智能拆条系统：LLM 链路优化合并方案（代码级完整版）

> 目标：把两份方案合成一版既有总体架构，又能直接交给 Codex / 开发同事落地的技术文档。  
> 这版不再只讲方向，会补充：配置怎么加、prompt 怎么写、pipeline 步骤怎么挂、函数怎么改、输入输出结构怎么设计、fallback 怎么做、开发顺序怎么排。

---

## 0. 核心结论

当前项目的问题不是单纯“大模型输入长”，而是三类问题叠加：

```text
1. ASR 被硬规则提前裁剪，例如 NEWS_KEYWORDS，语义可能被误删。
2. 文本 Agent 输入重复、JSON 字段冗余，大模型注意力被稀释。
3. 编辑判断靠关键词、阈值、硬规则，不能适配复杂新闻场景。
```

所以优化方向不是删分析，而是：

```text
保留原始分析结果
↓
新增轻量模型语义压缩 / 判断层
↓
后续大模型只吃最小必要信息
↓
工程硬规则只做安全兜底
```

统一模型：

```text
文本类模型：ep-20260604155430-pt5bq
视觉类模型：ep-20260526134210-fq62r
```

---

## 1. 总体架构怎么改

### 1.1 AI 配音模式

改造前：

```text
metadata
audio_extract
frame_extract
chunk_build
asr
vision
timeline
timeline_digest
content_analysis
short_video_edit_plan
voiceover_script
tts
subtitles
cut_plan
render
```

改造后：

```text
metadata
audio_extract
frame_extract
chunk_build
asr
asr_digest                  新增：每个 chunk 的 ASR 语义压缩
vision                      改造：优先使用 asr_digest.speech
timeline                    改造：保留 asr_text，新增 asr_digest
timeline_digest             改造：优先使用 asr_digest
content_analysis            保留：仍做整体内容理解
candidate_refine            新增：候选片段轻量模型筛选
short_video_edit_plan       改造：结构化文本输入
merge_decision              新增：模型判断是否合并同新闻链
voiceover_script            改造：结构化文本输入
voiceover_quality_check     新增：文案质检，先 warning，后续再自动重写
tts
subtitles
cut_plan
render
```

### 1.2 高光重组模式

改造前：

```text
metadata
audio_extract
frame_extract
chunk_build
asr
vision
timeline
timeline_digest
video_understanding
highlight_detection
highlight_reassembly_plan
reassembly_cut_plan
reassembly_render
```

改造后：

```text
metadata
audio_extract
frame_extract
chunk_build
asr
asr_digest                  新增：每个 chunk 的 ASR 语义压缩
vision                      改造：优先使用 asr_digest.speech
timeline                    改造：保留 asr_text，新增 asr_digest
timeline_digest             改造：优先使用 asr_digest
video_understanding         保留：仍做整体视频理解
highlight_detection         保留：仍输出候选高光
candidate_refine            新增：候选片段轻量模型筛选
highlight_reassembly_plan   改造：结构化文本输入
reassembly_cut_plan
reassembly_render
```

### 1.3 不是删除原链路，而是重新分工

```text
原有分析链路：提供事实来源、视觉理解、候选集合
新增轻量模型层：压缩、筛选、分组、标注
规划类 Agent：基于少量结构化信息做短视频规划
工程规则：时间码、边界、文件、schema、渲染安全兜底
```

---

## 2. 硬规则边界

### 2.1 必须保留的工程硬规则

这些不能交给模型：

```text
1. 时间码合法性：end 必须大于 start
2. 不能超出源视频边界
3. 多源虚拟时间轴边界裁剪
4. FFmpeg 渲染参数、滤镜、音频合成
5. 输出 JSON schema 校验
6. require_tts=true 时 TTS 文件必须存在
7. source_path、manifest、缓存 hash、版本管理
8. 最大输出时长硬上限
9. 不能使用不存在的时间码
10. rerun / rerun-from / resume / manifest 进度显示
```

### 2.2 应该升级为模型判断的硬规则

这些属于新闻编辑判断，不适合写死：

```text
1. ASR 哪些句子重要
2. 哪些候选片段值得保留
3. 片段是否需要上下文
4. 片段是否可独立成片
5. 多个片段是否属于同一新闻链
6. 是否应该拆成多条视频
7. 片段排序是否符合新闻叙事
8. 哪些原声有证据价值
9. AI 配音文案是否信息量不足
10. 目标时长下是否应该合并或删减片段
```

比如 `_should_merge_short_video_scripts()` 里固定关键词可以保留，但只能作为 fallback。模型判断成功时，以模型判断为主。

---

## 3. 配置文件 `config.toml` 怎么改

新增或调整以下配置。

```toml
[llm]
text_llm_model_name = "ep-20260604155430-pt5bq"
vision_llm_model_name = "ep-20260526134210-fq62r"

[asr_digest]
enabled = true
model_name = "ep-20260604155430-pt5bq"
fallback_models = []
max_workers = 4
max_input_chars_per_chunk = 4000
target_chars_per_chunk = 320
target_chars_for_vision = 500
temperature = 0.0
max_tokens = 600
fail_policy = "fallback_to_compact_text"
skip_empty = true
debug = true

[candidate_refine]
enabled = true
model_name = "ep-20260604155430-pt5bq"
fallback_models = []
max_workers = 4
max_input_chars_per_clip = 5000
max_context_chunks = 3
max_tokens = 300
temperature = 0.0
fail_policy = "fallback_keep"
debug = true

[merge_decision]
enabled = true
model_name = "ep-20260604155430-pt5bq"
fallback_models = []
max_tokens = 300
temperature = 0.0
fail_policy = "fallback_rules"

[voiceover_quality_check]
enabled = true
model_name = "ep-20260604155430-pt5bq"
fallback_models = []
max_tokens = 300
temperature = 0.0
fail_policy = "warning_only"

[llm_input]
max_text_agent_input_chars = 20000
max_asr_digest_input_chars = 5000
max_candidate_refine_input_chars = 5000
max_short_video_edit_plan_input_chars = 12000
max_voiceover_script_input_chars = 10000
max_voiceover_quality_check_input_chars = 8000
max_highlight_reassembly_plan_input_chars = 10000
max_merge_decision_input_chars = 8000

max_llm_candidate_clips = 14
max_llm_context_chunks = 24
max_llm_key_facts = 8
max_llm_source_boundaries = 20
```

注意：

```text
不要把 max_text_agent_input_chars 设成 60000 当长期方案。
每一步单独限制，哪个步骤爆了，就说明 compact 没做好。
```

---

## 4. `newsclip_agent/llm.py` 怎么改

### 4.1 `_safe_json` 改成支持字符串原样输入

当前问题：`call_json()` 会把 `input_data` 全部 `_safe_json()`，如果 `_safe_json()` 用 `json.dumps(..., indent=2)`，JSON 会膨胀很多。

修改为：

```python
from typing import Any


def _safe_json(value: Any) -> str:
    import json

    if isinstance(value, str):
        return value

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
```

作用：

```text
1. 结构化文本输入可以原样进入模型。
2. dict/list 输入转成紧凑 JSON，不再 indent=2。
3. 输出仍然要求 JSON，不改变 extract_json_object 解析方式。
```

---

## 5. `newsclip_agent/llm_digest.py` 怎么改

### 5.1 删除或废弃 `NEWS_KEYWORDS`

不要再让 `NEWS_KEYWORDS` 决定 ASR 哪些内容重要。

### 5.2 `compact_asr_for_llm` 降级为 fallback

```python
def compact_asr_for_llm(text: str, max_chars: int = 320) -> str:
    """
    ASR digest 失败时的兜底压缩。
    不做关键词筛选，不做新闻词打分，不做语义判断。
    """
    return compact_text(text, max_chars=max_chars)
```

这一步很关键：

```text
ASR 的语义取舍交给 asr_digest 的文本模型。
compact_asr_for_llm 只做工程兜底，不做新闻判断。
```

---

## 6. `newsclip_agent/prompts.py` 怎么改

新增以下 prompt。重点是：输出字段只保留程序真正消费的字段。

### 6.1 `ASR_DIGEST_PROMPT`

```python
ASR_DIGEST_PROMPT = """
你是新闻视频 ASR 压缩助手。

任务：
把原始 ASR 压缩成更短的 speech，供后续视频理解和新闻拆条使用。

要求：
1. 只能使用 ASR 原文信息，不得编造。
2. 不要按固定关键词裁剪。
3. 保留人物、机构、地点、时间、动作、结果、关键表态、关键数字。
4. 删除重复口播、寒暄、语气词、明显无意义内容。
5. ASR 很短时允许 speech 很短，不要扩写。
6. ASR 有明显识别错误时，不要脑补修正成确定事实。
7. 尽量控制在 target_chars 内。
8. 只输出 JSON，不要 Markdown。

输出格式：
{"speech":"压缩后的 ASR 文本"}
"""
```

### 6.2 `CANDIDATE_REFINE_PROMPT`

```python
CANDIDATE_REFINE_PROMPT = """
你是新闻短视频候选片段筛选编辑。

任务：
判断输入的候选片段是否值得进入后续 AI 配音规划或原声高光重组。

判断标准：
1. 片段是否包含明确新闻信息。
2. 是否有足够画面或原声价值。
3. 是否能独立表达一个清楚信息点。
4. 是否需要前后文才能避免断章取义。
5. 是否更适合 AI 配音、原声重组，或两者都适合。

只能根据输入判断，不得编造。

输出只允许 JSON，不要 Markdown，不要解释。
字段必须极简：
{
  "k": 1,
  "u": "both",
  "i": 1,
  "c": 0,
  "g": "g1",
  "r": 1
}

字段说明：
k: keep，1=保留，0=丢弃
u: usage，av=AI配音，re=原声重组，both=两者都适合，drop=丢弃
i: independent，1=可独立成片，0=不可独立
c: needs_context，1=需要上下文，0=不需要
g: story_group，同一新闻链用同一个短分组名，如 g1/g2/g3
r: rank，1-99，数字越小越优先
"""
```

不要输出：

```text
reason
context_reason
story_group_hint
rank_hint
selection_reason
risk_level
```

如果需要排查，模型原始输出写入 debug 目录，不进主输出。

### 6.3 `MERGE_DECISION_PROMPT`

```python
MERGE_DECISION_PROMPT = """
你是新闻短视频拆条编辑。

任务：
判断多个短视频脚本是否应该合并，避免把同一新闻链拆成碎片。

判断标准：
1. 是否属于同一事件、同一议题、同一新闻链。
2. 拆开后是否会导致背景不足或断章取义。
3. 合并后是否仍能控制在合理时长。
4. 是否存在明显不同主体、不同事件、不同角度，必须拆开。

输出只允许 JSON：
{
  "m": 1,
  "groups": [
    ["v_001", "v_002"]
  ]
}

字段说明：
m: merge，1=建议合并，0=不合并
groups: 需要合并的视频 id 分组；不合并时为空数组
"""
```

### 6.4 `VOICEOVER_QUALITY_CHECK_PROMPT`

```python
VOICEOVER_QUALITY_CHECK_PROMPT = """
你是新闻 AI 配音文案质检助手。

任务：
检查配音文案是否满足输入的镜头脚本和事实要求。

只判断：
1. 是否明显过短。
2. 是否漏掉必须事实。
3. 是否有编造。
4. 是否结构不完整。
5. 是否需要重写。

输出只允许 JSON：
{
  "ok": 1,
  "rewrite": 0,
  "missing": []
}

字段说明：
ok: 1=通过，0=不通过
rewrite: 1=建议重写，0=不需要
missing: 缺失的必要事实编号数组，例如 [1,3]；没有则 []
"""
```

### 6.5 `HIGHLIGHT_REASSEMBLY_TEXT_PROMPT`

```python
HIGHLIGHT_REASSEMBLY_TEXT_PROMPT = """
你是凤凰新闻视频原声高光重组编辑。

任务：
根据输入的候选片段，选择、排序并组合成一条或多条原声高光视频。

要求：
1. 只能从候选片段中选择。
2. 不得编造时间码。
3. 不得选择 k=0 或 u=drop 的片段。
4. 原声重组优先选择 u=re 或 u=both 的片段。
5. 如果 c=1，必须连带选择同组上下文片段，或放弃该片段。
6. 同一 story_group 的片段优先组合在一起。
7. selected_clips 必须展开为对象，不要只输出 clip_id。
8. 只输出 JSON，不要 Markdown。

输出格式：
{
  "output_videos": [
    {
      "reassembly_id": "hr_001",
      "title": "标题",
      "selected_clips": [
        {
          "source_clip_id": "clip_001",
          "source_id": "source_1",
          "source_start": "00:00:00.000",
          "source_end": "00:00:12.000",
          "duration_seconds": 12,
          "role": "核心表态",
          "transition_after": "hard_cut"
        }
      ],
      "excluded_clip_ids": ["clip_003"]
    }
  ]
}
"""
```

这里保留 `role` 和 `transition_after`，因为后续剪辑和人工排查有用。删除 `selection_reason / risk_level / context_integrity_check`。

### 6.6 `SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT`

```python
SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT = """
你是新闻短视频 AI 配音剪辑策划。

任务：
根据视频概况、候选片段和筛选结果，生成 AI 配音短视频规划。

要求：
1. 默认优先生成 1 条完整短视频。
2. 只有不同事件、不同主体、不同角度，才拆多条。
3. 同一新闻链不要拆成碎片。
4. 只能使用候选片段时间码，不得编造。
5. 不得选择 k=0 或 u=drop 的片段。
6. AI 配音模式优先选择 u=av 或 u=both 的片段。
7. 输出必须是合法 JSON。

输出格式：
{
  "recommended_video_count": 1,
  "scripts": [
    {
      "short_video_id": "v_001",
      "topic": "主题",
      "news_angle": "角度",
      "title": "标题",
      "target_duration_seconds": 55,
      "source_clip_ids": ["clip_001"],
      "must_keep_fact_points": ["事实1"],
      "editing_structure": [
        {
          "order": 1,
          "shot_id": "v_001_s01",
          "source_start": "00:00:00.000",
          "source_end": "00:00:08.000",
          "duration_seconds": 8,
          "visual": "画面摘要",
          "fact": "这一镜头要讲的事实"
        }
      ]
    }
  ],
  "discarded_clip_ids": []
}
"""
```

删除这些默认输出字段：

```text
cover_text
subtitle_keywords
visual_selection_strategy
audio_strategy
priority
reason
required_context
split_reason
merge_with_other_clips_reason
```

### 6.7 `VOICEOVER_SCRIPT_TEXT_PROMPT`

```python
VOICEOVER_SCRIPT_TEXT_PROMPT = """
你是凤凰新闻 AI 配音文案编辑。

任务：
根据短视频镜头脚本生成配音文案。

要求：
1. 只能使用输入事实，不得编造。
2. 每个 narration_segment 必须对应一个 shot_id。
3. 文案要完整，不要只有标题式短句。
4. target_duration_seconds >= 40 秒时，必须有开场、背景、核心事实、分析或冲突、收束。
5. 每句适合字幕展示，建议 14-22 个汉字。
6. 只输出 JSON，不要 Markdown。

输出格式：
{
  "scripts": [
    {
      "short_video_id": "v_001",
      "narration_text": "完整配音文案",
      "narration_segments": [
        {
          "shot_id": "v_001_s01",
          "text": "这一镜头对应的配音"
        }
      ]
    }
  ]
}
"""
```

删除：

```text
estimated_duration
reason
style_explanation
risk_notes
```

这些可以程序后算，不要模型输出。

---

## 7. `newsclip_agent/pipeline.py` 怎么改

### 7.1 STEP_ORDER 加步骤

```python
AI_VOICEOVER_STEP_ORDER = [
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "asr_digest",
    "vision",
    "timeline",
    "timeline_digest",
    "content_analysis",
    "candidate_refine",
    "short_video_edit_plan",
    "merge_decision",
    "voiceover_script",
    "voiceover_quality_check",
    "tts",
    "subtitles",
    "cut_plan",
    "render",
]
```

```python
HIGHLIGHT_REASSEMBLY_STEP_ORDER = [
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "asr_digest",
    "vision",
    "timeline",
    "timeline_digest",
    "video_understanding",
    "highlight_detection",
    "candidate_refine",
    "highlight_reassembly_plan",
    "reassembly_cut_plan",
    "reassembly_render",
]
```

### 7.2 DEPENDENCIES 加依赖

```python
DEPENDENCIES.update({
    "asr_digest": ["asr", "chunk_build"],
    "vision": ["frame_extract", "chunk_build", "asr", "asr_digest"],
    "timeline": ["asr", "asr_digest", "vision"],
    "timeline_digest": ["timeline", "asr_digest"],
    "candidate_refine": ["timeline_digest"],
    "short_video_edit_plan": ["content_analysis", "candidate_refine"],
    "merge_decision": ["short_video_edit_plan"],
    "voiceover_script": ["short_video_edit_plan", "merge_decision"],
    "voiceover_quality_check": ["voiceover_script", "short_video_edit_plan"],
    "highlight_reassembly_plan": ["video_understanding", "highlight_detection", "candidate_refine"],
})
```

### 7.3 `_run_text_agent` 支持文本输入

```python
def _run_text_agent(self, step: str, input_data: Any) -> dict[str, Any]:
    vdir = self._step_version_dir(step)
    ensure_dir(vdir)

    self._log_llm_input_size(step, input_data)

    if isinstance(input_data, str):
        write_text(vdir / "input.txt", input_data)
        write_json(vdir / "input_meta.json", {
            "format": "text",
            "chars": len(input_data),
        })
    else:
        write_json(vdir / "input.json", input_data)

    # 后面保持原逻辑：call_json -> parsed -> write output.json
```

### 7.4 `_log_llm_input_size` 按 step 限制

```python
def _text_agent_input_limit(self, step: str) -> int:
    cfg = self.config.raw.get("llm_input", {})
    return int(
        cfg.get(f"max_{step}_input_chars")
        or cfg.get("max_text_agent_input_chars", 20000)
        or 20000
    )


def _input_size_chars(self, input_data: Any) -> int:
    if isinstance(input_data, str):
        return len(input_data)
    return len(json.dumps(
        input_data,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ))


def _log_llm_input_size(self, step: str, input_data: Any) -> None:
    size = self._input_size_chars(input_data)
    print(f"LLM input size [{step}]: {size} chars")

    limit = self._text_agent_input_limit(step)
    if size > limit:
        raise RuntimeError(
            f"LLM input size [{step}] exceeds {limit}; "
            f"compact 输入未生效或仍有冗余字段"
        )
```

---

## 8. 新增 `step_asr_digest`

### 8.1 主输出结构

```json
{
  "version": "asr_digest_v1",
  "model": "ep-20260604155430-pt5bq",
  "chunks": [
    {
      "chunk_id": "chunk_0001",
      "ts": "00:00:00.000-00:01:00.000",
      "speech": "压缩文本"
    }
  ]
}
```

### 8.2 后续只取 `speech`

```python
def _asr_digest_by_chunk_id(self) -> dict[str, str]:
    data = self._load_optional_step_json("asr_digest", {"chunks": []})
    return {
        str(item.get("chunk_id")): str(item.get("speech") or "")
        for item in data.get("chunks", [])
        if isinstance(item, dict) and item.get("chunk_id")
    }
```

### 8.3 单 chunk 输入

给模型的输入只包含：

```json
{
  "target_chars": 320,
  "asr_text": "原始 ASR 文本"
}
```

不要传：

```text
chunk_id
ts
model
version
source_path
debug
```

这些模型不需要。程序自己知道 chunk_id/ts，写输出时补上。

### 8.4 伪代码

```python
def step_asr_digest(self) -> dict[str, Any]:
    cfg = self.config.raw.get("asr_digest", {})
    if not cfg.get("enabled", True):
        return {"version": "asr_digest_v1", "disabled": True, "chunks": []}

    chunks = self._load_step_json("chunk_build").get("chunks", [])
    asr = self._load_step_json("asr")
    segs_by_chunk = self._asr_segments_by_chunk_id(asr)

    max_workers = int(cfg.get("max_workers", 4))
    results: list[dict[str, Any]] = []

    def process_one(chunk: dict[str, Any]) -> dict[str, Any]:
        cid = str(chunk.get("chunk_id") or "")
        raw_text = self._join_asr_text(segs_by_chunk.get(cid, []))
        ts = self._chunk_ts(chunk)

        if not raw_text.strip() and cfg.get("skip_empty", True):
            return {"chunk_id": cid, "ts": ts, "speech": ""}

        raw_text = raw_text[: int(cfg.get("max_input_chars_per_chunk", 4000))]
        input_data = {
            "target_chars": int(cfg.get("target_chars_per_chunk", 320)),
            "asr_text": raw_text,
        }

        try:
            result = self.llm_text.call_json(
                model=str(cfg.get("model_name") or self.text_llm_model_name),
                fallback_models=list(cfg.get("fallback_models") or []),
                prompt=prompts.ASR_DIGEST_PROMPT,
                input_data=input_data,
                temperature=float(cfg.get("temperature", 0.0)),
                max_tokens=int(cfg.get("max_tokens", 600)),
                debug_dir=self._debug_dir("asr_digest", cid),
            )
            speech = str(result.parsed.get("speech") or "").strip()
        except Exception as exc:
            if cfg.get("fail_policy", "fallback_to_compact_text") == "fallback_to_compact_text":
                speech = compact_asr_for_llm(
                    raw_text,
                    max_chars=int(cfg.get("target_chars_per_chunk", 320)),
                )
                self._write_debug_error("asr_digest", cid, exc)
            else:
                raise

        return {"chunk_id": cid, "ts": ts, "speech": speech}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_one, c) for c in chunks]
        for fut in as_completed(futures):
            results.append(fut.result())

    # 按原 chunks 顺序排序，避免并发返回乱序
    order = {str(c.get("chunk_id")): idx for idx, c in enumerate(chunks)}
    results.sort(key=lambda x: order.get(str(x.get("chunk_id")), 999999))

    output = {
        "version": "asr_digest_v1",
        "model": str(cfg.get("model_name") or self.text_llm_model_name),
        "chunks": results,
    }
    write_json(self._step_version_dir("asr_digest") / "asr_digest.json", output)
    return output
```

---

## 9. `vision` 怎么改

当前 `vision` 如果使用 `compact_asr_for_llm()`，容易继续被旧关键词裁剪影响。改成优先使用 `asr_digest.speech`。

### 9.1 改造逻辑

```python
asr_digest_map = self._asr_digest_by_chunk_id()

speech = asr_digest_map.get(cid)
if not speech:
    speech = compact_asr_for_llm(
        raw_asr_text,
        max_chars=int(asr_digest_cfg.get("target_chars_for_vision", 500)),
    )

input_data = {
    "chunk": compact_chunk_for_vision(chunk),
    "asr_text": speech,
    "prompt_version": prompt_version,
    "model": model,
}
```

结论：

```text
视觉模型看到的是语义压缩后的 ASR，
不是 NEWS_KEYWORDS 筛过的 ASR。
```

---

## 10. `timeline` 怎么改

### 10.1 保留原始 ASR，新增 `asr_digest`

当前可能类似：

```python
"asr_text": " ".join(s.get("text", "") for s in segs).strip()
```

改成：

```python
asr_text = " ".join(s.get("text", "") for s in segs).strip()

item = {
    "chunk_id": chunk["chunk_id"],
    "start": chunk.get("start"),
    "end": chunk.get("end"),
    "asr_text": asr_text,
    "asr_digest": asr_digest_by_chunk_id.get(chunk["chunk_id"], ""),
    "vision": vision_by_chunk_id.get(chunk["chunk_id"], {}),
}
```

这样不删除原始 ASR，只新增一个更适合后续 LLM 的短 speech。

---

## 11. `timeline_digest` 怎么改

### 11.1 优先使用 `item.asr_digest`

当前如果是：

```python
"speech": compact_asr_for_llm(item.get("asr_text", ""), max_chars=...)
```

改成：

```python
speech = item.get("asr_digest") or compact_asr_for_llm(
    item.get("asr_text", ""),
    max_chars=int(llm_input_cfg.get("max_asr_chars_per_chunk", 320)),
)
```

### 11.2 digest version 升级

```python
digest_version = "llm_timeline_digest_v2_asr_digest"
```

### 11.3 input hash 加入 `asr_digest`

```python
input_hash = {
    "timeline": self._step_content_hash("timeline"),
    "asr_digest": self._step_content_hash("asr_digest"),
    "vision": self._step_content_hash("vision"),
}
```

### 11.4 `timeline_digest` 输出字段建议

```json
{
  "version": "llm_timeline_digest_v2_asr_digest",
  "items": [
    {
      "chunk_id": "chunk_0001",
      "t": "00:00:00.000-00:01:00.000",
      "speech": "ASR digest speech",
      "visual": "画面摘要",
      "score": 7,
      "flags": ["speech", "person", "scene"]
    }
  ]
}
```

不要传：

```text
raw_asr
model
debug
usage
latency
完整 vision response
完整 source manifest
```

---

## 12. 新增 `candidate_refine`

### 12.1 职责

`candidate_refine` 用轻量文本模型并发筛选候选片段。

判断：

```text
1. 是否保留
2. 适合 AI 配音、原声重组，还是都适合
3. 是否可独立成片
4. 是否需要上下文
5. 属于哪个新闻链分组
6. 优先级排序
```

### 12.2 主输出结构

```json
{
  "version": "candidate_refine_v1",
  "clips": [
    {
      "clip_id": "clip_001",
      "k": 1,
      "u": "both",
      "i": 1,
      "c": 0,
      "g": "g1",
      "r": 1
    }
  ]
}
```

没有：

```text
reason
context_reason
story_group_hint
rank_hint
```

### 12.3 单 clip 输入结构

```json
{
  "topic": "新闻主题",
  "clip": {
    "id": "clip_001",
    "t": "00:00:10.000-00:00:22.000",
    "dur": 12,
    "sum": "候选片段摘要",
    "visual": "画面摘要",
    "speech": "片段对应 speech",
    "score": "news=8,visual=7,ind=8"
  },
  "ctx": [
    "前一个 chunk speech",
    "后一个 chunk speech"
  ]
}
```

字段短一点没关系，模型能读懂即可。

### 12.4 normalize 函数

```python
def _normalize_candidate_refine_output(
    self,
    clip_id: str,
    parsed: dict[str, Any],
    fallback_rank: int,
) -> dict[str, Any]:
    u = str(parsed.get("u") or "both").strip()
    if u not in {"av", "re", "both", "drop"}:
        u = "both"

    try:
        rank = int(parsed.get("r") or fallback_rank)
    except Exception:
        rank = fallback_rank

    return {
        "clip_id": clip_id,
        "k": 1 if parsed.get("k", 1) else 0,
        "u": u,
        "i": 1 if parsed.get("i", 0) else 0,
        "c": 1 if parsed.get("c", 0) else 0,
        "g": str(parsed.get("g") or "g1")[:12],
        "r": rank,
    }
```

### 12.5 fallback_keep

模型失败时不要丢片段：

```python
def _fallback_candidate_refine_clip(self, clip_id: str, fallback_rank: int) -> dict[str, Any]:
    return {
        "clip_id": clip_id,
        "k": 1,
        "u": "both",
        "i": 0,
        "c": 0,
        "g": "g1",
        "r": fallback_rank,
    }
```

### 12.6 `step_candidate_refine` 伪代码

```python
def step_candidate_refine(self) -> dict[str, Any]:
    cfg = self.config.raw.get("candidate_refine", {})
    if not cfg.get("enabled", True):
        return {"version": "candidate_refine_v1", "disabled": True, "clips": []}

    # AI 配音模式可从 content_analysis 取候选；重组模式可从 highlight_detection 取候选
    candidate_clips = self._load_candidate_clips_for_current_mode()
    timeline_digest = self._load_step_json("timeline_digest")
    topic = self._extract_video_topic()

    max_workers = int(cfg.get("max_workers", 4))
    results = []

    def process_one(idx: int, clip: dict[str, Any]) -> dict[str, Any]:
        clip_id = str(clip.get("clip_id") or clip.get("id") or f"clip_{idx+1:03d}")
        input_data = self._build_candidate_refine_input(
            topic=topic,
            clip=clip,
            timeline_digest=timeline_digest,
            max_context_chunks=int(cfg.get("max_context_chunks", 3)),
        )

        try:
            result = self.llm_text.call_json(
                model=str(cfg.get("model_name") or self.text_llm_model_name),
                fallback_models=list(cfg.get("fallback_models") or []),
                prompt=prompts.CANDIDATE_REFINE_PROMPT,
                input_data=input_data,
                temperature=float(cfg.get("temperature", 0.0)),
                max_tokens=int(cfg.get("max_tokens", 300)),
                debug_dir=self._debug_dir("candidate_refine", clip_id),
            )
            return self._normalize_candidate_refine_output(
                clip_id,
                result.parsed,
                fallback_rank=idx + 1,
            )
        except Exception as exc:
            self._write_debug_error("candidate_refine", clip_id, exc)
            if cfg.get("fail_policy", "fallback_keep") == "fallback_keep":
                return self._fallback_candidate_refine_clip(clip_id, idx + 1)
            raise

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_one, idx, c) for idx, c in enumerate(candidate_clips)]
        for fut in as_completed(futures):
            results.append(fut.result())

    order = {str(c.get("clip_id") or c.get("id")): idx for idx, c in enumerate(candidate_clips)}
    results.sort(key=lambda x: x.get("r") or order.get(x.get("clip_id"), 999999))

    output = {"version": "candidate_refine_v1", "clips": results}
    write_json(self._step_version_dir("candidate_refine") / "candidate_refine.json", output)
    return output
```

---

## 13. `short_video_edit_plan` 输入文本化

### 13.1 输入只保留

```text
1. 视频主题摘要
2. 核心事实，最多 8 条
3. 候选片段，最多 14 条
4. candidate_refine 字段
5. 邻近上下文，最多 24 个 chunk
6. 目标时长 / 输出模式 / 最大输出数量
```

### 13.2 不再传

```text
完整 timeline_digest
完整 source_videos
完整 source_manifest
raw ASR
debug
usage
latency
完整 content_analysis 原文
```

### 13.3 输入文本模板

```text
任务：生成 AI 配音短视频规划。

参数：
- target=55s
- output_mode=single
- max_output_videos=1

视频：
主题：……
摘要：……
核心事实：
1. …
2. …

候选片段：
[clip_001] t=00:00:10.000-00:00:22.000 dur=12s k=1 u=both i=1 c=0 g=g1 r=1
摘要：……
画面：……
speech：……

[clip_002] ...

输出：严格按 prompt JSON 格式。
```

### 13.4 builder 伪代码

```python
def _build_short_video_edit_plan_text(self) -> str:
    llm_cfg = self.config.raw.get("llm_input", {})
    max_clips = int(llm_cfg.get("max_llm_candidate_clips", 14))
    max_facts = int(llm_cfg.get("max_llm_key_facts", 8))

    content = self._load_step_json("content_analysis")
    refine = self._candidate_refine_by_clip_id()
    clips = self._candidate_clips_for_ai_voiceover()[:max_clips]

    lines = []
    lines.append("任务：生成 AI 配音短视频规划。")
    lines.append("")
    lines.append("参数：")
    lines.append(f"- target={self.run_options.target_duration_seconds}s")
    lines.append(f"- output_mode={self.run_options.output_mode}")
    lines.append(f"- max_output_videos={self.run_options.max_output_videos}")
    lines.append("")
    lines.append("视频：")
    lines.append(f"主题：{self._brief_topic(content)}")
    lines.append(f"摘要：{self._brief_summary(content)}")
    lines.append("核心事实：")
    for i, fact in enumerate(self._key_facts(content)[:max_facts], 1):
        lines.append(f"{i}. {fact}")

    lines.append("")
    lines.append("候选片段：")
    for clip in clips:
        cid = self._clip_id(clip)
        r = refine.get(cid, {})
        lines.append(
            f"[{cid}] t={self._clip_time(clip)} dur={self._clip_dur(clip)}s "
            f"k={r.get('k', 1)} u={r.get('u', 'both')} i={r.get('i', 0)} "
            f"c={r.get('c', 0)} g={r.get('g', 'g1')} r={r.get('r', 99)}"
        )
        lines.append(f"摘要：{self._clip_summary(clip)}")
        lines.append(f"画面：{self._clip_visual(clip)}")
        lines.append(f"speech：{self._clip_speech(clip)}")
        lines.append("")

    lines.append("输出：严格按 prompt JSON 格式。")
    return "\n".join(lines)
```

### 13.5 调用方式

```python
def step_short_video_edit_plan(self) -> dict[str, Any]:
    input_text = self._build_short_video_edit_plan_text()
    return self._run_text_agent(
        step="short_video_edit_plan",
        input_data=input_text,
        prompt=prompts.SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT,
    )
```

如果现有 `_run_text_agent` 不支持 prompt 参数，就按项目当前调用封装适配，关键是 input_data 传字符串。

---

## 14. 新增 `merge_decision`

### 14.1 目的

替代 `_should_merge_short_video_scripts()` 里的固定关键词判断。

### 14.2 注意

旧规则不要删，降级为 fallback：

```text
模型判断成功：按模型 groups 合并
模型失败 / JSON 解析失败：走旧关键词规则
```

### 14.3 输入文本模板

```text
任务：判断这些脚本是否属于同一新闻链，是否应合并。

[v_001]
主题：……
角度：……
目标时长：55s
事实：
- …
片段：
- clip_001
- clip_002

[v_002]
主题：……
角度：……
目标时长：35s
事实：
- …
片段：
- clip_003

输出 JSON：
{"m":1,"groups":[["v_001","v_002"]]}
```

### 14.4 应用逻辑

```python
def step_merge_decision(self) -> dict[str, Any]:
    edit_plan = self._load_step_json("short_video_edit_plan")
    scripts = edit_plan.get("scripts") or []

    if len(scripts) <= 1:
        output = {"version": "merge_decision_v1", "m": 0, "groups": []}
        write_json(self._step_version_dir("merge_decision") / "merge_decision.json", output)
        return output

    cfg = self.config.raw.get("merge_decision", {})
    input_text = self._build_merge_decision_text(scripts)

    try:
        result = self.llm_text.call_json(
            model=str(cfg.get("model_name") or self.text_llm_model_name),
            fallback_models=list(cfg.get("fallback_models") or []),
            prompt=prompts.MERGE_DECISION_PROMPT,
            input_data=input_text,
            temperature=float(cfg.get("temperature", 0.0)),
            max_tokens=int(cfg.get("max_tokens", 300)),
            debug_dir=self._debug_dir("merge_decision"),
        )
        output = self._normalize_merge_decision(result.parsed, scripts)
    except Exception as exc:
        self._write_debug_error("merge_decision", "main", exc)
        output = self._fallback_merge_decision_by_rules(scripts)

    write_json(self._step_version_dir("merge_decision") / "merge_decision.json", output)
    return output
```

### 14.5 在后续应用

```python
decision = self._load_optional_step_json("merge_decision", {})
if decision.get("m") == 1:
    edit_plan = self._merge_short_video_scripts_by_decision(edit_plan, decision)
else:
    edit_plan = self._normalize_short_video_split_decision_by_rules(edit_plan)
```

---

## 15. `voiceover_script` 输入文本化

### 15.1 输入只包含

```text
short_video_id
topic
news_angle
target_duration_seconds
must_keep_fact_points
editing_structure:
  shot_id
  source_start/source_end
  duration_seconds
  visual
  fact
```

### 15.2 不传

```text
editing_script 兼容副本
完整 short_video_edit_plan
voiceover_timing_contracts 全量
duration_strategy 全量
run_options 全量
subtitle_keywords
cover_text
priority
reason
```

### 15.3 输入文本模板

```text
任务：生成 AI 配音文案。

[v_001]
主题：……
角度：……
目标时长：55s
必须讲清事实：
1. …
2. …

镜头结构：
1. shot_id=v_001_s01 t=00:00:00.000-00:00:08.000 dur=8s
画面：……
事实：……

2. shot_id=v_001_s02 ...

输出：严格按 prompt JSON 格式。
```

### 15.4 builder 伪代码

```python
def _build_voiceover_script_text(self) -> str:
    edit_plan = self._load_step_json("short_video_edit_plan")
    scripts = self._apply_merge_decision_if_needed(edit_plan).get("scripts", [])

    lines = ["任务：生成 AI 配音文案。", ""]
    for script in scripts:
        sid = script.get("short_video_id")
        lines.append(f"[{sid}]")
        lines.append(f"主题：{script.get('topic', '')}")
        lines.append(f"角度：{script.get('news_angle', '')}")
        lines.append(f"目标时长：{script.get('target_duration_seconds', '')}s")
        lines.append("必须讲清事实：")
        for i, fact in enumerate(script.get("must_keep_fact_points") or [], 1):
            lines.append(f"{i}. {fact}")
        lines.append("")
        lines.append("镜头结构：")
        for shot in script.get("editing_structure") or []:
            lines.append(
                f"{shot.get('order')}. shot_id={shot.get('shot_id')} "
                f"t={shot.get('source_start')}-{shot.get('source_end')} "
                f"dur={shot.get('duration_seconds')}s"
            )
            lines.append(f"画面：{shot.get('visual', '')}")
            lines.append(f"事实：{shot.get('fact', '')}")
            lines.append("")
    lines.append("输出：严格按 prompt JSON 格式。")
    return "\n".join(lines)
```

---

## 16. 新增 `voiceover_quality_check`

### 16.1 职责

检查 AI 配音文案是否太短、事实缺失、结构不完整。

第一版建议：

```text
只 warning，不自动重写。
```

第二版再加：

```text
rewrite=1 时重新调用 voiceover_script。
```

### 16.2 输入文本模板

```text
任务：检查配音文案质量。

目标时长：55s
必须讲清事实：
1. …
2. …
3. …

文案：
……

输出 JSON：
{"ok":1,"rewrite":0,"missing":[]}
```

### 16.3 伪代码

```python
def step_voiceover_quality_check(self) -> dict[str, Any]:
    cfg = self.config.raw.get("voiceover_quality_check", {})
    script_data = self._load_step_json("voiceover_script")
    edit_plan = self._load_step_json("short_video_edit_plan")

    input_text = self._build_voiceover_quality_check_text(script_data, edit_plan)

    try:
        result = self.llm_text.call_json(
            model=str(cfg.get("model_name") or self.text_llm_model_name),
            fallback_models=list(cfg.get("fallback_models") or []),
            prompt=prompts.VOICEOVER_QUALITY_CHECK_PROMPT,
            input_data=input_text,
            temperature=float(cfg.get("temperature", 0.0)),
            max_tokens=int(cfg.get("max_tokens", 300)),
            debug_dir=self._debug_dir("voiceover_quality_check"),
        )
        output = self._normalize_voiceover_quality_check(result.parsed)
    except Exception as exc:
        self._write_debug_error("voiceover_quality_check", "main", exc)
        output = {"version": "voiceover_quality_check_v1", "ok": 1, "rewrite": 0, "missing": [], "warning": "quality_check_failed"}

    write_json(self._step_version_dir("voiceover_quality_check") / "voiceover_quality_check.json", output)
    return output
```

---

## 17. `highlight_reassembly_plan` 输入文本化

### 17.1 输入只保留

```text
1. 视频主题
2. 视频摘要
3. 候选片段，最多 14 条
4. candidate_refine 字段
5. source_boundaries
6. target / max_clip_count / output_mode
```

### 17.2 不再传

```text
完整 timeline_digest
完整 video_analysis
完整 candidate_clips 原始 JSON
完整 source_videos
source_manifest 路径
debug / usage / latency
```

### 17.3 输入文本模板

```text
任务：原声高光重组。

参数：
- target=90s
- max_clip_count=8
- output_mode=single

视频：
主题：……
摘要：……

候选片段：
[clip_001] t=00:00:10.000-00:00:22.000 dur=12s source=source_1 k=1 u=re i=1 c=0 g=g1 r=1
摘要：……
speech：……
visual：……

多源边界：
source_1: 0-120s
source_2: 120-240s

输出：严格按 prompt JSON 格式。
```

### 17.4 builder 伪代码

```python
def _build_highlight_reassembly_plan_text(self) -> str:
    llm_cfg = self.config.raw.get("llm_input", {})
    max_clips = int(llm_cfg.get("max_llm_candidate_clips", 14))
    max_boundaries = int(llm_cfg.get("max_llm_source_boundaries", 20))

    video = self._load_step_json("video_understanding")
    detection = self._load_step_json("highlight_detection")
    refine = self._candidate_refine_by_clip_id()
    clips = self._candidate_clips_for_reassembly(detection)[:max_clips]

    lines = []
    lines.append("任务：原声高光重组。")
    lines.append("")
    lines.append("参数：")
    lines.append(f"- target={self.run_options.target_duration_seconds}s")
    lines.append(f"- max_clip_count={self.run_options.max_clip_count}")
    lines.append(f"- output_mode={self.run_options.output_mode}")
    lines.append("")
    lines.append("视频：")
    lines.append(f"主题：{self._brief_topic(video)}")
    lines.append(f"摘要：{self._brief_summary(video)}")
    lines.append("")
    lines.append("候选片段：")

    for clip in clips:
        cid = self._clip_id(clip)
        r = refine.get(cid, {})
        lines.append(
            f"[{cid}] t={self._clip_time(clip)} dur={self._clip_dur(clip)}s "
            f"source={self._clip_source_id(clip)} k={r.get('k', 1)} u={r.get('u', 'both')} "
            f"i={r.get('i', 0)} c={r.get('c', 0)} g={r.get('g', 'g1')} r={r.get('r', 99)}"
        )
        lines.append(f"摘要：{self._clip_summary(clip)}")
        lines.append(f"speech：{self._clip_speech(clip)}")
        lines.append(f"visual：{self._clip_visual(clip)}")
        lines.append("")

    lines.append("多源边界：")
    for b in self._source_boundaries()[:max_boundaries]:
        lines.append(f"{b['source_id']}: {b['start']}-{b['end']}s")

    lines.append("")
    lines.append("输出：严格按 prompt JSON 格式。")
    return "\n".join(lines)
```

---

## 18. `web_app.py` 怎么改

### 18.1 step order / 进度展示

AI 配音模式增加：

```text
asr_digest
candidate_refine
merge_decision
voiceover_quality_check
```

高光重组模式增加：

```text
asr_digest
candidate_refine
```

### 18.2 COMMON_REUSABLE_STEPS

建议第一版：

```python
COMMON_REUSABLE_STEPS = [
    "source_prepare",
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "asr_digest",
    "vision",
    "timeline",
    "timeline_digest",
]
```

暂时不要把 `candidate_refine` 放公共步骤。原因：

```text
AI 配音和高光重组对 usage 的偏好不同。
candidate_refine 虽然可通用，但第一版先按模式各自跑，避免公共缓存误复用。
```

---

## 19. `run_multisource_pipeline.py` 怎么改

公共分析复制列表加入：

```python
"asr_digest"
```

不要先加 `candidate_refine`。

示意：

```python
COMMON_REUSABLE_STEPS = [
    "source_prepare",
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "asr_digest",
    "vision",
    "timeline",
    "timeline_digest",
]
```

---

## 20. 输出字段瘦身原则

以后每个轻量模型步骤都按这个原则设计：

```text
1. 下游不用的字段，不输出。
2. 人看起来有解释价值，但程序不用的字段，不进主输出。
3. debug、reason、usage、latency 只进 debug 文件，不进主输出。
4. 输出字段只保留 pipeline 后续消费字段。
```

### 20.1 错误示例

```json
{
  "keep": true,
  "usage": "ai_voiceover / reassembly / both / discard",
  "independent": true,
  "needs_context": false,
  "context_reason": "",
  "story_group_hint": "",
  "rank_hint": 0,
  "reason": ""
}
```

### 20.2 推荐示例

```json
{
  "clip_id": "clip_001",
  "k": 1,
  "u": "both",
  "i": 1,
  "c": 0,
  "g": "g1",
  "r": 1
}
```

字段含义：

```text
k = keep，1 保留，0 丢弃
u = usage，av / re / both / drop
i = independent，1 可独立，0 不可独立
c = needs_context，1 需要上下文，0 不需要
g = story_group，同一新闻链分组
r = rank，数字越小越优先
```

---

## 21. 开发顺序

### 第 1 步：基础输入机制

改：

```text
newsclip_agent/llm.py
newsclip_agent/pipeline.py
config.toml
```

完成：

```text
1. _safe_json 支持 str
2. dict/list 输入紧凑 JSON
3. _run_text_agent 支持 input.txt
4. _log_llm_input_size 分 step 限制
```

验证：

```bash
python -m py_compile newsclip_agent/llm.py newsclip_agent/pipeline.py
```

### 第 2 步：ASR digest

改：

```text
prompts.py
llm_digest.py
pipeline.py
web_app.py
run_multisource_pipeline.py
config.toml
```

完成：

```text
1. 新增 ASR_DIGEST_PROMPT
2. 新增 step_asr_digest
3. STEP_ORDER 加 asr_digest
4. vision 使用 asr_digest.speech
5. timeline 保留 asr_text，新增 asr_digest
6. timeline_digest 优先 asr_digest
7. compact_asr_for_llm 只做 fallback
```

验证：

```bash
python run_pipeline.py --task-id 测试任务 --rerun-from asr_digest
```

检查：

```text
outputs/<task>/asr_digest/v1/asr_digest.json
```

### 第 3 步：candidate_refine

改：

```text
prompts.py
pipeline.py
config.toml
web_app.py
```

完成：

```text
1. 新增 CANDIDATE_REFINE_PROMPT
2. 新增 step_candidate_refine
3. 每 clip 并发调用 ep-20260604155430-pt5bq
4. 主输出只保留 clip_id/k/u/i/c/g/r
5. 模型失败 fallback_keep
```

验证：

```bash
python run_pipeline.py --task-id 测试任务 --rerun-from candidate_refine
```

### 第 4 步：文本化 `highlight_reassembly_plan`

改：

```text
pipeline.py
prompts.py
```

完成：

```text
1. 输入从完整 JSON 改为结构化文本
2. 使用 candidate_refine 字段
3. 输出字段瘦身
4. reassembly_cut_plan 兼容 excluded_clip_ids
```

验证：

```bash
python run_pipeline.py \
  --task-id 测试任务 \
  --production-mode highlight_reassembly \
  --rerun-from highlight_reassembly_plan \
  --audio-policy original \
  --skip-tts \
  --no-require-tts
```

### 第 5 步：文本化 `short_video_edit_plan`

改：

```text
pipeline.py
prompts.py
```

完成：

```text
1. 不传完整 timeline_digest
2. 只传候选片段 + refine + 邻近上下文
3. 输出极简 scripts
4. 兼容旧的 _write_compat_short_video_plan_and_editing_script
```

### 第 6 步：merge_decision

改：

```text
pipeline.py
prompts.py
```

完成：

```text
1. 模型优先判断是否合并
2. 现有关键词规则作为 fallback
3. 输出只保留 m/groups
```

### 第 7 步：voiceover_script + quality_check

改：

```text
pipeline.py
prompts.py
```

完成：

```text
1. voiceover_script 输入文本化
2. 输出极简
3. 新增 voiceover_quality_check
4. quality_check 失败时先 warning，第二阶段再做自动重写
```

---

## 22. 给 Codex 的完整开发指令

可以直接复制给 Codex：

```text
请基于当前仓库 clean-highlight-reassembly 分支实现一次完整的大模型输入与判断逻辑优化。

总目标：
不要删除现有分析链路，在现有 pipeline 上补充 asr_digest、candidate_refine、merge_decision、voiceover_quality_check，并把高风险文本 Agent 改为结构化文本输入。复杂编辑判断优先使用大模型，现有硬规则仅作为 fallback。工程安全规则必须保留。

模型要求：
1. 所有文本处理统一使用 ep-20260604155430-pt5bq。
2. 所有视觉模块统一使用 ep-20260526134210-fq62r。

第一部分：基础输入机制
1. 修改 newsclip_agent/llm.py：
   - _safe_json 支持 input_data 为 str 时原样返回。
   - dict/list 输入使用 json.dumps(..., ensure_ascii=False, separators=(",", ":"), default=str)，不要 indent=2。
   - 输出仍然用 extract_json_object 解析 JSON，不改变输出解析方式。
2. 修改 pipeline.py：
   - _run_text_agent 支持 input_data 为 str，保存 input.txt 和 input_meta.json。
   - dict 输入仍保存 input.json。
   - _log_llm_input_size 支持 str 和紧凑 JSON。
   - 增加 _text_agent_input_limit(step)，支持 config.toml 中 max_<step>_input_chars。

第二部分：ASR 语义压缩
1. config.toml 新增 [asr_digest]。
2. prompts.py 新增 ASR_DIGEST_PROMPT，输出只允许 {"speech":"..."}。
3. llm_digest.py 删除/废弃 NEWS_KEYWORDS；compact_asr_for_llm 只作为 fallback，调用 compact_text，不做关键词筛选。
4. pipeline.py：
   - STEP_ORDER 在 asr 后加入 asr_digest。
   - DEPENDENCIES 加 asr_digest。
   - 新增 step_asr_digest。
   - 使用 ThreadPoolExecutor 并发处理每个 chunk。
   - 每个 chunk 输入只包含 target_chars 和 asr_text，不要把 chunk_id/ts/model/version 传给模型。
   - 主输出 asr_digest/v1/asr_digest.json，结构只包含 version、model、chunks；chunks 每项只包含 chunk_id、ts、speech。
   - debug、usage、latency、error 不写入主输出，只写 debug。
   - 单 chunk 失败时 fallback 到 compact_text，默认不阻断流程。
   - vision 优先使用 asr_digest.speech。
   - timeline 保留原始 asr_text，同时新增 asr_digest。
   - timeline_digest 优先使用 asr_digest，digest_version 升级为 llm_timeline_digest_v2_asr_digest。
5. web_app.py 和 run_multisource_pipeline.py 加入 asr_digest 步骤和公共复用。

第三部分：候选片段轻量筛选
1. config.toml 新增 [candidate_refine]。
2. prompts.py 新增 CANDIDATE_REFINE_PROMPT。
3. 输出字段必须极简：
   {"k":1,"u":"both","i":1,"c":0,"g":"g1","r":1}
   不要输出 reason/context_reason/story_group_hint/rank_hint 等无消费字段。
4. pipeline.py 新增 step_candidate_refine：
   - 读取 highlight_detection 或 content_analysis 中的 candidate_clips。
   - 每个 clip 并发调用文本模型 ep-20260604155430-pt5bq。
   - 单 clip 输入只包含 topic、clip id/time/duration/summary/visual/speech/score，以及最多前后 context chunks。
   - 主输出 candidate_refine/v1/candidate_refine.json：
     {"version":"candidate_refine_v1","clips":[{"clip_id":"clip_001","k":1,"u":"both","i":1,"c":0,"g":"g1","r":1}]}
   - 模型失败 fallback_keep，不丢片段。
   - debug 信息单独写，不进入主输出。

第四部分：文本化规划类 Agent
1. highlight_reassembly_plan：
   - 改为结构化文本输入。
   - 使用 video_brief、candidate_clips、candidate_refine、source_boundaries。
   - 不再传完整 timeline_digest、完整 video_analysis、完整 candidate_clips、source_manifest。
   - prompt 使用 HIGHLIGHT_REASSEMBLY_TEXT_PROMPT。
   - 输出字段只保留 output_videos、reassembly_id、title、selected_clips、excluded_clip_ids。
   - selected_clips 每项只保留 source_clip_id、source_id、source_start、source_end、duration_seconds、role、transition_after。
2. short_video_edit_plan：
   - 改为结构化文本输入。
   - 只传视频概况、核心事实、候选片段、candidate_refine、邻近上下文、目标时长。
   - 不传完整 timeline_digest/source_videos/source_manifest。
   - prompt 使用 SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT。
   - 输出字段瘦身，只保留后续真正需要字段。
3. voiceover_script：
   - 改为结构化文本输入。
   - 不再同时传 short_video_edit_plan 和 editing_script 两份重复内容。
   - 只传 short_video_id/topic/angle/target_duration/must_keep_fact_points/editing_structure。
   - prompt 使用 VOICEOVER_SCRIPT_TEXT_PROMPT。
   - 输出只保留 short_video_id、narration_text、narration_segments。

第五部分：模型化复杂判断
1. 新增 merge_decision：
   - 使用 MERGE_DECISION_PROMPT。
   - 输入多个 scripts 的主题、角度、事实、片段 id、目标时长。
   - 输出只保留 {"m":1,"groups":[["v_001","v_002"]]}。
   - 模型判断优先，现有 _should_merge_short_video_scripts 关键词规则只作为 fallback。
2. 新增 voiceover_quality_check：
   - 使用 VOICEOVER_QUALITY_CHECK_PROMPT。
   - 输入目标时长、必讲事实、生成文案。
   - 输出只保留 {"ok":1,"rewrite":0,"missing":[]}。
   - 第一版只 warning，不自动重写。

第六部分：兼容与验证
1. 保持 Web 启动方式不变：python run_web.py。
2. 保持 AI 配音模式和高光重组模式兼容。
3. 所有新增步骤都要支持 rerun / rerun-from / resume / manifest 进度显示。
4. 添加必要 fallback：
   - asr_digest 单 chunk 失败时 fallback 到 compact_text。
   - candidate_refine 失败时 fallback_keep。
   - merge_decision 失败时 fallback 到旧关键词规则。
   - voiceover_quality_check 失败时 warning_only。
5. 不要删除原始 ASR，timeline 中保留 asr_text，同时新增 asr_digest。
6. 不要把 debug、usage、latency、entities、key_facts、reason 等大字段写入主输出。
7. 修改后至少运行：
   - python -m py_compile newsclip_agent/llm.py newsclip_agent/pipeline.py
   - AI 配音模式 rerun-from asr_digest
   - 高光重组模式 rerun-from highlight_reassembly_plan
```

---

## 23. 最终验收标准

### 23.1 文件层面

必须看到：

```text
config.toml 新增 asr_digest / candidate_refine / merge_decision / voiceover_quality_check / llm_input
prompts.py 新增 7 个 prompt
llm.py 的 _safe_json 不再 indent=2，支持 str 原样输入
llm_digest.py 不再使用 NEWS_KEYWORDS 做 ASR 筛选
pipeline.py 新增 4 个步骤，并改造 3 个规划类 Agent 输入
web_app.py 进度步骤包含新增步骤
run_multisource_pipeline.py 公共复用加入 asr_digest
```

### 23.2 输出层面

必须看到：

```text
outputs/<task>/asr_digest/v1/asr_digest.json
outputs/<task>/candidate_refine/v1/candidate_refine.json
outputs/<task>/merge_decision/v1/merge_decision.json
outputs/<task>/voiceover_quality_check/v1/voiceover_quality_check.json
```

### 23.3 输入大小层面

必须看到日志：

```text
LLM input size [short_video_edit_plan]: < 12000 chars
LLM input size [voiceover_script]: < 10000 chars
LLM input size [highlight_reassembly_plan]: < 10000 chars
```

### 23.4 质量层面

应该改善：

```text
1. 不再因为 NEWS_KEYWORDS 误删 ASR 重点。
2. 视觉分析拿到更稳定的 speech。
3. 候选片段不再只靠固定阈值和关键词判断。
4. 同一新闻链不容易被拆碎。
5. voiceover_script 不再因输入重复而生成短、散、漏事实的文案。
6. 规划类 Agent 输入更短、更准、更容易调试。
```

---

## 24. 一句话总结

这次优化不是“删步骤”，而是把项目从：

```text
大 JSON 乱传 + 关键词硬裁剪 + 规则硬判断
```

升级成：

```text
原始事实保留 + 轻量模型压缩 + 轻量模型筛选 + 结构化文本规划 + 工程规则兜底
```

最终效果应该是：模型输入更短，判断更像新闻编辑，代码仍然可控、可回滚、可调试。
