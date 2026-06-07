# content_analysis micro_segments 分批预选优化方案与代码开发指南

## 1. 背景与问题

当前 AI 配音链路中，`content_analysis` 的职责是从 `asr_micro_segment` 生成的 `micro_segments` 中选出适合进入 AI 配音短视频候选池的片段。

当前链路大致是：

```text
asr_micro_segment
  ↓
content_analysis
  ↓
ai_voiceover_candidate_materialize
  ↓
short_video_edit_plan
  ↓
voiceover_script
  ↓
tts / subtitles / cut_plan / render
```

这次报错的根因是：`content_analysis` 当前一次性读取 `asr_micro_segment.segments`，然后把大量 `micro_segment` 的完整 ASR 文本、画面摘要、摘要字段拼成一个大 prompt，导致输入长度超过配置限制。

现有 `_build_content_analysis_micro_segment_text()` 的核心问题不是 `micro_segments` 这个设计本身，而是它把全部 segment 一次性拼给大模型。`micro_segments` 是正确方向，它解决了原来按大 chunk 选片不够精细的问题；真正需要优化的是 `content_analysis` 前的候选筛选方式。

因此，本方案不回退到旧的 `timeline_digest` 选片方式，而是保留 `micro_segments`，新增一层“分 batch 预选 segment”的机制。

---

## 2. 优化目标

### 2.1 主要目标

1. 保留 `micro_segments` 作为 AI 配音候选选择的基本单位。
2. 避免 `content_analysis` 一次性接收所有 micro_segments。
3. 通过分 batch 并发预选，先把大量 micro_segments 缩小为较小候选池。
4. 最终 `content_analysis` 只对预选后的候选池做最终选择。
5. 下游继续通过 `micro_segment_id` 回查原始时间、source_id、ASR、画面信息，避免破坏现有剪辑链路。
6. 不影响原声高光重组流程，不影响 `short_video_edit_plan`、`voiceover_script`、`tts`、`cut_plan` 的现有职责。

### 2.2 非目标

本次不做以下事情：

1. 不删除 `asr_micro_segment`。
2. 不把 `content_analysis` 直接改回吃 `timeline_digest`。
3. 不让大模型把一批 segment 总结成一篇无法定位原视频的摘要。
4. 不修改 `short_video_edit_plan` 的核心输入结构。
5. 不改渲染、字幕、TTS、cut_plan 的主逻辑。

---

## 3. 推荐后的整体链路

新增步骤后，AI 配音链路建议变成：

```text
asr_micro_segment
  ↓
content_analysis_preselect        新增：分 batch 预选 micro_segment_id
  ↓
content_analysis                  修改：只读取预选候选池，做最终 selected_segments
  ↓
ai_voiceover_candidate_materialize
  ↓
short_video_edit_plan
  ↓
voiceover_script
  ↓
tts
  ↓
subtitles
  ↓
cut_plan
  ↓
render
```

核心原则：

```text
给大模型看的内容可以压缩；
给下游代码传递的核心必须是 micro_segment_id；
真正剪辑时再通过 micro_segment_id 回查原始 asr_micro_segment。
```

---

## 4. 分 batch 预选策略

### 4.1 总体规则

根据 `micro_segments` 总数动态决定预选策略：

```text
总 micro_segments <= 10：
    不走预选，直接进入 content_analysis。

11 <= 总 micro_segments <= 30：
    batch_size = 10
    max_keep_per_batch = 5
    max_final_candidates = 20

总 micro_segments > 30：
    batch_size = 10
    max_keep_per_batch = 3
    max_final_candidates = 30
```

原因：

1. 10 个以内，直接让 `content_analysis` 选即可，没有必要多跑一轮模型。
2. 30 个以内，素材规模不大，每批可以多保留一些，避免误删。
3. 超过 30 个，素材池较大，每批保留过多会导致最终候选池继续膨胀，所以要收紧。

### 4.2 以这次 65 个 micro_segments 为例

```text
65 个 micro_segments
batch_size = 10
max_keep_per_batch = 3
```

会被切成 7 批：

```text
batch_001：1 - 10
batch_002：11 - 20
batch_003：21 - 30
batch_004：31 - 40
batch_005：41 - 50
batch_006：51 - 60
batch_007：61 - 65
```

每批最多保留 3 个：

```text
7 批 × 每批最多 3 个 = 最多 21 个候选
```

最终 `content_analysis` 只接收这 21 个以内的候选，而不是一次性接收 65 个完整 segment。

---

## 5. 新增配置建议

在 `config.toml` 中新增：

```toml
[content_analysis_preselect]
enabled = true

# 总数小于等于该值时，跳过预选，直接 content_analysis
direct_threshold = 10

# 小规模 segment 池规则
small_total_threshold = 30
small_batch_size = 10
small_max_keep_per_batch = 5
small_max_final_candidates = 20

# 大规模 segment 池规则
large_batch_size = 10
large_max_keep_per_batch = 3
large_max_final_candidates = 30

# 并发数，第一版建议 4，稳定后再调高
max_workers = 4

# 单个 batch 输入字段压缩上限
max_asr_chars_per_segment = 220
max_visual_chars_per_segment = 100
max_summary_chars_per_segment = 120
max_input_chars_per_batch = 8000

# 模型输出安全控制
require_micro_segment_id = true
allow_empty_batch_result = true
```

同时建议在 `[llm_input]` 中新增或确认：

```toml
[llm_input]
max_content_analysis_preselect_input_chars = 9000
max_content_analysis_input_chars = 12000
```

说明：

1. `max_content_analysis_preselect_input_chars` 用于限制单个 batch 的模型输入。
2. `max_content_analysis_input_chars` 用于限制最终 `content_analysis` 输入。
3. 不建议简单提高 `max_text_agent_input_chars`，否则只是绕过问题，不能解决长视频、多源视频的根因。

---

## 6. 需要修改的文件

### 6.1 `config.toml`

新增 `[content_analysis_preselect]` 配置段。

### 6.2 `newsclip_agent/workflow_registry.py`

在 AI 配音 workflow 中插入新步骤：

```text
asr_micro_segment
content_analysis_preselect
content_analysis
ai_voiceover_candidate_materialize
```

需要同时修改：

1. `WORKFLOWS[("ai_voiceover", False)]`
2. `WORKFLOWS[("ai_voiceover", True)]`
3. `DEPENDENCIES`

新增依赖建议：

```python
"content_analysis_preselect": ["asr_micro_segment", "timeline_digest", "source_aggregate"],
"content_analysis": ["content_analysis_preselect", "asr_micro_segment", "timeline_digest", "source_aggregate"],
```

注意：

1. 统一多源流程必须插入该步骤。
2. 单源 AI 配音流程也可以插入该步骤，但通过 `direct_threshold` 自动跳过小任务。
3. 原声重组流程不需要加入该步骤。

### 6.3 `newsclip_agent/pipeline.py`

需要新增或修改以下内容：

1. `AGENT_INFO` 增加 `content_analysis_preselect`。
2. 新增 `step_content_analysis_preselect()`。
3. 新增 `_build_content_analysis_preselect_batch_text()`。
4. 新增 `_run_content_analysis_preselect_batch()`。
5. 新增 `_merge_content_analysis_preselect_results()`。
6. 修改 `_build_content_analysis_micro_segment_text()`，让它优先读取预选候选池。
7. 保留无预选时的 fallback，避免旧任务或小任务失败。
8. 修改 `_log_llm_input_size()` 的错误提示，避免一直提示“应改用 timeline_digest”。

### 6.4 `newsclip_agent/prompts.py`

新增 `CONTENT_ANALYSIS_PRESELECT_PROMPT`。

要求 prompt 只做“分 batch 选 ID”，不要让模型做全文总结。

---

## 7. 关键数据结构设计

### 7.1 `content_analysis_preselect.json`

建议输出文件：

```json
{
  "version": "content_analysis_preselect_v1",
  "enabled": true,
  "source": "asr_micro_segment",
  "strategy": {
    "total_segments": 65,
    "direct_threshold": 10,
    "batch_size": 10,
    "max_keep_per_batch": 3,
    "max_final_candidates": 30,
    "max_workers": 4
  },
  "batch_count": 7,
  "input_segment_count": 65,
  "kept_segment_count": 21,
  "kept_segments": [
    {
      "micro_segment_id": "source_001_ms_0007",
      "source_id": "source_1",
      "batch_id": "batch_001",
      "score": 8,
      "summary": "一句话说明这段讲什么",
      "reason": "信息完整，有明确新闻事实和画面支撑"
    }
  ],
  "batches": [
    {
      "batch_id": "batch_001",
      "start_index": 0,
      "end_index": 10,
      "input_count": 10,
      "kept_count": 3,
      "status": "success"
    }
  ],
  "warnings": []
}
```

### 7.2 模型输出格式

每个 batch 模型只输出：

```json
{
  "kept_segments": [
    {
      "micro_segment_id": "source_001_ms_0007",
      "score": 8,
      "summary": "一句话摘要",
      "reason": "为什么适合进入候选池"
    }
  ]
}
```

必须要求：

1. `micro_segment_id` 必须来自输入。
2. 不允许模型编造新的 ID。
3. `score` 建议 1～10。
4. 可以返回空数组，表示这一批没有值得保留的内容。
5. 每批最多返回 `max_keep_per_batch` 个。

---

## 8. Prompt 设计建议

### 8.1 新增 `CONTENT_ANALYSIS_PRESELECT_PROMPT`

建议写成中文：

```python
CONTENT_ANALYSIS_PRESELECT_PROMPT = """
你是新闻短视频 AI 配音素材预选助手。

你将看到一批已经按 ASR 语义切好的 micro_segments。
每个 micro_segment_id 是唯一标识，后续剪辑只能通过这些 ID 回查原视频时间。

你的任务：
从当前这一批 micro_segments 中，选出适合进入 AI 配音短视频候选池的片段。

选择标准：
1. 优先选择包含明确新闻事实、人物表态、现场动作、政策变化、冲突变化、结论信息的片段。
2. 优先选择可以独立解释一个新闻点的片段。
3. 优先选择画面和声音能互相支撑的片段。
4. 不要选择纯过渡、重复寒暄、背景噪声、无关口播、信息空泛的片段。
5. 如果这一批没有值得保留的片段，可以返回空数组。
6. 不要为了凑数量强行选择。
7. 不要输出输入中不存在的 micro_segment_id。
8. 不要输出时间码。
9. 不要改写 micro_segment_id。

输出 JSON：
{
  "kept_segments": [
    {
      "micro_segment_id": "输入中的 micro_segment_id",
      "score": 1-10,
      "summary": "一句话说明这段讲什么",
      "reason": "为什么适合保留"
    }
  ]
}
"""
```

### 8.2 batch 输入文本格式

每批输入建议格式：

```text
任务：从当前 batch 的 micro_segments 中预选 AI 配音候选片段。

batch_id: batch_001
max_keep_per_batch: 3

规则：
- 只能选择下面出现过的 micro_segment_id。
- 最多选择 3 个。
- 可以一个都不选。
- 不要输出时间码。

micro_segments:

[source_001_ms_0001]
source=source_1
index=1
中文摘要=...
声音=...
画面=...
duration=...

[source_001_ms_0002]
...
```

注意：

1. batch 阶段可以给模型看压缩后的 ASR 和画面信息。
2. 不需要完整 ASR，保留足够判断即可。
3. batch 阶段只负责“选 ID”，不是生成最终新闻概览。

---

## 9. 代码开发指南

### 9.1 修改 `AGENT_INFO`

在 `newsclip_agent/pipeline.py` 的 `AGENT_INFO` 中新增：

```python
"content_analysis_preselect": (
    "agents/content_analysis_preselect",
    "content_analysis_preselect.json",
    prompts.CONTENT_ANALYSIS_PRESELECT_PROMPT,
    "content_analysis_preselect_v1",
),
```

注意：

1. 不要替换现有 `content_analysis`。
2. 新步骤只作为 `content_analysis` 的前置预选。
3. 输出目录独立，方便调试和回滚。

### 9.2 修改 workflow

在 `newsclip_agent/workflow_registry.py` 中，AI 配音流程插入：

```python
"asr_micro_segment",
"content_analysis_preselect",
"content_analysis",
"ai_voiceover_candidate_materialize",
```

依赖新增：

```python
"content_analysis_preselect": ["asr_micro_segment", "timeline_digest", "source_aggregate"],
"content_analysis": ["content_analysis_preselect", "asr_micro_segment", "timeline_digest", "source_aggregate"],
```

鲁棒性要求：

1. 如果 `content_analysis_preselect.enabled = false`，步骤也应该能写出一个 pass-through 输出，不能让依赖断掉。
2. 如果总 segment 数小于等于 `direct_threshold`，也写出 pass-through 输出，`kept_segments` 等于原 segment 的轻量列表。
3. 这样 `content_analysis` 永远可以读取 `content_analysis_preselect`，不需要判断文件是否缺失。

### 9.3 新增 `step_content_analysis_preselect()`

伪代码：

```python
def step_content_analysis_preselect(self) -> None:
    source_doc = self._load_step_json("asr_micro_segment")
    segments = [x for x in source_doc.get("segments", []) if isinstance(x, dict)]

    cfg = self.config.raw.get("content_analysis_preselect", {})
    enabled = bool(cfg.get("enabled", True))

    strategy = self._resolve_content_analysis_preselect_strategy(len(segments), cfg)
    input_hash = stable_hash({
        "asr_micro_segment": self._step_content_hash("asr_micro_segment"),
        "strategy": strategy,
        "enabled": enabled,
    })

    if self._can_reuse("content_analysis_preselect", input_hash):
        print("复用缓存: content_analysis_preselect")
        return

    if not enabled or len(segments) <= strategy["direct_threshold"]:
        output = self._build_content_analysis_preselect_passthrough(segments, strategy, enabled)
        self._write_content_analysis_preselect_output(output, input_hash)
        return

    batches = self._split_micro_segments_for_preselect(segments, strategy["batch_size"])

    # 并发调用每个 batch
    results = self._run_content_analysis_preselect_batches(batches, strategy)

    output = self._merge_content_analysis_preselect_results(
        segments=segments,
        batch_results=results,
        strategy=strategy,
    )
    self._write_content_analysis_preselect_output(output, input_hash)
```

### 9.4 策略解析函数

新增：

```python
def _resolve_content_analysis_preselect_strategy(self, total_segments: int, cfg: dict[str, Any]) -> dict[str, Any]:
    direct_threshold = int(cfg.get("direct_threshold", 10) or 10)
    small_total_threshold = int(cfg.get("small_total_threshold", 30) or 30)

    if total_segments <= small_total_threshold:
        batch_size = int(cfg.get("small_batch_size", 10) or 10)
        max_keep = int(cfg.get("small_max_keep_per_batch", 5) or 5)
        max_final = int(cfg.get("small_max_final_candidates", 20) or 20)
    else:
        batch_size = int(cfg.get("large_batch_size", 10) or 10)
        max_keep = int(cfg.get("large_max_keep_per_batch", 3) or 3)
        max_final = int(cfg.get("large_max_final_candidates", 30) or 30)

    return {
        "direct_threshold": max(0, direct_threshold),
        "small_total_threshold": max(1, small_total_threshold),
        "batch_size": max(1, batch_size),
        "max_keep_per_batch": max(1, max_keep),
        "max_final_candidates": max(1, max_final),
        "max_workers": max(1, int(cfg.get("max_workers", 4) or 4)),
        "max_asr_chars_per_segment": max(40, int(cfg.get("max_asr_chars_per_segment", 220) or 220)),
        "max_visual_chars_per_segment": max(20, int(cfg.get("max_visual_chars_per_segment", 100) or 100)),
        "max_summary_chars_per_segment": max(20, int(cfg.get("max_summary_chars_per_segment", 120) or 120)),
        "max_input_chars_per_batch": max(1000, int(cfg.get("max_input_chars_per_batch", 8000) or 8000)),
    }
```

### 9.5 batch 切分函数

```python
def _split_micro_segments_for_preselect(self, segments: list[dict[str, Any]], batch_size: int) -> list[dict[str, Any]]:
    batches = []
    for start in range(0, len(segments), batch_size):
        end = min(start + batch_size, len(segments))
        batches.append({
            "batch_id": f"batch_{len(batches) + 1:03d}",
            "start_index": start,
            "end_index": end,
            "segments": segments[start:end],
        })
    return batches
```

### 9.6 构造 batch 输入

```python
def _build_content_analysis_preselect_batch_text(
    self,
    batch: dict[str, Any],
    strategy: dict[str, Any],
) -> str:
    lines = [
        "任务：从当前 batch 的 micro_segments 中预选 AI 配音候选片段。",
        f"batch_id: {batch['batch_id']}",
        f"max_keep_per_batch: {strategy['max_keep_per_batch']}",
        "",
        "规则：",
        "- 只能选择下面出现过的 micro_segment_id。",
        f"- 最多选择 {strategy['max_keep_per_batch']} 个。",
        "- 可以一个都不选。",
        "- 不要输出时间码。",
        "- 不要改写 micro_segment_id。",
        "",
        "micro_segments:",
        "",
    ]

    for offset, seg in enumerate(batch["segments"], start=1):
        msid = str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
        if not msid:
            continue
        source_id = str(seg.get("source_id") or "").strip()
        duration = seg.get("duration_seconds", "")
        summary = compact_text(str(seg.get("summary") or ""), max_chars=strategy["max_summary_chars_per_segment"])
        speech = sanitize_llm_text(str(seg.get("asr_text") or seg.get("speech") or ""), max_chars=strategy["max_asr_chars_per_segment"])
        visual = compact_text(str(seg.get("visual_summary") or seg.get("visual") or ""), max_chars=strategy["max_visual_chars_per_segment"])

        lines.append(f"[{msid}]")
        lines.append(f"source={source_id}")
        lines.append(f"index={batch['start_index'] + offset}")
        lines.append(f"duration={duration}")
        if summary:
            lines.append(f"摘要：{summary}")
        if speech:
            lines.append(f"声音：{speech}")
        if visual:
            lines.append(f"画面：{visual}")
        lines.append("")

    text = "\n".join(lines).strip()
    if len(text) > strategy["max_input_chars_per_batch"]:
        # 兜底：继续压缩 speech / visual / summary，不能直接放任超限
        text = self._shrink_preselect_batch_text(batch, strategy)
    return text
```

注意：

1. `summary / speech / visual` 都要做字符限制。
2. batch 阶段不要传完整 JSON。
3. 即使 batch_size = 10，也要保留输入长度兜底。

### 9.7 并发执行 batch

建议使用 `ThreadPoolExecutor`：

```python
from concurrent.futures import ThreadPoolExecutor, as_completed
```

伪代码：

```python
def _run_content_analysis_preselect_batches(self, batches: list[dict[str, Any]], strategy: dict[str, Any]) -> list[dict[str, Any]]:
    max_workers = min(strategy["max_workers"], len(batches)) or 1
    results = []

    def run_one(batch: dict[str, Any]) -> dict[str, Any]:
        text_input = self._build_content_analysis_preselect_batch_text(batch, strategy)
        parsed = self._run_text_agent_for_preselect_batch(batch, text_input)
        return self._normalize_content_analysis_preselect_batch_result(batch, parsed, strategy)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(run_one, batch): batch for batch in batches}
        for future in as_completed(future_map):
            batch = future_map[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({
                    "batch_id": batch["batch_id"],
                    "status": "failed",
                    "error": str(exc),
                    "kept_segments": [],
                    "input_count": len(batch.get("segments") or []),
                })

    return sorted(results, key=lambda x: x.get("batch_id", ""))
```

鲁棒性要求：

1. 单个 batch 失败，不建议直接让整个任务失败。
2. 可以记录该 batch 的错误，并继续其他 batch。
3. 如果所有 batch 都失败，再抛出用户可见错误。
4. 如果部分失败但仍有候选，可以继续进入最终 `content_analysis`，并在输出中写入 `warnings`。

### 9.8 避免并发写同一目录冲突

不能让多个线程同时写同一个 `agents/content_analysis_preselect/vX` 目录下的同名文件。

建议两种方式：

方案 A：batch 调用不使用 `_run_text_agent()`，而是新增一个轻量方法，直接调用 `self.llm_text.call_json()`，每个 batch 写到独立目录：

```text
agents/content_analysis_preselect/v1/batches/batch_001/
agents/content_analysis_preselect/v1/batches/batch_002/
```

方案 B：如果复用 `_run_text_agent()`，需要给每个 batch 虚拟 step 名称，但这会污染 `AGENT_INFO` 和 manifest，不推荐。

建议使用方案 A。

### 9.9 batch 调用方法

```python
def _run_text_agent_for_preselect_batch(self, batch: dict[str, Any], input_data: str) -> Any:
    if self.llm_text is None:
        raise RuntimeError("缺少 text LLM 配置")

    llm_cfg = self.config.llm
    provider = llm_cfg.get("text_llm_provider", "openai")
    model = self.options.model or llm_cfg.get("text_llm_model_name") or llm_cfg.get(f"text_{provider}_model_name")
    fallback = llm_cfg.get(f"text_{provider}_fallback_models", [])
    max_tokens = int(llm_cfg.get("text_llm_max_tokens", 16000) or 16000)

    version, vdir = self._version_dir("content_analysis_preselect", "agents/content_analysis_preselect")
    batch_dir = vdir / "batches" / batch["batch_id"]
    ensure_dir(batch_dir)

    self._log_llm_input_size("content_analysis_preselect", input_data)
    write_text(batch_dir / "input.txt", input_data)
    write_json(batch_dir / "input_meta.json", {"type": "text", "chars": len(input_data)})
    write_text(batch_dir / "prompt.txt", prompts.CONTENT_ANALYSIS_PRESELECT_PROMPT)

    result = self.llm_text.call_json(
        model=model,
        fallback_models=fallback,
        prompt=prompts.CONTENT_ANALYSIS_PRESELECT_PROMPT,
        input_data=input_data,
        temperature=0.2,
        debug_dir=batch_dir / "_llm_debug",
        max_tokens=max_tokens,
    )

    write_json(batch_dir / "raw_response.json", {
        "model": result.model,
        "created_at": now_iso(),
        "raw_text": result.raw_text,
        "usage": result.usage,
        "finish_reason": result.finish_reason,
        "latency_ms": result.latency_ms,
    })
    write_json(batch_dir / "model_output.json", result.parsed)
    return result.parsed
```

注意：

1. 上面是开发示意，不建议直接复制后不检查上下文。
2. `_version_dir()` 如果每个线程同时调用可能产生版本目录竞争，最好在 `step_content_analysis_preselect()` 开头先确定一次 `version, vdir`，然后传给 batch 方法。
3. 线程里只写各自 batch 子目录，避免同名文件冲突。

### 9.10 规范化 batch 输出

```python
def _normalize_content_analysis_preselect_batch_result(
    self,
    batch: dict[str, Any],
    parsed: Any,
    strategy: dict[str, Any],
) -> dict[str, Any]:
    allowed_ids = {
        str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
        for seg in batch.get("segments", [])
        if str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
    }

    raw_items = []
    if isinstance(parsed, dict) and isinstance(parsed.get("kept_segments"), list):
        raw_items = parsed.get("kept_segments")
    elif isinstance(parsed, list):
        raw_items = parsed

    kept = []
    seen = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        msid = str(item.get("micro_segment_id") or item.get("id") or "").strip()
        if not msid or msid not in allowed_ids or msid in seen:
            continue
        seen.add(msid)
        score = item.get("score", 0)
        try:
            score = int(score)
        except Exception:
            score = 0
        kept.append({
            "micro_segment_id": msid,
            "batch_id": batch["batch_id"],
            "score": max(0, min(10, score)),
            "summary": compact_text(str(item.get("summary") or ""), max_chars=160),
            "reason": compact_text(str(item.get("reason") or ""), max_chars=160),
        })
        if len(kept) >= strategy["max_keep_per_batch"]:
            break

    return {
        "batch_id": batch["batch_id"],
        "start_index": batch["start_index"],
        "end_index": batch["end_index"],
        "input_count": len(batch.get("segments") or []),
        "kept_count": len(kept),
        "status": "success",
        "kept_segments": kept,
    }
```

关键鲁棒性：

1. 丢弃模型编造的 ID。
2. 丢弃重复 ID。
3. 限制每批最多保留数量。
4. 限制 `summary` 和 `reason` 长度。
5. `score` 做类型转换和边界限制。

### 9.11 汇总 batch 结果

```python
def _merge_content_analysis_preselect_results(
    self,
    segments: list[dict[str, Any]],
    batch_results: list[dict[str, Any]],
    strategy: dict[str, Any],
) -> dict[str, Any]:
    by_id = {
        str(seg.get("micro_segment_id") or seg.get("id") or "").strip(): seg
        for seg in segments
        if str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
    }

    items = []
    warnings = []
    for result in batch_results:
        if result.get("status") != "success":
            warnings.append({
                "batch_id": result.get("batch_id"),
                "error": result.get("error"),
            })
            continue
        for item in result.get("kept_segments") or []:
            msid = item.get("micro_segment_id")
            if msid in by_id:
                source = by_id[msid]
                enriched = dict(item)
                enriched["source_id"] = source.get("source_id", "")
                enriched["duration_seconds"] = source.get("duration_seconds", "")
                items.append(enriched)

    # 去重，保留第一次出现或最高分
    dedup = {}
    for item in items:
        msid = item["micro_segment_id"]
        old = dedup.get(msid)
        if old is None or int(item.get("score") or 0) > int(old.get("score") or 0):
            dedup[msid] = item

    kept = list(dedup.values())
    kept.sort(key=lambda x: int(x.get("score") or 0), reverse=True)
    kept = kept[: strategy["max_final_candidates"]]

    return {
        "version": "content_analysis_preselect_v1",
        "enabled": True,
        "source": "asr_micro_segment",
        "strategy": strategy,
        "batch_count": len(batch_results),
        "input_segment_count": len(segments),
        "kept_segment_count": len(kept),
        "kept_segments": kept,
        "batches": [
            {k: v for k, v in result.items() if k != "kept_segments"}
            for result in batch_results
        ],
        "warnings": warnings,
    }
```

建议增强：

1. 如果担心一个 source 独占全部候选，可以在汇总时加 source 均衡。
2. 如果 `kept` 为空，但原始 segments 不为空，可以 fallback 选前若干条信息量较高的 segment，避免流程断掉。

### 9.12 写出 preselect 结果

建议封装：

```python
def _write_content_analysis_preselect_output(self, output: dict[str, Any], input_hash: str) -> None:
    version, vdir = self._version_dir("content_analysis_preselect", "agents/content_analysis_preselect")
    ensure_dir(vdir)
    out = write_json(vdir / "content_analysis_preselect.json", output)
    status = self._base_status("content_analysis_preselect", version, input_hash, [out])
    self._write_status(vdir, status)
    self._record_step(
        step="content_analysis_preselect",
        version=version,
        status="success",
        output=relpath(out, self.task_dir),
        input_hash=input_hash,
        output_files=[out],
        extra={
            "summary": {
                "input_segments": output.get("input_segment_count", 0),
                "kept_segments": output.get("kept_segment_count", 0),
                "batch_count": output.get("batch_count", 0),
            }
        },
    )
    print("完成: content_analysis_preselect")
```

注意：

1. 实际实现时避免 `_version_dir()` 在同一步里重复生成不同版本目录。
2. 最好在 step 开头确定 `version, vdir`，所有 batch 和最终文件都写入同一个 `vdir`。

### 9.13 修改 `_build_content_analysis_micro_segment_text()`

核心修改：让最终 `content_analysis` 优先读取 `content_analysis_preselect.kept_segments`。

逻辑建议：

```python
def _load_segments_for_content_analysis(self) -> list[dict[str, Any]]:
    micro_doc = self._load_step_json("asr_micro_segment")
    all_segments = [x for x in micro_doc.get("segments", []) if isinstance(x, dict)]
    by_id = {
        str(seg.get("micro_segment_id") or seg.get("id") or "").strip(): seg
        for seg in all_segments
        if str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
    }

    preselect_doc = self._load_optional_step_json("content_analysis_preselect", {})
    kept = preselect_doc.get("kept_segments") if isinstance(preselect_doc, dict) else None
    if isinstance(kept, list) and kept:
        selected = []
        for item in kept:
            if not isinstance(item, dict):
                continue
            msid = str(item.get("micro_segment_id") or "").strip()
            if msid in by_id:
                seg = dict(by_id[msid])
                seg["preselect_summary"] = item.get("summary") or ""
                seg["preselect_reason"] = item.get("reason") or ""
                seg["preselect_score"] = item.get("score") or 0
                selected.append(seg)
        if selected:
            return selected

    return all_segments
```

然后 `_build_content_analysis_micro_segment_text()` 改成读取这个函数返回的 segments，而不是永远读取全量。

最终 `content_analysis` 输入仍然应该保留：

```text
micro_segment_id
source_id
duration
preselect_summary
preselect_reason
短 ASR
短 visual
```

不要再拼完整 ASR。

---

## 10. `short_video_edit_plan` 是否需要修改

本次不建议修改 `short_video_edit_plan`。

原因：

1. `short_video_edit_plan` 当前读取的是 `ai_voiceover_candidate_materialize` 后的 `candidate_clips`。
2. 它已经对输入做了字段级裁剪：候选数量有限，summary、speech、visual 都有长度限制。
3. 它的职责是从 candidate_clips 中规划短视频结构，不应该直接接收 batch 预选结果。
4. batch 预选结果只服务于 `content_analysis`，不应该绕过 `ai_voiceover_candidate_materialize`。

保持现有下游链路最稳：

```text
content_analysis_preselect
  ↓
content_analysis
  ↓ selected_segments
ai_voiceover_candidate_materialize
  ↓ candidate_clips
short_video_edit_plan
```

---

## 11. 鲁棒性要求

### 11.1 ID 校验

必须校验模型输出的 `micro_segment_id` 是否存在于当前 batch 输入。

不合法 ID 直接丢弃，不要进入下游。

### 11.2 空 batch 允许

某一批可能没有有价值内容，允许返回：

```json
{"kept_segments": []}
```

不要因为单批为空就失败。

### 11.3 单 batch 失败处理

建议：

```text
单个 batch 失败：记录 warning，继续其他 batch。
全部 batch 失败：抛出错误。
部分成功且有候选：继续。
```

### 11.4 预选结果为空的 fallback

如果所有 batch 都成功但没有选出任何候选，建议 fallback：

```text
从原始 micro_segments 中选择前 N 个有 ASR 文本的 segment，N 可以是 min(10, len(segments))。
```

同时在输出中写入 warning：

```json
{"type": "empty_preselect_fallback"}
```

这样可以避免流程完全断掉。

### 11.5 输入长度兜底

即使 batch_size = 10，也必须检查输入长度。

如果单批输入超过限制，要进一步压缩：

```text
ASR 220 -> 120
visual 100 -> 60
summary 120 -> 80
```

仍然超过则抛出清晰错误。

### 11.6 并发安全

并发执行 batch 时：

1. 每个线程写独立 batch 目录。
2. 不要多个线程同时写 `final_output.json`、`status.json`、`content_analysis_preselect.json`。
3. 最终汇总文件只在主线程写。

### 11.7 缓存复用

`input_hash` 应包含：

```text
asr_micro_segment 内容 hash
preselect 策略配置
prompt version
model 名称
```

否则修改 batch_size 或 max_keep 后可能误用旧缓存。

---

## 12. 错误提示优化

当前 `_log_llm_input_size()` 报错文案固定提示“应改用 timeline_digest”，这对现在的 `content_analysis` micro_segment 路线不准确。

建议改成根据 step 区分：

```python
if len(text) > limit:
    if step == "content_analysis":
        hint = "请启用 content_analysis_preselect 或降低 micro_segment 输入字段长度。"
    elif step == "content_analysis_preselect":
        hint = "请降低 batch_size 或减少每个 segment 的 ASR/画面字段长度。"
    else:
        hint = "请检查该步骤输入压缩策略。"
    raise RuntimeError(f"LLM input size [{step}] exceeds limit={limit}; {hint}")
```

这样排查时更准确。

---

## 13. 开发顺序建议

### 第一步：只加配置和 workflow

1. `config.toml` 新增 `[content_analysis_preselect]`。
2. `workflow_registry.py` 插入新步骤和依赖。
3. `pipeline.py` 增加空实现，先让步骤可以 pass-through。

验证目标：

```text
pipeline 能跑到 content_analysis_preselect，并生成 pass-through 文件。
```

### 第二步：实现 direct_threshold pass-through

当 segment 数小于等于 10 时，不调用模型，直接输出轻量 kept_segments。

验证目标：

```text
短视频不会多跑一次 LLM。
```

### 第三步：实现 batch 切分和单 batch 调用

先不要并发，只串行跑 batch，方便调试。

验证目标：

```text
65 个 segments 能切成 7 批；
每批输出 kept_segments；
非法 ID 会被过滤。
```

### 第四步：实现并发

串行稳定后，再引入 `ThreadPoolExecutor`。

验证目标：

```text
max_workers = 4 时，batch 并发执行；
输出顺序仍然按 batch_id 排序；
失败 batch 有 warning。
```

### 第五步：修改 content_analysis 输入

让 `_build_content_analysis_micro_segment_text()` 只读取预选后的候选池。

验证目标：

```text
content_analysis input.txt 不再包含全部 65 个 segments；
输入长度低于 max_content_analysis_input_chars。
```

### 第六步：跑完整 AI 配音链路

验证：

```text
content_analysis
ai_voiceover_candidate_materialize
short_video_edit_plan
voiceover_script
tts
subtitles
cut_plan
render
```

确保下游没有因为新增步骤受到影响。

---

## 14. 测试用例建议

### 14.1 短视频测试

条件：

```text
micro_segments <= 10
```

预期：

```text
content_analysis_preselect pass-through
不调用 batch LLM
content_analysis 正常运行
```

### 14.2 中等视频测试

条件：

```text
11 <= micro_segments <= 30
```

预期：

```text
batch_size = 10
max_keep_per_batch = 5
最终候选不超过 20
```

### 14.3 长视频 / 多源测试

条件：

```text
micro_segments > 30
```

预期：

```text
batch_size = 10
max_keep_per_batch = 3
最终候选不超过 30
content_analysis 输入不超过 12000 chars
```

### 14.4 模型输出非法 ID 测试

让模型输出一个不存在的 ID。

预期：

```text
非法 ID 被过滤
不会进入 content_analysis
不会进入 ai_voiceover_candidate_materialize
```

### 14.5 单 batch 失败测试

模拟一个 batch 调用失败。

预期：

```text
该 batch 记录 warning
其他 batch 正常
只要最终候选不为空，流程继续
```

### 14.6 所有 batch 都为空测试

预期：

```text
触发 fallback
写入 warning
流程继续或给出清晰错误
```

---

## 15. 回滚方案

如果新逻辑上线后出现问题，可以快速回滚：

### 15.1 配置关闭

```toml
[content_analysis_preselect]
enabled = false
```

关闭后 `content_analysis_preselect` 仍写 pass-through 输出，不影响依赖。

### 15.2 workflow 回滚

如果需要完全回滚，移除 workflow 中的：

```text
content_analysis_preselect
```

并恢复：

```python
"content_analysis": ["asr_micro_segment", "timeline_digest", "source_aggregate"]
```

### 15.3 函数回滚

恢复 `_build_content_analysis_micro_segment_text()` 为直接读取 `asr_micro_segment`。

不建议作为长期方案，只用于紧急回滚。

---

## 16. 风险与注意事项

### 16.1 过早过滤风险

batch 预选可能误删好片段。

缓解：

```text
30 个以内每批最多保留 5 个；
超过 30 个每批最多保留 3 个；
最终候选池保留到 20～30 个；
允许 batch 返回 0 个，但不强制凑数。
```

### 16.2 模型偏向前半段风险

如果最终汇总只按 score 排序，可能让某些 batch 或 source 被挤掉。

缓解：

1. 汇总时保留 batch_id。
2. 可以增加 source 均衡策略。
3. 如果是多源任务，可以保证每个 source 至少保留若干候选。

### 16.3 并发限流风险

max_workers 不宜一开始过大。

建议：

```text
第一版 max_workers = 4
稳定后再调到 6 或 8
```

### 16.4 下游 ID 丢失风险

任何阶段都不能丢 `micro_segment_id`。

禁止只输出摘要，不带 ID。

### 16.5 旧任务兼容风险

旧任务可能没有 `content_analysis_preselect` 输出。

缓解：

`_load_segments_for_content_analysis()` 必须支持 fallback 到 `asr_micro_segment`。

---

## 17. 最终推荐结论

这次问题不应该通过简单提高 `max_text_agent_input_chars` 解决，也不应该回退到 `timeline_digest`。

推荐正式方案是：

```text
新增 content_analysis_preselect。
按 micro_segments 总数动态选择 batch 策略。
每批让大模型选 micro_segment_id，而不是总结全文。
最终 content_analysis 只读取预选候选池。
下游继续通过 micro_segment_id 回查原始 segment，生成 candidate_clips。
short_video_edit_plan 保持现有 candidate_clips 输入，不做大改。
```

推荐默认策略：

```text
总数 <= 10：直接 content_analysis
11～30：batch_size=10，每批最多留 5 个，最终最多 20 个
>30：batch_size=10，每批最多留 3 个，最终最多 30 个
max_workers=4
```

这个方案既能解决当前 65 个 micro_segments 导致 content_analysis 输入爆炸的问题，也能兼容短视频、中等视频和长视频，不会破坏后续 `ai_voiceover_candidate_materialize -> short_video_edit_plan -> voiceover_script -> TTS/render` 的主流程。
