# AI 配音 micro_segment 链路代码开发方案（统一多源链路修订版）

> 本版是在上一版代码开发方案基础上，把“口头描述”改成“可直接交给开发执行的具体改法”。
>
> 原则仍然不变：不为了跑通而静默 fallback；凡是上游数据不符合协议，必须在对应步骤 fail-fast，并把问题暴露出来。
>
> 本文不重新设计提示词。用户会自行维护提示词；代码只适配提示词的输入和输出协议。当前已知用户新版提示词文档已包含 `asr_micro_segment`、`asr_event_candidate`、`short_video_edit_plan_text`、`voiceover_script_text`、`voiceover_repair_text`，并且已补充 AI 配音专用的 `content_analysis_micro_segment`。本代码开发方案不重新设计提示词，只要求把提示词文档中的 `content_analysis_micro_segment` 正确落到仓库文件、`prompts.py` 常量和 `AGENT_INFO` 绑定中；如果仓库文件尚未创建，开发必须先创建文件，不能继续复用旧 chunk 版 `CONTENT_ANALYSIS_PROMPT`。

>
> **本轮修订前提**：当前项目运行时，即使只传入 1 个视频，也走统一多源链路；也就是实际有效路径以 `("ai_voiceover", True)`、`source_prepare -> source_analysis -> source_aggregate` 为准。旧的单源 legacy workflow 暂时不作为本轮开发目标，只保留兼容，不投入主要改造成本。
>
> **本轮修订重点**：只完善代码开发方案文档，不改架构文档、不改提示词文档、不直接改仓库代码。

---

## 0. 当前代码事实与本次改造边界

### 0.1 当前关键代码事实

当前 `pipeline.py` 里：

```python
AGENT_INFO = {
    "asr_micro_segment": (..., prompts.ASR_MICRO_SEGMENT_PROMPT, ...),
    "content_analysis": (..., prompts.CONTENT_ANALYSIS_PROMPT, ...),
    "short_video_edit_plan": (..., prompts.SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT, ...),
    "voiceover_script": (..., prompts.VOICEOVER_SCRIPT_TEXT_PROMPT, ...),
}
```

这说明：

```text
1. asr_micro_segment 是 LLM 步骤。
2. content_analysis 现在仍绑定 CONTENT_ANALYSIS_PROMPT。
3. short_video_edit_plan / voiceover_script 已经是 LLM 步骤。
4. 目前没有 ai_voiceover_candidate_materialize 这个显式步骤。
```

当前 `workflow_registry.py` 里 AI 配音统一多源链路是：

```python
("ai_voiceover", True): [
    "source_prepare",
    "source_analysis",
    "source_aggregate",
    "asr_micro_segment",
    "content_analysis",
    "short_video_edit_plan",
    "voiceover_script",
    "voiceover_quality_gate",
    "tts",
    "subtitles",
    "cut_plan",
    "news_quality_gate",
    "news_quality_ai_review",
    "render",
]
```

视频重组统一多源链路是：

```python
("highlight_reassembly", True): [
    "source_prepare",
    "source_analysis",
    "source_aggregate",
    "video_understanding",
    "asr_event_candidate",
    "candidate_filter",
    "highlight_reassembly_plan",
    "reassembly_cut_plan",
    "news_quality_gate",
    "news_quality_ai_review",
    "reassembly_render",
]
```

本次改造只改 AI 配音链路。视频重组链路不接入 `asr_micro_segment`，也不接入 `ai_voiceover_candidate_materialize`。

### 0.2 本轮补充修订说明

本轮是在原代码开发方案基础上的微调补充，不推翻原方案、不重写架构、不大删大改。

补充重点：

```text
1. 确认 content_analysis_micro_segment 提示词已经在提示词优化文档中存在。
   本方案只补代码接入要求：prompt 文件、prompts.py 常量、AGENT_INFO 绑定和运行前校验。
2. 明确 Web / 统一多源链路是本轮验收主路径。
   直接 run_pipeline.py --input 的 legacy 单源路径不作为本轮验收范围，避免为了兼容旧链路重新引入 digest fallback。
3. 明确禁止三类隐式兜底：
   asr_micro_segment fallback 到 timeline_digest.speech；
   content_analysis fallback 到 chunk candidate；
   short_video_edit_plan 模型空输出后自动选前 N 个 clip。
4. 明确下游读取路径：
   short_video_edit_plan 只能读取 ai_voiceover_candidate_materialize 的 candidate_clips，不能继续读取 content_analysis.candidate_clips。
5. 补充 fail-fast：
   selected_segments 为空、candidate_clips 为空、scripts 为空、narration_segments 为空、tts outputs 为空、cut_plan clips 为空都必须失败。
```

---

## 1. 总体目标代码链路

改造后的 AI 配音链路应为：

```text
source_analysis
  -> 每个 source 子任务产生原始 asr_segments.json

source_aggregate
  -> source_aggregate.json
  -> source_raw_asr_index.json

asr_micro_segment
  输入：source_raw_asr_index.json + chunk visual context
  输出：micro_segments.json

content_analysis
  输入：micro_segments
  输出：selected_segments

ai_voiceover_candidate_materialize
  输入：selected_segments + micro_segments
  输出：ai_voiceover_candidates/candidate_clips.json

short_video_edit_plan
  输入：candidate_clips
  输出：clip_ids
  代码物化：clip_id -> editing_structure / shot_id / source_start / source_end

voiceover_script
  输入：shot plan
  输出：shot_id 对齐 narration_segments

tts
  输入：narration_segments
  输出：按 shot_id 的 TTS 音频

cut_plan
  输入：editing_structure + voiceover_script + tts
  输出：最终可渲染剪辑计划
```

ID 主链路：

```text
raw_asr_segment_id
  -> micro_segment_id
  -> candidate_clip.clip_id
  -> editing_structure.shot_id
  -> narration_segments.shot_id
  -> cut_plan.clips[].shot_id
```

时间主链路：

```text
raw_asr_segment.start/end
  -> micro_segment.source_start/source_end
  -> candidate_clip.source_start/source_end with padding
  -> editing_structure.source_start/source_end
  -> cut_plan.clips.source_start/source_end
```

严禁再出现：

```text
asr_micro_segment 从 timeline_digest.speech 按字数比例推时间。
content_analysis 输出为空但 success。
short_video_edit_plan 输出为空但自动选前 8 个。
tts outputs=0 但 success。
```

---

## 2. 修改 `workflow_registry.py`

### 2.0 本轮只改统一多源 AI 配音 workflow

因为当前运行时“单源也走多源统一逻辑”，所以本轮实际有效 workflow 是：

```python
("ai_voiceover", True)
```

旧的：

```python
("ai_voiceover", False)
```

暂时只作为 legacy 兼容路径，不作为本轮主开发目标。不要为了旧单源路径额外写一套 raw ASR 构造、候选读取、prompt 分支，否则会把链路重新做复杂。

开发原则：

```text
1. 主路径只认统一多源链路。
2. 单视频输入也必须先进入 source_prepare/source_analysis/source_aggregate。
3. asr_micro_segment 的输入统一来自 source_aggregate 生成的 source_raw_asr_index.json。
4. 不再维护“单源直接从 asr + chunk_build 构造 micro_segment”的主逻辑。
5. legacy 单源 workflow 可以暂时不改，或同步加 step 但不作为验收路径。
```

补充边界：

```text
当前 Web 入口默认 use_unified_source_pipeline=true，且 Web 初始化任务时会按 unified workflow 展示步骤。
但 PipelineRunner._active_step_order() 实际用 _is_virtual_source_manifest() 判断 unified。
因此，直接执行 run_pipeline.py --input xxx.mp4、且没有 source_manifest 的 CLI 单源任务，仍可能进入 ("ai_voiceover", False) legacy workflow。

本轮验收只覆盖 Web / run_multisource_pipeline / source_manifest 统一链路。
不要为了直接 CLI 单源路径临时拼 raw ASR index，也不要为它恢复 timeline_digest.speech fallback。
如果后续要支持 CLI 单源，也应先把 CLI 单源包装进 source_prepare/source_analysis/source_aggregate，而不是另写一套旧链路。
```

### 2.1 新增 AI 配音候选物化步骤

文件：

```text
newsclip_agent/workflow_registry.py
```

只修改统一多源 AI 配音 workflow。

把：

```python
("ai_voiceover", True): [
    "source_prepare",
    "source_analysis",
    "source_aggregate",
    "asr_micro_segment",
    "content_analysis",
    "short_video_edit_plan",
    "voiceover_script",
    "voiceover_quality_gate",
    "tts",
    "subtitles",
    "cut_plan",
    "news_quality_gate",
    "news_quality_ai_review",
    "render",
]
```

改成：

```python
("ai_voiceover", True): [
    "source_prepare",
    "source_analysis",
    "source_aggregate",
    "asr_micro_segment",
    "content_analysis",
    "ai_voiceover_candidate_materialize",
    "short_video_edit_plan",
    "voiceover_script",
    "voiceover_quality_gate",
    "tts",
    "subtitles",
    "cut_plan",
    "news_quality_gate",
    "news_quality_ai_review",
    "render",
]
```

### 2.2 legacy 单源 workflow 的处理

`("ai_voiceover", False)` 本轮不作为主路径。

有两种允许做法：

```text
方案 A：不改 legacy 单源 workflow。
前提：确认 run_web / web_app / run_pipeline 在当前配置下不会进入 legacy 单源 workflow。

方案 B：为了避免未来误用，也同步插入 ai_voiceover_candidate_materialize。
前提：仍然不为 legacy 单源单独写一套 raw ASR 加载逻辑。
```

推荐方案 A。

如果选择方案 B，只允许同步插入 step，不允许引入 digest fallback：

```python
"content_analysis",
"ai_voiceover_candidate_materialize",
"short_video_edit_plan",
```

### 2.3 新增依赖

当前：

```python
"content_analysis": ["asr_micro_segment", "timeline_digest", "source_aggregate"],
"short_video_edit_plan": ["content_analysis"],
```

改成：

```python
"content_analysis": ["asr_micro_segment", "timeline_digest", "source_aggregate"],
"ai_voiceover_candidate_materialize": ["content_analysis", "asr_micro_segment"],
"short_video_edit_plan": ["ai_voiceover_candidate_materialize"],
```

注意：

```text
1. 这里保留 content_analysis 对 source_aggregate 的依赖，因为当前主路径就是统一多源。
2. 不再为 legacy 单源设计独立依赖。
3. 如果未来恢复 legacy 单源 workflow，再单独设计兼容方案。
```

### 2.4 视频重组 workflow 不改

不要改：

```python
("highlight_reassembly", False)
("highlight_reassembly", True)
```

不要把下面两个步骤加进视频重组链路：

```text
asr_micro_segment
ai_voiceover_candidate_materialize
```

### 2.5 必须新增 step handler

只改 workflow 不够。

`PipelineRunner.run()` 会按 step 名拼 handler：

```python
handler_name = f"step_{step}"
handler = getattr(self, handler_name, None)
```

所以新增 workflow step 后，必须在 `pipeline.py` 实现：

```python
def step_ai_voiceover_candidate_materialize(self) -> None:
    ...
```

否则运行到该步骤会直接报：

```text
workflow step ai_voiceover_candidate_materialize has no handler
```

### 2.6 这一步的链路影响检查

改完以后：

```text
AI 配音统一多源链路：content_analysis 后一定会进入 candidate materialize。
视频重组链路：不受影响，仍走 asr_event_candidate / candidate_filter / reassembly_cut_plan。
```

潜在问题：

```text
如果 web_app.py 的步骤标题、导出清单、进度显示是写死的，会显示英文 step 或漏显示。
```

处理方式：

```text
搜索 web_app.py 中 step label / export / progress 相关 mapping。
如果有固定列表，增加：
ai_voiceover_candidate_materialize -> 生成 AI 配音候选片段
```

不要因为 UI 没显示就不加 workflow step。

---

## 3. 修改 `pipeline.py`：新增 raw ASR index

文件：

```text
newsclip_agent/pipeline.py
```

### 3.1 目标

在 `source_aggregate` 阶段输出：

```text
source_aggregate/v*/source_raw_asr_index.json
```

这个文件是 `asr_micro_segment` 的唯一可靠 ASR 输入。

### 3.2 不要改 ASR 本身的识别逻辑

当前 `step_asr()` 已经输出 `asr_segments.json`，每条 segment 有：

```python
"start": start + local_start,
"end": start + local_end,
"text": text,
"raw_text": cleaned["raw_text"],
"removed_tokens": cleaned["removed_tokens"],
"asr_segment_id": wid,
"chunk_id": window.get("chunk_id", ""),
```

这里先不改 ASR 识别，不扩大 `clean_asr_text()` 的清理规则。

本次只在聚合阶段把它整理成跨 source 统一索引。

### 3.3 新增 helper：读取子任务 ASR

放在 `step_source_aggregate()` 附近，建议放在 `_build_source_summary()` 后面。

新增函数：

```python
def _load_child_source_asr_segments(
    self,
    *,
    source_id: str,
    source_index: int | str | None,
    summary_doc: dict[str, Any],
) -> list[dict[str, Any]]:
    work_task_dir = str(summary_doc.get("work_task_dir") or "").strip()
    if not work_task_dir:
        return []

    child_dir = self.task_dir / work_task_dir
    child_manifest = read_json(child_dir / "manifest.json", {})
    asr_step = (child_manifest.get("steps") or {}).get("asr") or {}
    asr_rel = str(asr_step.get("output") or "").strip()
    if not asr_rel:
        return []

    asr_doc = read_json(child_dir / asr_rel, {})
    raw_segments = asr_doc.get("segments") or []
    if not isinstance(raw_segments, list):
        return []

    out: list[dict[str, Any]] = []
    for idx, seg in enumerate(raw_segments, start=1):
        if not isinstance(seg, dict):
            continue

        start = self._time_value_seconds(seg.get("start"))
        end = self._time_value_seconds(seg.get("end"))
        if start is None or end is None or end <= start:
            continue

        raw_text = str(seg.get("raw_text") or "").strip()
        text = raw_text or str(seg.get("text") or "").strip()
        if not text:
            continue

        out.append({
            "asr_segment_id": f"{source_id}_asr_{idx:06d}",
            "source_raw_asr_segment_id": seg.get("asr_segment_id") or seg.get("id") or "",
            "source_id": source_id,
            "source_index": source_index,
            "start_seconds": round(float(start), 3),
            "end_seconds": round(float(end), 3),
            "duration_seconds": round(float(end - start), 3),
            "text": text,
            "raw_text": raw_text,
            "normalized_text": re.sub(r"\s+", " ", text).strip(),
            "source_chunk_id_hint": seg.get("chunk_id", ""),
            "removed_tokens": seg.get("removed_tokens", []),
        })
    return out
```

说明：

```text
1. 优先用 child manifest 的 asr output，不猜路径。
2. asr_segment_id 重新生成，避免不同 source 的 chunk_0001 重名。
3. text 优先 raw_text，其次 text。
4. normalized_text 只合并空白，不删内容。
5. 不删除音乐、掌声、噪声、嗯啊。
```

鲁棒性：

```text
如果 child manifest 没有 asr output，返回空。
但 source_aggregate 最后要把 diagnostics 写出来。
asr_micro_segment 后续发现 raw ASR 为空时 fail-fast。
```

### 3.4 新增 helper：ASR 映射 chunk

放在同一区域。

```python
def _map_asr_segment_to_source_chunks(
    self,
    *,
    asr_start: float,
    asr_end: float,
    chunks: list[dict[str, Any]],
    source_id: str,
) -> tuple[str, list[str], list[dict[str, Any]]]:
    overlaps: list[tuple[float, dict[str, Any]]] = []
    diagnostics: list[dict[str, Any]] = []

    for chunk in chunks:
        if str(chunk.get("source_id") or "") != str(source_id):
            continue
        c_start = float(chunk.get("local_start_seconds") or chunk.get("start_seconds") or 0)
        c_end = float(chunk.get("local_end_seconds") or chunk.get("end_seconds") or c_start)
        overlap = max(0.0, min(asr_end, c_end) - max(asr_start, c_start))
        if overlap > 0:
            overlaps.append((overlap, chunk))

    if overlaps:
        overlaps.sort(key=lambda x: x[0], reverse=True)
        primary = str(overlaps[0][1].get("chunk_id") or overlaps[0][1].get("global_chunk_id") or "")
        ids = []
        for _ov, chunk in overlaps:
            cid = str(chunk.get("chunk_id") or chunk.get("global_chunk_id") or "")
            if cid and cid not in ids:
                ids.append(cid)
        return primary, ids, diagnostics

    # 没有 overlap 时，不伪造映射，但给最近 chunk 做 diagnostic
    center = (asr_start + asr_end) / 2
    nearest: tuple[float, dict[str, Any]] | None = None
    for chunk in chunks:
        if str(chunk.get("source_id") or "") != str(source_id):
            continue
        c_start = float(chunk.get("local_start_seconds") or chunk.get("start_seconds") or 0)
        c_end = float(chunk.get("local_end_seconds") or chunk.get("end_seconds") or c_start)
        distance = min(abs(center - c_start), abs(center - c_end))
        if nearest is None or distance < nearest[0]:
            nearest = (distance, chunk)

    diagnostics.append({
        "type": "asr_chunk_no_overlap",
        "source_id": source_id,
        "asr_start_seconds": round(asr_start, 3),
        "asr_end_seconds": round(asr_end, 3),
        "nearest_chunk_id": (nearest[1].get("chunk_id") if nearest else ""),
        "nearest_distance_seconds": round(nearest[0], 3) if nearest else None,
    })
    return "", [], diagnostics
```

鲁棒性：

```text
1. 没有 overlap 不应伪造 chunk_id。
2. visual_context 后续可以为空。
3. 但必须写 diagnostics，方便排查时间轴错位。
```

### 3.5 在 `step_source_aggregate()` 中生成 raw_asr_index

当前 `step_source_aggregate()` 已经遍历 source summary 并生成 `summaries` 和 `chunks`。

在 `chunks.sort(...)` 后面、构造 `aggregate` 前面，新增：

```python
raw_asr_sources: list[dict[str, Any]] = []
raw_asr_diagnostics: list[dict[str, Any]] = []

for source_summary in summaries:
    source_id = str(source_summary.get("source_id") or "")
    source_index = source_summary.get("source_index")
    # 需要重新拿 summary_doc；建议前面循环时把 summary_doc 放入一个 dict：summary_docs_by_source_id[source_id] = summary_doc
    summary_doc = summary_docs_by_source_id.get(source_id, {})
    raw_segments = self._load_child_source_asr_segments(
        source_id=source_id,
        source_index=source_index,
        summary_doc=summary_doc,
    )

    enriched_segments = []
    for seg in raw_segments:
        primary_chunk_id, source_chunk_ids, diags = self._map_asr_segment_to_source_chunks(
            asr_start=float(seg["start_seconds"]),
            asr_end=float(seg["end_seconds"]),
            chunks=chunks,
            source_id=source_id,
        )
        raw_asr_diagnostics.extend(diags)
        enriched_segments.append({
            **seg,
            "primary_chunk_id": primary_chunk_id,
            "source_chunk_ids": source_chunk_ids,
        })

    raw_asr_sources.append({
        "source_id": source_id,
        "source_index": source_index,
        "display_name": source_summary.get("display_name", ""),
        "duration_seconds": source_summary.get("duration_seconds"),
        "asr_segments": enriched_segments,
    })
```

注意：当前代码没有 `summary_docs_by_source_id`，所以在原本读取 `summary_doc` 的循环里加：

```python
summary_docs_by_source_id: dict[str, dict[str, Any]] = {}
...
summary_docs_by_source_id[source_id] = summary_doc
```

补充要求：

```text
1. source_aggregate 读取 raw ASR 时，必须通过 child manifest 的 asr output 找文件，不猜固定路径。
2. source_aggregate 的 input_hash 建议纳入 child asr output 的路径、hash 或 child manifest 中 asr step 的 output_hash。
   仅依赖 source_analysis hash 可能导致子任务 ASR 产物变化但 source_aggregate 被错误复用。
3. 因为 FunASR window 有 overlap，raw_asr_index 可以保留所有有效 ASR segment，但 diagnostics 里应记录疑似重复边界段。
   本轮不强制复杂去重，禁止为去重改写文本或丢失事实。
```

然后写文件：

```python
raw_asr_index = {
    "version": "source_raw_asr_index_v1",
    "source_count": len(raw_asr_sources),
    "sources": raw_asr_sources,
    "diagnostics": raw_asr_diagnostics,
}
raw_asr_out = write_json(vdir / "source_raw_asr_index.json", raw_asr_index)
```

`aggregate` 增加引用：

```python
"raw_asr_index_file": relpath(raw_asr_out, self.task_dir),
"raw_asr_segment_count": sum(len(s.get("asr_segments") or []) for s in raw_asr_sources),
"raw_asr_diagnostics_count": len(raw_asr_diagnostics),
```

`_base_status` 和 `_record_step` 的 output_files 要包含两个文件：

```python
out = write_json(vdir / "source_aggregate.json", aggregate)
self._write_status(vdir, self._base_status("source_aggregate", version, input_hash, [out, raw_asr_out]))
self._record_step(
    step="source_aggregate",
    version=version,
    status="success",
    output=relpath(out, self.task_dir),
    input_hash=input_hash,
    output_files=[out, raw_asr_out],
    extra={"summary": {"chunks": len(chunks), "sources": len(summaries), "raw_asr_segments": ...}},
)
```

链路检查：

```text
1. source_aggregate 原有输出不变，视频重组读取 source_aggregate.json 不受影响。
2. raw_asr_index 是新增文件，AI 配音读取。
3. 如果 raw_asr_segments=0，不要在 source_aggregate 失败，因为有些任务可能不走 AI 配音；在 asr_micro_segment 失败更准确。
```

---

## 4. 修改 `pipeline.py`：统一加载 raw ASR index

### 4.0 本轮不再走 legacy 单源 raw ASR 构造

因为当前单视频也走统一多源逻辑，所以 `asr_micro_segment` 的 raw ASR 输入统一来自：

```text
source_aggregate/v*/source_raw_asr_index.json
```

不再把下面这条路径作为主路径：

```text
单源任务 -> 当前任务 asr + chunk_build -> 运行时拼 raw_asr_index
```

如果代码里保留这个 legacy 分支，只能作为兼容兜底，并且默认不启用。不能在统一多源主路径缺 raw ASR 时自动 fallback 到 legacy，也不能 fallback 到 `timeline_digest.speech`。

### 4.1 新增 `_load_source_raw_asr_index()`

放在 `_load_current_timeline_digest()` 附近。

```python
def _load_source_raw_asr_index(self) -> dict[str, Any]:
    aggregate = self._load_step_json("source_aggregate")
    rel = str(aggregate.get("raw_asr_index_file") or "").strip()
    if rel:
        doc = read_json(self.task_dir / rel, {})
        if isinstance(doc, dict) and doc.get("sources"):
            return doc

    agg_output = self._step_output("source_aggregate")
    if agg_output:
        candidate = (self.task_dir / agg_output).parent / "source_raw_asr_index.json"
        doc = read_json(candidate, {})
        if isinstance(doc, dict) and doc.get("sources"):
            return doc

    raise UserFacingPipelineError(
        "source_raw_asr_index_missing",
        user_message="AI 配音微片段生成失败：source_raw_asr_index.json 不存在或为空。",
        suggestions=[
            "请先重跑 source_aggregate。",
            "检查 source_aggregate 是否已写出 raw_asr_index_file。",
            "确认单视频输入也走 source_prepare/source_analysis/source_aggregate 统一链路。",
        ],
        technical_detail={
            "source_aggregate_output": self._step_output("source_aggregate"),
        },
    )
```

说明：

```text
1. 主路径只读 source_aggregate。
2. 找不到 raw_asr_index 就失败。
3. 不回退 timeline_digest。
4. 不在这里临时拼单源 raw ASR。
```

### 4.2 新增 `_source_raw_asr_index_hash()`

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

    return self._step_content_hash("source_aggregate")
```

### 4.3 禁止 fallback 配置

即使 `config.toml` 里保留兼容开关，也必须默认关闭：

```toml
[compat]
allow_asr_micro_segment_from_timeline_digest = false
allow_legacy_single_source_raw_asr_runtime_build = false
```

如果将来临时排查需要打开 legacy 分支，必须显式配置，且日志里写清楚：

```text
legacy_single_source_raw_asr_runtime_build=true
```

本轮验收不使用该分支。

---

## 5. 修改 `pipeline.py`：asr_micro_segment 改为 raw ASR 输入

### 5.1 改 `step_asr_micro_segment()`

当前：

```python
sentences = self._build_asr_sentence_units()
if cfg.get("include_visual_summary", True):
    sentences = self._attach_visual_to_asr_sentences(sentences)
```

改成：

```python
sentences = self._build_asr_sentence_units_from_raw_asr()
# 新函数已经附加 visual_context，不再调用旧 _attach_visual_to_asr_sentences
```

input_hash 改成：

```python
input_hash = stable_hash({
    "source_raw_asr_index": self._source_raw_asr_index_hash(),
    "visual_context": self._current_timeline_digest_hash(),
    "asr_micro_segment_cfg": cfg,
    "prompt_version": "asr_micro_segment_raw_asr_v2",
})
```

在写 `asr_sentences.json` 前加 fail-fast：

```python
if not sentences:
    raise UserFacingPipelineError(
        "raw_asr_empty",
        user_message="AI 配音微片段生成失败：没有可用原始 ASR。",
        suggestions=[
            "检查 source_aggregate/v*/source_raw_asr_index.json 是否生成。",
            "检查 ASR 步骤是否产生有效 segments。",
        ],
        technical_detail={"step": "asr_micro_segment"},
    )
```

在 `segments = ...` 后加：

```python
if not segments:
    raise UserFacingPipelineError(
        "micro_segments_empty",
        user_message="AI 配音微片段生成失败：没有生成任何 micro_segment。",
        suggestions=[
            "检查 asr_micro_segment 模型输出 groups 是否为空。",
            "检查 asr_sentences.json 是否有有效句子。",
        ],
        technical_detail={"sentence_count": len(sentences)},
    )
```

链路影响：

```text
1. asr_micro_segment 不再依赖 digest.speech 的文本。
2. 如果 raw ASR 没有，停在 asr_micro_segment，不再空跑到后面。
3. 视频重组不走这个步骤，不受影响。
```

### 5.2 新增 `_build_chunk_visual_context_index()`

```python
def _build_chunk_visual_context_index(self) -> dict[str, dict[str, Any]]:
    digest = self._load_current_timeline_digest()
    out: dict[str, dict[str, Any]] = {}
    for item in digest.get("chunks", []) or []:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("chunk_id") or item.get("global_chunk_id") or "").strip()
        if not cid:
            continue
        out[cid] = {
            "chunk_id": cid,
            "source_id": item.get("source_id", ""),
            "visual_context": compact_text(str(item.get("visual") or item.get("visual_summary") or ""), max_chars=240),
            "screen_text": item.get("screen_text", []) if isinstance(item.get("screen_text"), list) else [],
            "visible_people": item.get("visible_people", []) if isinstance(item.get("visible_people"), list) else [],
        }
    return out
```

### 5.3 新增 `_build_asr_sentence_units_from_raw_asr()`

```python
def _build_asr_sentence_units_from_raw_asr(self) -> list[dict[str, Any]]:
    raw_index = self._load_source_raw_asr_index()
    visual_index = self._build_chunk_visual_context_index()
    cfg = self.config.raw.get("asr_micro_segment", {})
    max_chars = int(cfg.get("max_sentence_chars", 120) or 120)

    sentences: list[dict[str, Any]] = []

    for source in raw_index.get("sources") or []:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id") or "").strip() or "source_1"
        source_index = source.get("source_index") or 1
        raw_segments = source.get("asr_segments") or []
        raw_segments = [s for s in raw_segments if isinstance(s, dict)]
        raw_segments.sort(key=lambda s: float(s.get("start_seconds") or 0))

        for seg in raw_segments:
            text = str(seg.get("normalized_text") or seg.get("text") or seg.get("raw_text") or "").strip()
            if not text:
                continue
            start = self._time_value_seconds(seg.get("start_seconds"))
            end = self._time_value_seconds(seg.get("end_seconds"))
            if start is None or end is None or end <= start:
                continue

            # 第一版：raw ASR segment 就是 sentence unit；只有超长才在该 ASR segment 内按标点保守切分
            parts = [text]
            if len(text) > max_chars:
                parts = self._split_long_asr_text_for_sentence_units(text, max_chars=max_chars)

            total_chars = sum(len(p) for p in parts) or len(text)
            cursor = float(start)
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                part_duration = (float(end) - float(start)) * (len(part) / total_chars) if total_chars else 0
                part_start = cursor
                part_end = min(float(end), cursor + part_duration)
                cursor = part_end

                source_chunk_ids = [str(x) for x in (seg.get("source_chunk_ids") or []) if str(x).strip()]
                visuals = []
                screen_text: list[Any] = []
                visible_people: list[Any] = []
                for cid in source_chunk_ids:
                    ctx = visual_index.get(cid) or {}
                    v = str(ctx.get("visual_context") or "").strip()
                    if v and v not in visuals:
                        visuals.append(v)
                    for t in ctx.get("screen_text") or []:
                        if t not in screen_text:
                            screen_text.append(t)
                    for p in ctx.get("visible_people") or []:
                        if p not in visible_people:
                            visible_people.append(p)

                sentences.append({
                    "sentence_id": f"s{len(sentences) + 1:04d}",
                    "source_id": source_id,
                    "source_index": source_index,
                    "asr_segment_id": seg.get("asr_segment_id", ""),
                    "source_raw_asr_segment_id": seg.get("source_raw_asr_segment_id", ""),
                    "source_chunk_ids": source_chunk_ids,
                    "primary_chunk_id": seg.get("primary_chunk_id", ""),
                    "start_seconds": round(part_start, 3),
                    "end_seconds": round(part_end, 3),
                    "text": part,
                    "raw_text": seg.get("raw_text", ""),
                    "visual_context": compact_text("，".join(visuals), max_chars=int(cfg.get("visual_summary_chars", 160) or 160)),
                    "screen_text": screen_text,
                    "visible_people": visible_people,
                })
    return sentences
```

新增 `_split_long_asr_text_for_sentence_units()`：

```python
def _split_long_asr_text_for_sentence_units(self, text: str, *, max_chars: int) -> list[str]:
    text = str(text or "").strip()
    if not text or len(text) <= max_chars:
        return [text] if text else []
    parts = re.split(r"([。！？；.!?;])", text)
    units: list[str] = []
    current = ""
    for i in range(0, len(parts), 2):
        frag = parts[i]
        punct = parts[i + 1] if i + 1 < len(parts) else ""
        candidate = current + frag + punct
        if current and len(candidate) > max_chars:
            units.append(current.strip())
            current = (frag + punct).strip()
        else:
            current = candidate.strip()
    if current:
        units.append(current.strip())
    if not units:
        return [text]
    return units
```

注意：

```text
这里仍然有时间按字符比例拆分，但只发生在单条 raw ASR segment 内。
不再把 60 秒 chunk 的时间按摘要字数分配。
```

### 5.4 修改 `_materialize_micro_segments_from_sentence_groups()`

当前输出字段需要调整。用下面结构替换 append 部分。

```python
micro_segment_id = f"{source_id}_ms_{i + 1:04d}"
asr_segment_ids = []
source_chunk_ids = []
visuals = []
screen_text = []
visible_people = []

for s in s_list:
    aid = str(s.get("asr_segment_id") or "").strip()
    if aid and aid not in asr_segment_ids:
        asr_segment_ids.append(aid)
    for cid in s.get("source_chunk_ids") or []:
        if cid and cid not in source_chunk_ids:
            source_chunk_ids.append(cid)
    v = str(s.get("visual_context") or "").strip()
    if v and v not in visuals:
        visuals.append(v)
    for t in s.get("screen_text") or []:
        if t not in screen_text:
            screen_text.append(t)
    for p in s.get("visible_people") or []:
        if p not in visible_people:
            visible_people.append(p)

asr_text = "".join(str(s.get("text") or "") for s in s_list).strip()
visual_context = compact_text("，".join(visuals), max_chars=240)

segments.append({
    "micro_segment_id": micro_segment_id,
    "segment_id": micro_segment_id,  # 兼容旧读取
    "source_id": source_id,
    "source_index": source_index,
    "sentence_ids": sids,
    "asr_segment_ids": asr_segment_ids,
    "source_chunk_ids": source_chunk_ids,
    "source_start_seconds": round(start, 3),
    "source_end_seconds": round(end, 3),
    "start_seconds": round(start, 3),
    "end_seconds": round(end, 3),
    "source_start": seconds_to_timecode(start, ms=True),
    "source_end": seconds_to_timecode(end, ms=True),
    "duration_seconds": round(end - start, 3),
    "asr_text": asr_text,
    "visual_context": visual_context,
    "visual": visual_context,
    "visual_summary": visual_context,
    "screen_text": screen_text,
    "visible_people": visible_people,
    "materialized_by_code": True,
})
```

不要输出：

```text
clean_text
speech_summary
semantic_type
independent
needs_context
keep_candidate
```

除非后续代码确实需要。第一版不要加复杂字段。

### 5.5 修改 `_split_segment_by_sentence_boundary()` 和 `_merge_too_short_adjacent_segments()`

这两个函数现在会更新旧字段。需要同步更新新字段。

在拆分后补：

```python
sub_seg["source_start_seconds"] = round(start, 3)
sub_seg["source_end_seconds"] = round(end, 3)
sub_seg["start_seconds"] = round(start, 3)
sub_seg["end_seconds"] = round(end, 3)
sub_seg["source_start"] = seconds_to_timecode(start, ms=True)
sub_seg["source_end"] = seconds_to_timecode(end, ms=True)
sub_seg["asr_text"] = "".join(s["text"] for s in s_list).strip()
sub_seg["asr_segment_ids"] = list(dict.fromkeys(s.get("asr_segment_id", "") for s in s_list if s.get("asr_segment_id")))
sub_seg["source_chunk_ids"] = list(dict.fromkeys(cid for s in s_list for cid in (s.get("source_chunk_ids") or [])))
```

在合并太短 segment 时，除了旧字段，还要合并：

```python
nxt["source_start_seconds"] = current["source_start_seconds"]
nxt["start_seconds"] = current["start_seconds"]
nxt["source_start"] = current["source_start"]
nxt["asr_text"] = current.get("asr_text", "") + nxt.get("asr_text", "")
nxt["asr_segment_ids"] = list(dict.fromkeys((current.get("asr_segment_ids") or []) + (nxt.get("asr_segment_ids") or [])))
nxt["source_chunk_ids"] = list(dict.fromkeys((current.get("source_chunk_ids") or []) + (nxt.get("source_chunk_ids") or [])))
nxt["visual_context"] = compact_text("，".join(x for x in [current.get("visual_context", ""), nxt.get("visual_context", "")] if x), max_chars=240)
nxt["visual"] = nxt["visual_context"]
nxt["visual_summary"] = nxt["visual_context"]
```

鲁棒性：

```text
如果合并逻辑太复杂，第一版可以关闭 merge_too_short_adjacent_segments，只保留短段，让 content_analysis 自己不选。
不要因为短就跨 source 合并。
```

---

## 6. 修改 `pipeline.py`：content_analysis 明确只接受 selected_segments

这是用户特别指出的地方：必须写清楚具体怎么改。

### 6.1 当前问题函数

当前函数：

```python
def step_content_analysis(self) -> None:
    cfg = self.config.raw.get("asr_micro_segment", {})
    use_micro_segments = cfg.get("enabled", True)
    ...
    result = self._run_text_agent("content_analysis", text_input)

    if use_micro_segments and ("candidate_segments" in result or "candidate_clips" not in result):
        result = self._materialize_content_analysis_candidates_from_segments(result)
    else:
        result = self._materialize_candidate_clips_from_chunks(result, source_step="content_analysis")

    self._overwrite_step_json("content_analysis", result, materialized=True)
```

必须替换。

### 6.2 新增 `_load_micro_segments_or_raise()`

```python
def _load_micro_segments_or_raise(self) -> list[dict[str, Any]]:
    doc = self._load_step_json("asr_micro_segment")
    segments = doc.get("segments") if isinstance(doc, dict) else []
    if not isinstance(segments, list) or not segments:
        raise UserFacingPipelineError(
            "micro_segments_empty",
            user_message="AI 配音内容分析失败：没有可用 micro_segment。",
            suggestions=["回查 asr_micro_segment/v*/micro_segments.json。"],
            technical_detail={"step": "content_analysis"},
        )
    return [seg for seg in segments if isinstance(seg, dict)]
```

### 6.3 修改 `_build_content_analysis_micro_segment_text()`

当前函数仍用 `segment_id`、`摘要` 等。改为只喂模型需要的信息。

```python
def _build_content_analysis_micro_segment_text(self) -> str:
    micro_segments = self._load_micro_segments_or_raise()
    cfg = self.config.raw.get("asr_micro_segment", {})
    max_segments = int(cfg.get("max_segments_for_content_analysis", 120) or 120)

    lines = [
        "你将看到一组 AI 配音候选 micro_segments。",
        "每个 micro_segment_id 是唯一标识。",
        "只从这些 micro_segment_id 中选择适合 AI 配音短视频的片段。",
        "不要输出时间码。不要输出 chunk_id。不要输出 source_id。",
        "",
        "micro_segments:",
        "",
    ]

    for seg in micro_segments[:max_segments]:
        ms_id = str(seg.get("micro_segment_id") or seg.get("segment_id") or "").strip()
        if not ms_id:
            continue
        lines.append(f"[{ms_id}]")
        lines.append(f"source_id={seg.get('source_id', '')}")
        lines.append(f"duration={seg.get('duration_seconds', '')}")
        lines.append(f"声：{compact_text(str(seg.get('asr_text') or ''), max_chars=500)}")
        visual = str(seg.get("visual_context") or seg.get("visual") or seg.get("visual_summary") or "").strip()
        if visual:
            lines.append(f"画：{compact_text(visual, max_chars=300)}")
        lines.append("")

    lines.append('输出 JSON 格式：{"selected_segments":[{"micro_segment_id":"...","summary":"...","role":"fact"}]}')
    return "\n".join(lines).strip()
```

注意：

```text
1. 这里可以有提示性文字，但真正提示词由 prompt 文件控制。
2. input_data 只负责给足字段，不要把 chunk_id 暴露成可选 ID。
3. 不给 source_start/source_end，避免模型学着输出时间码。
```

### 6.4 新增 normalize 函数

```python
def _normalize_ai_voiceover_content_analysis_result(
    self,
    result: Any,
    micro_segments: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise UserFacingPipelineError(
            "content_analysis_invalid_output",
            user_message="AI 配音内容分析失败：模型输出不是 JSON object。",
            suggestions=["检查 content_analysis 提示词，输出必须包含 selected_segments。"],
            technical_detail={"raw_type": type(result).__name__},
        )

    raw_selected = result.get("selected_segments")
    if not isinstance(raw_selected, list):
        raise UserFacingPipelineError(
            "content_analysis_missing_selected_segments",
            user_message="AI 配音内容分析失败：模型没有输出 selected_segments。",
            suggestions=["检查 content_analysis 提示词输出格式。"],
            technical_detail={"keys": list(result.keys())},
        )

    seg_by_id = {
        str(seg.get("micro_segment_id") or seg.get("segment_id") or "").strip(): seg
        for seg in micro_segments
        if str(seg.get("micro_segment_id") or seg.get("segment_id") or "").strip()
    }

    allowed_roles = {"hook", "fact", "context", "evidence", "conflict", "development", "transition", "ending"}
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    invalid_items: list[dict[str, Any]] = []

    for index, item in enumerate(raw_selected, start=1):
        if not isinstance(item, dict):
            invalid_items.append({"index": index, "reason": "not_object", "value": item})
            continue
        ms_id = str(item.get("micro_segment_id") or item.get("segment_id") or item.get("id") or "").strip()
        if not ms_id:
            invalid_items.append({"index": index, "reason": "missing_micro_segment_id", "value": item})
            continue
        if ms_id not in seg_by_id:
            invalid_items.append({"index": index, "reason": "micro_segment_id_not_found", "micro_segment_id": ms_id})
            continue
        if ms_id in seen:
            continue
        seen.add(ms_id)

        role = str(item.get("role") or "fact").strip().lower()
        if role not in allowed_roles:
            role = "fact"

        selected.append({
            "micro_segment_id": ms_id,
            "summary": str(item.get("summary") or "").strip(),
            "role": role,
        })

    if not selected:
        raise UserFacingPipelineError(
            "content_analysis_selected_segments_empty",
            user_message="AI 配音内容分析失败：没有选出任何有效 micro_segment。",
            suggestions=[
                "检查 content_analysis 提示词是否过于严格。",
                "检查输入 micro_segments 是否有有效 ASR 文本。",
                "检查模型是否输出了不存在的 micro_segment_id。",
            ],
            technical_detail={
                "input_micro_segment_count": len(micro_segments),
                "raw_selected_count": len(raw_selected),
                "invalid_items": invalid_items[:50],
            },
        )

    return {
        "version": "ai_voiceover_content_analysis_v2",
        "input_unit": "micro_segment",
        "selected_segments": selected,
        "invalid_items": invalid_items,
        "materialized_by_code": True,
        "raw_model_plan": result,
    }
```

鲁棒性：

```text
1. 格式不对直接失败。
2. 输出不存在的 micro_segment_id 直接记录 invalid。
3. 全部无效直接失败。
4. 不 fallback chunk。
```

### 6.5 直接替换 `step_content_analysis()`

建议把函数改成下面结构。

```python
def step_content_analysis(self) -> None:
    cfg = self.config.raw.get("asr_micro_segment", {})
    use_micro_segments = bool(cfg.get("enabled", True))

    if self.options.production_mode == "ai_voiceover":
        if not use_micro_segments:
            raise UserFacingPipelineError(
                "ai_voiceover_micro_segment_disabled",
                user_message="AI 配音内容分析失败：当前方案要求启用 asr_micro_segment。",
                suggestions=["在 config.toml 中启用 [asr_micro_segment].enabled=true。"],
            )

        if self.manifest.get("steps", {}).get("asr_micro_segment", {}).get("status") != "success":
            raise UserFacingPipelineError(
                "asr_micro_segment_not_ready",
                user_message="AI 配音内容分析失败：asr_micro_segment 未成功完成。",
                suggestions=["先从 asr_micro_segment 步骤重跑。"],
            )

        micro_segments = self._load_micro_segments_or_raise()
        text_input = self._build_content_analysis_micro_segment_text()
        result = self._run_text_agent("content_analysis", text_input)
        normalized = self._normalize_ai_voiceover_content_analysis_result(result, micro_segments)
        self._overwrite_step_json("content_analysis", normalized, materialized=True)
        return

    # 非 AI 配音模式理论上不会进入 content_analysis。
    # 如果未来 legacy 单源流程还会进入，这里不要猜。
    raise UserFacingPipelineError(
        "content_analysis_unsupported_mode",
        user_message=f"content_analysis 不支持当前 production_mode={self.options.production_mode}。",
        suggestions=["检查 workflow_registry 是否把 content_analysis 放到了错误的 workflow。"],
    )
```

重要：

```text
这会移除 AI 配音模式下的 chunk fallback。
如果旧单源 AI 配音仍想支持 chunk content_analysis，需要另开 legacy 配置，不要默认启用。
```

### 6.6 `content_analysis_micro_segment` 提示词接入要求

`content_analysis_micro_segment` 提示词已经在用户的提示词优化文档中明确，本代码方案不再重新设计提示词内容。

代码层面必须做三件事：

```text
1. 新增 prompt 文件：
   newsclip_agent/prompt_texts/content_analysis_micro_segment.txt

2. 在 prompts.py 新增常量：
   CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT = _load_prompt("content_analysis_micro_segment.txt")

3. 把 AGENT_INFO["content_analysis"] 从旧 CONTENT_ANALYSIS_PROMPT 改成新常量。
```

`prompts.py` 推荐增加：

```python
# AI 配音 micro_segment 内容分析：从 micro_segments 中选择 selected_segments。
CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT = _load_prompt("content_analysis_micro_segment.txt")
```

`AGENT_INFO` 推荐改成：

```python
"content_analysis": (
    "agents/content_analysis",
    "content_analysis.json",
    prompts.CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT,
    "content_analysis_micro_segment_v1",
),
```

注意：

```text
1. 旧 CONTENT_ANALYSIS_PROMPT 可以暂时保留给 legacy 或历史兼容，但本轮 AI 配音统一多源链路不能再使用它。
2. 如果 content_analysis_micro_segment.txt 文件尚未落到仓库，开发必须先从提示词优化文档复制创建。
3. 如果运行时找不到 CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT，应 fail-fast。
4. 禁止继续用旧 content_analysis.txt 冒充 micro_segment selector。
```

---

## 7. 修改 `pipeline.py`：新增 `step_ai_voiceover_candidate_materialize()`

### 7.1 不加入 AGENT_INFO

这是纯代码步骤，不是 LLM，不要加到 `AGENT_INFO`。

### 7.2 新增 helper：source duration 查询

```python
def _source_duration_by_id(self) -> dict[str, float]:
    durations: dict[str, float] = {}
    for item in self.manifest.get("source_videos", []) or []:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("source_id") or "").strip()
        dur = self._time_value_seconds(item.get("duration_seconds") or item.get("original_duration_seconds"))
        if sid and dur is not None:
            durations[sid] = float(dur)
    if durations:
        return durations

    aggregate = self._load_optional_step_json("source_aggregate", {})
    for item in aggregate.get("sources") or []:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("source_id") or "").strip()
        dur = self._time_value_seconds(item.get("duration_seconds"))
        if sid and dur is not None:
            durations[sid] = float(dur)
    return durations
```

### 7.3 新增步骤函数

```python
def step_ai_voiceover_candidate_materialize(self) -> None:
    analysis = self._load_step_json("content_analysis")
    micro_doc = self._load_step_json("asr_micro_segment")
    cfg = self.config.raw.get("ai_voiceover_candidate", {})
    padding_before = float(cfg.get("padding_before_seconds", 0.5) or 0.5)
    padding_after = float(cfg.get("padding_after_seconds", 0.5) or 0.5)
    min_clip_seconds = float(cfg.get("min_clip_seconds", 3.0) or 3.0)
    max_clip_seconds = float(cfg.get("max_clip_seconds", 30.0) or 30.0)

    input_hash = stable_hash({
        "content_analysis": self._step_content_hash("content_analysis"),
        "asr_micro_segment": self._step_content_hash("asr_micro_segment"),
        "cfg": cfg,
        "version": "ai_voiceover_candidate_materialize_v1",
    })
    if self._can_reuse("ai_voiceover_candidate_materialize", input_hash):
        print("复用缓存: ai_voiceover_candidate_materialize")
        return

    version, vdir = self._version_dir("ai_voiceover_candidate_materialize", "ai_voiceover_candidates")
    ensure_dir(vdir)

    segments = micro_doc.get("segments") if isinstance(micro_doc, dict) else []
    if not isinstance(segments, list) or not segments:
        raise UserFacingPipelineError(
            "micro_segments_empty",
            user_message="AI 配音候选片段生成失败：没有 micro_segments。",
            suggestions=["回查 asr_micro_segment 输出。"],
        )

    seg_by_id = {
        str(seg.get("micro_segment_id") or seg.get("segment_id") or "").strip(): seg
        for seg in segments
        if isinstance(seg, dict) and str(seg.get("micro_segment_id") or seg.get("segment_id") or "").strip()
    }

    selected = analysis.get("selected_segments") if isinstance(analysis, dict) else []
    if not isinstance(selected, list) or not selected:
        raise UserFacingPipelineError(
            "selected_segments_empty",
            user_message="AI 配音候选片段生成失败：content_analysis 没有 selected_segments。",
            suggestions=["回查 agents/content_analysis/v*/content_analysis.json。"],
        )

    durations = self._source_duration_by_id()
    candidate_clips: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []

    for index, item in enumerate(selected, start=1):
        if not isinstance(item, dict):
            diagnostics.append({"index": index, "error": "selected_item_not_object"})
            continue
        ms_id = str(item.get("micro_segment_id") or "").strip()
        seg = seg_by_id.get(ms_id)
        if not seg:
            diagnostics.append({"index": index, "error": "micro_segment_not_found", "micro_segment_id": ms_id})
            continue

        source_id = str(seg.get("source_id") or "").strip()
        start = self._time_value_seconds(seg.get("source_start_seconds") or seg.get("start_seconds"))
        end = self._time_value_seconds(seg.get("source_end_seconds") or seg.get("end_seconds"))
        if not source_id or start is None or end is None or end <= start:
            diagnostics.append({
                "index": index,
                "error": "invalid_micro_segment_time",
                "micro_segment_id": ms_id,
                "source_id": source_id,
                "start": start,
                "end": end,
            })
            continue

        source_duration = durations.get(source_id)
        padded_start = max(0.0, float(start) - padding_before)
        padded_end = float(end) + padding_after
        if source_duration is not None and source_duration > 0:
            padded_end = min(float(source_duration), padded_end)

        duration = padded_end - padded_start
        if duration <= 0:
            diagnostics.append({"index": index, "error": "invalid_padded_time", "micro_segment_id": ms_id})
            continue

        # 不要为了 max_clip_seconds 强行截断事实。
        # 如果超过 max，只记录 warning；真正拆分应在 asr_micro_segment 阶段做。
        warnings = []
        if duration < min_clip_seconds:
            warnings.append("clip_shorter_than_min_clip_seconds")
        if max_clip_seconds > 0 and duration > max_clip_seconds:
            warnings.append("clip_longer_than_max_clip_seconds_should_fix_in_micro_segment")

        clip_id = f"av_clip_{len(candidate_clips) + 1:04d}"
        visual = str(seg.get("visual_context") or seg.get("visual") or seg.get("visual_summary") or "").strip()
        asr_text = str(seg.get("asr_text") or "").strip()
        summary = str(item.get("summary") or "").strip() or compact_text(asr_text, max_chars=120)
        role = str(item.get("role") or "fact").strip() or "fact"

        candidate_clips.append({
            "clip_id": clip_id,
            "candidate_type": "ai_voiceover",
            "derived_from": "micro_segment",
            "source_id": source_id,
            "source_index": seg.get("source_index"),
            "source_start_seconds": round(padded_start, 3),
            "source_end_seconds": round(padded_end, 3),
            "local_start_seconds": round(padded_start, 3),
            "local_end_seconds": round(padded_end, 3),
            "start_seconds": round(padded_start, 3),
            "end_seconds": round(padded_end, 3),
            "source_start": seconds_to_timecode(padded_start, ms=True),
            "source_end": seconds_to_timecode(padded_end, ms=True),
            "local_start": seconds_to_timecode(padded_start, ms=True),
            "local_end": seconds_to_timecode(padded_end, ms=True),
            "start": seconds_to_timecode(padded_start, ms=True),
            "end": seconds_to_timecode(padded_end, ms=True),
            "duration_seconds": round(duration, 3),
            "micro_segment_id": ms_id,
            "micro_segment_ids": [ms_id],
            "micro_segment_start_seconds": round(float(start), 3),
            "micro_segment_end_seconds": round(float(end), 3),
            "asr_segment_ids": seg.get("asr_segment_ids", []),
            "source_chunk_ids": seg.get("source_chunk_ids", []),
            "asr_text": asr_text,
            "speech": asr_text,
            "summary": summary,
            "role": role,
            "visual_context": visual,
            "visual": visual,
            "visual_summary": visual,
            "padding_before_seconds": padding_before,
            "padding_after_seconds": padding_after,
            "warnings": warnings,
        })

    if not candidate_clips:
        debug = {"selected_count": len(selected), "diagnostics": diagnostics}
        write_json(vdir / "materialize_debug.json", debug)
        raise UserFacingPipelineError(
            "ai_voiceover_candidate_materialize_empty",
            user_message="AI 配音候选片段生成失败：已选 micro_segment 无法物化为可剪辑片段。",
            suggestions=[
                "检查 selected_segments 中的 micro_segment_id 是否存在。",
                "检查 micro_segments.json 的 source_start_seconds/source_end_seconds。",
                "检查 source duration 和 padding 配置。",
            ],
            technical_detail=debug,
        )

    output = {
        "version": "ai_voiceover_candidate_clips_v1",
        "candidate_type": "ai_voiceover",
        "source": "micro_segment",
        "candidate_clips": candidate_clips,
        "diagnostics": diagnostics,
    }
    out = write_json(vdir / "candidate_clips.json", output)
    debug_out = write_json(vdir / "materialize_debug.json", {
        "selected_count": len(selected),
        "candidate_count": len(candidate_clips),
        "diagnostics": diagnostics,
    })

    status_doc = self._base_status("ai_voiceover_candidate_materialize", version, input_hash, [out, debug_out])
    self._write_status(vdir, status_doc)
    self._record_step(
        step="ai_voiceover_candidate_materialize",
        version=version,
        status="success",
        output=relpath(out, self.task_dir),
        input_hash=input_hash,
        output_files=[out, debug_out],
        extra={"summary": {"candidate_clips": len(candidate_clips), "diagnostics": len(diagnostics)}},
    )
    print("完成: ai_voiceover_candidate_materialize")
```

鲁棒性：

```text
1. selected_segments 为空不继续。
2. micro_segment_id 不存在不静默成功。
3. 全部物化失败直接失败。
4. 不强行截断超过 max_clip_seconds 的片段，避免破坏事实边界。
```

---

## 8. 修改候选池读取函数

### 8.1 修改 `_candidate_source_hash()`

当前 AI 配音 hash 是 `content_analysis`。

改成：

```python
def _candidate_source_hash(self) -> str:
    if self.options.production_mode == "highlight_reassembly":
        return self._step_content_hash("candidate_filter") or self._step_content_hash("asr_event_candidate")
    return self._step_content_hash("ai_voiceover_candidate_materialize")
```

### 8.2 修改 `_load_candidate_clips_for_current_mode()`

当前 AI 配音从 `content_analysis.candidate_clips` 读取。

改成：

```python
def _load_candidate_clips_for_current_mode(self) -> list[dict[str, Any]]:
    if self.options.production_mode == "highlight_reassembly":
        filtered = self._load_candidate_clip_pool(filtered=True)
        if filtered:
            return filtered
        pool = self._load_candidate_clip_pool(filtered=False)
        if pool:
            return pool
        return []

    if self.options.production_mode == "ai_voiceover":
        doc = self._load_optional_step_json("ai_voiceover_candidate_materialize", {})
        clips = doc.get("candidate_clips", []) if isinstance(doc, dict) else []
        if isinstance(clips, dict):
            clips = clips.get("candidate_clips", [])
        if not isinstance(clips, list):
            return []
        return [clip for clip in clips if isinstance(clip, dict)]

    return []
```

不要 fallback 到 content_analysis。

如果必须兼容旧任务，加显式配置：

```python
compat = self.config.raw.get("compat", {})
if compat.get("allow_ai_voiceover_candidate_from_content_analysis", False):
    ...旧读取...
```

默认 false。

链路影响：

```text
short_video_edit_plan 必须等 ai_voiceover_candidate_materialize 成功后才能读到 clips。
如果 materialize 没跑，clips 为空，short_video_edit_plan 会 fail-fast。
```

---

## 9. 修改 `pipeline.py`：short_video_edit_plan 严格化

### 9.1 修改 `_build_short_video_edit_plan_text()`

当前函数依赖 content_analysis 的 `main_topic/key_facts`。新版 content_analysis 不再输出这些字段。

将函数改成只基于 candidate clips。

```python
def _build_short_video_edit_plan_text(self) -> str:
    llm_cfg = self.config.raw.get("llm_input", {})
    max_clips = int(llm_cfg.get("max_llm_candidate_clips", llm_cfg.get("max_candidate_clips_for_edit_plan", 14)) or 14)
    clips = self._load_candidate_clips_for_current_mode()[:max_clips]

    if not clips:
        raise UserFacingPipelineError(
            "short_video_edit_plan_no_candidate_clips",
            user_message="AI 配音选片规划失败：没有可用 candidate_clips。",
            suggestions=["回查 ai_voiceover_candidates/v*/candidate_clips.json。"],
        )

    lines = [
        "任务：从候选 clips 中选择适合制作 AI 配音新闻短视频的 clips，并按最终成片顺序排列。",
        "只输出 clip_ids，不要输出时间码。",
        "",
        "运行参数：",
        f"- output_mode：{self.options.output_mode}",
        f"- max_output_videos：{self.options.max_output_videos}",
        f"- min_output_video_seconds：{self.options.min_output_video_seconds}",
        f"- max_output_video_seconds：{self.options.max_output_video_seconds}",
        f"- target_duration_seconds：{self.options.target_duration_seconds}",
        "",
        "候选 clips：",
    ]

    for index, clip in enumerate(clips):
        cid = self._clip_id(clip, index)
        lines.append(f"[{cid}]")
        lines.append(f"source_id={clip.get('source_id', '')}")
        lines.append(f"duration={clip.get('duration_seconds', '')}")
        if clip.get("role"):
            lines.append(f"role={clip.get('role')}")
        summary = str(clip.get("summary") or "").strip()
        if summary:
            lines.append(f"事实：{compact_text(summary, max_chars=300)}")
        speech = str(clip.get("asr_text") or clip.get("speech") or "").strip()
        if speech:
            lines.append(f"声音：{sanitize_llm_text(speech, max_chars=400)}")
        visual = str(clip.get("visual_context") or clip.get("visual") or clip.get("visual_summary") or "").strip()
        if visual:
            lines.append(f"画面：{compact_text(visual, max_chars=300)}")
        lines.append("")

    lines.append('输出 JSON 只允许数组，例如：[{"clip_ids":["av_clip_0001","av_clip_0003"]}]')
    return "\n".join(lines).strip()
```

不要把以下字段给模型当主输入：

```text
micro_segment_id
source_start/source_end
source_chunk_ids
asr_segment_ids
```

这些是代码追溯字段，不是选片主字段。

### 9.2 修改 `_materialize_short_video_edit_plan()`

#### 9.2.1 解析 raw_videos 后立即检查

当前有 fallback：

```python
if not raw_videos and clips_by_id:
    raw_videos = [{"clip_ids": list(clips_by_id)[:8], ...}]
```

删除这段，替换为：

```python
if not raw_videos:
    raise UserFacingPipelineError(
        "short_video_edit_plan_empty",
        user_message="AI 配音选片规划失败：模型没有输出任何视频方案。",
        suggestions=[
            "检查 short_video_edit_plan 提示词输出是否为数组。",
            "检查输出对象是否包含 clip_ids。",
        ],
        technical_detail={"raw_model_plan": result},
    )
```

#### 9.2.2 clip_id 缺失要失败

当前：

```python
selected = [clips_by_id[cid] for cid in clip_ids if cid in clips_by_id]
if not selected:
    continue
```

改成：

```python
if not clip_ids:
    raise UserFacingPipelineError(
        "short_video_edit_plan_missing_clip_ids",
        user_message="AI 配音选片规划失败：某个视频方案没有 clip_ids。",
        technical_detail={"video_index": video_index, "video": video},
    )

missing = [cid for cid in clip_ids if cid not in clips_by_id]
if missing:
    raise UserFacingPipelineError(
        "short_video_edit_plan_clip_id_not_found",
        user_message="AI 配音选片规划失败：模型选择了候选池不存在的 clip_id。",
        suggestions=["检查 short_video_edit_plan 输出是否只使用候选池中的 clip_id。"],
        technical_detail={
            "missing_clip_ids": missing,
            "available_clip_ids": list(clips_by_id.keys())[:100],
        },
    )

selected = [clips_by_id[cid] for cid in clip_ids]
```

#### 9.2.3 must_keep_fact_points 从 selected clip summary 生成

当前依赖：

```python
content.get("key_facts")
```

改成：

```python
must_keep = []
for clip in selected:
    text = str(clip.get("summary") or "").strip()
    if text and text not in must_keep:
        must_keep.append(text)
```

#### 9.2.4 editing_structure 增加追溯字段

当前每个 shot 已有 source/time/fact/visual。增加：

```python
"micro_segment_ids": clip.get("micro_segment_ids", []),
"micro_segment_id": clip.get("micro_segment_id", ""),
"asr_segment_ids": clip.get("asr_segment_ids", []),
"source_chunk_ids": clip.get("source_chunk_ids", []),
"candidate_type": clip.get("candidate_type", ""),
"derived_from": clip.get("derived_from", ""),
```

并调整：

```python
visual = str(clip.get("visual_context") or clip.get("visual") or clip.get("visual_summary") or "").strip()
fact = str(clip.get("summary") or clip.get("asr_text") or clip.get("speech") or "").strip()
section = str(clip.get("role") or ("hook" if shot_index == 1 else "fact")).strip()
narration_intent = section
```

不要使用旧 `content.main_topic` 作为默认选片讲述重点。

#### 9.2.5 scripts 为空必须失败

在 return 前加：

```python
if not scripts:
    raise UserFacingPipelineError(
        "short_video_edit_plan_no_scripts",
        user_message="AI 配音选片规划失败：没有生成任何可用短视频结构。",
        suggestions=[
            "检查模型输出 clip_ids 是否为空。",
            "检查 candidate_clips 是否能被 clip_id 匹配。",
        ],
        technical_detail={"raw_model_plan": result},
    )
```

链路影响：

```text
1. 模型输出错 ID 会直接停在 short_video_edit_plan。
2. 不会再生成 scripts=[] success。
3. voiceover_script / tts 不会收到空输入。
```

---

## 10. 修改 voiceover_script 输入构造

### 10.1 先定位当前函数，不要猜

开发时先在 `pipeline.py` 搜索：

```text
step_voiceover_script
VOICEOVER_SCRIPT_TEXT_PROMPT
voiceover_script
narration_segments
```

必须找到当前 voiceover_script 的输入构造函数或内联代码。

如果没有独立 builder，新增：

```python
def _build_voiceover_script_input_from_edit_plan(self, edit_plan: dict[str, Any]) -> dict[str, Any]:
    ...
```

### 10.2 新增 builder 具体代码

```python
def _build_voiceover_script_input_from_edit_plan(self, edit_plan: dict[str, Any]) -> dict[str, Any]:
    scripts = edit_plan.get("scripts") if isinstance(edit_plan, dict) else []
    if not isinstance(scripts, list) or not scripts:
        raise UserFacingPipelineError(
            "voiceover_script_no_edit_plan_scripts",
            user_message="AI 配音文案生成失败：short_video_edit_plan 没有 scripts。",
            suggestions=["回查 agents/short_video_edit_plan/v*/short_video_edit_plan.json。"],
        )

    payload_scripts = []
    for index, script in enumerate(scripts, start=1):
        if not isinstance(script, dict):
            continue
        sid = str(script.get("short_video_id") or f"v_{index:03d}")
        editing_structure = script.get("editing_structure") or []
        if not isinstance(editing_structure, list) or not editing_structure:
            raise UserFacingPipelineError(
                "voiceover_script_empty_editing_structure",
                user_message="AI 配音文案生成失败：存在空的 editing_structure。",
                technical_detail={"short_video_id": sid},
            )

        shots = []
        for shot in editing_structure:
            if not isinstance(shot, dict):
                continue
            shot_id = str(shot.get("shot_id") or "").strip()
            if not shot_id:
                raise UserFacingPipelineError(
                    "voiceover_script_missing_shot_id",
                    user_message="AI 配音文案生成失败：editing_structure 存在缺失 shot_id 的镜头。",
                    technical_detail={"short_video_id": sid, "shot": shot},
                )
            target_duration = float(shot.get("target_duration_seconds") or shot.get("duration_seconds") or 0)
            shots.append({
                "shot_id": shot_id,
                "visual_summary": str(shot.get("visual_summary") or shot.get("visual") or "").strip(),
                "fact": str(shot.get("fact") or shot.get("news_fact_to_explain") or "").strip(),
                "narration_intent": str(shot.get("narration_intent") or shot.get("section") or "fact").strip(),
                "target_duration_seconds": round(target_duration, 3),
                "narration_min_chars": int(shot.get("narration_min_chars") or shot.get("min_chars") or 0),
                "narration_target_chars": int(shot.get("narration_target_chars") or shot.get("target_chars") or 0),
                "narration_max_chars": int(shot.get("narration_max_chars") or shot.get("max_chars") or 0),
            })

        payload_scripts.append({
            "short_video_id": sid,
            "angle": str(script.get("news_angle") or script.get("voiceover_brief") or "").strip(),
            "required_facts": script.get("must_keep_fact_points") or [],
            "target_duration_seconds": script.get("target_duration_seconds") or self.options.target_duration_seconds,
            "visual_total_seconds": script.get("visual_total_seconds") or editing_structure_duration(editing_structure),
            "shots": shots,
        })

    if not payload_scripts:
        raise UserFacingPipelineError(
            "voiceover_script_input_empty",
            user_message="AI 配音文案生成失败：没有可用 shots。",
        )

    return {"scripts": payload_scripts}
```

### 10.3 修改 `step_voiceover_script()`

当前实现不在本方案直接贴全量替换，因为需要先定位原函数。改造原则是：

```python
edit_plan = self._load_step_json("short_video_edit_plan")
input_data = self._build_voiceover_script_input_from_edit_plan(edit_plan)
result = self._run_text_agent("voiceover_script", input_data)
# normalize + validate
```

必须在 `_run_text_agent` 后校验：

```python
self._normalize_voiceover_scripts(result)
self._validate_voiceover_alignment_or_raise(result, edit_plan)
```

如果当前校验函数名称不同，以现有实际函数为准，但规则必须满足：

```text
1. scripts 非空。
2. 每个 short_video_id 对得上。
3. 每个 input shot_id 有且只有一个 narration_segment。
4. 不允许 extra shot_id。
5. text 不为空。
6. narration_text 可由 narration_segments 重建。
```

如果现有 `_validate_voiceover_alignment_or_raise()` 依赖旧字段，需要改到读取新版 `edit_plan.scripts[].editing_structure`。

---

## 11. 修改 `pipeline.py`：TTS fail-fast

### 11.1 在 `_step_tts_impl()` 开头加 scripts 为空检查

当前：

```python
scripts = voiceover.get("scripts", []) if isinstance(voiceover, dict) else []
```

后面立即加：

```python
tts_is_hard_required = self.options.require_tts and self.options.audio_policy in {"ai_voiceover", "mixed"}
if tts_is_hard_required and not scripts:
    raise UserFacingPipelineError(
        "tts_no_voiceover_scripts",
        user_message="TTS 生成失败：AI 配音文案为空。",
        suggestions=[
            "回查 agents/voiceover_script/v*/voiceover_script.json。",
            "回查 agents/short_video_edit_plan/v*/short_video_edit_plan.json。",
        ],
    )
```

注意：当前函数后面又定义了 `tts_is_hard_required`。要避免重复定义：把后面的定义删掉或复用前面的变量。

### 11.2 outputs 为空必须失败

当前：

```python
failed = sum(...)
success = sum(...)
tts_is_hard_required = ...
overall_status = "success" if failed == 0 else ...
```

改成：

```python
failed = sum(1 for item in outputs if item.get("status") == "failed")
success = sum(1 for item in outputs if item.get("status") == "success")

if tts_is_hard_required and not outputs:
    overall_status = "failed"
    hard_error = "TTS required but outputs is empty"
elif tts_is_hard_required and success == 0:
    overall_status = "failed"
    hard_error = "TTS required but no successful output"
else:
    overall_status = "success" if failed == 0 else ("failed" if tts_is_hard_required else "partial_success")
    hard_error = ""
```

记录 step 后：

```python
if overall_status == "failed":
    raise UserFacingPipelineError(
        "tts_failed_or_empty",
        user_message="TTS 生成失败：没有生成可用配音音频。",
        suggestions=["检查 voiceover_script 文案是否为空。", "检查 TTS 模型日志。"],
        technical_detail={"success": success, "failed": failed, "total": len(outputs), "error": hard_error},
    )
```

这样不会再出现：

```text
tts success total=0
```

---

## 12. 修改 cut_plan 前置检查

### 12.1 新增 helper

```python
def _validate_ai_voiceover_inputs_before_cut_plan(
    self,
    edit_plan: dict[str, Any],
    voiceover: dict[str, Any],
    tts: dict[str, Any],
) -> None:
    scripts = edit_plan.get("scripts") if isinstance(edit_plan, dict) else []
    voice_scripts = voiceover.get("scripts") if isinstance(voiceover, dict) else []
    tts_outputs = tts.get("outputs") if isinstance(tts, dict) else []

    issues = []
    if not isinstance(scripts, list) or not scripts:
        issues.append("short_video_edit_plan.scripts is empty")
    if not isinstance(voice_scripts, list) or not voice_scripts:
        issues.append("voiceover_script.scripts is empty")
    if self.options.require_tts and self.options.audio_policy in {"ai_voiceover", "mixed"}:
        if not isinstance(tts_outputs, list) or not tts_outputs:
            issues.append("tts.outputs is empty")

    for script in scripts or []:
        sid = script.get("short_video_id", "")
        shots = script.get("editing_structure") or []
        if not shots:
            issues.append(f"{sid}: editing_structure is empty")
            continue
        for shot in shots:
            if not isinstance(shot, dict):
                continue
            shot_id = shot.get("shot_id", "")
            if not shot.get("source_id"):
                issues.append(f"{sid}/{shot_id}: missing source_id")
            start = self._time_value_seconds(shot.get("source_start_seconds") or shot.get("source_start"))
            end = self._time_value_seconds(shot.get("source_end_seconds") or shot.get("source_end"))
            if start is None or end is None or end <= start:
                issues.append(f"{sid}/{shot_id}: invalid source time")

    if issues:
        raise UserFacingPipelineError(
            "cut_plan_ai_voiceover_input_invalid",
            user_message="生成剪辑计划失败：AI 配音上游结构不完整。",
            suggestions=[
                "回查 ai_voiceover_candidates、short_video_edit_plan、voiceover_script、tts。",
            ],
            technical_detail={"issues": issues[:100]},
        )
```

### 12.2 在 `step_cut_plan()` 开头调用

找到 `step_cut_plan()` 后，在读取 edit_plan / voiceover / tts 后加：

```python
if self.options.production_mode == "ai_voiceover":
    self._validate_ai_voiceover_inputs_before_cut_plan(edit_plan, voiceover, tts)
```

不要等到 `news_quality_gate` 才发现空结构。

---

## 13. 修改错误提示：news_quality_gate 按模式区分

当前 `_reassembly_error("quality_gate_failed")` 的建议偏向视频重组。

修改方式：

```python
def _quality_gate_failed_suggestion(self) -> str:
    if self.options.production_mode == "ai_voiceover":
        return "回查 ai_voiceover_candidates、short_video_edit_plan、voiceover_script、cut_plan。"
    return "回查 reassembly_cut_plan 或 candidate_clip_pool_filtered。"
```

在 `step_news_quality_gate()` 生成 `diagnostic_error` 时使用该 suggestion。

如果 `_reassembly_error()` 是通用函数，不想增加 self 依赖，就在调用处覆盖：

```python
diagnostic_error = self._reassembly_error("quality_gate_failed", issues=issues)
if self.options.production_mode == "ai_voiceover":
    diagnostic_error["suggestion"] = "回查 ai_voiceover_candidates、short_video_edit_plan、voiceover_script、cut_plan。"
```

---

## 14. 修改 `config.toml`

新增：

```toml
[ai_voiceover_candidate]
padding_before_seconds = 0.5
padding_after_seconds = 0.5
min_clip_seconds = 3.0
max_clip_seconds = 30.0
fail_on_empty = true

[compat]
allow_ai_voiceover_candidate_from_content_analysis = false
allow_asr_micro_segment_from_timeline_digest = false
```

修改 `[asr_micro_segment]`：

```toml
source = "raw_asr"
fail_on_empty = true
max_sentence_chars = 120
min_segment_seconds = 3.0
hard_max_segment_seconds = 25.0
split_overlong_group = true
max_segments_for_content_analysis = 120
visual_summary_chars = 160
```

注意：

```text
allow_asr_micro_segment_from_timeline_digest 默认 false。
如果 raw ASR index 缺失，应失败，不应回退 digest。
```

---

## 15. prompts.py 与提示词文件

### 15.1 asr_micro_segment

用户新版提示词要求输入：

```text
[s0001]
语音文本内容
```

代码已经按这个格式生成。

### 15.2 short_video_edit_plan

用户新版提示词要求输出：

```json
[
  {
    "clip_ids": ["clip_001"]
  }
]
```

代码 `_materialize_short_video_edit_plan()` 必须接受 list，并严格校验 `clip_ids`。

### 15.3 voiceover_script

用户新版提示词要求输入 scripts/shots 结构，并输出：

```json
{
  "scripts": [
    {
      "short_video_id": "v_001",
      "narration_text": "...",
      "narration_segments": [
        {"shot_id": "v_001_s01", "text": "..."}
      ]
    }
  ]
}
```

代码输入 builder 必须匹配该结构。

### 15.4 content_analysis_micro_segment

当前用户新版提示词文档没有 `content_analysis_micro_segment`，但新版代码协议必须依赖它。

开发时必须新增：

```text
newsclip_agent/prompt_texts/content_analysis_micro_segment.txt
prompts.CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT
```

并把 `AGENT_INFO["content_analysis"]` 从旧 chunk prompt 改到新 prompt：

```python
"content_analysis": (
    "agents/content_analysis",
    "content_analysis.json",
    prompts.CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT,
    "content_analysis_micro_segment_v1",
),
```

如果 `content_analysis_micro_segment.txt` 尚未从提示词优化文档落到仓库，开发应先创建该文件。运行时仍找不到时，程序应在启动或运行 content_analysis 前失败，不允许继续使用旧的：

```python
prompts.CONTENT_ANALYSIS_PROMPT
```

`prompts.py` 需要保留旧常量也可以，但本轮统一多源 AI 配音主链路的 `AGENT_INFO["content_analysis"]` 必须绑定新常量。

最低输出协议：

```json
{
  "selected_segments": [
    {
      "micro_segment_id": "source_001_ms_0001",
      "summary": "这段表达的新闻事实",
      "role": "fact"
    }
  ]
}
```

禁止输出和依赖：

```text
1. 禁止让模型输出时间码。
2. 禁止让模型输出 source_start/source_end。
3. 禁止让模型输出 chunk_id 作为主引用。
4. 禁止让模型输出 candidate_clips。
5. candidate_clips 只能由 ai_voiceover_candidate_materialize 生成。
```

---

## 16. 导出和 UI 适配

### 16.1 导出必须包含

```text
source_aggregate/v*/source_raw_asr_index.json
agents/asr_micro_segment/v*/asr_sentences.json
agents/asr_micro_segment/v*/micro_segments.json
agents/content_analysis/v*/input.txt
agents/content_analysis/v*/model_output.json
agents/content_analysis/v*/content_analysis.json
ai_voiceover_candidates/v*/candidate_clips.json
ai_voiceover_candidates/v*/materialize_debug.json
agents/short_video_edit_plan/v*/input.txt
agents/short_video_edit_plan/v*/model_output.json
agents/short_video_edit_plan/v*/short_video_edit_plan.json
agents/voiceover_script/v*/input.json 或 input.txt
agents/voiceover_script/v*/model_output.json
agents/voiceover_script/v*/voiceover_script.json
tts/omnivoice/v*/tts_outputs.json
edit/cut_plan/v*/cut_plan.json
quality/news_quality_gate/v*/news_quality_gate.json
```

### 16.2 zip 路径分隔符

如果导出 zip 使用 Windows 路径，必须改为：

```python
arcname = relative_path.as_posix()
```

不能再出现反斜杠变成 zip 内单个文件名。

---

## 17. 开发验证清单

### 17.1 source_aggregate 验证

必须看到：

```text
source_aggregate/v*/source_raw_asr_index.json
```

并且：

```text
raw_asr_segment_count > 0
每条 ASR 有 source_id/start_seconds/end_seconds/text/source_chunk_ids
```

### 17.2 asr_micro_segment 验证

必须看到：

```text
asr_sentences.json 的句子来自 raw_asr_index
micro_segments.json 的 start/end 来自 raw ASR，不来自 chunk 字数比例
```

### 17.3 content_analysis 验证

必须看到：

```json
{
  "version": "ai_voiceover_content_analysis_v2",
  "input_unit": "micro_segment",
  "selected_segments": []
}
```

`selected_segments` 必须非空。为空就失败。

### 17.4 candidate materialize 验证

必须看到：

```text
ai_voiceover_candidates/v*/candidate_clips.json
```

每个 candidate：

```text
clip_id
source_id
source_start_seconds
source_end_seconds
duration_seconds
summary
visual
micro_segment_ids
asr_segment_ids
source_chunk_ids
```

### 17.5 short_video_edit_plan 验证

模型输出只能是：

```json
[{"clip_ids":["av_clip_0001"]}]
```

代码物化后：

```text
scripts 非空
editing_structure 非空
每个 shot 有 source_id/source_start/source_end/shot_id/source_clip_id
```

### 17.6 voiceover / TTS 验证

```text
voiceover_script.scripts > 0
每个 shot_id 都有 narration_segment
text 非空
tts.outputs > 0
tts.outputs[].segments 覆盖 shot_id
```

### 17.7 不允许出现

```text
voiceover_script success scripts=0
tts success total=0
cut_plan success 但 output_videos clips 为空
news_quality_gate 才发现空结构
```

---

## 18. 本轮不再作为用户确认项，而是作为开发阻断条件

### 18.1 content_analysis_micro_segment 代码接入缺失

提示词内容已经在提示词优化文档中存在，本轮不再把“提示词是否设计”作为待确认项。

但如果仓库代码没有完成以下接入，仍然是开发阻断条件：

```text
newsclip_agent/prompt_texts/content_analysis_micro_segment.txt
prompts.CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT
AGENT_INFO["content_analysis"] 绑定 prompts.CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT
```

禁止行为：

```text
1. 用旧 content_analysis.txt 冒充 micro_segment selector。
2. 让旧 CONTENT_ANALYSIS_PROMPT 输出 candidate_clips。
3. content_analysis 输出为空后 fallback 到 chunk。
4. content_analysis 直接生成 candidate_clips，绕过 ai_voiceover_candidate_materialize。
```

### 18.2 `clean_asr_text()` 是否清理过强

这不是本轮必须改的内容，但开发必须检查，不得扩大清理范围。

本轮原则：

```text
1. raw_asr_index 保留 raw_text/text/removed_tokens。
2. micro_segment 输入优先使用 normalized_text 或 text。
3. 不删除有效新闻事实。
4. 不在 micro_segment 阶段改写 ASR 事实。
```

### 18.3 是否允许 micro_segment 跨 chunk

默认允许同 source 内跨 chunk。

要求：

```text
1. 跨 chunk 时记录多个 source_chunk_ids。
2. 时间边界仍来自 raw ASR。
3. visual_context 可以拼接多个 chunk 的视觉摘要。
4. 禁止跨 source 合并。
```

### 18.4 旧 AI 配音 chunk 模式是否保留

本轮默认不保留隐式 chunk fallback。

如果未来要保留，只能通过显式配置打开：

```toml
[compat]
allow_legacy_ai_voiceover_chunk_content_analysis = true
```

默认必须是：

```toml
[compat]
allow_legacy_ai_voiceover_chunk_content_analysis = false
```

并且本轮验收不覆盖 legacy chunk 模式。

---

## 19. 给 Codex 的执行要求

```text
1. 不重写整个 pipeline.py。
2. 不修改视频重组 workflow。
3. 不让 AI 配音 fallback 到 chunk content_analysis。
4. 不让 asr_micro_segment fallback 到 timeline_digest.speech。
5. 不让 short_video_edit_plan 默认选前几个 clip。
6. 不让 TTS outputs=0 success。
7. 不扩大 clean_asr_text 的语义清理。
8. 所有新增步骤必须写 step_status、manifest、output_files。
9. 所有新增产物必须进入失败导出包。
10. 如果 content_analysis_micro_segment 仓库文件、prompts.py 常量或 AGENT_INFO 绑定缺失，停止并提示，不要用旧 prompt 继续。
```



---

## 19.1 代码审查重点补充

提交代码后，重点检查以下位置，避免“看起来跑通、实际仍走旧链路”：

```text
1. AGENT_INFO["content_analysis"] 是否已经绑定 CONTENT_ANALYSIS_MICRO_SEGMENT_PROMPT。
2. step_content_analysis() 是否已经删除 AI 配音模式下的 chunk fallback。
3. _materialize_content_analysis_candidates_from_segments() 是否不再作为主链路生成 candidate_clips。
4. step_ai_voiceover_candidate_materialize() 是否是唯一生成 AI 配音 candidate_clips 的地方。
5. _load_candidate_clips_for_current_mode() 是否在 ai_voiceover 模式只读 ai_voiceover_candidate_materialize。
6. _materialize_short_video_edit_plan() 是否删除“模型空输出自动选前 N 个 clip”的兜底。
7. step_voiceover_script() 是否校验 shot_id 覆盖和 text 非空。
8. step_tts / cut_plan 是否对空 scripts、空 narration_segments、空 outputs、空 clips fail-fast。
9. 直接 run_pipeline.py --input 是否被明确排除在本轮验收之外，避免误判。
```

---

## 20. 本轮修订后的最终验收口径

本轮只验收统一多源 AI 配音链路。即使只有 1 个输入视频，也必须按下面链路运行：

```text
source_prepare
source_analysis
source_aggregate
asr_micro_segment
content_analysis
ai_voiceover_candidate_materialize
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

最终必须满足：

```text
1. source_aggregate 生成 source_raw_asr_index.json。
2. asr_micro_segment 只从 raw ASR index 构造 sentence units。
3. content_analysis 只输出 selected_segments。
4. ai_voiceover_candidate_materialize 只根据 selected_segments + micro_segments 生成 candidate_clips。
5. short_video_edit_plan 只读取 ai_voiceover_candidate_materialize 的 candidate_clips。
6. voiceover_script 必须覆盖全部 shot_id。
7. require_tts=true 时，tts.outputs 必须非空。
8. cut_plan 不能接收空 editing_structure、空 voiceover_script、空 tts。
9. news_quality_gate 不再作为空链路兜底报错点。
```

最终禁止：

```text
1. asr_micro_segment fallback 到 timeline_digest.speech。
2. content_analysis fallback 到 chunk candidate。
3. short_video_edit_plan 模型空输出后自动选前 N 个 clip。
4. voiceover_script success 但 scripts=0。
5. tts success 但 outputs=0。
6. cut_plan success 但 clips=0。
```
