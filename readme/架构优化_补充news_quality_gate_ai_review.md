下面给你一个**最终版优化方案**。我把前后两次分析合并起来，按“先救命、再提速、再工业化”的顺序排。重点不是泛泛讲架构，而是明确到：**为什么改、改哪些文件、怎么改、优先级、关键代码片段、风险点**。

---

# 一、总体判断

你现在这版已经比之前明显进步了。

之前的问题是：
**模型一次性输出太复杂的 JSON，时间码、clip_id、source_id、shot_id、成片结构都让 LLM 生成，容易乱。**

你更新后变成：

> **LLM 只做轻判断：选 chunk_ids / clip_ids；代码负责补时间码、clip_id、shot_id、duration、旧结构兼容。**

这是正确方向。

当前新版流程里，AI 配音主链路已经简化为：

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
content_analysis
short_video_edit_plan
voiceover_script
tts
subtitles
cut_plan
render
```

`candidate_refine`、`merge_decision`、`voiceover_quality_check` 已经从主流程里删除了。

这会明显减少 LLM 调用次数，速度会快很多。但删掉这些步骤以后，必须补上**代码级确定性校验**，否则模型错误会直接进入 TTS 和 render。

所以最终优化目标是：

> **LLM 轻量判断 + 代码强结构化 + 确定性质量门禁 + 统一流程注册 + 全局资源控制。**

本次文档补充两项已经加入项目的关键改动：

1. `news_quality_gate`：渲染前硬校验，负责检查时间码、cut plan、TTS、字幕、可渲染性等确定性问题。
2. `news_quality_ai_review`：新闻编辑软审校，负责检查事实误导、资料画面误用、跨源拼接风险、声画理解风险等编辑质量问题。

两者不要混在一起：**前者决定技术上能不能渲染，后者判断新闻表达是否有风险。**

---

# 二、最终优先级路线图

## P0：必须马上改，影响正确性

1. 修复 `timeline_digest` 缺失 `source_id`，否则多源规则实际失效。
2. 增加 `voiceover_quality_gate`，替代被删除的 LLM 质量检查。
3. 增加 candidate filter，替代被删除的 `candidate_refine`；如果使用 AI 版，只输出 `keep_clip_ids / merge_groups`。
4. 升级 prompt version 或加入 prompt hash，避免旧缓存误复用。

## P1：强烈建议改，影响稳定性和维护性

5. 统一 workflow registry，消除 `web_app.py / pipeline.py / run_multisource_pipeline.py` 三处流程重复。
6. 完善 render 前质量体系：`news_quality_gate` 做硬校验，`news_quality_ai_review` 做新闻软审校。
7. 增加跨进程资源限流，防止多任务、多源、多线程把 GPU / LLM / TTS 打爆。
8. 改 `content_analysis` 输出，加 `main_topic`，提高后续规划和文案稳定性。

## P2：工业化增强

9. 增加 `source_quality_check`，检测源视频质量。
10. Web job 改成本地持久化 job store。
11. `PipelineRunner` 拆分为 steps 模块。
12. `config.toml` 去掉硬编码 API key，改环境变量。
13. 增加 Pydantic schema，对 LLM 输出做结构校验。

---

# 三、P0-1：修复 timeline_digest 缺失 source_id

## 问题原因

你新版提示词里明确要求每个 chunk 包含 `chunk_id、source_id、时间范围、speech、visual、screen_text、scene_type`。例如 `content_analysis.txt` 里明确说 `source_id` 是输入字段。

`short_video_edit_plan.txt` 和 `highlight_reassembly_text.txt` 也都强调多源规则：不同 `source_id` 不能当成同一现场、同一机位、同一句话自然延续。 

但当前 `step_timeline_digest()` 生成 digest 时，没有把 `source_id` 写进去，只写了 `chunk_id/time/speech/visual/screen_text/people/scene/score/flags`。

后面 `_format_timeline_chunks_text()` 虽然会尝试读：

```python
source_id = c.get("source_id") or "source_1"
```

但 digest 里没有真实 source_id，所以多源输入会全部退化成 `source_1`。

## 影响

这会导致：

1. 多源素材边界规则失效。
2. 模型误以为所有 chunk 都来自同一个源。
3. 跨源半句话拼接风险变高。
4. 资料画面、背景画面、现场画面的边界更容易乱。
5. 后续虽然 materialize 能找回 source_id，但选择阶段已经错了。

## 修改文件

```text
newsclip_agent/pipeline.py
```

## 修改位置

`step_timeline_digest()`。

## 直接替换代码片段

找到当前：

```python
chunks.append({
    "chunk_id": item.get("chunk_id", ""),
    "time": f"{item.get('start', '')}-{item.get('end', '')}",
    "start_seconds": item.get("start_seconds", 0),
    "end_seconds": item.get("end_seconds", 0),
    "speech": compact_asr_for_llm(...),
    "visual": compact_text(...),
    ...
})
```

改成：

```python
digest_version = "llm_timeline_digest_v3_with_source_id"

...

chunks.append({
    "chunk_id": item.get("chunk_id", ""),
    "source_id": item.get("source_id", "source_1"),
    "source_index": item.get("source_index", ""),
    "local_start_seconds": item.get("local_start_seconds"),
    "local_end_seconds": item.get("local_end_seconds"),
    "time": f"{item.get('start', '')}-{item.get('end', '')}",
    "start_seconds": item.get("start_seconds", 0),
    "end_seconds": item.get("end_seconds", 0),
    "speech": compact_asr_for_llm(
        item.get("asr_digest") or item.get("asr_text", ""),
        max_chars=int(llm_input_cfg.get("max_asr_chars_per_chunk", 320)),
    ),
    "visual": compact_text(
        item.get("visual_summary", ""),
        max_chars=int(llm_input_cfg.get("max_visual_chars_per_chunk", 160)),
    ),
    "screen_text": compact_list(
        item.get("screen_text", []),
        max_items=int(llm_input_cfg.get("max_screen_text_items", 5)),
    ),
    "people": compact_list(
        item.get("visible_people", []),
        max_items=int(llm_input_cfg.get("max_people_items", 5)),
    ),
    "scene": item.get("scene_type", ""),
    "visual_score": item.get("visual_value_score", 0),
    "hook_score": item.get("hook_score", 0),
    "flags": build_chunk_flags(item),
})
```

注意：`digest_version` 必须改，否则旧缓存可能继续复用。

---

# 四、P0-2：新增 voiceover_quality_gate，替代删除的 voiceover_quality_check

## 问题原因

你现在删掉了 `voiceover_quality_check`，`tts` 直接依赖 `voiceover_script`。`DEPENDENCIES` 里也是：

```python
"tts": ["voiceover_script"]
```



新版 `voiceover_script_text.txt` 写得不错，要求：

* `shot_id` 必须来自输入。
* 不能新增、删除或改写。
* 每个 `narration_segment` 必须对应一个 `shot_id`。
* `narration_text` 必须等于所有 segments 的自然合并。

但这些不能只靠模型。必须用代码检查。

## 修改文件

```text
newsclip_agent/pipeline.py
```

## 流程修改

AI 配音流程从：

```text
voiceover_script
tts
subtitles
cut_plan
render
```

改成：

```text
voiceover_script
voiceover_quality_gate
tts
subtitles
cut_plan
render
```

## dependencies 修改

在 `DEPENDENCIES` 增加：

```python
"voiceover_quality_gate": ["voiceover_script", "short_video_edit_plan"],
"tts": ["voiceover_script", "voiceover_quality_gate"],
```

## step order 修改

`AI_VOICEOVER_STEP_ORDER` 改成：

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
    "short_video_edit_plan",
    "voiceover_script",
    "voiceover_quality_gate",
    "tts",
    "subtitles",
    "cut_plan",
    "render",
]
```

`UNIFIED_AI_VOICEOVER_STEP_ORDER` 同样加：

```python
"voiceover_quality_gate",
```

## 新增方法

放进 `PipelineRunner`：

```python
def step_voiceover_quality_gate(self) -> None:
    voiceover = self._load_step_json("voiceover_script")
    edit_plan = self._load_step_json("short_video_edit_plan")

    input_hash = stable_hash({
        "voiceover_script": self._step_content_hash("voiceover_script"),
        "short_video_edit_plan": self._step_content_hash("short_video_edit_plan"),
        "version": "voiceover_quality_gate_v1",
    })
    if self._can_reuse("voiceover_quality_gate", input_hash):
        print("复用缓存: voiceover_quality_gate")
        return

    plan_by_id = {
        str(s.get("short_video_id")): s
        for s in edit_plan.get("scripts", [])
        if isinstance(s, dict) and s.get("short_video_id")
    }

    errors: list[str] = []
    warnings: list[str] = []

    scripts = voiceover.get("scripts", [])
    if not isinstance(scripts, list) or not scripts:
        errors.append("voiceover_script.scripts 为空或格式错误")

    for script in scripts:
        if not isinstance(script, dict):
            errors.append("voiceover_script.scripts 中存在非对象元素")
            continue

        sid = str(script.get("short_video_id") or "")
        plan = plan_by_id.get(sid)
        if not plan:
            errors.append(f"未知 short_video_id：{sid}")
            continue

        expected_shot_ids = [
            str(shot.get("shot_id"))
            for shot in plan.get("editing_structure", [])
            if isinstance(shot, dict) and shot.get("shot_id")
        ]

        segments = script.get("narration_segments", [])
        if not isinstance(segments, list):
            errors.append(f"{sid} narration_segments 不是数组")
            continue

        actual_shot_ids = [
            str(seg.get("shot_id"))
            for seg in segments
            if isinstance(seg, dict) and seg.get("shot_id")
        ]

        if expected_shot_ids != actual_shot_ids:
            errors.append(
                f"{sid} shot_id 不对齐：expected={expected_shot_ids}, actual={actual_shot_ids}"
            )

        segment_texts = [
            str(seg.get("text") or "").strip()
            for seg in segments
            if isinstance(seg, dict)
        ]
        joined = "".join(segment_texts).replace(" ", "")
        narration_text = str(script.get("narration_text") or "").replace(" ", "").strip()

        if joined != narration_text:
            errors.append(f"{sid} narration_text 与 narration_segments.text 合并结果不一致")

        if any(not text for text in segment_texts):
            errors.append(f"{sid} 存在空的 narration segment")

        banned_words = [
            "震惊",
            "万万没想到",
            "全网炸锅",
            "彻底翻车",
            "惊天反转",
            "内幕曝光",
            "太突然了",
        ]
        for word in banned_words:
            if word in narration_text:
                errors.append(f"{sid} 包含营销号表达：{word}")

        visual_total = float(plan.get("visual_total_seconds") or plan.get("target_duration_seconds") or 0)
        estimated_chars = len(narration_text)
        estimated_seconds = estimated_chars / float(self.duration_settings.tts_actual_chars_per_second or 5.2)

        if visual_total > 0 and estimated_seconds > visual_total + 8:
            warnings.append(
                f"{sid} 文案可能偏长：估算配音 {estimated_seconds:.1f}s，画面 {visual_total:.1f}s"
            )

    version, vdir = self._version_dir("voiceover_quality_gate", "quality/voiceover")
    ensure_dir(vdir)

    result = {
        "version": "voiceover_quality_gate_v1",
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
    }
    out = write_json(vdir / "voiceover_quality_gate.json", result)

    if errors:
        self._record_step(
            step="voiceover_quality_gate",
            version=version,
            status="failed",
            output=relpath(out, self.task_dir),
            input_hash=input_hash,
            output_files=[out],
            extra={"errors": errors, "warnings": warnings},
        )
        raise UserFacingPipelineError(
            "voiceover quality gate failed",
            user_message="AI 配音文案结构检查失败：" + "；".join(errors[:5]),
            technical_detail=result,
        )

    self._write_status(vdir, self._base_status("voiceover_quality_gate", version, input_hash, [out]))
    self._record_step(
        step="voiceover_quality_gate",
        version=version,
        status="success",
        output=relpath(out, self.task_dir),
        input_hash=input_hash,
        output_files=[out],
        extra={"warnings": warnings},
    )
    print("完成: voiceover_quality_gate")
```

---

# 五、P0-3：新增 AI 版 candidate_filter，替代旧 candidate_refine

## 6.1 为什么不是恢复旧 candidate_refine

旧 `candidate_refine` 的问题是输出太复杂，职责太混杂。它容易把“筛选”“用途判断”“独立成片判断”“分组”“排序”“解释原因”都塞在一个 JSON 里。

新版不要恢复旧结构，而是新增轻量 AI 版 `candidate_filter`：

```text
输入：已经 materialize 的 candidate_clips。
输出：只告诉代码保留哪些 clip，以及哪些 clip 建议合并。
```

## 6.2 candidate_filter 的职责

```text
负责：
- 判断候选片段是否有新闻价值。
- 判断是否重复、空泛、半句话、上下文不足。
- 判断相邻候选是否属于同一事实链，是否建议合并使用。

不负责：
- 不生成时间码。
- 不生成 source_id。
- 不生成 duration。
- 不生成 shot_id。
- 不决定最终输出几条视频。
- 不生成配音文案。
```

## 6.3 prompt 文件

如果你项目里已经使用：

```text
newsclip_agent/prompt_texts/candidate_filter.txt
```

那文档里统一叫 `candidate_filter`，不要再混用 `candidate_filter_ai`，避免 step 名和文件名不一致。

## 6.4 推荐模型输出

最终输出保持极简：

```json
{
  "keep_clip_ids": [],
  "merge_groups": []
}
```

`merge_groups` 格式：

```json
{
  "keep_clip_ids": ["clip_001", "clip_002", "clip_003"],
  "merge_groups": [
    ["clip_002", "clip_003"]
  ]
}
```

不要再让模型输出：

```json
{
  "drop": [],
  "reason": "",
  "usage": {}
}
```

原因：

```text
1. drop 可以由代码根据 all_clip_ids - keep_clip_ids 算出来。
2. reason 会增加废话和 token，不影响后续流程。
3. usage 没必要，因为当前 production_mode 已经确定。
```

## 修改文件

```text
newsclip_agent/pipeline.py
```

## 修改位置

`_materialize_candidate_clips_from_chunks()`。当前函数会把模型输出的 `chunk_ids` 转成 `candidate_clips`，补齐时间码和字段。

## 新增函数

```python
def _filter_materialized_candidate_clips(
    self,
    clips: list[dict[str, Any]],
    *,
    mode: str,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []

    for clip in clips:
        speech = str(clip.get("speech") or "").strip()
        visual = str(clip.get("visual") or "").strip()
        summary = str(clip.get("summary") or "").strip()
        duration = float(clip.get("duration_seconds") or 0)

        if duration <= 0:
            continue

        # 太短容易是断句或无意义碎片
        if duration < 5:
            continue

        # 原声重组必须有可听懂的原声信息
        if mode == "highlight_reassembly" and len(speech) < 12:
            continue

        # AI 配音可以语音弱一些，但至少要有画面或摘要信息
        if mode == "ai_voiceover" and len(speech + visual + summary) < 16:
            continue

        weak_markers = [
            "空镜",
            "无新增信息",
            "信息弱",
            "泛用",
            "B-roll",
            "b-roll",
            "不确定",
            "无法判断",
        ]
        weak_hit = sum(
            1
            for marker in weak_markers
            if marker in speech or marker in visual or marker in summary
        )

        # 只命中一个“不确定”不一定删，命中多个再删
        if weak_hit >= 2:
            continue

        # 资料画面不能单独作为原声高光核心
        if mode == "highlight_reassembly":
            archive_markers = ["资料画面", "Archive", "File", "画面来源", "视频来源"]
            if any(m in visual for m in archive_markers) and len(speech) < 20:
                continue

        filtered.append(clip)

    return filtered
```

## 在 `_materialize_candidate_clips_from_chunks()` 末尾调用

当前有：

```python
normalized["candidate_clips"] = materialized
```

改成：

```python
materialized = self._filter_materialized_candidate_clips(
    materialized,
    mode=self.options.production_mode,
)

normalized["candidate_clips"] = materialized
```

## 额外建议

过滤后如果为空，不要直接失败，可以保留原始最强候选兜底：

```python
if not materialized and raw_materialized:
    materialized = raw_materialized[:1]
    normalized["candidate_filter_fallback"] = "all filtered; kept first candidate as fallback"
```

---

# 六、P0-4：升级 prompt version 或加入 prompt hash

## 问题原因

你提示词已经大改，但 `AGENT_INFO` 里的版本号还是类似：

```python
"content_analysis_v1"
"short_video_edit_plan_text_v1"
"voiceover_script_text_v1"
```



这会导致：

1. 旧缓存误复用。
2. 输出结构已经变了，但版本号看不出来。
3. debug 时不知道是哪版提示词产物。

## 修改文件

```text
newsclip_agent/pipeline.py
```

## 修改 AGENT_INFO

建议改成：

```python
AGENT_INFO = {
    "video_understanding": (
        "agents/video_understanding",
        "video_analysis.json",
        prompts.VIDEO_UNDERSTANDING_PROMPT,
        "video_understanding_minimal_v2",
    ),
    "highlight_detection": (
        "agents/highlight_detection",
        "candidate_clips.json",
        prompts.HIGHLIGHT_DETECTION_PROMPT,
        "highlight_detection_chunk_ids_v2",
    ),
    "content_analysis": (
        "agents/content_analysis",
        "content_analysis.json",
        prompts.CONTENT_ANALYSIS_PROMPT,
        "content_analysis_chunk_ids_v2",
    ),
    "short_video_edit_plan": (
        "agents/short_video_edit_plan",
        "short_video_edit_plan.json",
        prompts.SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT,
        "short_video_edit_plan_clip_ids_v2",
    ),
    "voiceover_script": (
        "agents/voiceover_script",
        "voiceover_script.json",
        prompts.VOICEOVER_SCRIPT_TEXT_PROMPT,
        "voiceover_script_segments_v2",
    ),
    "highlight_reassembly_plan": (
        "agents/highlight_reassembly",
        "highlight_reassembly_plan.json",
        prompts.HIGHLIGHT_REASSEMBLY_TEXT_PROMPT,
        "highlight_reassembly_clip_ids_v2",
    ),
}
```

## 更稳的做法：加 prompt hash

在 `_run_text_agent()` 里 input_hash 应该包含：

```python
"prompt_hash": stable_hash(prompt),
"prompt_version": prompt_version,
"model": model,
"fallback": fallback,
"temperature": temperature,
"max_tokens": max_tokens,
```

这样以后你忘记改 version，只要 prompt 文件内容变了，缓存也会失效。

---

# 七、P1-1：统一 workflow registry，解决三处流程重复

## 问题原因

现在流程定义至少有三份：

### `web_app.py`

Web 里有：

* `LEGACY_SINGLE_AI_VOICEOVER_STEP_ORDER`
* `LEGACY_SINGLE_HIGHLIGHT_REASSEMBLY_STEP_ORDER`
* `UNIFIED_AI_VOICEOVER_STEP_ORDER`
* `UNIFIED_HIGHLIGHT_REASSEMBLY_STEP_ORDER`



### `pipeline.py`

Pipeline 里也有同样的 step order 和 dependencies。

### `run_multisource_pipeline.py`

多源 runner 里又单独定义了 common steps。

这会导致以后每加一个步骤，例如 `voiceover_quality_gate`、`source_quality_check`，你要改三处。只要漏一处，Web UI、实际执行、公共进度同步就会不一致。

## 新增文件

```text
newsclip_agent/workflow_registry.py
```

统一 workflow_registry，解决三处流程重复

## 8.1 问题

当前流程定义至少散落在：

```text
web_app.py
pipeline.py
run_multisource_pipeline.py
```

如果每加一个 step 都要改三处，很容易出现：

```text
Web UI 显示有这个步骤，但 pipeline 没跑。
pipeline 跑了这个步骤，但 Web 进度没有。
多源公共分析和单源分析步骤不一致。
```

## 8.2 新增文件

```text
newsclip_agent/workflow_registry.py
```

## 8.3 推荐 workflow

```python
WORKFLOWS: dict[tuple[str, bool], list[str]] = {
    ("ai_voiceover", False): [
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
        "content_analysis",
        "candidate_filter",
        "short_video_edit_plan",
        "voiceover_script",
        "voiceover_quality_gate",
        "tts",
        "subtitles",
        "cut_plan",
        "news_quality_gate",
        "news_quality_ai_review",
        "render",
    ],
    ("highlight_reassembly", False): [
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
        "video_understanding",
        "highlight_detection",
        "candidate_filter",
        "highlight_reassembly_plan",
        "reassembly_cut_plan",
        "news_quality_gate",
        "news_quality_ai_review",
        "reassembly_render",
    ],
    ("ai_voiceover", True): [
        "source_prepare",
        "source_analysis",
        "source_aggregate",
        "content_analysis",
        "candidate_filter",
        "short_video_edit_plan",
        "voiceover_script",
        "voiceover_quality_gate",
        "tts",
        "subtitles",
        "cut_plan",
        "news_quality_gate",
        "news_quality_ai_review",
        "render",
    ],
    ("highlight_reassembly", True): [
        "source_prepare",
        "source_analysis",
        "source_aggregate",
        "video_understanding",
        "highlight_detection",
        "candidate_filter",
        "highlight_reassembly_plan",
        "reassembly_cut_plan",
        "news_quality_gate",
        "news_quality_ai_review",
        "reassembly_render",
    ],
}
```


## 文件内容

```python
from __future__ import annotations

from typing import Any


LEGACY_SINGLE_COMMON_REUSABLE_STEPS = [
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

UNIFIED_SOURCE_COMMON_REUSABLE_STEPS = [
    "source_prepare",
    "source_analysis",
    "source_aggregate",
]


WORKFLOWS: dict[tuple[str, bool], list[str]] = {
    ("ai_voiceover", False): [
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
    ],
    ("highlight_reassembly", False): [
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
        "video_understanding",
        "highlight_detection",
        "highlight_reassembly_plan",
        "reassembly_cut_plan",
        "news_quality_gate",
        "news_quality_ai_review",
        "reassembly_render",
    ],
    ("ai_voiceover", True): [
        "source_prepare",
        "source_analysis",
        "source_aggregate",
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
    ],
    ("highlight_reassembly", True): [
        "source_prepare",
        "source_analysis",
        "source_aggregate",
        "video_understanding",
        "highlight_detection",
        "highlight_reassembly_plan",
        "reassembly_cut_plan",
        "news_quality_gate",
        "news_quality_ai_review",
        "reassembly_render",
    ],
}


DEPENDENCIES: dict[str, list[str]] = {
    "source_prepare": [],
    "source_analysis": [],
    "source_aggregate": ["source_analysis"],

    "metadata": [],
    "audio_extract": ["metadata"],
    "frame_extract": ["metadata"],
    "chunk_build": ["metadata", "frame_extract"],
    "asr": ["audio_extract"],
    "asr_digest": ["asr", "chunk_build"],
    "vision": ["frame_extract", "chunk_build", "asr", "asr_digest"],
    "timeline": ["asr", "asr_digest", "vision"],
    "timeline_digest": ["timeline", "asr_digest"],

    "content_analysis": ["timeline_digest", "source_aggregate"],
    "short_video_edit_plan": ["content_analysis"],
    "voiceover_script": ["short_video_edit_plan"],
    "voiceover_quality_gate": ["voiceover_script", "short_video_edit_plan"],
    "tts": ["voiceover_script", "voiceover_quality_gate"],
    "subtitles": ["voiceover_script", "tts"],
    "cut_plan": ["short_video_edit_plan", "voiceover_script", "subtitles", "tts"],
    # AI 配音模式：cut_plan 后做硬校验，再做 AI 新闻软审校。
    # 原声重组模式：reassembly_cut_plan 后也复用同一套质量门禁。
    # 如果项目里仍保留单一 DEPENDENCIES 字典，news_quality_gate 可在 step 内按 production_mode 读取不同上游。
    "news_quality_gate": ["cut_plan", "reassembly_cut_plan"],
    "news_quality_ai_review": ["news_quality_gate"],

    "video_understanding": ["timeline_digest", "source_aggregate"],
    "highlight_detection": ["timeline_digest", "source_aggregate", "video_understanding"],
    "highlight_reassembly_plan": ["highlight_detection", "video_understanding"],
    "reassembly_cut_plan": ["highlight_reassembly_plan"],
    "render": ["cut_plan", "news_quality_gate", "news_quality_ai_review"],
    "reassembly_render": ["reassembly_cut_plan", "news_quality_gate", "news_quality_ai_review"],
}


def use_unified_source_pipeline(config: Any, source_count: int = 1) -> bool:
    workflow = config.raw.get("workflow", {}) if hasattr(config, "raw") else {}
    return bool(workflow.get("use_unified_source_pipeline", True)) or source_count > 1


def step_order(production_mode: str, *, unified: bool) -> list[str]:
    return list(WORKFLOWS.get((production_mode, unified)) or WORKFLOWS[("ai_voiceover", unified)])


def common_reusable_steps(*, unified: bool) -> list[str]:
    return list(
        UNIFIED_SOURCE_COMMON_REUSABLE_STEPS
        if unified
        else LEGACY_SINGLE_COMMON_REUSABLE_STEPS
    )
```

## 修改 `web_app.py`

删除本地 step order，导入：

```python
from newsclip_agent.workflow_registry import (
    step_order,
    common_reusable_steps,
)
```

替换：

```python
def _common_reusable_steps_for_request(
    *,
    source_count: int,
    use_unified_source_pipeline: bool,
) -> list[str]:
    unified = bool(use_unified_source_pipeline or source_count > 1)
    return common_reusable_steps(unified=unified)
```

替换：

```python
def _step_order_for_production_mode(
    production_mode: str,
    *,
    use_unified_source_pipeline: bool = True,
    source_count: int = 1,
) -> list[str]:
    unified = bool(use_unified_source_pipeline or source_count > 1)
    return step_order(production_mode, unified=unified)
```

## 修改 `pipeline.py`

删除本地 step order 和 dependencies，导入：

```python
from .workflow_registry import DEPENDENCIES, step_order
```

替换 `_active_step_order()`：

```python
def _active_step_order(self) -> list[str]:
    return [
        step
        for step in step_order(
            self.options.production_mode,
            unified=self._is_virtual_source_manifest(),
        )
        if step != "source_prepare"
    ]
```

注意：pipeline 单源实际没有 `source_prepare` step，Web manifest 有，所以这里过滤掉。

## 修改 `run_multisource_pipeline.py`

导入：

```python
from newsclip_agent.workflow_registry import (
    UNIFIED_SOURCE_COMMON_REUSABLE_STEPS,
    LEGACY_SINGLE_COMMON_REUSABLE_STEPS,
)
```

替换：

```python
COMMON_REUSABLE_STEPS = [
    "source_analysis",
    "source_aggregate",
]
COMMON_PROGRESS_STEPS = ["source_prepare"] + COMMON_REUSABLE_STEPS
LEGACY_COMMON_REUSABLE_STEPS = [...]
```

为：

```python
COMMON_REUSABLE_STEPS = [
    step
    for step in UNIFIED_SOURCE_COMMON_REUSABLE_STEPS
    if step != "source_prepare"
]
COMMON_PROGRESS_STEPS = ["source_prepare", *COMMON_REUSABLE_STEPS]
LEGACY_COMMON_REUSABLE_STEPS = LEGACY_SINGLE_COMMON_REUSABLE_STEPS
```

---

# 八、P1-2：补充 news_quality_gate 与 news_quality_ai_review

你现在已经把 `news_quality_gate` 和 `news_quality_ai_review` 加到项目里了，文档这里必须明确：

```text
news_quality_gate 没有提示词。
news_quality_ai_review 才有提示词。
```

两者是 render 前连续的两层质量体系：

```text
cut_plan / reassembly_cut_plan
        ↓
news_quality_gate
        ↓
news_quality_ai_review
        ↓
render / reassembly_render
```

## 9.1 news_quality_gate 的职责

`news_quality_gate` 是代码硬校验 step，不调用 LLM，也没有 prompt 文件。

不要创建：

```text
newsclip_agent/prompt_texts/news_quality_gate.txt
```

它检查确定性问题：

```text
1. cut_plan / reassembly_cut_plan 是否为空。
2. source_start < source_end。
3. 视频总时长是否大于 0。
4. require_tts=true 时，TTS 音频是否生成。
5. 字幕结构是否存在。
6. AI 配音模式下，voiceover_script、tts、subtitles、cut_plan 是否能互相对齐。
7. 原声重组模式下，selected_clips 是否存在、是否可渲染。
8. 输出文件路径、临时目录、素材路径是否具备渲染条件。
```

这些问题不交给 AI，因为代码更稳定、更便宜、不会随机误判。

发现 hard errors 时，`news_quality_gate` 可以直接阻断 render。

## 9.2 news_quality_ai_review 的职责

`news_quality_ai_review` 是 AI 新闻软审校 step，调用文本 LLM。

它使用提示词：

```text
newsclip_agent/prompt_texts/news_quality_ai_review.txt
```

---

## 2. 推荐流程位置

AI 配音模式：

```text
voiceover_script
voiceover_quality_gate
tts
subtitles
cut_plan
news_quality_gate
news_quality_ai_review
render
```

原声重组模式：

```text
highlight_reassembly_plan
reassembly_cut_plan
news_quality_gate
news_quality_ai_review
reassembly_render
```

多源统一模式同理，`news_quality_gate` 和 `news_quality_ai_review` 都放在最终 render 前。

---

## 3. 两个 step 的职责边界

| step | 类型 | 是否调用 LLM | 阻断策略 | 主要作用 |
|---|---|---:|---|---|
| `news_quality_gate` | 硬校验 | 否 | 可以阻断 | 保证 cut plan、时间码、TTS、字幕、可渲染性没有硬错误 |
| `news_quality_ai_review` | 软审校 | 是 | 初期建议不阻断 | 判断新闻表达、事实误导、资料画面误用、跨源拼接等编辑风险 |

建议初期策略：

```text
news_quality_gate 发现 errors：阻断 render。
news_quality_ai_review 返回 medium/high：只写入 warning，不阻断。
稳定跑一批样本后，再考虑 high 风险是否阻断。
```


---

## 6. 配置建议

`config.toml` 建议增加或确认：

```toml
[news_quality_gate]
enabled = true
block_invalid_timecode = true
block_empty_video = true
block_missing_tts = true
block_missing_subtitle = false
block_voiceover_shot_mismatch = true

[news_quality_ai_review]
enabled = true
block_high_risk = false
warn_medium_risk = true
model_name = ""
fallback_models = []
temperature = 0.0
max_tokens = 500
max_input_chars = 12000
fail_policy = "warning"
```

建议初期：

```text
block_high_risk = false
fail_policy = "warning"
```

原因是 AI 审校可能误判。先把结果写入 manifest 和 Web 日志，积累一批样本后，再决定是否让 high 风险阻断 render。

---

## 7. news_quality_ai_review 结果规范化

大模型提示词 E:\ai\skills\AutomaticEditing\newsclip_agent\prompt_texts\news_quality_ai_review.txt
大模型输出解析

```python
def _normalize_news_quality_ai_review(self, result: Any) -> dict[str, Any]:
    raw = result if isinstance(result, dict) else {}

    risk_level = str(raw.get("risk_level") or "low").strip().lower()
    if risk_level not in {"low", "medium", "high"}:
        risk_level = "low"

    issues = raw.get("issues")
    if not isinstance(issues, list):
        issues = []

    issues = [
        str(x).strip()
        for x in issues
        if str(x).strip()
    ][:10]

    return {
        "risk_level": risk_level,
        "issues": issues,
    }
```

阻断逻辑：

```python
ai_review = self._normalize_news_quality_ai_review(raw_ai_review)

if cfg.get("block_high_risk", False) and ai_review["risk_level"] == "high":
    errors.extend(ai_review["issues"] or ["AI 新闻终审判断为 high 风险"])

if ai_review["risk_level"] in {"medium", "high"}:
    warnings.extend(ai_review["issues"])
```

---


# 九、P1-3：content_analysis 输出增加 main_topic

## 问题原因

你现在 `content_analysis.txt` 只输出数组：

```json
[
  {
    "chunk_ids": [],
    "summary": ""
  }
]
```



代码虽然能用 candidate summaries 兜底生成 `summary/key_facts`，但 `main_topic` 很容易为空。后续 `_materialize_short_video_edit_plan()` 会从 content 里拿：

```python
topic = content.get("main_topic") or content.get("topic") or ""
```

如果没有主主题，后续 `news_angle / voiceover_brief / topic` 都会偏弱。

## 修改文件

```text
newsclip_agent/prompt_texts/content_analysis.txt
```

## 建议输出格式改为

```json
{
  "main_topic": "",
  "candidate_clips": [
    {
      "chunk_ids": [],
      "summary": ""
    }
  ]
}
```

## 修改提示词关键段

把输出字段改成：

```text
输出字段：
- main_topic：用一句话概括这些素材最核心的新闻主题。只能基于输入内容。
- candidate_clips：候选片段数组。
- chunk_ids：候选片段由哪些 chunk 组成，必须来自输入中已有的 chunk_id。
- summary：该候选片段表达的完整新闻事实或画面价值。
```

输出格式改成：

```json
{
  "main_topic": "",
  "candidate_clips": [
    {
      "chunk_ids": [],
      "summary": ""
    }
  ]
}
```

代码 `_materialize_candidate_clips_from_chunks()` 已经兼容 dict 里的 `candidate_clips`，所以改动小。

---

# 十、P1-4：把 prompt 里的“最多 8 个”改成配置驱动

## 问题原因

提示词里写死“候选片段最多 8 个”。
配置里也有：

```toml
max_candidate_clips_for_edit_plan = 8
max_llm_candidate_clips = 14
```



多个地方写数量，后续容易不一致。

## 修改方式

### prompt 改

把：

```text
候选片段最多 8 个
```

改成：

```text
候选片段数量以输入运行参数为准；信息不足可以少于上限，不要硬凑。
```

### pipeline 输入构造加参数

在 `_build_content_analysis_brief_text()` 里加：

```python
max_candidates = int(
    self.config.raw.get("llm_input", {}).get("max_candidate_clips_for_edit_plan", 8)
)
lines.append(f"- max_candidate_clips：{max_candidates}")
```

`highlight_detection` 同理。

---

# 十一、P1-5：全局资源限流，防止并发打爆

## 问题原因

现在配置里已经有：

```toml
source_max_workers = 3
per_source_vision_max_workers = 2
vision_global_max_workers = 6
text_global_max_workers = 6
```



Web 层还允许：

```toml
max_running_jobs = 4
```



如果同时跑 4 个任务，每个任务 3 个 source，每个 source 又有视觉并发，就可能把 LLM API、GPU、TTS 全部打满。

## 新增文件

```text
newsclip_agent/resource_manager.py
```

## 代码

```python
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import time
import os

from .utils import ensure_dir


class FileSemaphore:
    def __init__(self, root: Path, name: str, limit: int):
        self.root = ensure_dir(root / name)
        self.limit = max(1, int(limit))

    @contextmanager
    def acquire(self, timeout: float = 600.0):
        start = time.time()
        token: Path | None = None

        try:
            while True:
                for i in range(self.limit):
                    path = self.root / f"slot_{i}.lock"
                    try:
                        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        os.write(fd, str(time.time()).encode("utf-8"))
                        os.close(fd)
                        token = path
                        yield
                        return
                    except FileExistsError:
                        continue

                if time.time() - start > timeout:
                    raise TimeoutError(f"等待资源超时：{self.root.name}")

                time.sleep(0.2)
        finally:
            if token and token.exists():
                try:
                    token.unlink()
                except Exception:
                    pass


def llm_text_slot(config):
    limit = int(config.raw.get("multi_source_analysis", {}).get("text_global_max_workers", 4))
    return FileSemaphore(config.root_dir / "outputs" / "__locks__", "llm_text", limit)


def llm_vision_slot(config):
    limit = int(config.raw.get("multi_source_analysis", {}).get("vision_global_max_workers", 4))
    return FileSemaphore(config.root_dir / "outputs" / "__locks__", "llm_vision", limit)


def tts_slot(config):
    limit = int(config.raw.get("resource_limits", {}).get("tts_global_max_workers", 1))
    return FileSemaphore(config.root_dir / "outputs" / "__locks__", "tts", limit)


def asr_slot(config):
    limit = int(config.raw.get("resource_limits", {}).get("asr_global_max_workers", 1))
    return FileSemaphore(config.root_dir / "outputs" / "__locks__", "asr", limit)
```

## 使用位置

LLM 文本调用：

```python
from .resource_manager import llm_text_slot

with llm_text_slot(self.config).acquire():
    result = self.llm_text.call_json(...)
```

视觉调用：

```python
from .resource_manager import llm_vision_slot

with llm_vision_slot(self.config).acquire():
    result = self.llm_vision.call_json(...)
```

TTS 调用：

```python
from .resource_manager import tts_slot

with tts_slot(self.config).acquire():
    result = generate_omnivoice_audio(...)
```

ASR 调用：

```python
from .resource_manager import asr_slot

with asr_slot(self.config).acquire():
    # funasr transcribe
```

---

# 十二、P2-1：新增 source_quality_check

## 问题原因

当前预处理主要是：

* ffprobe metadata
* ffmpeg 抽音频
* ffmpeg 抽帧
* 固定 chunk

`audio_extract` 已经有一些容错，比如 ffmpeg 普通抽音频失败后会用容错参数重试，没有音轨时生成静音音频。

但新闻视频工业流程还应该检测：

* 源视频是否损坏。
* 是否无音轨。
* 是否黑屏。
* 是否静音过多。
* 分辨率是否异常。
* 时长是否异常。
* 是否可能是占位文件。
* 画面比例是否和任务比例冲突。

## 新增 step

```text
metadata -> source_quality_check -> audio_extract / frame_extract
```

## 新增文件

```text
newsclip_agent/media_quality.py
```

## 基础版代码

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from .utils import write_json


def build_source_quality_report(
    *,
    video: Path,
    metadata: dict[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    duration = float(metadata.get("duration") or 0)
    width = int(metadata.get("width") or 0)
    height = int(metadata.get("height") or 0)
    video_codec = str(metadata.get("video_codec") or "")
    audio_codec = str(metadata.get("audio_codec") or "")
    size = video.stat().st_size if video.exists() else 0

    warnings: list[str] = []
    errors: list[str] = []
    flags: list[str] = []

    if size < 1024 * 100:
        warnings.append("源视频文件体积偏小，可能是占位或损坏文件")
        flags.append("tiny_file")

    if duration <= 0:
        errors.append("视频时长无效")
        flags.append("invalid_duration")

    if not video_codec:
        errors.append("未检测到视频编码")
        flags.append("missing_video_codec")

    if not audio_codec:
        warnings.append("未检测到音频编码，ASR 和原声重组可能不可用")
        flags.append("missing_audio_codec")

    if width <= 0 or height <= 0:
        errors.append("视频分辨率无效")
        flags.append("invalid_resolution")

    if duration > 3600:
        warnings.append("源视频超过 1 小时，建议使用长视频策略或先切分")
        flags.append("very_long_source")

    report = {
        "version": "source_quality_check_v1",
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "quality_flags": flags,
        "duration_seconds": duration,
        "width": width,
        "height": height,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "size_bytes": size,
    }
    write_json(output_path, report)
    return report
```

## pipeline 增加 step

```python
def step_source_quality_check(self) -> None:
    metadata = self._load_step_json("metadata")
    source = self._source_video()

    input_hash = stable_hash({
        "metadata": self._step_content_hash("metadata"),
        "source_quality_version": "source_quality_check_v1",
    })
    if self._can_reuse("source_quality_check", input_hash):
        print("复用缓存: source_quality_check")
        return

    version, vdir = self._version_dir("source_quality_check", "preprocess/source_quality")
    ensure_dir(vdir)

    from .media_quality import build_source_quality_report

    out = vdir / "source_quality_report.json"
    report = build_source_quality_report(
        video=source,
        metadata=metadata,
        output_path=out,
    )

    status = "success" if report.get("ok") else "failed"
    self._write_status(vdir, self._base_status("source_quality_check", version, input_hash, [out]))
    self._record_step(
        step="source_quality_check",
        version=version,
        status=status,
        output=relpath(out, self.task_dir),
        input_hash=input_hash,
        output_files=[out],
        extra={
            "warnings": report.get("warnings", []),
            "errors": report.get("errors", []),
        },
    )

    if not report.get("ok"):
        raise UserFacingPipelineError(
            "source quality check failed",
            user_message="源视频质量检查失败：" + "；".join(report.get("errors", [])[:5]),
            technical_detail=report,
        )

    print("完成: source_quality_check")
```

---

# 十三、P2-2：Web job 持久化

## 问题原因

`web_app.py` 目前有内存队列：

```python
JOBS = {}
PENDING_JOB_IDS = deque()
```



任务启动是 subprocess，日志写到 `web_jobs/job_id.log`。

问题是：

* Web 服务重启，内存队列丢。
* running 状态无法恢复。
* 多个 Web 服务进程之间互相不知道。
* pending job 可能丢失。

## 新增文件

```text
newsclip_agent/job_store.py
```

```python
from __future__ import annotations

from pathlib import Path
from typing import Any
from datetime import datetime

from .utils import ensure_dir, read_json, write_json


class JobStore:
    def __init__(self, root: Path):
        self.root = ensure_dir(root)

    def job_path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.json"

    def save(self, job: dict[str, Any]) -> None:
        payload = dict(job)
        payload.pop("process", None)
        payload.pop("log_file", None)
        payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
        write_json(self.job_path(payload["job_id"]), payload)

    def load_all(self) -> list[dict[str, Any]]:
        jobs = []
        for path in sorted(self.root.glob("*.json")):
            item = read_json(path, {})
            if item:
                jobs.append(item)
        return jobs

    def mark_orphan_running_as_failed(self) -> None:
        for job in self.load_all():
            if job.get("status") == "running":
                job["status"] = "failed"
                job["returncode"] = -1
                job["user_message"] = "Web 服务重启后，原运行进程状态无法确认，已标记为异常终止。"
                self.save(job)
```

`web_app.py` 增加：

```python
from newsclip_agent.job_store import JobStore

JOB_STORE = JobStore(OUTPUTS_DIR / "__jobs__")
```

改 `_persist_job`：

```python
def _persist_job(job: dict[str, Any]) -> None:
    JOB_STORE.save(job)
    write_json(OUTPUTS_DIR / job["task_id"] / "last_web_job.json", _public_job(job))
```

启动时恢复 pending：

```python
@app.on_event("startup")
def startup_restore_jobs() -> None:
    JOB_STORE.mark_orphan_running_as_failed()
    for job in JOB_STORE.load_all():
        if job.get("status") == "pending":
            JOBS[job["job_id"]] = job
            PENDING_JOB_IDS.append(job["job_id"])

    _refresh_remote_station_counts()
    _schedule_jobs()
```

---

# 十四、P2-3：`run_web.py` 增强启动参数，但兼容原命令

## 当前问题

`run_web.py` 现在只支持：

```bash
python run_web.py
python run_web.py --no-preflight
```

host 固定 `0.0.0.0`，端口固定从 7860 开始找。

你现在经常要并行跑 7860 / 7870，这里应该改成可配置。

## 替换版

```python
from __future__ import annotations

import argparse
import socket
import sys

import uvicorn

from newsclip_agent.config import load_config
from newsclip_agent.asr_preflight import run_asr_preflight


def find_free_port(host: str = "127.0.0.1", start: int = 7860, limit: int = 20) -> int:
    for port in range(start, start + limit):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) != 0:
                return port
    raise RuntimeError(f"没有找到可用端口: {start}-{start + limit - 1}")


def preflight(config_path: str) -> None:
    config = load_config(config_path)
    funasr_cfg = config.funasr
    if not bool(funasr_cfg.get("startup_check", True)):
        print("跳过启动前 ASR 环境自检")
        return

    print("启动前检查：FFmpeg / CUDA / FunASR / 模型路径 ...")
    report = run_asr_preflight(
        config,
        smoke_test=bool(funasr_cfg.get("startup_smoke_test", False)),
    )

    if not report.get("ok"):
        print("启动前检查失败：")
        print(report)
        raise SystemExit(2)

    print("启动前检查通过")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--port-start", type=int, default=7860)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--no-preflight", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if not args.no_preflight:
        preflight(args.config)

    port = args.port or find_free_port(host="127.0.0.1", start=args.port_start)
    print(f"Web 工作台地址: http://{args.host}:{port}")
    uvicorn.run("web_app:app", host=args.host, port=port, reload=False)
```

兼容原来的：

```bash
python run_web.py
```

也支持：

```bash
python run_web.py --port 7870 --no-preflight
python run_web.py --port-start 7870
```

---

# 十五、P2-4：PipelineRunner 拆分，避免继续变成上帝类

## 当前问题

`PipelineRunner` 现在承担了：

* 配置加载
* manifest 管理
* LLM client 管理
* ASR
* 预处理
* 多源分析
* 时间线
* 所有文本 agent
* TTS
* 字幕
* cut plan
* render
* 缓存
* 兼容输出

`PipelineRunner.__init__` 里就完成了配置加载、目录初始化、manifest、LLM client 创建等工作。

`run()` 通过 `getattr(self, f"step_{step}")` 动态执行所有步骤。

这能跑，但会越来越难维护。

## 建议拆分方式

不要一次大重构。先做“轻拆分”。

新增：

```text
newsclip_agent/steps/preprocess_steps.py
newsclip_agent/steps/analysis_steps.py
newsclip_agent/steps/planning_steps.py
newsclip_agent/steps/render_steps.py
newsclip_agent/steps/quality_steps.py
```

第一阶段只是把函数搬出去，保留 `PipelineRunner.step_xxx()` 入口。

例如：

```python
# newsclip_agent/steps/quality_steps.py

def run_voiceover_quality_gate(runner) -> None:
    # 把 step_voiceover_quality_gate 的实际逻辑放这里
    ...

def run_news_quality_gate(runner) -> None:
    # 把 step_news_quality_gate 的实际逻辑放这里
    ...
```

`pipeline.py` 中保留：

```python
from .steps import quality_steps

def step_voiceover_quality_gate(self) -> None:
    return quality_steps.run_voiceover_quality_gate(self)

def step_news_quality_gate(self) -> None:
    return quality_steps.run_news_quality_gate(self)
```

这样不会破坏现有调用链。

---

# 十六、P2-5：config.toml 安全改造

## 当前问题

`config.toml` 里直接放了 LLM API key 和 base_url。这个本地调试可以，进仓库不安全。

## 建议改法

把：

```toml
text_doubao_api_key = "xxx"
vision_doubao_api_key = "xxx"
```

改成：

```toml
text_doubao_api_key_env = "DOUBAO_TEXT_API_KEY"
vision_doubao_api_key_env = "DOUBAO_VISION_API_KEY"
```

`pipeline.py` 的 `_make_llm()` 改：

```python
def _make_llm(self, kind: str) -> OpenAICompatibleClient | None:
    llm = self.config.llm
    provider = llm.get(f"{kind}_llm_provider", "openai")
    if provider not in ("openai", "doubao"):
        return None

    key_env = str(llm.get(f"{kind}_{provider}_api_key_env") or "")
    key = os.environ.get(key_env) if key_env else ""
    if not key:
        key = llm.get(f"{kind}_{provider}_api_key")

    base_url = llm.get(f"{kind}_{provider}_base_url")

    if not key or not base_url:
        return None

    return OpenAICompatibleClient(
        api_key=key,
        base_url=base_url,
        timeout=int(llm.get(f"llm_{kind}_timeout", llm.get("llm_text_timeout", 180))),
        max_retries=int(llm.get("llm_max_retries", 3)),
    )
```

---

# 十七、最终推荐执行顺序

## 第 1 批：当天就改

只改这几个，风险小、收益大：

```text
1. pipeline.py：timeline_digest 加 source_id/source_index/local_start/local_end
2. pipeline.py：加 voiceover_quality_gate
3. pipeline.py：加 _filter_materialized_candidate_clips()
4. pipeline.py：升级 AGENT_INFO prompt version
5. prompt_texts/content_analysis.txt：输出增加 main_topic
```

这批改完后，多源稳定性和 AI 配音稳定性会明显提升。

---

## 第 2 批：第二轮改

```text
1. 新增 workflow_registry.py
2. web_app.py 改为读取 workflow_registry
3. pipeline.py 改为读取 workflow_registry
4. run_multisource_pipeline.py 改为读取 workflow_registry
5. 补充 news_quality_gate + news_quality_ai_review 到 render 前质量链路
```

这批改完后，流程维护会稳定很多，不会再出现 Web 和 pipeline 步骤漂移。

---

## 第 3 批：第三轮改

```text
1. 新增 resource_manager.py
2. LLM / ASR / TTS 调用加跨进程 slot lock
3. 新增 source_quality_check
4. run_web.py 支持 --port / --port-start / --config
5. 新增 job_store.py
```

这批改完后，系统更接近实际生产环境。

---

## 第 4 批：后续重构

```text
1. PipelineRunner 拆 steps 模块
2. Pydantic schema 校验所有 LLM 输出
3. media_quality 增加黑屏、静音、花屏检测
4. subtitle / tts / cut_plan 做更细的对齐校验
5. 所有 step 的 input_hash 统一纳入 prompt_hash / model / config
```

---

# 十八、最终系统标准

改完后，你这个系统应该变成下面这种分工：

```text
LLM 负责：
- ASR 摘要
- 画面事实描述
- 内容理解
- 候选 chunk 选择
- clip 排序
- 配音文案生成
- 新闻软审校：判断事实误导、资料画面误用、跨源拼接风险

代码负责：
- 时间码
- clip_id
- source_id
- shot_id
- duration
- 多源边界
- 缓存
- 断点续跑
- 质量门禁
- 字幕对齐
- TTS 对齐
- render 前硬校验
- AI 审校结果规范化与阻断策略
- 资源调度
```

这才是适合新闻自动剪辑的架构。

---

# 十九、最终结论

你现在这版比之前更接近工业级，尤其是这三个方向是对的：

1. **模型输出变轻。**
2. **代码 materialize 工程结构。**
3. **删除部分 LLM 步骤，减少耗时。**

但删掉 `candidate_refine / merge_decision / voiceover_quality_check` 后，必须立刻补代码规则和质量门禁。

最重要的最终优化点是：

```text
1. 修复 timeline_digest 缺 source_id。
2. 增加 voiceover_quality_gate。
3. 增加 candidate filter。
4. 增加 news_quality_gate。
5. 增加 news_quality_ai_review。
6. 统一 workflow_registry。
7. 增加全局资源限流。
```

这 6 个做完，项目就会从“能跑通的 AI 剪辑 demo/工作台”，升级到“有工程稳定性和新闻业务边界的自动剪辑系统”。
