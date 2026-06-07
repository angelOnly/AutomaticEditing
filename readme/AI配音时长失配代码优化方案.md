# AI 配音时长失配代码优化方案

## 1. 本次问题定位

这次输出的视频约 1 分 24 秒，但 AI 配音只有 30 多秒。结合日志包和代码，问题不是单纯 TTS 没生成，而是：

1. `short_video_edit_plan` 允许规划出接近 90 秒的画面；
2. `voiceover_script` 生成的文案实际只够 30 秒左右；
3. `tts_duration_reconcile.json` 已经判定所有 shot 都是 `too_short / hard=true / ok=false`；
4. 但 `step_tts()` 只把这个校验结果写成文件，没有把它当作失败条件；
5. `step_cut_plan()` 对 AI 配音模式下的总时长不匹配又降级成 warning，继续进入 render。

本方案目标是：

- AI 配音模式下，不能再产出“画面很长但 AI 配音只覆盖一小段”的成片；
- 保留 Web 启动方式 `python run_web.py`；
- 不破坏原声高光重组模式；
- 先以“硬阻断”为主，后续再做自动重写文案或自动压缩画面。

---

## 2. 当前代码证据

### 2.1 工作流顺序

`workflow_registry.py` 中统一多源 AI 配音链路顺序是：

```text
source_prepare
source_analysis
source_aggregate
asr_micro_segment
content_analysis_preselect
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

这说明 `tts` 后面还有 `subtitles / cut_plan / render`，如果 `tts` 只写警告不失败，后续就会继续出片。

### 2.2 配置把长视频放开了

`config.toml` 里：

```toml
[short_video]
default_target_seconds = 30
hard_max_without_confirmation = 60
allow_long_video_default = true
max_long_video_seconds = 90
```

同时这次命令带了：

```bash
--target-duration 30
--max-output-video-seconds 90
--allow-long-video
```

所以 84 秒左右的视频没有被拦住。

### 2.3 代码虽然生成了 TTS 时长校验，但没有阻断

`step_tts()` 里调用了：

```python
self._build_tts_duration_reconcile(editing, voiceover, {"outputs": outputs})
```

但返回值没有保存变量，也没有根据 `ok=false` 改变 `overall_status`。

当前 `overall_status` 只看：

- TTS output 是否为空；
- 是否有 segment 生成失败；
- 是否缺少某些 segment；
- 是否全部成功。

它没有检查：

```text
tts_duration_reconcile.ok == false
```

这就是为什么日志里 `tts_duration_reconcile.json` 明明 `ok=false`，但步骤仍然显示 `完成: tts (success)`。

### 2.4 cut_plan 又把 AI 配音总时长不匹配降级成 warning

`step_cut_plan()` 里有这段逻辑：

```python
if mismatch_reason:
    if self.options.production_mode == "ai_voiceover":
        warnings.append(mismatch_reason + "；AI配音模式不再因总时长误差阻断。")
        repair_reasons.append("voiceover_duration_mismatch_warning_only")
    else:
        blocked_reasons.append(mismatch_reason)
```

也就是说，即使画面和 AI 配音总时长差很多，AI 配音模式也不会因此阻断。

### 2.5 compact 逻辑会把画面压到 TTS 时长附近，但不保证“内容完整”

`duration_policy.compact_voiceover_segment_times()` 会把各 segment 的 target 时间改成 TTS 实际时长连续排列。

`step_cut_plan()` 中也会在 `ai_voiceover_compact_to_tts=true` 且 `timeline_mode=compact_segmented` 时调用 `_compact_clips_to_voiceover_segments()`。

这类逻辑可以把画面压短，但不能解决“文案本身太短”的根因。更重要的是，当 TTS 明显 too_short 时，不应该继续渲染。

---

## 3. 优化原则

### 原则一：AI 配音模式下，TTS 时长校验必须成为硬门禁

只要出现下面任意情况，默认不允许继续进入 `subtitles / cut_plan / render`：

```text
tts_duration_reconcile.ok = false
存在 hard=true 的 too_short / too_long / missing_tts
总 TTS 时长明显小于画面时长
```

### 原则二：短视频规划阶段必须受目标时长约束

AI 配音模式默认应该围绕 `target_duration_seconds` 输出，不应该因为 `max_output_video_seconds=90` 或 `allow_long_video=true` 就随便做成长视频。

### 原则三：自动修复可以做，但不能“修不好也继续出片”

如果开启自动修复：

```toml
enable_tts_duration_repair = true
max_tts_repair_rounds = 2
```

那么修复失败后必须阻断，而不是 warning 后继续。

### 原则四：AI 配音和原声重组要分开

这次问题只针对：

```text
production_mode = ai_voiceover
audio_policy = ai_voiceover / mixed
require_tts = true
```

不要影响 `highlight_reassembly` 原声重组模式。

---

## 4. 需要修改的文件

### 必改文件

```text
newsclip_agent/pipeline.py
newsclip_agent/duration_policy.py
config.toml
```

### 可选修改文件

```text
newsclip_agent/prompts.py
web_app.py
```

### 不需要改的文件

```text
run_web.py
run_pipeline.py
```

原因：

- `run_web.py` 只是启动 FastAPI 和 ASR preflight；
- `run_pipeline.py` 只是调用 `newsclip_agent.pipeline.main()`；
- 保持它们不动，能兼容现有 `python run_web.py`。

---

## 5. 具体修改方案

## 5.1 修改一：让 `tts_duration_reconcile` 变成 TTS 硬门禁

### 修改文件

```text
newsclip_agent/pipeline.py
```

### 修改位置

函数：

```python
def _step_tts_impl(self) -> None:
```

当前代码：

```python
out_index = write_json(base_dir / "tts_outputs.json", {
    "version": version,
    "outputs": outputs,
    "voice_id": voice_config.get("voice_id", ""),
    "voice_name": voice_config.get("voice_name", ""),
})
self._build_tts_duration_reconcile(editing, voiceover, {"outputs": outputs})
failed = sum(1 for item in outputs if item.get("status") == "failed")
success = sum(1 for item in outputs if item.get("status") == "success")
```

### 建议改法

把 reconcile 结果接住，并在 AI 配音硬需求下阻断：

```python
reconcile = self._build_tts_duration_reconcile(editing, voiceover, {"outputs": outputs})
reconcile_ok = bool(reconcile.get("ok", True))
reconcile_hard_segments = self._tts_reconcile_hard_segments(reconcile)
```

然后在 `overall_status` 判断里加入：

```python
elif tts_is_hard_required and not reconcile_ok:
    overall_status = "failed"
    hard_error = "TTS duration reconcile failed"
elif tts_is_hard_required and reconcile_hard_segments:
    overall_status = "failed"
    hard_error = "TTS duration has hard mismatch segments"
```

最后 `raise UserFacingPipelineError` 的 `technical_detail` 带上 reconcile：

```python
technical_detail={
    "success": success,
    "failed": failed,
    "total": len(outputs),
    "error": hard_error,
    "missing": missing_tts_segments,
    "tts_duration_reconcile": reconcile,
}
```

### 新增辅助函数

放在 `_build_tts_duration_reconcile()` 附近：

```python
def _tts_reconcile_hard_segments(self, reconcile: dict[str, Any]) -> list[dict[str, Any]]:
    hard_segments: list[dict[str, Any]] = []
    for video in reconcile.get("videos") or []:
        if not isinstance(video, dict):
            continue
        sid = str(video.get("short_video_id") or "")
        for seg in video.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            if seg.get("hard") or seg.get("status") in {"missing_tts", "too_short", "too_long"}:
                item = dict(seg)
                item["short_video_id"] = sid
                hard_segments.append(item)
    return hard_segments
```

### 效果

这次日志里的情况会直接在 `tts` 阶段失败：

```text
v_001_s01 too_short hard=true
v_001_s02 too_short hard=true
v_001_s03 too_short hard=true
v_001_s04 too_short hard=true
```

后续不会继续执行：

```text
subtitles
cut_plan
news_quality_gate
render
```

---

## 5.2 修改二：cut_plan 不再把 AI 配音总时长失配降级成 warning

### 修改文件

```text
newsclip_agent/pipeline.py
```

### 修改位置

函数：

```python
def step_cut_plan(self) -> None:
```

当前逻辑：

```python
if mismatch_reason:
    if self.options.production_mode == "ai_voiceover":
        warnings.append(mismatch_reason + "；AI配音模式不再因总时长误差阻断。")
        repair_reasons.append("voiceover_duration_mismatch_warning_only")
    else:
        blocked_reasons.append(mismatch_reason)
```

### 建议改法

新增配置控制：

```toml
[voiceover]
ai_voiceover_block_duration_mismatch = true
```

代码改成：

```python
block_ai_voiceover_mismatch = bool(
    self.config.raw.get("voiceover", {}).get("ai_voiceover_block_duration_mismatch", True)
)

if mismatch_reason:
    if self.options.production_mode == "ai_voiceover":
        if block_ai_voiceover_mismatch:
            blocked_reasons.append(mismatch_reason)
        else:
            warnings.append(mismatch_reason + "；配置允许 AI 配音总时长误差仅警告。")
            repair_reasons.append("voiceover_duration_mismatch_warning_only")
    else:
        blocked_reasons.append(mismatch_reason)
```

### 效果

即使 TTS 阶段没有拦住，`cut_plan` 阶段也会兜底阻断。

---

## 5.3 修改三：`_build_tts_duration_reconcile()` 增加视频级汇总字段

### 修改文件

```text
newsclip_agent/pipeline.py
```

### 修改位置

函数：

```python
def _build_tts_duration_reconcile(...)
```

### 当前不足

现在每个 video 只有：

```json
{
  "short_video_id": "v_001",
  "ok": false,
  "segments": []
}
```

但没有直接给出：

- 画面总时长；
- TTS 总时长；
- 覆盖比例；
- hard segment 数量。

### 建议新增字段

在每个 video 里加：

```python
video_target_total = sum(float(seg.get("target_duration_seconds") or 0) for seg in segment_checks)
video_tts_total = sum(float(seg.get("tts_actual_duration_seconds") or 0) for seg in segment_checks)
coverage_ratio = video_tts_total / video_target_total if video_target_total > 0 else 0.0
hard_count = sum(1 for seg in segment_checks if seg.get("hard"))
```

输出：

```python
videos.append({
    "short_video_id": sid,
    "ok": all(seg.get("status") in {"ok", "slightly_short", "slightly_long"} for seg in segment_checks),
    "target_total_duration_seconds": round(video_target_total, 3),
    "tts_total_duration_seconds": round(video_tts_total, 3),
    "coverage_ratio": round(coverage_ratio, 3),
    "hard_segment_count": hard_count,
    "segments": segment_checks,
})
```

整体 result 增加：

```python
result = {
    "ok": all(video.get("ok") for video in videos),
    "target_total_duration_seconds": round(sum(v.get("target_total_duration_seconds", 0) for v in videos), 3),
    "tts_total_duration_seconds": round(sum(v.get("tts_total_duration_seconds", 0) for v in videos), 3),
    "videos": videos,
}
```

### 效果

以后排查时不需要手算，日志直接能看出：

```text
画面总时长 84.8s
TTS 总时长 30.2s
覆盖比例 35.6%
```

---

## 5.4 修改四：短视频规划阶段收紧 max_output_video_seconds

### 修改文件

```text
newsclip_agent/pipeline.py
```

### 修改位置

函数：

```python
def _build_short_video_edit_plan_text(self) -> str:
```

当前输入给模型的是：

```python
f"- min_output_video_seconds：{self.options.min_output_video_seconds}",
f"- max_output_video_seconds：{self.options.max_output_video_seconds}",
```

但没有明确告诉模型：

```text
AI 配音模式下，优先靠近 target_duration_seconds，不要因为 max 是 90 就输出 90 秒。
```

### 建议新增有效上限计算

新增函数：

```python
def _effective_ai_voiceover_max_seconds(self) -> int:
    target = int(self.options.target_duration_seconds or self.duration_settings.default_target_seconds or 30)
    configured_max = int(self.options.max_output_video_seconds or self.duration_settings.normal_max_seconds)
    if self.options.production_mode != "ai_voiceover":
        return configured_max
    if self.options.allow_long_video:
        return min(configured_max, int(self.duration_settings.max_long_video_seconds))
    return min(configured_max, max(target + 8, int(self.duration_settings.normal_max_seconds)))
```

然后 `_build_short_video_edit_plan_text()` 改为：

```python
effective_max = self._effective_ai_voiceover_max_seconds()
```

输入里增加：

```python
f"- target_duration_seconds：{self.options.target_duration_seconds}",
f"- effective_max_output_video_seconds：{effective_max}",
"- 重要：AI 配音视频优先接近 target_duration_seconds；除非明确允许长版，否则不要贴着 max_output_video_seconds 输出。",
```

同时保留原始参数用于 debug：

```python
f"- raw_max_output_video_seconds：{self.options.max_output_video_seconds}",
f"- allow_long_video：{self.options.allow_long_video}",
```

### 效果

模型更不容易把 `--max-output-video-seconds 90` 理解成“尽量做 90 秒”。

---

## 5.5 修改五：物化 short_video_edit_plan 后做一次代码级时长裁决

### 修改文件

```text
newsclip_agent/pipeline.py
```

### 修改位置

函数：

```python
def step_short_video_edit_plan(self) -> None:
```

当前逻辑：

```python
result = self._materialize_short_video_edit_plan(raw_result)
...
self._validate_ai_voiceover_editing_structure_or_raise(result)
self._overwrite_step_json("short_video_edit_plan", result, materialized=True)
```

### 建议新增校验

在 `_validate_ai_voiceover_editing_structure_or_raise(result)` 后面加入：

```python
self._validate_ai_voiceover_plan_duration_or_raise(result)
```

新增函数：

```python
def _validate_ai_voiceover_plan_duration_or_raise(self, edit_plan: dict[str, Any]) -> None:
    if self.options.production_mode != "ai_voiceover":
        return

    effective_max = self._effective_ai_voiceover_max_seconds()
    allow_long = bool(self.options.allow_long_video)
    issues: list[str] = []

    for script in edit_plan.get("scripts") or []:
        if not isinstance(script, dict):
            continue
        sid = str(script.get("short_video_id") or "unknown")
        visual_seconds = editing_structure_duration(script.get("editing_structure") or [])

        if visual_seconds <= 0:
            issues.append(f"{sid}: visual duration is 0")
            continue

        if visual_seconds > effective_max + 0.01:
            issues.append(
                f"{sid}: visual duration {visual_seconds:.1f}s exceeds effective max {effective_max:.1f}s"
            )

        if visual_seconds > self.duration_settings.hard_max_without_confirmation and not allow_long:
            issues.append(
                f"{sid}: visual duration {visual_seconds:.1f}s exceeds hard max without long-video confirmation"
            )

    if issues:
        raise UserFacingPipelineError(
            "ai_voiceover_plan_duration_invalid",
            user_message="AI 配音选片规划失败：规划画面时长超过当前任务允许范围。",
            suggestions=[
                "去掉 --allow-long-video 或调小 --max-output-video-seconds 后，从 short_video_edit_plan 重跑。",
                "如果确实要长版，请确认长版并要求 voiceover_script 生成足够长文案。",
            ],
            technical_detail={"issues": issues[:50], "effective_max_output_video_seconds": effective_max},
        )
```

### 效果

即使模型输出过长规划，也会在 `short_video_edit_plan` 阶段失败，不会等到最后 render 才暴露。

---

## 5.6 修改六：修正 voiceover_script 字数校验口径

### 修改文件

```text
newsclip_agent/pipeline.py
```

### 问题

`_validate_voiceover_script_duration_or_raise()` 当前是按 script 级 `target_duration_seconds` 判断，并且只有 `target >= 40` 才强校验。

但这次每个 shot 都很短，整体又严重不足，导致 `voiceover_alignment_check` 只看到两个 `too_long warning`，没拦住。

### 建议

新增 segment 级估算校验，直接复用每个 segment 的 `target_duration_seconds`：

```python
def _validate_voiceover_segment_text_duration_or_raise(self, voiceover_output: dict[str, Any]) -> None:
    if self.options.production_mode != "ai_voiceover":
        return

    voice_cfg = self.config.raw.get("voiceover", {})
    min_ratio = float(voice_cfg.get("pre_tts_segment_min_ratio", 0.65))
    chars_per_second = float(voice_cfg.get("voiceover_chars_per_second", 4.8))

    issues = []
    for script in voiceover_output.get("scripts") or []:
        if not isinstance(script, dict):
            continue
        sid = str(script.get("short_video_id") or "unknown")
        for seg in script.get("narration_segments") or []:
            if not isinstance(seg, dict):
                continue
            target = float(seg.get("target_duration_seconds") or 0)
            text = re.sub(r"\s+", "", str(seg.get("text") or ""))
            estimated = len(text) / max(chars_per_second, 0.1)
            if target > 0 and estimated < target * min_ratio:
                issues.append({
                    "short_video_id": sid,
                    "shot_id": seg.get("shot_id"),
                    "target_duration_seconds": round(target, 3),
                    "estimated_text_duration_seconds": round(estimated, 3),
                    "chars": len(text),
                    "min_ratio": min_ratio,
                })

    if issues:
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

在 `step_voiceover_script()` 里，生成并 attach timing 后调用：

```python
self._validate_voiceover_segment_text_duration_or_raise(final_output)
```

放在：

```python
self._attach_voiceover_script_timings(final_output, edit_plan)
```

之后。

### 效果

在 TTS 之前就能提前拦住明显太短的文案，避免浪费 TTS 时间。

---

## 5.7 修改七：配置项调整

### 修改文件

```text
config.toml
```

### 建议改成

```toml
[short_video]
default_target_seconds = 30
hard_max_without_confirmation = 60
allow_long_video_default = false
max_long_video_seconds = 90
```

`allow_long_video_default` 建议改成 `false`。确实要长视频时，通过 Web 或 CLI 显式开启。

```toml
[voiceover]
ai_voiceover_min_ratio = 0.80
duration_mismatch_block_threshold_seconds = 1.0
ai_voiceover_compact_to_tts = true
ai_voiceover_mismatch_policy = "block"
ai_voiceover_block_duration_mismatch = true
compact_duration_tolerance_seconds = 1.0
compact_min_target_ratio = 0.65
compact_severe_short_ratio = 0.65
pre_tts_segment_min_ratio = 0.65
```

### 当前不建议继续使用

```toml
ai_voiceover_min_ratio = 0.50
duration_mismatch_block_threshold_seconds = 3.0
ai_voiceover_mismatch_policy = "repair_then_warn"
compact_duration_tolerance_seconds = 10.0
compact_min_target_ratio = 0.58
compact_severe_short_ratio = 0.58
```

原因是这组配置太宽松，会允许明显不合格的 AI 配音继续进入成片。

---

## 5.8 修改八：Web 侧长视频参数不要默认注入

### 修改文件

```text
web_app.py
```

### 目标

Web 任务默认不要自动带：

```bash
--max-output-video-seconds 90
--allow-long-video
```

### 建议

搜索构造命令的位置，通常是调用：

```python
subprocess.Popen([... RUNNER 或 MULTI_SOURCE_RUNNER ...])
```

或者拼接参数的位置。

调整原则：

1. AI 配音模式默认只传：

```bash
--target-duration 30
```

2. 只有用户在 Web 上明确勾选“允许长版”时，才传：

```bash
--allow-long-video
--max-output-video-seconds 90
```

3. Web 表单里如果有 `max_output_video_seconds`，默认值建议改成 35 或 45，不要默认 90。

4. 如果用户选择“长版 AI 配音”，页面需要提示：

```text
长版 AI 配音要求文案和 TTS 覆盖完整，否则任务会在 TTS 或 cut_plan 阶段失败。
```

---

## 6. 关键伪代码汇总

### 6.1 TTS 阶段硬阻断

```python
reconcile = self._build_tts_duration_reconcile(editing, voiceover, {"outputs": outputs})
hard_segments = self._tts_reconcile_hard_segments(reconcile)

if tts_is_hard_required and hard_segments:
    overall_status = "failed"
    hard_error = "TTS duration hard mismatch"
```

### 6.2 cut_plan 兜底阻断

```python
if mismatch_reason:
    if self.options.production_mode == "ai_voiceover" and block_ai_voiceover_mismatch:
        blocked_reasons.append(mismatch_reason)
    else:
        warnings.append(mismatch_reason)
```

### 6.3 short_video_edit_plan 时长阻断

```python
result = self._materialize_short_video_edit_plan(raw_result)
self._validate_ai_voiceover_editing_structure_or_raise(result)
self._validate_ai_voiceover_plan_duration_or_raise(result)
```

### 6.4 voiceover_script 预估文案时长阻断

```python
self._attach_voiceover_script_timings(final_output, edit_plan)
self._validate_voiceover_segment_text_duration_or_raise(final_output)
```

---

## 7. 开发顺序

### 第一步：先做最小安全修复

只改：

```text
newsclip_agent/pipeline.py
config.toml
```

实现：

1. `tts_duration_reconcile.ok=false` 时 `tts` 失败；
2. `cut_plan` 不再把 AI 配音总时长失配自动降级 warning；
3. 配置改为 block。

这一步能最快防止再次产出坏视频。

### 第二步：增强 planning 阶段约束

实现：

1. `_effective_ai_voiceover_max_seconds()`；
2. `_validate_ai_voiceover_plan_duration_or_raise()`；
3. `_build_short_video_edit_plan_text()` 加 target 和 effective_max。

这一步能减少后面失败概率。

### 第三步：增强 voiceover_script 阶段预检

实现：

1. `_validate_voiceover_segment_text_duration_or_raise()`；
2. 在 `step_voiceover_script()` 生成后调用。

这一步能提前发现文案太短，节省 TTS 时间。

### 第四步：Web 参数清理

实现：

1. AI 配音默认不传 `--allow-long-video`；
2. 默认不传 `--max-output-video-seconds 90`；
3. 长版变成明确选项。

---

## 8. 测试步骤

### 8.1 用你这次任务复现测试

先用原任务，从 `short_video_edit_plan` 重跑：

```bash
python run_pipeline.py \
  --task-id <原任务ID> \
  --rerun-from short_video_edit_plan \
  --target-duration 30
```

不要带：

```bash
--max-output-video-seconds 90
--allow-long-video
```

预期：

- `short_video_edit_plan` 输出接近 30 秒；
- 如果文案太短，`voiceover_script` 或 `tts` 阶段失败；
- 不应该继续 render 出坏视频。

### 8.2 TTS 硬阻断测试

人为保留一个 80 秒画面规划，但只写 30 秒文案，重跑：

```bash
python run_pipeline.py --task-id <任务ID> --rerun-from tts
```

预期：

```text
tts failed
manifest.status = failed
不会继续 subtitles / cut_plan / render
```

### 8.3 cut_plan 兜底测试

如果绕过 TTS 校验，让 `tts_outputs.json` 成功但总时长不匹配，重跑：

```bash
python run_pipeline.py --task-id <任务ID> --rerun-from cut_plan
```

预期：

```text
cut_plan failed
blocked_reasons 包含画面总时长与 AI 配音误差
```

### 8.4 原声重组模式回归测试

运行 highlight reassembly：

```bash
python run_pipeline.py --task-id <任务ID> --production-mode highlight_reassembly --rerun-from highlight_reassembly_plan
```

预期：

- 不受 AI 配音 TTS 校验影响；
- 原声重组仍按原逻辑输出。

### 8.5 Web 启动兼容测试

```bash
python run_web.py
```

预期：

- Web 正常启动；
- 普通 AI 配音任务默认接近 30 秒；
- 不勾选长版时不会注入 `--allow-long-video`；
- 如果任务失败，页面显示 `TTS duration reconcile failed` 或中文用户提示。

---

## 9. 回滚方案

### 9.1 配置级回滚

如果短期内觉得阻断太严格，可以只回滚配置：

```toml
ai_voiceover_block_duration_mismatch = false
ai_voiceover_mismatch_policy = "repair_then_warn"
compact_duration_tolerance_seconds = 10.0
```

但不建议长期这样做，因为会重新允许坏视频出片。

### 9.2 代码级回滚

把以下新增调用注释掉即可：

```python
self._validate_ai_voiceover_plan_duration_or_raise(result)
self._validate_voiceover_segment_text_duration_or_raise(final_output)
```

以及 TTS 阶段里对 `reconcile_ok` 的硬失败判断。

### 9.3 推荐保留项

即使回滚其他逻辑，也建议保留：

```text
_build_tts_duration_reconcile() 的视频级汇总字段
```

因为它只增强日志，不改变流程行为。

---

## 10. 风险点和兼容性说明

### 风险一：失败率会上升

这是预期结果。之前是“坏结果也成功”，改完后会变成“坏结果直接失败”。

### 风险二：长版 AI 配音更容易失败

如果允许 90 秒长版，就必须让文案和 TTS 真正覆盖 90 秒。否则会被拦住。

### 风险三：不同 TTS 语速会影响时长

当前配置里 OmniVoice speed 是 `0.85`。实际 TTS 时长和字数估算不一定完全一致，所以：

- pre-TTS 只能做粗筛；
- 真正硬判断以 TTS 后的 `actual_duration_seconds` 为准。

### 风险四：不要影响原声重组

所有新增硬判断都要限制在：

```python
self.options.production_mode == "ai_voiceover"
self.options.audio_policy in {"ai_voiceover", "mixed"}
self.options.require_tts
```

避免误伤 `highlight_reassembly`。

---

## 11. 推荐最终策略

我建议最终按下面策略落地：

1. **默认去掉** Web / CLI 里的：

```bash
--max-output-video-seconds 90
--allow-long-video
```

2. **TTS 阶段硬阻断**：

```text
tts_duration_reconcile.ok=false 不允许继续
```

3. **cut_plan 阶段兜底阻断**：

```text
AI 配音总时长与画面总时长明显不匹配，不允许 render
```

4. **short_video_edit_plan 阶段控制画面时长**：

```text
默认接近 target_duration_seconds，长版必须显式确认
```

5. **voiceover_script 阶段提前检查文案覆盖**：

```text
明显太短的文案不要进入 TTS
```

这样改完后，这次的错误会在 TTS 阶段直接失败，不会再生成“1分24秒画面 + 30秒 AI 配音”的成片。
