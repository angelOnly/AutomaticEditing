下面给你第二阶段：**详细优化方案 + 代码开发指南**。
这次重点不是简单放宽阈值，而是把“文案不满足最终成片需求”统一纳入 repair 流程，避免还没修就中断。

---

# 一、错误原因讲清楚

你现在遇到的错误是：

```text
voiceover_script_segment_duration_too_short
```

它不是 TTS 报错，也不是 render 报错，而是在 **voiceover_script 生成后、TTS 之前** 被代码主动拦截。

当前链路是：

```text
step_voiceover_script()
  -> _finalize_voiceover_script_with_repair()
      -> _validate_voiceover_script_against_editing_structure()
      -> 大模型 repair，只处理 alignment 不通过的情况
      -> _validate_voiceover_segment_text_duration_or_raise()
      -> _validate_voiceover_script_duration_or_raise()
      -> _enforce_long_video_confirmation_after_voiceover()
```

代码里 `_finalize_voiceover_script_with_repair()` 确实先做了 repair，但 repair 只在 `alignment.ok == false` 时进入；后面的 `_validate_voiceover_segment_text_duration_or_raise()` 是直接硬抛错。也就是说，现在的 repair 没覆盖你这次真正失败的校验。  

你截图里的 warning：

```text
v_001_s01 chars 62 min_chars 66 warning
v_001_s02 chars 39 min_chars 47 warning
```

来自 `_validate_voiceover_script_against_editing_structure()`，这类 warning 默认不会触发 repair，因为配置里 `repair_on_warnings = false`。

真正中断的是 `_validate_voiceover_segment_text_duration_or_raise()`。它会按：

```text
estimated = 文案字数 / voiceover_chars_per_second
如果 estimated < target_duration_seconds * pre_tts_segment_min_ratio
就抛 voiceover_script_segment_duration_too_short
```

这段代码当前直接 `raise UserFacingPipelineError`，没有进入大模型 repair。

所以根因一句话：

> 代码里虽然有“大模型文案修复”，但修复入口只覆盖 alignment 硬错误，没有覆盖后置的 segment 时长硬校验，导致文案太短时直接中断。

---

# 二、优化目标

这次优化目标应该是：

```text
所有影响 TTS / 字幕 / cut_plan / render 的文案问题，都先尝试修复；
修复 N 轮仍失败，才中断。
```

不要简单粗暴地把阈值调低，也不要直接忽略 `voiceover_script_segment_duration_too_short`。因为这样虽然能跑完，但可能继续出现：

```text
AI 配音 30 秒
画面 60 秒或 90 秒
后半段无字幕、无解说
```

正确方向是：

```text
生成文案
↓
统一校验
↓
发现文案短 / 长 / 空 / 不对齐 / 撑不起 shot 时长
↓
进入 repair
↓
重新校验
↓
仍失败才中断
```

---

# 三、需要修改的文件

主要改一个文件：

```text
newsclip_agent/pipeline.py
```

可选改一个配置文件：

```text
config.toml
```

这次不建议先动 prompt 主体，也不建议改 Web 启动方式。`python run_web.py` 仍然保持兼容，因为 Web 只是拼命令调用 `run_pipeline.py`，这次修复发生在 pipeline 内部。Web 当前会把 `--target-duration`、`--max-output-video-seconds`、`--allow-long-video` 等参数传进 pipeline，命令构造在 `_build_run_command()`。

---

# 四、核心修改方案

## 方案总览

在 `pipeline.py` 里做三件事：

```text
1. 把 segment duration 校验从“直接 raise”改成“返回 issues”
2. 把这些 issues 纳入现有 voiceover repair 流程
3. repair 后再次统一校验，最后仍失败才 raise
```

当前已有 repair 函数：

```text
_build_voiceover_repair_text_input()
_repair_one_voiceover_script_patch()
_apply_voiceover_repair_patch()
_finalize_voiceover_script_with_repair()
```

可以复用，不需要另起一套复杂机制。现有 repair 输入会收集 `char_budget_issues` 和 `char_budget_warnings`，并让模型返回：

```json
{
  "short_video_id": "...",
  "repaired_segments": [
    {
      "shot_id": "...",
      "new_text": "..."
    }
  ]
}
```

对应代码位置：  

所以最稳妥的做法是：

> 把 `_validate_voiceover_segment_text_duration_or_raise()` 发现的问题转换成 repair 能识别的 `char_budget_issues`。

---

# 五、具体代码开发指南

## 1. 新增一个“只收集问题、不抛错”的函数

在 `pipeline.py` 里，建议把现有：

```python
def _validate_voiceover_segment_text_duration_or_raise(...)
```

拆成两个函数：

```python
def _collect_voiceover_segment_duration_issues(...)
def _validate_voiceover_segment_text_duration_or_raise(...)
```

### 新函数职责

```text
_collect_voiceover_segment_duration_issues()
只负责扫描 narration_segments，返回 issues 列表，不 raise。
```

建议返回结构：

```python
[
    {
        "short_video_id": "v_001",
        "shot_id": "v_001_s01",
        "target_duration_seconds": 13.8,
        "estimated_text_duration_seconds": 8.2,
        "chars": 39,
        "min_ratio": 0.65,
        "required_estimated_seconds": 8.97,
        "issue_type": "segment_duration_too_short",
        "repair_hint": "expand_text",
    }
]
```

### 代码参考

```python
def _collect_voiceover_segment_duration_issues(
    self,
    voiceover_output: dict[str, Any],
) -> list[dict[str, Any]]:
    if self.options.production_mode != "ai_voiceover":
        return []

    voice_cfg = self.config.raw.get("voiceover", {})
    min_ratio = float(voice_cfg.get("pre_tts_segment_min_ratio", 0.65))
    chars_per_second = float(voice_cfg.get("voiceover_chars_per_second", 4.8))

    issues: list[dict[str, Any]] = []

    for script in voiceover_output.get("scripts") or []:
        if not isinstance(script, dict):
            continue

        sid = str(script.get("short_video_id") or "unknown")

        for seg in script.get("narration_segments") or []:
            if not isinstance(seg, dict):
                continue

            shot_id = str(seg.get("shot_id") or "")
            target = self._first_number(seg.get("target_duration_seconds")) or 0.0
            text = clean_voiceover_text(str(seg.get("text") or ""))
            chars = len(re.sub(r"\s+", "", text))
            estimated = chars / max(chars_per_second, 0.1)

            if target > 0 and estimated < target * min_ratio:
                issues.append({
                    "short_video_id": sid,
                    "shot_id": shot_id,
                    "target_duration_seconds": round(target, 3),
                    "estimated_text_duration_seconds": round(estimated, 3),
                    "required_estimated_seconds": round(target * min_ratio, 3),
                    "chars": chars,
                    "min_ratio": min_ratio,
                    "chars_per_second": chars_per_second,
                    "issue_type": "segment_duration_too_short",
                    "repair_hint": "expand_text",
                })

    return issues
```

然后把原函数改成：

```python
def _validate_voiceover_segment_text_duration_or_raise(
    self,
    voiceover_output: dict[str, Any],
) -> None:
    issues = self._collect_voiceover_segment_duration_issues(voiceover_output)
    if not issues:
        return

    raise UserFacingPipelineError(
        "voiceover_script_segment_duration_too_short",
        user_message="配音文案生成失败：部分 shot 的文案明显短于画面目标时长。",
        suggestions=[
            "从 voiceover_script 重跑，让模型按每个 shot 的 target 秒数补足文案。",
            "如果希望视频更短，请从 short_video_edit_plan 重跑，减少画面片段。",
        ],
        technical_detail={"issues": issues[:50]},
    )
```

这样先保持原有行为不变，只是把问题收集逻辑拆出来。

---

## 2. 把 segment duration issues 注入 repair 流程

修改 `_finalize_voiceover_script_with_repair()`。

现在它是：

```python
alignment = self._validate_voiceover_script_against_editing_structure(...)
...
if not alignment.get("ok") and repair_enabled:
    ...
...
self._validate_voiceover_segment_text_duration_or_raise(final_output)
```

建议改成：

```text
alignment 检查
segment_duration_issues 检查
如果 alignment 不 ok，或 segment_duration_issues 非空，则进入 repair
repair 后重新检查两者
仍失败才 raise
```

### 建议新增辅助函数

```python
def _merge_segment_duration_issues_into_alignment(
    self,
    alignment: dict[str, Any],
    segment_issues: list[dict[str, Any]],
) -> dict[str, Any]:
    ...
```

它的作用是把 `segment_duration_too_short` 转成 repair 能识别的 `char_budget_issues`。

### 参考实现

```python
def _merge_segment_duration_issues_into_alignment(
    self,
    alignment: dict[str, Any],
    segment_issues: list[dict[str, Any]],
) -> dict[str, Any]:
    if not segment_issues:
        return alignment

    merged = dict(alignment)
    checks = [dict(check) for check in merged.get("checks", []) if isinstance(check, dict)]
    by_sid = {str(check.get("short_video_id")): check for check in checks}

    for issue in segment_issues:
        sid = str(issue.get("short_video_id") or "")
        shot_id = str(issue.get("shot_id") or "")
        if not sid or not shot_id:
            continue

        check = by_sid.get(sid)
        if check is None:
            check = {
                "short_video_id": sid,
                "ok": False,
                "shot_count": 0,
                "segment_count": 0,
                "missing_shot_ids": [],
                "extra_shot_ids": [],
                "empty_text_shot_ids": [],
                "char_budget_issues": [],
                "char_budget_warnings": [],
            }
            checks.append(check)
            by_sid[sid] = check

        char_issue = {
            "shot_id": shot_id,
            "chars": int(issue.get("chars") or 0),
            "type": "too_short",
            "severity": "hard",
            "source": "segment_duration",
            "target_duration_seconds": issue.get("target_duration_seconds"),
            "estimated_text_duration_seconds": issue.get("estimated_text_duration_seconds"),
            "required_estimated_seconds": issue.get("required_estimated_seconds"),
            "min_ratio": issue.get("min_ratio"),
        }

        check.setdefault("char_budget_issues", []).append(char_issue)
        check["ok"] = False

    merged["checks"] = checks

    hard_issues = list(merged.get("hard_issues") or [])
    for issue in segment_issues:
        hard_issues.append(
            f"{issue.get('short_video_id')}/{issue.get('shot_id')}: "
            f"segment narration too short for target duration "
            f"(target={issue.get('target_duration_seconds')}s, "
            f"estimated={issue.get('estimated_text_duration_seconds')}s)"
        )

    merged["hard_issues"] = hard_issues
    merged["ok"] = not hard_issues
    merged["segment_duration_issues"] = segment_issues[:50]
    return merged
```

这样现有 `_check_is_text_patch_repairable()` 会识别 `char_budget_issues`，从而进入 repair。这个函数当前只要有 `char_budget_issues` 或空文案就允许修复：

---

## 3. 修改 `_finalize_voiceover_script_with_repair()` 主流程

建议把当前逻辑改成下面这个结构。

### 目标流程

```python
alignment = alignment_check()
segment_issues = collect_segment_duration_issues()
alignment = merge_segment_issues_into_alignment(alignment, segment_issues)

if not alignment.ok and repair_enabled:
    for round in max_repair_rounds:
        repair
        normalize
        sync
        attach timing

        alignment = alignment_check()
        segment_issues = collect_segment_duration_issues()
        alignment = merge_segment_issues_into_alignment(alignment, segment_issues)

        if alignment.ok:
            break

if not alignment.ok:
    raise alignment error

validate_segment_duration_or_raise()
validate_script_duration_or_raise()
enforce_long_video_confirmation()
```

### 关键点

repair 循环里每一轮都要重新收集：

```text
alignment issues
segment duration issues
```

不能只重新检查 alignment，否则你这次的问题还是会漏掉。

### 参考代码结构

```python
alignment = self._validate_voiceover_script_against_editing_structure(
    final_output,
    edit_plan,
    raise_on_error=False,
)
segment_issues = self._collect_voiceover_segment_duration_issues(final_output)
alignment = self._merge_segment_duration_issues_into_alignment(alignment, segment_issues)

...

if not alignment.get("ok") and repair_enabled:
    for round_index in range(1, max_repair_rounds + 1):
        ...
        # apply patches
        ...

        self._normalize_voiceover_scripts(final_output)
        self._sync_voiceover_narration_text_from_segments(final_output)
        self._attach_voiceover_script_timings(final_output, edit_plan)

        alignment = self._validate_voiceover_script_against_editing_structure(
            final_output,
            edit_plan,
            raise_on_error=False,
        )
        segment_issues = self._collect_voiceover_segment_duration_issues(final_output)
        alignment = self._merge_segment_duration_issues_into_alignment(alignment, segment_issues)

        write_json(vdir / f"repair_round_{round_index}" / "alignment_after_repair.json", alignment)

        if alignment.get("ok"):
            break
```

最后仍然保留：

```python
if not alignment.get("ok"):
    self._validate_voiceover_script_against_editing_structure(...)
```

但这里建议不要只调用原 alignment raise，因为 segment duration issue 已经并入 alignment 了。更好是新增一个统一 raise：

```python
def _raise_voiceover_repair_failed(...)
```

---

## 4. 修改 repair 输入，让模型知道“为什么要补”

现在 `_build_voiceover_repair_text_input()` 只把 `char_budget_issues`、`char_budget_warnings`、empty 等放进去。它会构造 `problem_segments`，里面有当前字数、min/target/max、当前文本、前后文、画面、事实等。

我们把 segment duration issue 塞进 `char_budget_issues` 后，模型已经能看到问题。但建议补充两项：

```python
"target_duration_seconds"
"estimated_text_duration_seconds"
"required_estimated_seconds"
"issue_source"
```

在 `_build_voiceover_repair_text_input()` 里构造 `problem_segments` 时，当前有：

```python
"target_duration_seconds": self._first_number(shot.get("target_duration_seconds")),
```

建议增加从 issue_map 里带入更详细的 issue。

### 当前 issue_map 太粗

现在它是：

```python
issue_map[str(issue.get("shot_id"))] = issue.get("type", "unknown")
```

这会丢掉详细原因。建议改为：

```python
issue_map[str(issue.get("shot_id"))] = issue
```

然后构造 `problem_segments` 时：

```python
issue = issue_map.get(shot_id, {})
issue_type = issue.get("type") if isinstance(issue, dict) else str(issue)
```

新增字段：

```python
"issue_type": issue_type,
"issue_source": issue.get("source", "") if isinstance(issue, dict) else "",
"estimated_text_duration_seconds": issue.get("estimated_text_duration_seconds") if isinstance(issue, dict) else None,
"required_estimated_seconds": issue.get("required_estimated_seconds") if isinstance(issue, dict) else None,
"repair_instruction": (
    "当前文案按语速估算撑不起该 shot 的目标画面时长。"
    "请在不编造事实的前提下补足解释、背景或承接句；"
    "如果事实不足，只做适度补充，不要重复废话。"
)
```

这样 repair 模型不会只看到“too_short”，而是知道“配音估算时长不够”。

---

## 5. 配置建议

可以在 `config.toml` 的 `[voiceover_script]` 增加一个开关，避免未来想回滚时必须改代码：

```toml
[voiceover_script]
repair_enabled = true
max_repair_rounds = 2
repair_temperature = 0.1
repair_max_tokens = 1000
repair_on_warnings = false
repair_structural_mismatch = false

# 新增
repair_on_segment_duration_issues = true
```

然后 `_finalize_voiceover_script_with_repair()` 里读：

```python
repair_on_segment_duration_issues = bool(
    cfg.get("repair_on_segment_duration_issues", True)
)
segment_issues = (
    self._collect_voiceover_segment_duration_issues(final_output)
    if repair_on_segment_duration_issues
    else []
)
```

默认建议 `true`。

不建议把 `repair_on_warnings` 直接改成 `true`。因为 warning 可能很多，容易引发不必要的 repair。更精确的做法是：**只有会导致后续硬失败的 segment duration issue 才强制修。**

---

# 六、TTS 后也要考虑，但不要这次一步做太大

你这次问题是在 TTS 前失败。先把 TTS 前文案修复闭环做好。

后续可以再做第二层：

```text
TTS 生成后
↓
_build_tts_duration_reconcile()
↓
发现某些 segment 实际 TTS 太短 / 太长
↓
可选择重新 repair 文案并重跑 TTS
```

现在 TTS 阶段已经会做 reconcile，并且如果有 hard mismatch segment，会让 tts 失败： 

但这一步涉及重跑 TTS，复杂度更高，建议先不做。第一版只解决 **TTS 前文案估算太短直接中断**。

---

# 七、边界条件

## 1. 不要让模型为了撑时长编造事实

repair prompt 里必须强调：

```text
只能基于已有事实、画面、must_keep_fact_points、visual_summary 补充；
可以增加背景承接、解释性连接句；
不能新增未经输入支持的事实。
```

否则“补长”会变成幻觉。

## 2. 如果画面太长，不能无限补文案

比如一个 shot 目标 25 秒，但只有一个事实点，模型很难写出自然的 25 秒解说。
所以 `max_repair_rounds` 保持 2 就够了。2 轮仍失败，应该提示：

```text
文案修复后仍无法覆盖目标时长，建议从 short_video_edit_plan 重跑，缩短画面或减少片段。
```

## 3. 不要修 structural mismatch

如果是：

```text
shot_id 缺失
segment 数量不一致
extra_shot_ids
missing_shot_ids
```

这类结构性问题，简单文本 patch 未必能修。当前 `_check_is_text_patch_repairable()` 对 missing/extra 是直接返回 false，这个逻辑可以先保留。

如果后续要修结构，应该单独做“重生成 narration_segments”，不要混在文本 patch 里。

## 4. warning 不一定要修

你截图里的 62 vs 66、39 vs 47 是轻微 warning。它本身不是失败原因。
建议不要打开全量 `repair_on_warnings=true`，否则很多轻微偏差也会触发大模型，增加耗时和不稳定性。

## 5. 修复后必须同步 narration_text

每次 patch 后，都要调用：

```python
self._normalize_voiceover_scripts(final_output)
self._sync_voiceover_narration_text_from_segments(final_output)
self._attach_voiceover_script_timings(final_output, edit_plan)
```

当前代码 repair 后已经这么做了，保留即可。

否则会出现：

```text
narration_segments 修了
但 narration_text 还是旧的
TTS 用旧文本
字幕用新旧不一致
```

---

# 八、鲁棒性注意点

## 1. 任何新增 issue 都要有 `shot_id`

没有 `shot_id` 的 issue 不要塞进 repair，因为 patch 是按 `shot_id` 替换：

```python
patch_map = {shot_id: new_text}
```

当前 `_apply_voiceover_repair_patch()` 就是按 `shot_id` 替换文本。

## 2. repair 后要落盘 debug 文件

建议每轮写：

```text
repair_round_1/alignment_before_repair.json
repair_round_1/segment_duration_issues_before_repair.json
repair_round_1/alignment_after_repair.json
repair_round_1/segment_duration_issues_after_repair.json
```

这样后面你再看日志，不会只看到“失败”，能知道模型修了没有、修后差多少。

## 3. 修复失败时，错误信息要能指导用户重跑哪一步

最终仍失败时，建议错误提示改成：

```text
配音文案修复后仍短于画面目标时长。
建议：
1. 从 voiceover_script 重跑，尝试重新生成更完整文案；
2. 如果仍失败，从 short_video_edit_plan 重跑，减少画面片段或降低目标时长；
3. 检查 agents/voiceover_script/v*/repair_round_*。
```

## 4. 不要影响原声重组模式

所有新增逻辑都加：

```python
if self.options.production_mode != "ai_voiceover":
    return []
```

避免影响 `highlight_reassembly`。

## 5. 不要破坏 Web 启动

这次不改 `web_app.py` 的 `_build_run_command()`。Web 仍旧调用：

```text
python run_web.py
```

里面启动 Uvicorn，然后 Web 提交任务时继续走 `run_pipeline.py` 或 `run_multisource_pipeline.py`。`run_pipeline.py` 只是转发到 `newsclip_agent.pipeline.main()`。 

---

# 九、开发顺序

建议按这个顺序做：

## 第一步：拆分收集函数

改：

```text
newsclip_agent/pipeline.py
```

把 `_validate_voiceover_segment_text_duration_or_raise()` 拆成：

```text
_collect_voiceover_segment_duration_issues()
_validate_voiceover_segment_text_duration_or_raise()
```

先不改变行为，确保原有错误还能正常抛出。

## 第二步：新增 merge 函数

新增：

```python
_merge_segment_duration_issues_into_alignment()
```

把 segment issues 转成 alignment 的 `char_budget_issues`。

## 第三步：改 `_finalize_voiceover_script_with_repair()`

让它在 repair 前后都执行：

```python
alignment = ...
segment_issues = ...
alignment = self._merge_segment_duration_issues_into_alignment(...)
```

## 第四步：增强 repair 输入

改 `_build_voiceover_repair_text_input()`：

* `issue_map` 从字符串改成 dict
* `problem_segments` 增加 duration issue 详细字段
* 增加 `repair_instruction`

## 第五步：加配置开关

`config.toml` 新增：

```toml
repair_on_segment_duration_issues = true
```

读取时默认 true。

## 第六步：补 debug 文件

每轮 repair 前后写 JSON，方便排查。

---

# 十、测试步骤

## 测试 1：复现你当前任务

用当前失败任务：

```text
rerun = voiceover_script
```

或者 Web 上点“从此步骤重跑 / 生成配音文案”。

预期：

```text
不再直接 voiceover_script_segment_duration_too_short
而是先进入 repair_round_1
```

看日志应出现：

```text
Voiceover alignment warnings
repair_round_1
alignment_after_repair
```

## 测试 2：确认修复后进入 TTS

如果 repair 成功，后续应该继续：

```text
voiceover_script success
tts running / success
subtitles success
cut_plan success
render success
```

## 测试 3：确认字幕跟配音

生成后看：

```text
subtitles/v_001/subtitle.srt
tts/omnivoice/v_001/tts_outputs.json
edit/cut_plan/v*/cut_plan.json
```

预期：

```text
字幕时长跟 TTS segments 一致
cut_plan 中 video_duration_seconds 接近 voiceover_duration_seconds
不会出现后半段无字幕但继续播放很久
```

## 测试 4：故意构造极短文案

把某个 segment 文本手动改成：

```text
好。
```

重跑 `voiceover_script` 或从该步骤继续。

预期：

```text
先 repair
repair 失败后才报错
错误里包含 segment_duration_issues
```

## 测试 5：原声重组不受影响

跑 `production_mode=highlight_reassembly`。

预期：

```text
不进入 voiceover repair
不触发新增 segment duration 校验
```

---

# 十一、回滚方案

最简单回滚：

```toml
[voiceover_script]
repair_on_segment_duration_issues = false
```

如果你按我建议保留了 `_validate_voiceover_segment_text_duration_or_raise()` 的旧行为，那么关闭这个开关后，逻辑会退回：

```text
segment 文案太短 -> 直接中断
```

代码级回滚也简单：

```text
还原 _finalize_voiceover_script_with_repair() 中 merge segment issues 的部分
保留拆出来的 collect 函数也不影响
```

---

# 十二、最终推荐实现口径

我建议不要写成“所有 warning 都修”，而是写成：

```text
所有会导致后续 TTS / cut_plan / render 失败的文案问题，都必须先进入 repair。
轻微 warning 不强制修。
repair 失败后才中断。
```

这能避免两个问题：

```text
1. 现在这种还没修就失败；
2. 所有小 warning 都修，导致耗时增加和文案反复漂移。
```

这次最关键的改动就是：

```text
把 voiceover_script_segment_duration_too_short
从“后置硬中断”
改成“repair issue -> 修复 -> 再校验 -> 仍失败才中断”
```

这样才符合你最初想要的“大模型文案修复”设计。
