# AI 配音 micro_segment 链路最终优化方案

## 0. 本方案目标

本方案用于修正 AI 配音链路中 `asr_micro_segment -> content_analysis -> ai_voiceover_candidate_materialize -> short_video_edit_plan -> voiceover_script -> tts -> cut_plan -> render` 的长度控制、候选片段物化、失败诊断和空结果拦截问题。

这次重点不是简单把 `max_clip_seconds` 调大，而是重新定义：

```text
micro_segment 是事实颗粒。
candidate_clip 是画面承载片段。
short_video_edit_plan 是成片组织单位。
```

因此，三者不能使用同一个硬阈值，也不应该出现“上游允许 35 秒，下游 30 秒直接失败”的配置冲突。

---

## 1. 当前问题总结

### 1.1 直接报错

当前失败点是：

```text
ai_voiceover_candidate_overlong
```

含义是：

```text
content_analysis 已经选中了 micro_segment，
ai_voiceover_candidate_materialize 在给 micro_segment 加 padding 后，
发现候选片段超过 ai_voiceover_candidate.max_clip_seconds，
并且 fail_on_overlong = true，
所以流程提前失败。
```

### 1.2 根因不是 ASR 失败

这次不是：

```text
ASR 没内容
micro_segment 没生成
content_analysis 空结果
LLM 没返回
TTS 失败
```

而是候选片段物化阶段的长度阈值过死。

### 1.3 当前配置的核心矛盾

旧配置大致是：

```toml
[asr_micro_segment]
hard_max_segment_seconds = 35.0

[ai_voiceover_candidate]
padding_before_seconds = 0.5
padding_after_seconds = 0.5
max_clip_seconds = 30.0
fail_on_overlong = true
truncate_overlong = false
```

这会导致：

```text
asr_micro_segment 阶段允许 35 秒 micro_segment。
candidate 阶段只允许 30 秒 candidate_clip。
candidate 还要额外加前后 padding。
```

实际安全长度不是 30 秒，而是：

```text
30 - padding_before - padding_after
```

也就是说，上游合法的 micro_segment，下游会直接失败。

这个不是 fail-fast 的问题，而是上下游协议没有对齐。

---

## 2. 最终长度策略

### 2.1 micro_segment 的定位

`micro_segment` 不应该是一个完整故事，也不应该是最终成片片段。

它应该是：

```text
一个可被 AI 配音文案独立引用的事实动作、表态、结果、冲突点或背景信息。
```

也就是“事实颗粒”。

### 2.2 micro_segment 推荐长度

从新闻素材实际剪辑角度，推荐：

```text
最小长度：4 秒
理想长度：8～22 秒
偏长但可接受：22～35 秒
硬上限：35 秒
超过 35 秒：原则上必须拆
```

对应配置：

```toml
[asr_micro_segment]
min_segment_seconds = 4.0
preferred_min_segment_seconds = 8
preferred_max_segment_seconds = 22
hard_max_segment_seconds = 35.0
split_overlong_group = true
```

注意：

```text
hard_max_segment_seconds = 35.0 不是鼓励生成 35 秒片段，
而是允许少数完整表态、连续现场动作、发布会回答不要被硬拆碎。
```

常规情况下，micro_segment 应该集中在 8～22 秒。

---

### 2.3 candidate_clip 的定位

`candidate_clip` 是后续 `short_video_edit_plan` 可以选择的画面候选。

它可以比 micro_segment 稍长，因为它需要保留：

```text
画面起势
动作完整性
前后呼吸
现场连续性
转场安全边界
```

所以 candidate_clip 不应该比 micro_segment 更短。

### 2.4 candidate_clip 推荐长度

推荐：

```text
最小长度：5 秒
理想长度：10～35 秒
偏长但允许：35～50 秒
硬上限：60 秒
超过 60 秒：失败或要求人工确认
```

对应配置：

```toml
[ai_voiceover_candidate]
padding_before_seconds = 0.5
padding_after_seconds = 0.8

min_clip_seconds = 5.0
preferred_max_clip_seconds = 45.0
max_clip_seconds = 60.0

fail_on_empty = true
fail_on_overlong = true
fail_on_short = false
truncate_overlong = false
```

这里的重点是：

```text
preferred_max_clip_seconds = 45.0 是软上限。
max_clip_seconds = 60.0 是硬上限。
```

即：

```text
45 秒以内：正常。
45～60 秒：允许，但写 warning / diagnostics。
60 秒以上：fail-fast。
```

这样可以避免真实新闻中 36 秒、42 秒这种合理片段被直接拦死。

---

## 3. 推荐最终配置

建议将 `config.toml` 中相关部分调整为：

```toml
[asr_micro_segment]
enabled = true
source = "raw_asr"
fail_on_empty = true
fail_on_empty_groups = true
allow_sentence_fallback = false

min_segment_seconds = 4.0
preferred_min_segment_seconds = 8
preferred_max_segment_seconds = 22
hard_max_segment_seconds = 35.0

window_sentence_count = 40
window_overlap_sentence_count = 5
max_sentence_chars = 120
max_segments_for_content_analysis = 120
include_visual_summary = true
visual_summary_chars = 160
split_overlong_group = true
drop_empty_text_segment = true

[ai_voiceover_candidate]
padding_before_seconds = 0.5
padding_after_seconds = 0.8

min_clip_seconds = 5.0
preferred_max_clip_seconds = 45.0
max_clip_seconds = 60.0

fail_on_empty = true
fail_on_overlong = true
fail_on_short = false
truncate_overlong = false

[compat]
allow_ai_voiceover_candidate_from_content_analysis = false
allow_asr_micro_segment_from_timeline_digest = false
allow_legacy_ai_voiceover_chunk_content_analysis = false
```

### 3.1 为什么不建议关闭 fail_on_overlong

不建议简单改成：

```toml
fail_on_overlong = false
```

原因是这会把真实错误重新隐藏掉。

正确做法是：

```text
合理偏长：warning。
严重超长：fail-fast。
```

也就是增加软上限 `preferred_max_clip_seconds`，而不是取消硬失败。

### 3.2 为什么不建议默认 truncate_overlong

不建议默认：

```toml
truncate_overlong = true
```

因为 candidate_clip 时间来自 raw ASR / micro_segment。如果静默截断，很可能切掉一句话后半段、关键动作或事实闭环。

推荐策略是：

```text
超过 45 秒：记录 long_but_allowed_clips。
超过 60 秒：失败，并输出具体 micro_segment_id / source_id / duration。
```

---

## 4. pipeline.py 修改方案

主要修改文件：

```text
newsclip_agent/pipeline.py
```

---

## 4.1 修改 step_ai_voiceover_candidate_materialize()

### 4.1.1 新增 preferred_max_clip_seconds

在读取 candidate 配置时，增加：

```python
preferred_max_clip_seconds = float(cfg.get("preferred_max_clip_seconds", 45.0) or 0.0)
```

原有的 `max_clip_seconds` 保留，作为硬上限。

---

### 4.1.2 diagnostics 增加 long_but_allowed_clips

原 diagnostics 中如果只有 `overlong_clips`，会把所有超过阈值的片段都视为失败。

建议改为：

```python
diagnostics = {
    "short_clips": [],
    "long_but_allowed_clips": [],
    "overlong_clips": [],
}
```

含义：

```text
short_clips：低于 min_clip_seconds 的片段。
long_but_allowed_clips：超过 preferred_max_clip_seconds，但没超过 max_clip_seconds 的片段。
overlong_clips：超过 max_clip_seconds 的片段。
```

---

### 4.1.3 长度判断改为软硬两层

候选片段生成后，先计算：

```python
raw_duration = padded_end - padded_start
```

然后使用两层判断：

```python
if preferred_max_clip_seconds > 0 and raw_duration > preferred_max_clip_seconds:
    diagnostics["long_but_allowed_clips"].append({
        "micro_segment_id": micro_segment_id,
        "source_id": source_id,
        "duration_seconds": round(raw_duration, 3),
        "preferred_max_clip_seconds": preferred_max_clip_seconds,
        "max_clip_seconds": max_clip_seconds,
    })

if max_clip_seconds > 0 and raw_duration > max_clip_seconds:
    diagnostics["overlong_clips"].append({
        "micro_segment_id": micro_segment_id,
        "source_id": source_id,
        "duration_seconds": round(raw_duration, 3),
        "max_clip_seconds": max_clip_seconds,
        "truncated": truncate_overlong,
    })
    if truncate_overlong:
        padded_end = padded_start + max_clip_seconds
```

行为变成：

```text
42 秒：不失败，写入 long_but_allowed_clips。
51 秒：不失败，写入 long_but_allowed_clips。
61 秒：写入 overlong_clips，并在 fail_on_overlong=true 时失败。
```

---

## 4.2 materialize_debug.json 增加 duration_stats

不要在代码里写 `...`。

`...` 只是方案里的占位符，实际代码必须计算出真实数值。

建议在生成 debug 前增加：

```python
durations = [
    float(clip.get("duration_seconds") or 0)
    for clip in candidate_clips
    if float(clip.get("duration_seconds") or 0) > 0
]

duration_stats = {
    "min": round(min(durations), 3) if durations else 0.0,
    "max": round(max(durations), 3) if durations else 0.0,
    "avg": round(sum(durations) / len(durations), 3) if durations else 0.0,
    "count": len(durations),
    "over_preferred_count": len(diagnostics["long_but_allowed_clips"]),
    "over_hard_count": len(diagnostics["overlong_clips"]),
}
```

然后写入 debug：

```python
debug = {
    "selected_segment_count": len(content.get("selected_segments") or []),
    "micro_segment_count": len(micro_segments),
    "candidate_clip_count": len(candidate_clips),
    "missing_segment_ids": missing_segment_ids,
    "diagnostics": diagnostics,
    "duration_stats": duration_stats,
    "config": {
        "padding_before_seconds": padding_before,
        "padding_after_seconds": padding_after,
        "min_clip_seconds": min_clip_seconds,
        "preferred_max_clip_seconds": preferred_max_clip_seconds,
        "max_clip_seconds": max_clip_seconds,
        "truncate_overlong": truncate_overlong,
        "fail_on_overlong": fail_on_overlong,
        "fail_on_short": fail_on_short,
    },
}
```

生成出来的 `materialize_debug.json` 应类似：

```json
{
  "duration_stats": {
    "min": 8.42,
    "max": 47.86,
    "avg": 22.31,
    "count": 6,
    "over_preferred_count": 1,
    "over_hard_count": 0
  }
}
```

这样后续排查时可以直接知道：

```text
候选片段数量是多少
最短多少秒
最长多少秒
平均多少秒
有几个超过软上限
有几个超过硬上限
```

---

## 4.3 overlong 报错信息补强

当前报错只提示 `AI 配音候选片段存在超过 max_clip_seconds 的片段`，但不够直观。

建议 `technical_detail` 保留完整 debug，并在 user_message / suggestions 中明确：

```text
哪些 micro_segment_id 超长
来自哪个 source_id
实际 duration_seconds 是多少
当前 max_clip_seconds 是多少
```

建议错误信息结构：

```python
raise UserFacingPipelineError(
    "ai_voiceover_candidate_overlong",
    user_message="AI 配音候选片段超过硬上限，请拆分超长 micro_segment 或调大 ai_voiceover_candidate.max_clip_seconds。",
    suggestions=[
        "查看 ai_voiceover_candidates/*/materialize_debug.json 中的 diagnostics.overlong_clips。",
        "如果片段在 45～60 秒之间，应只作为 warning，不应失败。",
        "如果片段超过 60 秒，建议回到 asr_micro_segment 阶段拆分。",
    ],
    technical_detail=debug,
)
```

---

## 5. micro_segment 合并逻辑修正

### 5.1 当前隐患

`_enforce_micro_segment_limits()` 的处理顺序通常是：

```text
先拆超过 hard_max 的 segment。
再合并太短的 segment。
```

隐患是：

```text
拆完以后是合法的。
但合并短段后，可能又合并出一个超过 hard_max 的 micro_segment。
```

### 5.2 修改原则

短段可以合并，但合并后不能超过 `hard_max_segment_seconds`。

建议 `_merge_too_short_adjacent_segments()` 增加 `hard_max` 参数。

调用从：

```python
fixed = self._merge_too_short_adjacent_segments(fixed, min_seg)
```

改为：

```python
fixed = self._merge_too_short_adjacent_segments(fixed, min_seg, hard_max)
```

函数签名从：

```python
def _merge_too_short_adjacent_segments(self, segments, min_seg):
```

改为：

```python
def _merge_too_short_adjacent_segments(self, segments, min_seg, hard_max):
```

合并前增加判断：

```python
merged_duration = float(nxt.get("end_seconds") or 0) - float(current.get("start_seconds") or 0)

can_merge = (
    current.get("duration_seconds", 0) < min_seg
    and current.get("source_id") == nxt.get("source_id")
    and (hard_max <= 0 or merged_duration <= hard_max)
)
```

只有 `can_merge = true` 才合并。

### 5.3 增加二次校验

`_enforce_micro_segment_limits()` 建议变为三步：

```text
第一步：超过 hard_max 的先按句子边界拆。
第二步：短段谨慎合并，合并后不得超过 hard_max。
第三步：合并后再次检查，仍超过 hard_max 的继续拆或 fail-fast。
```

不建议静默截断 micro_segment。

原因：

```text
micro_segment 的边界来自 raw ASR 和句子组。
静默截断可能切断一句话、事实闭环或画面动作。
```

---

## 6. content_analysis 选择偏好补强

`content_analysis` 的职责是从 micro_segments 中选择适合 AI 配音的事实颗粒。

这里不应该让模型生成时间码，也不应该让模型修改时间。

但可以在输入说明中加入选择偏好：

```text
优先选择 10～35 秒内的 micro_segment。
22～35 秒属于偏长片段，只有在完整表态、连续现场动作、不可拆事实时才选择。
不要因为片段长就自动丢弃，但不要选择多个信息混杂的长片段。
同一事实重复出现时，优先选择画面更清楚、信息更完整的一段。
```

注意：

```text
content_analysis 仍然只输出 micro_segment_id。
不要输出 source_start/source_end。
不要让 LLM 自己改时间。
```

---

## 7. short_video_edit_plan 配合调整

candidate 上限放宽后，必须防止 `short_video_edit_plan` 连续选择多个长 clip。

建议在 `SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT` 或构造输入说明中加入：

```text
优先使用 10～35 秒 candidate_clip。
35～50 秒 candidate_clip 只有在信息完整、画面连续、不可拆时使用。
不要连续选择多个偏长 candidate_clip。
如果一个长 clip 已经覆盖主要事实，不要再重复选择同主题 clip。
最终成片要有开头事实、核心解释、结尾信息，不要堆素材。
```

代码层面继续保持：

```text
short_video_edit_plan 只能读取 ai_voiceover_candidate_materialize 产出的 candidate_clips。
不能回读 content_analysis.candidate_clips。
不能在模型空输出时自动选前 N 个 clip。
```

---

## 8. fail-fast 策略

本方案不是取消 fail-fast，而是把 fail-fast 放在真正严重的问题上。

### 8.1 必须 fail-fast 的情况

以下情况必须失败：

```text
raw_asr_index 缺失或为空。
asr_micro_segment 输出为空。
content_analysis.selected_segments 为空。
ai_voiceover_candidate_materialize 生成 candidate_clips 为空。
candidate_clip 超过 max_clip_seconds 硬上限。
short_video_edit_plan 输出 videos / selected clips 为空。
voiceover_script.scripts 为空。
narration_segments 为空。
require_tts = true 但 tts outputs 为空。
cut_plan clips 为空。
```

### 8.2 只应该 warning 的情况

以下情况不应该失败：

```text
candidate_clip 超过 preferred_max_clip_seconds，但没有超过 max_clip_seconds。
micro_segment 处于 22～35 秒偏长区间。
candidate_clip 处于 45～60 秒偏长区间。
某个片段略短，但不是无效时间。
```

---

## 9. 产物导出补强

这次排查中还暴露一个问题：失败任务的压缩包里不一定包含失败步骤的完整 debug 产物。

建议检查 Web 导出逻辑，确保失败时也能导出：

```text
ai_voiceover_candidates/v*/candidate_clips.json
ai_voiceover_candidates/v*/materialize_debug.json
manifest.json
web_jobs/*.log
```

尤其是 `materialize_debug.json`，它是排查 overlong / short / missing segment 的关键文件。

如果失败步骤已经写出 debug，但导出包没有包含，应该修 Web 打包清单。

---

## 10. 开发顺序

建议分阶段开发，不要一次性大改。

### 第一步：先改配置

修改：

```text
config.toml
```

重点：

```text
candidate max_clip_seconds 从 30 改为 60。
新增 preferred_max_clip_seconds = 45。
padding 改为 before 0.5 / after 0.8。
micro_segment preferred_max 改为 22，hard_max 保持 35。
```

先验证这次失败任务能否通过 `ai_voiceover_candidate_materialize`。

### 第二步：改 candidate diagnostics

修改：

```text
newsclip_agent/pipeline.py
step_ai_voiceover_candidate_materialize()
```

增加：

```text
preferred_max_clip_seconds
long_but_allowed_clips
duration_stats
更详细的 overlong technical_detail
```

### 第三步：修 micro_segment 合并逻辑

修改：

```text
_enforce_micro_segment_limits()
_merge_too_short_adjacent_segments()
```

目标：

```text
短段可以合并，但合并后不得超过 hard_max_segment_seconds。
合并后做二次校验。
```

### 第四步：补 content_analysis 选择偏好

修改：

```text
_build_content_analysis_micro_segment_text()
或对应 content_analysis prompt
```

目标：

```text
让模型优先选择 10～35 秒事实颗粒。
偏长片段只在必要时选择。
```

### 第五步：补 short_video_edit_plan 选择策略

修改：

```text
SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT
或对应输入构造说明
```

目标：

```text
避免连续选择多个 35～50 秒长 clip。
```

### 第六步：检查失败产物导出

修改：

```text
web_app.py 或相关打包 / 下载逻辑
```

目标：

```text
失败时也能导出 materialize_debug.json。
```

---

## 11. 测试方案

### 11.1 当前失败任务复测

使用这次失败的素材重新跑 AI 配音链路。

预期：

```text
不再因为 30 秒以上 candidate_clip 直接失败。
45～60 秒片段进入 long_but_allowed_clips。
超过 60 秒才进入 overlong_clips 并失败。
candidate_clips.json 正常生成。
materialize_debug.json 正常生成 duration_stats。
```

### 11.2 普通 2～3 分钟新闻测试

预期：

```text
micro_segment 大多数集中在 8～22 秒。
candidate_clip 大多数集中在 10～35 秒。
long_but_allowed_clips 数量较少。
overlong_clips 应该为 0。
```

### 11.3 发布会 / 长讲话素材测试

预期：

```text
允许少数 35～50 秒 candidate_clip。
short_video_edit_plan 不应连续选择多个长 clip。
voiceover_script 不应生成过短解说覆盖过长画面。
```

### 11.4 异常素材测试

测试：

```text
ASR 空。
content_analysis 空。
candidate_clips 空。
voiceover_script 空。
tts outputs 空。
cut_plan clips 空。
```

预期：

```text
停在对应步骤。
不要继续跑到 news_quality_gate 才失败。
```

### 11.5 视频重组模式回归测试

预期：

```text
视频重组模式不走 asr_micro_segment。
不走 ai_voiceover_candidate_materialize。
继续走原声重组自己的 candidate / filter / reassembly plan 链路。
```

---

## 12. 回滚方案

如果改完后发现片段明显偏长，先不要回滚架构，只回调参数。

### 12.1 温和回调

```toml
[ai_voiceover_candidate]
preferred_max_clip_seconds = 40.0
max_clip_seconds = 55.0
```

### 12.2 更严格回调

```toml
[ai_voiceover_candidate]
preferred_max_clip_seconds = 38.0
max_clip_seconds = 50.0
```

### 12.3 不建议回滚到 30 秒硬上限

不建议恢复：

```toml
max_clip_seconds = 30.0
```

原因是这会重新制造：

```text
真实新闻片段合理，但流程因为长度阈值过死直接失败。
```

---

## 13. 最终原则

最终方案可以概括为：

```text
AI 配音 micro_segment 按事实颗粒控制：8～22 秒为主，35 秒封顶。
candidate_clip 按画面承载控制：10～35 秒为主，45 秒以上 warning，60 秒封顶。
超过软上限不失败，超过硬上限才 fail-fast。
```

同时继续坚持：

```text
不静默 fallback。
不空结果 success。
不让 LLM 自己改时间码。
不把 content_analysis 的结果直接当 candidate_clip 使用。
short_video_edit_plan 只能读取物化后的 candidate_clips。
失败时必须导出可排查 debug。
```

这样既能保留 fail-fast 的可靠性，又不会因为 30 秒硬限制把真实新闻里的合理长片段直接拦死。
