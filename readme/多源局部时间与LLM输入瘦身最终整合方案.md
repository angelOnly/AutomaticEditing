# 多源局部时间体系与 LLM 输入瘦身最终整合方案

> 说明：本文是把前面分散的优化方案、代码现状分析、LLM 输入补充方案，以及最新修正意见统一合并后的最终整合版。本文不是重新简化后的方案，而是将已有方案按工程落点重新归并，保留原方案里的关键问题、代码建议、替换方向、优先级与验收标准。

---

## 一、当前代码现状与总判断

当前 `clean-highlight-reassembly` 分支还处在 **“统一多源入口 + 虚拟时间兼容”** 的中间态，不是最终需要的 **“多源素材池 + 局部时间体系”**。

几个关键事实：

1. `run_web.py` 只是启动 Web 和 ASR preflight，然后交给 `web_app:app`，没有时间体系逻辑。
2. `web_app.py` 已经有多源任务入口，会生成 `common_source_key` / `common_task_id`，并给子任务 manifest 写入 common 关联信息。
3. 但提交任务时仍然只是把 `--source-request` 传给 `run_multisource_pipeline.py`，Web 层没有真正做磁盘锁和 common manifest 二次检查。
4. `pipeline.py` 里仍然用 `_is_virtual_source_manifest()` 作为多源判断，而且要求 `timeline_mode == "virtual"`，说明虚拟时间概念还在核心分支里。
5. `step_source_aggregate()` 仍然在算：

```python
global_start = virtual_start + local_start
global_end = virtual_start + local_end
```

然后把它写回：

```text
start_seconds / end_seconds / source_start / source_end
```

这就是当前最核心的问题：代码仍然把多源素材拼成一条虚拟连续时间线。

另外，`PipelineRunner.run()` 是通过：

```python
handler = getattr(self, f"step_{step}")
```

动态找步骤方法的，所以只要 workflow 里有：

```text
voiceover_quality_gate
```

但 `PipelineRunner` 没有：

```python
step_voiceover_quality_gate()
```

任务就会直接崩。

一句话总结：本次不是“小修补”，而是要把当前代码里的 `multi_source_virtual` 中间态，彻底改成 `multi_source_pool + local_time`。核心改动点集中在：

```text
pipeline.py 的多源判断
source_aggregate
LLM compact 输入
clip registry / materialize
cut_plan / render
common analysis 复用
ASR 清洗
Web UI 和配置残留
```

---

## 二、优先级 0：先修 AI 配音必崩 bug

这是第一优先级，不属于架构重构，是确定性 bug。

### 2.1 当前问题

`PipelineRunner.run()` 不做 `hasattr` 保护，直接：

```python
handler = getattr(self, f"step_{step}")
```

所以如果 workflow 里存在：

```text
voiceover_quality_gate
```

但 `PipelineRunner` 没有：

```python
step_voiceover_quality_gate()
```

就一定报：

```text
'PipelineRunner' object has no attribute 'step_voiceover_quality_gate'
```

### 2.2 建议改法

在 `newsclip_agent/pipeline.py` 的 `PipelineRunner` 类中增加：

```python
def step_voiceover_quality_gate(self) -> None:
    self._mark_skipped(
        "voiceover_quality_gate",
        "已删除：配音文案质量改由代码规则、TTS时长和成片时长校验处理。"
    )


def step_voiceover_quality_check(self) -> None:
    self._mark_skipped(
        "voiceover_quality_check",
        "已删除：配音文案质量改由代码规则、TTS时长和成片时长校验处理。"
    )
```

不要写成：

```python
def step_voiceover_quality_gate(self):
    return self.step_voiceover_quality_check()
```

原因是：当前执行步骤名是 `voiceover_quality_gate`，必须把 manifest 里的这个步骤标成 skipped。`_mark_skipped()` 当前会按传入 step 写 manifest，适合直接复用。

同时建议给 `run()` 加一个更友好的保护：

```python
handler_name = f"step_{step}"
handler = getattr(self, handler_name, None)
if handler is None:
    raise RuntimeError(
        f"工作流步骤 {step} 没有对应实现方法 {handler_name}，请检查 workflow_registry.py"
    )
```

这样以后再有步骤名不一致，不会变成 Python 原始 attribute error。

---

## 三、废弃虚拟时间轴，改成多源局部时间体系

### 3.1 当前问题

现在多源判断函数叫：

```python
_is_virtual_source_manifest()
_is_virtual_multi_source()
```

而且判断条件是：

```python
source_manifest.get("timeline_mode") == "virtual"
```

这会导致后面所有代码自然继续围绕 virtual 写。

当前 `step_source_aggregate()` 是最大问题点。它做了三件不该做的事：

1. 读取 `virtual_start_seconds`
2. 计算 `global_start/global_end`
3. 把全局时间写回 `start_seconds/end_seconds/source_start/source_end`

旧逻辑大致是：

```python
virtual_start = float(source_item.get("virtual_start_seconds") or summary_doc.get("virtual_start_seconds") or 0)
...
global_start = virtual_start + local_start
global_end = virtual_start + local_end
...
"start_seconds": round(global_start, 3),
"end_seconds": round(global_end, 3),
"start": round(global_start, 3),
"end": round(global_end, 3),
"source_start": seconds_to_timecode(global_start, ms=True),
"source_end": seconds_to_timecode(global_end, ms=True),
```

这会把多个独立 source 错误地塑造成一条连续时间线。

### 3.2 新语义：multi_source_pool

新增函数，不建议直接原地硬改所有调用，先做一次语义替换：

```python
def _is_multi_source_pool(self) -> bool:
    manifest = getattr(self, "manifest", {}) or {}
    source_videos = manifest.get("source_videos") or []

    if manifest.get("source_mode") in {"multi_source_pool", "multi_source_virtual"} and source_videos:
        return True

    if not manifest.get("source_manifest"):
        return False

    try:
        source_manifest = read_json(self._source_manifest_path(), {})
    except Exception:
        return False

    return bool(source_manifest.get("sources"))
```

然后逐步把这些调用：

```python
self._is_virtual_source_manifest()
self._is_virtual_multi_source()
```

替换成：

```python
self._is_multi_source_pool()
```

短期为了少改，可以先保留旧函数，但只做别名：

```python
def _is_virtual_source_manifest(self) -> bool:
    return self._is_multi_source_pool()


def _is_virtual_multi_source(self) -> bool:
    return self._is_multi_source_pool()
```

但后续第二轮清理时要删掉旧函数名，否则虚拟时间概念删不干净。

### 3.3 重写 `step_source_aggregate()`

新版职责：

```text
source_aggregate 只做素材池聚合，不做时间拼接。
```

输出应该类似：

```json
{
  "version": "source_aggregate_local_time_v1",
  "source_count": 3,
  "successful_source_count": 3,
  "sources": [
    {
      "source_id": "source_001",
      "source_index": 1,
      "display_name": "xxx.mp4",
      "duration_seconds": 30.0,
      "summary": "...",
      "key_facts": []
    }
  ],
  "timeline_digest": {
    "version": "multi_source_local_timeline_digest_v1",
    "timeline_mode": "source_pool_local_time",
    "chunks": [
      {
        "source_id": "source_001",
        "source_index": 1,
        "chunk_id": "source_001_chunk_0001",
        "local_chunk_id": "chunk_0001",
        "local_start": "00:00:00.000",
        "local_end": "00:00:30.000",
        "local_start_seconds": 0.0,
        "local_end_seconds": 30.0,
        "duration_seconds": 30.0,
        "speech": "...",
        "visual": "..."
      }
    ]
  }
}
```

具体替换方向：

```python
local_start = float(
    chunk.get("local_start_seconds", chunk.get("start_seconds", chunk.get("start", 0))) or 0
)
local_end = float(
    chunk.get("local_end_seconds", chunk.get("end_seconds", chunk.get("end", local_start))) or local_start
)

local_chunk_id = str(chunk.get("chunk_id") or f"chunk_{index:04d}")
global_chunk_id = (
    local_chunk_id
    if local_chunk_id.startswith(f"{source_id}_")
    else f"{source_id}_{local_chunk_id}"
)

chunks.append({
    **chunk,
    "chunk_id": global_chunk_id,
    "local_chunk_id": local_chunk_id,
    "source_id": source_id,
    "source_index": source_summary.get("source_index"),
    "local_start_seconds": round(local_start, 3),
    "local_end_seconds": round(local_end, 3),
    "local_start": seconds_to_timecode(local_start, ms=True),
    "local_end": seconds_to_timecode(local_end, ms=True),
    "duration_seconds": round(max(0.0, local_end - local_start), 3),
})
```

排序不要再按全局时间排：

```python
chunks.sort(key=lambda item: (
    int(item.get("source_index") or 0),
    float(item.get("local_start_seconds") or 0),
    str(item.get("chunk_id") or ""),
))
```

`total_duration` 也不要再用 `max(end_seconds)`，因为多源不是一条连续视频。可以改成：

```python
total_source_duration = sum(
    float(item.get("duration_seconds") or 0)
    for item in summaries
)
```

最终：

```python
"timeline_digest": {
    "version": "multi_source_local_timeline_digest_v1",
    "timeline_mode": "source_pool_local_time",
    "total_source_duration_seconds": round(total_source_duration, 3),
    "chunks": chunks,
}
```

---

## 四、LLM 输入瘦身与白名单重构

### 4.1 当前问题

`timeline_digest` 的方向是正确的，它本来就是为了避免把完整分析 JSON 直接塞给大模型，解决 LLM 输入过大、字段重复、上下文冗余的问题。

但当前代码里的：

```python
_load_current_timeline_digest_for_llm()
_load_compact_source_aggregate_for_llm()
```

又重新构造了较多结构化字段，并且仍然包含：

```text
start
end
start_seconds
end_seconds
virtual_start_seconds
virtual_end_seconds
```

其中 `_load_current_timeline_digest_for_llm()` 仍在输出 `start/end`，而 `_load_compact_source_aggregate_for_llm()` 仍在输出 `virtual_start_seconds/virtual_end_seconds`。

这会导致：

1. 输入冗余；
2. token 成本高；
3. 模型误以为多源素材是一条连续时间线；
4. 模型可能继续输出时间戳；
5. 内部字段污染模型判断；
6. source_aggregate 的 compact 输入里仍然包含 `virtual_start_seconds / virtual_end_seconds`，会继续把虚拟时间概念传给模型。

所以这个问题不能只叫：

```text
LLM 输入去掉虚拟时间
```

最终要升级为：

```text
LLM 输入瘦身与白名单重构
```

### 4.2 新原则：保留 timeline_digest 作为 LLM 输入压缩层

这里不废弃 `timeline_digest`，也不否定 `timeline_digest` 给 LLM 使用。

最终原则是：

```text
timeline_digest 继续作为 LLM 输入压缩层。
LLM 输入继续基于 timeline_digest / candidate clips 构造，但必须经过白名单瘦身。
```

具体规则：

1. 保留 `timeline_digest`，不废弃。
2. 不把完整 chunk 原始 JSON 直接塞给模型。
3. LLM 看到的是经过压缩后的 `timeline_digest / candidate clips` 摘要。
4. `timeline_digest` 给模型时只能保留必要信息。
5. 多源时间只展示 `source_id + 局部 time_range`。
6. 禁止 `start/end/source_start/source_end/global/virtual` 等字段进入模型。
7. 候选 clip 尽量控制在 10-20 个，不要把全部 chunk 都塞给模型。
8. 大模型只输出 `clip_id`、`shot_id`、排序、文案，不输出时间戳。
9. 代码根据 `clip_id` 回填 `source_id + local_start/local_end`。

也就是说，`timeline_digest` 仍然是压缩层，但不能原样把内部结构完整传给模型；必须经过白名单函数生成极简、可读、无虚拟时间字段的版本。

### 4.3 LLM 输入统一三层结构

所有文本模型输入统一拆成三层。

#### 第一层：source summary

每个 source 一条摘要，不超过 150-200 字。

示例：

```text
素材摘要：
source_001：主播介绍事件背景，主要说明事故发生时间、地点和初步原因。
source_002：现场画面，包含人群聚集、救援车辆和现场同期声。
source_003：当事人采访，情绪明显，适合做核心表达。
```

#### 第二层：candidate clips

只给候选 clip，不给全部 chunk。

示例：

```text
候选片段：
1. clip_id: source_001_clip_003
   source: source_001
   time: 00:00:12-00:00:28
   visual: 主播口播，画面稳定，信息清晰。
   speech: 主播交代事件背景和核心事实。
   value: 适合放在开头，建立新闻背景。
   risk: 无明显风险。

2. clip_id: source_002_clip_001
   source: source_002
   time: 00:00:05-00:00:21
   visual: 现场人群聚集，救援车辆经过。
   speech: 现场同期声较嘈杂，但画面信息强。
   value: 适合作为现场证明画面。
   risk: 注意避免过度解读现场原因。
```

#### 第三层：task instruction

只说明当前任务。

高光重组：

```text
任务：
从候选片段中选择并排序，生成 1-3 条原声高光短视频。
不同 source 是独立素材，不代表连续时间线。
不要根据 source 顺序推断事件先后。
只根据画面、同期声、新闻价值和叙事完整性选择 clip_id。
```

AI 配音规划：

```text
任务：
从候选画面中规划一条 AI 解说短视频。
每个 shot 必须绑定一个 clip_id。
不要输出时间戳。
不要编造候选片段以外的事实。
```

配音文案生成：

```text
任务：
根据已确定的 shot 顺序生成中文新闻解说文案。
一句话适合一行字幕。
不写记者署名。
不编造画面和事实。
narration_segments 必须按 shot_id 对齐。
```

### 4.4 不同 Agent 的输入改法

#### 4.4.1 高光重组 `highlight_reassembly_plan`

输入只给候选 clip，不给完整 timeline。

输入示例：

```text
任务：
从候选片段中选择并排序，生成原声高光短视频。

素材摘要：
source_001：……
source_002：……

候选片段：
1. clip_id: source_001_clip_003
   source: source_001
   time: 00:00:12-00:00:28
   visual: 特写讲话，人物情绪明显。
   speech: 同期声表达完整，信息密度高。
   value: 适合作为开头。

2. clip_id: source_002_clip_001
   source: source_002
   time: 00:00:00-00:00:45
   visual: 主播口播，交代背景。
   speech: 背景信息完整。
   value: 适合补充上下文。
```

输出只允许 `clip_id`：

```json
{
  "output_videos": [
    {
      "reassembly_id": "hr_001",
      "title": "现场冲突高光",
      "clip_ids": [
        "source_001_clip_003",
        "source_002_clip_001"
      ]
    }
  ]
}
```

不允许模型输出：

```text
start
end
source_start
source_end
global_start
global_end
```

时间全部由代码根据 `clip_id` 回填。

#### 4.4.2 AI 配音规划 `short_video_edit_plan`

输入只给候选画面 clip：

```text
任务：
从候选画面中规划一条 AI 解说短视频。

素材摘要：
source_001：……
source_002：……

候选画面：
1. clip_id: source_002_clip_001
   time: 00:00:00-00:00:12
   visual: 主播口播，画面稳定。
   fact: 说明事件背景。
   use: 适合开头。

2. clip_id: source_004_clip_003
   time: 00:01:10-00:01:25
   visual: 现场人群聚集。
   fact: 说明现场情况。
   use: 适合正文。
```

输出只绑定 `clip_id`：

```json
{
  "short_videos": [
    {
      "short_video_id": "v_001",
      "title": "事件现场",
      "shots": [
        {
          "shot_id": "v_001_s01",
          "clip_id": "source_002_clip_001",
          "purpose": "交代事件背景"
        },
        {
          "shot_id": "v_001_s02",
          "clip_id": "source_004_clip_003",
          "purpose": "展示现场情况"
        }
      ]
    }
  ]
}
```

#### 4.4.3 配音文案 `voiceover_script`

配音文案不需要完整 timeline，只需要已经确定的 shot 列表。

输入示例：

```text
短视频 v_001：

shot_id: v_001_s01
clip_id: source_002_clip_001
画面：主播口播，交代事件背景。
要表达：说明事件发生的基本情况。

shot_id: v_001_s02
clip_id: source_004_clip_003
画面：现场人群聚集，救援车辆经过。
要表达：说明现场反应和后续处置。
```

输出字段继续保留：

```json
{
  "scripts": [
    {
      "short_video_id": "v_001",
      "narration_text": "",
      "narration_segments": [
        {
          "shot_id": "v_001_s01",
          "text": ""
        }
      ]
    }
  ]
}
```

这些字段后续 TTS、字幕、cut_plan 依赖，不建议改，也不建议中文化字段名。

### 4.5 代码层新增统一白名单构造函数

建议在 `pipeline.py` 或新文件里增加一个专门的 LLM 输入构造模块。

例如新增：

```text
newsclip_agent/llm_materials.py
```

禁止进入 LLM 的字段：

```python
FORBIDDEN_LLM_FIELDS = {
    "start",
    "end",
    "start_seconds",
    "end_seconds",
    "source_start",
    "source_end",
    "global_start",
    "global_end",
    "global_start_seconds",
    "global_end_seconds",
    "virtual_start",
    "virtual_end",
    "virtual_start_seconds",
    "virtual_end_seconds",
    "source_path",
    "file_path",
    "manifest_path",
    "cache_path",
    "work_task_dir",
    "source_manifest",
    "normalized_file",
    "original_path",
}
```

允许进入 LLM 的信息：

```python
ALLOWED_LLM_FIELDS = {
    "source_id",
    "source_index",
    "display_name",
    "clip_id",
    "chunk_id",
    "shot_id",
    "time_range",
    "local_start",
    "local_end",
    "duration_seconds",
    "visual",
    "speech",
    "summary",
    "key_facts",
    "reason",
    "risk_flags",
    "value",
    "purpose",
}
```

注意：

```text
local_start_seconds / local_end_seconds 代码内部可以保留，但不建议给模型。
```

给模型看：

```text
time: 00:00:12-00:00:28
```

就够了。

### 4.6 替换 `_load_current_timeline_digest_for_llm()`

当前这个函数名字容易诱导继续把 timeline 结构化字段给 LLM。

建议废弃：

```python
_load_current_timeline_digest_for_llm()
```

改成：

```python
_build_llm_material_digest_text()
```

职责改为：

```text
读取 source_aggregate / timeline_digest / candidate clips
只抽取 source 摘要、clip_id、source_id、局部 time_range、visual、speech、value、risk
生成一段短文本
```

示例返回：

```python
{
    "material_digest": "...极简文本...",
    "source_count": 3,
    "candidate_clip_count": 12
}
```

而不是：

```python
{
    "chunks": [...]
}
```

如果短期不改函数名，也至少要改函数输出：

```python
compact_chunks.append({
    "chunk_id": chunk.get("chunk_id") or "",
    "source_id": chunk.get("source_id", ""),
    "source_index": chunk.get("source_index", ""),
    "time_range": (
        f"{chunk.get('local_start') or seconds_to_timecode(float(chunk.get('local_start_seconds') or 0), ms=True)}"
        f"-"
        f"{chunk.get('local_end') or seconds_to_timecode(float(chunk.get('local_end_seconds') or 0), ms=True)}"
    ),
    "duration_seconds": chunk.get("duration_seconds", 0),
    "speech": compact_text(str(chunk.get("speech") or ""), max_chars=70),
    "visual": compact_text(str(chunk.get("visual") or ""), max_chars=50),
    "visual_score": chunk.get("visual_score", 0),
    "hook_score": chunk.get("hook_score", 0),
    "flags": compact_list(chunk.get("flags") or [], max_items=2, max_chars_each=18),
})
```

不要再给：

```text
start
end
start_seconds
end_seconds
source_start
source_end
global_start
global_end
virtual_start
virtual_end
```

同时 `_load_compact_source_aggregate_for_llm()` 也还在输出：

```python
"virtual_start_seconds"
"virtual_end_seconds"
```

这两个必须删掉，改成：

```python
sources.append({
    "source_id": item.get("source_id", ""),
    "source_index": item.get("source_index", ""),
    "display_name": item.get("display_name", ""),
    "duration_seconds": item.get("duration_seconds", 0),
    "summary": compact_text(str(item.get("summary") or ""), max_chars=160),
    "key_facts": compact_list(item.get("key_facts") or [], max_items=2, max_chars_each=70),
})
```

### 4.7 具体建议函数

#### 4.7.1 构造 source 摘要文本

```python
def _build_llm_source_summary_text(self, max_sources: int = 20) -> str:
    aggregate = self._load_optional_step_json("source_aggregate", {})
    sources = aggregate.get("sources") or []

    lines = ["素材摘要："]
    for item in sources[:max_sources]:
        source_id = str(item.get("source_id") or "")
        display_name = str(item.get("display_name") or "")
        duration = float(item.get("duration_seconds") or 0)
        summary = compact_text(str(item.get("summary") or ""), max_chars=160)

        lines.append(
            f"- {source_id}"
            f"{f'（{display_name}）' if display_name else ''}"
            f"，时长约 {duration:.1f} 秒：{summary}"
        )

    return "\n".join(lines)
```

#### 4.7.2 构造候选 clip 文本

```python
def _build_llm_candidate_clip_text(
    self,
    clips: list[dict[str, Any]],
    *,
    max_clips: int = 20,
    max_visual_chars: int = 80,
    max_speech_chars: int = 120,
) -> str:
    lines = ["候选片段："]

    for idx, clip in enumerate(clips[:max_clips], start=1):
        clip_id = str(clip.get("clip_id") or "")
        source_id = str(clip.get("source_id") or "")
        local_start = str(clip.get("local_start") or "")
        local_end = str(clip.get("local_end") or "")
        time_range = str(clip.get("time_range") or f"{local_start}-{local_end}")

        visual = compact_text(str(clip.get("visual") or clip.get("visual_summary") or ""), max_chars=max_visual_chars)
        speech = compact_text(str(clip.get("speech") or ""), max_chars=max_speech_chars)
        value = compact_text(str(clip.get("value") or clip.get("reason") or ""), max_chars=80)
        risk_flags = clip.get("risk_flags") or []

        lines.extend([
            f"{idx}. clip_id: {clip_id}",
            f"   source: {source_id}",
            f"   time: {time_range}",
            f"   visual: {visual}",
            f"   speech: {speech}",
            f"   value: {value}",
            f"   risk: {', '.join(map(str, risk_flags)) if risk_flags else '无明显风险'}",
        ])

    return "\n".join(lines)
```

#### 4.7.3 统一构造高光重组输入

```python
def _build_highlight_reassembly_llm_input_text(self) -> str:
    clips = self._load_candidate_clips_for_llm()

    return "\n\n".join([
        "任务：从候选片段中选择并排序，生成原声高光短视频。",
        "规则：不同 source 是独立素材，不代表连续时间线；不要根据 source 顺序推断事件先后；只输出 clip_id，不输出时间戳。",
        self._build_llm_source_summary_text(),
        self._build_llm_candidate_clip_text(
            clips,
            max_clips=int(self.config.raw.get("llm_input", {}).get("max_llm_candidate_clips", 14)),
        ),
        "输出 JSON：{\"output_videos\":[{\"reassembly_id\":\"hr_001\",\"title\":\"\",\"clip_ids\":[\"\"]}]}",
    ])
```

#### 4.7.4 统一构造 AI 配音规划输入

```python
def _build_short_video_edit_plan_llm_input_text(self) -> str:
    clips = self._load_candidate_clips_for_llm()

    return "\n\n".join([
        "任务：从候选画面中规划一条 AI 解说短视频。",
        "规则：每个 shot 必须绑定一个 clip_id；不要输出时间戳；不要编造候选片段以外的事实。",
        self._build_llm_source_summary_text(),
        self._build_llm_candidate_clip_text(clips),
        "输出 JSON：{\"short_videos\":[{\"short_video_id\":\"v_001\",\"title\":\"\",\"shots\":[{\"shot_id\":\"v_001_s01\",\"clip_id\":\"\",\"purpose\":\"\"}]}]}",
    ])
```

#### 4.7.5 统一构造配音文案输入

```python
def _build_voiceover_script_llm_input_text(self) -> str:
    edit_plan = self._load_step_json("short_video_edit_plan")
    lines = [
        "任务：根据已确定的 shot 顺序生成中文新闻解说文案。",
        "要求：新闻口吻，短句，一句话适合一行字幕；不写记者署名；不编造事实；narration_segments 必须按 shot_id 对齐。",
    ]

    for video in edit_plan.get("short_videos") or edit_plan.get("videos") or []:
        short_video_id = str(video.get("short_video_id") or "")
        lines.append(f"\n短视频 {short_video_id}：")

        for shot in video.get("shots") or []:
            lines.append(f"shot_id: {shot.get('shot_id', '')}")
            lines.append(f"clip_id: {shot.get('clip_id', '')}")
            lines.append(f"画面：{compact_text(str(shot.get('visual') or shot.get('visual_summary') or ''), max_chars=100)}")
            lines.append(f"要表达：{compact_text(str(shot.get('purpose') or shot.get('narration_focus') or ''), max_chars=100)}")

    lines.append(
        '\n输出 JSON：{"scripts":[{"short_video_id":"v_001","narration_text":"","narration_segments":[{"shot_id":"v_001_s01","text":""}]}]}'
    )

    return "\n".join(lines)
```

### 4.8 不要让模型看全部 chunk

这一点要提升成方案原则。

当前多源聚合时会整理 chunks。`step_source_aggregate()` 现在会把每个 source 的 digest chunks 全部聚合到 `timeline_digest.chunks`。

新规则：

```text
timeline_digest.chunks 可以存在于内部产物。
但大模型不能直接看全部 chunks。
```

后续应该是：

```text
source chunks
↓
代码/规则/轻量模型生成候选 clips
↓
只把候选 clips 10-20 个给 LLM
↓
LLM 做最终选择、排序、叙事、文案
```

候选过滤依据可以是：

```text
visual_score
hook_score
ASR 信息密度
是否有人物
是否现场画面
是否有冲突/变化
是否包含核心事实
是否重复
是否风险过高
```

这样模型输入会稳定很多，也更适合 DeepSeek v4 flash 这类结构化能力一般的模型。

---

## 五、候选 clip 统一变成 `clip_id → source_id + local time`

最终目标是：

```text
LLM 不输出时间，只输出 clip_id。
```

所以需要在代码里建立一个确定性的 clip registry。

### 5.1 建议新增内部结构

在 `candidate_refine` 或 `highlight_detection` 之后，统一生成：

```json
{
  "clips": [
    {
      "clip_id": "source_002_clip_0001",
      "source_id": "source_002",
      "source_index": 2,
      "chunk_id": "source_002_chunk_0001",
      "local_start": "00:00:00.000",
      "local_end": "00:00:45.000",
      "local_start_seconds": 0.0,
      "local_end_seconds": 45.0,
      "duration_seconds": 45.0,
      "visual_summary": "...",
      "speech": "...",
      "news_value": "..."
    }
  ]
}
```

### 5.2 新增工具函数

```python
def _load_clip_registry(self) -> dict[str, dict[str, Any]]:
    candidates = self._load_step_json("highlight_detection")
    clips = candidates.get("clips") or candidates.get("candidate_clips") or []
    registry = {}

    for index, clip in enumerate(clips, start=1):
        source_id = str(clip.get("source_id") or "")
        if not source_id:
            continue

        clip_id = str(clip.get("clip_id") or "")
        if not clip_id:
            clip_id = f"{source_id}_clip_{index:04d}"
            clip["clip_id"] = clip_id

        local_start = float(clip.get("local_start_seconds") or 0)
        local_end = float(clip.get("local_end_seconds") or 0)

        if local_end <= local_start:
            continue

        registry[clip_id] = {
            **clip,
            "source_id": source_id,
            "local_start_seconds": round(local_start, 3),
            "local_end_seconds": round(local_end, 3),
            "local_start": clip.get("local_start") or seconds_to_timecode(local_start, ms=True),
            "local_end": clip.get("local_end") or seconds_to_timecode(local_end, ms=True),
            "duration_seconds": round(local_end - local_start, 3),
        }

    return registry
```

后续所有 plan 都通过这个 registry 回填，不允许相信模型给的时间。

---

## 六、重组 plan 只允许输出 clip_id

当前 `AGENT_INFO` 里 `highlight_reassembly_plan` 仍然是一个文本 Agent。

建议把 prompt 和解析逻辑都改成：

```json
{
  "output_videos": [
    {
      "reassembly_id": "hr_001",
      "title": "",
      "clip_ids": [
        "source_002_clip_0001",
        "source_004_clip_0003"
      ]
    }
  ]
}
```

不要让模型输出：

```text
source_start
source_end
start
end
global_start
global_end
```

### 6.1 代码侧 materialize

新增：

```python
def _materialize_reassembly_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
    registry = self._load_clip_registry()
    warnings = []

    for video in plan.get("output_videos") or []:
        selected = []
        for clip_id in video.get("clip_ids") or []:
            clip = registry.get(str(clip_id))
            if not clip:
                warnings.append({
                    "type": "missing_clip_id",
                    "clip_id": clip_id,
                    "message": "模型输出了不存在的 clip_id，已跳过。"
                })
                continue

            selected.append({
                "clip_id": clip["clip_id"],
                "source_id": clip["source_id"],
                "chunk_id": clip.get("chunk_id", ""),
                "local_start": clip["local_start"],
                "local_end": clip["local_end"],
                "local_start_seconds": clip["local_start_seconds"],
                "local_end_seconds": clip["local_end_seconds"],
                "duration_seconds": clip["duration_seconds"],
            })

        video["selected_clips"] = selected
        video.pop("clip_ids", None)

    plan["warnings"] = warnings
    return plan
```

如果某个视频 `selected_clips` 为空，就丢弃这条 `output_video`。

如果所有 `output_video` 都为空，再失败。

---

## 七、AI 配音 shot plan 也只绑定 clip_id

AI 配音流程同理。

模型输出：

```json
{
  "videos": [
    {
      "short_video_id": "v_001",
      "shots": [
        {
          "shot_id": "v_001_s01",
          "clip_id": "source_002_clip_0001",
          "narration_focus": "说明现场冲突"
        }
      ]
    }
  ]
}
```

代码回填：

```json
{
  "shot_id": "v_001_s01",
  "clip_id": "source_002_clip_0001",
  "source_id": "source_002",
  "local_start": "00:00:00.000",
  "local_end": "00:00:45.000",
  "local_start_seconds": 0.0,
  "local_end_seconds": 45.0
}
```

`voiceover_script` 仍然保留字段：

```text
narration_text
narration_segments
shot_id
text
```

不要中文化字段，因为 TTS / subtitle / cut_plan 依赖字段名。`AGENT_INFO` 里现在 `voiceover_script` 走的就是 `VOICEOVER_SCRIPT_TEXT_PROMPT`。

---

## 八、render 只接受 source_id + local time

渲染阶段要加一道强约束：

```python
def _validate_materialized_clip(self, clip: dict[str, Any], source_registry: dict[str, Any]) -> tuple[bool, str]:
    source_id = str(clip.get("source_id") or "")
    if source_id not in source_registry:
        return False, f"source_id 不存在: {source_id}"

    start = float(clip.get("local_start_seconds") or -1)
    end = float(clip.get("local_end_seconds") or -1)
    duration = float(source_registry[source_id].get("duration_seconds") or 0)

    if start < 0 or end <= start:
        return False, f"local_start/local_end 不合法: {start}-{end}"

    if end > duration + 0.05:
        return False, f"片段超出源视频时长: {source_id} {end:.3f}s > {duration:.3f}s"

    return True, ""
```

错误提示改成：

```text
片段无效：source_id 不存在，或 local_start/local_end 超出该源视频时长。
```

不要再出现：

```text
片段时间不在任何源视频边界内
```

因为新架构没有虚拟边界。

---

## 九、旧产物直接废弃，不做迁移

不要兼容旧虚拟时间。

建议加一个新格式校验函数：

```python
LEGACY_TIME_FIELDS = {
    "virtual_start_seconds",
    "virtual_end_seconds",
    "global_start_seconds",
    "global_end_seconds",
    "global_start",
    "global_end",
}

REQUIRED_LOCAL_TIME_FIELDS = {
    "source_id",
    "local_start_seconds",
    "local_end_seconds",
}


def _assert_no_legacy_virtual_time(self, obj: Any, *, path: str = "") -> None:
    if isinstance(obj, dict):
        found = LEGACY_TIME_FIELDS.intersection(obj.keys())
        if found:
            raise UserFacingPipelineError(
                "legacy virtual time fields detected",
                user_message=(
                    "检测到旧架构产物，包含虚拟时间字段："
                    + ", ".join(sorted(found))
                    + "。本次架构升级后不兼容旧任务产物，请重新运行任务。"
                ),
                technical_detail={"path": path, "fields": sorted(found)},
            )
        for key, value in obj.items():
            self._assert_no_legacy_virtual_time(value, path=f"{path}.{key}" if path else key)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            self._assert_no_legacy_virtual_time(value, path=f"{path}[{index}]")
```

在这些步骤开始前调用：

```text
source_aggregate
highlight_detection
candidate_refine
highlight_reassembly_plan
short_video_edit_plan
reassembly_cut_plan
cut_plan
render
```

更简单的落点：

```text
在 _can_reuse() 里，如果准备复用旧 output，先读 output JSON，检测到旧字段直接返回 False 或抛出“需要重新跑”。
```

当前 `_can_reuse()` 只看 status、input_hash、output 是否存在，不检查格式。

建议改成：

```python
if output and output.endswith(".json"):
    data = read_json(self.task_dir / output, {})
    self._assert_no_legacy_virtual_time(data)
```

但要注意：这样可能让很多旧任务在 UI 点开时报错。更稳的做法是在具体步骤入口校验，不在所有读取处校验。

---

## 十、common analysis 单飞复用

`web_app.py` 现在已经生成：

```python
common_source_key = _common_source_key(req, raw_source_items)
common_task_id = f"common_{common_source_key}"
```

并写到 child manifest。

但是从提交逻辑看，它还是直接为每个 child task 构造 command、入队、启动。

目标行为是：

```text
同一批 source，不管 AI 配音还是原声重组，只允许一个 common analysis 执行。
```

### 10.1 建议实现位置

不要放在 `PipelineRunner.step_source_analysis()` 里做主锁。

应该放在：

```text
run_multisource_pipeline.py
```

或 Web job 启动前的 multi-source coordinator。

原因是：

```text
PipelineRunner 是单任务执行器，它不应该负责跨 Web job 的调度。
```

### 10.2 推荐新增模块

新增：

```text
newsclip_agent/common_analysis.py
```

核心函数：

```python
def acquire_common_analysis(
    *,
    outputs_dir: Path,
    common_source_key: str,
    owner_job_id: str,
) -> CommonAnalysisHandle:
    ...
```

目录：

```text
outputs/__common__/common_xxx/
    common_manifest.json
    .common.lock
```

manifest：

```json
{
  "common_source_key": "xxx",
  "common_task_id": "common_xxx",
  "common_status": "pending",
  "common_owner_job_id": "",
  "common_started_at": "",
  "common_finished_at": "",
  "common_output_task_dir": ""
}
```

### 10.3 拿锁后的规则

伪代码：

```python
with file_lock(common_dir / ".common.lock"):
    manifest = read_json(common_manifest_path, {})

    if manifest.get("common_status") == "success":
        return ReuseCommon(manifest)

    if manifest.get("common_status") == "running":
        return WaitCommon(manifest)

    if manifest.get("common_status") == "failed":
        raise UserFacingPipelineError(
            "common analysis failed",
            user_message="公共分析已失败，请手动重新运行 common analysis。"
        )

    manifest.update({
        "common_status": "running",
        "common_owner_job_id": owner_job_id,
        "common_started_at": now_iso(),
    })
    write_json(common_manifest_path, manifest)

    return RunCommon(...)
```

重点是：

```text
拿到锁之后必须重新读 manifest。
```

---

## 十一、ASR token 清洗：三层兜底

`config.toml` 现在已有 ASR、digest、LLM input 配置。

建议新增：

```text
newsclip_agent/asr_cleaning.py
```

```python
ASR_SPECIAL_TOKEN_PATTERNS = [
    r"<\s*\|\s*zh\s*\|\s*>",
    r"<\s*\|\s*en\s*\|\s*>",
    r"<\s*\|\s*NEUTRAL\s*\|\s*>",
    r"<\s*\|\s*Speech\s*\|\s*>",
    r"\[Music\]",
    r"\[Applause\]",
]


def clean_asr_text(text: str) -> dict[str, Any]:
    raw = text or ""
    removed = []

    clean = raw
    for pattern in ASR_SPECIAL_TOKEN_PATTERNS:
        matches = re.findall(pattern, clean, flags=re.IGNORECASE)
        if matches:
            removed.extend(matches)
            clean = re.sub(pattern, "", clean, flags=re.IGNORECASE)

    clean = re.sub(r"\s+", " ", clean).strip()

    return {
        "raw_text": raw,
        "clean_text": clean,
        "removed_tokens": removed,
    }
```

落点：

1. `step_asr()` 写 ASR JSON 前：保存 `raw_text / clean_text / removed_tokens`。
2. `step_timeline()` 构建 chunk speech 时：优先用 `clean_text`。
3. `_build_llm_material_digest_text()` 或 LLM input 构造前：最后兜底再清一次。

这样即使旧 ASR 文件有脏 token，也不会进入 LLM。

---

## 十二、Web UI 也要去掉 virtual 字段

`web_app.py` 的 source preview 现在还返回：

```python
"virtual_start": item.get("virtual_start", ""),
"virtual_end": item.get("virtual_end", ""),
```

这也要删。

改成：

```python
"duration_seconds": item.get("duration_seconds"),
```

最多展示：

```python
"local_time_range": f"00:00:00.000-{seconds_to_timecode(duration, ms=True)}"
```

不要再在 UI 上出现 `virtual start/end`，否则用户和后续开发都会继续误解为一条拼接时间线。

---

## 十三、配置文件调整

### 13.1 删除虚拟时间相关配置

`llm_input.max_llm_source_boundaries` 现在还在，说明 prompt 输入里可能还保留 source boundaries 思路。

新架构下建议废弃：

```toml
max_llm_source_boundaries = 20
```

改成：

```toml
max_llm_sources = 20
max_llm_candidate_clips = 14
max_llm_context_chunks = 3
```

### 13.2 API key 配置保持不动

本次优化不调整 API key 写法。

也就是说，最终方案里不加入：

```text
API key 不要硬编码在仓库里
```

本次优化只聚焦：

```text
时间体系
LLM 输入
clip_id 映射
渲染坐标
common 复用
ASR 清洗
确定性 bug 修复
```

不动 `config.toml` 里的 key 写法。

---

## 十四、最终改造顺序

按这个顺序做，风险最低：

```text
1. 修复 AI 配音 step 名称不一致
   补 step_voiceover_quality_gate，避免 AI 配音必然失败。

2. 废弃虚拟时间轴
   多源全部改为 source_id + local_start/local_end。

3. LLM 输入瘦身与白名单重构
   timeline_digest 继续作为 LLM 输入压缩层。
   LLM 输入继续基于 timeline_digest / candidate clips 构造，但必须经过白名单瘦身。
   LLM 只看 source 摘要、candidate clip 摘要、任务说明。

4. LLM 输出收敛
   模型只输出 clip_id / shot_id / 排序 / 文案，不输出时间戳。

5. clip registry 与 materialize
   代码根据 clip_id 回填 source_id + local_start/local_end。

6. 渲染逻辑改造
   render 直接用 source_id + local time 裁剪。

7. ASR token 清洗
   ASR / timeline / LLM input 三层清洗。

8. common analysis 单飞复用
   同一批 source 同时启动多个模式，只跑一次 common。

9. 旧产物废弃策略
   检测到 virtual/global 字段直接要求重新跑，不做旧格式迁移。

10. 清 UI 和配置残留
    删除 virtual_start / virtual_end 展示和 source_boundaries 配置。
```

更细化的工程顺序也可以写成：

```text
1. 补 step_voiceover_quality_gate()
   先让 AI 配音流程不再必崩。

2. 增加旧虚拟字段检测
   检测到 virtual/global 字段直接要求重新跑，不做迁移。

3. 重命名多源判断语义
   _is_virtual_source_manifest → _is_multi_source_pool。

4. 重写 source_aggregate
   不再生成 global_start/global_end/start_seconds/end_seconds/source_start/source_end。

5. 改 LLM compact 输入
   只给 source_id、chunk_id、local time、speech、visual。
   timeline_digest 继续作为输入压缩层，但必须经过白名单瘦身。

6. 改 candidate clips
   统一生成 clip_id，并建立 clip registry。

7. 改 highlight_reassembly_plan
   模型只输出 clip_ids，代码 materialize 时间。

8. 改 short_video_edit_plan / voiceover_script
   shot 只绑定 clip_id，代码回填 source_id + local time。

9. 改 cut_plan / render
   ffmpeg 只按 source_id 找源文件，按 local_start/local_end 裁剪。

10. 做 common analysis 单飞锁
    common_source_key + .common.lock + common_manifest 二次检查。

11. 做 ASR 清洗三层兜底
    ASR 落盘、timeline 构建、LLM input 前都清洗。

12. 清 UI 和配置残留
    删除 virtual_start / virtual_end 展示和 source_boundaries 配置。
```

---

## 十五、验收标准

改完后，用这几个标准判断是否真的完成：

```text
1. 新产物里不再出现 virtual_start_seconds / virtual_end_seconds。
2. 新产物里不再出现 global_start_seconds / global_end_seconds。
3. LLM 输入里没有 start/end 这种模糊字段，只出现 source_id + 局部 time_range。
4. timeline_digest 仍然保留，并继续作为 LLM 输入压缩层的来源。
5. 传给 LLM 的 timeline_digest / candidate clips 是极简、可读、白名单字段版本。
6. LLM 输出不包含任何时间戳，只包含 clip_id / shot_id / 排序 / 文案。
7. render 不再做“虚拟时间 → source boundary → local time”的转换。
8. render 只按 source_id 找源文件，并按 local_start/local_end 裁剪。
9. 同一批 source 同时跑 AI 配音和原声重组，只产生一个 common analysis。
10. 旧任务产物不会被自动猜测转换，直接提示重新运行。
11. AI 配音不会再因为 voiceover_quality_gate 方法不存在而失败。
12. Web UI 不再展示 virtual_start / virtual_end。
13. 配置里不再保留 max_llm_source_boundaries 这类 source boundary 思路配置。
14. API key 配置保持现状，不纳入本次优化范围。
```

---

## 十六、最终结论

这次改造的核心不是单纯删除几个字段，而是把当前分支里的虚拟时间兼容逻辑彻底收口，形成统一的新架构：

```text
multi_source_pool + local_time + clip_id materialize
```

具体来说：

```text
多源素材不是一条连续时间线。
每个 source 都保留自己的局部时间。
LLM 不再判断、不再生成、不再修正时间。
LLM 只做选择、排序、叙事和文案。
所有时间都由代码根据 clip_id 回填。
timeline_digest 继续作为 LLM 输入压缩层，但必须经过白名单瘦身。
```

最终要达到的效果是：

```text
代码负责确定性时间坐标。
模型负责内容理解与编排。
render 只相信 source_id + local_start/local_end。
common analysis 只跑一次。
旧虚拟时间产物直接废弃。
```
