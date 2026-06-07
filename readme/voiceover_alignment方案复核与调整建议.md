# Voiceover alignment 报错与“后半段无配音/无字幕”优化方案复核与调整建议

## 一、结论

你让大模型给出的优化方案，**总体方向是对的，基本符合预期**。

它抓住了这次问题的核心：

```text
voiceover_script 生成的 narration_segments 太短，
但当前 repair 主要只处理 alignment 硬失败，
没有把“文案估算时长不足”纳入 repair 闭环，
所以最后直接在 voiceover_script 阶段抛出：
voiceover_script_segment_duration_too_short
```

同时，你实际看到的另一个现象：

```text
AI 配音只有 30 秒左右，后面继续播放原视频，没有 AI 字幕
```

不是单靠修 `voiceover_script_segment_duration_too_short` 就能彻底解决的。它还需要：

```text
1. TTS 前：文案估算时长不足时，先 repair，而不是直接失败。
2. TTS 前：不仅检查单个 shot，还要检查整条视频 narration_text 总时长。
3. TTS 后：检查真实音频总时长是否覆盖最终画面时长。
4. cut_plan / render 前：检查最终字幕和配音覆盖是否到视频末尾。
```

所以原方案的“四层闭环”方向是正确的，但我建议做几处调整，避免过度设计、重复校验和漏掉现有代码里已经有的机制。

---

## 二、当前代码现状核对

### 1. `run_web.py` 不是根因

`run_web.py` 主要做端口选择、ASR preflight 和启动 uvicorn，不参与 voiceover_script、TTS、字幕、render 的具体逻辑。

所以这次不需要优先改 `run_web.py`。

### 2. `config.toml` 已经有部分时长控制配置

当前配置里已经有：

```toml
[voiceover_script]
repair_enabled = true
max_repair_rounds = 2
repair_on_warnings = false

[voiceover]
voiceover_chars_per_second = 4.8
voiceover_min_chars_per_second = 3.8
voiceover_max_chars_per_second = 5.8
default_target_duration_seconds = 60
ai_voiceover_min_ratio = 0.80
duration_mismatch_block_threshold_seconds = 1.0
ai_voiceover_block_duration_mismatch = true
subtitle_follow_actual_tts = true
compact_min_target_ratio = 0.65
compact_severe_short_ratio = 0.65
```

这说明当前工程已经有“配音/画面时长约束”的基础，不是完全没有校验。问题在于这些校验没有完整接入 voiceover repair 闭环，且 TTS 后 / cut_plan 前的整体覆盖判断还不够强。

### 3. 当前 `_finalize_voiceover_script_with_repair()` 的确只在 alignment 不通过时 repair

当前流程是：

```text
normalize
sync narration_text
attach timings
validate alignment
如果 alignment.ok=false 且 repair_enabled=true，才 repair
最后再调用：
_validate_voiceover_segment_text_duration_or_raise()
_validate_voiceover_script_duration_or_raise()
_enforce_long_video_confirmation_after_voiceover()
```

这和原方案判断一致。

也就是说，当前问题会变成：

```text
alignment 只是 warning
↓
不触发 repair
↓
segment duration 硬校验发现太短
↓
直接失败
```

你的日志正是这种类型：

```text
Voiceover alignment warnings:
- shot_id=v_001_s01 chars=62 min_chars=66 under_chars=4 warning
- shot_id=v_001_s02 chars=39 min_chars=47 under_chars=8 warning

运行失败：voiceover_script_segment_duration_too_short
```

### 4. 当前 segment 文案时长校验是直接 raise，没有先 collect 给 repair

当前 `_validate_voiceover_segment_text_duration_or_raise()` 会按：

```text
estimated = 文案字数 / voiceover_chars_per_second
if estimated < target_duration_seconds * pre_tts_segment_min_ratio:
    raise voiceover_script_segment_duration_too_short
```

这就是当前失败的直接触发点。

所以原方案里建议拆成：

```text
_collect_voiceover_segment_duration_issues()
_validate_voiceover_segment_text_duration_or_raise()
```

是合理的。

### 5. 当前整条文案时长校验不够准确

当前 `_validate_voiceover_script_duration_or_raise()` 主要使用：

```text
script.target_duration_seconds
actual_chars
hard_min_chars = target * 2.5
soft_min_chars = target * 3.0
```

这里有一个问题：

```text
script.target_duration_seconds 未必等于 editing_structure 最终画面总时长。
```

如果最终画面已经被 editing_structure / cut_plan 扩到了 50-60 秒，但 voiceover_script 里 target 还是 30 秒，就会出现判断偏松。

所以原方案提出：

```text
整条目标时长优先用 editing_structure 的 shot.target_duration_seconds / duration_seconds 累加
```

这个建议是必要的。

### 6. 当前 TTS reconcile 已经有 segment 和 video 总时长字段，但视频级阈值不够强

当前 `_build_tts_duration_reconcile()` 已经会计算：

```text
target_total_duration_seconds
tts_total_duration_seconds
coverage_ratio
segments
```

但是当前主要还是以 segment 状态判断 `video.ok`。

潜在问题是：

```text
每个 segment 都只是 slightly_short，
video.ok 仍可能为 true，
但整条配音总时长可能只有最终画面的 0.75-0.85。
```

所以原方案里建议增加：

```text
tts_script_ok_min_ratio = 0.90
tts_script_hard_min_ratio = 0.75
```

方向是对的。

但这里不一定需要另写一套完全独立的 `_collect_tts_script_duration_issues()`，更稳的做法是直接增强现有 `_build_tts_duration_reconcile()`，在 video 级别增加：

```json
"total_check": {
  "status": "ok / slightly_short / too_short / too_long",
  "hard": true,
  "coverage_ratio": 0.5,
  "target_visual_duration_seconds": 60,
  "actual_tts_duration_seconds": 30
}
```

然后让 `_step_tts_impl()` 把 video 级 hard mismatch 纳入失败条件。

### 7. 当前 cut_plan 已经有 duration mismatch 阻断，但还缺“字幕覆盖”专门检查

当前 `step_cut_plan()` 会读取：

```text
editing_script
voiceover_script
tts
subtitles
```

并且在 AI 配音模式下会先调用：

```text
_validate_ai_voiceover_inputs_before_cut_plan(editing, voiceover_script, tts)
```

后面也会计算：

```text
video_duration
voiceover_duration
duration_delta
mismatch_reason
```

这说明“render 前防呆”已经有一部分基础。

但它主要看的是：

```text
画面时长 vs voiceover_duration
```

对“字幕文件实际最后一条字幕是否覆盖到末尾”还不够明确。

所以原方案里加字幕覆盖校验是合理的，只是我建议它优先放在：

```text
cut_plan 生成阶段 / render 前置检查
```

而不是只放在 `_step_render_impl()` 里。因为如果等到 render 后才发现，代价更高，也更难让用户知道应该从哪一步重跑。

---

## 三、对原优化方案的判断

### 1. 能否解决当前报错？

**可以，但前提是要按方案真正把 segment duration issue 合并进 repair。**

也就是说，只新增配置不够，必须改 `_finalize_voiceover_script_with_repair()`。

当前报错：

```text
voiceover_script_segment_duration_too_short
```

应该从：

```text
直接 raise
```

改成：

```text
先 collect issue
↓
合并到 alignment check
↓
触发 voiceover repair
↓
repair 后重新校验
↓
仍不通过才 raise
```

这样才能解决你现在“略短/明显短直接失败”的问题。

### 2. 能否解决“AI 配音只有 30 秒，后面没字幕”？

**可以解决大部分，但需要补强两个地方：**

```text
1. TTS 后整条真实音频覆盖率必须进入 hard gate。
2. subtitle 输出后的实际覆盖结束时间也要进入 cut_plan/render 前检查。
```

否则可能出现：

```text
TTS 前估算通过
↓
TTS 实际生成偏短
↓
cut_plan 或 render 仍继续
↓
最终视频后半段无配音/无字幕
```

所以原方案说的 TTS 后、render 前检查不能省。

### 3. 原方案有没有过度设计？

有一点。

原方案把很多问题都拆成新函数、新 issue、新配置，方向没错，但第一版不建议一次性做得太复杂。

更推荐分成三层落地：

```text
第一层：最小修复当前报错。
第二层：增强 TTS 后整条覆盖率阻断。
第三层：增强字幕/配音最终覆盖校验。
```

不要第一版就大面积重构所有 duration issue 数据结构，否则容易引入新 bug。

---

## 四、建议保留的部分

### 1. 保留：segment duration issue 进入 repair

这是当前报错的核心修复。

建议新增：

```text
_collect_voiceover_segment_duration_issues(voiceover_output)
```

它只收集问题，不 raise。

每个 issue 至少包含：

```json
{
  "short_video_id": "v_001",
  "shot_id": "v_001_s02",
  "type": "segment_duration_too_short",
  "source": "pre_tts_segment_duration",
  "target_duration_seconds": 12.0,
  "estimated_text_duration_seconds": 8.1,
  "required_estimated_seconds": 7.8,
  "chars": 39,
  "suggested_add_chars": 8,
  "severity": "hard"
}
```

注意：

```text
这里的 severity 不要完全复用 alignment warning / hard 的概念。
```

因为“文案比 min_chars 少 4 个字”可能只是 warning，但“估算配音时长明显短于 shot 画面时长”就应该触发 repair。

### 2. 保留：整条 narration_text 估算时长检查

这个也应该保留。

原因是有些场景下：

```text
每段都勉强不算太短，
但整条总文案明显短。
```

如果只看 segment，会漏掉整条 60 秒视频只有 30 秒配音的问题。

建议新增：

```text
_collect_voiceover_script_duration_issues(voiceover_output, edit_plan)
```

目标时长优先级建议调整为：

```text
1. edit_script.visual_total_seconds
2. editing_structure_duration(edit_script.editing_structure)
3. sum(shot.target_duration_seconds / duration_seconds)
4. edit_script.estimated_total_duration_seconds
5. voice_script.target_duration_seconds
6. options.target_duration_seconds
7. voiceover.default_target_duration_seconds
```

原方案里第 1 优先级写的是 `editing_structure 内所有 shot.target_duration_seconds 之和`，这个也可以，但当前代码里已经有 `editing_structure_duration()`，优先复用现有函数更稳。

### 3. 保留：repair 后重新 normalize / sync / attach / validate

这个必须保留。

每轮 repair 后都要重新执行：

```text
_normalize_voiceover_scripts()
_sync_voiceover_narration_text_from_segments()
_attach_voiceover_script_timings()
_validate_voiceover_script_against_editing_structure()
_collect_voiceover_segment_duration_issues()
_collect_voiceover_script_duration_issues()
```

否则 repair 后的字数、时长、alignment 都可能是旧数据。

### 4. 保留：TTS 后整条真实音频时长检查

这个应该基于当前已有的 `_build_tts_duration_reconcile()` 增强，而不是完全另起一套。

建议增加配置：

```toml
[voiceover]
tts_script_ok_min_ratio = 0.90
tts_script_hard_min_ratio = 0.75
tts_script_ok_max_ratio = 1.15
tts_script_hard_max_ratio = 1.40
```

判断逻辑：

```text
coverage_ratio = tts_total_duration_seconds / target_total_duration_seconds

coverage_ratio >= 0.90：ok
0.75 <= coverage_ratio < 0.90：slightly_short，可配置为失败或警告
coverage_ratio < 0.75：hard too_short，必须失败
```

在 AI 配音生产模式下，我建议：

```text
低于 0.90 就阻断，至少第一版先严格。
```

因为你现在最不能接受的是“生成了一个坏视频”。

### 5. 保留：字幕覆盖检查

这个也应该保留。

当前字幕生成逻辑是：

```text
优先用 tts_segments 生成 SRT
否则用 narration_segments
否则用整条 script 切字幕
```

所以最终字幕不一定天然覆盖到画面末尾。

建议新增：

```text
_validate_ai_voiceover_coverage_before_render(cut_plan, tts, subtitles)
```

检查：

```text
visual_total_duration
voiceover_cover_end
subtitle_cover_end
```

阈值：

```toml
[voiceover]
voiceover_cover_min_ratio = 0.90
subtitle_cover_min_ratio = 0.90
```

---

## 五、建议调整的部分

### 调整 1：不要把 `repair_on_warnings = true` 当主方案

原方案这个判断是对的。

不建议简单打开：

```toml
repair_on_warnings = true
```

原因：

```text
alignment warning 可能只是少 3-5 个字，
如果全部触发 repair，会增加耗时，也可能让模型反复改写本来可用的文案。
```

更推荐：

```text
普通 char_budget_warnings 仍然不 repair。
pre_tts_segment_duration / pre_tts_script_duration 这类时长 issue 单独触发 repair。
```

也就是 `_check_is_text_patch_repairable()` 增加判断：

```text
只要 char_budget_issues 中存在 source 为：
- pre_tts_segment_duration
- pre_tts_script_duration

就允许 repair。
```

### 调整 2：`issue_map` 不要只存一个 type

原方案指出的问题正确。

当前 repair input 里：

```python
issue_map[shot_id] = issue.get("type", "unknown")
```

这个会丢掉：

```text
estimated_text_duration_seconds
required_estimated_seconds
suggested_add_chars
script_target_duration_seconds
script_estimated_duration_seconds
```

建议改成：

```text
issue_map[shot_id] = [issue1, issue2, ...]
```

然后 `problem_segments` 里带完整 `issues`。

但第一版不必做得特别复杂，只要保证 repair prompt 能看到：

```text
这个 shot 现在估算几秒
目标至少几秒
建议补多少字
不能新增事实
```

即可。

### 调整 3：script duration issue 分摊不要太复杂

原方案提出按 shot 时长比例分摊缺口，方向正确。

但第一版可以更简单：

```text
1. 如果已有 segment_duration_too_short，就优先修这些段。
2. 如果没有，就把所有 narration_segments 都作为可扩写对象。
3. suggested_add_chars 按 target_duration_seconds 比例分配。
4. 每段最小补 6-8 字，最大不要超过 shot.narration_max_chars。
```

不要一开始就引入太多权重规则，避免模型输出不稳定。

### 调整 4：TTS 后检查不要重复发明一套，直接增强 reconcile

当前 `_build_tts_duration_reconcile()` 已经有：

```text
videos[].target_total_duration_seconds
videos[].tts_total_duration_seconds
videos[].coverage_ratio
videos[].segments
```

所以建议直接扩展：

```text
videos[].total_check
```

再新增：

```text
_tts_reconcile_hard_videos()
```

然后在 `_step_tts_impl()` 的失败判断中加入：

```text
elif tts_is_hard_required and reconcile_hard_videos:
    overall_status = "failed"
    hard_error = "TTS duration has hard mismatch videos"
```

这样改动最小，也最贴合当前代码。

### 调整 5：render 前覆盖检查更建议放到 cut_plan 阶段

原方案说放 render 前是对的，但实现位置建议优先放在：

```text
step_cut_plan() 生成 output_videos 之前 / 之后
```

原因：

```text
cut_plan 已经同时拿到了 editing、voiceover_script、tts、subtitles。
这里最适合判断最终画面、配音、字幕是否覆盖一致。
```

`_step_render_impl()` 也可以保留一个兜底检查，但不建议把主要阻断逻辑只放 render。

---

## 六、建议最终落地方案

### 第一阶段：先解决当前报错

只改：

```text
newsclip_agent/pipeline.py
config.toml
```

核心改动：

```text
1. 新增 _collect_voiceover_segment_duration_issues()
2. 新增 _collect_voiceover_script_duration_issues()
3. 新增 _merge_voiceover_duration_issues_into_alignment()
4. 修改 _finalize_voiceover_script_with_repair()
5. 修改 _build_voiceover_repair_text_input()
6. 修改 _check_is_text_patch_repairable()
```

目标：

```text
voiceover_script_segment_duration_too_short 不再直接失败，
而是先进入 voiceover repair。
```

失败条件改为：

```text
repair 两轮后仍明显不足，才失败。
```

### 第二阶段：防止 TTS 实际音频只有 30 秒

改：

```text
_build_tts_duration_reconcile()
_tts_reconcile_hard_videos()
_step_tts_impl()
```

目标：

```text
TTS 真实音频总时长明显短于最终画面时长时，直接在 TTS 阶段失败。
```

不要进入 cut_plan / render。

### 第三阶段：防止字幕/配音覆盖不到末尾

改：

```text
step_cut_plan()
必要时补 _validate_ai_voiceover_coverage_before_render()
```

目标：

```text
最终画面 60 秒，字幕只到 30 秒，直接阻断。
```

用户提示应该明确：

```text
AI 配音/字幕覆盖范围明显短于最终视频，建议从 voiceover_script 或 tts 重跑。
```

---

## 七、推荐配置调整

建议在 `[voiceover_script]` 下新增：

```toml
# 是否把 TTS 前分段/整条时长不足纳入 voiceover_script repair
repair_on_segment_duration_issues = true
repair_on_script_duration_issues = true

# TTS 前整条文案估算时长阈值
script_estimated_soft_min_ratio = 0.85
script_estimated_hard_min_ratio = 0.65
```

建议在 `[voiceover]` 下新增：

```toml
# TTS 后整条真实配音时长阈值
tts_script_ok_min_ratio = 0.90
tts_script_hard_min_ratio = 0.75
tts_script_ok_max_ratio = 1.15
tts_script_hard_max_ratio = 1.40

# cut_plan / render 前覆盖阈值
voiceover_cover_min_ratio = 0.90
subtitle_cover_min_ratio = 0.90
```

保留：

```toml
repair_on_warnings = false
max_repair_rounds = 2
pre_tts_segment_min_ratio = 0.65
```

如果你当前 `config.toml` 里没有显式写：

```toml
pre_tts_segment_min_ratio = 0.65
```

建议补上，避免依赖代码默认值。

---

## 八、最终修改后的预期行为

### 当前失败用例

现在：

```text
v_001_s01 少 4 字 warning
v_001_s02 少 8 字 warning
后续 segment duration 校验失败
直接抛 voiceover_script_segment_duration_too_short
```

改完后：

```text
alignment warning 保留
segment duration issue 被 collect
合并进 repair check
触发 voiceover repair
模型补写 v_001_s01 / v_001_s02
重新 sync narration_text
重新 attach timings
重新校验
通过后进入 TTS
```

### TTS 后仍然只有 30 秒的情况

改完后：

```text
目标画面 60 秒
TTS 实际 30 秒
coverage_ratio = 0.50
低于 hard_min_ratio 0.75
TTS 阶段失败
不会 render 坏视频
```

### 字幕只到前半段的情况

改完后：

```text
目标画面 60 秒
subtitle_cover_end = 31 秒
subtitle_cover_ratio = 0.52
低于 subtitle_cover_min_ratio 0.90
cut_plan / render 前失败
不会输出后半段无字幕的视频
```

---

## 九、还需要特别注意的点

### 1. 不要让模型为了凑时长胡编

repair prompt 必须强调：

```text
只能基于已有事实扩写。
不能新增画面没有出现的信息。
不能新增事实。
不能改变 shot_id。
不能新增/删除 narration_segments。
不能改变顺序。
保持新闻口吻。
一句话适合一行字幕。
```

否则它可能靠废话或编造事实补字数。

### 2. 不要靠拉伸 30 秒音频解决 60 秒画面

轻微 atempo 可以用于 5%-8% 的误差。

但类似：

```text
画面 60 秒
配音 30 秒
```

不能靠音频拉伸解决，必须回到：

```text
voiceover_script repair
或 short_video_edit_plan 缩短画面
```

### 3. 原声模式不能被误伤

这些硬校验只应该作用于：

```text
production_mode = ai_voiceover
且 audio_policy != original
且 require_tts = true
```

不要影响原声高光重组模式。

### 4. 多条短视频要逐条校验

不能只算全局总时长。

必须按：

```text
short_video_id
```

分别校验。

否则可能出现：

```text
v_001 正常
v_002 配音严重不足
全局平均看起来还行
```

---

## 十、最终建议结论

这份优化方案**可以作为开发依据**，但建议按下面这个版本收敛：

```text
1. 保留“四层闭环”的总体设计。
2. 第一版优先修 TTS 前 repair 闭环，解决当前 voiceover_script_segment_duration_too_short。
3. TTS 后不要另起复杂结构，直接增强现有 _build_tts_duration_reconcile()。
4. 字幕/配音覆盖检查优先放在 cut_plan 阶段，render 阶段只做兜底。
5. 不要打开 repair_on_warnings 作为主方案，而是让 duration issue 单独触发 repair。
6. 不要改 run_web.py、web_app.py、run_pipeline.py。
7. prompts.py 可小改 VOICEOVER_REPAIR_TEXT_PROMPT，强化“时长修复但不编造事实”。
```

最终最小必改范围：

```text
newsclip_agent/pipeline.py
config.toml
```

可选小改：

```text
newsclip_agent/prompts.py
```

不建议改：

```text
run_web.py
web_app.py
run_pipeline.py
```

这样改后，既能解决你这次的报错，也能避免后续继续生成“前半段有 AI 配音和字幕，后半段没配音没字幕”的坏视频。
