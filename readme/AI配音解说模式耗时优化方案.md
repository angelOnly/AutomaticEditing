# AI 配音解说模式耗时优化方案

> 适用范围：当前项目中的「AI 配音解说模式」视频处理链路。  
> 目标：在不明显影响视频剪辑效果的前提下，将 4 分钟左右视频的处理耗时从 15-20 分钟级别，尽量压缩到 5-8 分钟，理想情况下接近 5 分钟。

---

## 1. 当前问题概述

当前 AI 配音解说模式处理一个约 4 分钟的视频时，实际运行耗时明显过长。根据完整跑完的示例日志分析，视频长度约 260 秒，但处理链路在执行到第 10 步时已经耗时约 10 分钟，完整跑完预计接近 20 分钟甚至更长。

当前流程大致为：

```text
metadata
→ audio_extract
→ frame_extract
→ chunk_build
→ asr
→ vision
→ timeline
→ video_understanding
→ highlight_detection
→ short_video_planning
→ editing_script
→ voiceover_script
→ risk_review
→ tts
→ subtitles
→ cut_plan
→ render
```

表面上看是 17 个步骤太多，但实际瓶颈并不是步骤数量本身，而是：

1. 大模型 Agent 调用次数太多；
2. 多个大模型步骤串行执行；
3. 每个大模型步骤输入内容过大；
4. 多个步骤反复读取同一份完整 timeline JSON；
5. 视觉分析 chunk 串行执行；
6. ASR 模型每次任务重复加载；
7. `risk_review` 阻塞主流程，但它对最终成片没有必要。

---

## 2. 当前耗时长的具体原因

### 2.1 大模型调用过多，且全部串行

当前 AI 配音模式中，核心的大模型调用包括：

```text
vision
video_understanding
highlight_detection
short_video_planning
editing_script
voiceover_script
risk_review
```

其中 `vision` 又会按 chunk 分成多次视觉模型调用。文本 Agent 之间也是串行执行：前一个步骤完成后，后一个步骤才开始。

这导致整体耗时基本等于多个大模型请求耗时的简单累加。

---

### 2.2 `vision` 视觉分析串行执行

当前视觉分析逻辑大致是：

```python
for chunk in chunks:
    call_vision_model(chunk)
```

也就是说，如果一个 4 分钟视频被切成 5 个 chunk，那么 5 个视觉请求是一个接一个跑的。

示例中每个视觉 chunk 大约需要 45-77 秒，5 个 chunk 串行后，视觉阶段接近 6 分钟。

实际上这些 chunk 之间没有依赖关系，完全可以并发执行。

---

### 2.3 大模型输入内容太大

示例里多个文本 Agent 的输入达到了 3 万字符以上。这个规模对于一个 4 分钟新闻视频来说明显过大。

问题不在于视频内容本身真的需要这么多文字，而是当前传给大模型的是较完整的 JSON 中间产物，里面包含大量字段，例如：

```json
{
  "chunk_id": "chunk_0001",
  "start": "00:00",
  "end": "01:00",
  "start_seconds": 0,
  "end_seconds": 60,
  "asr_text": "...",
  "asr_segments": [
    {
      "start": 0.1,
      "end": 3.2,
      "text": "..."
    }
  ],
  "scene_type": "...",
  "visual_summary": "...",
  "screen_text": [],
  "visible_people": [],
  "visual_value_score": 7,
  "hook_score": 6,
  "risk_tags": [],
  "notes": "..."
}
```

其中很多字段对模型决策并不必要，尤其是：

- `asr_segments`：完整逐句时间戳片段；
- `raw_result`：模型原始输出；
- 完整 frame 列表；
- 完整视觉原始响应；
- 重复的 start/end 字段；
- 大量风险审核相关字段；
- 之前步骤已经总结过、后续又重复传递的字段。

真正对大模型有用的通常只有：

- 时间段；
- 压缩后的 ASR 文本；
- 视觉摘要；
- 屏幕文字关键词；
- 人物/场景信息；
- 画面价值评分；
- 开头吸引力评分；
- 候选片段信息。

---

### 2.4 多个 Agent 重复阅读同一份 timeline

当前流程中，多个 Agent 都会读取完整 timeline 或大量上游 JSON：

```text
video_understanding 读取 timeline
highlight_detection 读取 timeline + video_analysis
short_video_planning 读取 candidate_clips + video_analysis
aediting_script 读取 short_video_plan + candidate_clips + timeline
voiceover_script 读取 editing_script + video_analysis + timeline
risk_review 再读取多份结果
```

这导致模型反复阅读同一批材料，重复完成类似的新闻理解、内容判断和剪辑规划。

从生产效率看，这是当前链路最大的结构性问题。

---

### 2.5 `editing_script` 任务过重

`editing_script` 当前既要理解新闻内容，又要参考候选片段，又要规划短视频结构，又要生成标题、封面文案、镜头安排等。

如果它输入的是完整 timeline 和多个上游 JSON，模型就会在这一步重新做一遍内容理解，耗时很容易变成整个流程中最大的瓶颈。

示例中 `editing_script` 是最慢步骤之一。

---

### 2.6 `voiceover_script` 输入过重

AI 配音文案生成本来应该是一个相对轻量的任务：根据已经确定的短视频结构，写旁白即可。

但当前 `voiceover_script` 仍然会读取完整或接近完整的视频分析信息，导致它也在重复理解视频内容。

正确做法应该是：

> 前面步骤已经决定“剪什么、怎么剪、每个镜头表达什么”，`voiceover_script` 只负责“怎么说”。

---

### 2.7 `risk_review` 对 AI 配音成片流程不是必要步骤

当前 `risk_review` 放在 `voiceover_script` 之后、`tts` 之前，会阻塞最终成片。

但在当前产品场景中，系统目标是生成可剪辑视频，而不是做正式发布审核。因此 `risk_review` 对成片不是必要依赖。

既然输入素材已经是可剪辑视频，且最终仍由人来判断是否发布，那么 `risk_review` 可以直接删除，或者改成可选的发布前审核功能。

---

### 2.8 ASR 模型每次任务重复加载

日志显示 ASR 实际识别耗时很短，但模型加载耗时很长。例如：

```text
FunASR model loaded on cuda:0 in 35s+
实际识别约 1-2s
```

也就是说，ASR 阶段慢的主要原因不是识别，而是每次任务都重新加载模型。

应改为 Web 服务启动后加载一次 ASR 模型，后续任务复用同一个 ASR Engine。

---

## 3. 总体优化目标

优化目标不是简单减少步骤数量，而是减少不必要的大模型阅读和重复推理。

优化后的原则：

1. 完整 JSON 继续落盘，方便断点续跑和调试；
2. 给大模型的输入必须使用精简 digest，不再传完整 JSON；
3. 能合并的大模型步骤合并；
4. 无依赖的大模型请求并发执行；
5. `risk_review` 从主流程删除；
6. ASR 模型常驻；
7. 抽帧数量合理降低；
8. 保持视频剪辑效果，不做激进牺牲。

---

## 4. 建议的新流程

### 4.1 原流程

```text
metadata
→ audio_extract
→ frame_extract
→ chunk_build
→ asr
→ vision
→ timeline
→ video_understanding
→ highlight_detection
→ short_video_planning
→ editing_script
→ voiceover_script
→ risk_review
→ tts
→ subtitles
→ cut_plan
→ render
```

### 4.2 优化后流程

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

### 4.3 删除或合并的步骤

| 原步骤 | 处理方式 | 说明 |
|---|---|---|
| `risk_review` | 删除 | 不阻塞成片，不作为 AI 配音模式必要流程 |
| `video_understanding` | 合并 | 与 `highlight_detection` 合并为 `content_analysis` |
| `highlight_detection` | 合并 | 与 `video_understanding` 合并为 `content_analysis` |
| `short_video_planning` | 合并 | 与 `editing_script` 合并为 `short_video_edit_plan` |
| `editing_script` | 合并 | 合并后直接输出短视频剪辑方案 |
| `voiceover_script` | 保留但轻量化 | 只根据剪辑方案写旁白，不再重新理解完整视频 |

---

## 5. 详细优化方案一：删除 `risk_review`

### 5.1 当前问题

`risk_review` 对最终成片没有直接影响，但会阻塞后续：

```text
voiceover_script → risk_review → tts → render
```

### 5.2 优化方案

AI 配音解说模式下直接删除 `risk_review`：

```text
voiceover_script → tts → subtitles → cut_plan → render
```

### 5.3 预期收益

可节省约 1 分钟左右。

### 5.4 影响

对视频剪辑效果无影响。

如果未来需要审核功能，可以作为单独按钮或可选流程：

```text
生成视频后 → 可选执行 risk_review
```

---

## 6. 详细优化方案二：新增 `timeline_digest`

### 6.1 当前问题

当前多个 Agent 直接读取完整 `merged_timeline.json`，导致输入过大。

完整 timeline 适合程序使用，不适合直接喂给大模型。

### 6.2 优化方案

新增一个大模型专用压缩文件：

```text
timeline/llm_timeline_digest.json
```

或：

```text
timeline/llm_timeline_digest.txt
```

建议保留 JSON 结构，但字段极简。

### 6.3 推荐字段

每个 chunk 只保留：

```json
{
  "chunk_id": "chunk_0001",
  "time": "00:00-01:00",
  "speech": "压缩后的 ASR 文本，最多 300-500 字",
  "visual": "视觉摘要，最多 100-200 字",
  "screen_text": ["最多 3 个关键屏幕文字"],
  "people": ["最多 3 个可见人物或身份"],
  "scene": "发布会 / 口播 / 现场 / 图表 / 资料画面",
  "visual_score": 7,
  "hook_score": 6
}
```

### 6.4 需要删除的字段

不要喂给大模型：

```text
asr_segments
raw_result
完整 frame 列表
完整视觉 raw_response
完整风险字段
重复 start/end 字段
调试 notes
大段上游 JSON 原文
```

### 6.5 文本格式示例

如果希望进一步减少 token，也可以生成文本 digest：

```text
[chunk_0001 | 00:00-01:00]
ASR：工信部介绍一季度工业经济和算力发展情况，提到工业增长、算力基础设施、人工智能赋能制造业等内容。
画面：发布会现场，多名官员发言，背景板为新闻发布会。
屏幕文字：工信部、一季度、算力发展
人物：发布会发言人
评分：visual=7, hook=6

[chunk_0002 | 01:00-02:00]
ASR：继续介绍算力规模、工业互联网和新型工业化建设。
画面：发布会中景，偶有资料画面。
屏幕文字：算力、工业互联网
评分：visual=6, hook=7
```

### 6.6 推荐输入规模

对于 4 分钟视频，压缩后的 digest 应控制在：

```text
4000-8000 字符
```

不应再出现 3 万字符以上输入。

---

## 7. 详细优化方案三：合并 `video_understanding + highlight_detection`

### 7.1 当前问题

`video_understanding` 和 `highlight_detection` 都在做内容理解：

- 视频主题是什么；
- 关键事实是什么；
- 哪些片段有新闻价值；
- 哪些内容适合做短视频；
- 哪些时间段有高光。

两个步骤分开会导致模型重复阅读 timeline。

### 7.2 优化方案

合并为：

```text
content_analysis
```

输入：

```text
metadata + llm_timeline_digest + run_options
```

输出：

```json
{
  "video_summary": "整条视频的一句话总结",
  "main_topic": "核心主题",
  "key_facts": [
    "事实点 1",
    "事实点 2"
  ],
  "content_structure": [
    {
      "time": "00:00-01:00",
      "summary": "这一段讲什么",
      "value": "为什么有用"
    }
  ],
  "candidate_clips": [
    {
      "clip_id": "clip_001",
      "start": "00:18",
      "end": "00:48",
      "summary": "候选高光片段摘要",
      "why_selected": "选择原因",
      "news_value_score": 8,
      "visual_score": 7,
      "hook_score": 8,
      "independence_score": 8,
      "recommended_usable_seconds": 30
    }
  ]
}
```

### 7.3 输出字段瘦身

AI 配音模式不需要这些字段：

```text
must_keep_original_audio
original_audio_reason
original_audio_value_score
original_audio_type
recommended_original_audio_start
recommended_original_audio_end
why_original_audio_beats_ai_voiceover
risk_level
risk_reason
```

候选片段只保留能支持剪辑决策的字段即可。

### 7.4 预期收益

减少一次大模型调用，减少一次完整输入阅读。

预计可节省：

```text
1-3 分钟
```

---

## 8. 详细优化方案四：合并 `short_video_planning + editing_script`

### 8.1 当前问题

`short_video_planning` 负责规划做几条短视频、选哪些片段；`editing_script` 再负责生成标题、封面文案、镜头结构和剪辑脚本。

这两个步骤高度相关。分开执行会导致：

1. `short_video_planning` 做一次短视频规划；
2. `editing_script` 再读规划和候选片段，重新组织一次；
3. 模型输入和输出都很重。

### 8.2 优化方案

合并为：

```text
short_video_edit_plan
```

输入：

```text
content_analysis + llm_timeline_digest + run_options
```

输出：

```json
{
  "recommended_video_count": 3,
  "scripts": [
    {
      "short_video_id": "sv_01",
      "title": "短视频标题",
      "cover_text": "封面文案",
      "topic": "这条短视频的核心主题",
      "target_duration_seconds": 30,
      "source_clip_ids": ["clip_001", "clip_003"],
      "editing_structure": [
        {
          "shot_id": "s1",
          "source_start": "00:18",
          "source_end": "00:25",
          "purpose": "开头抛出核心事实",
          "visual_summary": "发布会发言人介绍关键数据",
          "must_say_facts": [
            "一季度工业经济保持增长",
            "算力基础设施持续扩展"
          ]
        },
        {
          "shot_id": "s2",
          "source_start": "01:10",
          "source_end": "01:20",
          "purpose": "补充解释背景",
          "visual_summary": "发布会现场或资料画面",
          "must_say_facts": []
        }
      ],
      "voiceover_brief": "旁白要解释这个数据为什么重要，语言克制，避免夸张。"
    }
  ]
}
```

### 8.3 预期收益

这一步收益最大。

原本两个步骤中，尤其 `editing_script` 可能耗时很长。合并后可明显减少重复推理。

预计可节省：

```text
3-8 分钟
```

---

## 9. 详细优化方案五：轻量化 `voiceover_script`

### 9.1 当前问题

`voiceover_script` 当前输入过多，导致它又在做视频理解和新闻判断。

### 9.2 优化方案

`voiceover_script` 只接受 `short_video_edit_plan` 中和旁白有关的信息。

输入示例：

```json
{
  "style": "凤凰卫视新闻解说，克制、清楚、信息密度高，不标题党",
  "short_videos": [
    {
      "short_video_id": "sv_01",
      "title": "...",
      "target_duration_seconds": 30,
      "shots": [
        {
          "shot_id": "s1",
          "time": "00:18-00:25",
          "visual": "发布会发言人介绍关键数据",
          "purpose": "开头抛出核心事实",
          "must_say_facts": ["一季度工业经济保持增长"]
        }
      ],
      "voiceover_brief": "..."
    }
  ]
}
```

不再输入：

```text
完整 timeline
完整 video_analysis
完整 candidate_clips
完整 raw ASR segments
完整视觉 raw output
```

### 9.3 输出要求

输出应简单稳定：

```json
{
  "short_video_id": "sv_01",
  "segments": [
    {
      "shot_id": "s1",
      "voiceover_text": "...",
      "estimated_duration_seconds": 5
    }
  ],
  "full_voiceover_text": "..."
}
```

### 9.4 预期收益

`voiceover_script` 可从 2-3 分钟降到 30-60 秒。

---

## 10. 详细优化方案六：视觉分析并发

### 10.1 当前问题

视觉 chunk 当前串行执行，耗时累加。

### 10.2 优化方案

使用线程池并发处理视觉 chunk：

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

with ThreadPoolExecutor(max_workers=3) as executor:
    futures = [executor.submit(analyze_chunk, chunk) for chunk in chunks]
    for future in as_completed(futures):
        result = future.result()
```

### 10.3 默认并发数

建议默认：

```text
vision_max_workers = 3
```

原因：

1. 视觉大模型接口可能限流；
2. 并发过高容易失败；
3. 3 个并发已经能显著降低耗时；
4. 后续可根据接口稳定性调到 5。

### 10.4 结果排序

并发返回后，需要按 `chunk_id` 或 `start_seconds` 排序，保证下游 timeline 顺序稳定。

### 10.5 预期收益

视觉阶段可从 5-6 分钟降到 1-2 分钟。

---

## 11. 详细优化方案七：减少抽帧数量

### 11.1 当前问题

当前参数类似：

```text
chunk_seconds = 60
frame_interval = 5
```

4 分钟视频大约抽 50 多帧。对于发布会、口播、新闻素材来说，这个密度偏高。

### 11.2 推荐默认值

建议改为：

```text
frame_interval = 10
chunk_seconds = 60
vision_max_frames_per_chunk = 6
```

即：

- 每 10 秒抽 1 帧；
- 每 60 秒一个 chunk；
- 每个 chunk 最多传 6 张图给视觉模型。

### 11.3 不同视频类型建议

| 视频类型 | frame_interval | max_frames_per_chunk |
|---|---:|---:|
| 发布会 / 口播 / 演播室 | 10-15 秒 | 4-6 |
| 普通新闻素材 | 10 秒 | 6 |
| 现场事件 / 冲突 / 事故 | 5-8 秒 | 8 |
| 高精度调试模式 | 3-5 秒 | 8-12 |

### 11.4 推荐策略

默认使用：

```text
frame_interval = 10
vision_max_frames_per_chunk = 6
```

如果用户选择“精细分析模式”，再使用：

```text
frame_interval = 5
vision_max_frames_per_chunk = 8
```

### 11.5 预期收益

减少视觉模型输入，降低视觉分析耗时和成本。

---

## 12. 详细优化方案八：ASR 模型常驻

### 12.1 当前问题

ASR 阶段大量时间花在模型加载上，而不是识别上。

### 12.2 优化方案

在 Pipeline 或 Web 服务生命周期内缓存 ASR Engine。

伪代码：

```python
class Pipeline:
    def __init__(self):
        self._asr_engine = None

    def get_asr_engine(self):
        if self._asr_engine is None:
            self._asr_engine = FunASREngine(...)
        return self._asr_engine

    def step_asr(self):
        engine = self.get_asr_engine()
        result = engine.transcribe(audio_path)
        return result
```

不要在每次任务结束后立即：

```python
engine.release()
```

### 12.3 显存处理

如果担心显存占用，可以增加配置：

```toml
[asr]
keep_model_loaded = true
```

当内存紧张或用户手动释放时，再执行 release。

### 12.4 预期收益

首个任务仍需加载模型，但后续任务 ASR 阶段可从约 40 秒降到 3-5 秒。

---

## 13. 大模型输入输出规范建议

### 13.1 输入规范

每个 Agent 输入只给它完成任务必须的信息。

不要为了“保险”把所有上游 JSON 都塞进去。

### 13.2 输出规范

输出字段越多，模型越慢，越容易出错。

AI 配音模式下应避免输出和原声保留、风险审核、复杂解释相关的字段。

### 13.3 推荐字段控制

#### `content_analysis` 输出控制

保留：

```text
video_summary
main_topic
key_facts
content_structure
candidate_clips
```

#### `candidate_clips` 保留字段

```text
clip_id
start
end
summary
why_selected
news_value_score
visual_score
hook_score
independence_score
recommended_usable_seconds
```

#### `short_video_edit_plan` 输出控制

保留：

```text
short_video_id
title
cover_text
topic
target_duration_seconds
source_clip_ids
editing_structure
voiceover_brief
```

#### `voiceover_script` 输出控制

保留：

```text
short_video_id
segments
full_voiceover_text
estimated_duration_seconds
```

---

## 14. 推荐配置参数

建议新增或调整配置：

```toml
[workflow]
default_frame_interval = 10
chunk_seconds = 60
vision_max_workers = 3
vision_max_frames_per_chunk = 6
enable_risk_review = false

[asr]
keep_model_loaded = true

[llm]
use_timeline_digest = true
max_speech_chars_per_chunk = 500
max_visual_chars_per_chunk = 200
max_screen_text_items = 3
max_people_items = 3
```

---

## 15. 推荐落地顺序

### 阶段一：低风险提速

优先做这些，不改变核心剪辑逻辑：

1. 删除主流程中的 `risk_review`；
2. 视觉分析改线程池并发；
3. 默认抽帧间隔改为 10 秒；
4. 每个视觉 chunk 最多传 6 帧；
5. ASR 模型常驻复用。

预期收益：

```text
15-20 分钟 → 8-12 分钟
```

---

### 阶段二：中间数据压缩

新增：

```text
llm_timeline_digest
```

所有文本 Agent 不再直接读取完整 `merged_timeline.json`。

预期收益：

```text
8-12 分钟 → 6-9 分钟
```

---

### 阶段三：Agent 合并

合并：

```text
video_understanding + highlight_detection → content_analysis
short_video_planning + editing_script → short_video_edit_plan
```

并轻量化：

```text
voiceover_script → voiceover_script_light
```

预期收益：

```text
6-9 分钟 → 4.5-6 分钟
```

---

## 16. 优化后预期耗时

| 阶段 | 当前耗时 | 优化后目标 |
|---|---:|---:|
| 预处理 | 几秒 | 几秒 |
| ASR | 约 40 秒 | 首次 40 秒，后续 3-5 秒 |
| 视觉分析 | 5-6 分钟 | 1-2 分钟 |
| 内容理解 + 高光检测 | 6-7 分钟 | 1.5-3 分钟 |
| 短视频规划 + 剪辑脚本 | 5-10 分钟 | 2-4 分钟 |
| 配音文案 | 2-3 分钟 | 30-60 秒 |
| risk_review | 约 1 分钟 | 删除 |
| TTS / 字幕 / 渲染 | 约 1 分钟 | 基本不变 |

最终目标：

```text
保守目标：6-9 分钟
理想目标：4.5-6 分钟
```

---

## 17. 最终结论

当前 AI 配音解说模式耗时过长，根因不是 FFmpeg 或本地处理慢，而是大模型链路设计过重。

最核心的问题是：

```text
多个 Agent 串行执行，并且反复读取 3 万字符级别的完整 JSON。
```

最有效的优化方向是：

```text
压缩输入
合并 Agent
并发视觉
删除 risk_review
ASR 常驻
减少抽帧
```

最终建议的新主流程是：

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

这套方案不会明显牺牲剪辑效果，但可以显著降低处理耗时，是当前 AI 配音模式最值得优先实施的优化方向。
