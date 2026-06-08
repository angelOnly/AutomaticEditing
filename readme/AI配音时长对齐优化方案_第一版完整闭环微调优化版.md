# AI 配音 TTS 与视频时长对齐优化方案：第一版完整闭环微调版

> 本文档基于原《AI配音时长对齐优化方案（二次补充优化版 / 三次微调合并版）》做增补型收敛。
>
> 本次不推翻原方案主线，不大删大改，只把开发范围进一步收敛为“第一版完整闭环”：
>
> ```text
> tts_duration_reconcile
>   -> auto_duration_decision
>   -> adjusted_editing_script
>   -> cut_plan
>   -> subtitles
>   -> final_duration_check
>   -> render
> ```
>
> 核心目标：TTS 已经成功生成，但真实音频时长与原画面规划时长不匹配时，不再把任务误判为 TTS failed，而是通过确定性规则自动压缩画面，让 AI 配音视频继续生成。



## 0.1 本次在原文档上的微调范围

本次仍然坚持“第一版完整闭环”，不引入第二版、第三版那种复杂策略，不做大模型多轮补文案、不做 TTS 重跑、不做 hybrid repair。

但为了让方案能更稳地落到当前代码，需要在原方案基础上补充以下关键约束：

```text
1. 不能重复压缩：如果已经生成 adjusted_editing_script，cut_plan 阶段必须跳过现有 compact_to_tts 二次压缩。
2. 不能无脑 shrink：coverage_ratio 极低时要先判断 TTS 是否真的完整，避免把文案缺失、segment 丢失伪装成画面过长。
3. 不能只写文件不接链路：adjusted_editing_script 必须进入 cut_plan effective input 和 input_hash。
4. 字幕不需要重写一套：现有字幕逻辑已有 TTS segments 优先路径，本次重点是防缓存复用和最终校验。
5. final_duration_check 第一版可以作为 cut_plan/render 前门禁产物，不强制新增 workflow step。
```

本次优化后的核心目标从：

```text
生成 adjusted_editing_script
```

进一步明确为：

```text
让 adjusted_editing_script 真正成为 cut_plan 的 effective editing input，
并且保证后续 subtitles / render / cache / final check 都能感知这次调整。
```


---

# 一、本次优化结论

当前问题不是 TTS 模型失败，而是 **TTS 真实时长与规划画面时长不收敛**。

典型数据：

```text
规划画面时长：约 136 秒
TTS 实际时长：约 92 秒
coverage_ratio：约 0.675
差距：约 44 秒
```

旧逻辑的问题是：

```text
TTS 音频已经生成成功
  -> tts_duration_reconcile 发现 too_short
  -> 把 duration mismatch 当成 hard failure
  -> tts step failed
  -> cut_plan / subtitle / render 无法继续
```

新逻辑应该改成：

```text
TTS 音频已经生成成功
  -> tts_duration_reconcile 发现 too_short
  -> 判断这是 adjustable duration mismatch，不是 TTS fatal failure
  -> auto_duration_decision 决定 shrink_video
  -> adjusted_editing_script 根据 TTS 真实时长压缩画面
  -> cut_plan 使用 adjusted_editing_script
  -> subtitles 跟随 TTS 时间轴
  -> final_duration_check 通过后 render
```

一句话概括：

```text
旧逻辑：画面定死，TTS 不够就失败。
新逻辑：TTS 成功后，以真实 TTS 为主时间轴，自动缩画面继续出片。
```

---

# 二、本次明确只做第一版完整闭环

本方案不是临时绕过，也不是简单放宽阈值，而是把第一版做扎实：

```text
1. TTS 成功但 duration mismatch 时，不再直接 failed。
2. 保留并强化 tts_duration_reconcile.json。
3. 新增 auto_duration_decision.json。
4. 默认策略：AI 配音模式下，TTS 偏短时优先缩画面。
5. 新增 adjusted_editing_script.json，不覆盖原 editing_script。
6. cut_plan 优先读取 adjusted_editing_script。
7. subtitles 基于 TTS segments / cut_plan 重新对齐。
8. render 前增加 final_duration_check。
9. 所有新逻辑可通过配置关闭，可回滚。
```

## 2.1 本次不做的内容

这些内容可以后续增强，但不进入本次第一版主流程：

```text
1. 不做 post-TTS 自动补文案。
2. 不做补文案后重跑 TTS。
3. 不做 hybrid_repair_and_shrink。
4. 不做 LLM 多轮 duration decision。
5. 不做长版解说确认。
6. 不做 Web 三种用户模式。
7. 不做历史 TTS 语速校准。
8. 不强依赖完整 voiceover_precheck 作为第一版主流程。
```

原因：当前故障发生在 TTS 已成功之后。第一版要优先修好后 TTS 的确定性闭环，避免引入 LLM 重写文案、TTS 重跑、循环补救、事实扩写等复杂风险。

## 2.2 4.8 字/秒暂时写死

当前版本可以继续固定：

```text
voiceover_chars_per_second = 4.8
```

它只作为文案预算、日志、估算参考，不作为 post-TTS 对齐的核心依据。

TTS 后真正用于对齐的是：

```text
真实音频时长
真实 segment duration
真实 tts_total_duration_seconds
```

所以本版不需要做历史语速校准。

---

# 三、错误原因拆解

## 3.1 根因不是阈值太严格

不能简单把 mismatch 阈值放宽，否则会出现：

```text
TTS 92 秒
画面 136 秒
系统继续渲染
最终视频后面大量无解说、字幕断档、画面拖沓
```

真正缺的是：

```text
TTS 成功后的自动时长收敛层
```

## 3.2 当前链路缺口

当前链路大致是：

```text
editing_script
  -> voiceover_script
  -> tts
  -> tts_duration_reconcile
  -> failed
```

缺少：

```text
auto_duration_decision
adjusted_editing_script
cut_plan 使用 adjusted 版本
字幕跟随 TTS 重新对齐
render 前 final_duration_check
```

## 3.3 TTS failed 与 duration mismatch 被混在一起

必须拆开：

```text
TTS fatal failure：
- 音频没有生成
- 音频文件不存在
- required segment 缺失
- segment duration <= 0
- 模型调用异常
- voiceover_script 为空

adjustable duration mismatch：
- TTS 比画面短
- TTS 比画面长一点
- coverage_ratio 低
- 某些 segment too_short / too_long
```

只有第一类才应该让 tts step failed。第二类应该进入 auto_duration_decision。


## 3.4 当前代码中的实际拦截点

当前问题在代码里不是 OmniVoice 生成音频本身失败，而是在 `newsclip_agent/pipeline.py` 的 `_step_tts_impl()` 后半段：

```text
1. TTS outputs 已经生成。
2. _build_tts_duration_reconcile() 生成 reconcile。
3. reconcile_ok=false 或存在 reconcile_hard_videos / reconcile_hard_segments。
4. _step_tts_impl() 把 overall_status 设成 failed。
5. 抛出 UserFacingPipelineError(error_type="tts_duration_mismatch")。
```

因此开发时不要优先去改 TTS provider，也不要先去改文案 prompt。第一刀应该改的是：

```text
_step_tts_impl() 对 reconcile 结果的失败判定。
```

新的判断应该是：

```text
missing_audio / missing_required_segment / duration<=0  => fatal TTS failure
coverage_ratio too_low / too_short / too_long           => post-TTS duration decision
```

---

# 四、核心设计原则

## 4.1 AI 配音模式以 TTS 为主时间轴

AI 配音模式不同于原声重组模式。

```text
原声重组模式：原片画面和原声为主。
AI 配音模式：TTS 解说为主，画面围绕解说服务。
```

所以最终视频时长不能死守原始 editing_script 的画面规划，而应该在 TTS 生成后，根据真实 TTS 反向收敛画面。

## 4.2 只做确定性 shrink_video

第一版默认策略：

```text
TTS 明显短于规划画面时，不自动补文案，不重跑 TTS，优先缩画面。
```

原因：

```text
1. 新闻短视频不适合为了凑时长硬扩写。
2. post-TTS 补文案需要重新调用 LLM 和 TTS，容易形成循环。
3. LLM 为了凑时长可能重复、啰嗦甚至编造事实。
4. 当前案例更像画面规划偏长，而不是文案必须扩到 136 秒。
```

## 4.3 不覆盖原始产物

不能覆盖：

```text
editing_script.json
voiceover_script.json
tts_outputs.json
```

新增产物：

```text
tts_duration_reconcile.json
auto_duration_decision.json
adjusted_editing_script.json
final_duration_check.json
```

这样可以回溯、对比、回滚。

## 4.4 第一版只裁尾部，不动开头，不删除 shot

第一版为了安全：

```text
source_start 不变
只缩短 source_end
不删除 shot
不做中间裁剪
不做重新选片
```

原因：新闻素材的开头常常承担上下文承接，动 source_start 风险更高。


## 4.5 必须与现有 compact_to_tts 互斥

当前代码里 `step_cut_plan()` 已经有一层类似自动压缩逻辑：

```text
ai_voiceover_compact_to_tts=true
并且 tts_success=true
并且 timeline_mode=compact_segmented
并且 tts_segments 存在
    -> _compact_clips_to_voiceover_segments()
```

所以本方案新增 `adjusted_editing_script.json` 后，必须明确互斥规则：

```text
如果 cut_plan 使用 adjusted_editing_script：
    必须跳过 _compact_clips_to_voiceover_segments()
    必须跳过任何二次 compact_to_tts
否则可能发生重复压缩：
    原始 136s -> adjusted 100s -> compact_to_tts 再压到 75s
```

实现上不要只依赖 `prevent_double_compact` 这个 meta 字段，建议在 cut_plan 内部显式记录：

```python
effective_editing_source = "adjusted_editing_script"
skip_compact_to_tts = effective_editing_source == "adjusted_editing_script"
```

然后在原有 compact 条件里追加：

```python
and not skip_compact_to_tts
```

## 4.6 第一版不是放任所有低 coverage_ratio 都 shrink

当前案例 `coverage_ratio≈0.675` 可以进入 shrink_video，但不能把所有极低比例都自动 shrink。

例如：

```text
画面 120s，TTS 20s，coverage_ratio=0.16
```

这更可能是文案缺失、TTS segment 丢失、narration_segments 映射错误，而不只是画面规划太长。

因此第一版需要增加最低可行性保护：

```text
coverage_ratio >= 0.90：continue
0.60 <= coverage_ratio < 0.90：优先 shrink_video
coverage_ratio < 0.60：必须额外满足完整性条件，否则 action_required
```

coverage_ratio < 0.60 时允许 shrink_video 的条件：

```text
1. 每个 required shot 都有成功 TTS segment。
2. narration_segments 覆盖率 >= 95%。
3. tts_total_duration_seconds >= min_viable_voiceover_seconds，例如 45s，或 >= 原目标的 40%。
4. 预估 adjusted 后 video_tts_ratio <= 1.30。
5. voiceover_script.narration_text 非空，且不是极短文本。
```

不满足这些条件时，不要自动缩画面，应该输出 `action_required`，并提示“文案或 TTS segment 不完整”。

---

# 五、整体流程

改造后的主流程：

```text
1. editing_script
   仍然生成原始 AI 配音剪辑规划。

2. voiceover_script
   仍然生成 AI 配音文案。

3. tts
   生成 TTS 音频和 segment duration。

4. tts_duration_reconcile
   对比原始画面规划时长和 TTS 真实时长。

5. auto_duration_decision
   如果 TTS 成功但 duration mismatch，则决定 continue / shrink_video / action_required。

6. adjusted_editing_script
   如果 decision=shrink_video，则基于 TTS segment duration 生成调整后的画面规划。

7. cut_plan
   优先读取 adjusted_editing_script，而不是旧 editing_script。

8. subtitles
   基于 TTS segments 和最终 cut_plan 重新生成字幕时间轴。

9. final_duration_check
   render 前检查 TTS、cut_plan、subtitle、预计视频时长是否收敛。

10. render
   final_duration_check 通过后继续渲染。
```

---

# 六、关键产物设计

## 6.1 tts_duration_reconcile.json

保留现有产物，但它的语义要改：

```text
旧语义：失败证据。
新语义：TTS 与视频时长差异诊断，供 auto_duration_decision 使用。
```

建议字段：

```json
{
  "short_video_id": "v_001",
  "ok": false,
  "target_total_duration_seconds": 136.557,
  "tts_total_duration_seconds": 92.16,
  "coverage_ratio": 0.675,
  "total_gap_seconds": 44.397,
  "total_check": {
    "status": "too_short",
    "hard": true,
    "hard_type": "adjustable_duration_mismatch"
  },
  "segments": [
    {
      "shot_id": "v_001_s01",
      "target_duration_seconds": 28.301,
      "tts_actual_duration_seconds": 17.84,
      "coverage_ratio": 0.63,
      "gap_seconds": 10.461,
      "status": "too_short",
      "hard": true,
      "hard_type": "adjustable_duration_mismatch",
      "fatal": false
    }
  ]
}
```

重点：

```text
hard=true 不等于 fatal=true。
```

`too_short` 可以是 hard duration mismatch，但只要 TTS 音频存在，就不是 fatal TTS failure。

## 6.2 auto_duration_decision.json

职责：记录系统如何处理 TTS 与画面不匹配。

建议路径：

```text
duration/auto_duration_decision.json
```

或者放在 TTS 当前版本目录下也可以，但 cut_plan 要能稳定读取。

建议字段：

```json
{
  "schema_version": "auto_duration_decision_v1",
  "short_video_id": "v_001",
  "decision": "shrink_video",
  "reason": "TTS is shorter than planned video. AI voiceover mode uses TTS as the primary timeline.",
  "target_video_seconds_before": 136.557,
  "tts_seconds": 92.16,
  "coverage_ratio": 0.675,
  "target_video_seconds_after_estimated": 101.5,
  "rebuild_cut_plan": true,
  "rerun_tts": false,
  "repair_voiceover": false,
  "use_adjusted_editing_script": true,
  "created_from": {
    "tts_duration_reconcile": ".../tts_duration_reconcile.json",
    "editing_script": ".../editing_script.json",
    "tts_outputs": ".../tts_outputs.json"
  },
  "warnings": []
}
```

第一版只支持三种 decision：

```text
continue
shrink_video
action_required
```

暂时不支持：

```text
repair_voiceover
hybrid_repair_and_shrink
replan_video
long_form_confirm
```

## 6.3 adjusted_editing_script.json

职责：保存根据 TTS 真实时长调整后的 editing_script。

要求：

```text
1. 不覆盖原 editing_script。
2. 保留原有结构。
3. 保留所有 source / shot / story 字段。
4. 只调整画面时长相关字段。
5. 标记 adjusted_from、adjust_reason、duration_adjusted=true。
```

建议 meta：

```json
{
  "schema_version": "adjusted_editing_script_v1",
  "adjusted_from": "editing_script.json",
  "adjust_reason": "tts_duration_mismatch_shrink_video",
  "duration_adjusted": true,
  "prevent_double_compact": true,
  "auto_duration_decision_path": ".../auto_duration_decision.json",
  "tts_duration_reconcile_path": ".../tts_duration_reconcile.json"
}
```

每个 shot 建议补充：

```json
{
  "shot_id": "v_001_s01",
  "source_start_seconds": 120.0,
  "source_end_seconds": 138.84,
  "duration_seconds": 18.84,
  "target_duration_seconds": 18.84,
  "original_duration_seconds": 28.301,
  "original_target_duration_seconds": 28.301,
  "tts_actual_duration_seconds": 17.84,
  "tts_tail_padding_seconds": 1.0,
  "duration_adjusted": true,
  "adjust_action": "trim_tail_to_tts",
  "adjust_reason": "tts_segment_shorter_than_planned"
}
```

## 6.4 final_duration_check.json

职责：render 前最后门禁。

建议字段：

```json
{
  "schema_version": "final_duration_check_v1",
  "ok": true,
  "status": "pass",
  "tts_total_duration_seconds": 92.16,
  "cut_plan_total_duration_seconds": 101.2,
  "subtitle_total_duration_seconds": 92.16,
  "video_tts_ratio": 1.098,
  "checks": {
    "has_tts_audio": true,
    "has_valid_cut_plan": true,
    "has_valid_subtitles": true,
    "no_negative_duration": true,
    "source_bounds_ok": true,
    "video_tts_ratio_ok": true
  },
  "warnings": []
}
```

---

# 七、代码改造指南

## 7.1 修改 `newsclip_agent/pipeline.py` 的 `_step_tts_impl()`

### 当前问题

当前逻辑大概率是：

```text
_build_tts_duration_reconcile() 返回 ok=false
  -> 判断存在 hard mismatch
  -> overall_status = failed
  -> raise UserFacingPipelineError
```

这会把 TTS 成功但时长偏短的任务中断。

### 修改目标

拆分：

```text
TTS fatal failure
adjustable duration mismatch
```

### 建议逻辑

伪代码：

```python
def _step_tts_impl(self, ...):
    tts_outputs = generate_tts(...)

    fatal_errors = self._collect_tts_fatal_errors(tts_outputs)
    if fatal_errors:
        write_tts_status(status="failed", errors=fatal_errors)
        raise UserFacingPipelineError("tts_failed", ...)

    reconcile = self._build_tts_duration_reconcile(...)
    write_json(tts_dir / "tts_duration_reconcile.json", reconcile)

    duration_mismatch = not reconcile.get("ok", True)

    if duration_mismatch:
        decision = self._decide_post_tts_duration_action(
            editing_script=editing_script,
            voiceover_script=voiceover_script,
            tts_outputs=tts_outputs,
            reconcile=reconcile,
        )
        write_json(duration_dir / "auto_duration_decision.json", decision)

        if decision["decision"] == "shrink_video":
            adjusted = self._build_adjusted_editing_script_from_tts(
                editing_script=editing_script,
                tts_outputs=tts_outputs,
                reconcile=reconcile,
                decision=decision,
            )
            write_json(duration_dir / "adjusted_editing_script.json", adjusted)
            # 注意：TTS step 仍然是 success，不是 failed。
            return tts_success_with_duration_adjustment(...)

        if decision["decision"] == "continue":
            return tts_success_with_warning(...)

        if decision["decision"] == "action_required":
            return tts_action_required(...)

    return tts_success(...)
```

### fatal failure 判断

这些仍然必须 failed：

```text
1. TTS 输出为空。
2. required short_video 没有音频。
3. 音频路径不存在。
4. 音频 duration <= 0。
5. required narration_segment 没有对应 TTS segment。
6. TTS provider 抛异常。
7. voiceover_script 为空或 narration_text 为空。
```

### duration mismatch 判断

这些不应该 failed：

```text
1. total coverage_ratio 偏低。
2. segment coverage_ratio 偏低。
3. TTS 比画面短。
4. TTS 比画面略长。
5. reconcile.total_check.status = too_short / too_long。
```

只要音频真实存在，就是可调整问题。

---

## 7.2 新增 `_collect_tts_fatal_errors()`

职责：从 TTS 输出里收集真正无法继续的问题。

伪代码：

```python
def _collect_tts_fatal_errors(self, tts_outputs, required_segment_ids=None):
    errors = []

    if not tts_outputs:
        errors.append({"type": "empty_tts_outputs"})
        return errors

    for item in tts_outputs.get("items", []):
        audio_path = item.get("audio_path")
        duration = safe_float(item.get("duration_seconds"))

        if not audio_path:
            errors.append({"type": "missing_audio_path", "id": item.get("id")})
            continue

        if not Path(audio_path).exists():
            errors.append({"type": "audio_file_not_exists", "audio_path": audio_path})
            continue

        if duration <= 0:
            errors.append({"type": "invalid_audio_duration", "audio_path": audio_path, "duration": duration})

    # 如果 required_segment_ids 存在，要检查每个 required segment 是否都有 TTS。
    missing = required_segment_ids - set(actual_segment_ids)
    for segment_id in missing:
        errors.append({"type": "missing_required_tts_segment", "segment_id": segment_id})

    return errors
```

注意：optional segment 缺失可以先 warning，但 required / must segment 缺失不能继续。

---

## 7.3 修改 `_build_tts_duration_reconcile()`

### 保留现有逻辑

不建议推倒重写，继续复用现有字段：

```text
target_total_duration_seconds
tts_total_duration_seconds
coverage_ratio
total_check
segments
ok
hard
```

### 增强字段

给每个 total_check / segment 增加：

```text
fatal: false
hard_type: adjustable_duration_mismatch
can_adjust_by_shrink: true / false
can_continue_to_auto_decision: true / false
```

示例：

```json
{
  "status": "too_short",
  "hard": true,
  "fatal": false,
  "hard_type": "adjustable_duration_mismatch",
  "can_continue_to_auto_decision": true
}
```

### 判断原则

```text
missing_tts_audio / duration<=0 / missing_required_segment -> fatal=true
too_short / too_long -> fatal=false
```

---

## 7.4 新增 `_decide_post_tts_duration_action()`

### 职责

根据 reconcile 结果决定下一步：

```text
continue
shrink_video
action_required
```

### 第一版决策规则

```text
coverage_ratio >= 0.90:
    continue

0.60 <= coverage_ratio < 0.90:
    如果 TTS 音频存在、required segment 不缺、editing_script 可调整：
        shrink_video
    否则：
        action_required

coverage_ratio < 0.60:
    如果满足极低比例保护条件：
        shrink_video
    否则：
        action_required
```

你的案例：

```text
coverage_ratio = 0.675
TTS 音频存在
editing_script 可裁剪
=> shrink_video
```

### 伪代码

```python
def _decide_post_tts_duration_action(self, editing_script, voiceover_script, tts_outputs, reconcile):
    ratio = safe_float(reconcile.get("coverage_ratio"), default=1.0)
    target_total = safe_float(reconcile.get("target_total_duration_seconds"), 0)
    tts_total = safe_float(reconcile.get("tts_total_duration_seconds"), 0)

    fatal_errors = self._collect_tts_fatal_errors(tts_outputs)
    if fatal_errors:
        return {
            "schema_version": "auto_duration_decision_v1",
            "decision": "action_required",
            "reason": "TTS has fatal errors, cannot adjust video duration.",
            "fatal_errors": fatal_errors,
            "rebuild_cut_plan": False,
            "rerun_tts": False,
            "repair_voiceover": False,
        }

    if ratio >= self._voiceover_setting("post_tts_ok_min_ratio", 0.90):
        return {
            "decision": "continue",
            "reason": "TTS duration is close enough to planned video duration.",
            "rebuild_cut_plan": False,
            "rerun_tts": False,
            "repair_voiceover": False,
        }

    if tts_total > 0 and target_total > 0 and self._can_adjust_editing_script(editing_script):
        return {
            "schema_version": "auto_duration_decision_v1",
            "decision": "shrink_video",
            "reason": "TTS is shorter than planned video; shrink video in AI voiceover mode.",
            "target_video_seconds_before": target_total,
            "tts_seconds": tts_total,
            "coverage_ratio": ratio,
            "rebuild_cut_plan": True,
            "rerun_tts": False,
            "repair_voiceover": False,
            "use_adjusted_editing_script": True,
        }

    return {
        "decision": "action_required",
        "reason": "Duration mismatch exists but editing script cannot be safely adjusted.",
        "rebuild_cut_plan": False,
        "rerun_tts": False,
        "repair_voiceover": False,
    }
```

### 配置默认值

```toml
[voiceover]
post_tts_auto_reconcile_enabled = true
post_tts_ok_min_ratio = 0.90
post_tts_shrink_min_ratio = 0.70
post_tts_default_decision = "shrink_video"
tts_visual_tail_padding_seconds = 1.0
tts_driven_cut_plan = true
```

---

## 7.5 新增 `_build_adjusted_editing_script_from_tts()`

这是第一版最关键的函数。

### 输入

```text
editing_script
voiceover_script
tts_outputs
tts_duration_reconcile
auto_duration_decision
```

### 输出

```text
adjusted_editing_script.json
```

### 核心规则

```text
1. 不覆盖原 editing_script。
2. 不改变 shot_id。
3. 不改变 source_start。
4. 不删除 shot。
5. 只缩短 source_end。
6. 新时长基于 tts_actual_duration_seconds + tail_padding。
7. 新时长必须 clamp 到合法范围。
8. 多源字段必须完整保留。
9. 所有相关时长字段必须同步更新。
```

### 新时长计算

```text
raw_new_duration = tts_actual_duration_seconds + tail_padding_seconds
new_duration = clamp(raw_new_duration, min_duration_seconds, max_duration_seconds)
new_duration = min(new_duration, original_source_duration_seconds)
```

默认：

```text
tail_padding_seconds = 1.0
```

### min / max 取值策略

优先读取已有字段：

```text
min_duration_seconds
max_duration_seconds
```

如果没有：

```text
min_duration_seconds = max(3.0, original_duration_seconds * 0.60)
max_duration_seconds = original_duration_seconds
```

注意：第一版 max 不建议超过原始 duration。

### 必须同步的字段

如果存在这些字段，都要同步更新：

```text
source_end
source_end_seconds
local_end
local_end_seconds
end
end_seconds
duration_seconds
target_duration_seconds
target_end_seconds
clip_duration_seconds
```

其中最重要的是：

```text
source_end_seconds = source_start_seconds + new_duration
local_end_seconds = local_start_seconds + new_duration
duration_seconds = new_duration
target_duration_seconds = new_duration
```

如果存在字符串时间码字段：

```text
source_end = seconds_to_timecode(source_end_seconds)
local_end = seconds_to_timecode(local_end_seconds)
```

### 保留原值

每个 shot 增加：

```text
original_source_end_seconds
original_duration_seconds
original_target_duration_seconds
tts_actual_duration_seconds
tts_tail_padding_seconds
duration_adjusted
adjust_action
adjust_reason
```

### 伪代码

```python
def _build_adjusted_editing_script_from_tts(self, editing_script, tts_outputs, reconcile, decision):
    adjusted = copy.deepcopy(editing_script)
    tts_by_shot_id = self._index_tts_segments_by_shot_id(tts_outputs)
    reconcile_by_shot_id = self._index_reconcile_segments_by_shot_id(reconcile)

    tail_padding = self._voiceover_setting("tts_visual_tail_padding_seconds", 1.0)
    warnings = []

    for video in iter_short_videos(adjusted):
        for shot in iter_editing_shots(video):
            shot_id = shot.get("shot_id")
            original_duration = get_shot_duration(shot)
            source_start = safe_float(shot.get("source_start_seconds"))
            local_start = safe_float(shot.get("local_start_seconds"), default=None)

            tts_seg = tts_by_shot_id.get(shot_id)
            if not tts_seg:
                warnings.append({
                    "type": "missing_tts_segment_for_shot",
                    "shot_id": shot_id,
                    "action": "keep_original_duration"
                })
                continue

            tts_duration = safe_float(tts_seg.get("duration_seconds"))
            if tts_duration <= 0:
                warnings.append({
                    "type": "invalid_tts_duration_for_shot",
                    "shot_id": shot_id,
                    "action": "keep_original_duration"
                })
                continue

            min_d = safe_float(shot.get("min_duration_seconds"), default=max(3.0, original_duration * 0.60))
            max_d = safe_float(shot.get("max_duration_seconds"), default=original_duration)
            max_d = min(max_d, original_duration)

            raw_new = tts_duration + tail_padding
            new_duration = clamp(raw_new, min_d, max_d)

            # 防止非法值
            if new_duration <= 0 or new_duration > original_duration:
                warnings.append({
                    "type": "adjusted_duration_invalid",
                    "shot_id": shot_id,
                    "raw_new_duration": raw_new,
                    "action": "keep_original_duration"
                })
                continue

            # 保存原值
            shot["original_duration_seconds"] = original_duration
            shot["original_target_duration_seconds"] = shot.get("target_duration_seconds")
            shot["original_source_end_seconds"] = shot.get("source_end_seconds")

            # 同步更新
            shot["duration_seconds"] = round(new_duration, 3)
            shot["target_duration_seconds"] = round(new_duration, 3)

            if source_start is not None:
                source_end = source_start + new_duration
                shot["source_end_seconds"] = round(source_end, 3)
                shot["source_end"] = seconds_to_timecode(source_end)

            if local_start is not None:
                local_end = local_start + new_duration
                shot["local_end_seconds"] = round(local_end, 3)
                shot["local_end"] = seconds_to_timecode(local_end)

            if "target_end_seconds" in shot:
                target_start = safe_float(shot.get("target_start_seconds"), default=0)
                shot["target_end_seconds"] = round(target_start + new_duration, 3)

            shot["tts_actual_duration_seconds"] = round(tts_duration, 3)
            shot["tts_tail_padding_seconds"] = tail_padding
            shot["duration_adjusted"] = True
            shot["adjust_action"] = "trim_tail_to_tts"
            shot["adjust_reason"] = "tts_segment_shorter_than_planned"

    adjusted.setdefault("meta", {})["schema_version"] = "adjusted_editing_script_v1"
    adjusted["meta"]["duration_adjusted"] = True
    adjusted["meta"]["adjust_reason"] = "tts_duration_mismatch_shrink_video"
    adjusted["meta"]["prevent_double_compact"] = True
    adjusted["meta"]["auto_duration_decision_path"] = decision.get("path")
    adjusted["meta"]["warnings"] = warnings

    return adjusted
```



### 总量二次校正：避免每个 shot 都保留过长 padding

第一版只裁尾部是安全的，但如果 shot 数量较多，`tts_actual_duration + 1.0s padding` 可能导致总时长仍然明显大于 TTS。

因此 `_build_adjusted_editing_script_from_tts()` 不应只做单 shot 级别 clamp，还应该做一次总量校正：

```text
1. 第一轮：每个 shot 使用 tts_actual_duration + tail_padding_seconds。
2. 统计 adjusted_total_duration / tts_total_duration。
3. 如果 ratio <= final_video_tts_max_ratio，例如 1.20，则通过。
4. 如果 ratio > 1.20：
   - 先把非 must/opening shot 的 tail_padding 从 1.0 降到 0.5。
   - 仍不够时，允许非 must/opening shot 的 min_duration 从 original*0.60 降到 original*0.45。
   - opening_hook / must_keep / evidence shot 保持更保守下限。
5. 如果 ratio 仍 > 1.30，输出 action_required，不要盲目 render。
```

建议新增内部辅助函数：

```python
def _rebalance_adjusted_editing_total_duration(
    self,
    adjusted: dict,
    *,
    tts_total_seconds: float,
    max_ratio: float,
    warning_ratio: float,
) -> tuple[dict, list[dict]]:
    ...
```

第一版不要求复杂最优化，只要做到：

```text
优先减少非关键 shot 的 padding；
再降低非关键 shot 的 min clamp；
不删除 shot；
不移动 source_start；
不突破 source bounds。
```

### shot 重要性分级建议

用于总量二次校正时，建议按以下优先级保护：

```text
强保护，不轻易压缩：
- purpose=opening_hook
- importance=must_keep
- original_audio_required=true
- audio_mode=mixed_evidence / original_sound
- must_say_facts 非空

可压缩：
- purpose=evidence_visual
- 普通 visual support shot
- 只有过渡作用的 B-roll shot
```

如果字段缺失，则默认按普通 shot 处理，但不要直接删除。
---

## 7.6 修改 `step_cut_plan()`

### 当前问题

如果 cut_plan 仍读取原始 editing_script，就算生成了 adjusted_editing_script，也不会生效。

### 修改目标

新增：

```python
def _load_effective_editing_for_cut_plan(self):
    ...
```

逻辑：

```text
如果 tts_driven_cut_plan=true
并且 auto_duration_decision.rebuild_cut_plan=true
并且 adjusted_editing_script.json 存在：
    使用 adjusted_editing_script
否则：
    使用原 editing_script
```

### 伪代码

```python
def _load_effective_editing_for_cut_plan(self):
    original = self._load_step_json("editing_script")

    if not self._voiceover_setting("tts_driven_cut_plan", True):
        return original, {
            "editing_source": "original_editing_script",
            "reason": "tts_driven_cut_plan_disabled"
        }

    decision = self._load_optional_json("duration/auto_duration_decision.json")
    adjusted = self._load_optional_json("duration/adjusted_editing_script.json")

    if decision and adjusted and decision.get("rebuild_cut_plan"):
        return adjusted, {
            "editing_source": "adjusted_editing_script",
            "auto_duration_decision": decision,
            "adjusted_editing_script_path": "duration/adjusted_editing_script.json"
        }

    return original, {
        "editing_source": "original_editing_script",
        "reason": "no_adjusted_editing_script"
    }
```

### cut_plan meta 必须记录

```json
{
  "editing_source": "adjusted_editing_script",
  "auto_duration_decision_path": "duration/auto_duration_decision.json",
  "adjusted_editing_script_path": "duration/adjusted_editing_script.json",
  "prevent_double_compact": true
}
```


### 与现有 compact_to_tts 的互斥实现

当前 cut_plan 内部已有：

```python
if (
    self.duration_settings.ai_voiceover_compact_to_tts
    and tts_success
    and tts_item.get("timeline_mode") == "compact_segmented"
    and tts_segments
):
    clips = self._compact_clips_to_voiceover_segments(...)
```

改造后应变成：

```python
skip_compact_to_tts = effective_meta.get("editing_source") == "adjusted_editing_script"

if (
    self.duration_settings.ai_voiceover_compact_to_tts
    and not skip_compact_to_tts
    and tts_success
    and tts_item.get("timeline_mode") == "compact_segmented"
    and tts_segments
):
    clips = self._compact_clips_to_voiceover_segments(...)
```

同时在 warnings / repair_reasons 中记录：

```text
skip_compact_to_tts_because_adjusted_editing_script_used
```

这样后续排查时能明确知道：

```text
本次压缩发生在 adjusted_editing_script 阶段，cut_plan 没有再二次压缩。
```

---

## 7.7 修改 cut_plan input_hash

这是很容易漏的坑。

如果 cut_plan 有缓存 / 版本复用 / input hash，必须加入：

```text
editing_source
adjusted_editing_script_hash
auto_duration_decision_hash
tts_duration_reconcile_hash
tts_outputs_hash
```

否则会出现：

```text
adjusted_editing_script 已经生成
但 cut_plan 复用旧缓存
最终仍按 136 秒渲染
```

建议：

```python
cut_plan_inputs = {
    "effective_editing": effective_editing,
    "editing_source": meta["editing_source"],
    "adjusted_editing_script_hash": file_hash(adjusted_path),
    "auto_duration_decision_hash": file_hash(decision_path),
    "tts_duration_reconcile_hash": file_hash(reconcile_path),
    "tts_outputs_hash": file_hash(tts_outputs_path),
}
```

---

## 7.8 字幕时间轴同步

### 现状说明

当前 `step_subtitles()` 已经有 TTS segments 优先路径：

```text
如果 subtitle_follow_actual_tts=true 且 tts_segments 存在：
    使用 _tts_segments_to_srt(tts_segments)
否则如果 voiceover narration_segments 存在：
    使用 _segments_to_srt(narration_segments)
否则：
    使用整段文本按总时长切字幕
```

所以本次不要重写一套字幕系统。重点是保证：

```text
1. TTS step 不再因 duration mismatch failed。
2. subtitles 能拿到 tts_outputs 里的真实 segment timing。
3. subtitle_follow_actual_tts 默认开启。
4. subtitles input_hash 加入 adjusted_editing_script_hash，避免画面调整后复用旧字幕。
5. final_duration_check 检查字幕结束时间是否接近 TTS 总时长。
```

### 问题

如果字幕仍按原始 editing_script / 原始 shot target 生成，会出现：

```text
画面已经压到 100 秒
TTS 是 92 秒
字幕还按 136 秒分布
```

### 修改原则

AI 配音模式下，字幕应以 TTS segment 为准：

```text
subtitle_start = tts_segment.start_seconds
subtitle_end = tts_segment.end_seconds
```

如果字幕需要映射到画面 shot，只用 shot_id 关联，不用原始 target_duration 重新分配。

### 要检查

```text
1. subtitles 是否读取了旧 editing_script 的 duration。
2. subtitles 是否读取了 TTS segment timings。
3. adjusted 后是否重新生成 subtitles，而不是复用旧字幕文件。
4. subtitle input_hash 是否加入 tts_outputs_hash / adjusted_editing_script_hash。
```

### 字幕生成建议

```text
字幕文本：来自 voiceover_script.narration_segments.text
字幕时间：来自 tts_outputs.segment.start/end/duration
字幕 shot_id：保持与 narration_segments.shot_id 一致
```

---

## 7.9 新增 `final_duration_check()`

### 第一版落地方式

第一版不强制把 `final_duration_check` 注册成新的 workflow step。

推荐先作为 `step_cut_plan()` 末尾或 render 前的门禁产物：

```text
cut_plan 生成 output_videos
  -> 生成 final_duration_check.json
  -> 如果 pass / warning，允许 render
  -> 如果 action_required / failed，阻断 render
```

这样可以少改 `workflow_registry.py`，也能避免新增 step 之后依赖关系漏配。

如果后续要正式注册成 workflow step，再单独修改：

```text
workflow_registry.py:
render 依赖 final_duration_check
final_duration_check 依赖 cut_plan + subtitles + tts
```

### 触发位置

```text
render 前
```

或者：

```text
cut_plan 和 subtitles 生成后，render 之前
```

### 检查项

```text
1. TTS 总时长是否 > 0。
2. cut_plan 总时长是否 > 0。
3. subtitle 总时长是否 > 0。
4. cut_plan_total / tts_total 是否在合理范围。
5. 每个 clip duration 是否 > 0。
6. 每个 source_end 是否 >= source_start。
7. 每个 source_end 是否不超过原始素材边界。
8. 字幕是否为空。
9. 字幕结束时间是否明显超过 TTS 总时长。
10. 如果使用 adjusted_editing_script，cut_plan 是否真的使用 adjusted。
```

### 推荐阈值

```text
video_tts_ratio <= 1.20：通过
1.20 < video_tts_ratio <= 1.30：warning，可继续
video_tts_ratio > 1.30：action_required 或 failed

video_tts_ratio < 0.95：warning，说明画面可能短于 TTS
video_tts_ratio < 0.90：action_required
```

对于当前案例：

```text
TTS 92 秒
adjusted video 约 100 秒
ratio ≈ 1.08
=> 通过
```

### 输出

```text
final_duration_check.json
```

如果不通过，不要盲目 render。

---

# 八、边界条件与鲁棒性要求

## 8.1 TTS 音频缺失

处理：

```text
failed
```

原因：没有真实音频，无法以 TTS 为主时间轴。

## 8.2 required segment 缺失

处理：

```text
failed 或 action_required
```

不要自动缩画面掩盖 shot_id 映射错误。

## 8.3 optional segment 缺失

第一版处理：

```text
保留原 shot 时长
记录 warning
不整条失败
```

但如果缺失比例过高：

```text
missing_segments / total_segments > 20%
=> action_required
```

## 8.4 TTS segment duration <= 0

处理：

```text
如果是 required：failed
如果是 optional：保留原时长 + warning
```

## 8.5 adjusted 后时长非法

非法情况：

```text
new_duration <= 0
new_duration > original_duration
source_end <= source_start
source_end 超出原素材边界
```

处理：

```text
该 shot 保留原时长
记录 warning
如果 must shot 大量失败，则 action_required
```

## 8.6 不允许二次压缩

如果 cut_plan 后面已有 compact_to_tts / fit_to_duration 逻辑，必须避免重复压缩。

建议在 adjusted meta 写：

```json
{
  "prevent_double_compact": true
}
```

cut_plan 看到这个字段后：

```text
不要再次执行 compact_to_tts
不要再次按 TTS 压缩一遍
```

否则会出现：

```text
136 -> 100
100 -> 75
```

视频被压得过短。

## 8.7 多源字段不能丢

调整 shot 时必须保留：

```text
source_id
source_index
source_clip_id
source_start
source_end
source_start_seconds
source_end_seconds
local_start
local_end
local_start_seconds
local_end_seconds
```

不要只保留 start/end，否则后续渲染会找不到素材。

## 8.8 shot_id 必须稳定

`shot_id` 是 voiceover_script、tts_outputs、editing_script、cut_plan、subtitles 的关键连接键。

禁止：

```text
重新生成 shot_id
改变 shot_id
丢掉 shot_id
把 narration segment id 当成 shot_id
```

如果现有 `build_voiceover_timing_contract()` 已经有 shot_id 映射能力，应该复用并增强，不要重复实现一套映射。

## 8.9 TTS 偏长的预留处理

第一版重点解决 TTS 偏短，但代码结构要预留 too_long。

如果出现：

```text
TTS 75 秒
画面 60 秒
```

第一版可以先：

```text
action_required
```

或者轻微偏长时：

```text
如果 source 还有余量，可以延长到 max_duration_seconds。
```

但不要在第一版做复杂补画面。至少不要把 too_long 和 too_short 写死成同一种 shrink_video。

---

# 九、配置建议

在 `config.toml` 的 `[voiceover]` 或相近配置区加入：

```toml
[voiceover]
# 第一版：TTS 后自动时长收敛
tts_driven_cut_plan = true
post_tts_auto_reconcile_enabled = true
post_tts_ok_min_ratio = 0.90
post_tts_shrink_min_ratio = 0.70
post_tts_action_required_min_ratio = 0.0

# 第一版默认只缩画面，不补文案，不重跑 TTS
post_tts_repair_voiceover_enabled = false
post_tts_rerun_tts_enabled = false
post_tts_hybrid_enabled = false

# 画面尾部 padding
tts_visual_tail_padding_seconds = 1.0

# 字符预算暂时固定，非核心逻辑
voiceover_chars_per_second = 4.8

# 安全阈值
final_video_tts_max_ratio = 1.20
final_video_tts_warning_ratio = 1.30
final_video_tts_min_ratio = 0.90

# 回滚开关
use_adjusted_editing_script_for_cut_plan = true
```

要求：

```text
1. 所有配置必须有代码默认值。
2. config.toml 不写这些字段也不能报错。
3. 关闭 post_tts_auto_reconcile_enabled 后，走旧逻辑或 action_required。
```

---

# 十、验收标准

## 10.1 当前问题案例

输入：

```text
规划画面：约 136 秒
TTS 实际：约 92 秒
coverage_ratio：约 0.675
```

期望：

```text
1. tts step 不应 failed。
2. tts_duration_reconcile.json 正常生成。
3. auto_duration_decision.json 正常生成。
4. auto_duration_decision.decision = shrink_video。
5. adjusted_editing_script.json 正常生成。
6. adjusted_editing_script 总时长从 136 秒压缩到约 95-105 秒。
7. cut_plan 使用 adjusted_editing_script，而不是原始 editing_script。
8. cut_plan clips 总时长接近 adjusted_editing_script。
9. subtitles 跟随 TTS segments，不按旧 136 秒分布。
10. final_duration_check.json 通过或只有 warning。
11. render 可以继续。
12. 原始 editing_script.json / voiceover_script.json / tts_outputs.json 不被覆盖。
```

## 10.2 TTS fatal failure 案例

输入：

```text
TTS 没生成音频
或音频文件不存在
或 duration <= 0
```

期望：

```text
tts step failed
不生成 adjusted_editing_script
不进入 cut_plan
错误信息明确说明是 TTS 生成失败，不是 duration mismatch
```

## 10.3 segment 缺失案例

输入：

```text
must shot 缺少 TTS segment
```

期望：

```text
failed 或 action_required
不要自动 shrink_video 掩盖问题
```

输入：

```text
optional shot 缺少 TTS segment
```

期望：

```text
保留该 shot 原时长
记录 warning
继续处理其他 shot
```

## 10.4 cut_plan 缓存案例

操作：

```text
先跑一次旧 cut_plan
再生成 adjusted_editing_script
再跑 cut_plan
```

期望：

```text
cut_plan 不复用旧缓存
input_hash 因 adjusted_editing_script / auto_duration_decision / tts_duration_reconcile 变化而变化
```

## 10.5 字幕案例

期望：

```text
字幕结束时间接近 TTS 总时长
字幕不按原 136 秒平均拉伸
字幕 shot_id 与 TTS segment / voiceover segment 对齐
```

---

# 十一、回滚方案

如果新逻辑出现问题，可以通过配置关闭：

```toml
[voiceover]
post_tts_auto_reconcile_enabled = false
tts_driven_cut_plan = false
use_adjusted_editing_script_for_cut_plan = false
```

关闭后：

```text
cut_plan 继续使用原 editing_script
不读取 adjusted_editing_script
不执行 auto_duration_decision
```

因为不覆盖原始产物，所以回滚只需要关闭配置或删除新产物。

---

# 十二、现有代码落点与开发顺序

## 12.1 主要修改文件

第一版建议优先只改这些文件：

```text
newsclip_agent/pipeline.py
newsclip_agent/duration_policy.py       # 如需补充配置读取或阈值结构
config.toml                             # 只加默认配置示例，不依赖用户一定填写
newsclip_agent/workflow_registry.py     # 第一版可不改；只有 final_duration_check 独立成 step 时才改
```

不建议第一版改：

```text
prompts.py
llm agent 输出结构
tts_omnivoice.py 的核心生成逻辑
render 的 ffmpeg 合成主逻辑
```

因为当前问题不是 LLM 没写够，也不是 OmniVoice 不能生成，而是 TTS 成功后的时长收敛和失败分类不合理。

## 12.2 P0 开发任务

### P0-1：改 `_step_tts_impl()` 的失败分类

当前逻辑里，以下情况会导致 TTS step failed：

```text
not reconcile_ok
reconcile_hard_videos
reconcile_hard_segments
```

改造后应拆成两类：

```text
fatal_errors：
- outputs 为空
- 没有任何 success 音频
- required segment 缺失
- audio file 不存在
- actual_duration_seconds <= 0
- TTS provider 返回 failed

adjustable_duration_mismatch：
- reconcile too_short
- reconcile too_long
- coverage_ratio 低
- segment duration 与 target 不匹配
```

伪代码：

```python
fatal_errors = self._collect_tts_fatal_errors(
    outputs={"outputs": outputs},
    required_segment_keys=expected_tts_segments,
)

if fatal_errors:
    overall_status = "failed"
    hard_error = "TTS fatal failure"
else:
    reconcile = self._build_tts_duration_reconcile(editing, voiceover, {"outputs": outputs})
    write_json(base_dir / "tts_duration_reconcile.json", reconcile)

    if not reconcile.get("ok", True):
        decision = self._decide_post_tts_duration_action(
            editing_script=editing,
            voiceover_script=voiceover,
            tts_outputs={"outputs": outputs},
            reconcile=reconcile,
        )
        write_json(base_dir / "auto_duration_decision.json", decision)

        if decision["decision"] == "shrink_video":
            adjusted = self._build_adjusted_editing_script_from_tts(...)
            write_json(base_dir / "adjusted_editing_script.json", adjusted)
            overall_status = "success"
        elif decision["decision"] == "continue":
            overall_status = "success"
        else:
            overall_status = "action_required"
    else:
        overall_status = "success"
```

注意：

```text
TTS step status 可以是 success，但 extra.summary 里要记录 duration_adjusted=true。
如果 decision=action_required，可以把 manifest 设成 action_required，而不是误报 TTS failed。
```

### P0-2：新增 `_collect_tts_fatal_errors()`

建议签名：

```python
def _collect_tts_fatal_errors(
    self,
    tts_outputs: dict[str, Any],
    required_segment_keys: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    ...
```

必须检查：

```text
1. outputs 是否为空。
2. 每个 required short_video 是否有 success output。
3. 每个 required narration_segment 是否有 success segment。
4. 音频 file 字段是否存在。
5. self.task_dir / file 是否真实存在。
6. actual_duration_seconds 是否 > 0。
7. segment actual_duration_seconds 是否 > 0。
```

不要把这些当 fatal：

```text
too_short
too_long
coverage_ratio_low
segment_gap_seconds_large
hard=true 但 hard_type=adjustable_duration_mismatch
```

### P0-3：新增 `_decide_post_tts_duration_action()`

第一版只允许三种 decision：

```text
continue
shrink_video
action_required
```

推荐规则：

```python
ratio = reconcile.get("coverage_ratio")

if ratio >= 0.90:
    return continue

if 0.60 <= ratio < 0.90:
    if tts_complete and editing_adjustable:
        return shrink_video
    return action_required

if ratio < 0.60:
    if extreme_low_ratio_guard_passed:
        return shrink_video
    return action_required
```

极低比例保护函数建议：

```python
def _can_shrink_when_coverage_extremely_low(...):
    return (
        required_segments_complete
        and narration_segment_coverage >= 0.95
        and tts_total_seconds >= min_viable_voiceover_seconds
        and estimated_adjusted_ratio <= action_required_ratio
    )
```

### P0-4：新增 `_build_adjusted_editing_script_from_tts()`

核心要求沿用原方案，但补充两点：

```text
1. 除了逐 shot 调整，还要做总量二次校正。
2. adjusted meta 里必须写 effective_editing_source，供 cut_plan 判断是否跳过 compact_to_tts。
```

建议 meta：

```json
{
  "schema_version": "adjusted_editing_script_v1",
  "duration_adjusted": true,
  "effective_editing_source": "adjusted_editing_script",
  "prevent_double_compact": true,
  "adjust_reason": "tts_duration_mismatch_shrink_video",
  "adjust_strategy": "trim_tail_to_tts_with_total_rebalance",
  "auto_duration_decision_path": "tts/omnivoice/v001/auto_duration_decision.json",
  "tts_duration_reconcile_path": "tts/omnivoice/v001/tts_duration_reconcile.json"
}
```

### P0-5：修改 `step_cut_plan()` 使用 effective editing

当前：

```python
editing = self._load_step_json("editing_script")
```

改成：

```python
editing, effective_meta = self._load_effective_editing_for_cut_plan()
```

`_load_effective_editing_for_cut_plan()` 应该：

```text
1. 默认读取原 editing_script。
2. 如果配置开启 use_adjusted_editing_script_for_cut_plan。
3. 如果 tts 当前版本目录下存在 auto_duration_decision.json。
4. 如果 decision.rebuild_cut_plan=true。
5. 如果 adjusted_editing_script.json 存在且 meta.duration_adjusted=true。
6. 返回 adjusted_editing_script。
```

如果 adjusted 文件存在但结构不合法，不要静默回退，应该 warning 或 action_required。否则开发时会误以为调整生效。

### P0-6：修改 cut_plan input_hash

当前 cut_plan hash 主要包括：

```text
editing_script hash
tts hash
subtitles hash
运行参数
```

改造后必须加入：

```text
effective_editing_source
effective_editing_hash
auto_duration_decision_hash
adjusted_editing_script_hash
tts_duration_reconcile_hash
skip_compact_to_tts
```

这样可以避免：

```text
adjusted_editing_script 已生成，但 cut_plan 复用旧缓存。
```

### P0-7：cut_plan 阶段跳过二次 compact

改造后：

```python
skip_compact_to_tts = effective_meta.get("editing_source") == "adjusted_editing_script"
```

原 compact 条件必须追加：

```python
and not skip_compact_to_tts
```

并在 cut_plan 输出里写：

```json
{
  "editing_source": "adjusted_editing_script",
  "skip_compact_to_tts": true,
  "skip_compact_reason": "adjusted_editing_script_already_tts_driven"
}
```

### P0-8：生成 final_duration_check.json

第一版建议在 `step_cut_plan()` 末尾生成，不必先注册 workflow step。

检查结果写到：

```text
edit/cut_plan/v*/final_duration_check.json
```

或：

```text
tts/omnivoice/v*/final_duration_check.json
```

但更推荐放在 cut_plan 版本目录，因为它检查的是最终可渲染 cut_plan。

## 12.3 P1 开发任务

```text
1. subtitles input_hash 加 adjusted_editing_script_hash。
2. subtitle.json 记录 timing_source=tts_segments。
3. final_duration_check 检查 subtitle_end 与 tts_total_duration 是否接近。
4. too_long 轻微场景复用现有 auto_extend 逻辑。
5. Web/manifest summary 展示 duration_adjusted、decision、video_tts_ratio。
```

## 12.4 P2 暂不做

```text
1. post-TTS 自动补文案。
2. 补文案后重跑 TTS。
3. LLM 多轮 duration decision。
4. 历史 TTS 语速校准。
5. Web 三模式选择。
6. 重新选片、删除 shot、中间裁剪。
```

---

# 十三、最终给开发工具的执行说明

可以直接把下面这段给 Codex / 代码开发工具使用：

```text
请基于现有代码做增量修改，不要重构整个 pipeline，也不要大改 prompt。

目标：解决 AI 配音模式下 TTS 已成功生成，但真实 TTS 时长短于原始画面规划时，tts step 被错误标记 failed，导致 cut_plan/render 无法继续的问题。

本次只实现第一版完整闭环：
tts_duration_reconcile -> auto_duration_decision -> adjusted_editing_script -> cut_plan -> subtitles -> final_duration_check。

具体要求：

1. 修改 newsclip_agent/pipeline.py 的 _step_tts_impl()。
   - 区分 TTS fatal failure 和 adjustable duration mismatch。
   - TTS 音频存在、duration>0、required segment 不缺时，即使 coverage_ratio 偏低，也不要把 tts step 标 failed。
   - duration mismatch 应写入 tts_duration_reconcile.json，并进入 _decide_post_tts_duration_action()。

2. 新增 _collect_tts_fatal_errors()。
   - 音频缺失、文件不存在、duration<=0、required segment 缺失才算 fatal。
   - too_short/too_long/slightly_short/slightly_long 不是 fatal。

3. 增强 _build_tts_duration_reconcile()。
   - 保留原字段。
   - 增加 fatal、hard_type、can_continue_to_auto_decision 等字段。
   - hard=true 不等于 fatal=true。

4. 新增 _decide_post_tts_duration_action()。
   - 第一版只支持 continue / shrink_video / action_required。
   - coverage_ratio>=0.90 => continue。
   - coverage_ratio<0.90 且 TTS 可用、editing_script 可调整 => shrink_video。
   - 不做 post-TTS 自动补文案，不重跑 TTS，不做 hybrid。

5. 新增 auto_duration_decision.json。
   - decision=shrink_video 时，rerun_tts=false，repair_voiceover=false，rebuild_cut_plan=true。

6. 新增 _build_adjusted_editing_script_from_tts()。
   - 不覆盖原 editing_script。
   - 不改变 shot_id。
   - 不改变 source_start。
   - 不删除 shot。
   - 只裁尾部，缩短 source_end。
   - new_duration = clamp(tts_actual_duration + 1.0s padding, min_duration, max_duration)。
   - 如果 min/max 缺失，用 max(3.0, original_duration*0.60) 和 original_duration。
   - 必须同步更新 source_end/source_end_seconds/local_end/local_end_seconds/duration_seconds/target_duration_seconds/target_end_seconds 等字段。
   - 保留 source_id/source_index/source_clip_id 等多源字段。
   - 增加 original_duration_seconds、tts_actual_duration_seconds、duration_adjusted、adjust_reason 等审计字段。

7. 修改 step_cut_plan()。
   - 新增 _load_effective_editing_for_cut_plan()。
   - 如果 auto_duration_decision.rebuild_cut_plan=true 且 adjusted_editing_script.json 存在，则 cut_plan 使用 adjusted_editing_script。
   - cut_plan meta 记录 editing_source=adjusted_editing_script。

8. 修改 cut_plan input_hash。
   - 必须加入 adjusted_editing_script_hash、auto_duration_decision_hash、tts_duration_reconcile_hash、tts_outputs_hash、editing_source。
   - 防止复用旧 cut_plan。

9. 字幕逻辑要确认跟随 TTS segments。
   - 字幕时间轴以 TTS segment start/end 为准。
   - 不要按原始 136 秒画面规划分布。
   - subtitle input_hash 加入 tts_outputs_hash 和 adjusted_editing_script_hash。

10. 新增 final_duration_check.json。
    - render 前检查 tts_total_duration、cut_plan_total_duration、subtitle_total_duration、video_tts_ratio、clip duration、source bounds。
    - video_tts_ratio <= 1.20 通过。
    - 1.20-1.30 warning。
    - >1.30 action_required 或 failed。
    - 如果 source_end 越界、负时长、字幕为空，应 failed 或 action_required。

11. 配置要求。
    - 新配置必须有默认值。
    - post_tts_auto_reconcile_enabled、tts_driven_cut_plan、use_adjusted_editing_script_for_cut_plan 可关闭。
    - voiceover_chars_per_second 暂时固定 4.8，不做历史语速校准。

12. 鲁棒性要求。
    - duration mismatch 不是 TTS failed。
    - missing audio 才是 TTS failed。
    - adjusted_editing_script 不覆盖原产物。
    - 遇到单个 optional shot 无 TTS segment，保留原时长并 warning。
    - required/must shot 无 TTS segment，failed 或 action_required。
    - 如果 adjusted 后仍与 TTS 差距过大，final_duration_check 拦截，不要盲目 render。

13. 现有 compact_to_tts 互斥。
    - 如果 cut_plan 使用 adjusted_editing_script，必须跳过 _compact_clips_to_voiceover_segments。
    - cut_plan 输出 meta 记录 skip_compact_to_tts=true。
    - 避免 136s -> 100s -> 75s 的重复压缩。

14. 极低 coverage_ratio 保护。
    - coverage_ratio < 0.60 时不能无脑 shrink_video。
    - 必须检查 required segment 完整、narration segment 覆盖率、tts_total_duration_seconds、预估 adjusted ratio。
    - 不满足条件时 action_required，不要伪装成成功。

15. 字幕复用现有逻辑。
    - 不要重写字幕系统。
    - 确认 subtitle_follow_actual_tts=true 时使用 tts_segments。
    - subtitle input_hash 加 adjusted_editing_script_hash。

16. final_duration_check 第一版作为 cut_plan/render 前门禁产物。
    - 不强制新增 workflow step。
    - 如果后续注册成 step，再修改 workflow_registry.py 依赖。
```

---

# 十四、代码开发细节补充：建议函数清单

## 14.1 新增/修改函数清单

建议在 `PipelineRunner` 内新增或修改：

```text
新增：
- _collect_tts_fatal_errors()
- _decide_post_tts_duration_action()
- _can_shrink_when_coverage_extremely_low()
- _build_adjusted_editing_script_from_tts()
- _rebalance_adjusted_editing_total_duration()
- _load_effective_editing_for_cut_plan()
- _file_hash_optional()
- _build_final_duration_check()
- _write_final_duration_check_or_raise()

修改：
- _step_tts_impl()
- _build_tts_duration_reconcile()
- _tts_reconcile_hard_segments()
- _tts_reconcile_hard_videos()
- step_cut_plan()
- step_subtitles() 的 input_hash
```

## 14.2 `_tts_reconcile_hard_segments()` 的兼容修改

当前它可能把 `too_short / too_long` 都当 hard segments 返回。

建议改成：

```python
def _tts_reconcile_hard_segments(self, reconcile):
    hard_segments = []
    for video in reconcile.get("videos") or []:
        for seg in video.get("segments") or []:
            if seg.get("fatal") is True:
                hard_segments.append(seg)
            elif seg.get("hard") and seg.get("hard_type") != "adjustable_duration_mismatch":
                hard_segments.append(seg)
    return hard_segments
```

或者保留旧函数，但在 `_step_tts_impl()` 中不要直接把它作为 failed 条件，而是交给 `_decide_post_tts_duration_action()`。

## 14.3 `_build_tts_duration_reconcile()` 字段补充

对 total 和 segment 都建议补充：

```json
{
  "fatal": false,
  "hard_type": "adjustable_duration_mismatch",
  "can_continue_to_auto_decision": true,
  "can_adjust_by_shrink": true
}
```

当出现这些情况时才 `fatal=true`：

```text
missing_tts_audio
missing_required_segment
invalid_audio_duration
tts_provider_failed
```

## 14.4 adjusted shot 更新字段清单

每个 shot 调整时，至少同步：

```text
source_end_seconds
source_end
local_end_seconds
local_end
duration_seconds
target_duration_seconds
target_end_seconds
clip_duration_seconds
```

但不要改：

```text
shot_id
source_id
source_index
source_clip_id
source_start_seconds
source_start
local_start_seconds
local_start
```

保留审计字段：

```text
original_source_end_seconds
original_duration_seconds
original_target_duration_seconds
tts_actual_duration_seconds
tts_tail_padding_seconds
duration_adjusted
adjust_action
adjust_reason
```

## 14.5 final_duration_check 判定建议

推荐：

```text
pass：
- 0.90 <= video_tts_ratio <= 1.20
- subtitle_end <= tts_total + 1.0
- 所有 clip duration > 0
- 所有 source_end > source_start
- 使用 adjusted 时 cut_plan meta.editing_source=adjusted_editing_script

warning：
- 1.20 < video_tts_ratio <= 1.30
- 0.85 <= video_tts_ratio < 0.90
- subtitle_end 与 tts_total 差距 <= 3s

action_required / failed：
- video_tts_ratio > 1.30
- video_tts_ratio < 0.85
- 字幕为空
- source_end 越界
- 负时长
- adjusted_editing_script 存在但 cut_plan 未使用
- 使用 adjusted 后仍发生二次 compact
```

## 14.6 配置默认值建议

即使 `config.toml` 不写，也必须有代码默认值：

```toml
[voiceover]
post_tts_auto_reconcile_enabled = true
use_adjusted_editing_script_for_cut_plan = true
tts_driven_cut_plan = true
post_tts_ok_min_ratio = 0.90
post_tts_shrink_min_ratio = 0.60
post_tts_extreme_low_ratio = 0.60
post_tts_min_viable_voiceover_seconds = 45.0
tts_visual_tail_padding_seconds = 1.0
tts_visual_tail_padding_min_seconds = 0.3
final_video_tts_max_ratio = 1.20
final_video_tts_warning_ratio = 1.30
final_video_tts_min_ratio = 0.90
adjusted_non_must_min_ratio = 0.45
adjusted_default_min_ratio = 0.60
```

## 14.7 最小测试用例

```text
Case A：当前问题复现
- editing 136s
- TTS 92s
- coverage_ratio 0.675
期望：tts success，decision=shrink_video，cut_plan 使用 adjusted，final check pass/warning。

Case B：TTS provider 真失败
- audio file 不存在
期望：tts failed，不生成 adjusted。

Case C：required segment 缺失
- narration_segments 有 shot_id，但 tts segment 缺失
期望：failed/action_required，不 shrink。

Case D：极低 coverage_ratio
- editing 120s，TTS 20s
期望：action_required，除非完整性保护条件全部通过。

Case E：重复压缩防护
- adjusted_editing_script 已生成
- ai_voiceover_compact_to_tts=true
期望：cut_plan skip compact_to_tts，不发生二次压缩。

Case F：缓存防护
- 旧 cut_plan 已存在
- 新生成 adjusted_editing_script
期望：cut_plan input_hash 变化，不复用旧缓存。

Case G：字幕防护
- TTS segments 存在
期望：subtitle timing_source=tts_segments，字幕结束时间接近 TTS 总时长。
```

---

# 十四、最终总结

本次方案要从“完整智能修复系统”收敛为：

```text
确定性 TTS 驱动画面自动收敛系统
```

第一版做扎实后，应达到：

```text
TTS 成功但短于规划画面
  -> 不 failed
  -> 自动判断 shrink_video
  -> 生成 adjusted_editing_script
  -> cut_plan 使用 adjusted
  -> 字幕跟随 TTS
  -> render 前校验
  -> 继续出片
```

这已经能解决当前核心问题，不需要第一版引入 post-TTS 自动补文案、重跑 TTS、混合策略、长版确认等复杂逻辑。
