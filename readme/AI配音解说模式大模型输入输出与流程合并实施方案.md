# AI 配音解说模式大模型输入输出与流程合并实施方案

> 目标：这份文档不是概念方案，而是开发实施方案。重点说明：大模型输入怎么压缩、输出怎么瘦身、提示词怎么改、流程怎么合并、代码具体怎么改、兼容旧文件怎么做。  
> 适用项目：当前 `AutomaticEditing` 项目的 AI 配音解说模式。  
> 核心目标：4 分钟左右视频从 15-20 分钟级别压缩到 5-8 分钟，理想情况下接近 5 分钟，同时不明显降低剪辑效果。

---

## 0. 最终建议一句话

当前慢的核心不是 17 步本身，而是：

```text
大模型反复读取完整 timeline JSON + 多个 Agent 串行重复推理 + 视觉 chunk 串行 + ASR 重复加载
```

所以最终应改成：

```text
metadata
→ audio_extract
→ frame_extract
→ chunk_build
→ asr
→ vision_parallel
→ timeline
→ timeline_digest
→ content_analysis
→ short_video_edit_plan
→ voiceover_script_light
→ tts
→ subtitles
→ cut_plan
→ render
```

删除：

```text
risk_review
```

合并：

```text
video_understanding + highlight_detection → content_analysis
short_video_planning + editing_script → short_video_edit_plan
```

轻量化：

```text
voiceover_script 只接收 edit_plan 的 shots，不再读取完整 timeline / video_analysis
```

---

# 1. 当前代码中的问题定位

## 1.1 当前 AI 配音流程

当前 `newsclip_agent/pipeline.py` 中 AI 配音模式步骤为：

```python
AI_VOICEOVER_STEP_ORDER = [
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "vision",
    "timeline",
    "video_understanding",
    "highlight_detection",
    "short_video_planning",
    "editing_script",
    "voiceover_script",
    "risk_review",
    "tts",
    "subtitles",
    "cut_plan",
    "render",
]
```

主要问题是从 `vision` 以后，大模型任务太重。

---

## 1.2 当前文本 Agent 输入过重

当前几个核心步骤的输入大致是：

```python
def step_video_understanding(self):
    self._run_text_agent("video_understanding", {
        "merged_timeline": self._load_step_json("timeline"),
    })


def step_highlight_detection(self):
    self._run_text_agent("highlight_detection", {
        "merged_timeline": self._load_step_json("timeline"),
        "video_analysis": self._load_step_json("video_understanding"),
    })


def step_short_video_planning(self):
    self._run_text_agent("short_video_planning", {
        "candidate_clips": self._load_step_json("highlight_detection"),
        "video_analysis": self._load_step_json("video_understanding"),
        "duration_strategy": self._duration_strategy_payload(),
        "run_options": self._run_options_payload(),
    })


def step_editing_script(self):
    self._run_text_agent("editing_script", {
        "short_video_plan": self._load_step_json("short_video_planning"),
        "candidate_clips": self._load_step_json("highlight_detection"),
        "merged_timeline": self._load_step_json("timeline"),
        "duration_strategy": self._duration_strategy_payload(),
        "run_options": self._run_options_payload(),
    })
```

这会造成几个问题：

1. `timeline` 被反复读；
2. `video_analysis` 被反复读；
3. `candidate_clips` 被反复读；
4. `editing_script` 又重新理解前面已经理解过的视频；
5. JSON 字段太多，大模型输入 token 被浪费在无关字段上。

---

## 1.3 当前 timeline 太重

当前 `step_timeline` 里每个 chunk 会写入：

```python
timeline.append({
    "chunk_id": chunk["chunk_id"],
    "start": seconds_to_timecode(chunk["start"], ms=True),
    "end": seconds_to_timecode(chunk["end"], ms=True),
    "start_seconds": chunk["start"],
    "end_seconds": chunk["end"],
    "asr_text": " ".join(s.get("text", "") for s in segs).strip(),
    "asr_segments": segs,
    "scene_type": vr.get("scene_type", ""),
    "visual_summary": vr.get("visual_summary", ""),
    "screen_text": vr.get("screen_text", []),
    "visible_people": vr.get("visible_people", []),
    "is_live_scene": vr.get("is_live_scene", False),
    "is_archive_footage": vr.get("is_archive_footage", False),
    "visual_value_score": vr.get("visual_value_score", 0),
    "hook_score": vr.get("hook_score", 0),
    "risk_tags": vr.get("risk_tags", []),
    "notes": vr.get("notes", ""),
})
```

其中最不适合直接喂给大模型的是：

```python
"asr_segments": segs
```

因为 `asr_segments` 里面会包含大量逐句时间戳、文本、可能还有模型内部字段。对于高光选择、剪辑规划来说，模型大多数情况下不需要完整 `asr_segments`。

---

# 2. 大模型输入优化：新增 LLM Digest 层

## 2.1 设计原则

不要把完整 JSON 改没。完整 JSON 仍然保留，方便：

1. 断点续跑；
2. 调试；
3. 前端查看；
4. 后续程序按精确时间戳裁剪；
5. 手动编辑。

但是，喂给大模型时不要再用完整 JSON，而是新增一个大模型专用输入视图：

```text
timeline_digest
```

也就是：

```text
完整数据给程序用，压缩数据给大模型用。
```

---

## 2.2 新增文件

建议新增输出文件：

```text
timeline/llm_timeline_digest.json
```

其结构如下：

```json
{
  "version": "llm_timeline_digest_v1",
  "source_versions": {
    "timeline": "v1"
  },
  "video_duration_seconds": 260.0,
  "chunks": [
    {
      "chunk_id": "chunk_0001",
      "time": "00:00:00.000-00:01:00.000",
      "start_seconds": 0,
      "end_seconds": 60,
      "speech": "工信部介绍一季度工业经济和算力发展情况，提到工业增长、算力基础设施、人工智能赋能制造业等内容。",
      "visual": "发布会现场，中景为主，发言人坐在主席台，画面有工信部发布会背景板。",
      "screen_text": ["工业和信息化部", "一季度工业经济", "算力发展"],
      "people": ["发布会发言人"],
      "scene": "发布会",
      "visual_score": 6,
      "hook_score": 5,
      "flags": ["authority_statement"]
    }
  ]
}
```

---

## 2.3 字段保留规则

### 必须保留

| 字段 | 原因 |
|---|---|
| `chunk_id` | 后续映射回原始 chunk |
| `time` | 给模型理解时间范围 |
| `start_seconds` / `end_seconds` | 后续可转成 clip 时间 |
| `speech` | 核心文本信息 |
| `visual` | 核心画面信息 |
| `screen_text` | 新闻画面字幕、机构、数字、地点 |
| `people` | 人物和身份线索 |
| `scene` | 画面类型 |
| `visual_score` | 画面价值 |
| `hook_score` | 开头价值 |
| `flags` | 重要标记，如资料画面、发布会、图表等 |

### 不给大模型传

| 字段 | 原因 |
|---|---|
| `asr_segments` 全量 | 太重，后续裁剪时程序再用 |
| `raw_response` | 只用于 debug |
| `frame list` | 文本 Agent 不需要图片路径 |
| `notes` 长文本 | 容易重复和跑题 |
| 风控字段 | risk_review 已删除 |
| 所有 fallback/debug 字段 | 对剪辑决策无意义 |

---

## 2.4 speech 压缩规则

对于每个 60 秒 chunk，`speech` 建议控制在：

```text
最多 250-350 个中文字符
```

如果 ASR 文本短，直接保留。

如果 ASR 文本长，用规则压缩，不要再调用大模型压缩，否则又增加耗时。

### 简单规则压缩函数

新增文件或放在 `pipeline.py` 中：

```python
def compact_text(text: str, max_chars: int = 320) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_chars:
        return text

    # 优先按中文标点切句
    parts = re.split(r"(?<=[。！？!?；;])", text)
    selected = []
    total = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if total + len(part) > max_chars:
            break
        selected.append(part)
        total += len(part)

    if selected:
        return "".join(selected).strip()

    return text[:max_chars].strip() + "……"
```

### 更稳的新闻关键词保留规则

可以增强为：

```python
NEWS_KEYWORDS = [
    "宣布", "表示", "指出", "强调", "回应", "发布", "数据", "同比", "增长",
    "一季度", "工信部", "算力", "人工智能", "工业", "制造业", "政策",
    "冲突", "袭击", "外交", "会谈", "制裁", "事故", "伤亡",
]


def compact_asr_for_llm(text: str, max_chars: int = 320) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_chars:
        return text

    sentences = [x.strip() for x in re.split(r"(?<=[。！？!?；;])", text) if x.strip()]
    scored = []
    for idx, s in enumerate(sentences):
        score = 0
        for kw in NEWS_KEYWORDS:
            if kw in s:
                score += 2
        # 开头和结尾通常包含导语或总结，稍微加分
        if idx <= 1:
            score += 1
        if idx >= len(sentences) - 2:
            score += 1
        scored.append((score, idx, s))

    # 选高分句，但最终按原顺序拼回，避免语义乱序
    picked = []
    total = 0
    for score, idx, s in sorted(scored, key=lambda x: (-x[0], x[1])):
        if total + len(s) <= max_chars:
            picked.append((idx, s))
            total += len(s)

    picked.sort(key=lambda x: x[0])
    result = "".join(s for _, s in picked).strip()
    return result or text[:max_chars].strip() + "……"
```

---

## 2.5 screen_text / people 压缩规则

大模型不需要所有 OCR 字幕，最多保留 3-5 个。

```python
def compact_list(items: list, max_items: int = 5, max_chars_each: int = 30) -> list[str]:
    out = []
    seen = set()
    for item in items or []:
        if isinstance(item, dict):
            text = item.get("text") or item.get("name") or item.get("value") or json.dumps(item, ensure_ascii=False)
        else:
            text = str(item)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        text = text[:max_chars_each]
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= max_items:
            break
    return out
```

---

## 2.6 flags 生成规则

`flags` 是给大模型看的简洁线索，不需要长字段。

```python
def build_chunk_flags(item: dict) -> list[str]:
    flags = []
    scene = item.get("scene_type") or ""

    if "发布会" in scene:
        flags.append("press_conference")
    if "图表" in scene or "数据" in scene:
        flags.append("data_visual")
    if item.get("is_archive_footage"):
        flags.append("archive_footage")
    if item.get("is_live_scene"):
        flags.append("live_scene")
    if float(item.get("hook_score") or 0) >= 7:
        flags.append("good_opening")
    if float(item.get("visual_value_score") or 0) >= 7:
        flags.append("strong_visual")
    if item.get("visible_people"):
        flags.append("people_visible")

    return flags
```

---

## 2.7 新增 `step_timeline_digest`

在 `AI_VOICEOVER_STEP_ORDER` 中新增：

```python
AI_VOICEOVER_STEP_ORDER = [
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "vision",
    "timeline",
    "timeline_digest",
    "content_analysis",
    "short_video_edit_plan",
    "voiceover_script",
    "tts",
    "subtitles",
    "cut_plan",
    "render",
]
```

在 `DEPENDENCIES` 中新增：

```python
DEPENDENCIES = {
    ...
    "timeline_digest": ["timeline"],
    "content_analysis": ["timeline_digest"],
    "short_video_edit_plan": ["content_analysis", "timeline_digest"],
    "voiceover_script": ["short_video_edit_plan"],
    ...
}
```

新增函数：

```python
def step_timeline_digest(self) -> None:
    timeline_data = self._load_step_json("timeline")
    timeline = timeline_data.get("timeline", [])

    input_hash = stable_hash({
        "timeline": self._step_version("timeline"),
        "digest_version": "llm_timeline_digest_v1",
    })
    if self._can_reuse("timeline_digest", input_hash):
        print("复用缓存: timeline_digest")
        return

    version, vdir = self._version_dir("timeline_digest", "timeline")
    ensure_dir(vdir)

    chunks = []
    for item in timeline:
        chunks.append({
            "chunk_id": item.get("chunk_id", ""),
            "time": f'{item.get("start", "")}-{item.get("end", "")}',
            "start_seconds": item.get("start_seconds", 0),
            "end_seconds": item.get("end_seconds", 0),
            "speech": compact_asr_for_llm(item.get("asr_text", ""), max_chars=320),
            "visual": compact_text(item.get("visual_summary", ""), max_chars=160),
            "screen_text": compact_list(item.get("screen_text", []), max_items=5),
            "people": compact_list(item.get("visible_people", []), max_items=5),
            "scene": item.get("scene_type", ""),
            "visual_score": item.get("visual_value_score", 0),
            "hook_score": item.get("hook_score", 0),
            "flags": build_chunk_flags(item),
        })

    output = {
        "version": "llm_timeline_digest_v1",
        "source_versions": {
            "timeline": self._step_version("timeline"),
        },
        "chunks": chunks,
    }

    out = write_json(vdir / "llm_timeline_digest.json", output)
    self._write_status(vdir, self._base_status("timeline_digest", version, input_hash, [out]))
    self._record_step(
        step="timeline_digest",
        version=version,
        status="success",
        output=relpath(out, self.task_dir),
        input_hash=input_hash,
        output_files=[out],
        extra={"summary": {"chunks": len(chunks)}},
    )
    print("完成: timeline_digest")
```

---

# 3. 大模型输出优化：删掉低价值字段

## 3.1 当前输出太重的问题

当前 `highlight_detection` 输出中有大量 AI 配音模式不需要的原声字段，例如：

```json
{
  "must_keep_original_audio": false,
  "original_audio_reason": "",
  "original_audio_value_score": 0,
  "original_audio_type": "none",
  "original_audio_is_ai_replaceable": true,
  "recommended_original_audio_start": "",
  "recommended_original_audio_end": "",
  "recommended_original_audio_seconds": 0,
  "original_audio_complete_unit": false,
  "original_audio_transcript_summary": "",
  "why_original_audio_beats_ai_voiceover": ""
}
```

但 AI 配音解说模式下，默认不保留原声。这些字段会让模型花很多时间判断“是否保留原声”，但对最终成片没有实际价值。

---

## 3.2 输出字段瘦身原则

### content_analysis 输出只保留

1. 视频主题；
2. 关键事实；
3. 内容结构；
4. 候选片段；
5. 片段价值评分；
6. 推荐理由；
7. 是否需要上下文。

### 不再输出

1. 原声保留策略；
2. 风控审核报告；
3. 原声完整性字段；
4. 发布审核字段；
5. 太多解释性字段。

---

# 4. 流程合并一：`video_understanding + highlight_detection → content_analysis`

## 4.1 合并原因

这两个步骤高度重复：

```text
video_understanding：理解整条视频
highlight_detection：基于理解找候选片段
```

实际可以一次完成。大模型读一遍 `timeline_digest`，同时输出：

1. 整体理解；
2. 视频类型；
3. 关键事实；
4. 候选高光片段。

---

## 4.2 新增 Agent 配置

修改 `AGENT_INFO`：

```python
AGENT_INFO = {
    ...
    "content_analysis": (
        "agents/content_analysis",
        "content_analysis.json",
        prompts.CONTENT_ANALYSIS_PROMPT,
        "content_analysis_v1",
    ),
    "short_video_edit_plan": (
        "agents/short_video_edit_plan",
        "short_video_edit_plan.json",
        prompts.SHORT_VIDEO_EDIT_PLAN_PROMPT,
        "short_video_edit_plan_v1",
    ),
    ...
}
```

旧的 `video_understanding`、`highlight_detection` 可以暂时保留给历史任务和重组模式用。AI 配音模式默认走新 Agent。

---

## 4.3 新增 `step_content_analysis`

```python
def step_content_analysis(self) -> None:
    digest = self._load_step_json("timeline_digest")
    result = self._run_text_agent("content_analysis", {
        "timeline_digest": digest,
        "run_options": self._run_options_payload_minimal(),
    })

    # 兼容旧下游：可选，同时写出 video_analysis.json 和 candidate_clips.json
    self._write_compat_video_understanding_and_highlights(result)
```

新增简化运行参数：

```python
def _run_options_payload_minimal(self) -> dict[str, Any]:
    return {
        "target_duration_seconds": self.options.target_duration_seconds or self.duration_settings.default_target_seconds,
        "allow_long_video": self.options.allow_long_video,
        "audio_policy": self.options.audio_policy,
        "production_mode": self.options.production_mode,
    }
```

不要把完整 `duration_strategy` 每次都传给所有 Agent。只有和时长直接相关的 `short_video_edit_plan`、`voiceover_script` 需要。

---

## 4.4 content_analysis 输入格式

```json
{
  "timeline_digest": {
    "chunks": [
      {
        "chunk_id": "chunk_0001",
        "time": "00:00:00.000-00:01:00.000",
        "speech": "...",
        "visual": "...",
        "screen_text": ["..."],
        "people": ["..."],
        "scene": "发布会",
        "visual_score": 6,
        "hook_score": 5,
        "flags": ["press_conference"]
      }
    ]
  },
  "run_options": {
    "target_duration_seconds": 30,
    "allow_long_video": false,
    "audio_policy": "ai_voiceover",
    "production_mode": "ai_voiceover"
  }
}
```

---

## 4.5 content_analysis 输出格式

```json
{
  "main_topic": "",
  "video_news_type": "发布会 / 权威表态 / 数据信息 / 现场事件 / 解释分析 / 其他",
  "summary": "",
  "key_facts": [
    {
      "fact": "",
      "source_chunk_ids": ["chunk_0001"],
      "confidence": "high"
    }
  ],
  "content_structure": [
    {
      "chunk_ids": ["chunk_0001"],
      "time": "00:00:00.000-00:01:00.000",
      "section_type": "背景 / 核心事实 / 数据 / 表态 / 结尾",
      "summary": ""
    }
  ],
  "candidate_clips": [
    {
      "clip_id": "clip_001",
      "source_chunk_ids": ["chunk_0001", "chunk_0002"],
      "start_seconds": 0,
      "end_seconds": 75,
      "start": "00:00:00.000",
      "end": "00:01:15.000",
      "duration_seconds": 75,
      "clip_type": "核心事实 / 数据 / 表态 / 现场 / 解释",
      "summary": "",
      "why_selected": "",
      "news_value_score": 8,
      "information_density_score": 8,
      "visual_score": 6,
      "hook_score": 7,
      "independence_score": 7,
      "needs_context": false,
      "context_note": "",
      "recommendation": "strong_recommend / recommend / optional / discard"
    }
  ],
  "discard_reason": "如果没有足够高光片段，说明原因"
}
```

---

## 4.6 content_analysis Prompt 最终版

新增到 `newsclip_agent/prompts.py`：

```python
CONTENT_ANALYSIS_PROMPT = NEWS_BACKGROUND + COMMON_JSON_OUTPUT_RULES + """

你是一名凤凰卫视新闻短视频主编。你的任务是基于压缩后的视频时间轴 timeline_digest，一次性完成整条视频理解和高光候选片段识别。

重要说明：
1. 输入是压缩后的 timeline_digest，不是完整逐字稿。你只能基于输入内容判断，不得编造没有出现的事实、数字、人物、地点、机构。
2. 当前是 AI 配音解说模式，原视频声音默认不进入成片。人物讲话和发布会画面可以作为 B-roll，讲话内容由 AI 配音转述。
3. 你的重点不是找娱乐化爆点，而是找新闻价值、信息完整、画面可支撑、适合短视频传播的片段。
4. 不要强行推荐低价值片段。如果整条视频只适合剪 1 条，就只输出 1-2 个候选；如果信息很弱，可以输出空 candidate_clips。

判断维度：
- 新闻重要性
- 时效性
- 信息密度
- 画面证据价值
- 是否可独立成片
- 是否适合作为短视频开头
- 是否需要上下文防止断章取义

候选片段边界规则：
1. 候选片段可以跨多个 chunk，但不能跨越无关主题。
2. start_seconds/end_seconds 可以先给粗略边界，后续剪辑规划会细化镜头。
3. 如果某个片段需要前后背景，请 needs_context=true，并说明 context_note。
4. 发布会/政策/数据新闻优先选择“核心结论 + 必要背景 + 关键数据”的组合，不要只选开场寒暄或空镜。
5. 主持人口播不是优先画面，除非它承载核心事实或没有更好画面。

只输出 JSON，格式如下：
{
  "main_topic": "",
  "video_news_type": "发布会 / 权威表态 / 数据信息 / 现场事件 / 解释分析 / 其他",
  "summary": "",
  "key_facts": [
    {
      "fact": "",
      "source_chunk_ids": [],
      "confidence": "high / medium / low"
    }
  ],
  "content_structure": [
    {
      "chunk_ids": [],
      "time": "",
      "section_type": "背景 / 核心事实 / 数据 / 表态 / 结尾 / 其他",
      "summary": ""
    }
  ],
  "candidate_clips": [
    {
      "clip_id": "clip_001",
      "source_chunk_ids": [],
      "start_seconds": 0,
      "end_seconds": 0,
      "start": "HH:MM:SS.mmm",
      "end": "HH:MM:SS.mmm",
      "duration_seconds": 0,
      "clip_type": "核心事实 / 数据 / 表态 / 现场 / 解释 / 其他",
      "summary": "",
      "why_selected": "",
      "news_value_score": 0,
      "information_density_score": 0,
      "visual_score": 0,
      "hook_score": 0,
      "independence_score": 0,
      "needs_context": false,
      "context_note": "",
      "recommendation": "strong_recommend / recommend / optional / discard"
    }
  ],
  "discard_reason": ""
}
"""
```

---

# 5. 流程合并二：`short_video_planning + editing_script → short_video_edit_plan`

## 5.1 合并原因

当前 `short_video_planning` 负责决定剪几条、每条主题。  
`editing_script` 再负责每条的标题、封面、镜头结构。

这两个步骤强依赖，且模型会重复阅读 `candidate_clips` 和 `timeline`。

建议合并为：

```text
short_video_edit_plan
```

一次输出：

1. 推荐视频数量；
2. 每条视频主题；
3. 标题；
4. 封面文案；
5. 目标时长；
6. 使用哪些候选片段；
7. 具体镜头结构；
8. 每个镜头的画面说明；
9. 每个镜头要讲的事实；
10. 给旁白 Agent 的 brief。

---

## 5.2 新增 `step_short_video_edit_plan`

```python
def step_short_video_edit_plan(self) -> None:
    content = self._load_step_json("content_analysis")
    digest = self._load_step_json("timeline_digest")

    result = self._run_text_agent("short_video_edit_plan", {
        "content_analysis": self._compact_content_analysis_for_edit_plan(content),
        "timeline_digest": self._select_digest_for_candidates(digest, content),
        "duration_strategy": self._duration_strategy_payload_for_edit_plan(),
        "run_options": self._run_options_payload_minimal(),
    })

    self._write_compat_short_video_plan_and_editing_script(result)
```

重点：这里不要把完整 `timeline_digest` 无脑传入。只传候选片段涉及的 chunk 和相邻上下文。

---

## 5.3 只传相关 chunk，不传全量 digest

新增函数：

```python
def _select_digest_for_candidates(self, digest: dict[str, Any], content: dict[str, Any], neighbor: int = 1) -> dict[str, Any]:
    chunks = digest.get("chunks", [])
    if not chunks:
        return {"chunks": []}

    id_to_index = {c.get("chunk_id"): i for i, c in enumerate(chunks)}
    selected_indexes = set()

    for clip in content.get("candidate_clips", []):
        for cid in clip.get("source_chunk_ids", []):
            if cid not in id_to_index:
                continue
            idx = id_to_index[cid]
            for j in range(max(0, idx - neighbor), min(len(chunks), idx + neighbor + 1)):
                selected_indexes.add(j)

    # 如果模型没给 source_chunk_ids，就用 start/end 秒数兜底
    for clip in content.get("candidate_clips", []):
        start = float(clip.get("start_seconds") or 0)
        end = float(clip.get("end_seconds") or 0)
        if end <= start:
            continue
        for i, c in enumerate(chunks):
            c_start = float(c.get("start_seconds") or 0)
            c_end = float(c.get("end_seconds") or 0)
            if c_end >= start and c_start <= end:
                for j in range(max(0, i - neighbor), min(len(chunks), i + neighbor + 1)):
                    selected_indexes.add(j)

    # 如果还是空，最多传前 8 个 chunk，避免全量失控
    if not selected_indexes:
        selected_indexes = set(range(min(len(chunks), 8)))

    return {
        "version": digest.get("version", "llm_timeline_digest_v1"),
        "chunks": [chunks[i] for i in sorted(selected_indexes)],
    }
```

这样 `short_video_edit_plan` 的输入通常只有 2-5 个 chunk，而不是完整视频所有内容。

---

## 5.4 content_analysis 再压缩一次

```python
def _compact_content_analysis_for_edit_plan(self, content: dict[str, Any]) -> dict[str, Any]:
    return {
        "main_topic": content.get("main_topic", ""),
        "video_news_type": content.get("video_news_type", ""),
        "summary": compact_text(content.get("summary", ""), 240),
        "key_facts": content.get("key_facts", [])[:10],
        "candidate_clips": [
            {
                "clip_id": c.get("clip_id", ""),
                "source_chunk_ids": c.get("source_chunk_ids", []),
                "start_seconds": c.get("start_seconds", 0),
                "end_seconds": c.get("end_seconds", 0),
                "start": c.get("start", ""),
                "end": c.get("end", ""),
                "duration_seconds": c.get("duration_seconds", 0),
                "clip_type": c.get("clip_type", ""),
                "summary": compact_text(c.get("summary", ""), 160),
                "why_selected": compact_text(c.get("why_selected", ""), 120),
                "news_value_score": c.get("news_value_score", 0),
                "information_density_score": c.get("information_density_score", 0),
                "visual_score": c.get("visual_score", 0),
                "hook_score": c.get("hook_score", 0),
                "independence_score": c.get("independence_score", 0),
                "needs_context": c.get("needs_context", False),
                "context_note": compact_text(c.get("context_note", ""), 120),
                "recommendation": c.get("recommendation", ""),
            }
            for c in content.get("candidate_clips", [])
            if c.get("recommendation") != "discard"
        ][:8],
    }
```

候选片段最多传 8 个，避免模型花时间规划低价值片段。

---

## 5.5 duration_strategy 也要瘦身

当前 `_duration_strategy_payload()` 字段很多。对于 `short_video_edit_plan`，只需要：

```python
def _duration_strategy_payload_for_edit_plan(self) -> dict[str, Any]:
    settings = self.duration_settings
    return {
        "default_target_seconds": self.options.target_duration_seconds or settings.default_target_seconds,
        "allow_long_video": self.options.allow_long_video,
        "normal_range_seconds": [settings.normal_min_seconds, settings.normal_max_seconds],
        "context_range_seconds": [35, settings.context_max_seconds],
        "complex_range_seconds": [45, settings.complex_max_seconds],
        "hard_max_without_confirmation": settings.hard_max_without_confirmation,
        "audio_policy": self.options.audio_policy,
        "chars_per_second": settings.chars_per_second,
    }
```

---

## 5.6 short_video_edit_plan 输出格式

```json
{
  "recommended_video_count": 2,
  "overall_reason": "",
  "scripts": [
    {
      "short_video_id": "sv_01",
      "topic": "",
      "news_angle": "",
      "title": "",
      "cover_text": "",
      "target_duration_seconds": 35,
      "max_allowed_seconds": 45,
      "source_clip_ids": ["clip_001"],
      "must_keep_fact_points": [""],
      "voiceover_brief": "",
      "editing_structure": [
        {
          "shot_id": "sv_01_s01",
          "order": 1,
          "target_timeline": "00:00-00:04",
          "source_start": "00:00:10.000",
          "source_end": "00:00:16.000",
          "source_start_seconds": 10,
          "source_end_seconds": 16,
          "duration_seconds": 6,
          "purpose": "opening_hook",
          "visual": "",
          "news_fact_to_explain": "",
          "must_say_facts": [""],
          "audio_mode": "ai_voiceover",
          "subtitle_hint": ""
        }
      ],
      "subtitle_keywords": []
    }
  ],
  "discarded_clips": [
    {
      "clip_id": "clip_003",
      "reason": ""
    }
  ]
}
```

### 关键变化

旧的 `editing_script` 里大量原声字段删除：

```text
original_audio_required
original_audio_reason
original_audio_policy
original_audio_type
original_audio_value_score
original_audio_complete_unit
original_audio_transcript_summary
```

AI 配音模式全部默认：

```json
"audio_mode": "ai_voiceover"
```

除非你未来重新开启混原声模式，否则不要让模型判断原声。

---

## 5.7 short_video_edit_plan Prompt 最终版

新增到 `prompts.py`：

```python
SHORT_VIDEO_EDIT_PLAN_PROMPT = NEWS_BACKGROUND + COMMON_JSON_OUTPUT_RULES + """

你是一名凤凰卫视新闻短视频剪辑导演。你需要根据 content_analysis 和相关 timeline_digest，一次性完成短视频数量规划和剪辑脚本生成。

当前模式：AI 配音解说模式。
重要规则：
1. 原视频声音默认不进入成片，所有镜头 audio_mode 默认 ai_voiceover。
2. 人物讲话、发布会、图表、现场画面都可以作为 B-roll 画面，但内容由 AI 配音解释。
3. 不要输出 original_sound、mixed_evidence 或任何原声保留字段。
4. 不要强行拆多条。只有当不同片段有明确独立新闻点时才拆多条。
5. 如果多个候选片段共同构成同一事件的“背景 → 核心事实 → 数据/影响”，应优先合并为一条更完整的视频。
6. 每条短视频必须事实完整，不能只截一个没有上下文的片段。
7. 镜头选择要服务旁白：每个 shot 必须有 news_fact_to_explain，告诉后续旁白 Agent 这一镜头要讲什么。
8. source_start/source_end 必须来自输入时间范围内，不得编造不存在的时间段。
9. 不要把主持人口播当作默认优先画面；有发布会、图表、现场、权威画面时优先使用这些画面。
10. 如果画面变化不大，可以用较短镜头，不要为了凑时长保留长段原片。

时长规则：
- 快讯/单点事实：20-30 秒。
- 普通新闻高光：28-35 秒。
- 需要背景的数据/政策/发布会类：35-45 秒。
- 复杂解释类：45-60 秒，但除非 allow_long_video=true，否则尽量不要超过 60 秒。
- target_duration_seconds 是目标，不是必须精确等于；视频时长应服务讲清楚。

镜头结构建议：
- opening_hook：2-5 秒，给出核心看点画面。
- context：4-8 秒，补必要背景。
- main_fact：8-15 秒，讲核心事实。
- detail_or_data：4-10 秒，放数据、表态、图表或现场细节。
- ending：3-6 秒，明确收尾。

输出 JSON：
{
  "recommended_video_count": 0,
  "overall_reason": "",
  "scripts": [
    {
      "short_video_id": "sv_01",
      "topic": "",
      "news_angle": "",
      "title": "",
      "cover_text": "",
      "target_duration_seconds": 30,
      "max_allowed_seconds": 35,
      "source_clip_ids": [],
      "must_keep_fact_points": [],
      "voiceover_brief": "",
      "editing_structure": [
        {
          "shot_id": "sv_01_s01",
          "order": 1,
          "target_timeline": "00:00-00:04",
          "source_start": "HH:MM:SS.mmm",
          "source_end": "HH:MM:SS.mmm",
          "source_start_seconds": 0,
          "source_end_seconds": 0,
          "duration_seconds": 0,
          "purpose": "opening_hook / context / main_fact / detail_or_data / ending",
          "visual": "",
          "news_fact_to_explain": "",
          "must_say_facts": [],
          "audio_mode": "ai_voiceover",
          "subtitle_hint": ""
        }
      ],
      "subtitle_keywords": []
    }
  ],
  "discarded_clips": [
    {
      "clip_id": "",
      "reason": ""
    }
  ]
}
"""
```

---

# 6. voiceover_script 轻量化

## 6.1 当前问题

当前 `step_voiceover_script` 输入：

```python
final_output = self._run_text_agent("voiceover_script", {
    "editing_script": editing,
    "voiceover_timing_contracts": contracts,
    "video_analysis": self._load_step_json("video_understanding"),
    "duration_strategy": self._duration_strategy_payload(),
    "run_options": self._run_options_payload(),
})
```

这里 `video_analysis` 已经不应该再传。旁白 Agent 不应该重新理解视频，只应该根据镜头结构写解说。

---

## 6.2 新版输入

```python
def step_voiceover_script(self) -> None:
    edit_plan = self._load_step_json("short_video_edit_plan")

    voiceover_input = self._build_voiceover_light_input(edit_plan)

    final_output = self._run_text_agent("voiceover_script", voiceover_input)
    self._normalize_voiceover_scripts(final_output)
    self._enforce_long_video_confirmation_after_voiceover(final_output)
```

---

## 6.3 构造轻量 voiceover 输入

```python
def _build_voiceover_light_input(self, edit_plan: dict[str, Any]) -> dict[str, Any]:
    scripts = []
    for script in edit_plan.get("scripts", []):
        shots = []
        for shot in script.get("editing_structure", []):
            shots.append({
                "shot_id": shot.get("shot_id") or f'{script.get("short_video_id", "sv")}_s{shot.get("order", 0)}',
                "order": shot.get("order", 0),
                "target_timeline": shot.get("target_timeline", ""),
                "duration_seconds": shot.get("duration_seconds", 0),
                "purpose": shot.get("purpose", ""),
                "visual": compact_text(shot.get("visual", ""), 120),
                "news_fact_to_explain": compact_text(shot.get("news_fact_to_explain", ""), 160),
                "must_say_facts": shot.get("must_say_facts", [])[:5],
                "subtitle_hint": compact_text(shot.get("subtitle_hint", ""), 60),
            })

        scripts.append({
            "short_video_id": script.get("short_video_id", ""),
            "title": script.get("title", ""),
            "topic": script.get("topic", ""),
            "news_angle": script.get("news_angle", ""),
            "target_duration_seconds": script.get("target_duration_seconds", 30),
            "max_allowed_seconds": script.get("max_allowed_seconds", 35),
            "must_keep_fact_points": script.get("must_keep_fact_points", [])[:8],
            "voiceover_brief": compact_text(script.get("voiceover_brief", ""), 240),
            "shots": shots,
        })

    return {
        "voiceover_jobs": scripts,
        "style": {
            "tone": "凤凰卫视新闻解说，专业、克制、清楚、有信息密度",
            "forbidden_phrases": [
                "记者报道",
                "本台记者",
                "凤凰卫视记者",
                "发回报道",
                "为您报道",
                "下面来看",
                "以上是",
            ],
        },
        "duration_strategy": {
            "chars_per_second": self.duration_settings.chars_per_second,
            "tts_actual_chars_per_second": self.duration_settings.tts_actual_chars_per_second,
            "inter_sentence_gap_seconds": self.duration_settings.inter_sentence_gap_seconds,
        },
        "run_options": self._run_options_payload_minimal(),
    }
```

---

## 6.4 新版 voiceover 输出格式

```json
{
  "scripts": [
    {
      "short_video_id": "sv_01",
      "voiceover_style": "news",
      "target_duration_seconds": 35,
      "target_char_count": 130,
      "actual_char_count": 126,
      "estimated_duration_seconds": 33.5,
      "narration_segments": [
        {
          "shot_id": "sv_01_s01",
          "text": "",
          "char_count": 0,
          "estimated_duration_seconds": 0,
          "reason": ""
        }
      ],
      "narration_text": "",
      "opening_hook": "",
      "ending_sentence": "",
      "duration_fit": "ok / too_short / too_long",
      "revise_suggestion": ""
    }
  ]
}
```

这里建议去掉 `target_start` / `target_end` 强绑定，除非你仍想做 segment aligned TTS。AI 配音模式下更推荐连续旁白，由 TTS 实际时长反推镜头节奏。

如果你现在的 `tts` 和 `subtitles` 已依赖 `narration_segments.target_start/target_end`，可以过渡期保留，但由程序自动补齐，不让模型生成。

---

## 6.5 程序自动补 segment 时间

```python
def fill_voiceover_segment_times(self, voiceover: dict[str, Any], edit_plan: dict[str, Any]) -> dict[str, Any]:
    shot_time_by_id = {}
    for script in edit_plan.get("scripts", []):
        for shot in script.get("editing_structure", []):
            sid = shot.get("shot_id")
            if not sid:
                continue
            target = shot.get("target_timeline", "")
            # 也可以根据 order 和 duration_seconds 累计生成
            shot_time_by_id[sid] = {
                "target_timeline": target,
                "duration_seconds": shot.get("duration_seconds", 0),
            }

    for script in voiceover.get("scripts", []):
        cursor = 0.0
        for seg in script.get("narration_segments", []):
            shot_id = seg.get("shot_id", "")
            duration = float(shot_time_by_id.get(shot_id, {}).get("duration_seconds") or 0)
            if duration <= 0:
                duration = max(float(seg.get("estimated_duration_seconds") or 0), 2.0)
            seg["target_start"] = seconds_to_timecode(cursor, ms=True)
            seg["target_end"] = seconds_to_timecode(cursor + duration, ms=True)
            seg["target_duration_seconds"] = duration
            cursor += duration
    return voiceover
```

---

## 6.6 voiceover Prompt 最终版

替换或新增：

```python
VOICEOVER_LIGHT_PROMPT = NEWS_BACKGROUND + COMMON_JSON_OUTPUT_RULES + """

你是一名凤凰卫视新闻解说编辑。你只负责根据 voiceover_jobs 写 AI 配音文案，不需要重新选择高光片段，也不要重新规划剪辑。

输入说明：
- 每个 voiceover_job 是一条短视频。
- shots 是已经确定的镜头结构。
- 每个 shot 中的 news_fact_to_explain 和 must_say_facts 是这一镜头必须表达的信息。
- 你不得编造 shots 和 must_keep_fact_points 之外的新事实、数字、人物、地点。

写作要求：
1. 专业、克制、清楚、有信息密度。
2. 不要标题党，不要营销腔。
3. 不要写“记者报道”“本台记者”“凤凰卫视记者”“发回报道”“为您报道”“下面来看”“以上是”等报道署名或主持引导语。
4. 每条短视频要有起承转合：开头交代核心看点，中间讲清事实，必要时补背景或影响，最后明确收尾。
5. narration_segments 必须和 shots 一一对应，shot_id 必须来自输入。
6. 每个 segment 的 text 要服务对应 shot，不要把后面镜头的信息提前到前面。
7. 如果某个 shot 信息量不足，可以用更简洁句子，但不能编造。
8. narration_text 必须等于所有 narration_segments.text 按顺序拼接后的全文。
9. 按目标时长控制字数：中文新闻解说通常每秒 3.5-4.2 字。30 秒约 105-125 字，45 秒约 155-190 字，60 秒约 210-250 字。

只输出 JSON：
{
  "scripts": [
    {
      "short_video_id": "",
      "voiceover_style": "news",
      "target_duration_seconds": 30,
      "target_char_count": 120,
      "actual_char_count": 0,
      "estimated_duration_seconds": 0,
      "narration_segments": [
        {
          "shot_id": "",
          "text": "",
          "char_count": 0,
          "estimated_duration_seconds": 0,
          "reason": ""
        }
      ],
      "narration_text": "",
      "opening_hook": "",
      "ending_sentence": "",
      "duration_fit": "ok / too_short / too_long",
      "revise_suggestion": ""
    }
  ]
}
"""
```

然后 `AGENT_INFO["voiceover_script"]` 的 prompt 改成 `VOICEOVER_LIGHT_PROMPT`。

---

# 7. 视觉分析并发和抽帧优化

## 7.1 当前问题

当前 `step_vision` 是串行：

```python
for chunk in chunks:
    self.llm_vision.call_json(...)
```

应改成线程池并发。

---

## 7.2 新增参数

在 `RunOptions` 中新增：

```python
vision_max_workers: int = 3
vision_max_frames_per_chunk: int = 6
```

命令行参数新增：

```python
parser.add_argument("--vision-max-workers", type=int, default=3)
parser.add_argument("--vision-max-frames-per-chunk", type=int, default=6)
```

默认抽帧从：

```python
frame_interval: int = 5
```

改为：

```python
frame_interval: int = 10
```

---

## 7.3 并发伪代码

```python
from concurrent.futures import ThreadPoolExecutor, as_completed


def step_vision(self) -> None:
    ...
    chunks_to_process = []

    for chunk in chunks:
        # 复用逻辑保持不变
        if can_reuse_chunk:
            chunk_records.append(reused_record)
        else:
            chunks_to_process.append(chunk)

    def process_one_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
        cid = chunk["chunk_id"]
        cdir = ensure_dir(vdir / cid)
        frame_paths = [
            str(self.task_dir / f)
            for f in chunk.get("frames", [])
            if (self.task_dir / f).exists()
        ]
        frame_paths = select_frames_for_vision(
            frame_paths,
            max_frames=self.options.vision_max_frames_per_chunk,
        )

        asr_segments = self._segments_in_range(asr.get("segments", []), chunk["start"], chunk["end"])
        input_data = {
            "chunk": compact_chunk_for_vision(chunk),
            "asr_text": compact_asr_for_llm(" ".join(s.get("text", "") for s in asr_segments), 500),
            "prompt_version": prompt_version,
            "model": model,
        }

        write_json(cdir / "input.json", input_data)
        write_text(cdir / "prompt.txt", prompts.VISION_CHUNK_PROMPT)

        try:
            result = self.llm_vision.call_json(
                model=model,
                fallback_models=fallback,
                prompt=prompts.VISION_CHUNK_PROMPT,
                input_data=input_data,
                image_paths=frame_paths,
                temperature=0.1,
            )
            parsed = result.parsed
            parsed.setdefault("chunk_id", cid)
            parsed.setdefault("time_range", chunk["time_range"])
            status = "success"
            error = ""
        except Exception as exc:
            parsed = {"chunk_id": cid, "time_range": chunk["time_range"], "error": str(exc)}
            status = "failed"
            error = str(exc)

        write_json(cdir / "parsed_result.json", parsed)
        return {
            "chunk_id": cid,
            "status": status,
            "error": error,
            "result_file": relpath(cdir / "parsed_result.json", self.task_dir),
            "result": parsed,
        }

    max_workers = max(1, int(self.options.vision_max_workers or 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_one_chunk, c) for c in chunks_to_process]
        for future in as_completed(futures):
            record = future.result()
            chunk_records.append(record)
            print(f"vision {record['chunk_id']}: {record['status']}")

    # 排序，保证输出稳定
    chunk_records.sort(key=lambda x: x.get("chunk_id", ""))
    ...
```

---

## 7.4 关键帧选择函数

不要每个 chunk 固定传前 12 帧。建议每 chunk 最多 6 帧，从头中尾均匀采样。

```python
def select_frames_for_vision(frame_paths: list[str], max_frames: int = 6) -> list[str]:
    if len(frame_paths) <= max_frames:
        return frame_paths
    if max_frames <= 1:
        return [frame_paths[0]]

    indexes = []
    n = len(frame_paths)
    for i in range(max_frames):
        idx = round(i * (n - 1) / (max_frames - 1))
        indexes.append(idx)

    # 去重并保持顺序
    seen = set()
    selected = []
    for idx in indexes:
        if idx not in seen:
            seen.add(idx)
            selected.append(frame_paths[idx])
    return selected
```

---

## 7.5 合理抽帧默认值

建议默认：

```text
chunk_seconds = 60
frame_interval = 10
vision_max_frames_per_chunk = 6
vision_max_workers = 3
```

不同视频类型建议：

| 视频类型 | frame_interval | max_frames_per_chunk |
|---|---:|---:|
| 发布会 / 口播 / 演播室 | 10-15 秒 | 4-6 |
| 普通新闻素材 | 10 秒 | 6 |
| 现场事件 / 冲突 / 事故 | 5-8 秒 | 8 |
| 调试高精度模式 | 3-5 秒 | 8-12 |

默认不要再用 5 秒一帧。4 分钟新闻视频 10 秒一帧已经够用。

---

# 8. ASR 常驻复用

## 8.1 当前问题

ASR 实际识别很快，慢在每个任务重新加载 FunASR 模型。

---

## 8.2 修改方式

在 `PipelineRunner.__init__` 中新增：

```python
self._asr_engine = None
```

新增方法：

```python
def _get_asr_engine(self):
    if self._asr_engine is not None:
        return self._asr_engine

    from services.asr.FunASREngine import FunASREngine

    self._asr_engine = FunASREngine(
        model_dir=self.config.asr.get("model_dir"),
        device=self.config.asr.get("device", "cuda:0"),
        # 其他原本 step_asr 使用的参数照搬
    )
    return self._asr_engine
```

`step_asr` 中从：

```python
engine = FunASREngine(...)
result = engine.transcribe(str(audio))
engine.release()
```

改成：

```python
engine = self._get_asr_engine()
result = engine.transcribe(str(audio))
```

不要在每个任务结束后 release。

如果担心显存占用，可以加命令行参数：

```python
parser.add_argument("--release-asr-after-task", action="store_true")
```

然后：

```python
if self.options.release_asr_after_task and self._asr_engine:
    self._asr_engine.release()
    self._asr_engine = None
```

但默认不要释放。

---

# 9. 兼容旧下游的迁移方案

因为后面的 `tts`、`subtitles`、`cut_plan`、`render` 已经依赖旧文件名：

```text
short_video_planning
editing_script
voiceover_script
```

所以建议第一阶段不要大范围改后处理，而是让新步骤写出兼容文件。

---

## 9.1 short_video_edit_plan 兼容 editing_script

新增：

```python
def _write_compat_short_video_plan_and_editing_script(self, edit_plan: dict[str, Any]) -> None:
    # 1. 兼容 short_video_planning
    short_video_plan = {
        "recommended_video_count": edit_plan.get("recommended_video_count", len(edit_plan.get("scripts", []))),
        "overall_reason": edit_plan.get("overall_reason", ""),
        "short_videos": [],
        "discarded_clips": edit_plan.get("discarded_clips", []),
    }

    for script in edit_plan.get("scripts", []):
        short_video_plan["short_videos"].append({
            "short_video_id": script.get("short_video_id", ""),
            "topic": script.get("topic", ""),
            "news_angle": script.get("news_angle", ""),
            "video_type": script.get("video_type", "解说型"),
            "target_duration_seconds": script.get("target_duration_seconds", 30),
            "max_allowed_seconds": script.get("max_allowed_seconds", 35),
            "source_clip_ids": script.get("source_clip_ids", []),
            "must_keep_fact_points": script.get("must_keep_fact_points", []),
            "visual_selection_strategy": "由 short_video_edit_plan 生成",
            "audio_strategy": "ai_voiceover_main",
            "priority": "A",
            "reason": script.get("voiceover_brief", ""),
            "required_context": "",
        })

    # 2. 兼容 editing_script
    editing_script = {
        "scripts": []
    }
    for script in edit_plan.get("scripts", []):
        editing_script["scripts"].append({
            "short_video_id": script.get("short_video_id", ""),
            "title": script.get("title", ""),
            "cover_text": script.get("cover_text", ""),
            "target_duration_seconds": script.get("target_duration_seconds", 30),
            "max_allowed_seconds": script.get("max_allowed_seconds", 35),
            "estimated_total_duration_seconds": sum(
                float(s.get("duration_seconds") or 0)
                for s in script.get("editing_structure", [])
            ),
            "duration_reason": script.get("voiceover_brief", ""),
            "editing_structure": [
                {
                    "order": shot.get("order", idx + 1),
                    "shot_id": shot.get("shot_id", f'{script.get("short_video_id", "sv")}_s{idx+1:02d}'),
                    "target_timeline": shot.get("target_timeline", ""),
                    "source_start": shot.get("source_start", ""),
                    "source_end": shot.get("source_end", ""),
                    "duration_seconds": shot.get("duration_seconds", 0),
                    "purpose": shot.get("purpose", ""),
                    "visual": shot.get("visual", ""),
                    "visual_evidence_type": "",
                    "visual_selection_reason": "",
                    "importance": "must_keep",
                    "audio_mode": "ai_voiceover",
                    "original_audio_required": False,
                    "subtitle": shot.get("subtitle_hint", ""),
                    "editing_note": shot.get("news_fact_to_explain", ""),
                    "news_fact_to_explain": shot.get("news_fact_to_explain", ""),
                    "must_say_facts": shot.get("must_say_facts", []),
                }
                for idx, shot in enumerate(script.get("editing_structure", []))
            ],
            "need_voiceover": True,
            "subtitle_keywords": script.get("subtitle_keywords", []),
        })

    # 这里有两种策略：
    # A. 真正写到对应 step 的版本目录，并 record_step。
    # B. 只在当前 step 输出中保留，然后修改 _load_step_json 做 alias。
    # 推荐 A，兼容性更好。
```

---

## 9.2 更简单的 alias 方案

修改 `_load_step_json` 或新增 `_load_optional_step_json` 的 alias：

```python
STEP_ALIASES = {
    "short_video_planning": "short_video_edit_plan",
    "editing_script": "short_video_edit_plan",
}
```

但这会让后续读取字段时出问题，因为 `short_video_edit_plan` 的根结构不完全等于旧 `editing_script`。所以更推荐写兼容文件。

---

# 10. 删除 risk_review

## 10.1 流程删除

从 `AI_VOICEOVER_STEP_ORDER` 删除：

```python
"risk_review",
```

从 `DEPENDENCIES` 中可以保留历史项，但主流程不再触发。也可以直接删掉：

```python
"risk_review": ["editing_script", "voiceover_script", "video_understanding"],
```

`AGENT_INFO` 中可以保留，未来如果做“发布审核模式”再手动执行。

---

## 10.2 长视频确认跳转要改

当前 `_enforce_long_video_confirmation_after_voiceover` 中有：

```python
"continue_command": "--allow-long-video --rerun-from risk_review",
"continue_from_step": "risk_review",
```

因为 `risk_review` 删除了，所以要改成：

```python
"continue_command": "--allow-long-video --rerun-from tts",
"continue_from_step": "tts",
```

---

# 11. 具体代码改造清单

## 11.1 `pipeline.py` 必改点

### 修改 1：AI 步骤顺序

```python
AI_VOICEOVER_STEP_ORDER = [
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "vision",
    "timeline",
    "timeline_digest",
    "content_analysis",
    "short_video_edit_plan",
    "voiceover_script",
    "tts",
    "subtitles",
    "cut_plan",
    "render",
]
```

### 修改 2：依赖关系

```python
DEPENDENCIES.update({
    "timeline_digest": ["timeline"],
    "content_analysis": ["timeline_digest"],
    "short_video_edit_plan": ["content_analysis", "timeline_digest"],
    "voiceover_script": ["short_video_edit_plan"],
    "tts": ["voiceover_script"],
    "subtitles": ["voiceover_script", "tts"],
    "cut_plan": ["short_video_edit_plan", "voiceover_script", "subtitles", "tts"],
    "render": ["cut_plan"],
})
```

注意：如果 `cut_plan` 当前强依赖 `editing_script`，可以先让 `short_video_edit_plan` 写兼容 `editing_script`，这样 `cut_plan` 暂时不用改。

### 修改 3：AGENT_INFO

```python
AGENT_INFO.update({
    "content_analysis": (
        "agents/content_analysis",
        "content_analysis.json",
        prompts.CONTENT_ANALYSIS_PROMPT,
        "content_analysis_v1",
    ),
    "short_video_edit_plan": (
        "agents/short_video_edit_plan",
        "short_video_edit_plan.json",
        prompts.SHORT_VIDEO_EDIT_PLAN_PROMPT,
        "short_video_edit_plan_v1",
    ),
})
```

### 修改 4：新增 digest 工具函数

新增：

```python
compact_text
compact_asr_for_llm
compact_list
build_chunk_flags
select_frames_for_vision
```

可以放在 `pipeline.py` 顶部，也可以放到 `utils.py`。

推荐放到新文件：

```text
newsclip_agent/llm_digest.py
```

然后：

```python
from .llm_digest import (
    compact_text,
    compact_asr_for_llm,
    compact_list,
    build_chunk_flags,
    select_frames_for_vision,
)
```

### 修改 5：新增 step

```python
step_timeline_digest
step_content_analysis
step_short_video_edit_plan
```

### 修改 6：修改 step_voiceover_script

从读取旧的 `editing_script + video_analysis` 改为读取：

```python
short_video_edit_plan
```

并构造轻量输入。

### 修改 7：修改 step_vision 为线程池并发

新增 `ThreadPoolExecutor`。

### 修改 8：修改 step_asr 为常驻复用

新增 `_get_asr_engine`。

---

## 11.2 `prompts.py` 必改点

新增：

```python
CONTENT_ANALYSIS_PROMPT
SHORT_VIDEO_EDIT_PLAN_PROMPT
VOICEOVER_LIGHT_PROMPT
```

然后：

```python
AGENT_INFO["voiceover_script"] = (..., prompts.VOICEOVER_LIGHT_PROMPT, ...)
```

旧 prompt 可以保留，便于对比和回滚。

---

## 11.3 `run_pipeline.py` / CLI 必改点

将默认抽帧间隔从 5 改成 10。

新增参数：

```python
parser.add_argument("--vision-max-workers", type=int, default=3)
parser.add_argument("--vision-max-frames-per-chunk", type=int, default=6)
parser.add_argument("--release-asr-after-task", action="store_true")
```

并写入 `RunOptions`。

---

## 11.4 `config.toml` 建议新增

```toml
[workflow]
default_frame_interval = 10
vision_max_workers = 3
vision_max_frames_per_chunk = 6

[llm_input]
max_asr_chars_per_chunk = 320
max_visual_chars_per_chunk = 160
max_screen_text_items = 5
max_people_items = 5
max_candidate_clips_for_edit_plan = 8
candidate_neighbor_chunks = 1
```

如果不想改配置系统，先写死默认值也可以。

---

# 12. 输入规模目标

优化后，大模型输入应控制在：

| Agent | 输入目标 |
|---|---:|
| content_analysis | 4000-8000 字符 |
| short_video_edit_plan | 3000-7000 字符 |
| voiceover_script_light | 2000-5000 字符 |

不要再出现 3.2 万字符级别输入。  
如果某一步输入超过 1 万字符，应在日志里 warning。

新增调试日志：

```python
def _log_llm_input_size(self, step: str, input_data: dict[str, Any]) -> None:
    text = json.dumps(input_data, ensure_ascii=False)
    print(f"LLM input size [{step}]: {len(text)} chars")
    if len(text) > 10000:
        print(f"警告: {step} 输入超过 10000 字符，建议检查 digest 压缩")
```

在 `_run_text_agent` 里调用：

```python
self._log_llm_input_size(step, input_data)
```

---

# 13. 推荐落地顺序

## 第 1 阶段：低风险提速

1. 删除主流程 `risk_review`；
2. 视觉并发；
3. 抽帧默认 10 秒；
4. 每 chunk 最多 6 帧；
5. ASR 常驻。

预期：

```text
15-20 分钟 → 8-12 分钟
```

---

## 第 2 阶段：输入压缩

1. 新增 `timeline_digest`；
2. 所有文本 Agent 改读 digest；
3. `_run_text_agent` 打印输入字符数；
4. voiceover_script 去掉 video_analysis。

预期：

```text
8-12 分钟 → 6-9 分钟
```

---

## 第 3 阶段：Agent 合并

1. `video_understanding + highlight_detection → content_analysis`；
2. `short_video_planning + editing_script → short_video_edit_plan`；
3. 写兼容文件，避免后处理大改；
4. voiceover 使用轻量 prompt。

预期：

```text
6-9 分钟 → 4.5-7 分钟
```

---

# 14. 最终验收标准

## 14.1 性能指标

4 分钟左右视频：

```text
首次任务：6-8 分钟以内
ASR 模型已热启动：5-7 分钟以内
理想情况：接近 5 分钟
```

## 14.2 输入大小指标

```text
content_analysis 输入 < 8000 字符
short_video_edit_plan 输入 < 7000 字符
voiceover_script 输入 < 5000 字符
```

## 14.3 效果指标

至少检查：

1. 是否仍能选出新闻价值最高的片段；
2. 是否没有把资料画面误当现场；
3. 标题是否不标题党；
4. AI 解说是否事实完整；
5. 镜头是否与旁白匹配；
6. 成片时长是否合理；
7. 字幕是否正常；
8. cut_plan/render 是否兼容旧结构。

---

# 15. 最关键的开发注意事项

1. 不要把完整 JSON 删除，只是不要喂给大模型。
2. 不要让每个 Agent 都重新理解视频。
3. `content_analysis` 负责理解和找候选。
4. `short_video_edit_plan` 负责规划短视频和镜头。
5. `voiceover_script` 只负责写旁白。
6. AI 配音模式不要再让模型判断原声价值。
7. 视觉 chunk 必须并发。
8. ASR 必须复用模型。
9. 每一步都记录输入字符数，避免以后 prompt 又膨胀。
10. 第一版合并时建议写兼容旧文件，不要一次性重构所有后处理。

---

# 16. 最终推荐改造后的输入输出关系

```text
timeline/merged_timeline.json
    ↓ 程序压缩

timeline/llm_timeline_digest.json
    ↓

agents/content_analysis/v*/content_analysis.json
    ↓

agents/short_video_edit_plan/v*/short_video_edit_plan.json
    ↓ 兼容写出

agents/short_video_planning/v*/short_video_plan.json
agents/editing_script/v*/editing_script.json
    ↓

agents/voiceover_script/v*/voiceover_script.json
    ↓

tts → subtitles → cut_plan → render
```

这套方案可以在不大改渲染链路的前提下，先把最耗时的大模型输入和 Agent 结构优化掉。
