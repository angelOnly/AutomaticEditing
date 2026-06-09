# 多视频 AI 配音内容分析分组优化方案与代码开发指南

## 1. 问题结论

本次 8 个视频任务失败，不是视频文件本身无法处理，也不是最终输出视频时长超限，而是 **AI 配音解说链路的 `content_analysis` 步骤输入给文本大模型的字符数超过了系统限制**。

运行日志显示：

```text
LLM input size [content_analysis_preselect]: 4089 chars
LLM input size [content_analysis_preselect]: 3948 chars
LLM input size [content_analysis_preselect]: 3171 chars
完成: content_analysis_preselect

LLM input size [content_analysis]: 13013 chars
运行失败:
LLM input size [content_analysis] exceeds limit=12000
```

含义是：

```text
content_analysis_preselect 分批预筛成功
↓
预筛结果被合并
↓
content_analysis 把合并后的候选 micro_segments 一次性输入大模型
↓
输入 13013 字 > 限制 12000 字
↓
任务失败
```

所以真正超限的位置是：

```text
AI配音解说流程
第 5 步：内容分析 content_analysis
```

当前失败原因可以概括为：

```text
多视频素材池进入 AI 配音链路后，
前面每个视频虽然单独分析，
但后面的 content_analysis 会把预筛后的候选 micro_segments 合并成一份总输入，
一次性让大模型从所有视频里终选，
当 source 数量较多时，最终输入容易超过 max_content_analysis_input_chars。
```

---

## 2. 当前代码流程现状

### 2.1 前面确实是按视频单独分析

多源任务在 `source_analysis` 阶段会对每个 source 单独创建 `PipelineRunner(child_options)`，每个视频独立跑公共分析链路：

```text
metadata
audio_extract
frame_extract
chunk_build
asr
asr_digest
vision
timeline
timeline_digest
```

也就是说，前面不是 8 个视频直接混在一起分析。

### 2.2 后面会汇总成统一素材池

`source_aggregate` 会把每个视频的分析结果汇总成一个统一素材池，包括：

```text
sources
timeline_digest.chunks
raw_asr_index
raw_asr_segment_count
```

此后 AI 配音链路面对的就不再是单个视频，而是：

```text
多个 source 组成的素材池
```

### 2.3 `content_analysis_preselect` 已经分批，但只是预筛

当前已有 `content_analysis_preselect`，它会把 `asr_micro_segment` 产出的 segments 按 batch 切分，例如每批 10 个，然后分批调用大模型预选。

这一步的输入没有超限，日志已经说明：

```text
4089 chars
3948 chars
3171 chars
```

所以预筛分批本身是有效的。

### 2.4 真正的问题在 `content_analysis`

`content_analysis` 当前逻辑大致是：

```python
text_input = self._build_content_analysis_micro_segment_text()
result = self._run_text_agent("content_analysis", text_input)
result = self._normalize_content_analysis_selected_segments(result)
```

这里 `_build_content_analysis_micro_segment_text()` 会从 `_load_segments_for_content_analysis()` 获取候选 micro_segments。

如果存在 `content_analysis_preselect` 结果，它会优先使用 `preselect` 保留的候选；否则回退到全部 `asr_micro_segment`。

问题是：

```text
它拿到候选后，是合并成一份总输入。
```

当前 `_build_content_analysis_micro_segment_text()` 只是按数量限制：

```python
max_segments = cfg.get("max_segments_for_content_analysis", 120)

for seg in micro_segments[:max_segments]:
    ...
```

这不安全，因为 120 个 segment 的真实字符数不稳定：

```text
有的 segment 很短
有的 segment 带 160 字 ASR + 80 字画面 + 摘要
多个 source 合并后很容易超过 12000 字
```

当前配置里也存在容易触发超限的组合：

```toml
[llm_input]
max_content_analysis_input_chars = 12000

[content_analysis_preselect]
large_max_final_candidates = 30
max_asr_chars_per_segment = 220
max_visual_chars_per_segment = 100
max_summary_chars_per_segment = 120

[asr_micro_segment]
max_segments_for_content_analysis = 120
```

预筛最多保留 30 个候选，但最终 content_analysis 会把这些候选一起拼进去。8 个视频时，30 个候选段合并输入达到 13013 字，因此失败。

---

## 3. 优化目标

### 3.1 核心目标

将 `content_analysis` 从“所有 source 候选一次性终选”改成“按 source 分组终选，再汇总”。

新的逻辑：

```text
source 数量 <= 3：
    保持旧逻辑，所有候选一起终选。

source 数量 > 3：
    按 source 分组。
    默认每 3 个 source 一组。
    每组单独构造 content_analysis 输入。
    每组单独调用大模型选择 selected_segments。
    最后汇总各组 selected_segments。
    后续 ai_voiceover_candidate_materialize 仍然读取 content_analysis.selected_segments。
```

例如 8 个视频：

```text
第 1 组：source_001 + source_002 + source_003
第 2 组：source_004 + source_005 + source_006
第 3 组：source_007 + source_008
```

这样每次输入给大模型的候选段只覆盖部分 source，输入长度会明显下降。

### 3.2 保持兼容

优化后要保证：

```text
content_analysis 的最终输出结构仍然包含 selected_segments
每个 selected_segment 仍然使用 micro_segment_id
后续 ai_voiceover_candidate_materialize 不需要大改
不要恢复旧 segment_id/time 协议
不要让模型输出时间码
```

最终输出依然类似：

```json
{
  "version": "ai_voiceover_content_analysis_v2_grouped",
  "input_unit": "micro_segment",
  "selected_segments": [
    {
      "micro_segment_id": "source_001_ms_0001",
      "summary": "...",
      "role": "fact"
    }
  ],
  "materialized_by_code": true
}
```

### 3.3 增强鲁棒性

除了分组，还要补充字符预算控制：

```text
不能只靠 max_segments_for_content_analysis 按数量截断。
必须在构造输入文本时按字符数动态截断。
```

这样即便某一组内容异常长，也不会再次超过 `max_content_analysis_input_chars`。

---

## 4. 推荐配置修改

在 `config.toml` 新增配置段：

```toml
[content_analysis]
# 是否启用按 source 分组终选
group_by_source = true

# source 数量超过这个值才启用分组；3 个以内保持旧逻辑
group_when_source_count_gt = 3

# 每组最多包含几个 source
max_sources_per_group = 3

# 每组最多输入多少个 micro_segment
max_segments_per_group = 40

# 每组输入字符预算。不要贴着 12000，给 prompt 和封装留余量
max_input_chars_per_group = 10000

# 每组最多让模型选择几个 micro_segment
max_selected_segments_per_group = 6

# 所有组汇总后，最终最多保留多少个 selected_segments
max_final_selected_segments = 20

# 如果某组模型失败，是否允许跳过该组继续处理其他组
allow_partial_group_success = true

# 如果分组模式没有选出任何 segment，是否回退到旧的全量 content_analysis
fallback_to_single_pass_when_empty = false
```

可选同步调整：

```toml
[content_analysis_preselect]
large_max_final_candidates = 24
```

说明：

```text
原来 large_max_final_candidates = 30
如果继续保留 30，也可以靠分组解决超限。
但考虑后续短视频规划、配音、TTS 的负担，建议先降到 24。
```

如果希望改动更小，可以先不动 `content_analysis_preselect`，只加分组与字符预算。

---

## 5. 代码修改位置

主要修改文件：

```text
newsclip_agent/pipeline.py
config.toml
```

重点改动函数：

```text
PipelineRunner.step_content_analysis()
PipelineRunner._build_content_analysis_micro_segment_text()
```

新增辅助函数：

```text
PipelineRunner._content_analysis_cfg()
PipelineRunner._group_segments_for_content_analysis()
PipelineRunner._source_order_from_segments()
PipelineRunner._build_content_analysis_group_result()
PipelineRunner._merge_grouped_content_analysis_results()
```

---

## 6. 代码开发细节

### 6.1 新增配置读取函数

在 `PipelineRunner` 类里新增：

```python
def _content_analysis_cfg(self) -> dict[str, Any]:
    raw = self.config.raw.get("content_analysis", {}) or {}
    llm_input_cfg = self.config.raw.get("llm_input", {}) or {}
    asr_micro_cfg = self.config.raw.get("asr_micro_segment", {}) or {}

    return {
        "group_by_source": bool(raw.get("group_by_source", True)),
        "group_when_source_count_gt": max(1, int(raw.get("group_when_source_count_gt", 3) or 3)),
        "max_sources_per_group": max(1, int(raw.get("max_sources_per_group", 3) or 3)),
        "max_segments_per_group": max(1, int(raw.get("max_segments_per_group", 40) or 40)),
        "max_input_chars_per_group": max(
            1000,
            int(
                raw.get(
                    "max_input_chars_per_group",
                    min(
                        10000,
                        int(llm_input_cfg.get("max_content_analysis_input_chars", 12000) or 12000),
                    ),
                )
                or 10000
            ),
        ),
        "max_selected_segments_per_group": max(1, int(raw.get("max_selected_segments_per_group", 6) or 6)),
        "max_final_selected_segments": max(1, int(raw.get("max_final_selected_segments", 20) or 20)),
        "allow_partial_group_success": bool(raw.get("allow_partial_group_success", True)),
        "fallback_to_single_pass_when_empty": bool(raw.get("fallback_to_single_pass_when_empty", False)),
        "legacy_max_segments": max(1, int(asr_micro_cfg.get("max_segments_for_content_analysis", 120) or 120)),
    }
```

目的：

```text
集中处理默认值和类型转换，避免到处散落 int()/bool()。
```

---

### 6.2 新增 source 顺序提取函数

```python
def _source_order_from_segments(self, segments: list[dict[str, Any]]) -> list[str]:
    source_order: list[str] = []
    seen: set[str] = set()

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        source_id = str(seg.get("source_id") or "source_1").strip() or "source_1"
        if source_id not in seen:
            seen.add(source_id)
            source_order.append(source_id)

    return source_order
```

注意：

```text
不要直接 set 排序，因为 source 的顺序最好跟素材原始顺序一致。
```

---

### 6.3 新增按 source 分组函数

```python
def _group_segments_for_content_analysis(
    self,
    segments: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    source_order = self._source_order_from_segments(segments)

    if (
        not cfg["group_by_source"]
        or len(source_order) <= cfg["group_when_source_count_gt"]
    ):
        return [{
            "group_id": "all",
            "group_index": 1,
            "group_count": 1,
            "source_ids": source_order,
            "segments": segments[: cfg["legacy_max_segments"]],
            "grouped": False,
        }]

    by_source: dict[str, list[dict[str, Any]]] = {source_id: [] for source_id in source_order}
    for seg in segments:
        source_id = str(seg.get("source_id") or "source_1").strip() or "source_1"
        by_source.setdefault(source_id, []).append(seg)

    source_groups: list[list[str]] = []
    max_sources = cfg["max_sources_per_group"]
    for start in range(0, len(source_order), max_sources):
        source_groups.append(source_order[start:start + max_sources])

    groups: list[dict[str, Any]] = []
    for index, source_ids in enumerate(source_groups, start=1):
        group_segments: list[dict[str, Any]] = []
        for source_id in source_ids:
            group_segments.extend(by_source.get(source_id, []))

        groups.append({
            "group_id": f"group_{index:03d}",
            "group_index": index,
            "group_count": len(source_groups),
            "source_ids": source_ids,
            "segments": group_segments[: cfg["max_segments_per_group"]],
            "grouped": True,
        })

    return groups
```

设计说明：

```text
source 数量 <= 3 时走 all 组，兼容旧逻辑。
source 数量 > 3 时，每 3 个 source 一组。
每组再限制 max_segments_per_group，避免某组异常膨胀。
```

---

### 6.4 改造 `_build_content_analysis_micro_segment_text()`

当前函数是无参的：

```python
def _build_content_analysis_micro_segment_text(self) -> str:
    micro_segments = self._load_segments_for_content_analysis()
    ...
```

建议改成：

```python
def _build_content_analysis_micro_segment_text(
    self,
    micro_segments: list[dict[str, Any]] | None = None,
    *,
    group_id: str = "all",
    group_index: int = 1,
    group_count: int = 1,
    source_ids: list[str] | None = None,
    max_selected_segments: int | None = None,
    max_input_chars: int | None = None,
) -> str:
    if micro_segments is None:
        micro_segments = self._load_segments_for_content_analysis()

    cfg = self._content_analysis_cfg()
    max_segments = cfg["legacy_max_segments"]
    if max_input_chars is None:
        max_input_chars = cfg["max_input_chars_per_group"]
    if max_selected_segments is None:
        max_selected_segments = cfg["max_selected_segments_per_group"]

    source_ids = source_ids or self._source_order_from_segments(micro_segments)

    lines = [
        "你将看到一组已经按 ASR 语义切好的 micro_segments。",
        "每个 micro_segment_id 是唯一标识。",
        "任务：只从这些 micro_segment_id 中选择适合 AI 配音短视频的事实段。",
        "不要合并多个 micro_segment。",
        "不要输出新的时间码。",
        "输出 JSON 格式：{\"selected_segments\":[{\"micro_segment_id\":\"...\",\"summary\":\"...\",\"role\":\"fact\"}]}",
        "",
        f"当前分组：{group_id}，第 {group_index}/{group_count} 组。",
        "当前组 source_ids：" + ", ".join(source_ids),
        f"当前组最多选择 {max_selected_segments} 个 micro_segment。",
        "",
        "选择偏好：",
        "优先选择 10～35 秒内的 micro_segment。",
        "22～35 秒属于偏长片段，只有在完整表态、连续现场动作、不可拆事实时才选择。",
        "不要因为片段长就自动丢弃，但不要选择多个信息混杂的长片段。",
        "同一事实重复出现时，优先选择画面更清楚、信息更完整的一段。",
        "",
    ]

    included = 0
    truncated = False

    for seg in micro_segments[:max_segments]:
        msid = str(seg.get("micro_segment_id") or "").strip()
        if not msid:
            continue

        block_lines = [
            f"[{msid}]",
            f"source={seg.get('source_id', '')}",
            f"duration={seg.get('duration_seconds', '')}",
        ]

        if seg.get("preselect_summary"):
            block_lines.append(f"预选摘要：{seg['preselect_summary']}")
        else:
            summary = str(seg.get("summary") or "").strip()
            if summary:
                block_lines.append(f"摘要：{summary}")

        speech = sanitize_llm_text(str(seg.get("asr_text") or seg.get("speech") or ""), max_chars=160)
        if speech:
            block_lines.append(f"短ASR：{speech}")

        visual = compact_text(str(seg.get("visual_summary") or seg.get("visual") or ""), max_chars=80)
        if visual:
            block_lines.append(f"短画面：{visual}")

        block = "\n".join(block_lines).strip()

        # 字符预算保护：预留一点尾部说明空间，避免刚好顶满
        candidate_text = "\n".join(lines + [block, ""]).strip()
        if len(candidate_text) > max_input_chars:
            truncated = True
            break

        lines.append(block)
        lines.append("")
        included += 1

    if truncated:
        lines.append("")
        lines.append("注意：后续 micro_segments 已因输入长度预算被截断，不能选择未出现在上文的 micro_segment_id。")

    lines.append("")
    lines.append(f"本次实际提供 micro_segment 数：{included}")

    return "\n".join(lines).strip()
```

关键点：

```text
1. 支持传入 micro_segments 子集。
2. 加入 group_id、source_ids，让模型知道当前只是局部分组。
3. 加入 max_selected_segments_per_group，避免每组选太多。
4. 增加 max_input_chars 字符预算截断。
5. 截断时明确告诉模型不能选择未出现的 micro_segment_id。
```

鲁棒性注意：

```text
不要输出 time。
不要提示 segment_id。
仍然只要求 micro_segment_id。
否则会触发当前代码里的协议校验。
```

---

### 6.5 改造 `step_content_analysis()`

当前 `step_content_analysis()` 是单次构造输入、单次调用模型。

建议改成：

```python
def step_content_analysis(self) -> None:
    cfg = self.config.raw.get("asr_micro_segment", {})
    use_micro_segments = cfg.get("enabled", True)

    if not use_micro_segments or self.manifest.get("steps", {}).get("asr_micro_segment", {}).get("status") != "success":
        raise UserFacingPipelineError(
            "content_analysis_requires_micro_segments",
            user_message="内容分析失败：AI 配音主链路必须先生成 micro_segments，不能回退到 chunk 分析。",
            suggestions=["回查 agents/asr_micro_segment/v*/micro_segments.json。", "从 asr_micro_segment 重新运行。"],
        )

    _base, _out_name, bound_prompt, prompt_version = AGENT_INFO["content_analysis"]
    if bound_prompt != prompts.CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT or prompt_version != "content_analysis_micro_segment_v1":
        raise UserFacingPipelineError(
            "content_analysis_prompt_binding_invalid",
            user_message="content_analysis 提示词绑定无效：AI 配音 micro_segment 链路不能使用旧 chunk/content_analysis prompt。",
            suggestions=["确认 AGENT_INFO['content_analysis'] 绑定 CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT。"],
            technical_detail={"prompt_version": prompt_version},
        )

    ca_cfg = self._content_analysis_cfg()
    all_segments = self._load_segments_for_content_analysis()
    groups = self._group_segments_for_content_analysis(all_segments, ca_cfg)

    group_results: list[dict[str, Any]] = []
    group_errors: list[dict[str, Any]] = []

    for group in groups:
        try:
            text_input = self._build_content_analysis_micro_segment_text(
                group["segments"],
                group_id=group["group_id"],
                group_index=group["group_index"],
                group_count=group["group_count"],
                source_ids=group["source_ids"],
                max_selected_segments=ca_cfg["max_selected_segments_per_group"],
                max_input_chars=ca_cfg["max_input_chars_per_group"],
            )

            legacy_segment_hint = "只输出 segment_id" in text_input or "[segment_id]" in text_input
            if legacy_segment_hint or "\ntime=" in text_input:
                raise UserFacingPipelineError(
                    "content_analysis_prompt_binding_invalid",
                    user_message="content_analysis 输入协议无效：不得向模型提示 segment_id 或 time 时间码。",
                    suggestions=["检查 _build_content_analysis_micro_segment_text() 是否只输出 micro_segment_id。"],
                    technical_detail={
                        "legacy_segment_hint": legacy_segment_hint,
                        "contains_time": "\ntime=" in text_input,
                        "group_id": group["group_id"],
                    },
                )

            result = self._run_text_agent("content_analysis", text_input)
            normalized = self._normalize_content_analysis_selected_segments(result)

            group_results.append({
                "group_id": group["group_id"],
                "group_index": group["group_index"],
                "source_ids": group["source_ids"],
                "input_segment_count": len(group["segments"]),
                "selected_segments": normalized.get("selected_segments", []),
                "diagnostics": normalized.get("diagnostics", {}),
                "raw_model_plan": normalized.get("raw_model_plan"),
            })

        except Exception as exc:
            group_errors.append({
                "group_id": group.get("group_id"),
                "group_index": group.get("group_index"),
                "source_ids": group.get("source_ids", []),
                "error": str(exc),
            })

            if not ca_cfg["allow_partial_group_success"]:
                raise

    result = self._merge_grouped_content_analysis_results(
        group_results=group_results,
        group_errors=group_errors,
        cfg=ca_cfg,
    )

    if not result.get("selected_segments"):
        if ca_cfg["fallback_to_single_pass_when_empty"] and len(groups) > 1:
            text_input = self._build_content_analysis_micro_segment_text(
                all_segments,
                group_id="fallback_all",
                group_index=1,
                group_count=1,
                source_ids=self._source_order_from_segments(all_segments),
                max_selected_segments=ca_cfg["max_final_selected_segments"],
                max_input_chars=int(self.config.raw.get("llm_input", {}).get("max_content_analysis_input_chars", 12000) or 12000),
            )
            fallback_result = self._run_text_agent("content_analysis", text_input)
            result = self._normalize_content_analysis_selected_segments(fallback_result)

        if not result.get("selected_segments"):
            raise UserFacingPipelineError(
                "content_analysis_selected_segments_empty",
                user_message="内容分析失败：模型没有选出任何可用于 AI 配音的 micro_segment。",
                suggestions=[
                    "回查 agents/content_analysis/v*/model_output.json。",
                    "检查 asr_micro_segment 的 ASR 文本和画面摘要是否有效。",
                    "如果是多视频任务，请检查 grouped_content_analysis 的 group_results 和 group_errors。",
                ],
                technical_detail={
                    "group_results": group_results,
                    "group_errors": group_errors,
                },
            )

    self._overwrite_step_json("content_analysis", result, materialized=True)
    self._write_compat_content_analysis(result)
```

说明：

```text
1. 保留原来的 micro_segment 前置校验。
2. 保留原来的 prompt 绑定校验。
3. 先从 _load_segments_for_content_analysis() 获取预筛后的候选。
4. 按 source 分组。
5. 每组单独构造 text_input。
6. 每组单独调用 _run_text_agent("content_analysis", text_input)。
7. 归一化每组结果。
8. 最后合并 selected_segments。
9. 输出结构仍写回 content_analysis。
```

---

### 6.6 新增分组合并函数

```python
def _merge_grouped_content_analysis_results(
    self,
    *,
    group_results: list[dict[str, Any]],
    group_errors: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    for group in sorted(group_results, key=lambda x: int(x.get("group_index") or 0)):
        count_in_group = 0
        for item in group.get("selected_segments") or []:
            if not isinstance(item, dict):
                continue
            msid = str(item.get("micro_segment_id") or "").strip()
            if not msid or msid in seen:
                continue

            clean_item = {
                "micro_segment_id": msid,
                "summary": str(item.get("summary") or "").strip(),
                "role": str(item.get("role") or "fact").strip() or "fact",
                "source_group_id": group.get("group_id", ""),
            }

            selected.append(clean_item)
            seen.add(msid)
            count_in_group += 1

            if count_in_group >= cfg["max_selected_segments_per_group"]:
                break

        if len(selected) >= cfg["max_final_selected_segments"]:
            break

    selected = selected[: cfg["max_final_selected_segments"]]

    return {
        "version": "ai_voiceover_content_analysis_v2_grouped",
        "input_unit": "micro_segment",
        "selected_segments": selected,
        "diagnostics": {
            "selected_count": len(selected),
            "group_count": len(group_results),
            "group_error_count": len(group_errors),
            "group_errors": group_errors,
            "max_selected_segments_per_group": cfg["max_selected_segments_per_group"],
            "max_final_selected_segments": cfg["max_final_selected_segments"],
        },
        "materialized_by_code": True,
        "grouped_content_analysis": True,
        "group_results": group_results,
    }
```

注意：

```text
1. 合并时要按 group_index 保持稳定顺序。
2. micro_segment_id 要去重。
3. 每组最多保留 max_selected_segments_per_group。
4. 全局最多保留 max_final_selected_segments。
5. 保留 group_results 方便调试。
```

---

## 7. 调试文件与日志建议

为了方便定位类似问题，建议在分组模式下额外记录每组输入大小。

如果 `_run_text_agent()` 已经会记录：

```text
LLM input size [content_analysis]: xxx chars
```

那么多组时日志会出现多条同名日志，不容易区分是哪个组。

建议新增一个轻量日志：

```python
print(
    f"LLM grouped input size [content_analysis][{group['group_id']}]: "
    f"{len(text_input)} chars, sources={group['source_ids']}, segments={len(group['segments'])}",
    flush=True,
)
```

也可以把每组输入写到版本目录下，但如果想最小改动，可以先只打印日志。

更完整的调试产物建议：

```text
agents/content_analysis/v*/groups/group_001/input.txt
agents/content_analysis/v*/groups/group_001/model_output.json
agents/content_analysis/v*/groups/group_001/raw_response.json
```

不过当前 `_run_text_agent("content_analysis", text_input)` 可能默认写到同一个 content_analysis 目录，是否支持不同 debug_dir 要看 `_run_text_agent()` 的实现。如果不方便改，可以先不做细分 debug 目录，只在最终 `content_analysis.json` 里保留 `group_results`。

---

## 8. 边界条件

### 8.1 source 数量小于等于 3

行为：

```text
保持旧逻辑。
所有候选合并终选。
```

原因：

```text
source 数量少时，合并有利于大模型做全局对比，也不容易超长。
```

### 8.2 source 数量大于 3

行为：

```text
按 max_sources_per_group 分组。
默认每 3 个 source 一组。
```

例如：

```text
4 个 source -> 2 组：3 + 1
8 个 source -> 3 组：3 + 3 + 2
10 个 source -> 4 组：3 + 3 + 3 + 1
```

### 8.3 某个 source 没有候选 segment

行为：

```text
该 source 不应该导致失败。
分组函数会自然跳过空 segment。
```

如果某组最终没有有效 segments：

```text
可以跳过该组。
不要构造空输入调用模型。
```

建议补充：

```python
groups = [g for g in groups if g.get("segments")]
```

### 8.4 某组输入仍然过长

靠两层保护：

```text
max_segments_per_group
max_input_chars_per_group
```

如果某组单个 segment 就很长，仍然通过 `短ASR max_chars=160`、`短画面 max_chars=80` 控制。

如果拼接达到预算：

```text
停止追加后续 segment
在输入末尾提示：未出现的 micro_segment_id 不能选择
```

### 8.5 某组大模型失败

建议配置：

```toml
allow_partial_group_success = true
```

行为：

```text
记录 group_errors
其他组继续
只要最终 selected_segments 非空，就继续后续流程
```

如果所有组都失败或都没有选出内容：

```text
抛 UserFacingPipelineError
提示查看 group_results / group_errors
```

### 8.6 模型返回了未知 micro_segment_id

当前 `_normalize_content_analysis_selected_segments()` 会基于完整 `asr_micro_segment` 做校验：

```text
不在 seg_map 里的 id 会进入 invalid_items
不会进入 selected_segments
```

所以保持这个函数即可。

但分组模式下还有一个细节：

```text
模型理论上只能选当前组出现的 id。
如果它输出了其他组的 id，_normalize_content_analysis_selected_segments() 可能仍然认为它是合法的，因为它存在于全局 seg_map。
```

为了更严格，可以新增组内过滤：

```python
allowed_ids = {
    str(seg.get("micro_segment_id") or "").strip()
    for seg in group["segments"]
}

selected = [
    item for item in normalized.get("selected_segments", [])
    if str(item.get("micro_segment_id") or "").strip() in allowed_ids
]
```

建议在 `step_content_analysis()` 每组归一化后加这个过滤，避免跨组污染。

### 8.7 分组汇总后 selected_segments 太多

必须全局截断：

```text
max_final_selected_segments = 20
```

否则后续：

```text
ai_voiceover_candidate_materialize
short_video_edit_plan
voiceover_script
tts
subtitles
render
```

都会变重。

### 8.8 只跑失败步骤时的缓存问题

修改配置或代码后，建议重跑：

```text
从 content_analysis_preselect 开始重跑
```

如果只改 `content_analysis` 分组逻辑，也可以从：

```text
content_analysis
```

开始重跑。

但如果新增配置影响了 `content_analysis_preselect` 的候选数量，例如改了 `large_max_final_candidates`，就应该从：

```text
content_analysis_preselect
```

开始重跑。

---

## 9. 推荐重跑策略

### 9.1 只改代码，不改预筛配置

可以从失败步骤重跑：

```bash
python run_pipeline.py --task-id <任务ID> --rerun-from content_analysis
```

或者在 Web 页面点击：

```text
从此步后续
```

从 `content_analysis` 开始。

### 9.2 改了 `content_analysis_preselect` 配置

例如改了：

```toml
large_max_final_candidates = 24
```

建议从：

```bash
python run_pipeline.py --task-id <任务ID> --rerun-from content_analysis_preselect
```

这样能重新生成预筛结果，再进入新的分组 content_analysis。

---

## 10. 验收标准

### 10.1 8 个视频不再因为 content_analysis 输入超限失败

日志应该从：

```text
LLM input size [content_analysis]: 13013 chars
exceeds limit=12000
```

变成类似：

```text
LLM grouped input size [content_analysis][group_001]: 6500 chars
LLM grouped input size [content_analysis][group_002]: 7100 chars
LLM grouped input size [content_analysis][group_003]: 4800 chars
完成: content_analysis
```

### 10.2 输出结构兼容

`agents/content_analysis/v*/content_analysis.json` 中必须有：

```json
{
  "selected_segments": [...]
}
```

并且每个 item 必须包含：

```json
{
  "micro_segment_id": "...",
  "summary": "...",
  "role": "fact"
}
```

### 10.3 后续步骤不需要大改

以下步骤应继续正常读取：

```text
ai_voiceover_candidate_materialize
short_video_edit_plan
voiceover_script
tts
subtitles
cut_plan/render
```

### 10.4 空结果能明确报错

如果分组都没有选出内容，报错应该是用户可理解的：

```text
内容分析失败：模型没有选出任何可用于 AI 配音的 micro_segment。
```

并且 technical_detail 里包含：

```text
group_results
group_errors
```

方便排查是模型没选，还是某组调用失败。

---

## 11. 最小可行改动总结

这次不需要大改整体架构，只需要做四件事：

```text
1. config.toml 新增 [content_analysis] 配置。
2. _build_content_analysis_micro_segment_text() 支持传入 segment 子集，并增加字符预算截断。
3. 新增 _group_segments_for_content_analysis()，source > 3 时每 3 个 source 一组。
4. 改 step_content_analysis()，从单次全量调用改成分组调用 + 汇总 selected_segments。
```

最终效果：

```text
前面每个视频仍然独立分析。
content_analysis_preselect 仍然保留。
content_analysis 不再把所有视频候选一次性输入。
多视频任务按 source 分组终选。
最终 selected_segments 汇总给后续链路。
```

这样既能解决 8 个视频时输入超限问题，也不会破坏当前 AI 配音主链路。
