# AI 配音 micro_segment 链路架构优化方案

## 0. 结论先行

这次问题不是单纯的 prompt 字段错误，也不是 `quality_gate_failed` 本身的问题，而是 AI 配音链路中“分析单位、选片单位、剪辑单位、下游引用单位”没有彻底拆清楚。

当前链路里同时混用了四种概念：

| 层级 | 当前实际含义 | 应该承担的职责 | 是否应该给下游直接剪辑 |
|---|---|---|---|
| chunk | 约 60 秒的视频分析块 | 抽帧、视觉分析、ASR 聚合、建立视觉上下文 | 不应该直接作为 AI 配音最终剪辑单位 |
| raw ASR segment | ASR 引擎原始识别片段 | 提供真实语音文本和真实时间边界 | 可以作为 micro_segment 的时间来源 |
| micro_segment | 从 ASR 语义切出的更小事实段 | AI 配音模式的事实选片单位 | 可以物化为候选剪辑片段 |
| candidate_clip | 下游统一候选片段 | 给 `short_video_edit_plan`、`cut_plan` 使用 | 是下游真正引用和剪辑的统一单位 |

根本调整方向应该是：

```text
公共分析阶段：继续按 chunk 做视觉和摘要。
AI 配音阶段：用 raw ASR 派生 micro_segment，而不是用压缩后的 digest/speech。
AI 配音选片：content_analysis 选择 micro_segment。
代码物化：micro_segment -> ai_voiceover_candidate_clip。
下游统一：short_video_edit_plan 只面对 candidate_clip，不再关心它来自 chunk 还是 segment。
原声重组：继续走 asr_event_candidate / candidate_filter / reassembly_plan，不接入 asr_micro_segment。
```

这不是为了“让代码跑通”，而是为了让整条链路在数据语义上对齐。

---

## 1. 这次失败暴露出来的问题

AI 配音任务日志中的关键现象是：

```text
source_aggregate success，chunks=8, sources=2
asr_micro_segment success，segments=20
content_analysis success
short_video_edit_plan success
voiceover_script success，但 scripts=0
tts success，但 total=0
cut_plan success
news_quality_gate failed，quality_gate_failed
```

这说明系统不是在一开始就失败，而是“空结果一路被当成成功往后传”。真正异常点不是最后的 `news_quality_gate`，而是：

```text
content_analysis / short_video_edit_plan 没有形成有效 AI 配音脚本，
但后续 voiceover_script、tts、cut_plan 仍然继续成功。
```

更深层的原因是：

1. `asr_micro_segment` 的输入来源不应该是已经压缩过的 `timeline_digest.speech`。
2. `content_analysis` 到底选 `chunk_id` 还是 `segment_id` 没有在架构上定死。
3. `candidate_clip` 的语义不清晰，看起来像 chunk，又想承载 micro_segment。
4. AI 配音和视频重组虽然 workflow 分开，但候选池概念仍然容易混用。
5. 空候选、空脚本、空 TTS 没有 fail-fast，导致最终错误表现为泛化的 `quality_gate_failed`。

---

## 2. 当前链路的实际状态

### 2.1 公共分析阶段确实是 chunk 级

当前公共阶段大致是：

```text
source_prepare
source_analysis
  -> metadata
  -> audio_extract
  -> frame_extract
  -> chunk_build
  -> asr
  -> asr_digest
  -> vision
  -> timeline
  -> timeline_digest
source_aggregate
```

这里的 `chunk_build` 以 `chunk_seconds=60` 为主，把每个源视频切成约 60 秒 chunk。后续 `vision` 按 chunk 抽关键帧，`asr_digest` 按 chunk 压缩语音，`timeline` 再把视觉和语音摘要合并。

这个设计本身合理，因为视觉模型不适合对每一句 ASR 单独看图，按 chunk 做视觉理解更省成本，也能保留画面上下文。

### 2.2 但 AI 配音后面又引入了 asr_micro_segment

AI 配音 workflow 当前是：

```text
source_prepare
source_analysis
source_aggregate
asr_micro_segment
content_analysis
short_video_edit_plan
voiceover_script
voiceover_quality_gate
tts
subtitles
cut_plan
news_quality_gate
news_quality_ai_review
render
```

也就是说，在公共 chunk 分析后，AI 配音又新增了一层 `asr_micro_segment`。

这个方向也是对的，因为 AI 配音不适合直接拿 60 秒 chunk 做最终选片。AI 配音需要更细的事实单位，否则会出现：

```text
一个 60 秒 chunk 里包含多个事实；
短视频文案只讲其中一个事实；
但画面却拿了整段 60 秒；
最终文案、画面、TTS 时长都难对齐。
```

### 2.3 当前最大问题：micro_segment 的来源不应该是 digest/speech

现在 `asr_micro_segment` 的逻辑倾向于从当前 `timeline_digest` 读取 `speech`，再拆句、分组。

但 `timeline_digest.speech` 通常已经不是原始 ASR，而是被 `asr_digest` 或 `compact_asr_for_llm` 压缩过的摘要。摘要适合给大模型快速理解，不适合做精细切片。

这会导致：

```text
1. 文本不是真实原话，后续微分段不可靠。
2. 时间不是 ASR 原始时间，只能按字数比例估算。
3. 细节可能已经被摘要删掉，候选事实会丢失。
4. AI 配音文案变成基于“摘要的摘要”生成。
5. cut_plan 里的 source_start/source_end 可信度下降。
```

所以这里必须改：

```text
asr_micro_segment 必须从 raw ASR segments 派生，不能从 timeline_digest.speech 派生。
```

---

## 3. 应该重新定义四层数据单位

### 3.1 chunk：视觉和上下文单位

chunk 是公共分析单位，不是 AI 配音最终选片单位。

职责：

```text
1. 定义抽帧窗口。
2. 承载视觉分析结果。
3. 承载屏幕文字、人物、场景、画面价值。
4. 给 micro_segment 提供 visual_context。
5. 给大模型理解整条素材提供粗粒度结构。
```

chunk 结构可以保留：

```json
{
  "chunk_id": "src_001_chunk_0001",
  "source_id": "source_001",
  "local_start_seconds": 0.0,
  "local_end_seconds": 60.0,
  "visual_summary": "...",
  "screen_text": [],
  "visible_people": [],
  "asr_digest": "..."
}
```

注意：`asr_digest` 仍然有价值，但只作为理解摘要，不再作为 micro_segment 的原始输入。

### 3.2 raw ASR segment：真实语音时间单位

公共阶段必须保留每个 source 的原始 ASR 输出，并建立索引。

建议新增或标准化一个公共产物：

```text
source_raw_asr_index.json
```

推荐结构：

```json
{
  "version": "source_raw_asr_index_v1",
  "sources": [
    {
      "source_id": "source_001",
      "source_index": 1,
      "duration_seconds": 120.009,
      "asr_segments": [
        {
          "asr_segment_id": "source_001_asr_000001",
          "source_id": "source_001",
          "source_index": 1,
          "start_seconds": 3.12,
          "end_seconds": 7.84,
          "text": "原始 ASR 文本",
          "primary_chunk_id": "source_001_chunk_0001",
          "source_chunk_ids": ["source_001_chunk_0001"]
        }
      ]
    }
  ]
}
```

这里不建议加入复杂的 `clean_text`。如果确实需要规范化文本，也只能做极小范围的格式规整，例如去掉首尾空格、合并多余空格、统一换行；不能删除口头语、掌声、音乐、噪声、错词，也不能改写事实。

这个文件的作用是：

```text
1. 让 asr_micro_segment 有真实 ASR 时间来源。
2. 让后续追溯每个 micro_segment 来自哪几条原始 ASR。
3. 避免从摘要反推时间。
4. 为字幕、TTS 对齐、cut_plan 修复提供可审计依据。
```

### 3.3 micro_segment：AI 配音事实选片单位

micro_segment 是 AI 配音模式专用，不给原声重组模式使用。

它应该由 raw ASR segments 合并而来，而不是从摘要文本拆出来。

推荐结构先保持精简：

```json
{
  "micro_segment_id": "ms_0001",
  "source_id": "source_001",
  "source_index": 1,
  "source_start_seconds": 12.34,
  "source_end_seconds": 23.56,
  "source_start": "00:00:12.340",
  "source_end": "00:00:23.560",
  "duration_seconds": 11.22,

  "asr_segment_ids": ["source_001_asr_000003", "source_001_asr_000004"],
  "source_chunk_ids": ["source_001_chunk_0001"],

  "asr_text": "原始 ASR 拼接文本",

  "visual_context": "所属 chunk 的画面摘要",
  "screen_text": [],
  "visible_people": []
}
```

字段职责：

```text
micro_segment_id：给 content_analysis 选择用的唯一 ID。
source_id/source_index：说明来自哪个源视频。
source_start_seconds/source_end_seconds：真实剪辑时间来源，来自 raw ASR。
source_start/source_end：给人看和兼容旧 JSON 的时间码。
duration_seconds：快速判断片段长度。
asr_segment_ids：追溯由哪些原始 ASR 段合并而来。
source_chunk_ids：追溯它落在哪些视觉 chunk 内，用来取画面上下文。
asr_text：原始 ASR 拼接文本，不做语义清理。
visual_context/screen_text/visible_people：来自所属 chunk 的视觉分析结果。
```

这里的关键规则：

```text
micro_segment 的时间边界来自 raw ASR。
visual_context 来自 chunk。
source_chunk_ids 只是视觉上下文追溯，不代表剪辑边界。
不在 micro_segment 阶段引入复杂 clean_text、summary、semantic_type、independent、needs_context。
```

### 3.4 candidate_clip：下游统一候选片段

content_analysis 之后，下游不应该再知道“你选的是 chunk 还是 micro_segment”。统一交给 `candidate_clip`。

但在 AI 配音模式下，candidate_clip 应该明确：

```text
它是由 micro_segment 派生出来的剪辑片段。
它不是 60 秒 chunk。
它的 source_start/source_end 默认等于 micro_segment 的时间边界，再按配置加很小 padding。
```

推荐结构：

```json
{
  "clip_id": "av_clip_0001",
  "candidate_type": "ai_voiceover",
  "derived_from": "micro_segment",

  "source_id": "source_001",
  "source_index": 1,
  "source_start_seconds": 11.84,
  "source_end_seconds": 24.06,
  "source_start": "00:00:11.840",
  "source_end": "00:00:24.060",
  "duration_seconds": 12.22,

  "micro_segment_ids": ["ms_0001"],
  "asr_segment_ids": ["source_001_asr_000003", "source_001_asr_000004"],
  "source_chunk_ids": ["source_001_chunk_0001"],

  "asr_text": "原始 ASR 拼接文本",
  "visual_context": "画面上下文",
  "summary": "候选片段表达的新闻事实",
  "role": "fact",

  "padding_before_seconds": 0.5,
  "padding_after_seconds": 0.5
}
```

这里 `clip_id` 的含义必须重新讲清楚：

```text
clip_id 不是 chunk_id。
clip_id 不是 micro_segment_id。
clip_id 是从 content_analysis 之后给下游使用的统一候选片段 ID。
```

`micro_segment_ids` 的作用是追溯来源，不是下游剪辑引用主键。下游真正剪辑用：

```text
source_id + source_start_seconds + source_end_seconds
```

`summary` 和 `role` 来自 content_analysis 的选择结果，用于给 `short_video_edit_plan` 排序和组织成片；不再保留 `summary`、`role` 这类容易重复和膨胀的字段。

## 4. AI 配音和视频重组必须彻底隔离

### 4.1 现有 workflow 层面已经部分隔离

当前 workflow 已经把两条链路分开：

AI 配音：

```text
source_prepare
source_analysis
source_aggregate
asr_micro_segment
content_analysis
short_video_edit_plan
voiceover_script
tts
subtitles
cut_plan
news_quality_gate
render
```

视频重组：

```text
source_prepare
source_analysis
source_aggregate
video_understanding
asr_event_candidate
candidate_filter
highlight_reassembly_plan
reassembly_cut_plan
news_quality_gate
reassembly_render
```

这个方向是对的。

### 4.2 现在需要进一步隔离候选池语义

问题在于现在两个模式都可能使用模糊的 `candidate_clips`，但这两个 candidate 的语义完全不同。

建议明确拆成两个内部产物：

```text
AI 配音：ai_voiceover_candidate_clips.json
视频重组：reassembly_candidate_clips.json 或 candidate_clip_pool_filtered.json
```

AI 配音候选池：

```json
{
  "version": "ai_voiceover_candidate_clips_v1",
  "candidate_type": "ai_voiceover",
  "source": "asr_micro_segment",
  "candidate_clips": []
}
```

视频重组候选池：

```json
{
  "version": "reassembly_candidate_clips_v1",
  "candidate_type": "highlight_reassembly",
  "source": "asr_event_candidate",
  "candidate_clips": []
}
```

这两个文件可以最终都叫 `candidate_clips` 字段，但文件名、`candidate_type`、`derived_from` 必须不同。

### 4.3 两条链路的剪辑粒度不同

| 模式 | 选片单位 | 片段目标 | 时间边界来源 | 是否保留原声 |
|---|---|---|---|---|
| AI 配音 | micro_segment | 为解说文案提供画面证据 | raw ASR segment 时间 + 小 padding | 默认不保留原声，原声可极低音量或不用 |
| 视频重组 | ASR event clip / chunk group | 保留完整原声事件 | 原声事件完整边界 | 必须保留原声 |

不能让 AI 配音去复用原声重组的事件级 clip，因为那通常太长；也不能让原声重组用 micro_segment，因为那通常太碎，原声不完整。

---

## 5. 原始 ASR 应该怎么传到下游

### 5.1 公共阶段新增 raw ASR 索引

公共阶段每个 source 已经跑 ASR。现在要做的是把原始 ASR 作为稳定公共产物保留下来，而不是只留下 digest。

建议在 `source_analysis` 的每个子任务里保留：

```text
asr/v*/asr_segments.json
```

并在 `source_aggregate` 时聚合出：

```text
source_aggregate/v*/source_raw_asr_index.json
```

或者直接并入 `source_aggregate.json`：

```json
{
  "sources": [],
  "timeline_digest": {},
  "raw_asr_index": {
    "sources": []
  }
}
```

更推荐单独文件，避免 `source_aggregate.json` 太大。

### 5.2 raw ASR 与 chunk 建立映射

每条 ASR segment 要知道自己属于哪个 chunk：

```text
asr_segment.start/end 与 chunk.local_start/local_end 求 overlap。
最大 overlap 的 chunk_id 作为 primary chunk。
如果跨 chunk，可记录多个 source_chunk_ids。
```

推荐字段：

```json
{
  "asr_segment_id": "source_001_asr_000012",
  "source_id": "source_001",
  "start_seconds": 58.2,
  "end_seconds": 63.4,
  "text": "...",
  "primary_chunk_id": "source_001_chunk_0001",
  "source_chunk_ids": ["source_001_chunk_0001", "source_001_chunk_0002"]
}
```

### 5.3 asr_micro_segment 使用 raw ASR，不使用 digest

调整后 `asr_micro_segment` 输入应该来自：

```text
source_raw_asr_index + chunk visual context
```

它的处理过程应该是：

```text
1. 读取 raw ASR segments。
2. 按 source_id 分组。
3. 按 start_seconds 排序。
4. 仅做最小格式规整，例如去掉首尾空格、合并多余空格；不做语义清理。
5. 根据标点、停顿、句长、时间间隔合并为 micro_segment。
6. 给每个 micro_segment 附加 source_chunk_ids。
7. 从 chunk 视觉摘要中补 visual_context。
```

不要再用 `timeline_digest.speech` 做切分依据。

## 6. content_analysis 应该怎么接 segment

### 6.1 content_analysis 在 AI 配音模式下的职责

AI 配音模式下，content_analysis 不应该直接做最终剪辑，也不应该输出时间码。

它只做一件事：

```text
从 micro_segments 中选择适合 AI 配音短视频的事实段。
```

它的输入应该是 micro_segment 列表，而不是 chunk 列表。

输入示例：

```text
[ms_0001]
source_id=source_001 time=00:00:12.340-00:00:23.560 duration=11.22s
声：原始 ASR 文本
画：所属 chunk 的视觉摘要

[ms_0002]
...
```

### 6.2 大模型只输出 micro_segment_id，不输出时间码

原因：时间码应该由代码控制，不能让大模型重写。

content_analysis 的输出要保持精简。它不需要输出 `main_topic`、`key_facts`、`discarded_segments`、`role`、`story_group_id`。

推荐输出：

```json
{
  "selected_segments": [
    {
      "micro_segment_id": "ms_0001",
      "summary": "这段表达的新闻事实",
      "role": "fact"
    }
  ]
}
```

字段含义：

```text
micro_segment_id：代码用来回查 micro_segments.json，取得真实时间、source_id、ASR 和视觉上下文。
summary：这段被选中的事实概括，给后面的 candidate_clip 和 short_video_edit_plan 使用。
role：这段在成片中的粗略功能，可选值保持少量，例如 hook / fact / context / consequence / ending。
```

不输出 `discarded_segments` 的原因是：没有被选中的 segment 默认就是丢弃项，代码可以通过 `all_segments - selected_segments` 得到，无需让大模型逐条解释。

### 6.3 代码物化 selected_segments 为 candidate_clip

代码拿到 `selected_segments` 后，做下面转换：

```text
micro_segment_id
 -> 查 micro_segments.json
 -> 取 source_id、source_start_seconds、source_end_seconds、asr_text、visual_context
 -> 读取 content_analysis 给出的 summary、role
 -> 加 padding
 -> 生成 clip_id
 -> 输出 ai_voiceover_candidate_clips.json
```

这样下游看到的是稳定的 `candidate_clip`，而不是 segment。

这里要注意：`micro_segment_id` 不是消失了，而是被写入 `candidate_clip.micro_segment_ids` 作为来源追溯；下游主引用切换为 `clip_id`。

## 7. candidate_clip 怎么让下游理解

### 7.1 下游只认 clip_id

`short_video_edit_plan` 不应该再看到 `micro_segment_id` 作为主引用。它应该只看到：

```text
clip_id
source_id
source_start/source_end
duration_seconds
summary
role
visual_context
```

它输出时只引用 `clip_id`：

```json
{
  "videos": [
    {
      "short_video_id": "v_001",
      "title": "...",
      "clip_ids": ["av_clip_0001", "av_clip_0003"]
    }
  ]
}
```

然后代码根据 `clip_id` 回查 candidate_clip，补齐 shot：

```json
{
  "shot_id": "v_001_s01",
  "source_clip_id": "av_clip_0001",
  "source_id": "source_001",
  "source_start": "00:00:11.840",
  "source_end": "00:00:24.060",
  "duration_seconds": 12.22,
  "visual": "...",
  "news_fact_to_explain": "..."
}
```

### 7.2 micro_segment_id 是对齐来源，不是下游主引用

`micro_segment_id` 的作用不是直接给 `short_video_edit_plan`、TTS 或 cut_plan 当主键，而是作为 AI 配音链路里的事实来源和对齐依据：

```text
1. 回查原始 ASR。
2. 确认 candidate_clip 的事实边界来自哪段 ASR。
3. 检查文案是否覆盖该段事实。
4. 在 TTS 时长不匹配时，回查并调整 candidate_clip 时间边界。
5. 排查为什么某个 clip 被选中。
```

下游主链路：

```text
short_video_edit_plan 用 clip_id。
voiceover_script 用 shot_id。
tts 用 narration_segments。
cut_plan 用 source_id + source_start/source_end。
```

所以整体引用关系是：

```text
micro_segment_id -> candidate_clip.clip_id -> editing_structure.shot_id -> narration_segments.shot_id -> cut_plan.clips
```

这里不是说 `micro_segment_id` 只做无关 debug，而是说它不应该越级成为所有下游步骤的主 ID。它先确定事实和初始时间边界，再由代码物化成 candidate_clip，最后通过 shot_id 与配音文案和 TTS 对齐。

这个链路是能对上的。

## 8. 调整后的整体链路能不能对上

可以对上，但前提是把每一层的输入输出协议固定下来。

### 8.1 AI 配音完整链路

```text
公共阶段：
source_prepare
source_analysis
  -> chunk_build
  -> raw ASR
  -> asr_digest
  -> vision
  -> timeline_digest
source_aggregate
  -> timeline_digest
  -> source_raw_asr_index

AI 配音专用阶段：
asr_micro_segment
  输入：source_raw_asr_index + chunk visual context
  输出：micro_segments

content_analysis
  输入：micro_segments
  输出：selected_segments

ai_voiceover_candidate_materialize
  输入：selected_segments + micro_segments
  输出：ai_voiceover_candidate_clips

short_video_edit_plan
  输入：ai_voiceover_candidate_clips
  输出：scripts + editing_structure，引用 clip_id

voiceover_script
  输入：editing_structure，按 shot_id 生成 narration_segments
  输出：voiceover_script.scripts

tts
  输入：narration_segments
  输出：tts audio per segment

subtitles
  输入：voiceover_script + tts timings
  输出：subtitles

cut_plan
  输入：editing_structure + voiceover_script + tts + subtitles
  输出：output_videos[].clips

news_quality_gate
  检查：output_videos、clips、source_id、source_start/source_end、duration

render
  按 cut_plan 合成
```

### 8.2 ID 传递关系

```text
raw_asr_segment_id
  -> micro_segment.asr_segment_ids

chunk_id
  -> micro_segment.source_chunk_ids
  -> candidate_clip.source_chunk_ids

micro_segment_id
  -> candidate_clip.micro_segment_id

clip_id
  -> short_video_edit_plan.clip_ids
  -> editing_structure.source_clip_id

shot_id
  -> voiceover_script.narration_segments[].shot_id
  -> subtitles
  -> cut_plan 对齐
```

### 8.3 时间传递关系

```text
raw ASR start/end
  -> micro_segment source_start/source_end
  -> candidate_clip source_start/source_end with padding
  -> editing_structure source_start/source_end
  -> cut_plan clips source_start/source_end
  -> render ffmpeg cut
```

这里没有再从摘要反推时间，所以链路更稳。

---

## 9. 需要新增或调整的产物

### 9.1 公共阶段新增 raw ASR 索引

文件：

```text
source_aggregate/v*/source_raw_asr_index.json
```

用途：

```text
给 asr_micro_segment 提供真实 ASR 句子和时间。
```

### 9.2 AI 配音阶段新增 micro_segments 完整结构

文件：

```text
asr_micro_segment/v*/micro_segments.json
```

不要只保存 groups，也不要只保存摘要文本。必须保存完整 micro_segment 对象。

### 9.3 AI 配音候选池独立文件

文件：

```text
agents/content_analysis/v*/ai_voiceover_candidate_clips.json
```

或：

```text
ai_voiceover_candidates/v*/candidate_clips.json
```

推荐后者，因为它能减少 content_analysis 既当 agent 又当物化器的混乱。

### 9.4 保留 content_analysis 原始输出

继续保留：

```text
agents/content_analysis/v*/model_output.json
agents/content_analysis/v*/content_analysis.json
```

但 `content_analysis.json` 应该明确包含：

```json
{
  "version": "ai_voiceover_content_analysis_v1",
  "input_unit": "micro_segment",
  "selected_segments": []
}
```

这样调试时一眼能看出来本轮 content_analysis 选的是 segment，不是 chunk。`candidate_clips` 不应该由大模型直接写在这里，而应该由后续代码物化步骤生成。

## 10. 具体优化方向

### 10.1 优化一：公共阶段沉淀 raw ASR

当前公共阶段不能只给下游摘要。要给下游两类数据：

```text
摘要：给大模型快速理解。
raw ASR：给精细时间和 micro_segment。
```

需要保证每个 source 子任务的 ASR 产物能被 common 聚合任务读取，并统一 source_id。

重点不是把所有 ASR 都塞进 LLM，而是把 raw ASR 作为代码层可追溯数据保存下来。

### 10.2 优化二：asr_micro_segment 输入改为 raw ASR

`asr_micro_segment` 应该读取：

```text
source_raw_asr_index.json
source_aggregate.timeline_digest.chunks 的 visual context
```

处理原则：

```text
1. 不使用 digest.speech 切分。
2. 不丢原始 ASR text。
3. 时间边界直接继承 ASR。
4. 分组时不能跨 source。
5. 可以跨 chunk，但必须记录多个 source_chunk_ids。
6. 每个 micro_segment 的长度应有上下限，例如 5-20 秒。
```

### 10.3 优化三：content_analysis 改为 AI 配音专用 segment selector

AI 配音的 content_analysis 不再兼容 chunk 输出协议。它要清楚声明：

```text
输入单位：micro_segment
输出单位：selected_segment
禁止输出时间码
禁止输出 chunk_ids
禁止跨 source 合并
```

如果后续还想支持 chunk 选片，那应该是另一个步骤或另一个模式，不要在同一个 content_analysis 里隐式切换。

### 10.4 优化四：增加 candidate materializer

把“模型选择结果”与“下游候选 clip”拆开。

建议新增内部函数或步骤：

```text
ai_voiceover_candidate_materialize
```

职责：

```text
selected_segments -> candidate_clips
```

好处：

```text
1. 时间边界由代码控制。
2. clip_id 统一生成。
3. 下游只看到 candidate_clip。
4. debug 时可以清楚看到每个 clip 来自哪个 segment。
```

### 10.5 优化五：short_video_edit_plan 只吃 candidate_clip

`short_video_edit_plan` 的输入不要再混入 chunk_id 或 segment_id。

它应该看到的是：

```text
候选 clip 列表
每个 clip 的事实摘要、画面上下文、时长、source_id
```

它只输出：

```text
选择哪些 clip_id
按什么顺序
每个 clip 在成片里承担什么叙事作用
```

### 10.6 优化六：空结果 fail-fast

这不是根本架构，但必须做。

必须新增硬校验：

```text
asr_micro_segment 输出 micro_segments=0：失败
content_analysis 输出 selected_segments=0：失败
candidate_materialize 输出 candidate_clips=0：失败
short_video_edit_plan 输出 scripts=0：失败
voiceover_script 输入 scripts=0：失败
tts 输入 narration_segments=0：失败或明确 skipped，但不能 success
cut_plan 输出 output_videos=0：失败
```

用户看到的错误应该变成：

```text
AI 配音候选片段为空：content_analysis 没有选出任何 micro_segment。
```

而不是最后才看到：

```text
quality_gate_failed
```

---

## 11. 推荐的最终目录和文件结构

AI 配音任务目录建议变成：

```text
outputs/<task_id>/
  source_aggregate/v1/
    source_aggregate.json
    source_raw_asr_index.json

  asr_micro_segment/v1/
    input_raw_asr.json
    micro_segments.json
    step_status.json

  agents/content_analysis/v1/
    input.txt
    model_output.json
    content_analysis.json
    step_status.json

  ai_voiceover_candidates/v1/
    candidate_clips.json
    materialize_debug.json
    step_status.json

  agents/short_video_edit_plan/v1/
    input.txt
    short_video_edit_plan.json

  agents/voiceover_script/v1/
    voiceover_script.json

  tts/omnivoice/v1/
    tts_outputs.json

  edit/cut_plan/v1/
    cut_plan.json
    cut_plan_debug.json

  quality/news_quality_gate/v1/
    news_quality_gate.json
```

视频重组任务目录继续保持：

```text
outputs/<task_id>/
  agents/asr_event_candidate/v1/
  agents/candidate_filter/v1/
  agents/highlight_reassembly/v1/
  edit/reassembly_cut_plan/v1/
  quality/news_quality_gate/v1/
```

这样从目录上就能看出来两条链路是隔离的。

---

## 12. 和视频重组链路的关系

### 12.1 公共阶段共享

两种模式共享：

```text
source_prepare
source_analysis
source_aggregate
```

共享内容包括：

```text
metadata
raw ASR
vision
timeline_digest
source_aggregate
```

### 12.2 后处理阶段完全分离

AI 配音：

```text
raw ASR -> micro_segment -> ai_voiceover_candidate_clip -> voiceover script -> TTS -> cut_plan
```

视频重组：

```text
timeline / ASR event -> reassembly_candidate_clip -> reassembly_plan -> reassembly_cut_plan
```

### 12.3 为什么不能共用候选 clip

AI 配音 clip 的核心目标：

```text
短、可解释、可配音、画面服务文案。
```

视频重组 clip 的核心目标：

```text
原声完整、事件完整、讲话不断裂。
```

所以候选片段的时间边界和筛选标准不同。两个模式只能共享公共分析，不能共享候选池。

---

## 13. 调整后的质量检查策略

### 13.1 AI 配音质量检查

AI 配音 `news_quality_gate` 前应该增加更早的本地检查：

```text
1. 每个 script 至少有一个 editing_structure。
2. 每个 shot 必须有 source_id/source_start/source_end。
3. 每个 shot 必须能追溯到 candidate_clip。
4. 每个 candidate_clip 必须能追溯到 micro_segment。
5. 每个 micro_segment 必须能追溯到 raw ASR。
6. voiceover_script.narration_segments 必须覆盖所有 shot_id。
7. tts_outputs 必须覆盖所有 narration_segments。
```

### 13.2 视频重组质量检查

视频重组继续检查：

```text
1. selected_clips 不为空。
2. 每个 clip 有 source_id/source_start/source_end。
3. keep_original_audio=true。
4. source_end > source_start。
5. reassembly_cut_plan 有有效 output_videos。
```

两者的 gate 可以共用部分底层函数，但错误信息和回查建议必须按 production_mode 分开。

---

## 14. 分阶段实施建议

### 阶段一：先修数据源，不改大架构

目标：让 `asr_micro_segment` 不再吃 digest。

工作：

```text
1. 在 source_aggregate 阶段输出 source_raw_asr_index.json。
2. asr_micro_segment 改为读取 source_raw_asr_index。
3. micro_segments.json 输出完整 segment 对象。
4. 保留原有 workflow 名称，减少改动面。
```

验收：

```text
micro_segments.json 中每个 segment 都有 source_id、source_start_seconds、source_end_seconds、asr_text、source_chunk_ids。
```

### 阶段二：重定 content_analysis 协议

目标：AI 配音 content_analysis 只选 micro_segment。

工作：

```text
1. content_analysis 输入改为 micro_segments。
2. 输出 selected_segments。
3. 不允许输出 chunk_ids。
4. content_analysis.json 标明 input_unit=micro_segment。
```

验收：

```text
content_analysis.json 里 selected_segments 非空，且每个 micro_segment_id 都能在 micro_segments.json 找到。
```

### 阶段三：新增 AI 配音 candidate materializer

目标：统一下游候选 clip。

工作：

```text
1. selected_segments -> ai_voiceover_candidate_clips。
2. 生成 clip_id。
3. 加 padding。
4. 记录 micro_segment_id / asr_segment_ids / source_chunk_ids。
5. short_video_edit_plan 改为读取 ai_voiceover_candidate_clips。
```

验收：

```text
short_video_edit_plan 输入里只出现 clip_id，不再要求模型理解 micro_segment_id。
```

### 阶段四：补 fail-fast 和错误信息

目标：不要再空跑到 quality_gate。

工作：

```text
1. micro_segments=0 直接失败。
2. selected_segments=0 直接失败。
3. candidate_clips=0 直接失败。
4. scripts=0 直接失败。
5. tts total=0 不能标 success。
6. news_quality_gate 的错误提示按 AI 配音/视频重组区分。
```

验收：

```text
如果 content_analysis 没选出候选，任务应停在 content_analysis 或 candidate_materialize，错误信息指向候选为空。
```

### 阶段五：保留视频重组不受影响

目标：确认原声重组链路不被 AI 配音改动污染。

工作：

```text
1. workflow_registry 不让 highlight_reassembly 走 asr_micro_segment。
2. candidate_filter 继续只服务视频重组。
3. reassembly_cut_plan 继续读取 reassembly candidate。
4. news_quality_gate 根据 production_mode 读 cut_plan 或 reassembly_cut_plan。
```

验收：

```text
视频重组模式仍然从 asr_event_candidate -> candidate_filter -> highlight_reassembly_plan -> reassembly_cut_plan 跑通。
```

---

## 15. 需要特别避免的错误方向

### 15.1 不要只改 prompt

只改 prompt 会让模型更可能输出正确字段，但不能解决：

```text
1. micro_segment 输入来源不可靠。
2. 时间边界来自摘要估算。
3. candidate_clip 语义不清。
4. 空结果继续 success。
```

### 15.2 不要让 AI 配音直接回到 60 秒 chunk 选片

这样虽然容易跑通，但会牺牲成片质量：

```text
画面太长；
文案只覆盖局部事实；
TTS 时长不好对齐；
cut_plan 容易为了配音强行裁切；
短视频节奏差。
```

### 15.3 不要让原声重组复用 micro_segment

micro_segment 太碎，不适合原声重组。原声重组需要完整原声事件，而不是一句一句的语义片段。

### 15.4 不要让下游同时理解 chunk_id、segment_id、clip_id

下游只能用一个统一 ID：`clip_id`。

否则后续 prompt、TTS、字幕、cut_plan 会继续混乱。

---

## 16. 最终推荐架构图

```text
                          ┌─────────────────────┐
                          │    source_prepare    │
                          └──────────┬──────────┘
                                     │
                          ┌──────────▼──────────┐
                          │   source_analysis    │
                          │ chunk/asr/vision     │
                          └──────────┬──────────┘
                                     │
                          ┌──────────▼──────────┐
                          │   source_aggregate   │
                          │ timeline_digest      │
                          │ raw_asr_index        │
                          └───────┬───────┬─────┘
                                  │       │
              AI 配音链路          │       │        原声重组链路
                                  │       │
                     ┌────────────▼───┐   │
                     │ asr_micro_segment│   │
                     │ raw ASR -> ms   │   │
                     └────────────┬───┘   │
                                  │       │
                     ┌────────────▼───┐   │
                     │ content_analysis│   │
                     │ select ms       │   │
                     └────────────┬───┘   │
                                  │       │
                     ┌────────────▼────────┐
                     │ ai_voiceover_clips   │
                     │ ms -> candidate_clip │
                     └────────────┬────────┘
                                  │
                     ┌────────────▼────────┐
                     │ short_video_edit_plan│
                     │ select clip_id       │
                     └────────────┬────────┘
                                  │
                     ┌────────────▼────────┐
                     │ voiceover_script     │
                     │ shot_id narration    │
                     └────────────┬────────┘
                                  │
                     ┌────────────▼────────┐
                     │ tts / subtitles      │
                     └────────────┬────────┘
                                  │
                     ┌────────────▼────────┐
                     │ cut_plan / render    │
                     └─────────────────────┘

                                          ┌──────────────────────┐
                                          │ video_understanding   │
                                          └──────────┬───────────┘
                                                     │
                                          ┌──────────▼───────────┐
                                          │ asr_event_candidate   │
                                          │ event-level clips     │
                                          └──────────┬───────────┘
                                                     │
                                          ┌──────────▼───────────┐
                                          │ candidate_filter      │
                                          └──────────┬───────────┘
                                                     │
                                          ┌──────────▼───────────┐
                                          │ reassembly_plan       │
                                          └──────────┬───────────┘
                                                     │
                                          ┌──────────▼───────────┐
                                          │ reassembly_cut/render │
                                          └──────────────────────┘
```

---

## 17. 最终答案：调整后能不能对上

能对上，但必须满足下面几条硬规则：

```text
1. raw ASR 是 micro_segment 的唯一可靠文本和时间来源。
2. chunk 只提供视觉上下文，不作为 AI 配音最终剪辑单位。
3. content_analysis 在 AI 配音模式下只选择 micro_segment。
4. 代码把 micro_segment 物化成 candidate_clip。
5. short_video_edit_plan 只选择 candidate_clip.clip_id。
6. voiceover_script 只对 shot_id 写文案。
7. cut_plan 只用 source_id + source_start/source_end 剪视频。
8. 原声重组继续走 asr_event_candidate，不接入 micro_segment。
```

这样整个链路的语义就是闭合的：

```text
原始视频
 -> chunk 视觉理解
 -> raw ASR 真实时间
 -> micro_segment 语义事实
 -> candidate_clip 下游统一候选
 -> shot 成片结构
 -> narration 文案
 -> TTS
 -> cut_plan
 -> render
```

这才是根本性修复，而不是单纯把错误字段改到能跑。
