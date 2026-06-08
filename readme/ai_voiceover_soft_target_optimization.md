# AI 配音“硬目标改软目标”专项优化方案与代码开发指南

> 适用仓库：`angelOnly/AutomaticEditing`  
> 适用分支：`clean-highlight-reassembly`  
> 入口：`python run_web.py`  
> 本文只补充“`target_duration_seconds` 从硬目标改成软目标”这一件事。  
> 不重写整体流程，不大删大改，不改变 AI 配音主链路，只在现有时长方案上做语义拆分和鲁棒性补强。

---

## 1. 当前现象

你已经把 AI 配音视频的最长限制放宽到 180 秒以内，原来的：

```text
ai_voiceover_plan_duration_invalid
```

问题已经解决。

但新的现象是：

```text
剪辑出来的两个视频都是 30 秒
而且几乎刚好 30 秒
```

这说明：

```text
max_output_video_seconds = 180 已经作为硬上限生效
但 target_duration_seconds = 30 仍然在后续链路里被当作强目标使用
```

所以当前不是“最长限制没放开”，而是：

```text
硬上限放开了
目标时长没有软化
```

---

## 2. 错误原因说明

### 2.1 target_duration_seconds 名义上是目标，实际被当成硬收敛目标

当前 Web 请求里仍然会传：

```text
target_duration_seconds = 30
```

这个字段在代码里不是只给模型做参考，而是会继续影响后面的：

```text
short_video_edit_plan
voiceover_script
tts
subtitles
cut_plan
render
```

尤其是 AI 配音模式里，`target_duration_seconds` 会被后端解析成每条成片的配音目标时长，再分配到每个 shot 的时长预算。

结果就是：

```text
虽然素材可以剪 44 秒、52 秒、70 秒
但后续配音文案、字幕和 cut_plan 仍然围绕 30 秒收敛
最终成片就容易被压成刚好 30 秒
```

### 2.2 max_output_video_seconds 只解决“能不能超过”，不决定“最终剪多长”

现在你的配置大致变成：

```text
target_duration_seconds = 30
max_output_video_seconds = 180
allow_long_video = true
```

这三个字段的合理语义应该是：

```text
target_duration_seconds：用户期望 / 推荐时长 / 软目标
max_output_video_seconds：硬上限 / 超过才阻断
allow_long_video：是否允许超过普通短视频长度
```

但当前实际效果更接近：

```text
target_duration_seconds：实际成片收敛目标
max_output_video_seconds：只负责不报错
allow_long_video：只负责是否允许长版
```

所以放宽 180 秒后，规划不会被 38 秒、45 秒卡死了；但后续仍然会尽量把成片压回 30 秒。

### 2.3 当前代码里的关键链路

当前 Web 前端会从隐藏的 `targetDuration` 取默认 30 秒，然后放进请求体：

```text
target_duration_seconds: targetDuration
```

后端命令构造会继续把它转成：

```text
--target-duration 30
```

Pipeline 解析后，AI 配音规划物化阶段会把这个目标写进每条 script，并分配 shot duration budget。后续 voiceover / subtitles / cut_plan 再围绕这个目标做文案和时间线。

因此问题不在“模型不懂 180 秒”，而在代码链路里缺少一个中间层：

```text
requested_target_duration_seconds：用户期望的软目标
effective_target_duration_seconds：本次根据实际素材采用的目标
max_output_video_seconds：硬上限
```

---

## 3. 优化目标

本次优化只做一件事：

```text
把 target_duration_seconds 从“硬目标”改成“软目标”
```

优化后的语义应为：

```text
target_duration_seconds = 30
表示：优先希望短一些，30 秒附近最好。

max_output_video_seconds = 180
表示：最终成片不能超过 180 秒。

最终实际时长：
根据已选素材的 visual_total_seconds 自然决定。
只要不超过 max_output_video_seconds，就允许继续。
```

示例：

```text
用户目标：30 秒
硬上限：180 秒

素材自然成片 28 秒 -> 允许，最终约 28 秒
素材自然成片 44 秒 -> 允许，最终约 44 秒
素材自然成片 72 秒 -> 允许，最终约 72 秒
素材自然成片 130 秒 -> 允许，最终约 130 秒
素材自然成片 181 秒 -> 阻断、重试或裁剪
```

注意：

```text
不是让所有视频都变长
也不是让目标时长失效
而是让目标时长只作为“优先建议”，不再强行压缩最终成片
```

---

## 4. 核心设计：拆分三个时长字段

### 4.1 requested_target_duration_seconds

新增语义字段：

```text
requested_target_duration_seconds
```

含义：

```text
用户在 Web / CLI 里填写的期望时长
例如 30 秒
```

用途：

```text
给模型做参考
给日志和 manifest 展示
用于判断“是否明显超过用户期望”
但不作为最终硬裁剪依据
```

### 4.2 effective_target_duration_seconds

新增语义字段：

```text
effective_target_duration_seconds
```

含义：

```text
本次 AI 配音、字幕、cut_plan 实际采用的成片目标时长
```

在 soft 模式下：

```text
effective_target_duration_seconds = min(visual_total_seconds, max_output_video_seconds)
```

也就是：

```text
已选素材 44 秒，effective target 就是 44 秒
已选素材 72 秒，effective target 就是 72 秒
```

### 4.3 max_output_video_seconds

保留现有字段：

```text
max_output_video_seconds
```

含义：

```text
硬上限
超过才阻断、重试或要求裁剪
```

它不应该反向把所有视频压成 30 秒。

---

## 5. 配置文件优化

修改文件：

```text
config.toml
```

在 `[short_video]` 下补充或调整：

```toml
[short_video]
default_target_seconds = 30
allow_long_video_default = true
max_long_video_seconds = 180

# 新增：AI 配音目标时长模式
# fixed：旧逻辑，target_duration_seconds 作为强目标
# soft：新逻辑，target_duration_seconds 作为软目标
ai_voiceover_target_mode = "soft"

# 新增：软目标提示阈值，仅用于 warning，不用于阻断
soft_target_tolerance_ratio = 0.5
soft_target_min_extra_seconds = 15
```

说明：

```text
soft_target_tolerance_ratio = 0.5
soft_target_min_extra_seconds = 15
```

表示：

```text
如果用户目标是 30 秒
软目标提示范围约为 30 + max(15, 30 * 0.5) = 45 秒

44 秒：正常
60 秒：超过软目标，但不阻断，只记录 warning
181 秒：超过硬上限，才阻断
```

---

## 6. Web 前端修改

修改文件：

```text
web_static/app.js
```

### 6.1 不要再把 allowLongVideo 写死为 false

当前逻辑里类似：

```js
const allowLongVideo = false;
```

建议改成：

```js
const productionMode = $("productionMode").value;
const targetDuration = Number($("targetDuration").value || state.defaults.default_target_seconds || 30);

const isAiVoiceover = productionMode === "ai_voiceover";
const allowLongVideo = isAiVoiceover ? true : false;
const maxOutputVideoSeconds = isAiVoiceover ? 180 : targetDuration;
const targetDurationMode = isAiVoiceover ? "soft" : "fixed";
```

然后在 `baseRunRequest()` 的返回值中补充：

```js
target_duration_seconds: targetDuration,
target_duration_mode: targetDurationMode,
max_output_video_seconds: maxOutputVideoSeconds,
allow_long_video: allowLongVideo,
```

### 6.2 保留 targetDuration 控件，不要删除

当前 `targetDuration` 是隐藏控件，默认值 30。不要删除它。

原因：

```text
它仍然有价值
它表示用户希望“尽量短”
只是不能再强行控制最终成片长度
```

如果后续想在 UI 上暴露，可以改文案为：

```text
期望时长
```

而不是：

```text
最终时长
```

### 6.3 兼容旧任务恢复

`restoreRunControls(manifest)` 读取旧任务参数时，要兼容旧 manifest 没有 `target_duration_mode` 的情况。

建议：

```js
const opts = manifest.last_run_options || manifest.last_web_run_options || {};
const mode = opts.target_duration_mode || state.defaults.ai_voiceover_target_mode || "soft";
```

不要因为旧任务缺字段导致页面报错。

---

## 7. Web 后端请求结构修改

修改文件：

```text
web_app.py
```

### 7.1 RunRequest 补充字段

确认 `RunRequest` 里有以下字段：

```python
target_duration_seconds: int = 30
target_duration_mode: str = "soft"
max_output_video_seconds: int = 180
allow_long_video: bool = True
```

如果已有 `target_duration_mode`，只调整默认值和传参逻辑。

如果没有，则新增：

```python
target_duration_mode: str = str(SHORT_VIDEO_DEFAULTS.get("ai_voiceover_target_mode", "soft"))
```

### 7.2 命令构造时传递 target_duration_mode

在 `_build_run_command(req, ...)` 里，当前已经会传：

```text
--target-duration
--max-output-video-seconds
--allow-long-video
```

需要补充：

```python
cmd += ["--target-duration-mode", req.target_duration_mode or "soft"]
```

并确保：

```python
if req.allow_long_video:
    cmd.append("--allow-long-video")
```

仍然保留。

### 7.3 AI 配音默认 max_output_video_seconds 改成 180

建议只对 AI 配音模式改默认，不影响原声重组。

伪逻辑：

```python
if req.production_mode == "ai_voiceover":
    req.max_output_video_seconds = int(req.max_output_video_seconds or 180)
    req.allow_long_video = bool(req.allow_long_video)
    req.target_duration_mode = req.target_duration_mode or "soft"
```

注意不要把 `highlight_reassembly` 也强行套进这套逻辑。

---

## 8. Pipeline 参数解析修改

修改文件：

```text
newsclip_agent/pipeline.py
```

### 8.1 RunOptions 增加字段

在 `RunOptions` dataclass 中新增：

```python
target_duration_mode: str = "soft"
```

### 8.2 parse_args 增加命令行参数

在 `parse_args()` 中新增：

```python
parser.add_argument(
    "--target-duration-mode",
    choices=["fixed", "soft"],
    default=str(default_config.short_video.get("ai_voiceover_target_mode", "soft")),
)
```

这样兼容 CLI：

```bash
python run_pipeline.py \
  --target-duration 30 \
  --target-duration-mode soft \
  --max-output-video-seconds 180 \
  --allow-long-video
```

### 8.3 保留 fixed 模式用于回滚

不要删除旧行为。

如果出现软目标带来新问题，可以用：

```bash
--target-duration-mode fixed
```

或配置：

```toml
ai_voiceover_target_mode = "fixed"
```

快速回滚到旧逻辑。

---

## 9. Pipeline 核心逻辑修改

修改文件：

```text
newsclip_agent/pipeline.py
```

### 9.1 新增安全取数工具

如果现有代码已有 `_first_number()` 或类似工具，就复用现有工具。没有则新增一个小工具，避免空值、字符串、异常值导致崩溃。

示例：

```python
def _safe_float(self, value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number
```

注意需要：

```python
import math
```

如果文件里已有类似函数，不要重复造轮子。

### 9.2 新增软目标计算函数

新增函数：

```python
def _resolve_ai_voiceover_effective_target_seconds(
    self,
    *,
    visual_total_seconds: float,
    requested_target_seconds: float | None = None,
    max_allowed_seconds: float | None = None,
) -> float:
    requested = self._safe_float(
        requested_target_seconds or getattr(self.options, "target_duration_seconds", None),
        float(getattr(self.duration_settings, "default_target_seconds", 30) or 30),
    )

    max_allowed = self._safe_float(
        max_allowed_seconds or getattr(self.options, "max_output_video_seconds", None),
        float(getattr(self.duration_settings, "max_long_video_seconds", 180) or 180),
    )

    if max_allowed <= 0:
        max_allowed = 180.0

    visual_total = self._safe_float(visual_total_seconds, 0.0)
    mode = str(getattr(self.options, "target_duration_mode", "soft") or "soft").lower()

    if mode == "fixed":
        return round(min(max(requested, 1.0), max_allowed), 3)

    if visual_total <= 0:
        return round(min(max(requested, 1.0), max_allowed), 3)

    return round(min(max(visual_total, 1.0), max_allowed), 3)
```

核心语义：

```text
fixed 模式：旧逻辑，按 target_duration_seconds
soft 模式：按已选素材 visual_total_seconds
超过 max_output_video_seconds 时最多返回 max_allowed
真正是否阻断交给 validate 函数
```

### 9.3 修改 _resolve_voiceover_target_duration

如果现有函数 `_resolve_voiceover_target_duration()` 已经被多处调用，不建议直接大删大改。

推荐微调方式：

```python
def _resolve_voiceover_target_duration(
    self,
    visual_total_seconds: float = 0.0,
    plan: dict[str, Any] | None = None,
) -> float:
    plan = plan or {}
    requested_target = plan.get("requested_target_duration_seconds") or plan.get("target_duration_seconds")
    max_allowed = plan.get("max_allowed_seconds") or getattr(self.options, "max_output_video_seconds", None)

    return self._resolve_ai_voiceover_effective_target_seconds(
        visual_total_seconds=visual_total_seconds,
        requested_target_seconds=requested_target,
        max_allowed_seconds=max_allowed,
    )
```

如果旧代码调用 `_resolve_voiceover_target_duration(plan)`，不要直接破坏参数顺序。可以用兼容写法：

```python
def _resolve_voiceover_target_duration(self, plan: dict[str, Any] | None = None, visual_total_seconds: float = 0.0) -> float:
    ...
```

实际改法以当前函数签名为准，原则是：

```text
不要让它永远优先返回 options.target_duration_seconds = 30
soft 模式下应优先跟随 visual_total_seconds
```

---

## 10. short_video_edit_plan 物化阶段修改

修改文件：

```text
newsclip_agent/pipeline.py
```

位置：

```text
_materialize_short_video_edit_plan()
```

### 10.1 当前问题

当前物化阶段会生成：

```text
editing_structure
visual_total_seconds
target_duration_seconds
max_allowed_seconds
```

但 `target_duration_seconds` 往往还是 30，导致后续预算继续压向 30。

### 10.2 修改原则

物化阶段应同时保留：

```text
requested_target_duration_seconds = 用户期望，例如 30
effective_target_duration_seconds = 实际采用，例如 44
target_duration_seconds = effective_target_duration_seconds
visual_total_seconds = 选中片段真实总时长
max_allowed_seconds = 硬上限，例如 180
target_duration_mode = soft
```

### 10.3 推荐修改片段

在生成 `editing_structure` 后，先计算：

```python
visual_total = editing_structure_duration(editing_structure)

requested_target = float(getattr(self.options, "target_duration_seconds", 30) or 30)
max_allowed = float(getattr(self.options, "max_output_video_seconds", 180) or 180)

effective_target = self._resolve_ai_voiceover_effective_target_seconds(
    visual_total_seconds=visual_total,
    requested_target_seconds=requested_target,
    max_allowed_seconds=max_allowed,
)
```

然后分配 shot budget 时使用：

```python
editing_structure = self._assign_shot_duration_budget(editing_structure, effective_target)
```

不要再传固定 30。

最终 script 字段建议：

```python
script.update({
    "requested_target_duration_seconds": round(requested_target, 3),
    "target_duration_seconds": round(effective_target, 3),
    "effective_target_duration_seconds": round(effective_target, 3),
    "visual_total_seconds": round(visual_total, 3),
    "max_allowed_seconds": round(max_allowed, 3),
    "target_duration_mode": str(getattr(self.options, "target_duration_mode", "soft") or "soft"),
})
```

这样后续旧代码如果继续读取 `target_duration_seconds`，拿到的也是本次实际目标，而不是固定 30。

---

## 11. shot duration budget 修改

修改文件：

```text
newsclip_agent/pipeline.py
```

位置：

```text
_assign_shot_duration_budget()
```

### 11.1 修改原则

不要让 shot budget 把所有片段重新压缩到 30 秒。

soft 模式下：

```text
shot target_duration_seconds 应接近原始片段 duration_seconds
所有 shot 加起来约等于 visual_total_seconds
```

也就是说：

```text
clip 1 原始 28 秒
clip 2 原始 16 秒

总计 44 秒
shot budget 应该大致还是 28 + 16
而不是按比例压成 30 秒
```

### 11.2 边界处理

如果 `effective_target` 和 `visual_total` 很接近，可以直接保留原始 shot 时长：

```python
visual_total = editing_structure_duration(editing_structure)
if abs(float(effective_target or 0) - visual_total) <= 0.5:
    for shot in editing_structure:
        duration = float(shot.get("duration_seconds") or 0)
        shot["target_duration_seconds"] = round(duration, 3)
    return editing_structure
```

如果 `visual_total > max_output_video_seconds`，不要在这里静默压缩，应交给上游校验或专门裁剪逻辑处理。

---

## 12. 校验逻辑修改

修改文件：

```text
newsclip_agent/pipeline.py
```

位置：

```text
_validate_ai_voiceover_plan_duration_or_raise()
```

### 12.1 保留硬上限校验

继续保留：

```text
visual_seconds > effective_max_output_video_seconds
```

或：

```text
visual_seconds > max_output_video_seconds
```

超过就阻断。

### 12.2 target_duration_seconds 不再作为阻断依据

不要再出现类似：

```python
if visual_seconds > target_duration_seconds:
    raise ...
```

正确逻辑：

```text
visual_seconds <= max_output_video_seconds：允许
visual_seconds > target_duration_seconds：只记 warning
visual_seconds > max_output_video_seconds：阻断
```

### 12.3 增加 soft target warning

建议在校验时产出 diagnostics，不阻断：

```python
def _soft_target_warning(self, visual_seconds: float, requested_target: float) -> dict[str, Any] | None:
    ratio = float(getattr(self.duration_settings, "soft_target_tolerance_ratio", 0.5) or 0.5)
    min_extra = float(getattr(self.duration_settings, "soft_target_min_extra_seconds", 15) or 15)
    warning_threshold = requested_target + max(min_extra, requested_target * ratio)

    if visual_seconds <= warning_threshold:
        return None

    return {
        "type": "over_soft_target",
        "requested_target_seconds": round(requested_target, 3),
        "actual_visual_seconds": round(visual_seconds, 3),
        "warning_threshold_seconds": round(warning_threshold, 3),
        "message": "实际成片超过软目标，但未超过硬上限，允许继续。",
    }
```

可以写入：

```text
agents/short_video_edit_plan/v*/duration_diagnostics.json
```

或合并进最终 `short_video_edit_plan.json` 的 `diagnostics` 字段。

---

## 13. voiceover_script 阶段注意点

不建议这次大改 voiceover_script prompt 和结构。

只需要保证它读取到的：

```text
target_duration_seconds
```

已经是：

```text
effective_target_duration_seconds
```

而不是固定 30。

同时保留：

```text
requested_target_duration_seconds
```

方便 prompt 里说明：

```text
用户期望尽量接近 30 秒，但当前素材有效画面约 44 秒，请按 44 秒生成配音文案。
```

如果后续发现文案仍然偏短，再单独优化 voiceover prompt。

---

## 14. subtitles / cut_plan 阶段注意点

本次不建议直接大改字幕和 cut_plan。

只要前面 script 里的 `target_duration_seconds` 已经变成 effective target，后续大多数逻辑会自然跟随。

需要检查代码里是否还有硬编码逻辑：

```text
target_duration_seconds
default_target_seconds
30
```

重点搜索：

```bash
grep -R "target_duration_seconds" -n newsclip_agent
grep -R "default_target_seconds" -n newsclip_agent
grep -R "30" -n newsclip_agent | grep duration
```

如果发现 cut_plan 里仍然把视频压到 `options.target_duration_seconds`，应改成优先读取：

```text
script.effective_target_duration_seconds
script.visual_total_seconds
script.target_duration_seconds
```

读取顺序建议：

```python
target = (
    script.get("effective_target_duration_seconds")
    or script.get("visual_total_seconds")
    or script.get("target_duration_seconds")
    or self.options.target_duration_seconds
)
```

---

## 15. 边界条件

### 15.1 visual_total_seconds <= 0

回退到 requested target：

```text
effective_target = requested_target
```

避免空候选导致除零或空字幕。

### 15.2 visual_total_seconds < target_duration_seconds

例如：

```text
用户目标 30 秒
素材实际 24 秒
```

soft 模式下不应该强行补到 30 秒。

建议：

```text
effective_target = 24 秒
```

如果业务上必须最低 30 秒，那也不应该静默拉长，而应该在候选阶段补充片段。

### 15.3 visual_total_seconds > max_output_video_seconds

例如：

```text
素材规划 205 秒
硬上限 180 秒
```

必须阻断、重试或裁剪。

本次建议先阻断，不要静默裁剪，避免剪掉关键事实。

### 15.4 allow_long_video = false

如果：

```text
visual_total_seconds > hard_max_without_confirmation
allow_long_video = false
```

继续触发长版确认。

不要因为 soft target 把安全确认绕过去。

### 15.5 highlight_reassembly 不走这套

原声高光重组有自己的：

```text
reassembly_target_seconds
reassembly_max_clip_count
reassembly_max_clip_seconds
```

不要把 AI 配音 soft target 逻辑强行套到原声重组。

### 15.6 多成片模式

每个 script 单独计算：

```text
requested_target_duration_seconds
effective_target_duration_seconds
visual_total_seconds
```

不要用所有视频的总时长统一分配。

### 15.7 旧任务兼容

旧任务没有：

```text
target_duration_mode
requested_target_duration_seconds
effective_target_duration_seconds
```

读取时必须 fallback：

```text
effective_target_duration_seconds
-> visual_total_seconds
-> target_duration_seconds
-> options.target_duration_seconds
-> 30
```

---

## 16. 推荐开发顺序

### 第一步：先改前端请求

文件：

```text
web_static/app.js
```

目标：

```text
AI 配音默认：
allow_long_video = true
max_output_video_seconds = 180
target_duration_mode = soft
```

验证：

```text
Web 日志里的命令应出现：
--target-duration 30
--target-duration-mode soft
--max-output-video-seconds 180
--allow-long-video
```

### 第二步：改 web_app.py 透传参数

文件：

```text
web_app.py
```

目标：

```text
RunRequest 支持 target_duration_mode
_build_run_command() 透传 --target-duration-mode
```

验证：

```text
manifest.last_web_run_options 里应有：
target_duration_mode: "soft"
max_output_video_seconds: 180
allow_long_video: true
```

### 第三步：改 pipeline.py 参数解析

文件：

```text
newsclip_agent/pipeline.py
```

目标：

```text
RunOptions 支持 target_duration_mode
parse_args 支持 --target-duration-mode
```

验证：

```bash
python run_pipeline.py --help | grep target-duration-mode
```

### 第四步：改 effective target 计算

文件：

```text
newsclip_agent/pipeline.py
```

目标：

```text
soft 模式下 effective target 跟随 visual_total_seconds
fixed 模式下保持旧逻辑
```

### 第五步：改 materialize 输出字段

文件：

```text
newsclip_agent/pipeline.py
```

目标：

```text
short_video_edit_plan.json 中出现：
requested_target_duration_seconds
effective_target_duration_seconds
target_duration_mode
visual_total_seconds
max_allowed_seconds
```

### 第六步：检查 cut_plan 是否仍硬压 30

搜索：

```bash
grep -R "target_duration_seconds" -n newsclip_agent
```

只修正明显把 `options.target_duration_seconds` 当最终时长的地方。

---

## 17. 测试方案

### 17.1 用当前成功任务重跑

从：

```text
short_video_edit_plan
```

开始重跑。

预期：

```text
short_video_edit_plan 成功
voiceover_script 成功
tts 成功
cut_plan 成功
render 成功
```

### 17.2 检查 short_video_edit_plan.json

重点看每条 script：

```json
{
  "requested_target_duration_seconds": 30,
  "target_duration_seconds": 44.2,
  "effective_target_duration_seconds": 44.2,
  "visual_total_seconds": 44.2,
  "max_allowed_seconds": 180,
  "target_duration_mode": "soft"
}
```

不应该再出现：

```json
"target_duration_seconds": 30
```

同时 `visual_total_seconds` 明显大于 30。

### 17.3 检查最终成片

预期结果：

```text
如果素材自然为 44 秒，成片应接近 44 秒
如果素材自然为 52 秒，成片应接近 52 秒
不应该两个视频都刚好 30 秒
```

允许有小范围误差：

```text
±1 秒：正常
±3 秒：可接受
超过 5 秒：需要继续看 TTS / cut_plan 时长压缩逻辑
```

### 17.4 测试 fixed 回滚模式

命令：

```bash
python run_pipeline.py \
  --target-duration 30 \
  --target-duration-mode fixed \
  --max-output-video-seconds 180 \
  --allow-long-video
```

预期：

```text
仍然接近旧逻辑
方便回滚验证
```

---

## 18. 风险点

### 18.1 文案可能变长

soft target 后，44 秒画面会生成接近 44 秒的文案，不再压成 30 秒。

这是预期结果。

### 18.2 TTS 可能暴露新的不足

之前强行压到 30 秒时，TTS 文案短，问题不明显。

改成 60、90、120 秒后，可能暴露：

```text
TTS 生成慢
文案偏短
字幕覆盖不足
cut_plan 等待音频时长不匹配
```

这些属于后续阶段问题，不应该通过把视频压回 30 秒解决。

### 18.3 长视频质量需要模型判断

180 秒只是硬上限，不代表每条都应该剪到 180 秒。

后续可以继续优化 candidate_refine / short_video_edit_plan，让模型根据新闻复杂度决定：

```text
30 秒
45 秒
60 秒
90 秒
120 秒
```

---

## 19. 回滚方案

配置回滚：

```toml
[short_video]
ai_voiceover_target_mode = "fixed"
```

或命令行回滚：

```bash
--target-duration-mode fixed
```

前端临时回滚：

```js
const targetDurationMode = "fixed";
```

后端不需要删除新增字段。

---

## 20. 最终效果

改完后，时长语义应变成：

```text
target_duration_seconds = 30
表示：用户希望短视频尽量短，30 秒附近优先。

target_duration_mode = soft
表示：30 秒不是最终硬时长。

visual_total_seconds = 44.2
表示：当前选中素材自然画面总时长。

effective_target_duration_seconds = 44.2
表示：本次配音、字幕、cut_plan 实际按 44.2 秒处理。

max_output_video_seconds = 180
表示：超过 180 秒才阻断。
```

最终目标：

```text
短内容不会被强行拉长
复杂内容不会被强行压成 30 秒
只要不超过 180 秒，就允许根据素材自然成片
```

一句话总结：

```text
把 30 秒从“最终剪辑硬目标”降级为“用户期望软目标”，
把实际剪辑目标改成“已选素材真实时长”，
把 180 秒作为唯一硬上限。
```
