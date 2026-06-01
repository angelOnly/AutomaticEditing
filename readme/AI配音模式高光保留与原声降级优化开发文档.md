# AI 配音模式高光保留与原声降级优化开发文档

## 1. 需求背景

当前 Phoenix Newsclip Agent 面向新闻长视频自动剪辑，用户在 Web 页面选择“AI 配音”后，系统会执行：

1. 长视频分析。
2. 高光候选片段识别。
3. 短视频规划。
4. 剪辑脚本生成。
5. AI 配音脚本生成。
6. TTS 生成。
7. cut_plan 质量检查。
8. 渲染成片。

在最近一次任务中，原视频约 22 分钟：

```text
任务：美伊冲突与中东局势相关_20260527_0945
候选高光片段：5 个
短视频规划：3 条
最终成功渲染：1 条，约 36.96 秒
```

系统规划出的 3 条短视频分别是：

```text
sv_001_hormuz_intercept       美军拦截货船核心事件，目标约 55 秒
sv_002_diplomatic_response    多方外交回应，目标约 45 秒
sv_003_impact_outlook         油价与后续走势解读，目标约 50 秒
```

最终只输出了：

```text
sv_003_impact_outlook         36.96 秒
```

另外两条并不是新闻价值不足，而是在 cut_plan 阶段被质量规则阻断：

```text
sv_001：AI 配音与证据原声窗口重叠，句间空白过长，原声证据片段太短
sv_002：AI 配音与证据原声窗口重叠，句间空白过长，原声证据片段太短
```

这暴露出一个产品语义问题：用户选择的是“AI 配音”，不是“必须保留原声”。在 AI 配音模式下，原声只是可选增强，不应因为模型建议保留原声而阻断高光片段。

## 2. 为什么要优化

### 2.1 当前结果不符合用户预期

用户对 22 分钟新闻长视频的预期是：

```text
至少剪出 1 分钟左右；
内容丰满时，应输出 2-3 分钟的多条短视频或一条综合版。
```

而当前系统输出 36.96 秒，明显偏短，且漏掉了核心事件高光：

```text
美军拦截货船实录    未出片
外交部/印度回应      未出片
油价和停火走势       出片
```

从新闻价值看，`sv_001_hormuz_intercept` 反而应该是优先级最高的主片段，因为它包含最强现场画面和事件主体。现在它被音频规则挡掉，属于“高光误杀”。

### 2.2 当前系统把“原声建议”误当成“原声强需求”

模型在候选片段和剪辑脚本中会输出：

```json
"audio_mode": "mixed_evidence"
"original_audio_policy": "evidence_window"
"original_audio_type": "authority_quote"
```

当前系统把这些字段解释成：必须播放原声。

但用户在 Web 页面选择的音频策略是：

```text
AI 配音
```

在这个产品语义下，正确解释应当是：

```text
模型认为原声有参考价值；
但除非用户明确选择混合原声，否则不播放原声；
保留画面，使用 AI 配音转述即可。
```

### 2.3 当前质量规则过早阻断

当前这些问题会导致整条视频被 block：

```text
AI 配音和原声重叠
原声片段太短
原声占比过高
句间空白过长
compact 后视频低于计划时长
字幕和 TTS 时间不一致
```

其中前 3 个在 AI 配音模式下根本不应该成为问题：

```text
AI 配音模式下默认不播放原声；
不播放原声，就没有 AI/原声重叠；
不播放原声，也不需要检查原声片段时长和原声占比。
```

真正需要处理的是：

```text
AI 配音文案是否完整；
TTS 是否成功；
画面与 AI 配音是否匹配；
成片是否过短；
字幕是否同步；
事实和风控是否安全。
```

## 3. 优化目标

### 3.1 产品目标

1. AI 配音模式下，高光内容优先保留。
2. 原声默认关闭，不作为阻断项。
3. 模型建议保留原声时，默认降级为 AI 配音转述。
4. 只有用户明确选择混合原声/原声模式时，才启用原声证据窗口规则。
5. 对长视频，输出时长应由内容丰满度决定，不再被 30 秒强行牵引。
6. 质量规则从“过滤器优先”改成“修复/降级优先”。

### 3.2 行为目标

对于 22 分钟左右、包含多个高价值新闻点的素材，系统应优先输出：

```text
方案 A：多条短视频
- 2-3 条
- 每条 45-75 秒
- 总时长约 90-180 秒

方案 B：单条综合版
- 90-120 秒
- 事件 -> 回应 -> 影响 -> 后续走势
```

具体到本次样例，理想结果应是：

```text
sv_001_hormuz_intercept       55-65 秒，纯 AI 配音或少量可选原声
sv_002_diplomatic_response    45-55 秒，纯 AI 配音转述外交回应
sv_003_impact_outlook         45-55 秒，补足解读，不直接压到 36 秒
```

## 4. 当前问题链路分析

### 4.1 相关文件

当前核心逻辑分布在：

```text
newsclip_agent/duration_policy.py
newsclip_agent/pipeline.py
newsclip_agent/prompts.py
config.toml
tests/test_audio_policy.py
tests/test_voiceover_timeline_policy.py
```

### 4.2 当前关键函数

```text
duration_policy.py
- original_audio_volume_for_clip()
- evidence_audio_windows_from_clips()
- validate_voiceover_silence_for_evidence()
- validate_audio_policy()
- validate_voiceover_gaps()

pipeline.py
- step_tts()
- _requires_segment_aligned_voiceover()
- step_cut_plan()
- _compact_clips_to_voiceover_segments()
- _render_one()
- step_subtitles()
```

### 4.3 当前错误路径

以 AI 配音模式为例，当前实际路径是：

```text
1. 模型发现某个片段有原声证据价值
2. editing_script 输出 mixed_evidence / original_sound
3. original_audio_volume_for_clip() 给这个片段设置原声音量 > 0
4. evidence_audio_windows_from_clips() 生成证据原声窗口
5. _requires_segment_aligned_voiceover() 判定必须 segment_aligned
6. TTS 分段按镜头目标时间铺满
7. validate_voiceover_silence_for_evidence() 检测 AI 配音与原声窗口重叠
8. validate_audio_policy() 检测原声片段太短/原声占比问题
9. cut_plan 将整条视频标记为 blocked
10. render 跳过该短视频
```

问题点在第 3 步：用户选择 AI 配音时，系统不应该因为模型建议保留原声，就真的打开原声。

## 5. 新的设计原则

### 5.1 用户音频策略优先级最高

音频策略优先级：

```text
用户选择 > 系统配置 > 模型建议
```

如果用户选择：

```text
audio_policy = ai_voiceover
```

则默认行为必须是：

```text
不播放原声；
AI 配音为主音轨；
模型输出的 original_sound/mixed_evidence 只作为画面价值和文案参考；
不生成证据原声窗口；
不执行原声窗口重叠检查；
不执行原声最小时长检查；
不执行原声占比检查。
```

### 5.2 原声保留必须显式启用

只有以下情况才允许播放原声：

```text
audio_policy = mixed
audio_policy = original
或新增显式开关 allow_original_audio_evidence = true
```

如果未来需要 Web UI 支持，可以新增选项：

```text
音频策略：
- AI 配音（默认，不播放原声）
- AI 配音 + 证据原声
- 原声优先
```

### 5.3 高光片段不因原声问题被淘汰

在 AI 配音模式下：

```text
原声冲突 -> 不存在，因为原声默认关闭
原声太短 -> 不检查，因为不播放原声
原声占比过高 -> 不检查，因为原声占比为 0
```

如果模型输出了原声字段，应自动降级：

```text
audio_mode: mixed_evidence/original_sound -> ai_voiceover
keep_original_audio: false
original_audio_volume: 0
original_audio_policy: omit
```

同时将原声摘要转为 AI 文案素材：

```text
original_audio_transcript_summary -> voiceover must_keep_fact_points / narration context
```

## 6. 规则分层

### 6.1 仍然硬阻断的规则

这些问题必须 block：

```text
源视频不存在
时间码无效或无法切片
TTS 必须成功但完全失败
输出视频文件不存在
事实风控 high risk
画面和新闻事实严重不匹配
成片音视频时长差距巨大且无法自动修复
没有任何可用高光片段
```

### 6.2 AI 配音模式下不再阻断的规则

这些规则在 `audio_policy=ai_voiceover` 下应禁用：

```text
AI 配音和原声重叠
原声片段太短
原声占比过高
原声窗口不完整
原声价值分数不足
```

原因：

```text
AI 配音模式不播放原声，因此这些问题没有业务意义。
```

### 6.3 进入自动修复或降级的规则

这些问题不应直接淘汰视频：

```text
句间空白过长
compact 后视频低于计划时长
字幕和 TTS 时间不一致
AI 文案短于目标内容丰满度
镜头时长和 TTS 时长不匹配
```

修复策略：

```text
句间空白过长 -> compact 时间轴或补文案
compact 后过短 -> 补文案/补镜头/降级为警告
字幕不同步 -> 用 TTS segment 实际时间重写字幕
文案太短 -> 回到 voiceover_script 重写
镜头和 TTS 不匹配 -> 画面按 TTS 重排或扩充镜头
```

## 7. 具体代码修改方案

## 7.1 config.toml

### 7.1.1 新增配置

位置：

```text
config.toml
[voiceover]
```

建议新增：

```toml
# AI 配音模式下是否默认禁用原声。
ai_voiceover_disable_original_audio = true

# 是否允许模型建议的 mixed_evidence 在 AI 配音模式下自动打开原声。
# 默认 false，避免模型建议覆盖用户选择。
ai_voiceover_allow_model_original_audio = false

# 如果用户未来显式勾选“保留证据原声”，再设置为 true。
allow_original_audio_evidence = false

# compact 后成片不得低于计划目标时长比例。
# 例如计划 50 秒，compact 后低于 42.5 秒，则应补文案或补镜头。
compact_min_target_ratio = 0.85

# 长视频输出总时长建议。
long_source_min_total_output_seconds = 90
long_source_preferred_total_output_seconds = 150
long_source_min_video_count = 2

# 长视频判断阈值。
long_source_threshold_seconds = 600
```

### 7.1.2 保留已有配置

已有配置仍可保留：

```toml
compact_voiceover_default = true
segment_aligned_only_for_evidence = true
inter_sentence_gap_seconds = 0.28
max_inter_sentence_gap_seconds = 0.8
max_total_silence_ratio = 0.15
subtitle_follow_actual_tts = true
tts_actual_chars_per_second = 5.2
```

但它们应在新语义下工作：

```text
segment_aligned_only_for_evidence 只对 mixed/original 或显式保留原声生效；
AI 配音默认没有 evidence window。
```

## 7.2 duration_policy.py

位置：

```text
newsclip_agent/duration_policy.py
```

### 7.2.1 扩展 DurationSettings

新增字段：

```python
ai_voiceover_disable_original_audio: bool = True
ai_voiceover_allow_model_original_audio: bool = False
allow_original_audio_evidence: bool = False
compact_min_target_ratio: float = 0.85
long_source_threshold_seconds: float = 600.0
long_source_min_total_output_seconds: float = 90.0
long_source_preferred_total_output_seconds: float = 150.0
long_source_min_video_count: int = 2
```

并在 `load_duration_settings()` 中读取。

### 7.2.2 修改 original_audio_volume_for_clip()

当前函数会根据 `audio_mode=mixed_evidence/original_sound` 返回证据原声音量。

需要改成：

```python
def original_audio_volume_for_clip(
    clip: dict[str, Any],
    *,
    audio_policy: str,
    settings: DurationSettings | None = None,
    allow_original_audio_evidence: bool | None = None,
) -> float:
    settings = settings or DurationSettings()

    if audio_policy == "ai_voiceover":
        if settings.ai_voiceover_disable_original_audio and not (
            allow_original_audio_evidence or settings.allow_original_audio_evidence
        ):
            return 0.0

    if audio_policy == "original":
        return 1.0

    # mixed 模式才继续走原有 evidence/core_quote 判断。
```

核心语义：

```text
AI 配音模式下，模型输出 mixed_evidence 不再自动打开原声。
```

### 7.2.3 新增 normalize_audio_mode_for_policy()

建议新增纯函数：

```python
def normalize_audio_mode_for_policy(
    clip: dict[str, Any],
    *,
    audio_policy: str,
    settings: DurationSettings,
    allow_original_audio_evidence: bool = False,
) -> dict[str, Any]:
    item = dict(clip)
    if audio_policy == "ai_voiceover" and settings.ai_voiceover_disable_original_audio and not allow_original_audio_evidence:
        if item.get("audio_mode") in {"mixed_evidence", "original_sound"}:
            item["audio_mode_original"] = item.get("audio_mode")
            item["original_audio_policy_original"] = item.get("original_audio_policy")
            item["audio_mode"] = "ai_voiceover"
            item["keep_original_audio"] = False
            item["original_audio_volume"] = 0.0
            item["original_audio_policy"] = "omit"
            item["original_audio_downgraded_reason"] = "AI voiceover mode disables original audio by default"
    return item
```

这个函数用于在 `step_cut_plan()` 中统一规范 clip，避免多个位置散落判断。

### 7.2.4 修改 validate_audio_policy()

当前 `validate_audio_policy()` 会在 AI 配音模式下仍检查原声窗口。

需要加早退逻辑：

```python
original_audio_checks_enabled = not (
    audio_policy == "ai_voiceover_main"
    and settings.ai_voiceover_disable_original_audio
    and not settings.allow_original_audio_evidence
)

if original_audio_checks_enabled:
    # 原有 mixed_evidence/original_sound 检查
else:
    # 不检查原声最小时长、原声占比、AI/原声重叠
```

注意：`audio_policy` 在 cut_plan 输出里可能是：

```text
ai_voiceover_main
mixed_ai_voiceover
original_audio
```

因此判断需要兼容：

```python
audio_policy in {"ai_voiceover", "ai_voiceover_main"}
```

### 7.2.5 修改 evidence_audio_windows_from_clips()

建议不直接改函数签名，避免影响现有测试；而是在调用方控制。

新增函数：

```python
def should_build_evidence_audio_windows(
    *,
    audio_policy: str,
    settings: DurationSettings,
    allow_original_audio_evidence: bool = False,
) -> bool:
    if audio_policy in {"ai_voiceover", "ai_voiceover_main"}:
        return bool(allow_original_audio_evidence or settings.allow_original_audio_evidence)
    return audio_policy in {"mixed", "mixed_ai_voiceover", "original", "original_audio"}
```

## 7.3 pipeline.py

位置：

```text
newsclip_agent/pipeline.py
```

### 7.3.1 step_cut_plan() 中先降级原声

当前逻辑大致是：

```python
audio_mode = seg.get("audio_mode") or ...
original_volume = original_audio_volume_for_clip(...)
clips.append({...})
```

需要改成：

```python
raw_clip = {
    "source_start": ...,
    "source_end": ...,
    "target_start": ...,
    "duration_seconds": ...,
    "audio_mode": audio_mode,
    "original_audio_policy": seg.get("original_audio_policy", ""),
    "original_audio_type": seg.get("original_audio_type", ""),
    "original_audio_transcript_summary": seg.get("original_audio_transcript_summary", ""),
    ...
}

normalized = normalize_audio_mode_for_policy(
    raw_clip,
    audio_policy=self.options.audio_policy,
    settings=self.duration_settings,
    allow_original_audio_evidence=getattr(self.options, "allow_original_audio_evidence", False),
)

original_volume = original_audio_volume_for_clip(
    normalized,
    audio_policy=self.options.audio_policy,
    settings=self.duration_settings,
    allow_original_audio_evidence=getattr(self.options, "allow_original_audio_evidence", False),
)

normalized["original_audio_volume"] = original_volume
normalized["keep_original_audio"] = original_volume > 0
clips.append(normalized)
```

结果：

```text
AI 配音模式下，mixed_evidence/original_sound 自动降级为 ai_voiceover。
```

### 7.3.2 evidence_windows 生成加策略门

当前：

```python
evidence_windows = evidence_audio_windows_from_clips(clips)
```

改成：

```python
if should_build_evidence_audio_windows(
    audio_policy=self.options.audio_policy,
    settings=self.duration_settings,
    allow_original_audio_evidence=getattr(self.options, "allow_original_audio_evidence", False),
):
    evidence_windows = evidence_audio_windows_from_clips(clips)
else:
    evidence_windows = []
```

这样 AI 配音模式下不会生成原声窗口，也就不会触发：

```text
validate_voiceover_silence_for_evidence()
voiceover_original_overlap_seconds()
voiceover_timeline_required
```

### 7.3.3 _requires_segment_aligned_voiceover() 调整

当前函数会回看 editing_script，如果发现 `mixed_evidence/original_sound` 且原声音量 > 0，就返回 true。

需要新增第一优先级判断：

```python
if self.options.audio_policy == "ai_voiceover" and self.duration_settings.ai_voiceover_disable_original_audio:
    if not getattr(self.options, "allow_original_audio_evidence", False):
        return False
```

语义：

```text
AI 配音模式不因模型建议原声而进入 segment_aligned。
```

只有这些情况返回 true：

```text
audio_policy == mixed
audio_policy == original
allow_original_audio_evidence == true
```

### 7.3.4 step_tts() 的模式选择

当前已有模式：

```text
continuous
compact_segmented
segment_aligned
```

目标行为：

```text
AI 配音默认 -> compact_segmented
混合原声且存在证据窗口 -> segment_aligned
无分段文案 -> continuous
```

伪代码：

```python
if timeline_segments:
    if self._requires_segment_aligned_voiceover(item, editing_script):
        timeline_mode = "segment_aligned"
    else:
        timeline_mode = "compact_segmented"
else:
    timeline_mode = "continuous"
```

确保 AI 配音模式不会因为模型输出原声字段进入 `segment_aligned`。

### 7.3.5 step_cut_plan() 的 block 逻辑调整

当前：

```python
if evidence_windows:
    blocked_reasons.extend(validate_voiceover_silence_for_evidence(...))
```

改成：

```python
if evidence_windows and evidence_audio_checks_enabled:
    blocked_reasons.extend(validate_voiceover_silence_for_evidence(...))
```

同时：

```python
if tts_item.get("timeline_mode") == "segment_aligned":
    blocked_reasons.extend(validate_voiceover_gaps(...))
```

需要注意：

```text
AI 配音模式默认不应出现 segment_aligned；
如果出现，说明策略配置或流程异常，可记录 warning。
```

### 7.3.6 compact 后低于计划时长的处理

当前 `compact_segmented` 会按真实 TTS 压缩画面。例如计划 50 秒，最后 36.96 秒。

新增检查：

```python
target_duration = float(validation["target_duration_seconds"])
min_compact_duration = target_duration * self.duration_settings.compact_min_target_ratio

if (
    tts_item.get("timeline_mode") == "compact_segmented"
    and video_duration < min_compact_duration
):
    blocked_reasons.append(
        f"compact video duration {video_duration:.1f}s is below "
        f"{self.duration_settings.compact_min_target_ratio:.0%} of target {target_duration:.1f}s; "
        "rerun voiceover_script with richer narration or add supporting clips"
    )
```

但是这个不建议最终 hard block，而建议先实现为 `repair_required`：

```text
第一阶段：记录 warning，不阻断
第二阶段：实现 rerun voiceover_script 后，再改为自动修复
```

建议新增字段：

```json
"repair_reasons": [],
"warnings": []
```

而不是都塞进 `blocked_reasons`。

### 7.3.7 长视频输出总量检查

新增任务级检查，而不是单条视频检查。

在 `step_cut_plan()` 生成全部 `output_videos` 后，计算：

```python
source_duration = ffprobe source
renderable_videos = [v for v in output_videos if v["duration_status"] != "blocked"]
renderable_total_seconds = sum(v["video_duration_seconds"] for v in renderable_videos)
```

如果：

```text
source_duration >= long_source_threshold_seconds
renderable_total_seconds < long_source_min_total_output_seconds
```

不要直接失败，而是输出 warning：

```json
"coverage_warning": {
  "source_duration_seconds": 1320,
  "renderable_total_seconds": 36.96,
  "expected_min_total_seconds": 90,
  "suggestion": "rerun planning/voiceover with more output coverage"
}
```

后续可在 Web UI 显示：

```text
长视频覆盖不足：当前仅生成 36.96 秒，建议重跑规划或放宽输出数量。
```

## 7.4 prompts.py

位置：

```text
newsclip_agent/prompts.py
```

### 7.4.1 修改 AI 配音主导策略

在 `SHORT_VIDEO_DURATION_POLICY` 或 `VOICEOVER_TIMING_POLICY` 中明确：

```text
当 run_options.audio_policy == "ai_voiceover" 时：
1. 原声默认不进入成片。
2. 即使素材中存在权威讲话、军事通话、现场声，也默认由 AI 配音转述。
3. audio_mode 字段不得因为“原声有价值”而强制输出 original_sound/mixed_evidence。
4. 如果确有不可替代原声，只能在 original_audio_policy 中说明建议，但不得假设系统会播放。
5. 原声 transcript_summary 必须转化为 AI 解说素材。
```

### 7.4.2 修改 Editing Prompt

在 `EDITING_DIRECTOR_PROMPT` 中加入：

```text
如果 run_options.audio_policy == "ai_voiceover"：
- 默认所有镜头 audio_mode=ai_voiceover。
- 人物讲话/发布会/军事通话画面可以保留，但声音不保留。
- 原声内容应写入 original_audio_transcript_summary，供 AI 配音转述。
- 不得因为原声有价值而让整条视频依赖原声。
```

### 7.4.3 修改 Voiceover Prompt

在 `VOICEOVER_PROMPT` 中加入：

```text
如果某个 shot 原本含有人物讲话或原声摘要，但当前是 AI 配音模式：
- 不要输出“（原声播放）”这类文字。
- 直接用 AI 配音转述其核心含义。
- 原声窗口不需要留空。
```

本次样例中错误文本包括：

```text
（原声播放）指令清晰可辨。美方随即登船。
```

这类文案在 AI 配音模式下应改成：

```text
现场通话显示，美方在警告无效后升级行动，随后派出人员登船控制。
```

## 7.5 Web UI / RunOptions

### 7.5.1 新增用户可理解的音频选项

当前“AI 配音”容易和模型内部 `mixed_evidence` 冲突。建议 UI 改成：

```text
音轨策略：
1. AI 配音，不播放原声（推荐）
2. AI 配音 + 短证据原声
3. 原声优先
```

映射：

```text
AI 配音，不播放原声
audio_policy = ai_voiceover
allow_original_audio_evidence = false

AI 配音 + 短证据原声
audio_policy = mixed
allow_original_audio_evidence = true

原声优先
audio_policy = original
```

### 7.5.2 RunOptions 新增字段

位置：

```text
newsclip_agent/pipeline.py
RunOptions
web_app.py
web_static/app.js
```

新增：

```python
allow_original_audio_evidence: bool = False
```

前端请求体新增：

```js
allow_original_audio_evidence: $("allowOriginalAudioEvidence").checked
```

如果不做 UI，默认保持 false。

## 8. 测试方案

## 8.1 单元测试

### 8.1.1 AI 配音模式禁用原声

文件：

```text
tests/test_audio_policy.py
```

新增：

```python
def test_ai_voiceover_disables_model_requested_original_audio():
    volume = original_audio_volume_for_clip(
        {
            "audio_mode": "mixed_evidence",
            "original_audio_policy": "core_quote",
            "original_audio_value_score": 9,
        },
        audio_policy="ai_voiceover",
        settings=DurationSettings(
            ai_voiceover_disable_original_audio=True,
            allow_original_audio_evidence=False,
        ),
    )

    assert volume == 0.0
```

### 8.1.2 AI 配音模式不生成 evidence windows

新增：

```python
def test_ai_voiceover_does_not_build_evidence_windows_by_default():
    assert not should_build_evidence_audio_windows(
        audio_policy="ai_voiceover",
        settings=DurationSettings(ai_voiceover_disable_original_audio=True),
        allow_original_audio_evidence=False,
    )
```

### 8.1.3 mixed 模式仍生成 evidence windows

新增：

```python
def test_mixed_audio_builds_evidence_windows():
    assert should_build_evidence_audio_windows(
        audio_policy="mixed",
        settings=DurationSettings(),
        allow_original_audio_evidence=True,
    )
```

### 8.1.4 原声太短不阻断 AI 配音模式

新增：

```python
def test_short_original_audio_does_not_block_ai_voiceover_when_original_disabled():
    video = {
        "audio_policy": "ai_voiceover_main",
        "video_duration_seconds": 45,
        "voiceover_duration_seconds": 44,
        "voiceover": {"enabled": True},
        "clips": [
            {
                "audio_mode": "mixed_evidence",
                "duration_seconds": 5,
                "original_audio_volume": 0.0,
                "keep_original_audio": False,
                "original_audio_type": "authority_quote",
                "original_audio_policy": "core_quote",
                "original_audio_value_score": 9,
            }
        ],
    }

    result = validate_audio_policy(
        video,
        require_tts=True,
        settings=DurationSettings(ai_voiceover_disable_original_audio=True),
    )

    assert result["status"] == "ok"
```

### 8.1.5 AI/原声重叠不可能发生

新增：

```python
def test_ai_voiceover_overlap_check_skipped_when_original_disabled():
    video = {
        "audio_policy": "ai_voiceover_main",
        "video_duration_seconds": 45,
        "voiceover_duration_seconds": 44,
        "voiceover": {"enabled": True},
        "evidence_audio_windows": [],
        "voiceover_original_overlap_seconds": 0,
        "clips": [],
    }

    result = validate_audio_policy(
        video,
        require_tts=True,
        settings=DurationSettings(ai_voiceover_disable_original_audio=True),
    )

    assert result["status"] == "ok"
```

## 8.2 集成测试

### 8.2.1 本次样例回归

输入任务：

```text
outputs/美伊冲突与中东局势相关_20260527_0945
```

期望：

```text
sv_001_hormuz_intercept 不再因原声重叠被 skipped
sv_002_diplomatic_response 不再因原声太短被 skipped
render_outputs 至少包含 2 条视频
总成片时长 >= 90 秒，或给出 coverage_warning
```

### 8.2.2 AI 配音纯模式

运行参数：

```text
audio_policy=ai_voiceover
allow_original_audio_evidence=false
```

断言：

```text
所有 clips original_audio_volume == 0
evidence_audio_windows == []
voiceover_timeline_required == false
voiceover.timeline_mode in {"compact_segmented", "continuous"}
不出现 segment_aligned
```

### 8.2.3 混合原声模式

运行参数：

```text
audio_policy=mixed
allow_original_audio_evidence=true
```

断言：

```text
允许 evidence_audio_windows
允许 segment_aligned
如果 AI 与原声重叠，则 block 或自动静音 AI
原声片段太短仍可 block
```

## 9. 实施顺序

### 阶段一：修正 AI 配音模式语义

优先级最高。

修改文件：

```text
config.toml
newsclip_agent/duration_policy.py
newsclip_agent/pipeline.py
tests/test_audio_policy.py
tests/test_voiceover_timeline_policy.py
```

目标：

```text
AI 配音模式默认不播放原声；
模型建议原声不再触发 evidence window；
sv_001/sv_002 不再因原声问题被跳过。
```

### 阶段二：补充时长丰满度策略

修改文件：

```text
newsclip_agent/pipeline.py
newsclip_agent/prompts.py
tests/test_voiceover_duration_policy.py
```

目标：

```text
compact 后过短时，不直接接受；
优先补文案或补镜头；
长视频输出总时长不足时给 coverage_warning。
```

### 阶段三：Web UI 显式化音频策略

修改文件：

```text
web_static/index.html
web_static/app.js
web_app.py
newsclip_agent/pipeline.py
```

目标：

```text
用户能明确选择：
- AI 配音，不播放原声
- AI 配音 + 短证据原声
- 原声优先
```

### 阶段四：自动重写/修复流程

修改文件：

```text
newsclip_agent/pipeline.py
newsclip_agent/prompts.py
```

目标：

```text
当 compact 后低于目标比例，自动从 voiceover_script 重跑；
当规划覆盖不足，自动从 short_video_planning 重跑；
当字幕不同步，直接用 TTS segment 重写字幕。
```

## 10. 验收标准

### 10.1 行为验收

对于 `美伊冲突与中东局势相关_20260527_0945`：

```text
优化前：
规划 3 条，渲染 1 条，总时长 36.96 秒

优化后：
规划 3 条，至少渲染 2 条，理想渲染 3 条
总时长 >= 90 秒
AI 配音模式下 original_audio_volume 全部为 0
不因 AI/原声重叠阻断
不因原声太短阻断
```

### 10.2 文件验收

`cut_plan.json` 中：

```json
{
  "audio_policy": "ai_voiceover_main",
  "evidence_audio_windows": [],
  "voiceover_timeline_required": false,
  "voiceover_original_overlap_seconds": 0.0
}
```

`clips` 中：

```json
{
  "audio_mode": "ai_voiceover",
  "keep_original_audio": false,
  "original_audio_volume": 0.0,
  "original_audio_policy": "omit"
}
```

如果原片段原本是 mixed_evidence，应保留审计字段：

```json
{
  "audio_mode_original": "mixed_evidence",
  "original_audio_policy_original": "core_quote",
  "original_audio_downgraded_reason": "AI voiceover mode disables original audio by default"
}
```

### 10.3 测试验收

至少通过：

```text
pytest tests/test_audio_policy.py
pytest tests/test_voiceover_timeline_policy.py
pytest tests/test_voiceover_duration_policy.py
pytest tests/test_prompt_policy.py
```

全量测试中若仍存在既有配置断言，例如：

```text
test_doubao_config_integration expects text_llm_provider == "doubao"
```

需单独处理，不应与本需求混淆。

## 11. 风险与注意事项

### 11.1 不要完全删除原声能力

本需求不是移除原声，而是改变默认语义：

```text
AI 配音模式：默认不播放原声
混合模式：允许播放证据原声
原声模式：保留原片声音
```

### 11.2 不要让模型覆盖用户选择

模型输出的：

```text
must_keep_original_audio=true
original_audio_value_score=9
original_audio_policy=core_quote
```

只能作为建议，不能覆盖用户选择的 `audio_policy=ai_voiceover`。

### 11.3 不要把时长目标变成硬卡死

用户要求：

```text
视频时长不要严格卡死，要根据内容丰满度来。
```

因此：

```text
目标 50 秒不是必须精确 50 秒；
但 50 秒目标直接压成 36 秒，也应提示内容覆盖不足或触发补写。
```

建议采用范围：

```text
target_duration_seconds = 50
acceptable_min = 42.5
acceptable_max = 55
```

### 11.4 长视频要看总覆盖

单条视频 36 秒可能是合理的，但对 22 分钟长视频，只输出 36 秒通常不合理。

因此需要任务级覆盖检查：

```text
source_duration >= 10 分钟
renderable_total_seconds < 90 秒
=> coverage_warning 或触发重新规划
```

## 12. 最终结论

本次优化的核心不是取消质量规则，而是重新定义质量规则的职责：

```text
质量规则不应该误杀高光；
质量规则应该保护最终成片体验。
```

AI 配音模式下，正确策略是：

```text
默认不播放原声；
保留高光画面；
用 AI 配音完整转述；
音频问题不淘汰视频；
时长根据内容丰满度动态调整。
```

最终目标：

```text
高光内容优先保留；
原声只作为可选增强；
长视频输出有足够覆盖；
成片节奏自然、字幕同步、AI 配音完整。
```
