# AI 配音时长不匹配失败优化方案与代码开发指南

> 适用仓库：`angelOnly/AutomaticEditing`  
> 适用分支：`clean-highlight-reassembly`  
> 入口：`python run_web.py`  
> 本文定位：在现有实现上做微调、补强和鲁棒性优化，不重构整体工作流，不大删大改。

---

## 1. 本次错误现象

本次日志里前端最终显示为：

```text
tts_failed_or_empty
TTS 生成失败：没有生成可用配音音频。
```

但从日志产物看，真实情况不是 TTS 完全失败。

`tts/omnivoice/v2/tts_outputs.json` 中实际已经生成了可用音频：

```json
{
  "short_video_id": "v_001",
  "status": "success",
  "actual_duration_seconds": 17.6,
  "voice_file_exists": true
}
```

真正失败点在 `tts_duration_reconcile.json`：

```json
{
  "ok": false,
  "target_total_duration_seconds": 33.8,
  "tts_total_duration_seconds": 17.6,
  "coverage_ratio": 0.521,
  "videos": [
    {
      "short_video_id": "v_001",
      "ok": false,
      "target_total_duration_seconds": 33.8,
      "tts_total_duration_seconds": 17.6,
      "coverage_ratio": 0.521,
      "total_check": {
        "status": "too_short",
        "hard": true
      }
    }
  ]
}
```

所以本次真实错误应理解为：

```text
TTS 音频已生成，但实际配音时长 17.6 秒，被系统拿去和 33.8 秒目标时长比较；
覆盖率只有 52.1%，低于 hard 阈值，因此 tts 步骤被整体判定为 failed。
```

---

## 2. 根因分析

### 2.1 short_video_edit_plan 阶段：规划目标和画面长度已经不一致

`agents/short_video_edit_plan/v4/short_video_edit_plan.json` 中，当前只选了一条视频：

```json
{
  "short_video_id": "v_001",
  "source_clip_ids": ["av_clip_0001"],
  "target_duration_seconds": 30.0,
  "visual_total_seconds": 33.8
}
```

这里说明系统的规划目标是：

```text
AI 配音短视频目标时长：30 秒
选中的原始画面片段总长：33.8 秒
```

这本身可以接受，因为 AI 配音模式下，后续可以根据配音节奏裁画面、压缩画面或重新安排画面。但后续代码没有把“配音目标时长”稳定传递下去。

### 2.2 editing_script 兼容输出丢失了 target_duration_seconds

`pipeline.py` 中 `_write_compat_short_video_plan_and_editing_script()` 会把 `short_video_edit_plan` 转成旧兼容结构 `editing_script`。

当前转换时，shot 只保留了：

```python
"duration_seconds": shot.get("duration_seconds", 0)
```

但没有保留：

```text
target_duration_seconds
min_duration_seconds
max_duration_seconds
narration_min_chars
narration_target_chars
narration_max_chars
```

这会导致后面的 TTS 校验无法知道“这一段配音本来应该按 30 秒目标分配”，只能退回使用 `duration_seconds=33.8`。

### 2.3 TTS 校验阶段把原始画面长度当成配音目标

`_build_tts_duration_reconcile()` 中，每个 shot 的目标时长是这样取的：

```python
target = float(shot.get("target_duration_seconds") or shot.get("duration_seconds") or 0)
```

由于 `editing_script` 中没有 `target_duration_seconds`，它就直接退回到 `duration_seconds=33.8`。

因此系统最终不是按 30 秒检查，而是按 33.8 秒检查。

### 2.4 字数预算出现自相矛盾

本次 `short_video_edit_plan` 中还出现了一个明显异常：

```json
{
  "narration_min_chars": 114,
  "narration_target_chars": 144,
  "narration_max_chars": 90
}
```

这个预算不合法，因为：

```text
min_chars > max_chars
```

这会让 voiceover_script 生成阶段和校验阶段都不稳定。比如模型生成 94 字时，系统会同时认为：

```text
94 字超过 max 90
94 字低于 min 114
```

这不是模型的问题，而是预算字段本身不一致。

### 2.5 错误类型误导

当前 TTS 后置检查失败时，最终抛出的错误类型仍然是：

```text
tts_failed_or_empty
TTS 生成失败：没有生成可用配音音频。
```

但本次真实情况是：

```text
TTS 生成成功，音频存在，只是时长校验失败。
```

所以前端错误提示也需要优化，否则会把问题误导到 OmniVoice 模型、参考音频、CUDA 或 TTS 环境上。

---

## 3. 优化目标

本次优化不建议靠放宽一个阈值解决，而是把下面几个概念统一：

```text
画面原始长度：selected clips 的真实 source duration
配音目标长度：AI voiceover 希望生成的短视频目标长度
文案字数预算：根据配音目标长度推导出的 min / target / max chars
TTS 实际长度：OmniVoice 实际生成出来的音频长度
最终成片长度：cut_plan / render 阶段根据配音和画面组合后的长度
```

优化后应满足：

1. AI 配音模式下，TTS 校验优先按 `target_duration_seconds` 检查，不应默认拿原始画面 `duration_seconds` 做目标。
2. `editing_script` 必须完整继承 `short_video_edit_plan` 中已经算好的目标时长和字数预算。
3. 字数预算必须保证 `min_chars <= target_chars <= max_chars`。
4. 当 TTS 已生成但时长不匹配时，错误类型应明确显示为 `tts_duration_mismatch` 或 `tts_duration_too_short`。
5. 继续兼容现有 Web 启动方式：`python run_web.py`。
6. 尽量只改 `pipeline.py`，配置项只做可选增强，不影响已有任务运行。

---

## 4. 需要修改的文件

### 必改

```text
newsclip_agent/pipeline.py
```

### 可选修改

```text
config.toml
```

### 不建议本轮修改

```text
run_web.py
web_app.py
run_pipeline.py
run_multisource_pipeline.py
workflow_registry.py
prompts.py
```

原因：本次问题不是 Web 启动、任务队列、工作流顺序或 prompt 主体导致的，而是 AI 配音规划产物在兼容转换、字数预算和 TTS 校验之间传递不一致。

---

## 5. 代码开发方案

## 5.1 修改一：editing_script 兼容输出保留时长预算和字数预算

### 修改位置

```text
newsclip_agent/pipeline.py
函数：_write_compat_short_video_plan_and_editing_script()
```

当前逻辑中，生成 `editing_structure` 时大致是：

```python
editing_structure.append({
    "order": shot.get("order", idx + 1),
    "shot_id": shot.get("shot_id", f"{script.get('short_video_id', 'sv')}_s{idx + 1:02d}"),
    "source_clip_id": shot.get("source_clip_id", ""),
    "source_id": shot.get("source_id", ""),
    "source_start": shot.get("source_start", ""),
    "source_end": shot.get("source_end", ""),
    "duration_seconds": shot.get("duration_seconds", 0),
    ...
})
```

### 优化方向

在兼容结构中补充保留以下字段：

```text
target_duration_seconds
min_duration_seconds
max_duration_seconds
narration_min_chars
narration_target_chars
narration_max_chars
local_start
local_end
local_start_seconds
local_end_seconds
source_start_seconds
source_end_seconds
```

### 建议代码

在 `_write_compat_short_video_plan_and_editing_script()` 内，构造每个 `editing_structure` item 时，将原来的 dict 微调为：

```python
source_duration = self._safe_positive_float(shot.get("duration_seconds"), default=0.0)
target_duration = self._safe_positive_float(
    shot.get("target_duration_seconds"),
    default=source_duration,
)
min_duration = self._safe_positive_float(
    shot.get("min_duration_seconds"),
    default=0.0,
)
max_duration = self._safe_positive_float(
    shot.get("max_duration_seconds"),
    default=0.0,
)

# 兜底：如果没有 min/max，按 target 给一个合理范围，不再只依赖原始画面 duration。
if target_duration > 0:
    if min_duration <= 0:
        min_duration = round(target_duration * 0.65, 3)
    if max_duration <= 0:
        max_duration = round(target_duration * 1.35, 3)

item = {
    "order": shot.get("order", idx + 1),
    "shot_id": shot.get("shot_id", f"{script.get('short_video_id', 'sv')}_s{idx + 1:02d}"),
    "target_timeline": shot.get("target_timeline", ""),
    "source_clip_id": shot.get("source_clip_id", ""),
    "source_id": shot.get("source_id", ""),
    "source_index": shot.get("source_index", ""),
    "source_start": shot.get("source_start", ""),
    "source_end": shot.get("source_end", ""),
    "source_start_seconds": shot.get("source_start_seconds"),
    "source_end_seconds": shot.get("source_end_seconds"),
    "local_start": shot.get("local_start", shot.get("source_start", "")),
    "local_end": shot.get("local_end", shot.get("source_end", "")),
    "local_start_seconds": shot.get("local_start_seconds", shot.get("source_start_seconds")),
    "local_end_seconds": shot.get("local_end_seconds", shot.get("source_end_seconds")),

    # 原始画面长度仍保留，供 cut_plan/render 使用。
    "duration_seconds": source_duration,

    # AI 配音目标长度必须保留，供 voiceover/TTS 校验使用。
    "target_duration_seconds": target_duration,
    "min_duration_seconds": min_duration,
    "max_duration_seconds": max_duration,

    # 文案预算必须保留，供 voiceover_script 和校验使用。
    "narration_min_chars": shot.get("narration_min_chars"),
    "narration_target_chars": shot.get("narration_target_chars"),
    "narration_max_chars": shot.get("narration_max_chars"),

    "purpose": shot.get("purpose", ""),
    "visual": shot.get("visual", ""),
    "visual_evidence_type": "",
    "visual_selection_reason": "",
    "importance": "must_keep",
    "audio_mode": "ai_voiceover",
    "original_audio_required": False,
    "subtitle": shot.get("subtitle_hint", ""),
    "editing_note": shot.get("news_fact_to_explain", ""),
    "news_fact_to_explain": shot.get("news_fact_to_explain", ""),
    "must_say_facts": shot.get("must_say_facts", []),
}

editing_structure.append(self._normalize_voiceover_budget_fields(item))
```

这里引用了两个新增小函数，下面会说明。

---

## 5.2 修改二：新增安全数值解析函数

### 修改位置

```text
newsclip_agent/pipeline.py
class PipelineRunner 内部
```

建议放在 `_first_number()` 附近，因为这里已有数值解析相关函数。

### 新增函数

```python
def _safe_positive_float(self, value: Any, *, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number <= 0:
        return default
    return round(number, 3)
```

### 作用

避免这些异常输入污染后续时长计算：

```text
None
""
"abc"
NaN
inf
-1
0
```

---

## 5.3 修改三：统一修正文案字数预算，保证 min <= target <= max

### 修改位置

```text
newsclip_agent/pipeline.py
class PipelineRunner 内部
```

建议新增函数：

```python
def _normalize_voiceover_budget_fields(self, shot: dict[str, Any]) -> dict[str, Any]:
    item = dict(shot)

    min_chars = self._first_number(item.get("narration_min_chars"))
    target_chars = self._first_number(item.get("narration_target_chars"))
    max_chars = self._first_number(item.get("narration_max_chars"))

    voice_cfg = self.config.raw.get("voiceover", {}) if hasattr(self, "config") else {}
    cps = float(voice_cfg.get("voiceover_chars_per_second") or 4.8)

    target_duration = self._first_number(item.get("target_duration_seconds"))
    if target_duration is None:
        target_duration = self._first_number(item.get("duration_seconds"))

    # 如果完全没有预算，则按目标时长估算。
    if target_duration and target_duration > 0:
        estimated_target = max(1, int(round(target_duration * cps)))
        if target_chars is None or target_chars <= 0:
            target_chars = estimated_target
        if min_chars is None or min_chars <= 0:
            min_chars = int(round(target_chars * 0.75))
        if max_chars is None or max_chars <= 0:
            max_chars = int(round(target_chars * 1.25))

    # 如果仍然没有预算，直接返回，避免误伤旧产物。
    if min_chars is None and target_chars is None and max_chars is None:
        return item

    # 补齐缺失值。
    if target_chars is None:
        if min_chars is not None and max_chars is not None:
            target_chars = int(round((min_chars + max_chars) / 2))
        elif min_chars is not None:
            target_chars = min_chars
        elif max_chars is not None:
            target_chars = max_chars

    if min_chars is None:
        min_chars = int(round(float(target_chars) * 0.75))
    if max_chars is None:
        max_chars = int(round(float(target_chars) * 1.25))

    min_chars = int(max(0, round(float(min_chars))))
    target_chars = int(max(0, round(float(target_chars))))
    max_chars = int(max(0, round(float(max_chars))))

    # 核心修复：不能出现 min > target > max 这种矛盾。
    if min_chars > target_chars:
        min_chars = target_chars
    if max_chars < target_chars:
        max_chars = target_chars
    if min_chars > max_chars:
        min_chars = max_chars

    # 对很短 shot 做保护，避免 0 字预算。
    if target_duration and target_duration > 0:
        min_floor = 6 if target_duration < 3 else 10
        target_chars = max(target_chars, min_floor)
        max_chars = max(max_chars, target_chars)
        min_chars = min(max(min_chars, min_floor), target_chars)

    item["narration_min_chars"] = min_chars
    item["narration_target_chars"] = target_chars
    item["narration_max_chars"] = max_chars
    return item
```

### 这个函数解决的问题

把本次这种非法预算：

```json
{
  "narration_min_chars": 114,
  "narration_target_chars": 144,
  "narration_max_chars": 90
}
```

修正成合法区间，例如：

```json
{
  "narration_min_chars": 114,
  "narration_target_chars": 144,
  "narration_max_chars": 144
}
```

或者根据你的策略，也可以修成：

```json
{
  "narration_min_chars": 90,
  "narration_target_chars": 90,
  "narration_max_chars": 90
}
```

更推荐第一种，因为如果目标时长是 30 秒，90 字通常偏短，容易再次造成 TTS 实际时长不足。

---

## 5.4 修改四：在 short_video_edit_plan 物化后也做一次预算归一化

### 修改位置

```text
newsclip_agent/pipeline.py
函数：_materialize_short_video_edit_plan()
```

当前流程中，代码已经会做：

```python
editing_structure = self._assign_shot_duration_budget(editing_structure, target_duration)
editing_structure = self._attach_voiceover_char_budget(editing_structure)
```

建议在这两行之后补一层统一校验：

```python
editing_structure = [
    self._normalize_voiceover_budget_fields(shot)
    for shot in editing_structure
    if isinstance(shot, dict)
]
```

### 作用

即使 `_attach_voiceover_char_budget()` 里某些边界条件算出了不合法预算，也能在写入 `short_video_edit_plan.json` 前兜底修正。

这个是鲁棒性补强，不改变主流程。

---

## 5.5 修改五：TTS 校验目标优先使用 AI 配音目标，不再默认拿原始画面长度硬比

### 修改位置

```text
newsclip_agent/pipeline.py
函数：_build_tts_duration_reconcile()
```

当前逻辑：

```python
target = float(shot.get("target_duration_seconds") or shot.get("duration_seconds") or 0)
min_d = float(shot.get("min_duration_seconds") or (target * 0.65 if target else 0))
max_d = float(shot.get("max_duration_seconds") or (target * 1.35 if target else 0))
```

### 优化方向

新增一个函数专门解析 TTS 对齐目标，避免到处重复：

```python
def _resolve_tts_check_target_duration(self, shot: dict[str, Any]) -> tuple[float, float, float]:
    target = self._safe_positive_float(shot.get("target_duration_seconds"), default=0.0)
    source_duration = self._safe_positive_float(shot.get("duration_seconds"), default=0.0)

    # AI 配音模式：优先使用配音目标时长。
    if target <= 0 and self.options.production_mode == "ai_voiceover":
        target = source_duration

    # 非 AI 配音或旧产物：保留旧行为，兼容 duration_seconds。
    if target <= 0:
        target = source_duration

    min_d = self._safe_positive_float(shot.get("min_duration_seconds"), default=0.0)
    max_d = self._safe_positive_float(shot.get("max_duration_seconds"), default=0.0)

    if target > 0:
        if min_d <= 0:
            min_d = round(target * 0.65, 3)
        if max_d <= 0:
            max_d = round(target * 1.35, 3)

    if max_d > 0 and min_d > max_d:
        min_d = round(max_d * 0.75, 3)
    if target > 0:
        if min_d > target:
            min_d = round(target * 0.75, 3)
        if max_d < target:
            max_d = round(target * 1.25, 3)

    return target, min_d, max_d
```

然后在 `_build_tts_duration_reconcile()` 中替换为：

```python
target, min_d, max_d = self._resolve_tts_check_target_duration(shot)
```

### 重点说明

这个函数不是为了无脑放宽检查，而是为了明确优先级：

```text
AI 配音目标时长 > shot 目标预算 > 原始画面长度
```

原始画面长度仍然可以保留给 cut_plan 和 render，但不应在 AI 配音校验里默认成为硬目标。

---

## 5.6 修改六：错误类型和用户提示细化

### 修改位置

```text
newsclip_agent/pipeline.py
函数：_step_tts_impl()
```

当前失败时统一使用：

```python
error_type = "tts_missing_segments" if missing_tts_segments else ("tts_outputs_empty" if not outputs else "tts_failed_or_empty")
raise UserFacingPipelineError(
    error_type,
    user_message="TTS 生成失败：没有生成可用配音音频。",
    ...
)
```

### 优化方向

根据失败原因区分错误类型：

```python
if missing_tts_segments:
    error_type = "tts_missing_segments"
    user_message = "TTS 生成失败：部分 narration_segments 没有成功生成音频。"
elif not outputs:
    error_type = "tts_outputs_empty"
    user_message = "TTS 生成失败：没有生成任何配音输出。"
elif reconcile_hard_videos or reconcile_hard_segments or not reconcile_ok:
    error_type = "tts_duration_mismatch"
    user_message = "TTS 已生成音频，但配音时长与目标时长严重不匹配。"
else:
    error_type = "tts_failed_or_empty"
    user_message = "TTS 生成失败：没有生成可用配音音频。"

raise UserFacingPipelineError(
    error_type,
    user_message=user_message,
    suggestions=[
        "优先从 short_video_edit_plan 重新运行，重新生成配音目标时长和文案预算。",
        "检查 agents/short_video_edit_plan/v*/short_video_edit_plan.json 中 target_duration_seconds 与 narration_*_chars 是否合理。",
        "检查 agents/voiceover_script/v*/voiceover_script.json 的 narration_text 是否过短。",
        "检查 tts_duration_reconcile.json 中 target_total_duration_seconds、tts_total_duration_seconds、coverage_ratio。",
    ],
    technical_detail={
        "success": success,
        "failed": failed,
        "total": len(outputs),
        "error": hard_error,
        "missing": missing_tts_segments,
        "tts_duration_reconcile": reconcile,
    },
)
```

### 优化后的前端表现

本次这种情况应该显示为：

```text
TTS 已生成音频，但配音时长与目标时长严重不匹配。
目标时长：33.8 秒
实际 TTS：17.6 秒
覆盖率：52.1%
建议：从 short_video_edit_plan 重新运行，或检查文案字数预算。
```

这样用户不会误以为是 TTS 模型坏了。

---

## 5.7 可选修改：配置项补充，但不强依赖

### 修改位置

```text
config.toml
[voiceover]
```

可选增加：

```toml
# TTS 整体时长校验阈值
# ok_min 以下但高于 hard_min：warning
# hard_min 以下：failed
# 注意：这些配置只是阈值，不应代替 target_duration_seconds 传递修复。
tts_script_ok_min_ratio = 0.85
tts_script_hard_min_ratio = 0.65
tts_script_ok_max_ratio = 1.20
tts_script_hard_max_ratio = 1.50

# 分段 TTS 校验阈值
tts_segment_ok_min_ratio = 0.70
tts_segment_hard_min_ratio = 0.50
tts_segment_ok_max_ratio = 1.30
tts_segment_hard_max_ratio = 1.70
```

当前代码已有默认值读取逻辑，即使不加配置也能运行。因此配置项是增强可读性，不是必须。

---

## 6. 不建议的改法

### 6.1 不建议只去掉 `--max-output-video-seconds`

`--max-output-video-seconds` 主要影响规划上限，不是这次失败的直接原因。

本次日志中命令已经是：

```text
--target-duration 30
--max-output-video-seconds 45
--production-mode ai_voiceover
--rerun-from short_video_edit_plan
```

问题仍然发生，说明不是 `--max-output-video-seconds 90 --allow-long-video` 造成的。

### 6.2 不建议直接关闭 TTS duration reconcile

如果直接跳过 `_build_tts_duration_reconcile()`，短期能跑通，但会带来新问题：

```text
配音太短，画面太长，最终成片大段无声或节奏异常。
```

正确做法是让 reconcile 使用正确目标，而不是关闭 reconcile。

### 6.3 不建议把 hard_min_ratio 无脑降到 0.5 以下

这会掩盖文案过短问题。比如目标 30 秒，TTS 只有 12 秒，也可能被放过，最终成片质量会明显变差。

---

## 7. 边界条件与鲁棒性要求

### 7.1 旧任务兼容

旧任务可能没有这些字段：

```text
target_duration_seconds
min_duration_seconds
max_duration_seconds
narration_min_chars
narration_target_chars
narration_max_chars
```

处理原则：

```text
有 target_duration_seconds 时优先使用；
没有时继续兼容 duration_seconds；
不要因为旧字段缺失直接失败。
```

### 7.2 单 shot 与多 shot

单 shot 情况：

```text
visual_total_seconds 可能大于 target_duration_seconds。
TTS 校验应以 target_duration_seconds 为主。
```

多 shot 情况：

```text
每个 shot 应有独立 target_duration_seconds；
总 target_total_duration_seconds 应等于各 shot target 之和；
不要只拿整条视频 target 平均分配后丢失到兼容结构。
```

### 7.3 特别短的片段

对于 1-3 秒的小片段：

```text
不要生成过高 min_chars；
否则模型很难塞入足够文字。
```

建议：

```text
target_duration < 3 秒时，min_chars 最低 6；
target_duration >= 3 秒时，min_chars 最低 10。
```

### 7.4 TTS 速度差异

OmniVoice 真实语速受以下因素影响：

```text
reference_audio
reference_text
speed
中文标点
停顿
模型生成节奏
```

所以字数估算只能作为预算，不应要求完全等于目标时长。

建议保留区间判断：

```text
正常：85% - 120%
警告：65% - 85% 或 120% - 150%
失败：低于 65% 或高于 150%
```

具体阈值可以走配置。

### 7.5 TTS 实际成功但文件不存在

仍需保留当前检查：

```text
status=success 但 voice_file_exists=false，应判定异常。
```

不能因为本次要修 duration mismatch，就放宽真正的 TTS 文件缺失。

### 7.6 compact_segmented 模式

当前 AI 配音会走 `compact_segmented`，即把各 segment TTS 紧凑拼接。

这意味着：

```text
TTS 实际总时长可能天然短于原始画面总长。
```

所以 TTS 校验不能死盯原始画面时长，而应盯配音目标时长。

---

## 8. 开发顺序

建议按以下顺序开发，避免一次改太多不好定位：

### 第一步：增加两个辅助函数

```text
_safe_positive_float()
_normalize_voiceover_budget_fields()
```

先不改流程，只增加函数。

### 第二步：在 _materialize_short_video_edit_plan() 后加预算归一化

位置：

```python
editing_structure = self._assign_shot_duration_budget(editing_structure, target_duration)
editing_structure = self._attach_voiceover_char_budget(editing_structure)
editing_structure = [self._normalize_voiceover_budget_fields(shot) for shot in editing_structure if isinstance(shot, dict)]
```

跑一次，确认 `short_video_edit_plan.json` 不再出现：

```text
narration_min_chars > narration_max_chars
```

### 第三步：修改 _write_compat_short_video_plan_and_editing_script()

确保 `editing_script.json` 中每个 shot 都有：

```text
target_duration_seconds
min_duration_seconds
max_duration_seconds
narration_min_chars
narration_target_chars
narration_max_chars
```

### 第四步：修改 _build_tts_duration_reconcile()

把：

```python
target = float(shot.get("target_duration_seconds") or shot.get("duration_seconds") or 0)
```

替换为：

```python
target, min_d, max_d = self._resolve_tts_check_target_duration(shot)
```

### 第五步：细化 TTS 错误类型

将 `tts_failed_or_empty` 细分为：

```text
tts_outputs_empty
tts_missing_segments
tts_duration_mismatch
tts_failed_or_empty
```

### 第六步：补配置项说明，可选

如果想让阈值更透明，再补 `config.toml`。

---

## 9. 测试步骤

### 9.1 单元级检查

准备一个临时 shot：

```python
shot = {
    "duration_seconds": 33.8,
    "target_duration_seconds": 30.0,
    "narration_min_chars": 114,
    "narration_target_chars": 144,
    "narration_max_chars": 90,
}
```

调用：

```python
normalized = runner._normalize_voiceover_budget_fields(shot)
```

预期：

```text
normalized["narration_min_chars"] <= normalized["narration_target_chars"] <= normalized["narration_max_chars"]
```

### 9.2 从 short_video_edit_plan 重跑

Web 上建议从失败任务执行：

```text
rerun_from = short_video_edit_plan
```

命令层面等价于：

```bash
python run_pipeline.py \
  --task-id <任务ID> \
  --rerun-from short_video_edit_plan \
  --production-mode ai_voiceover \
  --target-duration 30
```

如果是 Web 多源任务，仍由 `python run_web.py` 调度，不需要直接手写 `run_multisource_pipeline.py`。

### 9.3 检查 short_video_edit_plan.json

重点看：

```text
scripts[0].target_duration_seconds
scripts[0].visual_total_seconds
scripts[0].editing_structure[*].target_duration_seconds
scripts[0].editing_structure[*].narration_min_chars
scripts[0].editing_structure[*].narration_target_chars
scripts[0].editing_structure[*].narration_max_chars
```

预期：

```text
不能再出现 min_chars > max_chars。
```

### 9.4 检查 editing_script.json

重点看：

```text
agents/editing_script/v*/editing_script.json
scripts[0].editing_structure[*].target_duration_seconds
```

预期：

```text
兼容 editing_script 中也有 target_duration_seconds。
TTS 校验不会再只能退回 duration_seconds。
```

### 9.5 检查 voiceover_script.json

重点看：

```text
narration_text 字数
narration_segments[*].text 字数
narration_segments[*].target_duration_seconds
```

预期：

```text
文案字数和 target_duration_seconds 大致匹配。
```

### 9.6 检查 tts_duration_reconcile.json

重点看：

```text
target_total_duration_seconds
tts_total_duration_seconds
coverage_ratio
total_check.status
total_check.hard
```

预期：

```text
target_total_duration_seconds 应接近 30 秒，而不是无条件等于原始画面 33.8 秒。
如果 TTS 仍过短，错误应显示 tts_duration_mismatch，而不是 tts_failed_or_empty。
```

---

## 10. 回滚方案

因为本方案主要改 `pipeline.py`，回滚很简单。

### 10.1 Git 回滚

开发前先建分支：

```bash
git checkout -b fix-ai-voiceover-duration-budget
```

如果需要回滚：

```bash
git checkout clean-highlight-reassembly -- newsclip_agent/pipeline.py config.toml
```

### 10.2 运行回滚

如果已经生成了错误的新任务产物，不建议手动改 JSON。直接：

```text
删除当前任务输出目录，或新建任务重新跑。
```

因为旧的 `short_video_edit_plan / editing_script / voiceover_script / tts_outputs` 之间有版本依赖，手动混用容易造成缓存复用错误。

---

## 11. 风险点

### 11.1 可能让部分短配音任务从 failed 变成 warning

这是符合预期的。因为之前有些任务是被错误目标时长挡住的。

但不能无脑放过所有短音频，所以仍保留 hard 阈值。

### 11.2 字数预算变大后，voiceover_script 可能更长

如果 `narration_max_chars` 被修正变大，模型可能生成更长文案。

这通常是好事，因为本次失败就是文案/TTS 太短。但需要防止超长，所以仍保留 `max_chars` 和 TTS too_long 校验。

### 11.3 旧任务缓存可能复用旧产物

如果只从 `tts` 重跑，仍会使用旧的 `editing_script` 和 `voiceover_script`。

所以修复后必须建议从：

```text
short_video_edit_plan
```

重新跑，而不是只重跑 TTS。

### 11.4 多源任务 common 分析不应重复跑

本次修改只影响模式专属阶段：

```text
short_video_edit_plan
editing_script
voiceover_script
tts
```

不应该要求重新跑：

```text
source_prepare
source_analysis
source_aggregate
```

除非上游素材本身变化。

---

## 12. 最终建议落地版本

建议本轮按“小步修复”落地，不做大重构：

```text
1. pipeline.py 新增安全数值解析和预算归一化函数。
2. short_video_edit_plan 物化后统一修正文案预算。
3. editing_script 兼容输出继承 target_duration_seconds 和 narration_*_chars。
4. TTS duration reconcile 使用统一目标解析函数。
5. TTS 错误提示区分 duration mismatch 和真正 TTS 失败。
6. config.toml 仅可选补充阈值说明。
```

这样能解决本次问题，同时保持现有 Web 启动方式、任务队列、多源 common 复用和工作流顺序不变。

---

## 13. 修复后的预期链路

修复前：

```text
short_video_edit_plan: target=30, visual=33.8
↓
editing_script 丢失 target_duration_seconds
↓
TTS 校验 target=duration_seconds=33.8
↓
TTS 实际 17.6
↓
17.6 / 33.8 = 0.521
↓
failed: tts_failed_or_empty
```

修复后：

```text
short_video_edit_plan: target=30, visual=33.8
↓
editing_script 保留 target_duration_seconds 和 narration_*_chars
↓
TTS 校验 target≈30
↓
如果 TTS 实际仍明显过短：failed: tts_duration_mismatch
如果 TTS 接近目标：继续 subtitles / cut_plan / render
↓
cut_plan/render 再按配音实际长度调整画面
```

---

## 14. 本次问题的一句话总结

这次不是 OmniVoice 没生成，也不是 Web 参数 `--max-output-video-seconds` 单独导致；核心是：

```text
AI 配音目标时长在 short_video_edit_plan 到 editing_script 的兼容转换中丢失，
导致 TTS 校验阶段把 33.8 秒原始画面长度当成配音目标；
同时字数预算出现 min_chars > max_chars，进一步导致文案过短和校验不稳定。
```

优先修复“目标时长传递”和“字数预算合法性”，再优化错误提示。
