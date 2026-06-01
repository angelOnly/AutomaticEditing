# AI 配音剪辑「目标时长与最终时长不一致」问题分析与优化方案

## 1. 问题背景

当前 AI 配音剪辑流程中，出现了如下报错：

```text
RuntimeError: Final compact video duration is below target requirements; blocked render:
compact video duration 46.5s is below 85% of target 55.0s; rerun voiceover_script with richer narration or merge supporting clips
```

这个错误说明：

- 系统规划阶段希望生成一条 **55 秒左右** 的 AI 配音解说视频；
- 实际 TTS 配音音频生成后，最终 compact 视频时长只有 **46.5 秒**；
- 代码设置了最低合格比例 `compact_min_target_ratio = 0.85`；
- 因此最低合格线是 `55 * 0.85 = 46.75 秒`；
- 实际 `46.5 秒` 比最低线少 `0.25 秒`；
- 当前代码没有容错，所以直接阻断渲染。

这次问题和之前 13 秒、21 秒碎片视频不同。之前属于严重过短，确实应该阻断；这次只差 0.25 秒，属于边界误杀。

---

## 2. 这个报错到底是什么意思？

报错中的核心信息是：

```text
compact video duration 46.5s is below 85% of target 55.0s
```

拆开解释：

| 字段 | 含义 |
|---|---|
| target 55.0s | 前面短视频规划步骤希望这条视频做成 55 秒左右 |
| 85% | 最低合格比例，防止目标 55 秒最后只生成十几秒 |
| 46.75s | 55 秒乘以 85% 得到的最低合格线 |
| compact video duration 46.5s | TTS 配音实际时长，也是最终 compact 视频时长 |
| blocked render | 系统认为低于最低线，所以停止继续渲染 |

这里的关键点是：**AI 配音模式下，最终视频时长基本由 TTS 音频实际时长决定，而不是由 target_duration_seconds 强行决定。**

---

## 3. 目标 55 秒是谁定的？

目标时长来自短视频规划步骤，一般是：

```text
short_video_edit_plan
```

该步骤会根据：

- 原视频内容；
- 候选片段数量；
- 用户设置的时长策略；
- 是否 AI 配音；
- 是否允许较长视频；
- 大模型对新闻完整性的判断；

生成类似下面的规划：

```json
{
  "short_video_id": "v_001",
  "topic": "美伊边谈边打，停火谈判卡在核问题",
  "target_duration_seconds": 55,
  "max_allowed_seconds": 60,
  "source_clip_ids": ["clip_001", "clip_002", "clip_003", "clip_004"]
}
```

所以，**55 秒不是最终视频的绝对时长，而是规划层给出的期望时长**。

它的作用是指导后续流程：

1. 让配音文案按 55 秒左右写；
2. 让剪辑计划按 55 秒左右安排画面；
3. 让质量检查判断最终结果有没有严重偏离目标；
4. 让系统避免生成过短碎片。

更准确的理解是：

```text
target_duration_seconds = 期望成片时长，不是强制精确时长
```

---

## 4. 最低合格比例 85% 是什么？

最低合格比例的作用是防止“规划是完整视频，但最终生成碎片视频”。

例如：

```text
目标 55 秒
实际 18 秒
```

如果没有最低合格比例，系统会继续渲染，最后用户就会看到一条明显过短、信息不完整的视频。

所以需要一个保护线：

```text
最终视频时长至少达到目标时长的一定比例
```

现在设置的是：

```python
compact_min_target_ratio = 0.85
```

也就是：

```text
最终时长 >= 目标时长 * 85%
```

对于目标 55 秒：

```text
55 * 0.85 = 46.75 秒
```

这条规则的初衷是对的，主要用于拦截下面这种情况：

| 目标时长 | 实际时长 | 是否应该拦截 |
|---:|---:|---|
| 45 秒 | 13 秒 | 应该拦截 |
| 50 秒 | 21 秒 | 应该拦截 |
| 55 秒 | 30 秒 | 应该拦截 |
| 55 秒 | 46.5 秒 | 不应该硬拦截，只应 warning |

当前问题不是“最低合格比例不该有”，而是：**最低合格比例缺少容错和分级处理。**

---

## 5. 为什么最终生成时长和目标对不上？

AI 配音剪辑模式的实际流程是：

```text
short_video_edit_plan 规划目标时长
        ↓
voiceover_script 生成配音文案
        ↓
TTS 根据配音文案生成音频
        ↓
cut_plan 根据 TTS 音频时长安排画面
        ↓
ffmpeg 渲染最终视频
```

在 compact_segmented 模式下，最终视频时长通常约等于 TTS 音频时长。

也就是说：

```text
最终视频时长 ≈ 配音音频时长
配音音频时长 ≈ 配音文案字数 / TTS 实际语速
```

所以如果目标是 55 秒，但配音文案写得不够长，最终 TTS 就可能只有 46 秒左右。

### 5.1 为什么不能直接把 46.5 秒强行拉到 55 秒？

理论上可以，但效果通常不好：

1. 如果拉慢音频，声音会不自然；
2. 如果后面补空镜头，会出现无声拖时间；
3. 如果画面延长但配音结束，观众会感觉视频断掉；
4. 如果强行重复素材，观感会变差。

所以当前设计采用的是：

```text
AI 配音模式下，视频最终时长跟随 TTS 实际时长。
```

这本身是合理的，但需要更好的质量控制机制。

---

## 6. 当前代码逻辑的问题

当前代码大概是这样的：

```python
min_compact_duration = target_duration * self.duration_settings.compact_min_target_ratio

if (
    tts_success
    and tts_item.get("timeline_mode") == "compact_segmented"
    and min_compact_duration > 0
    and video_duration < min_compact_duration
):
    raise RuntimeError(
        "Final compact video duration is below target requirements; blocked render:\n" + reason
    )
```

问题在于：

```python
video_duration < min_compact_duration
```

这是一个非常硬的判断，没有考虑：

- TTS 语速浮动；
- 视频编码时长误差；
- 字幕对齐误差；
- 片段拼接误差；
- 0.2 秒、0.5 秒这种边界差异；
- “轻微偏短”和“严重过短”的区别。

因此这次：

```text
目标 55 秒
最低线 46.75 秒
实际 46.5 秒
差距 0.25 秒
```

也被直接阻断。

这属于规则过严导致的误杀。

---

## 7. 优化目标

本次优化不是取消最低合格比例，而是把“单一硬阻断”改成“分级质量控制”。

目标：

1. 保留防碎片能力，继续拦截 13 秒、21 秒这种严重过短视频；
2. 增加 1 秒左右容错，避免 46.5 秒这种接近合格的视频被误杀；
3. 区分严重过短、明显偏短、轻微偏短；
4. 对轻微偏短只记录 warning，不阻断；
5. 对明显偏短才阻断；
6. 从源头优化 voiceover_script，让配音文案更接近目标时长；
7. 输出更清晰的错误信息，方便判断是该重写配音稿，还是允许继续。

---

## 8. 推荐的分级判断标准

建议把目标时长判断分成三层：

### 8.1 严重过短

```text
实际时长 < 目标时长 * 75%
```

这种必须阻断。

例如目标 55 秒：

```text
55 * 75% = 41.25 秒
```

如果实际低于 41.25 秒，说明视频明显过短，需要重写配音稿或合并更多片段。

### 8.2 明显偏短

```text
实际时长 + 容错秒数 < 目标时长 * 85%
```

这种也阻断。

例如目标 55 秒：

```text
最低合格线 = 46.75 秒
容错 = 1 秒
```

如果实际时长低于：

```text
46.75 - 1 = 45.75 秒
```

则阻断。

### 8.3 轻微偏短

```text
目标时长 * 85% - 容错秒数 <= 实际时长 < 目标时长 * 85%
```

这种只 warning，不阻断。

例如目标 55 秒：

```text
45.75 秒 <= 实际时长 < 46.75 秒
```

这类视频基本可接受。

你的这次结果：

```text
实际 46.5 秒
最低线 46.75 秒
只差 0.25 秒
```

应该属于轻微偏短，允许继续。

---

## 9. 具体修改建议一：优化 compact duration 阻断逻辑

### 9.1 修改位置

文件：

```text
newsclip_agent/pipeline.py
```

函数：

```text
step_cut_plan
```

报错位置附近：

```text
pipeline.py line 1830 附近
```

当前逻辑大概是：

```python
if (
    tts_success
    and tts_item.get("timeline_mode") == "compact_segmented"
    and min_compact_duration > 0
    and video_duration < min_compact_duration
):
    reason = (
        f"compact video duration {video_duration:.1f}s is below "
        f"{self.duration_settings.compact_min_target_ratio:.0%} of target {target_duration:.1f}s; "
        "rerun voiceover_script with richer narration or merge supporting clips"
    )
    repair_reasons.append(reason)
    warnings.append(reason)
    raise RuntimeError(
        "Final compact video duration is below target requirements; blocked render:\n" + reason
    )
```

### 9.2 建议改成

```python
duration_tolerance_seconds = 1.0
severe_short_ratio = 0.75
severe_min_duration = target_duration * severe_short_ratio

if (
    tts_success
    and tts_item.get("timeline_mode") == "compact_segmented"
    and min_compact_duration > 0
):
    if video_duration < severe_min_duration:
        reason = (
            f"compact video duration {video_duration:.1f}s is severely below "
            f"{severe_short_ratio:.0%} of target {target_duration:.1f}s; "
            "rerun voiceover_script with richer narration or merge supporting clips"
        )
        repair_reasons.append(reason)
        warnings.append(reason)
        raise RuntimeError(
            "Final compact video duration is severely below target requirements; blocked render:\n"
            + reason
        )

    if video_duration + duration_tolerance_seconds < min_compact_duration:
        reason = (
            f"compact video duration {video_duration:.1f}s is below "
            f"{self.duration_settings.compact_min_target_ratio:.0%} of target {target_duration:.1f}s "
            f"with tolerance {duration_tolerance_seconds:.1f}s; "
            "rerun voiceover_script with richer narration or merge supporting clips"
        )
        repair_reasons.append(reason)
        warnings.append(reason)
        raise RuntimeError(
            "Final compact video duration is below target requirements; blocked render:\n"
            + reason
        )

    if video_duration < min_compact_duration:
        reason = (
            f"compact video duration {video_duration:.1f}s is slightly below "
            f"{self.duration_settings.compact_min_target_ratio:.0%} of target {target_duration:.1f}s, "
            f"but within tolerance {duration_tolerance_seconds:.1f}s; allow render."
        )
        repair_reasons.append(reason)
        warnings.append(reason)
```

这样当前案例会变成：

```text
46.5 秒低于 46.75 秒，但差距只有 0.25 秒，在 1 秒容错内，允许继续。
```

---

## 10. 具体修改建议二：把阈值放到配置里

不建议把所有参数都写死在 `pipeline.py` 里，建议放到 duration settings 或 options 里。

可以新增配置项：

```python
compact_duration_tolerance_seconds: float = 1.0
compact_severe_short_ratio: float = 0.75
```

如果项目中已有 `DurationSettings`，可以加到对应 dataclass：

```python
@dataclass
class DurationSettings:
    compact_min_target_ratio: float = 0.85
    compact_duration_tolerance_seconds: float = 1.0
    compact_severe_short_ratio: float = 0.75
```

然后代码改成：

```python
duration_tolerance_seconds = self.duration_settings.compact_duration_tolerance_seconds
severe_short_ratio = self.duration_settings.compact_severe_short_ratio
severe_min_duration = target_duration * severe_short_ratio
```

这样后续可以在配置文件里调，而不用每次改代码。

---

## 11. 具体修改建议三：优化 voiceover_script 字数控制

这次 55 秒目标只生成 46.5 秒，根本原因大概率是配音稿略短，或者 TTS 语速偏快。

所以除了在 cut_plan 阶段加容错，还要在配音文案阶段让大模型写得更接近目标。

### 11.1 修改位置

文件：

```text
newsclip_agent/prompts.py
```

Prompt：

```text
VOICEOVER_LIGHT_PROMPT
```

### 11.2 增加规则

建议加入：

```text
AI 配音文案时长控制规则：

1. narration_text 必须贴近 target_duration_seconds，不是随便写一段摘要。
2. 中文新闻解说按每秒 3.0-3.5 个汉字估算。
3. 如果 target_duration_seconds = 45 秒，narration_text 建议 145-160 个汉字，最低不低于 135 个汉字。
4. 如果 target_duration_seconds = 50 秒，narration_text 建议 160-175 个汉字，最低不低于 150 个汉字。
5. 如果 target_duration_seconds = 55 秒，narration_text 建议 175-195 个汉字，最低不低于 165 个汉字。
6. 如果 target_duration_seconds = 60 秒，narration_text 建议 190-210 个汉字，最低不低于 180 个汉字。
7. 不允许 target_duration_seconds=55，但只输出 120 字左右的短文案。
8. 如果事实不足以支撑目标时长，duration_fit 必须输出 too_short，并在 revise_suggestion 中建议合并更多 source_clip_ids 或降低 target_duration_seconds。
9. 配音稿要分为：开场钩子、背景交代、核心事实、冲突/分歧、影响/风险、结尾收束。
```

### 11.3 为什么要这样做？

因为最终时长主要由 TTS 音频决定，而 TTS 音频主要由文案字数决定。

如果希望目标 55 秒，配音文案不能只写 130-140 字。  
更稳妥的是写到 170-190 字附近。

---

## 12. 具体修改建议四：配音文案校验不要过硬

之前建议加过“配音文案过短则阻断”的逻辑。但这里也要注意，不要把校验写得太死。

例如：

```python
min_chars = int(target * 3.0)
```

如果目标 55 秒：

```text
最低字数 = 165 字
```

如果实际 160 字，TTS 语速慢一点也可能生成接近 50 秒的视频，不一定要直接阻断。

所以建议改成分级：

### 12.1 严重不足

```text
实际字数 < target * 2.5
```

阻断。

目标 55 秒：

```text
55 * 2.5 = 137.5 字
```

如果低于 138 字，基本可以认为明显不足。

### 12.2 轻微不足

```text
target * 2.5 <= 实际字数 < target * 3.0
```

只 warning，不阻断。

目标 55 秒：

```text
138-164 字
```

可以继续，但记录 warning。

### 12.3 达标

```text
实际字数 >= target * 3.0
```

通过。

### 12.4 推荐代码

文件：

```text
newsclip_agent/pipeline.py
```

函数：

```text
_validate_voiceover_script_duration_or_raise
```

建议实现为：

```python
def _validate_voiceover_script_duration_or_raise(self, voiceover_output: dict[str, Any]) -> None:
    scripts = voiceover_output.get("scripts", [])
    if not isinstance(scripts, list):
        return

    hard_issues: list[str] = []
    soft_warnings: list[str] = []

    for script in scripts:
        if not isinstance(script, dict):
            continue

        short_video_id = script.get("short_video_id", "")
        target = float(script.get("target_duration_seconds") or 0)
        narration_text = str(script.get("narration_text") or "")
        actual_chars = len(narration_text.replace(" ", "").replace("\n", ""))

        hard_min_chars = int(target * 2.5)
        soft_min_chars = int(target * 3.0)

        if target >= 40 and actual_chars < hard_min_chars:
            hard_issues.append(
                f"{short_video_id} AI配音文案严重过短：目标 {target:.0f}s，"
                f"硬性最低 {hard_min_chars} 字，实际 {actual_chars} 字。"
            )
        elif target >= 40 and actual_chars < soft_min_chars:
            soft_warnings.append(
                f"{short_video_id} AI配音文案略短：目标 {target:.0f}s，"
                f"建议至少 {soft_min_chars} 字，实际 {actual_chars} 字。"
            )

        duration_fit = str(script.get("duration_fit") or "")
        if duration_fit == "too_short":
            hard_issues.append(
                f"{short_video_id} duration_fit=too_short，不能继续生成。"
            )

    if soft_warnings:
        # 如果项目有统一 warning 写入机制，建议写入对应 step 输出。
        print(
            "Voiceover duration soft warnings:\n"
            + "\n".join(f"- {x}" for x in soft_warnings)
        )

    if hard_issues:
        raise RuntimeError(
            "AI配音文案严重低于目标时长要求，已阻断后续 TTS/剪辑流程：\n"
            + "\n".join(f"- {x}" for x in hard_issues)
        )
```

注意：

- 不要因为轻微不足就阻断；
- 真正阻断的是严重不足；
- 最终是否接近目标，还要由 TTS 实际时长和 cut_plan 阶段再判断。

---

## 13. 具体修改建议五：让 cut_plan 错误提示更可理解

当前错误提示对开发者有用，但对用户不直观。

建议改成：

```text
最终成片略低于目标时长要求：
目标时长：55.0 秒
最低合格线：46.75 秒
实际时长：46.5 秒
差距：0.25 秒
处理方式：差距在 1.0 秒容错内，允许继续渲染，仅记录 warning。
```

如果阻断，则提示：

```text
最终成片明显低于目标时长要求，已停止渲染：
目标时长：55.0 秒
最低合格线：46.75 秒
实际时长：42.0 秒
差距：4.75 秒
建议：重新生成更长配音稿，或合并更多素材片段。
```

推荐封装一个 helper：

```python
def _format_compact_duration_message(
    self,
    video_duration: float,
    target_duration: float,
    min_compact_duration: float,
    tolerance_seconds: float,
    action: str,
) -> str:
    gap = max(0.0, min_compact_duration - video_duration)
    return (
        f"compact video duration check: "
        f"actual={video_duration:.1f}s, "
        f"target={target_duration:.1f}s, "
        f"min_required={min_compact_duration:.1f}s, "
        f"gap={gap:.1f}s, "
        f"tolerance={tolerance_seconds:.1f}s, "
        f"action={action}"
    )
```

---

## 14. 具体修改建议六：不要让 target_duration_seconds 随便变大

如果系统合并多个片段后直接把目标设成 55 或 60 秒，但配音文案和素材支撑不足，就容易出现目标和实际不一致。

建议在合并短视频规划时，目标时长要遵循：

```python
target_duration = min(max(total_target, 45), 60)
```

但还需要结合事实点数量：

```python
fact_count = len(fact_points)

if fact_count <= 3:
    target_duration = min(target_duration, 45)
elif fact_count <= 5:
    target_duration = min(target_duration, 55)
else:
    target_duration = min(target_duration, 60)
```

这样避免事实点很少，却规划 60 秒，导致配音稿硬凑或者生成后偏短。

推荐合并目标时长策略：

```python
def _estimate_target_duration_for_merged_script(
    self,
    fact_points: list[str],
    source_clip_ids: list[str],
    total_target: float,
) -> float:
    fact_count = len(fact_points)
    clip_count = len(source_clip_ids)

    if fact_count <= 3 and clip_count <= 2:
        return 45.0

    if fact_count <= 5:
        return min(max(total_target, 45.0), 55.0)

    return min(max(total_target, 50.0), 60.0)
```

---

## 15. 推荐最终方案

最终建议不是单点修改，而是三层一起优化：

### 第一层：规划层

优化 `short_video_edit_plan`：

- 默认优先生成一条完整视频；
- 合并后目标时长不要盲目设太长；
- 根据事实点数量估算目标时长；
- 避免只有少量信息却要求 60 秒。

### 第二层：配音层

优化 `voiceover_script`：

- 根据目标时长控制文案字数；
- 55 秒建议 175-195 字；
- 过短文案要 warning 或阻断；
- 不能目标 55 秒却只写 120 字。

### 第三层：剪辑质检层

优化 `cut_plan`：

- 保留最低合格比例 85%；
- 增加 1 秒容错；
- 增加严重过短线 75%；
- 轻微偏短只 warning；
- 明显偏短才 RuntimeError。

---

## 16. 建议 Codex 修改任务单

可以直接把下面这段发给 Codex：

```text
请优化 AI 配音剪辑模式下 target_duration_seconds 与最终 compact 视频时长不一致导致误阻断的问题。

当前问题：
目标时长 55s，compact_min_target_ratio=0.85，因此最低合格线是 46.75s。
实际 compact video duration 是 46.5s，只差 0.25s，但代码直接 raise RuntimeError，导致流程中断。
这属于轻微偏短，不应该阻断，只应记录 warning。

请按以下方案修改：

1. 修改 newsclip_agent/pipeline.py 中 step_cut_plan 里的 compact video duration 检查逻辑。
   当前逻辑是 video_duration < min_compact_duration 就直接 raise。
   请改成分级判断：
   - severe_short_ratio = 0.75，若 video_duration < target_duration * severe_short_ratio，则 raise RuntimeError。
   - duration_tolerance_seconds = 1.0，若 video_duration + duration_tolerance_seconds < min_compact_duration，则 raise RuntimeError。
   - 若 video_duration < min_compact_duration 但差距在 tolerance 内，只 warnings.append，不 raise。

2. 如果项目中存在 DurationSettings，请新增：
   - compact_duration_tolerance_seconds: float = 1.0
   - compact_severe_short_ratio: float = 0.75
   并在 step_cut_plan 中使用配置项，不要硬编码。

3. 优化错误提示，输出 actual、target、min_required、gap、tolerance、action，方便判断是严重过短、明显偏短，还是轻微偏短。

4. 修改 newsclip_agent/prompts.py 中 VOICEOVER_LIGHT_PROMPT。
   增加 AI 配音文案时长控制规则：
   - 45 秒建议 145-160 字，最低 135 字；
   - 50 秒建议 160-175 字，最低 150 字；
   - 55 秒建议 175-195 字，最低 165 字；
   - 60 秒建议 190-210 字，最低 180 字；
   - 不允许 target_duration_seconds=55 但只输出 120 字左右。
   - 如果事实不足以支撑目标时长，duration_fit 输出 too_short，并建议合并更多 source_clip_ids 或降低 target_duration_seconds。

5. 修改或新增 _validate_voiceover_script_duration_or_raise。
   不要对轻微字数不足直接阻断。
   建议：
   - actual_chars < target * 2.5：严重不足，raise RuntimeError；
   - target * 2.5 <= actual_chars < target * 3.0：只 warning；
   - actual_chars >= target * 3.0：通过。
   duration_fit=too_short 仍然直接 raise。

6. 增加测试：
   - target=55，video_duration=46.5，min_ratio=0.85，tolerance=1.0，不应 raise，只 warning。
   - target=55，video_duration=42，min_ratio=0.85，tolerance=1.0，应 raise。
   - target=55，video_duration=30，应按 severe_short_ratio 直接 raise。
   - target=55，narration_text 字数 130，应 raise。
   - target=55，narration_text 字数 155，应 warning，不 raise。
   - target=55，narration_text 字数 170，应通过。
```

---

## 17. 测试用例建议

新增测试文件：

```text
tests/test_compact_duration_policy.py
```

测试一：轻微偏短不阻断

```python
def test_compact_duration_slightly_below_min_should_warn_not_raise(pipeline):
    target_duration = 55.0
    video_duration = 46.5
    min_ratio = 0.85
    tolerance = 1.0

    min_required = target_duration * min_ratio

    assert video_duration < min_required
    assert video_duration + tolerance >= min_required
```

测试二：明显偏短阻断

```python
def test_compact_duration_below_min_beyond_tolerance_should_raise(pipeline):
    target_duration = 55.0
    video_duration = 42.0
    min_ratio = 0.85
    tolerance = 1.0

    min_required = target_duration * min_ratio

    assert video_duration + tolerance < min_required
```

测试三：严重过短阻断

```python
def test_compact_duration_severely_short_should_raise(pipeline):
    target_duration = 55.0
    video_duration = 30.0
    severe_ratio = 0.75

    assert video_duration < target_duration * severe_ratio
```

测试四：配音文案严重不足阻断

```python
def test_voiceover_chars_severely_short_should_raise(pipeline):
    target = 55
    actual_chars = 130
    hard_min = int(target * 2.5)

    assert actual_chars < hard_min
```

测试五：配音文案略短只 warning

```python
def test_voiceover_chars_slightly_short_should_warn(pipeline):
    target = 55
    actual_chars = 155
    hard_min = int(target * 2.5)
    soft_min = int(target * 3.0)

    assert actual_chars >= hard_min
    assert actual_chars < soft_min
```

测试六：配音文案达标

```python
def test_voiceover_chars_ok_should_pass(pipeline):
    target = 55
    actual_chars = 170
    soft_min = int(target * 3.0)

    assert actual_chars >= soft_min
```

---

## 18. 最终判断

这次错误的本质不是系统坏了，而是前面为了防止碎片视频加了强阻断，但阻断规则太硬。

正确优化方向是：

```text
不要取消最低合格比例；
不要让 13 秒、21 秒重新漏出去；
要增加容错；
要区分轻微偏短和严重过短；
要从配音文案阶段让内容更贴近目标时长。
```

最终建议落地为：

```text
1. 85% 最低合格比例保留；
2. 增加 1 秒容错；
3. 增加 75% 严重过短线；
4. 轻微偏短只 warning；
5. 明显偏短才阻断；
6. 55 秒配音文案建议写到 175-195 字；
7. 配音文案字数校验也分 warning 和 raise。
```

这样既能防止之前的 13 秒、21 秒碎片问题，又不会因为 46.5 秒和 46.75 秒这种边界差异误杀正常结果。
