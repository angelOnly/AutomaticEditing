# AI 配音 micro_segment 链路代码 Review 后补充开发方案

> 目标：针对 `clean-highlight-reassembly` 分支当前代码 review 暴露的问题，给出可直接交给开发执行的代码修改方案。
>
> 范围：只补强 AI 配音 micro_segment 主链路，不重新设计整体架构，不修改视频重组链路。
>
> 主入口：`run_web.py`
>
> 主链路：Web / unified source pipeline / `("ai_voiceover", True)`

---

## 0. 本次补充修改总览

当前代码已经完成了大部分核心链路：

```text
source_aggregate
  -> source_raw_asr_index
  -> asr_micro_segment
  -> content_analysis
  -> ai_voiceover_candidate_materialize
  -> short_video_edit_plan
  -> voiceover_script
  -> tts
  -> cut_plan
```

但仍有几个需要继续补强的地方：

```text
1. asr_micro_segment 仍然直接读 source_aggregate.json 内嵌 raw_asr_index，未把独立 source_raw_asr_index.json 作为唯一主输入。
2. legacy timeline_digest fallback 函数仍保留，配置误开或被直接调用后会退回旧链路。
3. asr_micro_segment input_hash 没有显式绑定 raw_asr_index 文件、visual_context hash 和 prompt version。
4. source_aggregate 的 ASR/chunk 映射 diagnostics 信息不足。
5. content_analysis 输入仍带 source 字段，建议进一步瘦身。
6. 旧的 _materialize_content_analysis_candidates_from_segments() 仍可被误用。
7. ai_voiceover_candidate_materialize 对 overlong candidate 默认不失败，生产验收应按 P0 处理。
8. tts / cut_plan 需要继续确认并补强空结果 fail-fast。
9. workflow 已声明 ai_voiceover_candidate_materialize，但必须继续验收 pipeline.py 中对应 step handler、输出文件和 fail-fast 行为是否真实完整。
10. run_web.py 端口探测建议修正 0.0.0.0 探测问题。
11. config.toml 如包含真实 API key，作为提交前安全项处理；它不作为本轮 micro_segment 链路是否合格的阻断项。
```

本方案按优先级分为：

```text
P0：必须改，否则可能重新出现旧链路或缓存错误。
P1：强烈建议改，否则调试和验收不稳定。
P2：体验和维护性优化。
```

## 0.1 当前代码事实校准

本节只用于防止重复开发，不改变原方案方向。

当前分支里已经落地的部分：

```text
1. AGENT_INFO["content_analysis"] 已绑定 CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT。
2. ("ai_voiceover", True) workflow 已加入 ai_voiceover_candidate_materialize，且 short_video_edit_plan 依赖已经指向该步骤。
3. workflow 声明已接入不等于 handler 和输出协议完全合格，仍必须验收 pipeline.py 中是否存在 step_ai_voiceover_candidate_materialize()，以及它是否真实写出 ai_voiceover_candidates/v*/candidate_clips.json 与 materialize_debug.json。
4. short_video_edit_plan 已有业务语义重试雏形，能识别空 videos / 无效 clip_id。
5. voiceover_script / tts 已有部分空结果 fail-fast 逻辑。
```

但当前仍需修正的部分：

```text
1. asr_micro_segment 仍读取 source_aggregate.json 内嵌 raw_asr_index，而不是独立 source_raw_asr_index.json。
2. asr_micro_segment input_hash 仍没有精确绑定 raw_asr_index 文件 hash、visual_context hash 和 AGENT_INFO prompt_version。
3. _build_asr_sentence_units_legacy_from_timeline_digest() 函数内部缺少硬保护，不能只依赖调用方 compat 判断。
4. source_aggregate ASR/chunk diagnostics 仍只能说明 no overlap，缺少 nearest_chunk_id / nearest_distance_seconds。
5. ai_voiceover_candidate_materialize 的 fail_on_overlong 当前默认仍可能为 false，生产验收需要改成 true。
6. ai_voiceover_candidate_materialize 的 overlong / short / empty 阻断判断必须发生在记录 success 之前，避免 manifest 与 status.json 状态不一致。
7. config.toml 中如存在真实 API key，提交公开仓库前必须移出并轮换；但这属于提交安全项，不作为本轮 micro_segment 主链路验收的阻断项。
8. ("ai_voiceover", False) legacy 单源 workflow 仍然存在，误入该 workflow 时不能静默 fallback 到 timeline_digest 或旧 chunk candidate。
```

本轮修改的核心不是新增另一套链路，而是把当前已经半落地的 micro_segment 链路收敛成唯一主链路：

```text
source_raw_asr_index.json 独立文件为唯一 ASR 时间来源；
content_analysis 只选择 micro_segment；
candidate_clips 只由代码物化；
下游只读取 ai_voiceover_candidate_materialize 输出；
所有空业务结果必须 fail-fast。
```

---

## 0.2 本轮 review 后的微调补充

本节只补充代码事实校准和验收红线，不改变原方案结构。

```text
1. ai_voiceover_candidate_materialize 在 unified workflow 中已经声明接入，因此文档后续不再把它当成“纯新增 step”，而是按“已接入但必须验收 handler / 输出 / fail-fast”处理。
2. workflow_registry.py 里出现 step 名不代表 pipeline.py 已有对应 handler；必须确认存在 step_ai_voiceover_candidate_materialize()，否则运行到该步骤会直接失败。
3. asr_micro_segment 的输入协议已经变化为独立 source_raw_asr_index.json，即使 prompt 文案没有大改，也必须升级 prompt_version / cache version，避免旧缓存误复用。
4. legacy 单源 workflow 仍可能被直接 CLI、旧 task、rerun_from、source_manifest 缺失等场景误入；误入时应 fail-fast，不能为了跑通恢复 timeline_digest fallback。
5. aggregate["raw_asr_index"] 如果暂时保留，只能用于 debug / 旧产物兼容；asr_micro_segment 主路径必须禁止读取。
6. content_analysis.json 中如果仍出现 candidate_clips，默认视为旧链路污染；除 raw_model_plan/debug 字段外，不允许作为下游输入。
7. config.toml API key 清理是提交前安全提醒，不纳入本轮链路 P0 阻断，避免干扰 micro_segment 主链路验收。
```

---

# P0-1. asr_micro_segment 必须读取 source_raw_asr_index.json 独立文件

## 当前问题

当前 `step_source_aggregate()` 已经写出了：

```text
source_aggregate/v*/source_raw_asr_index.json
```

并且在 `source_aggregate.json` 里写了：

```json
{
  "raw_asr_index_file": "source_aggregate/v*/source_raw_asr_index.json",
  "raw_asr_index": {...}
}
```

但 `step_asr_micro_segment()` 当前仍通过 `_build_asr_sentence_units()` 读取 `source_aggregate.json` 内嵌的 `raw_asr_index`。

这里需要把独立 `source_raw_asr_index.json` 从“旁路产物”升级为 asr_micro_segment 的唯一主输入。`source_aggregate.json` 里的内嵌 `raw_asr_index` 最多只能作为旧产物兼容，不应继续作为新版读取主路径。

这会导致：

```text
1. 独立文件 source_raw_asr_index.json 失去主数据源地位。
2. 如果 raw_asr_index 文件被修复或重跑，但 aggregate 内嵌副本未同步，asr_micro_segment 可能读旧数据。
3. source_aggregate.json 会继续膨胀，不利于后续大文件维护。
```

补充执行边界：

```text
推荐最终从 source_aggregate.json 删除内嵌 raw_asr_index，只保留 raw_asr_index_file、raw_asr_segment_count、raw_asr_diagnostics_count。

如果为了旧产物兼容暂时保留 aggregate["raw_asr_index"]，也必须明确：
1. asr_micro_segment 禁止读取 aggregate["raw_asr_index"]。
2. aggregate["raw_asr_index"] 只能作为 debug / 旧产物兼容字段。
3. 新版主链路唯一读取入口是 source_aggregate/v*/source_raw_asr_index.json。

这样可以避免“独立文件 + 内嵌副本”长期并存后出现数据不同步。
```

## 修改文件

```text
newsclip_agent/pipeline.py
```

## 具体修改 1：新增 `_load_source_raw_asr_index()`

建议放在 `_load_current_timeline_digest()` 附近。

新增函数：

```python
def _load_source_raw_asr_index(self) -> dict[str, Any]:
    """Load raw ASR index for AI voiceover micro-segmentation.

    主路径只允许从 source_aggregate 写出的 source_raw_asr_index.json 读取。
    找不到时 fail-fast，不回退 source_aggregate.json 内嵌副本，也不回退 timeline_digest.speech。
    """
    aggregate = self._load_optional_step_json("source_aggregate", {})

    rel = str(aggregate.get("raw_asr_index_file") or "").strip()
    if rel:
        path = self.task_dir / rel
        doc = read_json(path, {})
        if isinstance(doc, dict) and isinstance(doc.get("sources"), list):
            return doc

    # 兼容旧产物：如果 source_aggregate.json 旁边已经有 source_raw_asr_index.json，读取它。
    agg_output = self._step_output("source_aggregate")
    if agg_output:
        candidate = (self.task_dir / agg_output).parent / "source_raw_asr_index.json"
        doc = read_json(candidate, {})
        if isinstance(doc, dict) and isinstance(doc.get("sources"), list):
            return doc

    raise UserFacingPipelineError(
        "source_raw_asr_index_missing",
        user_message="AI 配音微片段生成失败：source_raw_asr_index.json 不存在或格式无效。",
        suggestions=[
            "请从 source_aggregate 重新运行。",
            "检查 source_aggregate/v*/source_raw_asr_index.json 是否生成。",
            "确认当前任务走的是统一多源链路 source_prepare -> source_analysis -> source_aggregate。",
        ],
        technical_detail={
            "source_aggregate_output": self._step_output("source_aggregate"),
            "raw_asr_index_file": rel,
        },
    )
```

## 具体修改 2：新增 `_source_raw_asr_index_hash()`

建议放在 `_current_timeline_digest_hash()` 附近。

```python
def _source_raw_asr_index_hash(self) -> str:
    aggregate = self._load_optional_step_json("source_aggregate", {})

    rel = str(aggregate.get("raw_asr_index_file") or "").strip()
    if rel:
        path = self.task_dir / rel
        if path.exists():
            return stable_hash(read_json(path, {}))

    agg_output = self._step_output("source_aggregate")
    if agg_output:
        candidate = (self.task_dir / agg_output).parent / "source_raw_asr_index.json"
        if candidate.exists():
            return stable_hash(read_json(candidate, {}))

    return ""
```

## 具体修改 3：把 `_build_asr_sentence_units()` 改成使用独立 raw index

当前函数大致是：

```python
def _build_asr_sentence_units(self) -> list[dict[str, Any]]:
    if not self._is_virtual_source_manifest():
        compat = self.config.raw.get("compat", {})
        if not compat.get("allow_asr_micro_segment_from_timeline_digest", False):
            return []
        return self._build_asr_sentence_units_legacy_from_timeline_digest()

    aggregate = self._load_optional_step_json("source_aggregate", {})
    raw_index = aggregate.get("raw_asr_index") if isinstance(aggregate, dict) else {}
    ...
```

替换为：

```python
def _build_asr_sentence_units(self) -> list[dict[str, Any]]:
    if not self._is_virtual_source_manifest():
        compat = self.config.raw.get("compat", {})
        if not bool(compat.get("allow_legacy_single_source_raw_asr_runtime_build", False)):
            raise UserFacingPipelineError(
                "asr_micro_segment_requires_unified_source_pipeline",
                user_message="ASR 微分段失败：AI 配音主链路必须走统一多源 source_aggregate，不能从 timeline_digest 反推时间。",
                suggestions=[
                    "请从 Web 入口或 run_multisource_pipeline.py 运行。",
                    "如果要支持 CLI 单视频，请先把单视频包装进 source_prepare/source_analysis/source_aggregate。",
                ],
                technical_detail={
                    "source_mode": self.manifest.get("source_mode"),
                    "source_manifest": self.manifest.get("source_manifest"),
                },
            )
        return self._build_asr_sentence_units_legacy_from_runtime_raw_asr()

    raw_index = self._load_source_raw_asr_index()
    raw_sources = raw_index.get("sources") if isinstance(raw_index, dict) else []
    if not isinstance(raw_sources, list):
        raw_sources = []

    sentences: list[dict[str, Any]] = []
    cfg = self.config.raw.get("asr_micro_segment", {})
    max_chars = int(cfg.get("max_sentence_chars", 120) or 120)

    for source in raw_sources:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id") or "source_1").strip() or "source_1"
        source_index = source.get("source_index") or 1
        raw_segments = [s for s in source.get("asr_segments") or [] if isinstance(s, dict)]
        raw_segments.sort(key=lambda s: float(s.get("start_seconds") or 0))

        for seg in raw_segments:
            start = self._time_value_seconds(seg.get("start_seconds"))
            end = self._time_value_seconds(seg.get("end_seconds"))
            if start is None or end is None or end <= start:
                continue

            text = str(seg.get("normalized_text") or seg.get("text") or seg.get("raw_text") or "").strip()
            if not text:
                continue

            parts = [text]
            if len(text) > max_chars:
                parts = self._split_long_asr_text_for_sentence_units(text, max_chars=max_chars)

            total_chars = sum(len(p) for p in parts) or len(text)
            cursor = float(start)
            for part in parts:
                part = str(part or "").strip()
                if not part:
                    continue
                part_duration = (float(end) - float(start)) * (len(part) / total_chars) if total_chars else 0
                part_start = cursor
                part_end = min(float(end), cursor + part_duration)
                cursor = part_end

                sentences.append({
                    "sentence_id": f"s{len(sentences) + 1:04d}",
                    "asr_segment_id": seg.get("asr_segment_id", ""),
                    "source_raw_asr_segment_id": seg.get("source_raw_asr_segment_id", ""),
                    "source_id": source_id,
                    "source_index": source_index,
                    "start_seconds": round(part_start, 3),
                    "end_seconds": round(part_end, 3),
                    "text": part,
                    "raw_text": seg.get("raw_text", ""),
                    "source_chunk_ids": seg.get("source_chunk_ids", []),
                    "primary_chunk_id": seg.get("primary_chunk_id", ""),
                })

    sentences.sort(key=lambda item: (
        str(item.get("source_id") or ""),
        float(item.get("start_seconds") or 0),
        float(item.get("end_seconds") or 0),
    ))
    for index, sentence in enumerate(sentences, start=1):
        sentence["sentence_id"] = f"s{index:04d}"
    return sentences
```

## 具体修改 4：新增 `_split_long_asr_text_for_sentence_units()`

如果当前已有类似函数，可以复用；没有就新增。

```python
def _split_long_asr_text_for_sentence_units(self, text: str, *, max_chars: int) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    parts = re.split(r"([。！？；.!?;])", text)
    units: list[str] = []
    current = ""

    for i in range(0, len(parts), 2):
        frag = parts[i]
        punct = parts[i + 1] if i + 1 < len(parts) else ""
        candidate = (current + frag + punct).strip()
        if current and len(candidate) > max_chars:
            units.append(current.strip())
            current = (frag + punct).strip()
        else:
            current = candidate

    if current:
        units.append(current.strip())

    # 如果没有标点导致仍然超长，按 max_chars 硬切，但只在单条 raw ASR 内部切。
    final: list[str] = []
    for unit in units or [text]:
        if len(unit) <= max_chars:
            final.append(unit)
        else:
            for start in range(0, len(unit), max_chars):
                final.append(unit[start:start + max_chars].strip())
    return [x for x in final if x]
```

## 验收标准

运行后检查：

```text
source_aggregate/v*/source_raw_asr_index.json 存在
agents/asr_micro_segment/v*/asr_sentences.json 存在
asr_sentences.json 的句子来自 raw_asr_index，而不是 timeline_digest.speech
```

检查失败场景：

```text
删除 source_raw_asr_index.json 后重跑 asr_micro_segment，应 fail-fast。
不能静默 fallback 到 timeline_digest。
```

额外代码搜索验收：

```text
全仓搜索 aggregate.get("raw_asr_index") / ["raw_asr_index"]。
除 source_aggregate 写入、debug 展示、旧产物兼容读取外，asr_micro_segment 主路径不得读取该字段。
如果仍发现 _build_asr_sentence_units() 直接读取 source_aggregate.json 内嵌 raw_asr_index，视为本项未完成。
```

---

# P0-2. 禁止 timeline_digest fallback 重新进入主链路

## 当前问题

当前代码中仍存在：

```python
_build_asr_sentence_units_legacy_from_timeline_digest()
```

这会从 `timeline_digest.speech` 或 `asr_text` 按字数比例分配时间。

这正是本轮架构要删除的旧问题。

## 修改文件

```text
newsclip_agent/pipeline.py
config.toml
```

## 具体修改 1：保留函数但默认不可用

不能只在 `_build_asr_sentence_units()` 调用方判断 compat。`_build_asr_sentence_units_legacy_from_timeline_digest()` 函数内部也必须二次检查 compat，防止未来其他调用绕过保护。

将 `_build_asr_sentence_units_legacy_from_timeline_digest()` 函数头部加硬保护：

```python
def _build_asr_sentence_units_legacy_from_timeline_digest(self) -> list[dict[str, Any]]:
    compat = self.config.raw.get("compat", {})
    if not bool(compat.get("allow_asr_micro_segment_from_timeline_digest", False)):
        raise UserFacingPipelineError(
            "timeline_digest_micro_segment_fallback_disabled",
            user_message="ASR 微分段失败：禁止从 timeline_digest.speech 反推 micro_segment 时间。",
            suggestions=[
                "请走 source_prepare -> source_analysis -> source_aggregate 统一链路。",
                "检查 source_aggregate 是否生成 source_raw_asr_index.json。",
            ],
        )

    # 只有显式 compat 开关打开时才允许继续旧逻辑。
    ...
```

## 具体修改 2：如果 compat 被打开，必须写明显 degraded 标记

在旧函数返回 sentences 前给每个 sentence 加：

```python
sentence["degraded_time_source"] = "timeline_digest_text_ratio"
sentence["warning"] = "legacy fallback: time was estimated from digest text length"
```

并在 `step_asr_micro_segment()` 输出里加：

```python
legacy_degraded = any(s.get("degraded_time_source") for s in sentences)
output = {
    "version": "asr_micro_segment_raw_asr_v2",
    "segments": segments,
    "diagnostics": group_diagnostics,
    "legacy_degraded": legacy_degraded,
}
```

如果 `legacy_degraded=True`，建议直接失败，除非配置显式允许：

```python
if legacy_degraded and not bool(cfg.get("allow_degraded_timeline_fallback_output", False)):
    raise UserFacingPipelineError(
        "asr_micro_segment_degraded_timeline_fallback",
        user_message="ASR 微分段失败：当前输出来自 timeline_digest 字数估时，不允许进入 AI 配音主链路。",
        suggestions=["请修复 source_raw_asr_index.json 后重跑。"],
    )
```

## 具体修改 3：config.toml 默认关闭 compat

在 `config.toml` 中确认或新增：

```toml
[compat]
allow_asr_micro_segment_from_timeline_digest = false
allow_legacy_single_source_raw_asr_runtime_build = false
```

## 验收标准

```text
1. Web 统一链路中，不会出现 degraded_time_source。
2. CLI 单源误入 legacy workflow 时，不会静默成功，而是提示必须走统一 source_aggregate。
3. 全仓搜索 allow_asr_micro_segment_from_timeline_digest，只能在 compat 明确保护逻辑里出现。
4. 直接运行 run_pipeline.py --input xxx.mp4 或复用旧 task 误入 ("ai_voiceover", False) 时，不允许退回 timeline_digest.speech 或旧 chunk candidate；应提示必须走 unified source pipeline。
```

---

# P0-3. 修正 asr_micro_segment input_hash

## 当前问题

当前 `step_asr_micro_segment()` 的 input_hash 使用：

```python
input_hash = stable_hash({
    "source_aggregate": self._step_content_hash("source_aggregate") if "source_aggregate" in self.manifest.get("steps", {}) else None,
    "asr_micro_segment_cfg": cfg,
    "prompt_version": 1,
})
```

问题：

```text
1. prompt_version 写死为 1。
2. 没有直接 hash source_raw_asr_index.json。
3. 没有明确纳入视觉上下文 hash。
4. 修改 raw_asr_index 文件后，可能继续复用旧 micro_segments.json。
```

## 修改文件

```text
newsclip_agent/pipeline.py
```

## 具体修改

这是缓存正确性的阻断项，不是普通优化。raw_asr_index 文件内容、visual_context、asr_micro_segment 配置、prompt_version 任一变化，都必须让 asr_micro_segment 缓存失效。

替换为：

```python
_base, _out_name, _prompt, prompt_version = AGENT_INFO["asr_micro_segment"]
input_hash = stable_hash({
    "source_raw_asr_index": self._source_raw_asr_index_hash(),
    "visual_context": self._current_timeline_digest_hash(),
    "asr_micro_segment_cfg": cfg,
    "prompt_version": prompt_version,
})
```

同时建议同步升级 AGENT_INFO 里的版本号：

```python
"asr_micro_segment": (
    "agents/asr_micro_segment",
    "micro_segments.json",
    prompts.ASR_MICRO_SEGMENT_PROMPT,
    "asr_micro_segment_raw_asr_v2",
)
```

原因：本次输入协议已经从 timeline_digest / aggregate 内嵌 raw_asr_index 收敛为独立 `source_raw_asr_index.json`，属于缓存语义变化。即使 prompt 文案没有大改，prompt_version / cache version 也不应继续停留在 `asr_micro_segment_v1`。

补充要求：

```text
这不是普通命名优化，而是缓存正确性要求。
只要 raw_asr_index、visual_context、asr_micro_segment_cfg、prompt_version 任一变化，都必须导致 asr_micro_segment 重新生成。
```

如果担心旧单源没有 `source_raw_asr_index`，不要 fallback，而是让 P0-1 的 `_load_source_raw_asr_index()` fail-fast。

## 验收标准

```text
1. 修改 source_raw_asr_index.json 内容后，asr_micro_segment 不能复用旧缓存。
2. 修改 ASR micro segment prompt version 后，asr_micro_segment 不能复用旧缓存。
3. 修改 timeline_digest 视觉摘要后，如果 include_visual_summary=true，asr_micro_segment 不能复用旧缓存。
```

---


# P0-4. 提交前清理 config.toml 中的真实 API key（安全项，不阻断本轮链路验收）

## 当前问题

当前仓库配置文件中可能直接包含真实大模型 API key。即使这不是 micro_segment 链路 bug 的根因，也属于提交和部署前必须处理的安全问题。

补充边界：

```text
本项是提交公开仓库前的安全提醒，不作为本轮 micro_segment 主链路是否合格的阻断项。
本轮 P0 链路阻断仍以 raw_asr_index 主输入、fallback 禁用、input_hash、handler 完整性和 fail-fast 为主。
```

这会导致：

```text
1. 仓库公开后 API key 泄露。
2. key 被滥用产生费用或触发风控。
3. 后续 docker / 服务器部署时很难区分示例配置和真实配置。
```

## 修改文件

```text
config.toml
config.example.toml
.gitignore
README 或部署说明
```

## 具体修改

建议处理方式：

```text
1. config.toml 不提交真实 key，只保留本地私有配置。
2. 新增 config.example.toml，里面只放占位符。
3. 代码读取 API key 时优先支持环境变量，例如 DOUBAO_API_KEY、DASHSCOPE_API_KEY。
4. .gitignore 加入本地私有配置文件，例如 config.local.toml、.env。
5. 已经进入公开仓库或共享仓库的 key 必须立即轮换。
```

补充要求：

```text
如果这些 key 已经推送到公开 GitHub，即使后续 commit 删除，也必须视为已泄露。
必须在火山方舟、DashScope 等对应平台立即轮换旧 key。
不能只依赖删除 config.toml 或改写历史来解决。
```

示例：

```toml
[llm]
text_doubao_api_key = "${DOUBAO_API_KEY}"
vision_doubao_api_key = "${DOUBAO_API_KEY}"
text_openai_api_key = "${DASHSCOPE_API_KEY}"
vision_openai_api_key = "${DASHSCOPE_API_KEY}"
```

## 验收标准

```text
1. 全仓搜索 sk-、api_key、doubao_api_key，不应出现真实 key。
2. config.example.toml 可以提交；真实 config.toml / config.local.toml 不提交。
3. 本地通过环境变量或私有配置仍能正常启动 Web 和跑任务。
4. 已泄露过的 key 完成轮换。
```

---
# P1-1. 补强 source_aggregate 的 ASR/chunk 映射 diagnostics

## 当前问题

当前 `_load_child_source_asr_segments()` 能记录 no overlap，但没有记录最近 chunk。

当 ASR 时间和 chunk 时间错位时，只知道没匹配，不知道偏差多少。

## 修改文件

```text
newsclip_agent/pipeline.py
```

## 具体修改 1：新增 `_map_asr_segment_to_source_chunks()`

建议替换当前 `_source_chunk_ids_for_time_range()` 的用途，或者保留旧函数，新增更完整函数。

```python
def _map_asr_segment_to_source_chunks(
    self,
    *,
    source_id: str,
    asr_segment_id: str,
    asr_start: float,
    asr_end: float,
    chunks: list[dict[str, Any]],
) -> tuple[str, list[str], list[dict[str, Any]]]:
    overlaps: list[tuple[float, dict[str, Any]]] = []
    diagnostics: list[dict[str, Any]] = []

    for chunk in chunks:
        if str(chunk.get("source_id") or "") != str(source_id):
            continue
        c_start = self._time_value_seconds(chunk.get("local_start_seconds") or chunk.get("start_seconds"))
        c_end = self._time_value_seconds(chunk.get("local_end_seconds") or chunk.get("end_seconds"))
        if c_start is None or c_end is None or c_end <= c_start:
            continue
        overlap = max(0.0, min(asr_end, c_end) - max(asr_start, c_start))
        if overlap > 0:
            overlaps.append((overlap, chunk))

    if overlaps:
        overlaps.sort(key=lambda x: x[0], reverse=True)
        ids: list[str] = []
        for _overlap, chunk in overlaps:
            cid = str(chunk.get("chunk_id") or chunk.get("global_chunk_id") or "").strip()
            if cid and cid not in ids:
                ids.append(cid)
        return (ids[0] if ids else ""), ids, diagnostics

    center = (asr_start + asr_end) / 2
    nearest: tuple[float, dict[str, Any]] | None = None
    for chunk in chunks:
        if str(chunk.get("source_id") or "") != str(source_id):
            continue
        c_start = self._time_value_seconds(chunk.get("local_start_seconds") or chunk.get("start_seconds"))
        c_end = self._time_value_seconds(chunk.get("local_end_seconds") or chunk.get("end_seconds"))
        if c_start is None or c_end is None:
            continue
        distance = min(abs(center - c_start), abs(center - c_end))
        if nearest is None or distance < nearest[0]:
            nearest = (distance, chunk)

    diagnostics.append({
        "reason": "asr_chunk_no_overlap",
        "source_id": source_id,
        "asr_segment_id": asr_segment_id,
        "asr_start_seconds": round(asr_start, 3),
        "asr_end_seconds": round(asr_end, 3),
        "nearest_chunk_id": (
            nearest[1].get("chunk_id") or nearest[1].get("global_chunk_id")
            if nearest else ""
        ),
        "nearest_distance_seconds": round(nearest[0], 3) if nearest else None,
    })
    return "", [], diagnostics
```

## 具体修改 2：在 `_load_child_source_asr_segments()` 中替换映射逻辑

当前：

```python
primary_chunk_id, source_chunk_ids = self._source_chunk_ids_for_time_range(...)
chunk_diagnostics = []
if not source_chunk_ids:
    chunk_diagnostics.append(...)
```

替换为：

```python
asr_segment_id = f"{source_id}_asr_{idx:06d}"
primary_chunk_id, source_chunk_ids, chunk_diagnostics = self._map_asr_segment_to_source_chunks(
    source_id=source_id,
    asr_segment_id=asr_segment_id,
    asr_start=float(start),
    asr_end=float(end),
    chunks=chunks,
)
```

并复用 `asr_segment_id`：

```python
out.append({
    "asr_segment_id": asr_segment_id,
    ...
    "primary_chunk_id": primary_chunk_id,
    "source_chunk_ids": source_chunk_ids,
    "diagnostics": chunk_diagnostics,
})
```

## 验收标准

构造一个 ASR 时间超出 chunk 范围的样例，检查：

```text
source_raw_asr_index.json.diagnostics[] 中有：
- reason=asr_chunk_no_overlap
- nearest_chunk_id
- nearest_distance_seconds
```

---

# P1-2. content_analysis 输入继续瘦身，删除 source 字段

## 当前问题

当前 `_build_content_analysis_micro_segment_text()` 给模型输入了：

```text
source=...
duration=...
声：...
画：...
摘要：...
```

`duration` 可以保留，但 `source` 容易让 content_analysis 过早关注来源，而不是只判断事实段是否适合 AI 配音。

## 修改文件

```text
newsclip_agent/pipeline.py
```

## 具体修改

当前片段：

```python
lines.append(f"source={seg.get('source_id', '')}")
lines.append(f"duration={seg.get('duration_seconds', '')}")
lines.append(f"声：{seg['asr_text']}")
if seg.get("visual_summary"):
    lines.append(f"画：{seg['visual_summary']}")
if seg.get("summary"):
    lines.append(f"摘要：{seg['summary']}")
```

建议替换为：

```python
lines.append(f"duration={seg.get('duration_seconds', '')}")
lines.append(f"声：{compact_text(str(seg.get('asr_text') or ''), max_chars=500)}")
visual = str(seg.get("visual_context") or seg.get("visual") or seg.get("visual_summary") or "").strip()
if visual:
    lines.append(f"画：{compact_text(visual, max_chars=300)}")
```

同时删除：

```python
if seg.get("summary"):
    lines.append(f"摘要：{seg['summary']}")
```

理由：

```text
micro_segment 阶段不应该提前塞 summary。
content_analysis 的 summary 应该由模型根据声画重新概括。
```

## 保留现有协议检查

继续保留：

```python
legacy_segment_hint = "只输出 segment_id" in text_input or "[segment_id]" in text_input
if legacy_segment_hint or "\ntime=" in text_input:
    raise UserFacingPipelineError(...)
```

再加一个检查，避免 chunk_id 泄漏：

```python
contains_chunk_id_hint = "chunk_id=" in text_input or "source_chunk_ids" in text_input
if contains_chunk_id_hint:
    raise UserFacingPipelineError(
        "content_analysis_input_contains_chunk_id",
        user_message="content_analysis 输入协议无效：不得向模型暴露 chunk_id。",
        suggestions=["检查 _build_content_analysis_micro_segment_text()。"],
    )
```

## 验收标准

检查：

```text
agents/content_analysis/v*/input.txt
```

不应出现：

```text
source=
time=
chunk_id
source_chunk_ids
segment_id
```

可以出现：

```text
[micro_segment_id]
duration=
声：
画：
```

---

# P1-3. 废弃旧 candidate 物化函数，防止误调用

## 当前问题

当前代码里仍有：

```python
_materialize_content_analysis_candidates_from_segments()
```

它会把 segment 直接物化成 candidate_clips，和当前原则冲突：

```text
AI 配音 candidate_clips 的唯一生成点应该是 step_ai_voiceover_candidate_materialize。
```

## 修改文件

```text
newsclip_agent/pipeline.py
```

## 具体修改方案 A：直接改为 hard deprecated

把函数内容整体替换为：

```python
def _materialize_content_analysis_candidates_from_segments(self, result: Any) -> dict[str, Any]:
    raise RuntimeError(
        "Deprecated: content_analysis must not materialize candidate_clips directly. "
        "Use step_ai_voiceover_candidate_materialize instead."
    )
```

## 具体修改方案 B：保留 compat，但默认关闭

如果担心旧任务复用，可以这样写：

```python
def _materialize_content_analysis_candidates_from_segments(self, result: Any) -> dict[str, Any]:
    compat = self.config.raw.get("compat", {})
    if not bool(compat.get("allow_content_analysis_direct_candidate_materialize", False)):
        raise RuntimeError(
            "Deprecated: content_analysis direct candidate materialize is disabled. "
            "Use step_ai_voiceover_candidate_materialize."
        )

    # 原旧逻辑放这里，仅用于兼容旧任务，不参与主链路验收。
    ...
```

并在 `config.toml` 中默认：

```toml
[compat]
allow_content_analysis_direct_candidate_materialize = false
```

## 验收标准

全仓搜索：

```text
_materialize_content_analysis_candidates_from_segments(
```

除函数定义外，不应有主链路调用。

---

# P1-4. ai_voiceover_candidate_materialize 默认对 overlong fail-fast（生产按 P0 执行）

## 当前问题

当前逻辑里：

```python
truncate_overlong = bool(cfg.get("truncate_overlong", False))
fail_on_overlong = bool(cfg.get("fail_on_overlong", False))
```

如果 candidate 超过 `max_clip_seconds`，默认只写 diagnostics，不失败。

这可能导致时长问题拖到 `voiceover_script`、`tts` 或 `cut_plan` 才暴露。

优先级说明：从工程整洁性看它属于 P1，但从生产验收看应按 P0 执行。因为 overlong candidate 一旦继续流入下游，就会破坏“错误在最近责任步骤暴露”的原则。

## 额外代码事实校准

当前 unified workflow 已声明 `ai_voiceover_candidate_materialize`，但这只代表流程表已接入。最终验收必须确认：

```text
1. pipeline.py 中存在 step_ai_voiceover_candidate_materialize()。
2. 该 handler 真实读取 content_analysis + asr_micro_segment。
3. 该 handler 真实写出 ai_voiceover_candidates/v*/candidate_clips.json 与 materialize_debug.json。
4. 空结果、overlong、short 等阻断判断发生在记录 success 之前。
```

建议增加最小代码搜索验收：

```bash
grep -R "def step_ai_voiceover_candidate_materialize" -n newsclip_agent/pipeline.py
grep -R "ai_voiceover_candidate_materialize" -n newsclip_agent/workflow_registry.py newsclip_agent/pipeline.py web_app.py
```

## 修改文件

```text
newsclip_agent/pipeline.py
config.toml
```

## 具体修改 1：调整默认值

在 `step_ai_voiceover_candidate_materialize()` 中改为：

```python
truncate_overlong = bool(cfg.get("truncate_overlong", False))
fail_on_overlong = bool(cfg.get("fail_on_overlong", True))
fail_on_short = bool(cfg.get("fail_on_short", False))
```

## 具体修改 2：如果 truncate_overlong 和 fail_on_overlong 同时打开，优先 fail

新增保护：

```python
if truncate_overlong and fail_on_overlong:
    raise UserFacingPipelineError(
        "ai_voiceover_candidate_invalid_config",
        user_message="AI 配音候选片段配置错误：truncate_overlong 和 fail_on_overlong 不能同时开启。",
        suggestions=["请二选一：验收阶段建议 fail_on_overlong=true，调试阶段才使用 truncate_overlong=true。"],
        technical_detail={
            "truncate_overlong": truncate_overlong,
            "fail_on_overlong": fail_on_overlong,
        },
    )
```

## 具体修改 3：阻断判断必须早于记录 success

当前实现如果先写文件并 `_record_step(status="success")`，再因为 overlong / short raise，会造成：

```text
status.json / manifest 可能已经显示 success；
运行过程却抛出 UserFacingPipelineError；
Web UI 和断点续跑会看到不一致状态。
```

因此 `ai_voiceover_candidate_materialize` 的 empty / overlong / short 阻断判断必须在记录 success 之前完成。

推荐顺序：

```python
should_fail_empty = fail_on_empty and not candidate_clips
should_fail_overlong = fail_on_overlong and bool(diagnostics["overlong_clips"])
should_fail_short = fail_on_short and bool(diagnostics["short_clips"])

status = "failed" if (should_fail_empty or should_fail_overlong or should_fail_short) else "success"

status_doc = self._base_status(
    "ai_voiceover_candidate_materialize",
    version,
    input_hash,
    [out, debug_out],
)
status_doc["status"] = status
if status == "failed":
    status_doc["technical_error"] = debug

self._write_status(vdir, status_doc)
self._record_step(
    step="ai_voiceover_candidate_materialize",
    version=version,
    status=status,
    output=relpath(out, self.task_dir),
    input_hash=input_hash,
    output_files=[out, debug_out],
    extra={"summary": {"candidate_clips": len(candidate_clips)}},
)

if should_fail_empty:
    raise UserFacingPipelineError(...)
if should_fail_overlong:
    raise UserFacingPipelineError(...)
if should_fail_short:
    raise UserFacingPipelineError(...)
```

验收要求：

```text
1. fail_on_overlong=true 且存在 overlong_clips 时，manifest 中该 step 必须是 failed。
2. status.json 必须是 failed。
3. materialize_debug.json 必须能看到 overlong_clips 详情。
4. 不允许先记录 success 再 raise。
```

## 具体修改 4：config.toml 明确默认策略

正式生产默认：`truncate_overlong=false`、`fail_on_overlong=true`。
调试阶段如果临时允许 `fail_on_overlong=false`，必须在 `materialize_debug.json` 记录 overlong 详情，并在 Web/日志里提示。

新增或确认：

```toml
[ai_voiceover_candidate]
padding_before_seconds = 0.5
padding_after_seconds = 0.5
min_clip_seconds = 3.0
max_clip_seconds = 30.0
fail_on_empty = true
fail_on_overlong = true
fail_on_short = false
truncate_overlong = false
```

## 验收标准

构造一个超过 `max_clip_seconds` 的 micro_segment，运行到 `ai_voiceover_candidate_materialize` 应失败，错误为：

```text
ai_voiceover_candidate_overlong
```

---

# P1-5. short_video_edit_plan 必须只读取 ai_voiceover_candidate_materialize

## 当前风险

workflow dependency 已经改了，但还要确认构建输入和物化函数没有继续读取：

```text
content_analysis.candidate_clips
highlight candidate pool
chunk candidate
```

## 修改文件

```text
newsclip_agent/pipeline.py
```

## 具体修改 1：新增统一加载函数

建议新增：

```python
def _load_ai_voiceover_candidate_clips_or_raise(self) -> list[dict[str, Any]]:
    doc = self._load_step_json("ai_voiceover_candidate_materialize")
    clips = doc.get("candidate_clips") if isinstance(doc, dict) else []
    if not isinstance(clips, list) or not clips:
        raise UserFacingPipelineError(
            "ai_voiceover_candidate_clips_empty",
            user_message="AI 配音选片规划失败：没有可用 candidate_clips。",
            suggestions=["回查 ai_voiceover_candidates/v*/candidate_clips.json。"],
            technical_detail={"step": "short_video_edit_plan"},
        )

    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        clip_id = str(clip.get("clip_id") or "").strip()
        source_id = str(clip.get("source_id") or "").strip()
        start = self._time_value_seconds(clip.get("source_start_seconds") or clip.get("start_seconds"))
        end = self._time_value_seconds(clip.get("source_end_seconds") or clip.get("end_seconds"))
        if not clip_id or not source_id or start is None or end is None or end <= start:
            invalid.append({
                "clip_id": clip_id,
                "source_id": source_id,
                "start": start,
                "end": end,
            })
            continue
        valid.append(clip)

    if not valid:
        raise UserFacingPipelineError(
            "ai_voiceover_candidate_clips_invalid",
            user_message="AI 配音选片规划失败：candidate_clips 全部无效。",
            suggestions=["回查 ai_voiceover_candidates/v*/materialize_debug.json。"],
            technical_detail={"invalid": invalid},
        )
    return valid
```

## 具体修改 2：`_build_short_video_edit_plan_text()` 只用这个函数

如果当前函数仍有类似：

```python
content = self._load_step_json("content_analysis")
candidate_clips = content.get("candidate_clips", [])
```

必须替换为：

```python
candidate_clips = self._load_ai_voiceover_candidate_clips_or_raise()
```

输入给模型时只暴露：

```python
{
    "clip_id": clip.get("clip_id"),
    "duration_seconds": clip.get("duration_seconds"),
    "summary": compact_text(str(clip.get("summary") or ""), max_chars=240),
    "role": clip.get("role", "fact"),
    "speech": compact_text(str(clip.get("speech") or clip.get("asr_text") or ""), max_chars=360),
    "visual": compact_text(str(clip.get("visual") or clip.get("visual_context") or ""), max_chars=220),
}
```

不要给模型输出或改写：

```text
source_start_seconds
source_end_seconds
micro_segment_id
asr_segment_ids
source_chunk_ids
```

这些留给代码回查。

## 具体修改 3：`_materialize_short_video_edit_plan()` 只允许 clip_id 回查

在物化函数开头构造：

```python
candidate_clips = self._load_ai_voiceover_candidate_clips_or_raise()
clip_map = {str(c.get("clip_id")): c for c in candidate_clips}
```

当模型返回 clip_id 时：

```python
clip = clip_map.get(clip_id)
if not clip:
    diagnostics["invalid_clip_ids"].append(clip_id)
    continue
```

生成 shot 时必须从 `clip` 拿真实时间：

```python
shot = {
    "shot_id": f"{short_video_id}_s{shot_index:02d}",
    "source_clip_id": clip_id,
    "source_id": clip["source_id"],
    "source_start": clip["source_start"],
    "source_end": clip["source_end"],
    "source_start_seconds": clip["source_start_seconds"],
    "source_end_seconds": clip["source_end_seconds"],
    "duration_seconds": clip["duration_seconds"],
    "visual": clip.get("visual") or clip.get("visual_context") or "",
    "news_fact_to_explain": clip.get("summary") or clip.get("asr_text") or "",
    "micro_segment_ids": clip.get("micro_segment_ids", []),
}
```

## 额外污染检查

```text
content_analysis.json 中如果仍出现 candidate_clips，默认视为旧链路污染。
除 raw_model_plan / debug 字段用于追溯模型原始输出外，不允许 short_video_edit_plan、cut_plan 或任何下游步骤读取 content_analysis.candidate_clips。
AI 配音 candidate_clips 的唯一正式来源必须是 ai_voiceover_candidate_materialize。
```

## 验收标准

```text
1. 删除 content_analysis.json 里的 candidate_clips 不影响 short_video_edit_plan。
2. 删除 ai_voiceover_candidates/v*/candidate_clips.json 后，short_video_edit_plan 必须失败。
3. short_video_edit_plan 输出中的 source_start/source_end 必须来自 candidate_clip，而不是模型输出。
```

---

# P1-6. voiceover_script 继续补强 narration_segments 对齐校验

## 当前状态

当前 `step_voiceover_script()` 已经去掉静默 fallback：模型不返回 scripts 会失败。

但还应继续确保：

```text
1. scripts 非空。
2. 每个 script 有 narration_segments。
3. 每个 narration_segment.shot_id 都能对上 editing_structure.shot_id。
4. 不允许模型新增不存在的 shot_id。
```

## 修改文件

```text
newsclip_agent/pipeline.py
```

## 具体修改：新增或补强 `_validate_voiceover_script_against_editing_structure()`

如果已经有此函数，则按下面逻辑补齐。

```python
def _validate_voiceover_script_against_editing_structure(
    self,
    voiceover: dict[str, Any],
    edit_plan: dict[str, Any],
) -> None:
    scripts = [x for x in voiceover.get("scripts") or [] if isinstance(x, dict)]
    if not scripts:
        raise UserFacingPipelineError(
            "voiceover_script_empty",
            user_message="配音文案校验失败：voiceover_script.scripts 为空。",
            suggestions=["回查 agents/voiceover_script/v*/voiceover_script.json。"],
        )

    expected_by_video: dict[str, set[str]] = {}
    for script in edit_plan.get("scripts") or []:
        if not isinstance(script, dict):
            continue
        vid = str(script.get("short_video_id") or "").strip()
        shot_ids: set[str] = set()
        for shot in script.get("editing_structure") or []:
            if isinstance(shot, dict) and shot.get("shot_id"):
                shot_ids.add(str(shot["shot_id"]))
        if vid:
            expected_by_video[vid] = shot_ids

    errors: list[dict[str, Any]] = []
    for script in scripts:
        vid = str(script.get("short_video_id") or "").strip()
        expected = expected_by_video.get(vid, set())
        segments = [x for x in script.get("narration_segments") or [] if isinstance(x, dict)]
        if not segments:
            errors.append({"short_video_id": vid, "reason": "narration_segments_empty"})
            continue
        actual = {str(x.get("shot_id") or "").strip() for x in segments if str(x.get("shot_id") or "").strip()}
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing or extra:
            errors.append({
                "short_video_id": vid,
                "missing_shot_ids": missing,
                "extra_shot_ids": extra,
            })

    if errors:
        raise UserFacingPipelineError(
            "voiceover_script_shot_id_mismatch",
            user_message="配音文案校验失败：narration_segments 的 shot_id 与剪辑结构不一致。",
            suggestions=[
                "回查 agents/voiceover_script/v*/voiceover_script.json。",
                "回查 agents/short_video_edit_plan/v*/short_video_edit_plan.json。",
                "确认 voiceover_script prompt 要求 narration_segments 必须逐个对齐 shot_id。",
            ],
            technical_detail={"errors": errors},
        )
```

## 验收标准

手动把 `voiceover_script.json` 的一个 `shot_id` 改错，然后重跑后续步骤，应在 voiceover 校验阶段失败，不应进入 tts。

---

# P1-7. tts 必须在 require_tts=true 时空输出失败

## 当前风险

之前出现过：

```text
tts success，但 total=0
```

必须保证：

```text
require_tts=true 时 outputs 为空就是失败。
```

## 修改文件

```text
newsclip_agent/pipeline.py
```

## 具体修改

在 `step_tts()` 生成完输出后，写 status 前加入：

```python
outputs = [x for x in result.get("outputs") or [] if isinstance(x, dict)]
failed = int(result.get("failed") or 0)
require_tts = bool(self.options.require_tts)

if require_tts and not outputs:
    raise UserFacingPipelineError(
        "tts_outputs_empty",
        user_message="TTS 生成失败：当前任务要求 AI 配音，但没有生成任何 TTS 音频。",
        suggestions=[
            "回查 agents/voiceover_script/v*/voiceover_script.json 是否有 narration_segments。",
            "检查 voice_id / OmniVoice 配置 / tts_ref 是否有效。",
            "检查 tts/v*/tts.json 的错误信息。",
        ],
        technical_detail={
            "require_tts": require_tts,
            "outputs": len(outputs),
            "failed": failed,
            "result": result,
        },
    )
```

如果当前 `step_tts()` 是逐条生成并累计 outputs，也可以在所有任务结束后加：

```python
if self.options.require_tts and len(outputs) == 0:
    status = "failed"
else:
    status = "success"
```

但推荐直接 raise，避免 manifest 记录 success。

## 额外校验：每个 narration_segment 应有对应 tts output

新增：

```python
def _expected_tts_segment_keys(self) -> set[tuple[str, str]]:
    voiceover = self._load_step_json("voiceover_script")
    expected: set[tuple[str, str]] = set()
    for script in voiceover.get("scripts") or []:
        if not isinstance(script, dict):
            continue
        vid = str(script.get("short_video_id") or "").strip()
        for seg in script.get("narration_segments") or []:
            if not isinstance(seg, dict):
                continue
            shot_id = str(seg.get("shot_id") or "").strip()
            text = str(seg.get("text") or "").strip()
            if vid and shot_id and text:
                expected.add((vid, shot_id))
    return expected
```

TTS 完成后：

```python
expected = self._expected_tts_segment_keys()
actual = {
    (str(x.get("short_video_id") or "").strip(), str(x.get("shot_id") or "").strip())
    for x in outputs
}
missing = sorted(expected - actual)
if self.options.require_tts and missing:
    raise UserFacingPipelineError(
        "tts_missing_segments",
        user_message="TTS 生成失败：部分 narration_segments 没有对应音频。",
        suggestions=["回查 tts/v*/tts.json。", "检查失败音频段的错误日志。"],
        technical_detail={"missing": missing},
    )
```

## 验收标准

```text
1. voiceover_script 有 narration_segments，但 TTS 生成失败时，step_tts 必须 failed。
2. outputs=[] 时不能 success。
3. 缺少任意 shot_id 音频时不能进入 subtitles/cut_plan。
```

---

# P1-8. cut_plan 必须在 clips 为空时失败

## 当前风险

之前链路出现过：

```text
cut_plan success
news_quality_gate failed
```

说明 cut_plan 层可能没有对空 clips 做足够阻断。

## 修改文件

```text
newsclip_agent/pipeline.py
```

## 具体修改

在 `step_cut_plan()` 写出 `cut_plan.json` 前，加入统一校验：

```python
def _validate_cut_plan_not_empty_or_raise(self, cut_plan: dict[str, Any]) -> None:
    videos = [x for x in cut_plan.get("output_videos") or [] if isinstance(x, dict)]
    if not videos:
        raise UserFacingPipelineError(
            "cut_plan_output_videos_empty",
            user_message="剪辑计划生成失败：cut_plan.output_videos 为空。",
            suggestions=["回查 short_video_edit_plan / voiceover_script / tts 输出。"],
            technical_detail={"cut_plan": cut_plan},
        )

    errors: list[dict[str, Any]] = []
    for video in videos:
        vid = str(video.get("short_video_id") or video.get("video_id") or "").strip()
        clips = [x for x in video.get("clips") or [] if isinstance(x, dict)]
        if not clips:
            errors.append({"short_video_id": vid, "reason": "clips_empty"})
            continue
        for clip in clips:
            source_id = str(clip.get("source_id") or "").strip()
            start = self._time_value_seconds(clip.get("source_start_seconds") or clip.get("source_start") or clip.get("start_seconds"))
            end = self._time_value_seconds(clip.get("source_end_seconds") or clip.get("source_end") or clip.get("end_seconds"))
            if not source_id or start is None or end is None or end <= start:
                errors.append({
                    "short_video_id": vid,
                    "reason": "invalid_clip_time_or_source",
                    "clip": clip,
                })

    if errors:
        raise UserFacingPipelineError(
            "cut_plan_clips_invalid",
            user_message="剪辑计划生成失败：存在空 clips 或无效 source/time。",
            suggestions=[
                "回查 cut_plan/v*/cut_plan.json。",
                "回查 short_video_edit_plan 的 editing_structure 是否有 source_start/source_end。",
                "回查 TTS 是否生成完整。",
            ],
            technical_detail={"errors": errors},
        )
```

然后在 `step_cut_plan()` 写文件前调用：

```python
self._validate_cut_plan_not_empty_or_raise(cut_plan)
```

## 验收标准

手动构造空 clips：

```json
{"output_videos": [{"short_video_id": "v_001", "clips": []}]}
```

应在 cut_plan 失败，而不是拖到 news_quality_gate。

---

# P2-1. run_web.py 端口探测修正

## 当前问题

`run_web.py` 默认 host 是：

```python
--host 0.0.0.0
```

但 `find_free_port()` 用传入 host 做 connect 探测。对 `0.0.0.0` 探测在部分环境不稳定。

## 修改文件

```text
run_web.py
```

## 具体修改

当前：

```python
host = args.host
if args.port is not None:
    port = args.port
else:
    port = find_free_port(host=host, start=7860)
```

替换为：

```python
host = args.host
if args.port is not None:
    port = args.port
else:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    port = find_free_port(host=probe_host, start=7860)
```

打印地址也建议优化：

```python
display_host = "127.0.0.1" if host == "0.0.0.0" else host
print(f"Web 工作台地址: http://{display_host}:{port}")
```

但 `uvicorn.run()` 仍然绑定原 host：

```python
uvicorn.run("web_app:app", host=host, port=port, reload=False)
```

## 验收标准

```text
1. 默认启动仍绑定 0.0.0.0。
2. 控制台打印本机可访问地址 127.0.0.1:port。
3. 7860 被占用时能正确探测到 7861/7862。
```

---

# P2-2. Web UI step label 补充

## 当前风险

workflow 里新增了：

```text
ai_voiceover_candidate_materialize
```

如果 Web UI step label 是固定映射，可能显示英文或漏显示。

## 修改文件

```text
web_app.py
web_static/index.html / JS 文件
```

## 具体修改

搜索：

```text
step label
stepName
STEP_LABELS
步骤
source_aggregate
short_video_edit_plan
```

如果有映射，补充：

```javascript
ai_voiceover_candidate_materialize: "生成 AI 配音候选片段"
```

后端如果有 Python 映射，补充：

```python
"ai_voiceover_candidate_materialize": "生成 AI 配音候选片段"
```

## 验收标准

Web 任务进度中应显示中文：

```text
生成 AI 配音候选片段
```

而不是裸 step 名。

---

# 统一回归测试清单

## 1. 正常 Web 单视频任务

入口：

```bash
python run_web.py --port 7860
```

上传 1 个视频，生产模式选择 AI 配音。

预期：

```text
即使只有 1 个视频，也走 unified source pipeline。
manifest.steps 包含：
source_prepare
source_analysis
source_aggregate
asr_micro_segment
content_analysis
ai_voiceover_candidate_materialize
short_video_edit_plan
voiceover_script
tts
subtitles
cut_plan
news_quality_gate
render
```

检查文件：

```text
source_aggregate/v*/source_raw_asr_index.json
agents/asr_micro_segment/v*/asr_sentences.json
agents/asr_micro_segment/v*/micro_segments.json
agents/content_analysis/v*/content_analysis.json
ai_voiceover_candidates/v*/candidate_clips.json
agents/short_video_edit_plan/v*/short_video_edit_plan.json
agents/voiceover_script/v*/voiceover_script.json
tts/v*/tts.json
cut_plan/v*/cut_plan.json
```

关键断言：

```text
raw_asr_segment_count > 0
micro_segments.segments > 0
content_analysis.selected_segments > 0
candidate_clips > 0
short_video_edit_plan.scripts > 0
voiceover_script.scripts > 0
require_tts=true 时 tts.outputs > 0
cut_plan.output_videos[].clips > 0
```

## 2. raw ASR 缺失测试

手动删除：

```text
source_aggregate/v*/source_raw_asr_index.json
```

重跑：

```text
asr_micro_segment
```

预期：

```text
失败：source_raw_asr_index_missing
不能 fallback 到 timeline_digest.speech
```

## 3. content_analysis 空选择测试

把模型输出模拟为：

```json
{"selected_segments": []}
```

预期：

```text
失败：content_analysis_selected_segments_empty
不能进入 ai_voiceover_candidate_materialize
```

## 4. candidate materialize 空结果测试

把 `selected_segments` 改成不存在的 micro_segment_id。

预期：

```text
失败：ai_voiceover_candidate_clips_empty
materialize_debug.json 里有 missing_segment_ids
```

## 5. short_video_edit_plan 无效 clip_id 测试

让模型输出：

```json
{"scripts":[{"short_video_id":"v_001","clip_ids":["not_exists"]}]}
```

预期：

```text
第一次语义校验失败后触发 semantic retry。
retry 仍失败则 short_video_edit_plan failed。
不能自动选择前 N 个 clip。
```

## 6. voiceover_script 空输出测试

让模型输出：

```json
{"scripts": []}
```

预期：

```text
失败：voiceover_script_model_empty 或 voiceover_script_empty
不能进入 tts
```

## 7. TTS 空输出测试

让 voiceover_script 有 narration_segments，但 TTS mock 返回：

```json
{"outputs": [], "failed": 0}
```

预期：

```text
失败：tts_outputs_empty
不能进入 subtitles / cut_plan
```

## 8. cut_plan 空 clips 测试

构造：

```json
{"output_videos": [{"short_video_id": "v_001", "clips": []}]}
```

预期：

```text
失败：cut_plan_clips_invalid 或 cut_plan_output_videos_empty
不能拖到 news_quality_gate 才失败
```

---


## 9. 配置安全测试

检查：

```bash
grep -R "sk-\|api_key\|doubao_api_key\|openai_api_key" -n .   --exclude-dir=.git   --exclude="config.local.toml"   --exclude=".env"
```

预期：

```text
1. 不出现真实 API key。
2. config.example.toml 只包含占位符。
3. 本地私有配置不进入 git。
```

---
# 最终建议提交顺序

建议按下面顺序拆 commit，方便回滚：

```text
commit 1: config 安全清理，移除真实 API key，新增 config.example.toml / 环境变量说明
commit 2: asr_micro_segment raw_asr_index 独立文件加载与 hash 修正
commit 3: 禁止 timeline_digest fallback 与 deprecated candidate materialize 防误用
commit 4: source_aggregate ASR/chunk diagnostics 补强
commit 5: content_analysis 输入瘦身与协议检查
commit 6: candidate overlong fail-fast 默认策略与状态一致性修正
commit 7: voiceover_script / tts / cut_plan 空结果 fail-fast 补强
commit 8: run_web 端口探测和 Web step label 优化
```

每个 commit 后都至少跑：

```bash
python -m py_compile run_web.py web_app.py run_pipeline.py newsclip_agent/pipeline.py newsclip_agent/workflow_registry.py
```

如果项目有测试命令，再跑：

```bash
pytest
```

没有测试时，至少跑一条 1 视频 Web AI 配音任务，检查上面的回归测试清单。
