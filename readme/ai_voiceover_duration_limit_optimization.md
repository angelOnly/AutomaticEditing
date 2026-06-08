# AI 配音成片时长限制优化方案与代码开发指南

> 适用仓库：`angelOnly/AutomaticEditing`  
> 适用分支：`clean-highlight-reassembly`  
> 入口：`python run_web.py`  
> 本文只围绕本次 `ai_voiceover_plan_duration_invalid` 问题做补充优化，不重写整体架构，不大改现有流程。

---

## 1. 问题背景

本次任务在 Web 页面中失败于：

```text
short_video_edit_plan
ai_voiceover_plan_duration_invalid
```

本轮传入短视频规划模型的关键约束为：

```text
target_duration_seconds = 30
raw_max_output_video_seconds = 45
effective_max_output_video_seconds = 38
allow_long_video = False
```

模型实际选择了两个 AI 配音候选片段：

```text
av_clip_0001 ≈ 28.3s
av_clip_0002 ≈ 15.9s
合计 ≈ 44.2s
```

44.2 秒超过了当前 `effective_max_output_video_seconds = 38`，所以代码在 `short_video_edit_plan` 阶段阻断，后续 `voiceover_script / tts / subtitles / cut_plan / render` 都没有继续执行。

---

## 2. 错误原因说明

### 2.1 失败不是 LLM 调用失败

`short_video_edit_plan` 这一步里，模型已经成功返回了 `clip_ids`。失败发生在模型输出之后的代码校验阶段。

当前执行链路是：

```text
step_short_video_edit_plan()
  -> _build_short_video_edit_plan_text()
  -> _run_text_agent("short_video_edit_plan", text_input)
  -> _short_video_edit_plan_semantic_diagnostics(raw_result)
  -> _materialize_short_video_edit_plan(raw_result)
  -> _validate_ai_voiceover_editing_structure_or_raise(result)
  -> _validate_ai_voiceover_plan_duration_or_raise(result)
```

这次失败点是最后的：

```text
_validate_ai_voiceover_plan_duration_or_raise(result)
```

也就是说：

```text
clip_id 有效
editing_structure 能生成
但 editing_structure 总画面时长超过当前允许上限
```

### 2.2 当前限制过硬

当前逻辑里，`target_duration_seconds = 30` 本来应该只是“期望成片时长”。但现在它和 `max_output_video_seconds / effective_max_output_video_seconds / allow_long_video` 组合后，实际变成了很强的硬限制。

对新闻拆条场景来说，这个限制偏死：

```text
短消息：30-45 秒合理
普通事件：60-90 秒合理
复杂国际新闻 / 中美关系 / 多素材汇总：90-180 秒合理
```

本次模型选择 44 秒内容，实际上并不离谱，只是被 38 秒的 effective max 卡掉了。

### 2.3 Web 当前固定关闭 allow_long_video

前端 `web_static/app.js` 的 `baseRunRequest()` 当前写死：

```js
const allowLongVideo = false;
```

因此即使 `config.toml` 里配置了 `allow_long_video_default`，Web 请求实际仍会传：

```json
"allow_long_video": false
```

这会导致后端始终按“非长版确认”策略走。

### 2.4 后端 Web 默认 max_output_video_seconds 偏小

`web_app.py` 的 `RunRequest` 当前默认值里：

```python
max_output_video_seconds: int = 45
allow_long_video: bool = bool(SHORT_VIDEO_DEFAULTS.get("allow_long_video_default", False))
```

而 `pipeline.py` 的命令行默认虽然是：

```python
--max-output-video-seconds default=90
```

但从 Web 启动时，`web_app.py` 会显式把 `req.max_output_video_seconds` 传给 pipeline，所以实际 Web 默认还是 45，而不是 pipeline CLI 默认 90。

---

## 3. 优化目标

### 3.1 产品目标

把 AI 配音成片时长从“硬卡 30-45 秒”改成“30 秒为推荐目标，180 秒以内可接受”。

建议目标：

```text
target_duration_seconds = 30      # 推荐目标，不作为硬上限
max_output_video_seconds = 180    # AI 配音硬上限
allow_long_video = True           # 默认允许 60 秒以上长版
```

### 3.2 技术目标

1. 解决 `ai_voiceover_plan_duration_invalid` 因 38/45 秒限制过死导致的误杀。
2. 保持现有 `python run_web.py` 启动方式不变。
3. 不大改工作流，不重写候选生成、TTS、字幕、渲染链路。
4. 保留代码校验，避免模型无限选片导致 3 分钟以上、5 分钟以上失控。
5. 增加边界保护：最大 180 秒，低于最小值或异常值自动兜底。
6. 对旧任务、断点续跑、重跑 from `short_video_edit_plan` 尽量兼容。

---

## 4. 总体优化方向

### 4.1 核心原则

不要取消时长校验，而是把校验分层：

```text
推荐目标：target_duration_seconds，例如 30 秒
软目标：尽量靠近 target，但允许超出
硬上限：max_output_video_seconds，例如 180 秒
长版开关：allow_long_video 默认 true
```

### 4.2 推荐最终策略

AI 配音模式下建议采用：

```text
30 秒：默认推荐长度
60 秒以内：普通新闻拆条，直接允许
60-180 秒：长版解说，默认允许，但需要 prompt 明确生成足够长文案
超过 180 秒：阻断，提示减少候选片段或降低 max_output_video_seconds
```

### 4.3 不建议的做法

不建议直接删除 `_validate_ai_voiceover_plan_duration_or_raise()`。

原因：

```text
删除后模型可能一次选太多片段
TTS 文案可能跟不上
字幕和 cut_plan 会出现更严重的后置失败
渲染阶段问题更难定位
```

应该保留校验，但把上限从 38/45 放宽到 180，并让 Web、配置、pipeline 三端一致。

---

## 5. 需要修改的文件

本次建议只小范围修改这些文件：

```text
config.toml
web_app.py
web_static/app.js
web_static/index.html
newsclip_agent/pipeline.py
```

可选补充：

```text
newsclip_agent/duration_policy.py
```

`duration_policy.py` 目前已有 `max_long_video_seconds`、`hard_max_without_confirmation` 等设置读取逻辑。如果只做 180 秒放宽，可以先不改它；如果要更规范地统一“AI 配音硬上限”，再补一个字段。

---

## 6. 具体代码开发方案

## 6.1 修改 `config.toml`

### 当前问题

当前配置：

```toml
[short_video]
default_target_seconds = 30
hard_max_without_confirmation = 60
allow_long_video_default = false
max_long_video_seconds = 90
```

这套配置更像“短视频模板”，不适合复杂新闻解说。

### 建议改法

小改即可：

```toml
[short_video]
default_target_seconds = 30
quick_news_min_seconds = 20
quick_news_max_seconds = 30
normal_min_seconds = 28
normal_max_seconds = 35
context_max_seconds = 45
complex_max_seconds = 60
hard_max_without_confirmation = 60
allow_long_video_default = true
max_long_video_seconds = 180
ai_voiceover_max_output_seconds = 180
```

说明：

```text
allow_long_video_default = true
```

表示 Web 默认允许长版，不再因为超过 60 秒就必须二次确认。

```text
max_long_video_seconds = 180
ai_voiceover_max_output_seconds = 180
```

`max_long_video_seconds` 用于兼容现有 duration_settings；`ai_voiceover_max_output_seconds` 是建议新增的更明确字段，用来区分 AI 配音解说的硬上限。

### 兼容说明

如果暂时不想新增 `ai_voiceover_max_output_seconds`，也可以只改：

```toml
allow_long_video_default = true
max_long_video_seconds = 180
```

但建议新增独立字段，后续更容易区分 AI 配音和原声重组。

---

## 6.2 修改 `web_app.py`

### 当前问题

`RunRequest` 默认值里：

```python
target_duration_seconds: int = int(SHORT_VIDEO_DEFAULTS.get("default_target_seconds", 30))
max_output_video_seconds: int = 45
allow_long_video: bool = bool(SHORT_VIDEO_DEFAULTS.get("allow_long_video_default", False))
```

其中 `max_output_video_seconds: int = 45` 是硬编码，导致 Web 默认过紧。

### 建议改法 1：RunRequest 默认读取配置

把 `max_output_video_seconds` 改成从配置读取：

```python
max_output_video_seconds: int = int(
    SHORT_VIDEO_DEFAULTS.get(
        "ai_voiceover_max_output_seconds",
        SHORT_VIDEO_DEFAULTS.get("max_long_video_seconds", 180),
    )
)
```

保留：

```python
allow_long_video: bool = bool(SHORT_VIDEO_DEFAULTS.get("allow_long_video_default", False))
```

因为配置里会改成 true。

### 建议改法 2：`/api/config` 暴露新配置

当前 `/api/config` 的 `short_video` 只返回：

```python
"default_target_seconds"
"hard_max_without_confirmation"
"allow_long_video_default"
```

建议补充：

```python
"max_long_video_seconds": int(SHORT_VIDEO_DEFAULTS.get("max_long_video_seconds", 180)),
"ai_voiceover_max_output_seconds": int(
    SHORT_VIDEO_DEFAULTS.get(
        "ai_voiceover_max_output_seconds",
        SHORT_VIDEO_DEFAULTS.get("max_long_video_seconds", 180),
    )
),
```

这样前端可以从配置拿到 180，而不是继续写死。

### 建议改法 3：命令构建前做兜底归一化

在 `_build_run_command()` 追加命令之前，建议增加一个轻量归一化，避免异常前端请求传入 0、负数、字符串等导致后续行为不可控。

推荐逻辑：

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
else:
    max_output_seconds = max(1, max_output_seconds or req.max_output_video_seconds)
```

然后命令里用：

```python
"--max-output-video-seconds",
str(max_output_seconds),
```

注意：这一段不要大改 `_build_run_command()` 结构，只在局部替换 `str(req.max_output_video_seconds)` 的来源。

---

## 6.3 修改 `web_static/app.js`

### 当前问题

`baseRunRequest()` 当前写死：

```js
const allowLongVideo = false;
```

这会覆盖配置里的默认值，导致 Web 永远不允许长版。

### 建议改法 1：前端 defaults 增加上限字段

当前 `state.defaults` 里没有 max output 字段，建议补充：

```js
max_long_video_seconds: 180,
ai_voiceover_max_output_seconds: 180,
```

在 `loadConfig()` 里补充读取：

```js
state.defaults.max_long_video_seconds = config.short_video?.max_long_video_seconds ?? state.defaults.max_long_video_seconds;
state.defaults.ai_voiceover_max_output_seconds = config.short_video?.ai_voiceover_max_output_seconds ?? state.defaults.ai_voiceover_max_output_seconds;
```

### 建议改法 2：不要再写死 allowLongVideo false

把：

```js
const allowLongVideo = false;
```

改成：

```js
const allowLongVideo = productionMode === "ai_voiceover"
  ? Boolean(state.defaults.allow_long_video_default)
  : false;
```

或者如果你希望 UI 上允许手动控制，可以读隐藏 checkbox：

```js
const allowLongVideo = productionMode === "ai_voiceover"
  ? Boolean($("allowLongVideo")?.checked ?? state.defaults.allow_long_video_default)
  : false;
```

但你当前页面把 `allowLongVideo` 控件隐藏了，最小改法是直接用配置默认值。

### 建议改法 3：请求里显式传 max_output_video_seconds

在 `baseRunRequest()` 返回对象里补充：

```js
max_output_video_seconds: productionMode === "ai_voiceover"
  ? Number(state.defaults.ai_voiceover_max_output_seconds || state.defaults.max_long_video_seconds || 180)
  : targetDuration,
```

更稳妥一点：

```js
const aiVoiceoverMaxSeconds = Number(
  state.defaults.ai_voiceover_max_output_seconds ||
  state.defaults.max_long_video_seconds ||
  180
);

const safeAiVoiceoverMaxSeconds = Math.max(30, Math.min(aiVoiceoverMaxSeconds, 180));
```

然后：

```js
max_output_video_seconds: productionMode === "ai_voiceover" ? safeAiVoiceoverMaxSeconds : targetDuration,
```

### 建议改法 4：target_duration_mode 更语义化

当前：

```js
target_duration_mode: allowLongVideo && targetDuration === 30 ? "auto_within_60" : "fixed",
```

这个 `auto_within_60` 已经不适合 180 秒策略。建议改成：

```js
target_duration_mode: allowLongVideo ? "soft_target_within_max" : "fixed",
```

如果后端暂时没有消费这个字段，也没关系，它至少能让 `last_web_run_options` 更容易排查。

---

## 6.4 修改 `web_static/index.html`

### 当前问题

`targetDuration` 输入框当前是隐藏控件：

```html
<input id="targetDuration" type="number" min="20" max="90" value="30" />
```

`allowLongVideo` 也是隐藏控件，而且默认 checked：

```html
<input id="allowLongVideo" type="checkbox" checked />
```

但前端 JS 当前并没有使用这个 checked，而是写死 false。

### 建议最小改法

只改 targetDuration 最大值，避免未来用户手动调到 120/180 时被 HTML 限制：

```html
<input id="targetDuration" type="number" min="20" max="180" value="30" />
```

### 可选增强

如果后续希望页面上显示“解说长度策略”，可以增加一个非隐藏选择器：

```html
<label id="aiVoiceoverLengthWrap" class="select-like">
  <span>解说长度</span>
  <select id="aiVoiceoverLengthMode">
    <option value="auto" selected>智能控制，最长 180 秒</option>
    <option value="short">尽量 30-45 秒</option>
    <option value="medium">允许 60-90 秒</option>
    <option value="long">允许 90-180 秒</option>
  </select>
</label>
```

但本轮不建议加这个 UI，容易牵涉更多交互。当前只要默认支持 180 秒即可。

---

## 6.5 修改 `newsclip_agent/pipeline.py`

### 当前问题

`_validate_ai_voiceover_plan_duration_or_raise()` 目前核心逻辑是：

```python
effective_max = self._effective_ai_voiceover_max_seconds()
allow_long = bool(self.options.allow_long_video)
...
if visual_seconds > effective_max + 0.01:
    issues.append(...)

if visual_seconds > self.duration_settings.hard_max_without_confirmation and not allow_long:
    issues.append(...)
```

问题是：

```text
effective_max 当前可能被算得太小
allow_long_video 从 Web 过来又是 false
最终 44 秒这种合理新闻解说也被挡掉
```

### 建议改法 1：明确 AI 配音硬上限

新增一个小函数，不要直接把逻辑散落在多个地方：

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

说明：

```text
最小 30 秒：避免配置错误为 0、1、5
最大 180 秒：防止误配成 9999 导致流程失控
默认 180 秒：符合你的产品判断
```

### 建议改法 2：调整 `_validate_ai_voiceover_plan_duration_or_raise()`

只微调，不删除校验。

建议逻辑：

```python
effective_max = max(
    float(self._effective_ai_voiceover_max_seconds() or 0),
    float(self._configured_ai_voiceover_max_seconds()),
)

effective_max = min(effective_max, float(self._configured_ai_voiceover_max_seconds()))
```

不过上面这种写法如果 `_effective_ai_voiceover_max_seconds()` 返回 38，而配置返回 180，最终会得到 180。更清晰的写法是：

```python
configured_hard_max = self._configured_ai_voiceover_max_seconds()
effective_max = configured_hard_max
```

然后保留 hard max without confirmation 的逻辑：

```python
if visual_seconds > effective_max + 0.01:
    issues.append(
        f"{sid}: visual duration {visual_seconds:.1f}s exceeds AI voiceover hard max {effective_max:.1f}s"
    )

if visual_seconds > self.duration_settings.hard_max_without_confirmation and not allow_long:
    issues.append(
        f"{sid}: visual duration {visual_seconds:.1f}s exceeds hard max without long-video confirmation"
    )
```

但因为我们会让 Web 默认 `allow_long_video = true`，所以 60-180 秒不会被第二条挡住。

### 建议改法 3：错误提示同步改掉

当前 suggestions 是：

```python
"去掉 --allow-long-video 或调小 --max-output-video-seconds 后，从 short_video_edit_plan 重跑。"
```

这个提示和实际需求相反。建议改成：

```python
suggestions=[
    "如果内容确实需要长版，请把 max_output_video_seconds 调大到 180，并允许 long video。",
    "如果只想要短版，请从 short_video_edit_plan 重跑，并要求模型减少 clip_ids 或缩短候选片段。",
]
```

### 建议改法 4：在 `_materialize_short_video_edit_plan()` 里写清楚 max_allowed

当前：

```python
max_allowed = max(target_duration, float(self.options.max_output_video_seconds or target_duration))
```

建议让它也使用统一硬上限：

```python
max_allowed = max(
    target_duration,
    min(
        float(self.options.max_output_video_seconds or target_duration),
        self._configured_ai_voiceover_max_seconds(),
    ),
)
```

或者如果想更直接：

```python
max_allowed = self._configured_ai_voiceover_max_seconds()
```

建议用第一种，兼容用户手动把 max_output_video_seconds 设置为 90、120、180 的场景。

---

## 7. Prompt 输入侧同步优化

当前 `short_video_edit_plan` 的 input 里已经会带：

```text
target_duration_seconds
raw_max_output_video_seconds
effective_max_output_video_seconds
allow_long_video
```

但当我们改成 180 后，模型仍可能误以为“target=30 就必须 30 秒”。建议在 `_build_short_video_edit_plan_text()` 的运行参数说明里补充一句。

建议补充内容：

```text
target_duration_seconds 是推荐目标，不是硬上限。
如果新闻信息量较大，可以超过 target_duration_seconds，但必须控制在 effective_max_output_video_seconds 以内。
AI 配音模式下，允许 60-180 秒长版解说；长版必须保证每个 shot 都有足够 narration_intent，便于后续 voiceover_script 生成足够长文案。
```

这样模型不会为了 30 秒强行丢关键信息，也不会因为看到 180 就无节制全选。

---

## 8. 边界条件与鲁棒性

### 8.1 最大时长边界

必须保留最大 180 秒硬上限。

建议统一规则：

```text
<= 0：视为无效，回退 180
30 以下：提升到 30
30-180：正常使用
> 180：压到 180
```

### 8.2 allow_long_video 的边界

建议：

```text
AI 配音模式：默认 true
视频重组模式：不强依赖这个字段，走 reassembly 自己的时长逻辑
CLI 手动传参：仍然尊重 --allow-long-video
```

注意：不要在 `pipeline.py` 里无条件把 `allow_long_video` 改成 true。更好的方式是在 Web 默认请求里传 true，CLI 保持当前行为，避免破坏命令行用户预期。

### 8.3 target_duration_seconds 的边界

`target_duration_seconds` 不建议改成 180。

推荐保留：

```text
target_duration_seconds = 30
```

它应该代表“模型优先尝试 30 秒左右”，不是硬上限。

### 8.4 长版文案风险

放宽到 180 后，后续最容易出问题的是：

```text
voiceover_script 文案太短
TTS 实际音频太短
字幕覆盖不足
cut_plan 画面与配音不匹配
```

所以必须同步保留现有：

```text
pre_tts_segment_min_ratio
tts_script_ok_min_ratio
tts_script_hard_min_ratio
voiceover_cover_min_ratio
subtitle_cover_min_ratio
```

不建议为了通过长版而关闭这些校验。

### 8.5 多源任务边界

多源任务走：

```text
web_app.py
  -> run_multisource_pipeline.py
  -> pipeline.py
```

所以只改 pipeline 不够，Web 请求侧也要把 `allow_long_video=true` 和 `max_output_video_seconds=180` 传下去。否则 common analysis 能复用，但模式阶段仍会被短上限卡住。

---

## 9. 开发顺序

### 第一步：改配置

先改 `config.toml`：

```toml
allow_long_video_default = true
max_long_video_seconds = 180
ai_voiceover_max_output_seconds = 180
```

### 第二步：改 `web_app.py`

改 `RunRequest.max_output_video_seconds` 默认值。  
改 `/api/config` 返回字段。  
在 `_build_run_command()` 做 max seconds 兜底。

### 第三步：改 `web_static/app.js`

改 `state.defaults`。  
改 `loadConfig()`。  
改 `baseRunRequest()`，不要写死 `allowLongVideo=false`。  
请求里显式传 `max_output_video_seconds=180`。

### 第四步：改 `web_static/index.html`

把隐藏的 `targetDuration` max 从 90 改成 180。

### 第五步：改 `pipeline.py`

新增 `_configured_ai_voiceover_max_seconds()`。  
微调 `_validate_ai_voiceover_plan_duration_or_raise()`。  
微调 `_materialize_short_video_edit_plan()` 的 `max_allowed`。  
更新错误提示。

### 第六步：只从失败步骤重跑

当前失败在 `short_video_edit_plan`，common analysis 已经完成，不需要从头跑。

建议重跑：

```text
从 short_video_edit_plan 后续重跑
```

Web 上点：

```text
选择任务 -> rerun_from -> short_video_edit_plan
```

或者命令行：

```bash
python run_multisource_pipeline.py --source-request <原 source_request.json> --task-id <原 task_id> --rerun-from short_video_edit_plan
```

---

## 10. 测试步骤

### 10.1 配置检查

启动 Web 后，打开浏览器控制台或请求：

```text
GET /api/config
```

确认返回：

```json
{
  "short_video": {
    "default_target_seconds": 30,
    "allow_long_video_default": true,
    "max_long_video_seconds": 180,
    "ai_voiceover_max_output_seconds": 180
  }
}
```

### 10.2 请求参数检查

提交任务后，看 `outputs/<task_id>/last_web_job.json` 或 `web_jobs/*.log`，确认命令包含：

```text
--target-duration 30
--max-output-video-seconds 180
--allow-long-video
```

### 10.3 重跑失败任务

对这次失败任务，从 `short_video_edit_plan` 重跑。

预期结果：

```text
short_video_edit_plan 不再因为 44.2s > 38s 失败
进入 voiceover_script
继续进入 tts / subtitles / cut_plan / render
```

### 10.4 长版测试

准备三类样本：

```text
短新闻：期望 30-45 秒
普通新闻：期望 60-90 秒
复杂多源新闻：允许 90-180 秒
```

检查：

```text
short_video_edit_plan.visual_total_seconds <= 180
target_duration_seconds 仍为 30 或合理值
voiceover_script 字数是否明显偏短
tts_outputs 是否成功
cut_plan 是否通过 duration mismatch
render 是否生成视频
```

---

## 11. 回滚方案

如果放宽到 180 后发现长版 TTS / cut_plan 不稳定，可以快速回滚配置，不必回滚全部代码。

### 方案 A：回滚到 90 秒

```toml
allow_long_video_default = true
max_long_video_seconds = 90
ai_voiceover_max_output_seconds = 90
```

### 方案 B：回滚到原短视频策略

```toml
allow_long_video_default = false
max_long_video_seconds = 90
ai_voiceover_max_output_seconds = 45
```

同时前端 `baseRunRequest()` 如果已经改为读取配置，则不需要再改 JS。

---

## 12. 风险点

### 风险 1：模型看到 180 后选太多片段

解决：Prompt 里强调：

```text
30 是推荐目标；只有新闻信息量需要时才超过；不要为了凑长而全选。
```

### 风险 2：配音文案偏短

解决：保留并强化现有 voiceover_script duration repair，不要关闭 `repair_on_script_duration_issues`、`enable_tts_duration_repair`。

### 风险 3：TTS 生成时间变长

180 秒文案会明显增加 TTS 耗时。可以接受，但 Web 任务日志要让用户知道正在生成，不要误以为卡死。

### 风险 4：字幕太密或换行不自然

当前字幕配置是 `max_lines = 1`，长版下字幕数量会增加，但不一定有问题。仍需检查一句话一行策略。

### 风险 5：旧任务恢复控件覆盖新默认值

`restoreRunControls()` 会从 `manifest.last_web_run_options` 恢复旧参数。旧失败任务可能仍记录：

```json
"max_output_video_seconds": 45,
"allow_long_video": false
```

所以重跑旧任务时，建议在后端 `_build_run_command()` 做兜底，或者在前端 `baseRunRequest()` 每次重跑都显式覆盖为新默认 180。

---

## 13. 本次最小可行改动总结

最小改动不用大动架构，只做这些：

```text
config.toml
- allow_long_video_default = true
- max_long_video_seconds = 180
- 新增 ai_voiceover_max_output_seconds = 180

web_app.py
- RunRequest.max_output_video_seconds 默认从配置读取，不再硬编码 45
- /api/config 返回 max_long_video_seconds / ai_voiceover_max_output_seconds
- _build_run_command 对 max_output_video_seconds 做 30-180 兜底

web_static/app.js
- baseRunRequest 不再 const allowLongVideo = false
- AI 配音请求显式传 allow_long_video=true
- AI 配音请求显式传 max_output_video_seconds=180

web_static/index.html
- targetDuration max 从 90 改为 180

pipeline.py
- 增加 AI 配音 hard max 读取函数
- 校验使用 180 作为硬上限
- 保留超过 180 的阻断
- 修正错误提示
```

这样能解决本次 `44.2s > 38s` 被误杀的问题，同时仍保留 180 秒硬上限，避免视频时长失控。
