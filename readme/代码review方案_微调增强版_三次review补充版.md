可以，API Key 这条我先不管，按你要求保留写死。下面只围绕**代码怎么具体改**，按优先级给你一份可以直接交给 Codex / 开发执行的方案。

> 本版是在原 review 方案基础上的微调增强版，不大删大改。核心补强点：先明确第一阶段/第二阶段边界，避免 Codex 一次改太多；对 fallback、截断、legacy、semantic retry、自检和失败导出包补充更严格的执行约束。

## 先按两阶段执行，避免一次改太散

第一阶段只修主链路阻断项，优先保证 AI 配音 micro_segment 主链路不再“表面 success、语义不可信”：

```text
1. asr_micro_segment 禁止默认 fallback，空 groups / valid_groups 为空必须 fail-fast。
2. candidate_materialize 默认永不截断，只记录 overlong。
3. content_analysis 输入/输出统一 micro_segment_id。
4. short_video_edit_plan 空业务结果做 semantic retry，仍空则 fail-fast。
5. voiceover_script / tts / cut_plan 保持空结果 fail-fast。
```

第二阶段再补诊断、兼容边界和排查体验：

```text
6. raw_asr_index diagnostics。
7. max_sentence_chars 在 sentence_units 生效。
8. legacy content_analysis candidate 函数隔离。
9. legacy CLI 单源阻断，但不能误伤 common_only 子任务。
10. 新增链路自检、失败导出包和运行态 flags。
```

---

# 一、修 `asr_micro_segment` 空 groups 自动兜底的问题

## 当前问题

现在 `_dedupe_and_validate_sentence_groups()` 里有这段逻辑：模型没返回某些 sentence，就把 missing sentence 全部补成 fallback group。这样会导致模型即使返回空 `groups`，代码也会自动把所有 ASR 句子转成 micro_segment，步骤仍然 success。

这和你的目标冲突：**micro_segment 失败就应该暴露失败，而不是静默补全。**

---

## 修改目标

改成：

```text
1. 默认禁止 fallback 补全 missing sentences。
2. 模型返回空 groups 或 valid_groups 为空时直接 fail-fast。
3. 如果模型返回了部分有效 groups，不要求覆盖全部 sentence；未覆盖的 sentence 只进入 diagnostics，不自动失败。
4. 如果将来临时排查需要 fallback，必须通过 config 显式开启。
5. fallback_used 必须写入 diagnostics，不能伪装成正常结果。
```

---

## 1.1 修改 `config.toml`

在 `[asr_micro_segment]` 下新增：

```toml
allow_sentence_fallback = false
fail_on_empty_groups = true
```

最终类似：

```toml
[asr_micro_segment]
enabled = true
source = "raw_asr"
fail_on_empty = true
fail_on_empty_groups = true
allow_sentence_fallback = false
min_segment_seconds = 3.0
preferred_min_segment_seconds = 8
preferred_max_segment_seconds = 25
hard_max_segment_seconds = 35.0
window_sentence_count = 40
window_overlap_sentence_count = 5
max_sentence_chars = 120
max_segments_for_content_analysis = 120
include_visual_summary = true
visual_summary_chars = 160
split_overlong_group = true
drop_empty_text_segment = true
```

---

## 1.2 改 `_dedupe_and_validate_sentence_groups()`

现在函数签名是：

```python
def _dedupe_and_validate_sentence_groups(self, groups: list[Any], sentences: list[dict[str, Any]]) -> list[dict[str, Any]]:
```

改成：

```python
def _dedupe_and_validate_sentence_groups(
    self,
    groups: list[Any],
    sentences: list[dict[str, Any]],
    *,
    allow_sentence_fallback: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
```

然后把函数整体改成这种结构：

```python
def _dedupe_and_validate_sentence_groups(
    self,
    groups: list[Any],
    sentences: list[dict[str, Any]],
    *,
    allow_sentence_fallback: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    valid_groups: list[dict[str, Any]] = []
    used_sids: set[str] = set()
    smap = {str(s.get("sentence_id")): s for s in sentences if s.get("sentence_id")}

    raw_group_count = len(groups) if isinstance(groups, list) else 0
    invalid_group_count = 0

    for g in groups or []:
        if isinstance(g, list):
            sids = g
            group_data: dict[str, Any] = {}
        elif isinstance(g, dict):
            sids = g.get("sentence_ids", [])
            group_data = dict(g)
        else:
            invalid_group_count += 1
            continue

        if not isinstance(sids, list):
            invalid_group_count += 1
            continue

        clean_sids: list[str] = []
        for sid in sids:
            sid = str(sid).strip()
            if sid in smap and sid not in used_sids:
                clean_sids.append(sid)
                used_sids.add(sid)

        if not clean_sids:
            invalid_group_count += 1
            continue

        valid_group = dict(group_data)
        valid_group["sentence_ids"] = clean_sids
        valid_groups.append(valid_group)

    missing_sentence_ids = [
        str(s.get("sentence_id"))
        for s in sentences
        if str(s.get("sentence_id")) not in used_sids
    ]

    fallback_used = False

    if allow_sentence_fallback and missing_sentence_ids:
        fallback_used = True
        for sid in missing_sentence_ids:
            s = smap.get(sid)
            if not s:
                continue
            valid_groups.append({
                "group_id": f"fallback_{sid}",
                "sentence_ids": [sid],
                "summary": str(s.get("text") or "")[:40],
                "segment_type": "fact",
                "keep_candidate": 1,
                "fallback_generated": True,
            })

    valid_groups.sort(
        key=lambda g: (
            str((smap.get(g["sentence_ids"][0]) or {}).get("source_id") or ""),
            float((smap.get(g["sentence_ids"][0]) or {}).get("start_seconds") or 0),
        )
    )

    diagnostics = {
        "raw_group_count": raw_group_count,
        "valid_group_count": len(valid_groups),
        "invalid_group_count": invalid_group_count,
        "sentence_count": len(sentences),
        "used_sentence_count": len(used_sids),
        "missing_sentence_count": len(missing_sentence_ids),
        "missing_sentence_ids": missing_sentence_ids[:200],
        "allow_sentence_fallback": allow_sentence_fallback,
        "fallback_used": fallback_used,
    }

    return valid_groups, diagnostics
```

补充约束：

```text
groups=[]：失败。
valid_groups=[]：失败。
missing_sentence_count>0：只记录 diagnostics，不失败。
allow_sentence_fallback=true：仅用于本地调试或临时排查，不作为生产默认值。
```

---

## 1.3 改 `step_asr_micro_segment()`

现在这里是：

```python
groups = self._dedupe_and_validate_sentence_groups(groups, sentences)
segments = self._materialize_micro_segments_from_sentence_groups(groups, sentences)
```

改成：

```python
allow_sentence_fallback = bool(cfg.get("allow_sentence_fallback", False))
fail_on_empty_groups = bool(cfg.get("fail_on_empty_groups", True))

groups, group_diagnostics = self._dedupe_and_validate_sentence_groups(
    groups,
    sentences,
    allow_sentence_fallback=allow_sentence_fallback,
)

write_json(vdir / "group_diagnostics.json", group_diagnostics)

if fail_on_empty_groups and not groups:
    raise UserFacingPipelineError(
        "asr_micro_segment_groups_empty",
        user_message="ASR 微分段失败：模型没有返回可用 groups，不能自动把所有 ASR 句子当作 micro_segment。",
        suggestions=[
            "回查 agents/asr_micro_segment/v*/raw_response.json。",
            "检查 asr_micro_segment prompt 是否要求输出 groups。",
            "如果只是临时排查，可显式设置 allow_sentence_fallback=true，但不建议作为生产默认值。",
        ],
        technical_detail=group_diagnostics,
    )

segments = self._materialize_micro_segments_from_sentence_groups(groups, sentences)
```

同时把最终输出改成带 diagnostics：

```python
output = {
    "version": "asr_micro_segment_raw_asr_v2",
    "segments": segments,
    "diagnostics": group_diagnostics,
}
```

---

## 1.4 修改输出文件列表

现在 `_record_step()` 只记录了 `micro_segments.json`。建议把几个调试文件也放进去：

```python
out = write_json(vdir / "micro_segments.json", output)

self._write_status(
    vdir,
    self._base_status(
        "asr_micro_segment",
        version,
        input_hash,
        [
            out,
            vdir / "asr_sentences.json",
            vdir / "raw_response.json",
            vdir / "group_diagnostics.json",
        ],
    ),
)

self._record_step(
    step="asr_micro_segment",
    version=version,
    status="success",
    output=relpath(out, self.task_dir),
    input_hash=input_hash,
    output_files=[
        out,
        vdir / "asr_sentences.json",
        vdir / "raw_response.json",
        vdir / "group_diagnostics.json",
    ],
    extra={
        "summary": {
            "segments": len(segments),
            "groups": len(groups),
            "fallback_used": group_diagnostics.get("fallback_used", False),
        }
    },
)
```

---

# 二、修 `candidate_materialize` 强行截断 clip 的问题

## 当前问题

现在 `step_ai_voiceover_candidate_materialize()` 里有：

```python
if max_clip_seconds > 0 and padded_end - padded_start > max_clip_seconds:
    padded_end = padded_start + max_clip_seconds
```

这会直接把超过 `max_clip_seconds` 的片段砍断。

这个不应该在 candidate 阶段做。因为 candidate 阶段只是把 `selected_segments + micro_segments` 物化为 clip，不能破坏 ASR 事实边界。

---

## 修改目标

改成：

```text
1. 不在 candidate materialize 阶段默认截断 source_end。
2. 超长只写 diagnostics。
3. 如果需要严格限制长度，在 asr_micro_segment 阶段按 sentence boundary 拆。
4. 如果配置要求 fail_on_overlong=true，则直接失败。
5. truncate_overlong 只允许作为本地调试开关，生产默认必须保持 false；不建议作为验收路径。
```

---

## 2.1 修改 `config.toml`

在 `[ai_voiceover_candidate]` 下新增：

```toml
truncate_overlong = false  # deprecated/debug only，生产默认必须保持 false
fail_on_overlong = false
```

最终类似：

```toml
[ai_voiceover_candidate]
padding_before_seconds = 0.5
padding_after_seconds = 0.5
min_clip_seconds = 3.0
max_clip_seconds = 30.0
truncate_overlong = false  # deprecated/debug only，生产默认必须保持 false
fail_on_overlong = false
fail_on_empty = true
```

---

## 2.2 修改 `step_ai_voiceover_candidate_materialize()`

在读取配置位置加：

```python
truncate_overlong = bool(cfg.get("truncate_overlong", False))
fail_on_overlong = bool(cfg.get("fail_on_overlong", False))
```

在循环前加 diagnostics：

```python
overlong_clips: list[dict[str, Any]] = []
invalid_time_segments: list[dict[str, Any]] = []
```

把原来的截断逻辑：

```python
if max_clip_seconds > 0 and padded_end - padded_start > max_clip_seconds:
    padded_end = padded_start + max_clip_seconds
```

替换成：

```python
raw_duration_with_padding = padded_end - padded_start

if max_clip_seconds > 0 and raw_duration_with_padding > max_clip_seconds:
    overlong_item = {
        "micro_segment_id": micro_segment_id,
        "source_id": source_id,
        "source_start_seconds": round(padded_start, 3),
        "source_end_seconds": round(padded_end, 3),
        "duration_seconds": round(raw_duration_with_padding, 3),
        "max_clip_seconds": max_clip_seconds,
        "action": "kept_full_range",
    }

    if truncate_overlong:
        padded_end = padded_start + max_clip_seconds
        overlong_item["action"] = "truncated_by_config"
        overlong_item["truncated_source_end_seconds"] = round(padded_end, 3)

    overlong_clips.append(overlong_item)
```

也就是说：默认只记录，不截断。

补充约束：

```text
candidate_materialize 阶段不要承担“拆片”职责。
超过 max_clip_seconds 的片段，只允许记录 overlong_clips；是否失败由 fail_on_overlong 控制。
真正要缩短片段，必须回到 asr_micro_segment 阶段按 sentence boundary 拆，而不是在 candidate 阶段硬砍 source_end。
```

---

## 2.3 对 invalid time 做记录

现在遇到无效时间是直接 `continue`：

```python
if start is None or end is None or end <= start:
    continue
```

改成：

```python
if start is None or end is None or end <= start:
    invalid_time_segments.append({
        "micro_segment_id": micro_segment_id,
        "source_id": seg.get("source_id", ""),
        "source_start_seconds": seg.get("source_start_seconds"),
        "source_end_seconds": seg.get("source_end_seconds"),
        "start_seconds": seg.get("start_seconds"),
        "end_seconds": seg.get("end_seconds"),
        "reason": "invalid_start_end",
    })
    continue
```

---

## 2.4 debug 输出补充

当前 debug 是：

```python
debug = {
    "selected_segment_count": ...,
    "micro_segment_count": ...,
    "candidate_clip_count": ...,
    "missing_segment_ids": ...,
}
```

改成：

```python
debug = {
    "selected_segment_count": len(content.get("selected_segments") or []),
    "micro_segment_count": len(micro_segments),
    "candidate_clip_count": len(candidate_clips),
    "missing_segment_ids": missing_segment_ids,
    "invalid_time_segments": invalid_time_segments,
    "overlong_clips": overlong_clips,
    "config": {
        "padding_before_seconds": padding_before,
        "padding_after_seconds": padding_after,
        "min_clip_seconds": min_clip_seconds,
        "max_clip_seconds": max_clip_seconds,
        "truncate_overlong": truncate_overlong,
        "fail_on_overlong": fail_on_overlong,
    },
}
```

---

## 2.5 fail_on_overlong 逻辑

在写 status 前加：

```python
hard_failed = False
hard_error_type = ""

if fail_on_empty and not candidate_clips:
    hard_failed = True
    hard_error_type = "empty_candidate_clips"

if fail_on_overlong and overlong_clips:
    hard_failed = True
    hard_error_type = "overlong_candidate_clips"
```

然后：

```python
status = "failed" if hard_failed else "success"
```

最后 raise 分两类：

```python
if fail_on_empty and not candidate_clips:
    raise UserFacingPipelineError(
        "ai_voiceover_candidate_clips_empty",
        user_message="AI 配音候选片段为空：content_analysis 没有形成可物化的 micro_segment。",
        suggestions=[
            "回查 agents/content_analysis/v*/content_analysis.json。",
            "回查 agents/asr_micro_segment/v*/micro_segments.json。",
        ],
        technical_detail=debug,
    )

if fail_on_overlong and overlong_clips:
    raise UserFacingPipelineError(
        "ai_voiceover_candidate_clips_overlong",
        user_message="AI 配音候选片段过长：存在超过 max_clip_seconds 的 micro_segment，不能在候选物化阶段强行截断。",
        suggestions=[
            "回到 asr_micro_segment 阶段拆分过长 micro_segment。",
            "调小 hard_max_segment_seconds 后从 asr_micro_segment 重跑。",
            "或者临时关闭 fail_on_overlong。",
        ],
        technical_detail=debug,
    )
```

---

# 三、统一 `content_analysis` 协议：只说 `micro_segment_id`

## 当前问题

`content_analysis_micro_segment.txt` 要求输出 `micro_segment_id`，但 `_build_content_analysis_micro_segment_text()` 里写的是“只输出 segment_id”。 

虽然 normalize 兼容 `segment_id`，但这是旧字段，会让模型混乱。

---

## 修改目标

```text
1. 输入文字统一叫 micro_segment_id。
2. 不再提示 segment_id。
3. 尽量不要给模型 time 时间码。
4. 输入可以保留 source_id，帮助模型区分多源素材；但输出不允许包含 source_id。
5. 只给 duration、声、画、source_id。
```

---

## 3.1 修改 `_build_content_analysis_micro_segment_text()`

把当前函数整体替换成：

```python
def _build_content_analysis_micro_segment_text(self) -> str:
    micro_doc = self._load_step_json("asr_micro_segment")
    micro_segments = micro_doc.get("segments", [])
    if not isinstance(micro_segments, list) or not micro_segments:
        raise UserFacingPipelineError(
            "content_analysis_micro_segments_empty",
            user_message="内容分析失败：没有可用 micro_segments。",
            suggestions=["回查 asr_micro_segment/v*/micro_segments.json。"],
            technical_detail={"step": "content_analysis"},
        )

    cfg = self.config.raw.get("asr_micro_segment", {})
    max_segments = int(cfg.get("max_segments_for_content_analysis", 120) or 120)

    lines = [
        "你将看到一组 AI 配音候选 micro_segments。",
        "每个 micro_segment_id 是唯一标识。",
        "任务：只从这些 micro_segment_id 中选择适合 AI 配音短视频的事实段。",
        "不要输出时间码。",
        "输入中的 source_id 只用于区分素材来源，不要输出 source_id。",
        "不要输出 chunk_id。",
        "不要输出 candidate_clips。",
        "只输出 selected_segments。",
        "",
        "micro_segments:",
        "",
    ]

    included = 0
    for seg in micro_segments:
        if not isinstance(seg, dict):
            continue
        if included >= max_segments:
            break

        ms_id = str(seg.get("micro_segment_id") or "").strip()
        if not ms_id:
            continue

        asr_text = compact_text(
            str(seg.get("asr_text") or ""),
            max_chars=500,
        )
        visual = compact_text(
            str(seg.get("visual_context") or seg.get("visual_summary") or seg.get("visual") or ""),
            max_chars=300,
        )

        if not asr_text:
            continue

        lines.append(f"[{ms_id}]")
        lines.append(f"source_id={seg.get('source_id', '')}")
        lines.append(f"duration={seg.get('duration_seconds', '')}")
        lines.append(f"声：{asr_text}")
        if visual:
            lines.append(f"画：{visual}")
        lines.append("")

        included += 1

    if included <= 0:
        raise UserFacingPipelineError(
            "content_analysis_no_valid_micro_segment_text",
            user_message="内容分析失败：micro_segments 中没有可用 ASR 文本。",
            suggestions=["检查 asr_micro_segment 输出里的 asr_text。"],
            technical_detail={"micro_segment_count": len(micro_segments)},
        )

    lines.append(
        '输出 JSON 格式：{"selected_segments":[{"micro_segment_id":"...","summary":"...","role":"fact"}]}'
    )
    return "\n".join(lines).strip()
```

重点：

```text
1. 删除 “只输出 segment_id”。
2. 删除 time=source_start-source_end。
3. 输出格式只写 micro_segment_id。
4. 输入可以展示 source_id，但只作为多源区分上下文；输出不要让模型输出 source_id。
```

---

## 3.2 修改 normalize 函数，保留兼容但主字段明确

`_normalize_content_analysis_selected_segments()` 可以继续兼容 `segment_id`，但建议加 diagnostics：

```python
legacy_segment_id_used = 0
invalid_selected_items: list[dict[str, Any]] = []
```

在解析 raw 时：

```python
elif isinstance(raw, dict):
    raw_micro_id = raw.get("micro_segment_id")
    raw_legacy_id = raw.get("segment_id") or raw.get("id")

    if raw_micro_id:
        micro_segment_id = str(raw_micro_id).strip()
    else:
        micro_segment_id = str(raw_legacy_id or "").strip()
        if micro_segment_id:
            legacy_segment_id_used += 1
```

遇到不存在的 ID 时：

```python
if not micro_segment_id or micro_segment_id in seen or micro_segment_id not in seg_map:
    invalid_selected_items.append({
        "raw": raw,
        "reason": "missing_or_unknown_micro_segment_id",
        "micro_segment_id": micro_segment_id,
    })
    continue
```

返回里加：

```python
"diagnostics": {
    "selected_raw_count": len(raw_items),
    "selected_valid_count": len(selected),
    "legacy_segment_id_used": legacy_segment_id_used,
    "invalid_selected_items": invalid_selected_items[:100],
},
```

---

# 四、给 `source_raw_asr_index` 增加 diagnostics

## 当前问题

现在 ASR segment 和 chunk 做 overlap，如果没有匹配到 chunk，就只是返回空 `primary_chunk_id/source_chunk_ids`。

这会导致后面如果画面摘要为空，很难知道是：

```text
1. ASR 时间错位；
2. chunk 时间错位；
3. chunk_id 没匹配；
4. source_id 不一致；
5. 还是本来就没有视觉摘要。
```

---

## 修改目标

```text
source_raw_asr_index.json 增加 diagnostics。
每条 ASR 找不到 chunk overlap 时写 asr_chunk_no_overlap。

实现上可以让 `_source_chunk_ids_for_time_range()` 返回一个 `diagnostic: dict | None`，调用处统一 append；如果沿用下面的 `list[dict]` 也可以，但要避免把调用链改复杂。
```

---

## 4.1 修改 `_source_chunk_ids_for_time_range()`

把签名从：

```python
def _source_chunk_ids_for_time_range(...) -> tuple[str, list[str]]:
```

改成：

```python
def _source_chunk_ids_for_time_range(
    self,
    *,
    source_id: str,
    start: float,
    end: float,
    chunks: list[dict[str, Any]],
) -> tuple[str, list[str], list[dict[str, Any]]]:
```

函数实现改成：

```python
def _source_chunk_ids_for_time_range(
    self,
    *,
    source_id: str,
    start: float,
    end: float,
    chunks: list[dict[str, Any]],
) -> tuple[str, list[str], list[dict[str, Any]]]:
    overlaps: list[tuple[float, str]] = []
    diagnostics: list[dict[str, Any]] = []

    same_source_chunks = [
        chunk
        for chunk in chunks
        if str(chunk.get("source_id") or "") == source_id
    ]

    for chunk in same_source_chunks:
        chunk_start = self._time_value_seconds(
            chunk.get("local_start_seconds") or chunk.get("start_seconds")
        )
        chunk_end = self._time_value_seconds(
            chunk.get("local_end_seconds") or chunk.get("end_seconds")
        )
        if chunk_start is None or chunk_end is None or chunk_end <= chunk_start:
            continue

        overlap = max(0.0, min(end, chunk_end) - max(start, chunk_start))
        if overlap <= 0:
            continue

        chunk_id = str(chunk.get("chunk_id") or chunk.get("global_chunk_id") or "").strip()
        if chunk_id:
            overlaps.append((overlap, chunk_id))

    overlaps.sort(key=lambda item: item[0], reverse=True)
    source_chunk_ids = [chunk_id for _overlap, chunk_id in overlaps]

    if source_chunk_ids:
        return source_chunk_ids[0], source_chunk_ids, diagnostics

    center = (start + end) / 2.0
    nearest: tuple[float, dict[str, Any]] | None = None

    for chunk in same_source_chunks:
        chunk_start = self._time_value_seconds(
            chunk.get("local_start_seconds") or chunk.get("start_seconds")
        )
        chunk_end = self._time_value_seconds(
            chunk.get("local_end_seconds") or chunk.get("end_seconds")
        )
        if chunk_start is None or chunk_end is None or chunk_end <= chunk_start:
            continue

        if center < chunk_start:
            distance = chunk_start - center
        elif center > chunk_end:
            distance = center - chunk_end
        else:
            distance = 0.0

        if nearest is None or distance < nearest[0]:
            nearest = (distance, chunk)

    diagnostics.append({
        "type": "asr_chunk_no_overlap",
        "source_id": source_id,
        # 调用方如果有原始 ASR ID，需要补进来，方便排查具体是哪条 ASR 没匹配到 chunk。
        "source_raw_asr_segment_id": "",
        "asr_segment_id": "",
        "asr_start_seconds": round(start, 3),
        "asr_end_seconds": round(end, 3),
        "nearest_chunk_id": (
            nearest[1].get("chunk_id") or nearest[1].get("global_chunk_id")
            if nearest else ""
        ),
        "nearest_distance_seconds": round(nearest[0], 3) if nearest else None,
        "same_source_chunk_count": len(same_source_chunks),
    })

    return "", [], diagnostics
```

---

## 4.2 修改 `_load_child_source_asr_segments()`

现在这里：

```python
primary_chunk_id, source_chunk_ids = self._source_chunk_ids_for_time_range(...)
```

改成：

```python
primary_chunk_id, source_chunk_ids, diags = self._source_chunk_ids_for_time_range(
    source_id=source_id,
    start=float(start),
    end=float(end),
    chunks=chunks,
)
```

然后函数需要返回 segments 和 diagnostics。

把函数签名改成：

```python
def _load_child_source_asr_segments(...) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
```

初始化：

```python
out: list[dict[str, Any]] = []
diagnostics: list[dict[str, Any]] = []
```

循环里：

```python
diagnostics.extend(diags)
```

最后：

```python
return out, diagnostics
```

如果前面提前 return，也要改成：

```python
return [], []
```

---

## 4.3 修改 `step_source_aggregate()`

现在这里：

```python
asr_segments = self._load_child_source_asr_segments(...)
raw_segments_all.extend(asr_segments)
```

改成：

```python
raw_asr_diagnostics: list[dict[str, Any]] = []
```

然后：

```python
asr_segments, asr_diags = self._load_child_source_asr_segments(
    source_id=source_id,
    source_index=source_index,
    summary_doc=summary_doc,
    chunks=chunks,
)
raw_asr_diagnostics.extend(asr_diags)
raw_segments_all.extend(asr_segments)
```

`raw_asr_index` 改成：

```python
raw_asr_index = {
    "version": "source_raw_asr_index_v1",
    "raw_asr_segment_count": len(raw_segments_all),
    "source_count": len(raw_sources),
    "sources": raw_sources,
    "diagnostics": raw_asr_diagnostics,
    "diagnostics_count": len(raw_asr_diagnostics),
}
```

`aggregate` 里也加：

```python
"raw_asr_diagnostics_count": len(raw_asr_diagnostics),
```

`extra summary` 也加：

```python
extra={
    "summary": {
        "chunks": len(chunks),
        "sources": len(summaries),
        "raw_asr_segments": len(raw_segments_all),
        "raw_asr_diagnostics": len(raw_asr_diagnostics),
    }
},
```

---

# 五、让 `max_sentence_chars` 真正生效

## 当前问题

配置里有 `max_sentence_chars = 120`，但 `_build_asr_sentence_units()` 现在是一条 raw ASR segment 直接变成一句。 

如果 ASR 一条很长，后面 micro_segment 就会粗。

---

## 修改目标

```text
只在单条 raw ASR segment 内部按标点切分。
时间可以按字符比例在该 raw ASR segment 内部分配。
不要回到 timeline_digest/chunk 级别按字数推时间。
source_raw_asr_index.json 继续保存原始 ASR segment，不要在 source_aggregate 阶段拆；拆分只发生在 asr_micro_segment 的 sentence_units 构造阶段。
```

---

## 5.1 新增函数 `_split_long_asr_text_for_sentence_units()`

放在 `_build_asr_sentence_units()` 附近：

```python
def _split_long_asr_text_for_sentence_units(
    self,
    text: str,
    *,
    max_chars: int,
) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return []

    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    parts = re.split(r"([。！？；，,.!?;])", text)
    units: list[str] = []
    current = ""

    for i in range(0, len(parts), 2):
        frag = parts[i] or ""
        punct = parts[i + 1] if i + 1 < len(parts) else ""
        candidate = (current + frag + punct).strip()

        if current and len(candidate) > max_chars:
            units.append(current.strip())
            current = (frag + punct).strip()
        else:
            current = candidate

    if current.strip():
        units.append(current.strip())

    # 如果没有标点，硬切，但只在单条 raw ASR segment 内切
    final_units: list[str] = []
    for unit in units or [text]:
        unit = unit.strip()
        if not unit:
            continue
        if len(unit) <= max_chars:
            final_units.append(unit)
            continue

        for start in range(0, len(unit), max_chars):
            piece = unit[start:start + max_chars].strip()
            if piece:
                final_units.append(piece)

    return final_units
```

---

## 5.2 修改 `_build_asr_sentence_units()`

在读取 raw_sources 后加：

```python
cfg = self.config.raw.get("asr_micro_segment", {})
max_chars = int(cfg.get("max_sentence_chars", 120) or 120)
```

把原来直接 append sentence 的部分：

```python
sentences.append({
    "sentence_id": f"s{len(sentences) + 1:04d}",
    ...
    "start_seconds": round(float(start), 3),
    "end_seconds": round(float(end), 3),
    "text": text,
    ...
})
```

替换成：

```python
parts = self._split_long_asr_text_for_sentence_units(
    text,
    max_chars=max_chars,
)

if not parts:
    continue

total_chars = sum(len(part) for part in parts) or len(text)
cursor = float(start)
duration = float(end) - float(start)

for part_index, part in enumerate(parts):
    part = part.strip()
    if not part:
        continue

    if part_index == len(parts) - 1:
        part_start = cursor
        part_end = float(end)
    else:
        part_duration = duration * (len(part) / max(total_chars, 1))
        part_start = cursor
        part_end = min(float(end), cursor + part_duration)
        cursor = part_end

    if part_end <= part_start:
        continue

    sentences.append({
        "sentence_id": f"s{len(sentences) + 1:04d}",
        "asr_segment_id": seg.get("asr_segment_id", ""),
        "source_raw_asr_segment_id": seg.get("source_raw_asr_segment_id", ""),
        "source_id": seg.get("source_id") or source.get("source_id") or "source_1",
        "source_index": seg.get("source_index", source.get("source_index", 0)),
        "start_seconds": round(float(part_start), 3),
        "end_seconds": round(float(part_end), 3),
        "text": part,
        "raw_text": seg.get("raw_text", ""),
        "source_chunk_ids": seg.get("source_chunk_ids", []),
        "primary_chunk_id": seg.get("primary_chunk_id", ""),
        "split_from_asr_segment": len(parts) > 1,
        "split_part_index": part_index + 1,
        "split_part_count": len(parts),
    })
```

这样 `max_sentence_chars` 才真正生效。

---

# 六、删除或隔离旧的 content_analysis candidate 物化函数

## 当前问题

`_materialize_content_analysis_candidates_from_segments()` 还留着，虽然现在主链路没调用，但它语义上是旧链路：从 `candidate_segments/candidate_clips` 直接生成 candidate。

---

## 修改目标

```text
1. 不一定要物理删除，避免影响旧任务。
2. 但必须改名成 legacy。
3. 函数内加保护，AI 配音统一多源主链路禁止调用。
```

---

## 6.1 改函数名

把：

```python
def _materialize_content_analysis_candidates_from_segments(self, result: Any) -> dict[str, Any]:
```

改成：

```python
def _materialize_content_analysis_candidates_from_segments_legacy(self, result: Any) -> dict[str, Any]:
```

如果全仓库没有引用，就只改函数名即可。

---

## 6.2 函数开头加保护

```python
def _materialize_content_analysis_candidates_from_segments_legacy(self, result: Any) -> dict[str, Any]:
    compat = self.config.raw.get("compat", {})
    if self.options.production_mode == "ai_voiceover" and self._is_virtual_source_manifest():
        if not bool(compat.get("allow_ai_voiceover_candidate_from_content_analysis", False)):
            raise UserFacingPipelineError(
                "legacy_content_analysis_candidate_materialize_disabled",
                user_message="旧版 content_analysis 直接生成 candidate_clips 的逻辑已禁用。",
                suggestions=[
                    "AI 配音统一多源主链路必须使用 ai_voiceover_candidate_materialize。",
                    "如果只是调试旧任务，可显式开启 compat.allow_ai_voiceover_candidate_from_content_analysis=true。",
                ],
            )

    ...
```

---

# 七、给 `short_video_edit_plan` 加业务语义重试

## 当前状态

现在已经没有“模型空输出就自动选前 N 个 clip”的 fallback。这个是对的。`short_video_edit_plan` 候选为空会失败，模型输出无法物化也会在校验时失败。

但还没有“合法 JSON 但 clip_ids 为空”的业务重试。

---

## 修改目标

```text
1. 第一次模型返回空 plan，不马上失败。
2. 用更明确的 retry input 再试一次。
3. 第二次仍为空才 fail-fast。
4. 不允许自动选前 N 个 clip。
```

---

## 7.1 新增配置

`config.toml`：

```toml
[short_video_edit_plan]
semantic_retry_on_empty = true
semantic_retry_rounds = 1
```

---

## 7.2 新增函数 `_short_video_edit_plan_has_valid_editing_structure()`

> 这里不要只判断 `scripts` 是否非空。semantic retry 的成功条件必须是：最终物化后的 `editing_structure` 至少包含 1 个能回查到 `candidate_clips` 的有效 `source_clip_id`。否则模型可能输出一个看似有结构、但 clip_id 是编造的 JSON，后面仍然会坏。

放在 `_materialize_short_video_edit_plan()` 附近：

```python
def _short_video_edit_plan_has_valid_editing_structure(self, plan: Any) -> bool:
    if not isinstance(plan, dict):
        return False

    candidates = self._load_optional_step_json("ai_voiceover_candidate_materialize", {})
    candidate_clips = candidates.get("candidate_clips", []) if isinstance(candidates, dict) else []
    valid_clip_ids = {
        str(clip.get("clip_id") or "").strip()
        for clip in candidate_clips
        if isinstance(clip, dict) and str(clip.get("clip_id") or "").strip()
    }
    if not valid_clip_ids:
        return False

    scripts = plan.get("scripts")
    if not isinstance(scripts, list) or not scripts:
        return False

    seen_shot_ids: set[str] = set()
    for script in scripts:
        if not isinstance(script, dict):
            continue
        shots = script.get("editing_structure")
        if not isinstance(shots, list) or not shots:
            continue

        for shot in shots:
            if not isinstance(shot, dict):
                continue
            shot_id = str(shot.get("shot_id") or "").strip()
            source_clip_id = str(shot.get("source_clip_id") or "").strip()
            if not shot_id or shot_id in seen_shot_ids:
                continue
            if source_clip_id not in valid_clip_ids:
                continue
            seen_shot_ids.add(shot_id)
            return True

    return False
```

兼容说明：如果代码里暂时还保留 `_short_video_edit_plan_has_scripts()` 这个旧函数名，也必须把内部逻辑改成上面的“有效 editing_structure”判断，不能继续只看 `scripts` 非空。

---

## 7.3 新增 retry 输入构造函数

```python
def _build_short_video_edit_plan_retry_text(
    self,
    *,
    previous_raw_result: Any,
) -> str:
    base = self._build_short_video_edit_plan_text()
    clips = self._load_candidate_clips_for_current_mode()

    clip_ids = [
        str(clip.get("clip_id") or "")
        for clip in clips
        if str(clip.get("clip_id") or "").strip()
    ]

    retry_note = [
        "",
        "【重要重试要求】",
        "上一次模型输出没有形成可用 clip_ids，因此无法生成短视频。",
        "这一次必须从候选 clips 中选择至少 1 个 clip_id。",
        "只能使用输入中存在的 clip_id。",
        "不要输出时间码。",
        "不要输出解释。",
        "输出必须是 JSON。",
        "",
        "可用 clip_id 列表：",
        ", ".join(clip_ids),
        "",
        "上一次模型输出：",
        json.dumps(previous_raw_result, ensure_ascii=False, indent=2, default=str)[:3000],
    ]

    return base + "\n" + "\n".join(retry_note)
```

---

## 7.4 修改 `step_short_video_edit_plan()`

把当前：

```python
result = self._run_text_agent("short_video_edit_plan", self._build_short_video_edit_plan_text())
result = self._materialize_short_video_edit_plan(result)
self._validate_ai_voiceover_editing_structure_or_raise(result)
self._overwrite_step_json("short_video_edit_plan", result, materialized=True)
```

改成：

```python
cfg = self.config.raw.get("short_video_edit_plan", {})
semantic_retry = bool(cfg.get("semantic_retry_on_empty", True))
retry_rounds = int(cfg.get("semantic_retry_rounds", 1) or 0)

raw_result = self._run_text_agent(
    "short_video_edit_plan",
    self._build_short_video_edit_plan_text(),
)

materialized = self._materialize_short_video_edit_plan(raw_result)

attempts = [
    {
        "attempt": 1,
        "raw_result": raw_result,
        "script_count": len(materialized.get("scripts") or []) if isinstance(materialized, dict) else 0,
    }
]

if (
    self.options.production_mode == "ai_voiceover"
    and semantic_retry
    and not self._short_video_edit_plan_has_valid_editing_structure(materialized)
):
    # 触发条件不要只看 scripts 是否为空：
    # 1. scripts 为空；
    # 2. editing_structure 为空；
    # 3. source_clip_id 不存在；
    # 4. source_clip_id 存在但回查不到 candidate_clip。
    for retry_index in range(retry_rounds):
        retry_input = self._build_short_video_edit_plan_retry_text(
            previous_raw_result=raw_result,
        )

        # 注意：不能直接用 _run_text_agent 同一个 step，否则缓存/input_hash/版本会混乱。
        # 建议写一个专门的 helper，或者临时直接调用 llm_text.call_json。
        raw_retry = self._run_text_agent_no_step_record(
            step="short_video_edit_plan_retry",
            prompt=prompts.SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT,
            input_data=retry_input,
            debug_base="agents/short_video_edit_plan_retry",
        )

        retry_materialized = self._materialize_short_video_edit_plan(raw_retry)

        attempts.append({
            "attempt": retry_index + 2,
            "raw_result": raw_retry,
            "script_count": len(retry_materialized.get("scripts") or []) if isinstance(retry_materialized, dict) else 0,
        })

        if self._short_video_edit_plan_has_valid_editing_structure(retry_materialized):
            materialized = retry_materialized
            materialized["semantic_retry_used"] = True
            materialized["semantic_retry_attempt"] = retry_index + 2
            break

materialized["semantic_retry_attempts"] = attempts

self._validate_ai_voiceover_editing_structure_or_raise(materialized)
self._overwrite_step_json("short_video_edit_plan", materialized, materialized=True)
self._write_compat_short_video_plan_and_editing_script(materialized)
```

---

## 7.5 新增 `_run_text_agent_no_step_record()`

因为 `_run_text_agent()` 会记录 step，如果 retry 也用同一个 step，会把 manifest 搞乱。新增一个只写 debug、不记录 manifest 的 helper：

```python
def _run_text_agent_no_step_record(
    self,
    *,
    step: str,
    prompt: str,
    input_data: Any,
    debug_base: str,
) -> Any:
    if self.llm_text is None:
        raise RuntimeError("缺少 text LLM 配置")

    llm_cfg = self.config.llm
    provider = llm_cfg.get("text_llm_provider", "openai")
    model = self.options.model or llm_cfg.get("text_llm_model_name") or llm_cfg.get(f"text_{provider}_model_name")
    fallback = llm_cfg.get(f"text_{provider}_fallback_models", [])
    max_tokens = int(llm_cfg.get("text_llm_max_tokens", 16000) or 16000)

    version, vdir = self._version_dir(step, debug_base)
    ensure_dir(vdir)

    if isinstance(input_data, str):
        write_text(vdir / "input.txt", input_data)
    else:
        write_json(vdir / "input.json", input_data)

    write_text(vdir / "prompt.txt", prompt)

    result = self.llm_text.call_json(
        model=model,
        fallback_models=fallback,
        prompt=prompt,
        input_data=input_data,
        temperature=0.2,
        debug_dir=vdir / "_llm_debug",
        max_tokens=max_tokens,
    )

    raw = {
        "model": result.model,
        "created_at": now_iso(),
        "raw_text": result.raw_text,
        "usage": result.usage,
        "finish_reason": result.finish_reason,
        "latency_ms": result.latency_ms,
    }

    write_json(vdir / "raw_response.json", raw)
    write_json(vdir / "model_output.json", result.parsed)

    return result.parsed
```

补充约束：

```text
1. retry 不能覆盖第一次 short_video_edit_plan 的 raw_response/model_output/final_output。
2. retry 成功后，最终 short_video_edit_plan.json 必须写 semantic_retry_used / semantic_retry_attempt / semantic_retry_attempts。
3. retry 失败时也要保留 agents/short_video_edit_plan_retry/v*/input.txt、raw_response.json、model_output.json。
4. 不允许用“自动选前 N 个 clip”代替 retry。
```

---

# 八、明确 legacy CLI 单源不支持新版 AI 配音主链路

## 当前问题

`("ai_voiceover", False)` 里没有 `ai_voiceover_candidate_materialize`。

但 `short_video_edit_plan` 现在对 AI 配音会读 `ai_voiceover_candidate_materialize`。所以直接跑 `run_pipeline.py --input xxx.mp4` 的 legacy 单源会失败。

这符合你当前文档边界，但错误提示可以更明确。

---

## 修改方案

在 `step_asr_micro_segment()` 开头加。注意：阻断条件不能误伤 `source_analysis` 创建的 common_only 子任务；只阻断准备执行 AI 配音专属步骤的 legacy 单源主任务。

```python
if (
    self.options.production_mode == "ai_voiceover"
    and not self._is_virtual_source_manifest()
    and not self.options.common_only
):
    compat = self.config.raw.get("compat", {})
    if not bool(compat.get("allow_legacy_single_source_ai_voiceover_micro_segment", False)):
        raise UserFacingPipelineError(
            "ai_voiceover_requires_unified_source_pipeline",
            user_message="AI 配音 micro_segment 主链路只支持统一多源 pipeline。直接 run_pipeline.py --input 的 legacy 单源链路暂不支持。",
            suggestions=[
                "请通过 Web 工作台运行。",
                "或通过 run_multisource_pipeline.py 包装成 source_manifest 后运行。",
                "不要开启 timeline_digest fallback，否则会回到旧链路。",
            ],
            technical_detail={
                "source_mode": self.manifest.get("source_mode"),
                "source_manifest": self.manifest.get("source_manifest"),
                "production_mode": self.options.production_mode,
            },
        )
```

然后在 `config.toml` 的 `[compat]` 加：

```toml
allow_legacy_single_source_ai_voiceover_micro_segment = false
```

---

# 九、调整 `_write_compat_content_analysis()`，避免写空 highlight_detection 误导

当前 `content_analysis` 输出已经没有 `candidate_clips`，但 `_write_compat_content_analysis()` 仍然写：

```python
candidate_clips = {
    "video_news_type": result.get("video_news_type", ""),
    "candidate_clips": result.get("candidate_clips", []),
}
self._write_compat_agent_output("highlight_detection", candidate_clips, "content_analysis")
```



这会生成一个空的 compat `highlight_detection`。虽然主链路不读它，但以后排查时容易误会。

---

## 修改方案

把 `_write_compat_content_analysis()` 改成：

```python
def _write_compat_content_analysis(self, result: dict[str, Any]) -> None:
    video_analysis = {
        "main_topic": result.get("main_topic", ""),
        "video_type": result.get("video_news_type", ""),
        "summary": result.get("summary", ""),
        "key_facts": result.get("key_facts", []),
        "people": result.get("people", []),
        "locations": result.get("locations", []),
        "content_structure": result.get("content_structure", []),
        "potential_angles": result.get("potential_angles", []),
        "compat_note": "content_analysis is now micro_segment selected_segments only.",
    }

    self._write_compat_agent_output("video_understanding", video_analysis, "content_analysis")

    # 新版 AI 配音链路不再从 content_analysis 生成 highlight_detection/candidate_clips。
    # 避免写一个空 candidate_clips 文件误导后续排查。
    if result.get("candidate_clips"):
        candidate_clips = {
            "video_news_type": result.get("video_news_type", ""),
            "candidate_clips": result.get("candidate_clips", []),
            "compat_note": "legacy candidate_clips from content_analysis",
        }
        self._write_compat_agent_output("highlight_detection", candidate_clips, "content_analysis")
```

---

# 十、建议最后加一个链路自检步骤

这不是必须，但非常建议加。因为这条链路字段很多，靠最后 `news_quality_gate` 才发现问题太晚。

建议拆成“每步 fail-fast + 最终全链路自检”两层，不要只把所有问题都拖到 voiceover_script 之后再发现。

---

## 新增函数 `_validate_ai_voiceover_micro_segment_chain()`

放在 `pipeline.py` 里：

```python
def _validate_ai_voiceover_micro_segment_chain(self) -> dict[str, Any]:
    if self.options.production_mode != "ai_voiceover":
        return {"ok": True, "skipped": "not_ai_voiceover"}

    checks: list[dict[str, Any]] = []
    hard_issues: list[str] = []

    aggregate = self._load_optional_step_json("source_aggregate", {})
    raw_count = int(aggregate.get("raw_asr_segment_count") or 0) if isinstance(aggregate, dict) else 0
    raw_asr_index_file = str(aggregate.get("raw_asr_index_file") or "").strip() if isinstance(aggregate, dict) else ""
    if raw_count <= 0:
        hard_issues.append("source_aggregate.raw_asr_segment_count is 0")
    if not raw_asr_index_file:
        hard_issues.append("source_aggregate.raw_asr_index_file is missing")

    micro = self._load_optional_step_json("asr_micro_segment", {})
    micro_segments = micro.get("segments", []) if isinstance(micro, dict) else []
    if not isinstance(micro_segments, list) or not micro_segments:
        hard_issues.append("asr_micro_segment.segments is empty")

    content = self._load_optional_step_json("content_analysis", {})
    selected = content.get("selected_segments", []) if isinstance(content, dict) else []
    if not isinstance(selected, list) or not selected:
        hard_issues.append("content_analysis.selected_segments is empty")

    candidates = self._load_optional_step_json("ai_voiceover_candidate_materialize", {})
    clips = candidates.get("candidate_clips", []) if isinstance(candidates, dict) else []
    if not isinstance(clips, list) or not clips:
        hard_issues.append("ai_voiceover_candidate_materialize.candidate_clips is empty")

    edit = self._load_optional_step_json("short_video_edit_plan", {})
    scripts = edit.get("scripts", []) if isinstance(edit, dict) else []
    if not isinstance(scripts, list) or not scripts:
        hard_issues.append("short_video_edit_plan.scripts is empty")

    voice = self._load_optional_step_json("voiceover_script", {})
    voice_scripts = voice.get("scripts", []) if isinstance(voice, dict) else []
    if not isinstance(voice_scripts, list) or not voice_scripts:
        hard_issues.append("voiceover_script.scripts is empty")

    result = {
        "ok": not hard_issues,
        "hard_issues": hard_issues,
        "counts": {
            "raw_asr_segments": raw_count,
            "micro_segments": len(micro_segments) if isinstance(micro_segments, list) else 0,
            "selected_segments": len(selected) if isinstance(selected, list) else 0,
            "candidate_clips": len(clips) if isinstance(clips, list) else 0,
            "edit_scripts": len(scripts) if isinstance(scripts, list) else 0,
            "voiceover_scripts": len(voice_scripts) if isinstance(voice_scripts, list) else 0,
        },
        "files": {
            "raw_asr_index_file": raw_asr_index_file,
        },
    }

    write_json(self.task_dir / "ai_voiceover_chain_check.json", result)

    if hard_issues:
        raise UserFacingPipelineError(
            "ai_voiceover_micro_segment_chain_invalid",
            user_message="AI 配音 micro_segment 链路自检失败：关键中间产物为空或缺失。",
            suggestions=[
                "按 raw_asr -> micro_segment -> selected_segments -> candidate_clips -> edit_plan -> voiceover_script 顺序回查。",
                "不要跳过中间步骤直接 rerun 后置步骤。",
            ],
            technical_detail=result,
        )

    return result
```

---

## 在关键步骤后调用

建议每一步先做局部校验，最后再做全链路自检：

```text
content_analysis 后：检查 selected_segments。
candidate_materialize 后：检查 candidate_clips。
short_video_edit_plan 后：检查 scripts/editing_structure。
voiceover_script 后：检查 narration_segments。
```

最终全链路自检可以在 `step_voiceover_script()` 成功校验后调用：

```python
self._validate_ai_voiceover_micro_segment_chain()
```

放在：

```python
self._validate_voiceover_script_duration_or_raise(final_output)
self._enforce_long_video_confirmation_after_voiceover(final_output)
```

后面。

---

# 十一、补充失败导出包内容

当前方案里新增了很多中间产物和 diagnostics。为了线上失败后能快速定位，建议把这些新增文件也加入失败导出包或任务失败诊断输出里。

至少包含：

```text
source_aggregate/source_raw_asr_index.json
agents/asr_micro_segment/v*/asr_sentences.json
agents/asr_micro_segment/v*/raw_response.json
agents/asr_micro_segment/v*/group_diagnostics.json
agents/asr_micro_segment/v*/micro_segments.json
agents/content_analysis/v*/input.txt
agents/content_analysis/v*/raw_response.json
agents/content_analysis/v*/content_analysis.json
ai_voiceover_candidates/candidate_clips.json
ai_voiceover_candidates/materialize_debug.json
agents/short_video_edit_plan/v*/input.txt
agents/short_video_edit_plan/v*/raw_response.json
agents/short_video_edit_plan_retry/v*/*
ai_voiceover_chain_check.json
manifest.json
resolved_workflow_steps.json 或 manifest 中当前任务实际 steps 顺序
config.toml 或 effective_config.json
```

注意：失败导出包只是排查增强，不改变主链路逻辑；不要为了导出包反向引入 fallback。

---

# 十二、二次 review 补充要求

这一节只是在原方案基础上的微调补强，不推翻前面的执行方案。开发时按前面章节改，但必须同时满足下面这些约束。

## 12.1 把 fallback 分成三类，避免误实现

后续代码和日志里不要把所有兜底都混叫 fallback，建议明确分成三类：

```text
1. 禁止数据源 fallback
   raw_asr 缺失时，asr_micro_segment 不能 fallback 到 timeline_digest.speech。

2. 禁止业务结果 fallback
   short_video_edit_plan 空时，不能自动选择前 N 个 clip。
   voiceover_script 空时，不能自动拼默认文案。
   tts outputs 为空时，不能记录 success。

3. 允许显式调试 fallback
   只能通过 config 显式开启，默认必须为 false，并且必须写 diagnostics。
```

注意：semantic retry 不是 fallback。semantic retry 是模型返回合法 JSON 但业务结果为空时的“业务语义重试”，不能被实现成自动补结果。

## 12.2 `source_aggregate` 缓存 hash 必须纳入 child ASR

`source_raw_asr_index.json` 是从各子任务 ASR 产物聚合出来的，所以 `source_aggregate` 的 `input_hash` 不能只依赖 source summary 或 source_analysis hash。

必须满足：

```text
1. 如果 child manifest 的 asr step 有 output_hash：
   source_aggregate input_hash 必须包含 child asr output_hash。

2. 如果 child manifest 没有 asr output_hash：
   至少把 child asr output 文件内容 hash 纳入 source_aggregate input_hash。

3. ASR 文件内容变化后：
   source_aggregate 必须重新生成 source_raw_asr_index.json。
```

这是缓存正确性的阻断项，不是可选优化。否则会出现 ASR 已变、但 raw_asr_index 仍复用旧结果的隐蔽问题。

## 12.3 `content_analysis` 的模式边界要写死

本方案里的 `content_analysis` 已经被重定义为 AI 配音 micro_segment selector。

因此必须明确：

```text
1. ai_voiceover 统一多源链路可以进入 content_analysis。
2. content_analysis 输入必须是 asr_micro_segment.segments。
3. content_analysis 输出必须是 selected_segments。
4. selected_segments 主字段必须是 micro_segment_id。
5. 任何非 ai_voiceover 模式进入 content_analysis，都视为 workflow 配置错误。
```

如果未来要恢复 legacy 单源或其他模式，不要复用这个新 prompt；要么在 workflow 层禁止进入，要么新增独立 step，例如 `content_analysis_micro_segment`。

## 12.4 candidate materialize 增加 `short_clips` diagnostics

前文已经要求 overlong 不默认截断。这里再补充短片段边界：

```text
1. 如果 padding 后仍短于 min_clip_seconds，默认不要失败。
2. 不要为了凑够 min_clip_seconds 盲目扩到下一个事实段。
3. 如果确实需要扩，只能在同 source 内，且不能越过相邻 micro_segment 的事实边界。
4. 默认只记录 short_clips diagnostics。
5. 如果后续需要强制失败，可增加 fail_on_short=false，默认仍为 false。
```

建议 debug 增加：

```python
short_clips: list[dict[str, Any]] = []

if min_clip_seconds > 0 and raw_duration_with_padding < min_clip_seconds:
    short_clips.append({
        "micro_segment_id": micro_segment_id,
        "source_id": source_id,
        "source_start_seconds": round(padded_start, 3),
        "source_end_seconds": round(padded_end, 3),
        "duration_seconds": round(raw_duration_with_padding, 3),
        "min_clip_seconds": min_clip_seconds,
        "action": "kept_original_range",
    })
```

并写入 `materialize_debug.json`：

```python
"short_clips": short_clips
```

## 12.5 链路自检读取 raw ASR 的方式必须和主协议一致

自检函数不要读取不存在的内嵌字段：

```python
raw_asr = source_aggregate.raw_asr_index
```

主协议是：

```json
{
  "raw_asr_index_file": "source_aggregate/v*/source_raw_asr_index.json",
  "raw_asr_segment_count": 123,
  "raw_asr_diagnostics_count": 0
}
```

所以自检应该读取：

```python
aggregate = self._load_optional_step_json("source_aggregate", {})
raw_count = int(aggregate.get("raw_asr_segment_count") or 0)
raw_asr_index_file = str(aggregate.get("raw_asr_index_file") or "").strip()
```

如果需要深查，再按 `raw_asr_index_file` 去读取文件。

## 12.6 `short_video_edit_plan` retry 成功条件必须是可物化结构

semantic retry 不能只看 `scripts` 非空。成功条件必须是：

```text
1. scripts 非空；
2. editing_structure 非空；
3. 每个有效 shot 有 shot_id；
4. shot_id 不重复；
5. source_clip_id 能在 ai_voiceover_candidate_materialize.candidate_clips 中找到；
6. 最终能由代码物化出 source_id/source_start/source_end。
```

如果模型输出了假的 `clip_id`，即使 JSON 合法，也必须视为业务无效，触发 semantic retry 或 fail-fast。

## 12.7 配置示例要避免长度阈值自相矛盾

`hard_max_segment_seconds` 必须大于等于 `preferred_max_segment_seconds`。

推荐示例：

```toml
preferred_min_segment_seconds = 8
preferred_max_segment_seconds = 25
hard_max_segment_seconds = 35.0
```

不要写成：

```toml
preferred_max_segment_seconds = 35
hard_max_segment_seconds = 25.0
```

否则规则语义会冲突，Codex 也容易实现出错误切分逻辑。

## 12.8 失败导出包必须包含运行上下文

除了前面列出的中间产物，失败导出包还必须包含：

```text
1. manifest.json
2. 当前 resolved workflow step order
3. config.toml 或 effective_config.json
4. effective_runtime_flags.json
```

原因是本次问题和 workflow、compat 开关、统一多源 / legacy 判断强相关。排查时必须确认实际跑的是：

```text
("ai_voiceover", True)
```

而不是误入：

```text
("ai_voiceover", False)
```

同时要能确认这些开关是否真的为 false：

```toml
allow_sentence_fallback = false
allow_asr_micro_segment_from_timeline_digest = false
allow_legacy_single_source_raw_asr_runtime_build = false
allow_ai_voiceover_candidate_from_content_analysis = false
```

---


## 12.9 当前分支已部分实现时，按验收项核对，不重复新增

如果当前分支已经存在 workflow / AGENT_INFO 的部分改动，开发不要重复新增，也不要因为“workflow 里已经有 step”就认为改造完成。

必须逐项核对：

```text
1. workflow 中是否已经插入 ai_voiceover_candidate_materialize。
2. pipeline.py 中是否已经实现 step_ai_voiceover_candidate_materialize handler。
3. handler 是否真正 fail-fast，而不是空结果 success。
4. asr_micro_segment / content_analysis / short_video_edit_plan / voiceover_script 是否仍存在旧 fallback。
5. short_video_edit_plan 是否只读取 ai_voiceover_candidate_materialize 的 candidate_clips。
6. semantic retry 是否保留首次 raw_response，并且 retry 失败后不会降级成功。
7. debug / diagnostics / output_files 是否完整写入 manifest 和失败导出包。
```

补充说明：

```text
PipelineRunner 是按 step 名动态查找 handler：step_xxx。
所以 workflow 中出现 ai_voiceover_candidate_materialize 只是第一步。
如果没有 step_ai_voiceover_candidate_materialize，运行时仍会直接失败。
```

本条不是新增架构，只是防止 Codex 在已有半成品代码上重复加一遍，或者只改 workflow 不改 handler。

## 12.10 fail-fast 要区分异常空结果和显式 skipped

前面要求 `voiceover_script / tts / cut_plan` 空结果不能 success，这个原则不变。

但实现时必须区分“异常空结果”和“用户显式跳过”：

```text
1. require_tts=true 且未设置 skip_tts 时，tts.outputs 为空必须失败。
2. skip_tts=true 时，tts 必须标记 skipped，不能标记 success。
3. only_analysis=true 时，tts / subtitles / cut_plan / render 被跳过是允许的，但后续 news_quality_gate / render 不应继续伪成功。
4. skip_render=true 只允许跳过 render，不能掩盖 cut_plan 空结果。
5. common_only=true 的子任务只跑公共分析阶段，不应被 legacy 单源阻断误伤。
```

也就是说：

```text
显式 skipped 可以存在。
异常空 success 不允许存在。
```

## 12.11 semantic retry 最终不能降级成功

`short_video_edit_plan` 的 semantic retry 不是 fallback，也不是自动补默认值。

semantic retry 后最终只能有两种结果：

```text
1. retry 后形成可物化 editing_structure，则 success，并记录 semantic_retry_used=true。
2. retry 后仍无法形成有效 editing_structure，则 fail-fast。
```

严禁出现第三种状态：

```text
retry 失败，但代码自动选择前 N 个 clip；
retry 失败，但生成空 editing_structure；
retry 失败，但继续标记 success；
retry 失败，但让 voiceover_script / tts / cut_plan 后面再暴露泛化错误。
```

同时要求：

```text
1. 第一次模型原始输出必须保留，例如 raw_response.json。
2. 第二次语义重试输出必须单独保留，例如 semantic_retry_response.json。
3. retry prompt / retry reason / invalid clip_ids / missing clip_ids 必须进入 diagnostics。
```

## 12.12 candidate_materialize padding 后必须 clamp 到 source duration

`candidate_materialize` 默认不能截断 overlong clip，这个原则不变。

但加 padding 后必须做素材边界修正：

```text
1. padded_start 不能小于 0。
2. padded_end 不能大于当前 source 的 duration_seconds。
3. clamp 只允许修正素材物理边界，不允许为了满足 min_clip_seconds 跨事实扩展。
4. clamp 后如果 end <= start，记录 invalid_time_segments 并跳过该 segment。
5. 如果所有 selected_segments 都因时间无效被跳过，必须 fail-fast。
```

建议 diagnostics 里增加：

```json
{
  "clamped_clips": [
    {
      "micro_segment_id": "ms_0001",
      "source_id": "source_001",
      "original_start_seconds": -0.2,
      "original_end_seconds": 12.5,
      "clamped_start_seconds": 0.0,
      "clamped_end_seconds": 12.5,
      "source_duration_seconds": 120.0
    }
  ]
}
```

注意：

```text
clamp 不是截断 overlong。
overlong 仍然只记录 overlong_clips，是否失败由 fail_on_overlong 控制。
clamp 只是防止 ffmpeg 收到负时间或超过源视频时长。
```

## 12.13 失败导出包补充 effective_runtime_flags.json

在 `12.8` 的基础上，失败导出包建议再补一个运行态文件：

```text
effective_runtime_flags.json
```

至少包含：

```json
{
  "production_mode": "ai_voiceover",
  "audio_policy": "ai_voiceover",
  "require_tts": true,
  "skip_tts": false,
  "skip_render": false,
  "only_analysis": false,
  "common_only": false,
  "source_manifest": "...",
  "source_mode": "unified",
  "is_unified_source_pipeline": true,
  "resolved_workflow_key": ["ai_voiceover", true],
  "resolved_workflow_steps": [
    "source_prepare",
    "source_analysis",
    "source_aggregate",
    "asr_micro_segment",
    "content_analysis",
    "ai_voiceover_candidate_materialize",
    "short_video_edit_plan"
  ]
}
```

原因：

```text
config.toml 只能说明默认配置。
真正排查失败时，更需要知道本次任务运行时最终生效的 options 和 resolved workflow。
尤其是 require_tts / skip_tts / only_analysis / common_only / unified 判断，都会影响 fail-fast 是否应该触发。
```

# 十三、验收矩阵

开发完成后不要只看单个任务是否跑通，至少用下面 5 类用例验收。

## 13.1 AI 配音统一多源正常任务

预期：

```text
source_aggregate.raw_asr_segment_count > 0
source_aggregate.raw_asr_index_file 非空
asr_micro_segment.segments > 0
content_analysis.selected_segments > 0
ai_voiceover_candidate_materialize.candidate_clips > 0
short_video_edit_plan.scripts > 0
short_video_edit_plan.scripts[].editing_structure > 0
voiceover_script.scripts > 0
voiceover_script.scripts[].narration_segments > 0
tts.outputs > 0
cut_plan.output_videos[].clips > 0
```

## 13.2 `asr_micro_segment` 模型返回空 groups

构造方式：临时让模型或 mock 返回：

```json
{"groups": []}
```

预期：

```text
失败在 asr_micro_segment。
错误码：asr_micro_segment_groups_empty。
不能继续进入 content_analysis。
不能自动把所有 ASR 句子补成 fallback group。
```

## 13.3 `content_analysis` 选择不存在的 micro_segment_id

构造方式：让 content_analysis 输出：

```json
{"selected_segments": [{"micro_segment_id": "ms_not_exists"}]}
```

预期：

```text
normalize diagnostics 记录 invalid_selected_items。
如果没有任何有效 selected_segments，必须 fail-fast。
不能生成空 candidate_clips 后继续 success。
```

## 13.4 `short_video_edit_plan` 返回合法 JSON 但无有效 clip_id

构造方式：让模型返回：

```json
{"scripts": []}
```

或返回不存在的：

```json
{"scripts": [{"editing_structure": [{"shot_id": "s1", "source_clip_id": "fake_clip"}]}]}
```

预期：

```text
触发 semantic retry。
retry 成功时保留第一次和第二次 raw_response。
retry 仍失败时 fail-fast。
不能自动选择前 N 个 clip。
```

## 13.5 视频重组任务不受影响

运行 highlight_reassembly。

预期：

```text
workflow 中不出现 asr_micro_segment。
workflow 中不出现 ai_voiceover_candidate_materialize。
仍走 video_understanding -> asr_event_candidate -> candidate_filter -> highlight_reassembly_plan。
原声重组候选池不被 AI 配音 candidate_clips 污染。
```

---

# 十四、建议执行顺序

你可以按这个顺序改，风险最低：

```text
第一阶段：

1. 修 asr_micro_segment fallback，空 groups / valid_groups 为空 fail-fast。
2. 修 candidate_materialize 默认不截断，只记录 overlong。
3. 统一 content_analysis micro_segment_id 文案。
4. 加 short_video_edit_plan 语义 retry，仍空则 fail-fast。
5. 确认 voiceover_script / tts / cut_plan 空结果不会 success。

第二阶段：

6. 加 raw_asr diagnostics。
7. 加 max_sentence_chars 切分。
8. 隔离 legacy content_analysis candidate 函数。
9. 加 legacy CLI 单源阻断提示，但不影响 common_only 子任务。
10. 加链路自检。
11. 补失败导出包和 effective_runtime_flags.json。
```

最小必修集是：

```text
1. asr_micro_segment fallback 禁用。
2. candidate_materialize 默认不截断。
3. content_analysis 统一 micro_segment_id。
4. short_video_edit_plan 空业务结果 semantic retry + fail-fast。
```

这四项不改，后面即使能跑通，也还是会出现“表面 success，但语义不可信”的问题。
