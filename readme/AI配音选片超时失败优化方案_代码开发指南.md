# AI 配音选片超时失败优化方案与代码开发指南

## 1. 问题背景

当前任务在 **short_video_edit_plan（短视频剪辑规划）** 阶段失败，错误类型为：

```text
ai_voiceover_plan_duration_invalid
```

这不是 ASR、TTS、字幕或渲染阶段的问题，而是 **AI 配音选片规划阶段** 已经生成了过长的画面结构，随后被代码的时长校验拦截。

本次实际现象：

```text
target_duration_seconds = 30
effective_max_output_video_seconds = 180
allow_long_video = True
模型输出 clip_ids 数量 = 11
11 个 clip 总时长 ≈ 264.236 秒
264.236 秒 > 180 秒
因此被 ai_voiceover_plan_duration_invalid 阻断
```

模型输出类似：

```json
[
  {
    "clip_ids": [
      "av_clip_0011",
      "av_clip_0001",
      "av_clip_0002",
      "av_clip_0005",
      "av_clip_0006",
      "av_clip_0003",
      "av_clip_0004",
      "av_clip_0007",
      "av_clip_0008",
      "av_clip_0009",
      "av_clip_0010"
    ]
  }
]
```

候选片段时长大致为：

```text
36.205
19.353
34.526
11.717
15.641
18.135
14.566
31.525
30.279
21.199
31.090
合计约 264.236 秒
```

## 2. 当前代码链路分析

当前入口链路：

```text
python run_web.py
  -> web_app.py
  -> _build_run_command()
  -> run_multisource_pipeline.py 或 run_pipeline.py
  -> newsclip_agent.pipeline.main()
  -> PipelineRunner.run()
  -> step_short_video_edit_plan()
```

其中：

- `run_web.py` 只负责启动 Web。
- `web_app.py` 负责把 Web 请求转换成命令行参数。
- `run_pipeline.py` 只是转发到 `newsclip_agent.pipeline.main()`。
- 实际选片失败逻辑在 `newsclip_agent/pipeline.py`。

### 2.1 Web 参数并不是主因

当前 Web 命令构造里，AI 配音模式会把 `max_output_video_seconds` 限制到配置的 `ai_voiceover_max_output_seconds` 范围内：

```python
max_output_seconds = int(req.max_output_video_seconds or 0)
configured_max = int(
    SHORT_VIDEO_DEFAULTS.get(
        "ai_voiceover_max_output_seconds",
        SHORT_VIDEO_DEFAULTS.get("max_long_video_seconds", 180),
    )
)

if req.production_mode == "ai_voiceover":
    if max_output_seconds <= 0:
        max_output_seconds = configured_max
    max_output_seconds = max(30, min(max_output_seconds, configured_max))
```

然后传入：

```python
"--max-output-video-seconds",
str(max_output_seconds),
```

如果 `req.allow_long_video` 为真，也会追加：

```python
if req.allow_long_video:
    cmd.append("--allow-long-video")
```

所以本次不是 `--max-output-video-seconds 90` 没去掉的问题。本次已经是：

```text
--max-output-video-seconds 180
--allow-long-video
```

### 2.2 AI 配音硬上限最多仍是 180 秒

`newsclip_agent/pipeline.py` 中 `_configured_ai_voiceover_max_seconds()` 会读取配置和运行参数，但最后强制压到 `30~180`：

```python
def _configured_ai_voiceover_max_seconds(self) -> float:
    short_cfg = self.config.raw.get("short_video", {}) if hasattr(self, "config") else {}
    values = [
        short_cfg.get("ai_voiceover_max_output_seconds"),
        short_cfg.get("max_long_video_seconds"),
        getattr(self.options, "max_output_video_seconds", None),
    ]
    for value in values:
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            return max(30.0, min(number, 180.0))
    return 180.0
```

所以当前语义是：

```text
allow_long_video = 可以超过 60 秒
但 AI 配音最大仍然不能超过 180 秒
```

不是：

```text
allow_long_video = 完全不限制时长
```

### 2.3 当前 short_video_edit_plan 的问题

当前 `_build_short_video_edit_plan_text()` 会告诉模型：

```text
target_duration_seconds 是推荐目标，不是硬上限。
如果新闻信息量较大，可以超过 target_duration_seconds。
AI 配音模式下，允许 60-180 秒长版解说。
```

这会导致模型把 `180` 理解成“可以尽量完整讲完”的空间，而不是安全上限。

当前 prompt 虽然写了：

```text
AI 配音视频优先接近 target_duration_seconds
不要贴着 max_output_video_seconds 输出
```

但没有明确要求：

```text
默认选 1-3 个 clip
禁止全选
总时长必须自检
超过 soft_budget 必须删
超过 hard_budget 必须重试
```

同时当前模型输出格式过于简单：

```json
[{"clip_ids":["..."]}]
```

模型不需要输出：

```text
selected_duration_seconds
为什么选择这些 clip
为什么放弃其他 clip
是否超时
```

所以模型容易只按“相关性”和“完整叙事”选片，而不是按“时长预算”选片。

### 2.4 当前代码只阻断，不自动重试或裁剪

`step_short_video_edit_plan()` 当前逻辑是：

```text
模型输出 clip_ids
-> 语义检查 clip_id 是否有效
-> _materialize_short_video_edit_plan()
-> _validate_ai_voiceover_editing_structure_or_raise()
-> _validate_ai_voiceover_plan_duration_or_raise()
-> 超时则直接失败
```

也就是说，代码可以准确算出 `visual_total_seconds`，但发现超过上限后只会抛错，不会把“你选了 264 秒，超过 180 秒”反馈给模型重试，也不会自动裁剪。

## 3. 根因总结

本次失败不是单点 bug，而是三层问题叠加：

### 3.1 上游候选都被包装成“可用 fact 片段”

`short_video_edit_plan` 读取的是 `ai_voiceover_candidate_materialize` 的候选结果，而不是原始所有片段。上游已经把候选过滤成 11 个看起来都值得用的 `fact` 片段。

模型看到的是一批“精华候选”，自然倾向于全部使用。

### 3.2 prompt 对长版过于宽松

当前 prompt 明确告诉模型：

```text
target 不是硬上限
可以超过 target
允许 60-180 秒长版
```

这会削弱 `target=30` 的约束。

### 3.3 代码缺少超时自恢复机制

当前只有失败校验，没有：

```text
时长统计 -> 超时重试 -> 二次校验 -> 代码兜底裁剪
```

所以一旦模型全选 11 个，就会直接失败。

## 4. 优化目标

本次优化目标不是简单“放大上限”，也不是让所有长视频通过，而是让系统稳定做到：

```text
1. 优先接近 target_duration_seconds。
2. effective_max_output_video_seconds 只是硬上限，不是推荐长度。
3. 模型第一次规划前就知道 soft_budget 和 hard_budget。
4. 如果模型超时，自动带着错误事实重试一次。
5. 如果重试后仍然超时，代码自动裁剪兜底。
6. 最终不再因为“全选候选导致超时”直接失败。
7. 不破坏现有 Web 启动方式 python run_web.py。
```

推荐策略：

```text
提示词硬约束
+ 代码计算总时长
+ 超时自动重试
+ 代码裁剪兜底
```

## 5. 修改范围

建议本轮主要修改：

```text
newsclip_agent/pipeline.py
```

可选修改：

```text
config.toml
```

不建议本轮修改：

```text
run_web.py
web_app.py
run_pipeline.py
run_multisource_pipeline.py
workflow_registry.py
```

原因：

- Web 参数传递当前是正常的。
- `python run_web.py` 启动方式不需要改。
- 问题发生在 `short_video_edit_plan` 的提示词和后置校验/重试逻辑。
- `run_multisource_pipeline.py` 会把 Web passthrough 参数继续传给主 pipeline，当前不是问题主因。

## 6. 配置项建议

可以先不加配置，直接在代码里用默认策略；如果希望可调，建议在 `config.toml` 增加：

```toml
[short_video_edit_plan]
duration_retry_enabled = true
duration_retry_max_rounds = 1
duration_soft_budget_ratio = 2.0
duration_soft_budget_min_seconds = 45
duration_soft_budget_max_seconds = 90
duration_hard_budget_margin_seconds = 0.0
fallback_trim_enabled = true
fallback_trim_min_clip_count = 1
fallback_trim_prefer_soft_budget = true
```

含义：

```text
duration_retry_enabled：
是否启用 short_video_edit_plan 超时重试。

duration_retry_max_rounds：
超时后最多重试几轮。建议 1，避免成本和不稳定性增加。

duration_soft_budget_ratio：
soft_budget = target_duration_seconds * ratio。

duration_soft_budget_min_seconds：
soft_budget 最低值，避免 target=30 时 soft_budget 过低。

duration_soft_budget_max_seconds：
soft_budget 最高值，避免 soft_budget 接近 180。

fallback_trim_enabled：
模型重试后仍超 hard_budget 时，是否用代码裁剪兜底。

fallback_trim_prefer_soft_budget：
裁剪时优先裁到 soft_budget；如果裁完过短或为空，再裁到 hard_budget。
```

为了少改动，也可以先不加 `config.toml`，在 `pipeline.py` 中写默认值：

```python
cfg = self.config.raw.get("short_video_edit_plan", {})
```

如果配置不存在，就用默认值。

## 7. 具体代码开发方案

下面所有改动都在：

```text
newsclip_agent/pipeline.py
```

### 7.1 新增预算计算函数

新增函数位置建议放在 `_effective_ai_voiceover_max_seconds()` 后面，靠近 `_build_short_video_edit_plan_text()`。

```python
def _short_video_edit_plan_duration_cfg(self) -> dict[str, Any]:
    cfg = self.config.raw.get("short_video_edit_plan", {}) if hasattr(self, "config") else {}
    return {
        "duration_retry_enabled": bool(cfg.get("duration_retry_enabled", True)),
        "duration_retry_max_rounds": int(cfg.get("duration_retry_max_rounds", 1) or 1),
        "duration_soft_budget_ratio": float(cfg.get("duration_soft_budget_ratio", 2.0) or 2.0),
        "duration_soft_budget_min_seconds": float(cfg.get("duration_soft_budget_min_seconds", 45) or 45),
        "duration_soft_budget_max_seconds": float(cfg.get("duration_soft_budget_max_seconds", 90) or 90),
        "fallback_trim_enabled": bool(cfg.get("fallback_trim_enabled", True)),
        "fallback_trim_min_clip_count": int(cfg.get("fallback_trim_min_clip_count", 1) or 1),
        "fallback_trim_prefer_soft_budget": bool(cfg.get("fallback_trim_prefer_soft_budget", True)),
    }


def _short_video_edit_plan_budgets(self) -> dict[str, float]:
    target = float(getattr(self.options, "target_duration_seconds", 30) or 30)
    hard = float(self._effective_ai_voiceover_max_seconds())
    cfg = self._short_video_edit_plan_duration_cfg()

    soft = target * cfg["duration_soft_budget_ratio"]
    soft = max(soft, cfg["duration_soft_budget_min_seconds"])
    soft = min(soft, cfg["duration_soft_budget_max_seconds"])
    soft = min(soft, hard)

    return {
        "target_seconds": round(target, 3),
        "soft_budget_seconds": round(soft, 3),
        "hard_budget_seconds": round(hard, 3),
    }
```

注意点：

- `hard_budget_seconds` 仍然沿用现有 `_effective_ai_voiceover_max_seconds()`。
- `soft_budget_seconds` 用来约束“不要默认做长版”。
- `soft_budget_seconds <= hard_budget_seconds`。

本次案例：

```text
target = 30
soft_budget = max(30*2, 45) = 60
hard_budget = 180
```

这样模型会优先做 60 秒以内，而不是贴近 180 秒。

### 7.2 优化 `_build_short_video_edit_plan_text()`

当前 prompt 对 long video 太宽松，需要强化硬约束。

在 `_build_short_video_edit_plan_text()` 内加入 budgets：

```python
budgets = self._short_video_edit_plan_budgets()
```

然后把运行参数部分从当前偏宽松的描述，调整为更明确的约束。

建议替换/新增这些文本：

```python
lines = [
    "任务：请生成 AI 配音短视频选片与讲述规划。",
    "",
    "运行参数：",
    f"- output_mode：{self.options.output_mode}",
    f"- max_output_videos：{self.options.max_output_videos}",
    f"- min_output_video_seconds：{self.options.min_output_video_seconds}",
    f"- target_duration_seconds：{budgets['target_seconds']}",
    f"- soft_budget_seconds：{budgets['soft_budget_seconds']}",
    f"- hard_budget_seconds：{budgets['hard_budget_seconds']}",
    f"- raw_max_output_video_seconds：{self.options.max_output_video_seconds}",
    f"- allow_long_video：{self.options.allow_long_video}",
    "",
    "硬约束：",
    "1. 你只能输出输入候选 clips 中存在的 clip_id。",
    "2. selected clip 的 duration 总和必须 <= hard_budget_seconds。",
    "3. 优先接近 target_duration_seconds；soft_budget_seconds 是推荐上限。",
    "4. hard_budget_seconds 只是安全上限，不是推荐长度，不能默认贴近 hard_budget_seconds。",
    "5. target_duration_seconds=30 左右时，默认选择 1-3 个核心 clip；只有主线不可拆时才允许更多。",
    "6. 禁止全选候选，除非所有候选总时长仍小于 soft_budget_seconds，并且每个 clip 都不可替代。",
    "7. 如果候选事实重复，优先保留信息密度高、画面证据强、能独立讲清主线的 clip。",
    "8. 输出前必须自行估算 selected_duration_seconds；如果超过 hard_budget_seconds，必须删除低优先级 clip。",
    "",
    "选片优先级：",
    "- 优先保留核心事件、结论性表态、关键事实。",
    "- 删除重复背景、弱画面、补充解释、同义信息、过长但信息密度低的 clip。",
    "- 如果要做 60 秒以上长版，必须是同一新闻链条不可拆，并且每个 shot 都有明确 narration_intent。",
    "",
]
```

输出格式可以继续保持最小结构：

```python
lines.append(
    '输出 JSON：只返回数组，例如 [{"clip_ids":["source_001_clip_0001"]}]。不要输出时间戳，不要输出额外字段。'
)
```

这里暂时不强制模型输出 `selected_duration_seconds`，避免大改解析逻辑。

### 7.3 新增 selected duration 诊断函数

新增函数用于 materialize 后检查每个 script 的时长。

```python
def _short_video_plan_duration_diagnostics(self, edit_plan: dict[str, Any]) -> dict[str, Any]:
    budgets = self._short_video_edit_plan_budgets()
    scripts = edit_plan.get("scripts") or []
    items: list[dict[str, Any]] = []
    hard_failed = False
    soft_exceeded = False

    for script in scripts:
        if not isinstance(script, dict):
            continue
        sid = str(script.get("short_video_id") or "unknown")
        clips = script.get("source_clip_ids") or []
        visual_seconds = editing_structure_duration(script.get("editing_structure") or [])

        item = {
            "short_video_id": sid,
            "source_clip_ids": clips,
            "clip_count": len(clips) if isinstance(clips, list) else 0,
            "visual_total_seconds": round(visual_seconds, 3),
            "target_seconds": budgets["target_seconds"],
            "soft_budget_seconds": budgets["soft_budget_seconds"],
            "hard_budget_seconds": budgets["hard_budget_seconds"],
            "soft_exceeded": visual_seconds > budgets["soft_budget_seconds"] + 0.01,
            "hard_exceeded": visual_seconds > budgets["hard_budget_seconds"] + 0.01,
        }
        if item["soft_exceeded"]:
            soft_exceeded = True
        if item["hard_exceeded"]:
            hard_failed = True
        items.append(item)

    return {
        "ok": not hard_failed,
        "hard_failed": hard_failed,
        "soft_exceeded": soft_exceeded,
        "items": items,
        "budgets": budgets,
    }
```

注意：

- `hard_failed` 是必须重试或失败。
- `soft_exceeded` 可以触发“压缩重试”，但不一定阻断。
- 对于 `target=30`，soft 超过说明模型可能过度选片。

### 7.4 新增 retry prompt 构造函数

当第一次输出超时，构造二次输入。

```python
def _build_short_video_edit_plan_duration_retry_text(
    self,
    *,
    base_input: str,
    raw_result: Any,
    materialized: dict[str, Any],
    diagnostics: dict[str, Any],
) -> str:
    clips_by_id = self._candidate_clips_by_id()
    budgets = diagnostics.get("budgets") or self._short_video_edit_plan_budgets()

    selected_ids: list[str] = []
    for script in materialized.get("scripts") or []:
        if not isinstance(script, dict):
            continue
        for cid in script.get("source_clip_ids") or []:
            cid = str(cid).strip()
            if cid and cid not in selected_ids:
                selected_ids.append(cid)

    lines = [
        base_input,
        "",
        "---",
        "上一次 short_video_edit_plan 输出没有通过时长校验，需要重新选择 clip_ids。",
        "",
        "失败原因：",
        f"- target_seconds：{budgets.get('target_seconds')}",
        f"- soft_budget_seconds：{budgets.get('soft_budget_seconds')}",
        f"- hard_budget_seconds：{budgets.get('hard_budget_seconds')}",
    ]

    for item in diagnostics.get("items") or []:
        lines.append(
            f"- {item.get('short_video_id')}: "
            f"clip_count={item.get('clip_count')}, "
            f"visual_total_seconds={item.get('visual_total_seconds')}, "
            f"soft_exceeded={item.get('soft_exceeded')}, "
            f"hard_exceeded={item.get('hard_exceeded')}"
        )

    lines.extend([
        "",
        "上一次选择的 clips：",
    ])

    for cid in selected_ids:
        clip = clips_by_id.get(cid)
        if not clip:
            lines.append(f"- {cid}: 未找到候选详情")
            continue
        duration = float(clip.get("duration_seconds") or 0)
        summary = compact_text(str(clip.get("summary") or clip.get("event_summary") or ""), max_chars=180)
        speech = sanitize_llm_text(str(clip.get("speech") or clip.get("asr_text") or ""), max_chars=220)
        visual = compact_text(str(clip.get("visual") or clip.get("visual_summary") or ""), max_chars=160)
        lines.append(f"- {cid} duration={duration:.3f}s")
        if summary:
            lines.append(f"  摘要：{summary}")
        if speech:
            lines.append(f"  声音：{speech}")
        if visual:
            lines.append(f"  画面：{visual}")

    lines.extend([
        "",
        "重试要求：",
        "1. 必须删除低优先级、重复信息、补充背景类 clip。",
        "2. 新的 clip_ids 总时长必须 <= hard_budget_seconds。",
        "3. 优先控制在 soft_budget_seconds 内；不要为了完整而全选。",
        "4. target 很短时，只保留最核心主线，默认 1-3 个 clip。",
        "5. 只输出 JSON 数组，例如 [{\"clip_ids\":[\"av_clip_0001\"]}]。",
        "6. 不要解释，不要输出时间戳，不要输出额外字段。",
    ])

    return "\n".join(lines)
```

注意：

- 这里复用 `base_input`，让模型仍然看到完整候选。
- 额外告诉模型上一次具体错在哪里。
- 明确让它删“低优先级、重复信息、补充背景类 clip”。

### 7.5 新增代码兜底裁剪函数

重试后如果仍然超过 `hard_budget`，用代码保证不会继续失败。

建议新增两个函数：

```python
def _clip_duration_by_id(self) -> dict[str, float]:
    durations: dict[str, float] = {}
    for index, clip in enumerate(self._load_candidate_clips_for_current_mode()):
        cid = self._clip_id(clip, index)
        if not cid:
            continue
        try:
            durations[cid] = max(0.0, float(clip.get("duration_seconds") or 0))
        except (TypeError, ValueError):
            durations[cid] = 0.0
    return durations
```

```python
def _trim_raw_short_video_plan_to_budget(
    self,
    raw_result: Any,
    *,
    budget_seconds: float,
    min_clip_count: int = 1,
) -> tuple[Any, dict[str, Any]]:
    durations = self._clip_duration_by_id()

    if isinstance(raw_result, list):
        raw_videos = raw_result
        root_is_list = True
    elif isinstance(raw_result, dict):
        raw_videos = raw_result.get("videos")
        if not isinstance(raw_videos, list):
            raw_videos = raw_result.get("scripts") if isinstance(raw_result.get("scripts"), list) else []
        root_is_list = False
    else:
        return raw_result, {"trimmed": False, "reason": "raw_result_not_supported"}

    trimmed_videos = []
    trim_reports = []

    for video in raw_videos:
        if not isinstance(video, dict):
            continue

        selected_clips_raw = video.get("selected_clips")
        if isinstance(selected_clips_raw, list) and selected_clips_raw:
            clip_ids = [
                str(item.get("clip_id") or item.get("source_clip_id") or "").strip()
                for item in selected_clips_raw
                if isinstance(item, dict)
            ]
        else:
            clip_ids = self._coerce_id_list(video.get("clip_ids") or video.get("source_clip_ids"))

        kept: list[str] = []
        dropped: list[str] = []
        total = 0.0

        for cid in clip_ids:
            duration = durations.get(cid, 0.0)
            if duration <= 0:
                dropped.append(cid)
                continue
            if kept and total + duration > budget_seconds + 0.01:
                dropped.append(cid)
                continue
            if not kept and duration > budget_seconds + 0.01:
                # 单个 clip 已经超过预算时，仍保留一个，后续让原有 hard 校验报错；
                # 不在这里切原片时间，避免破坏 clip 语义。
                kept.append(cid)
                total += duration
                continue
            kept.append(cid)
            total += duration

        if len(kept) < min_clip_count and clip_ids:
            # 兜底至少保留第一个有效 clip
            first = clip_ids[0]
            if first not in kept:
                kept = [first]
                total = durations.get(first, 0.0)
                dropped = [cid for cid in clip_ids if cid != first]

        new_video = dict(video)
        new_video.pop("selected_clips", None)
        new_video["clip_ids"] = kept
        trimmed_videos.append(new_video)

        trim_reports.append({
            "original_clip_ids": clip_ids,
            "kept_clip_ids": kept,
            "dropped_clip_ids": dropped,
            "kept_duration_seconds": round(total, 3),
            "budget_seconds": round(budget_seconds, 3),
        })

    if root_is_list:
        trimmed_result = trimmed_videos
    else:
        trimmed_result = dict(raw_result)
        if isinstance(raw_result.get("videos"), list):
            trimmed_result["videos"] = trimmed_videos
        elif isinstance(raw_result.get("scripts"), list):
            trimmed_result["scripts"] = trimmed_videos
        else:
            trimmed_result["videos"] = trimmed_videos

    return trimmed_result, {
        "trimmed": True,
        "budget_seconds": round(budget_seconds, 3),
        "reports": trim_reports,
    }
```

注意：

- 不建议直接切短单个 clip 的 source_end，因为这会破坏语义和后续字幕/配音逻辑。
- 代码兜底先按模型顺序累加，不超过预算就保留。
- 如果第一个 clip 本身超过预算，先保留一个，让后续校验决定是否失败。
- 这只是兜底，不是最佳编辑策略。

### 7.6 改造 `step_short_video_edit_plan()`

当前核心代码大致是：

```python
text_input = self._build_short_video_edit_plan_text()
raw_result = self._run_text_agent("short_video_edit_plan", text_input)
...
result = self._materialize_short_video_edit_plan(raw_result)
...
self._validate_ai_voiceover_plan_duration_or_raise(result)
self._overwrite_step_json("short_video_edit_plan", result, materialized=True)
```

建议改成以下结构：

```python
def step_short_video_edit_plan(self) -> None:
    if self.options.production_mode == "ai_voiceover" and not self._load_candidate_clips_for_current_mode():
        raise UserFacingPipelineError(
            "short_video_edit_plan_candidate_clips_empty",
            user_message="选片规划失败：AI 配音候选片段为空。",
            suggestions=["回查 ai_voiceover_candidates/v*/candidate_clips.json。", "从 content_analysis 重新运行。"],
        )

    text_input = self._build_short_video_edit_plan_text()
    raw_result = self._run_text_agent("short_video_edit_plan", text_input)

    first_diagnostics = self._short_video_edit_plan_semantic_diagnostics(raw_result)
    retry_diagnostics: dict[str, Any] | None = None

    if self.options.production_mode == "ai_voiceover" and not first_diagnostics.get("ok"):
        first_out = self.task_dir / self._step_output("short_video_edit_plan")
        if first_out.exists():
            write_json(first_out.parent / "semantic_retry_first_output.json", raw_result)
            write_json(first_out.parent / "semantic_retry_first_diagnostics.json", first_diagnostics)
        retry_input = (
            text_input
            + "\n\n---\n"
            + "上一次输出未通过业务校验，请只使用输入中存在的 clip_id 重新输出 JSON。"
            + f"\n失败原因：{first_diagnostics.get('reason')}"
            + f"\n无效 clip_id：{first_diagnostics.get('invalid_clip_ids', [])}"
            + f"\n可用 clip_id：{first_diagnostics.get('candidate_clip_ids', [])}"
        )
        raw_result = self._run_text_agent("short_video_edit_plan", retry_input)
        retry_diagnostics = self._short_video_edit_plan_semantic_diagnostics(raw_result)
        retry_out = self.task_dir / self._step_output("short_video_edit_plan")
        if retry_out.exists():
            write_json(retry_out.parent / "semantic_retry_response.json", raw_result)
            write_json(retry_out.parent / "semantic_retry_diagnostics.json", {
                "first": first_diagnostics,
                "retry": retry_diagnostics,
            })
        if not retry_diagnostics.get("ok"):
            raise UserFacingPipelineError(
                "short_video_edit_plan_semantic_retry_failed",
                user_message="AI 配音选片规划失败：模型重试后仍未返回可用 clip_id。",
                suggestions=["回查 agents/short_video_edit_plan/v*/semantic_retry_diagnostics.json。", "检查 candidate_clips 是否为空或 clip_id 是否过长难以复制。"],
                technical_detail={"first": first_diagnostics, "retry": retry_diagnostics},
            )

    result = self._materialize_short_video_edit_plan(raw_result)

    duration_retry_info = None
    trim_info = None

    if self.options.production_mode == "ai_voiceover":
        cfg = self._short_video_edit_plan_duration_cfg()
        duration_diag = self._short_video_plan_duration_diagnostics(result)
        out = self.task_dir / self._step_output("short_video_edit_plan")
        if out.exists():
            write_json(out.parent / "duration_first_diagnostics.json", duration_diag)

        need_duration_retry = (
            cfg["duration_retry_enabled"]
            and (
                duration_diag.get("hard_failed")
                or duration_diag.get("soft_exceeded")
            )
        )

        # 如果只是略微超过 soft_budget，但没有超过 hard_budget，可以选择不重试。
        # 为了避免 target=30 时输出 100+ 秒，建议 soft_exceeded 也重试一次。
        if need_duration_retry:
            retry_input = self._build_short_video_edit_plan_duration_retry_text(
                base_input=text_input,
                raw_result=raw_result,
                materialized=result,
                diagnostics=duration_diag,
            )
            retry_raw_result = self._run_text_agent("short_video_edit_plan", retry_input)
            retry_semantic = self._short_video_edit_plan_semantic_diagnostics(retry_raw_result)

            if out.exists():
                write_json(out.parent / "duration_retry_response.json", retry_raw_result)
                write_json(out.parent / "duration_retry_semantic_diagnostics.json", retry_semantic)

            if retry_semantic.get("ok"):
                retry_result = self._materialize_short_video_edit_plan(retry_raw_result)
                retry_duration_diag = self._short_video_plan_duration_diagnostics(retry_result)
                if out.exists():
                    write_json(out.parent / "duration_retry_diagnostics.json", retry_duration_diag)

                raw_result = retry_raw_result
                result = retry_result
                duration_retry_info = {
                    "triggered": True,
                    "first": duration_diag,
                    "retry": retry_duration_diag,
                    "retry_semantic": retry_semantic,
                }
            else:
                duration_retry_info = {
                    "triggered": True,
                    "first": duration_diag,
                    "retry_semantic": retry_semantic,
                    "retry_ignored": True,
                }

        # 重试后仍超过 hard_budget，则代码兜底裁剪。
        final_diag = self._short_video_plan_duration_diagnostics(result)
        if final_diag.get("hard_failed") and cfg["fallback_trim_enabled"]:
            budgets = final_diag.get("budgets") or self._short_video_edit_plan_budgets()
            budget = budgets["soft_budget_seconds"] if cfg["fallback_trim_prefer_soft_budget"] else budgets["hard_budget_seconds"]

            trimmed_raw, trim_info = self._trim_raw_short_video_plan_to_budget(
                raw_result,
                budget_seconds=float(budget),
                min_clip_count=cfg["fallback_trim_min_clip_count"],
            )
            trimmed_result = self._materialize_short_video_edit_plan(trimmed_raw)
            trimmed_diag = self._short_video_plan_duration_diagnostics(trimmed_result)

            # 如果按 soft_budget 裁剪后仍失败，再按 hard_budget 裁一次。
            if trimmed_diag.get("hard_failed") and budget < budgets["hard_budget_seconds"]:
                trimmed_raw, trim_info_hard = self._trim_raw_short_video_plan_to_budget(
                    raw_result,
                    budget_seconds=float(budgets["hard_budget_seconds"]),
                    min_clip_count=cfg["fallback_trim_min_clip_count"],
                )
                trimmed_result = self._materialize_short_video_edit_plan(trimmed_raw)
                trimmed_diag = self._short_video_plan_duration_diagnostics(trimmed_result)
                trim_info = {
                    "soft_trim": trim_info,
                    "hard_trim": trim_info_hard,
                }

            raw_result = trimmed_raw
            result = trimmed_result
            if out.exists():
                write_json(out.parent / "duration_trim_info.json", trim_info)
                write_json(out.parent / "duration_trim_diagnostics.json", trimmed_diag)

    if retry_diagnostics is not None:
        result["semantic_retry"] = {"first": first_diagnostics, "retry": retry_diagnostics}
    else:
        result["semantic_retry"] = {"first": first_diagnostics}

    if duration_retry_info:
        result["duration_retry"] = duration_retry_info
    if trim_info:
        result["duration_trim"] = trim_info

    self._validate_ai_voiceover_editing_structure_or_raise(result)
    self._validate_ai_voiceover_plan_duration_or_raise(result)
    self._overwrite_step_json("short_video_edit_plan", result, materialized=True)
    self._write_compat_short_video_plan_and_editing_script(result)
```

这个代码是开发指南级别，实际落地时注意几点：

1. `out = self.task_dir / self._step_output("short_video_edit_plan")` 依赖 `_run_text_agent()` 已经记录了 step output。当前已有语义重试代码就是这样写的，因此可以沿用。
2. duration retry 和 semantic retry 是两个不同维度：
   - semantic retry：clip_id 无效或为空。
   - duration retry：clip_id 有效，但总时长不合理。
3. duration retry 之后还要重新跑 semantic diagnostics。
4. duration retry 后仍然要经过原有 `_validate_ai_voiceover_plan_duration_or_raise()`，不能绕过最终校验。
5. 代码兜底裁剪只能保证总时长，不保证叙事最优，因此要写 `duration_trim` 诊断文件。

## 8. 是否必须让大模型删 clip

不必须。

推荐机制是：

```text
第一层：让大模型删
第二层：代码自动删
第三层：仍不合法再失败
```

原因：

- 删除哪个 clip 有编辑判断，大模型更适合判断主线和重复信息。
- 计算总时长和硬上限，代码更可靠。
- 完全让大模型删，不稳定。
- 完全让代码删，可能破坏叙事。

所以超时自动重试的目的不是“必须让大模型删几个”，而是：

```text
把上一次失败事实反馈给模型，让它做内容层面的二次选择。
如果它还是失败，代码做硬兜底。
```

## 9. 边界条件与鲁棒性

### 9.1 模型 retry 后仍然全选

处理：

```text
进入代码裁剪兜底。
按模型输出顺序累加 clip。
超过预算的 clip 跳过。
```

最终仍会进入原有时长校验。

### 9.2 单个 clip 就超过 hard_budget

处理：

```text
保留一个 clip，后续原有 hard 校验会失败。
```

不建议自动切短单个 clip，因为：

- 可能切断语义。
- 会影响后续 voiceover_script 的 shot 对齐。
- ASR/视觉摘要和 clip 时间可能不再匹配。

### 9.3 retry 返回非法 clip_id

处理：

```text
忽略 duration retry 结果，保留第一次合法结果，再尝试代码裁剪。
```

如果第一次结果也不合法，则原有 semantic retry 已经处理。

### 9.4 soft_budget 超过但 hard_budget 未超过

建议：

```text
触发一次 duration retry，但 retry 失败不阻断。
```

比如 target=30，模型选了 120 秒，没有超过 180，但明显偏离目标。这种应该尝试压缩；如果模型压缩失败，可以允许进入后续，或者根据配置决定是否裁剪到 soft_budget。

本方案建议：

```text
soft_exceeded -> 触发重试
hard_failed -> 必须重试/裁剪
```

### 9.5 候选全部相关，模型认为都不可删

处理：

```text
prompt 明确 hard_budget 是硬上限，不能以完整性为理由超过。
```

代码兜底确保不会超过 hard_budget。

### 9.6 多视频 output_mode

当前本次任务是：

```text
output_mode = single
max_output_videos = 1
```

但函数应兼容多个 script：

- diagnostics 按每个 script 计算。
- trim 函数按每个 video 独立裁剪。
- 不要把多个 video 的预算混在一起。

如果未来支持 `output_mode=multiple`，每条视频都应满足自己的 hard_budget。

### 9.7 空候选

现有代码已有：

```python
short_video_edit_plan_candidate_clips_empty
```

保持不变。

### 9.8 裁剪后为空

处理：

```text
如果裁剪后没有任何 clip，至少保留第一个有效 clip。
如果第一个也无效，最终 materialize 会 scripts 为空，然后后续校验失败。
```

可以补充更明确错误：

```text
duration_trim_empty
```

但不是必须。

### 9.9 复用缓存

`_can_reuse()` 依赖 input_hash。当前 `_run_text_agent()` 的 input 变了，理论上会生成新的 input_hash，不会误复用旧 prompt。

但要注意：

- 如果仅修改了 duration retry 的后处理函数，而初始 prompt 没变，旧任务 rerun 时可能复用旧 `short_video_edit_plan`。
- 测试时建议从 `--rerun short_video_edit_plan` 或 Web 选择重跑该步骤。
- 如果想强制避免复用旧结果，可以在 `_build_short_video_edit_plan_text()` 加一行版本标识：

```text
规划策略版本：duration_budget_v2
```

或者将 `AGENT_INFO["short_video_edit_plan"]` 的版本从：

```python
"short_video_edit_plan_text_v1"
```

改成：

```python
"short_video_edit_plan_text_v2"
```

但这会影响缓存命中。建议改版本，避免旧 prompt 缓存干扰。

## 10. 对现有 Web 启动方式的兼容

不需要改：

```text
python run_web.py
```

原因：

- Web 已经传递 `--target-duration`、`--target-duration-mode`、`--max-output-video-seconds`、`--allow-long-video`。
- 本次优化在 pipeline 内部完成。
- `run_multisource_pipeline.py` 会把 passthrough 参数继续传给 `pipeline_main()`。
- 不新增必须由 Web 传入的参数。

如果加了 `config.toml` 的 `[short_video_edit_plan]`，也是 pipeline 内部读取，不影响 Web。

## 11. 测试步骤

### 11.1 单元级手工测试

使用这次失败任务，优先只重跑：

```bash
python run_multisource_pipeline.py \
  --source-request outputs/多源剪辑_4段_AI配音解说_20260609_112816/input/source_request.json \
  --task-id 多源剪辑_4段_AI配音解说_20260609_112816 \
  --target-duration 30 \
  --target-duration-mode soft \
  --audio-policy ai_voiceover \
  --production-mode ai_voiceover \
  --output-mode single \
  --max-output-videos 1 \
  --min-output-video-seconds 30 \
  --max-output-video-seconds 180 \
  --allow-long-video \
  --require-tts \
  --rerun short_video_edit_plan
```

如果不想重新走 common analysis，确保 common 输出已经存在，并且任务 manifest 可复用。

### 11.2 检查新增诊断文件

预期生成：

```text
agents/short_video_edit_plan/v*/duration_first_diagnostics.json
```

如果触发重试：

```text
agents/short_video_edit_plan/v*/duration_retry_response.json
agents/short_video_edit_plan/v*/duration_retry_semantic_diagnostics.json
agents/short_video_edit_plan/v*/duration_retry_diagnostics.json
```

如果触发裁剪：

```text
agents/short_video_edit_plan/v*/duration_trim_info.json
agents/short_video_edit_plan/v*/duration_trim_diagnostics.json
```

### 11.3 验证最终 short_video_edit_plan

检查：

```text
agents/short_video_edit_plan/v*/short_video_edit_plan.json
```

重点看：

```json
{
  "scripts": [
    {
      "source_clip_ids": [],
      "visual_total_seconds": 0,
      "target_duration_seconds": 0,
      "max_allowed_seconds": 0,
      "duration_retry": {},
      "duration_trim": {}
    }
  ]
}
```

要求：

```text
visual_total_seconds <= 180
target=30 时优先控制在 45~60 左右
不再 11 个全选
```

### 11.4 继续跑后续步骤

如果 short_video_edit_plan 成功，再继续：

```bash
python run_multisource_pipeline.py \
  --source-request outputs/多源剪辑_4段_AI配音解说_20260609_112816/input/source_request.json \
  --task-id 多源剪辑_4段_AI配音解说_20260609_112816 \
  --target-duration 30 \
  --target-duration-mode soft \
  --audio-policy ai_voiceover \
  --production-mode ai_voiceover \
  --output-mode single \
  --max-output-videos 1 \
  --min-output-video-seconds 30 \
  --max-output-video-seconds 180 \
  --allow-long-video \
  --require-tts \
  --rerun-from voiceover_script
```

### 11.5 Web 测试

正常启动：

```bash
python run_web.py
```

在 Web 页面选择同样素材，重新提交 AI 配音任务。

观察：

- short_video_edit_plan 不再失败。
- 如果模型初次选太多，日志中应能看到 duration retry。
- 任务 manifest 最终不应停在 `ai_voiceover_plan_duration_invalid`。

## 12. 回滚方案

如果上线后出现新问题，回滚很简单：

### 12.1 仅配置关闭

如果加入了配置项，可以设置：

```toml
[short_video_edit_plan]
duration_retry_enabled = false
fallback_trim_enabled = false
```

这样保留 prompt 优化，但关闭自动重试/裁剪。

### 12.2 代码回滚

回滚 `newsclip_agent/pipeline.py` 中新增的：

```text
_short_video_edit_plan_duration_cfg
_short_video_edit_plan_budgets
_short_video_plan_duration_diagnostics
_build_short_video_edit_plan_duration_retry_text
_clip_duration_by_id
_trim_raw_short_video_plan_to_budget
```

并恢复 `step_short_video_edit_plan()` 原流程。

### 12.3 Prompt 版本回滚

如果修改了：

```python
AGENT_INFO["short_video_edit_plan"]
```

从 `short_video_edit_plan_text_v2` 回滚到 `short_video_edit_plan_text_v1`。

## 13. 风险点

### 13.1 代码裁剪可能破坏叙事

代码按顺序裁剪只能保证时长，不能保证叙事最佳。解决：

```text
优先让大模型 retry。
代码裁剪只作为最后兜底。
裁剪结果写入 duration_trim_info.json，方便排查。
```

### 13.2 soft_budget 过严导致内容不完整

如果 target=30，但新闻信息量很大，soft_budget=60 可能不够。解决：

```text
soft_budget 只触发 retry，不一定硬失败。
hard_budget 仍然是 180。
```

### 13.3 prompt 过硬导致模型选太少

如果模型只选 1 个 clip，后续 voiceover_script 可能信息不足。解决：

```text
min_output_video_seconds 仍然保留。
voiceover_script 和 cut_plan 后面仍有文案时长/成片时长检查。
必要时调大 duration_soft_budget_min_seconds。
```

### 13.4 重试增加一次 LLM 调用成本

最坏情况下多一次 text LLM 调用。可用配置控制：

```toml
duration_retry_max_rounds = 1
```

不建议超过 1。

## 14. 推荐开发顺序

### 第一步：只改 prompt

先强化 `_build_short_video_edit_plan_text()` 的硬约束和 soft/hard budget 文案。

验证模型是否还会全选 11 个。

### 第二步：加 duration diagnostics

加入 `_short_video_plan_duration_diagnostics()`，先只写诊断，不改变流程。

验证能正确记录：

```text
visual_total_seconds
soft_exceeded
hard_exceeded
```

### 第三步：加 duration retry

在 `step_short_video_edit_plan()` 中加入超时重试。

验证：

```text
第一次 264s
重试后减少 clip
最终 <= 180
```

### 第四步：加 fallback trim

如果 retry 后仍超 hard_budget，再启用代码裁剪。

### 第五步：补 config.toml

将开关配置化，方便线上快速关闭。

## 15. 最终预期效果

修改后，对于本次案例，流程应该从：

```text
模型全选 11 个 clip
-> 264 秒
-> 超过 180
-> 直接失败
```

变为：

```text
模型第一次可能仍选 11 个 clip
-> 代码计算 264 秒
-> 发现超过 hard_budget=180，同时远超 soft_budget=60
-> 自动构造 duration retry
-> 模型删掉重复/补充 clip
-> 如果仍超 180，代码裁剪兜底
-> 最终 short_video_edit_plan 成功
-> 后续进入 voiceover_script / tts / subtitles / cut_plan / render
```

本质上，这次优化不是“放宽限制”，而是把当前的失败式校验改成：

```text
可恢复的编辑规划流程
```

这样以后即使大模型再次倾向全选，系统也不会直接卡死在 `ai_voiceover_plan_duration_invalid`。
