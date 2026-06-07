# Voiceover alignment 优化方案（微调补充版）

下面这份按 **“详细优化方案 + 代码开发指南”** 写。重点是解决你现在这个问题：

```text
视频实际画面比较长，但 AI 配音文案 / TTS / 字幕只覆盖前半段，
后半段继续播放原视频，导致没有解说、没有字幕。
```

---

> 复核补充说明：以下是在原方案基础上的微调补充，不删减原有代码开发细节。原方案主线是对的；本次只补充当前仓库代码里需要特别注意的落点、优先级和边界。

# 一、错误原因

## 1. 当前失败点不是 TTS，也不是 render

你这次日志里的核心错误是：

```text
voiceover_script_segment_duration_too_short
```

它发生在 **voiceover_script 生成之后、TTS 之前**。

当前代码链路是：

```text
voiceover_script 生成
↓
_normalize_voiceover_scripts()
↓
_sync_voiceover_narration_text_from_segments()
↓
_attach_voiceover_script_timings()
↓
_validate_voiceover_script_against_editing_structure()
↓
如果 alignment 不通过，才进入 repair
↓
_validate_voiceover_segment_text_duration_or_raise()
↓
_validate_voiceover_script_duration_or_raise()
↓
_enforce_long_video_confirmation_after_voiceover()
```

实际代码里 `_finalize_voiceover_script_with_repair()` 确实是在 alignment repair 之后，才调用 segment duration 和 script duration 的最终硬校验。也就是说，当前 repair 主要服务于 alignment，而不是完整的时长闭环。

> 微调补充：这个判断符合当前代码。当前 `_finalize_voiceover_script_with_repair()` 的 repair 触发点确实只看 `alignment.ok`，而 `_validate_voiceover_segment_text_duration_or_raise()` 和 `_validate_voiceover_script_duration_or_raise()` 在 repair 之后才执行。因此当前报错不是 TTS 生成失败，而是文案时长硬校验没有进入 repair 闭环。

---

## 2. 当前 repair 只在 alignment 硬失败时触发

现在 repair 触发条件是：

```text
if not alignment.ok and repair_enabled
```

而你日志里那种：

```text
Voiceover alignment warnings:
- shot 文案略短
```

很多时候只是 warning，不一定会让 `alignment.ok = false`。

当前代码里，低于 `narration_min_chars` 会分成 warning 或 hard issue。warning 会打印出来，但不会让 alignment 失败；只有 `char_budget_issues` 才会让 `ok = false`。

所以现在会出现这种情况：

```text
alignment 没硬失败
↓
不进入 repair
↓
后面的 segment duration 校验发现明显太短
↓
直接 raise
```

这就是这次 `voiceover_script_segment_duration_too_short` 的根因之一。

> 微调补充：这里不建议简单把 `repair_on_warnings = true` 当成主方案。因为普通字数 warning 可能只是轻微偏差，全部触发 repair 会增加耗时，也可能让模型无必要改写。更稳的做法是：只把 `pre_tts_segment_duration`、`pre_tts_script_duration` 这类会影响成片覆盖的问题标记为 repairable。

---

## 3. 当前只校验“每段”，没有完整闭环校验“整条视频”

你现在看到的问题不是单纯某一段短，而是：

```text
AI 配音只有 30 秒左右
后面都是原视频
没有字幕
```

这说明系统缺少完整的四层校验：

```text
1. 每个 shot 文案估算时长是否够
2. 整条文案估算时长是否够
3. TTS 真实音频时长是否够
4. render 前字幕/配音是否覆盖到视频末尾
```

当前代码已有一部分基础：

* editing_structure 会生成每段的 `target_duration_seconds`、`target_start_seconds`、`target_end_seconds`。
* TTS 会按 narration_segments 生成 segment audio，并记录每段 `target_duration_seconds`、`actual_duration_seconds`。
* 字幕会优先跟随 TTS segments，如果没有再退回 narration_segments。
* TTS 后已有 `_build_tts_duration_reconcile()` 做 segment 级对齐检查。

但是缺口是：**这些校验还没有形成“整条视频级别的闭环阻断”**。

> 微调补充：当前 `_build_tts_duration_reconcile()` 已经有 `target_total_duration_seconds`、`tts_total_duration_seconds`、`coverage_ratio` 这类整条统计雏形，但 TTS 阶段的失败判断主要还是基于 segment 状态和 `reconcile.ok`。所以这里不是完全从零新增一套 TTS 校验，而是要把已有 video-level 汇总升级成真正参与阻断的 `total_check / hard_video` 判断。

---

# 二、优化目标

最终要达到这个效果：

```text
AI 配音模式下，最终画面多长，AI 配音和字幕就至少要覆盖到接近这个长度。

不能出现：
画面 60 秒
配音 30 秒
字幕 30 秒
后面继续播放原视频
```

具体目标分四层：

```text
第一层：TTS 前，单个 shot 文案不能明显太短。
第二层：TTS 前，整条 narration_text 总估算时长不能明显太短。
第三层：TTS 后，真实音频总时长不能明显短于画面总时长。
第四层：render 前，字幕/配音覆盖范围不能明显短于视频总时长。
```

> 微调补充：第四层更建议前置到 `cut_plan` 生成阶段或 `render` 前的专门 gate。因为 `cut_plan` 阶段已经同时拿到了 editing、tts、subtitles，并且会计算每条 output_video 的 clips、voiceover、subtitles。这样可以避免进入 `_render_one()` 后才发现覆盖不足。

---

# 三、推荐改动范围

## 必改文件

```text
newsclip_agent/pipeline.py
config.toml
```

## 可选改动文件

```text
newsclip_agent/prompts.py
```

如果现有 `VOICEOVER_REPAIR_TEXT_PROMPT` 已经足够约束“不改 shot_id、不新增事实、只修文案”，可以不改 prompt。但我建议加一点“时长修复”的明确描述。

## 不建议改的文件

```text
run_web.py
web_app.py
run_pipeline.py
```

原因是这次问题发生在 pipeline 内部时长校验和 repair 逻辑里。Web 入口只是把参数传进去，不是根因。`run_web.py` 只负责加载配置并启动服务；`web_app.py` 负责拼接 CLI 参数；`run_pipeline.py` 只是调用 `pipeline.main()`。

> 微调补充：这个改动范围是合理的。当前 `run_web.py` 只做端口、预检和 uvicorn 启动，不是这次时长不匹配的根因；问题主要集中在 `pipeline.py` 的 voiceover repair、TTS reconcile、cut_plan/render 前校验。

---

# 四、核心设计

## 总体链路

改完后，AI 配音模式应该变成：

```text
voiceover_script 生成
↓
normalize / sync / attach timings
↓
alignment 校验
↓
segment 文案估算时长校验
↓
script 整条文案估算时长校验
↓
把 alignment issue + segment duration issue + script duration issue 合并成 repair issue
↓
进入 voiceover_script repair
↓
repair 后重新 normalize / sync / attach timings
↓
重新校验三类问题
↓
全部通过后才进入 TTS
↓
TTS 生成真实音频
↓
segment TTS 时长校验
↓
script TTS 总时长校验
↓
字幕/配音覆盖校验
↓
render
```

关键原则是：

```text
TTS 前：发现文案不够，优先 repair 文案。
TTS 后：发现真实音频不够，不能继续静默 render。
render 前：发现覆盖不到末尾，必须阻断。
```

> 微调补充：这里的“render 前”建议不要只理解为 `_step_render_impl()` 内部。更好的落点是：`step_cut_plan()` 中在 `output_videos` 写出前完成覆盖校验；`_step_render_impl()` 再保留最终兜底检查。

---

# 五、第一部分：TTS 前整条文案时长校验

## 1. 新增“收集问题”函数，不要一上来 raise

当前代码里有最终硬校验函数：

```text
_validate_voiceover_segment_text_duration_or_raise()
_validate_voiceover_script_duration_or_raise()
```

建议拆成两层：

```text
_collect_voiceover_segment_duration_issues()
_collect_voiceover_script_duration_issues()

_validate_voiceover_segment_text_duration_or_raise()
_validate_voiceover_script_duration_or_raise()
```

其中 collect 函数只返回 issues，不抛异常。

这样可以把问题先交给 repair，而不是直接失败。

> 微调补充：这是本方案最关键的落地点。不要删除现有 `*_or_raise()`，而是新增 collect 层，然后让 `_finalize_voiceover_script_with_repair()` 先 collect、merge、repair，最后再调用原有 hard validate 兜底。这样改动最小，也保留了失败保护。

---

## 2. 整条文案目标时长怎么取

不能只用用户传入的：

```text
--target-duration 30
```

因为最终画面可能已经被 editing_structure 扩到了 60 秒。

整条视频目标时长建议按这个优先级：

```text
1. editing_structure 内所有 shot.target_duration_seconds 之和
2. editing_script.estimated_total_duration_seconds
3. voiceover_script.target_duration_seconds
4. options.target_duration_seconds
5. voiceover.default_target_duration_seconds
```

最重要的是第 1 个：

```text
editing_structure 的实际目标时长
```

因为后面 cut_plan 就是按 editing_structure 生成 clips，里面会累计 `target_start_seconds` / `target_end_seconds`，最终画面总时长就是它决定的。

> 微调补充：当前 `_attach_voiceover_segment_timing()` 代码里优先读取的是 `shot.duration_seconds`，然后才用 `source_start/source_end` 计算；如果 editing_structure 实际使用的是 `target_duration_seconds`，这里会导致 segment 上挂的目标时长不准。建议同步把 `_attach_voiceover_segment_timing()` 调整为优先使用 `target_duration_seconds`，再退回 `duration_seconds` / source range。否则后面的 segment duration collect 可能拿到错误目标。

---

## 3. 整条文案估算公式

```text
文案估算配音时长 = narration_text 去空白字数 / voiceover_chars_per_second
```

当前配置里已经有：

```toml
[voiceover]
voiceover_chars_per_second = 4.8
pre_tts_segment_min_ratio = 0.65
```

也有最小/最大语速预算：

```toml
voiceover_min_chars_per_second = 3.8
voiceover_max_chars_per_second = 5.8
```

这些字段当前已经存在。

---

## 4. 推荐新增配置

在 `[voiceover_script]` 下面加：

```toml
# 是否把 TTS 前整条/分段时长问题纳入 voiceover_script repair
repair_on_segment_duration_issues = true
repair_on_script_duration_issues = true

# TTS 前整条文案估算时长阈值
script_estimated_soft_min_ratio = 0.85
script_estimated_hard_min_ratio = 0.65
```

在 `[voiceover]` 下面加：

```toml
# TTS 后整条真实配音时长阈值
tts_script_ok_min_ratio = 0.90
tts_script_hard_min_ratio = 0.75
tts_script_ok_max_ratio = 1.15
tts_script_hard_max_ratio = 1.40

# render 前覆盖阈值
voiceover_cover_min_ratio = 0.90
subtitle_cover_min_ratio = 0.90
```

不要把：

```toml
repair_on_warnings = true
```

作为主方案。

> 微调补充：新增配置时要注意读取路径。如果只在 `pipeline.py` 里通过 `self.config.raw.get("voiceover", {})` 读取，可以不改 `duration_policy.py`；如果希望通过 `self.duration_settings.xxx` 读取，就必须同步扩展 `duration_policy.DurationSettings` 和 `load_duration_settings()`。为了减少改动，第一版建议新增阈值优先用 `self.config.raw` 读取。

当前配置里 `repair_on_warnings = false` 是合理的，因为打开所有 warning repair 会增加耗时，也可能导致文案被模型反复改写。

---

# 六、第二部分：把整条时长 issue 合并进 repair

## 1. 现有 repair 输入结构不够

当前 `_build_voiceover_repair_text_input()` 会把 `char_budget_issues` 放进去，并构造 `problem_segments`。但它现在的 `issue_map` 只保存了：

```python
issue_map[shot_id] = issue.type
```

这会丢掉很多信息，比如：

```text
目标时长
当前估算时长
缺多少秒
缺多少字
是 segment 短，还是整条 script 短
```

当前代码确实是只把 issue type 塞进 `issue_map`，再给 problem_segments 生成简单字段。

所以要改成：

```text
issue_map[shot_id] = [issue1, issue2, ...]
```

然后 problem_segments 里带完整 issue 信息。

> 微调补充：这个调整需要保留原有 `too_short/too_long/empty` 信息，不要只替换成新的 duration issue。建议 problem_segments 里同时保留 `issue_type` 和 `issues[]`，兼容现有 prompt，同时给模型更多修复依据。

---

## 2. segment duration issue 怎么合并

新增 collect 后，每个 segment issue 应该长这样：

```json
{
  "short_video_id": "v_001",
  "shot_id": "v_001_s02",
  "type": "segment_duration_too_short",
  "source": "pre_tts_segment_duration",
  "target_duration_seconds": 12.0,
  "estimated_text_duration_seconds": 8.1,
  "required_estimated_seconds": 10.2,
  "chars": 39,
  "required_chars": 49,
  "under_chars": 10,
  "severity": "hard"
}
```

然后合并到对应 alignment check 的：

```text
char_budget_issues
```

这样现有 repair 循环不用大改，因为它已经会遍历 `alignment.checks`，并把 `char_budget_issues` 当成可修复问题。

---

## 3. script duration issue 怎么合并

整条文案 issue 没有天然的 `shot_id`，但 repair 是按 `shot_id` 修 segment，所以需要把整条缺口分摊到 segments。

逻辑如下：

```text
如果整条文案估算太短：
1. 优先找已经过短的 segment。
2. 如果没有明显过短 segment，就把全部 narration_segments 都列为可扩写对象。
3. 按每个 shot.target_duration_seconds 分摊需要补的字数。
```

比如：

```text
目标画面：60 秒
当前文案估算：42 秒
缺口：18 秒
语速：4.8 字/秒
缺口字数：约 86 字
```

如果三个 shot 的目标时长是：

```text
shot1 = 30 秒
shot2 = 20 秒
shot3 = 10 秒
```

那分摊比例是：

```text
shot1 补 43 字左右
shot2 补 29 字左右
shot3 补 14 字左右
```

合并后的 issue 可以类似：

```json
{
  "short_video_id": "v_001",
  "shot_id": "v_001_s01",
  "type": "script_duration_too_short",
  "source": "pre_tts_script_duration",
  "script_target_duration_seconds": 60,
  "script_estimated_duration_seconds": 42,
  "script_required_duration_seconds": 51,
  "script_missing_chars": 86,
  "suggested_add_chars": 43,
  "severity": "warning_or_hard"
}
```

注意：**不要直接让模型改 `narration_text`**。

应该让模型改：

```text
narration_segments[].text
```

然后继续调用：

```text
_sync_voiceover_narration_text_from_segments()
```

重新生成整条 `narration_text`。

> 微调补充：分摊整条缺口时要加一个上限保护，避免某个 shot 被补得超过 `narration_max_chars` 太多。建议分摊时参考每个 shot 的 `narration_target_chars / narration_max_chars`，如果某段已经接近 max，就把缺口分摊给其他段，最后仍不够再失败。

---

# 七、第三部分：修改 `_finalize_voiceover_script_with_repair()`

## 当前问题

现在 `_finalize_voiceover_script_with_repair()` 的结构是：

```text
先 alignment
如果 alignment 不通过才 repair
最后再做 segment duration / script duration 硬校验
```

这会导致：

```text
时长问题没有机会 repair
直接失败
```

---

## 新结构建议

改成：

```text
1. normalize
2. sync narration_text
3. attach timings
4. alignment check
5. collect segment duration issues
6. collect script duration issues
7. merge issues into alignment
8. 如果 merged_alignment 不 ok，进入 repair
9. repair 每一轮后，重新执行 1-7
10. 最后仍然不 ok，再 raise
11. 最终再跑原来的 hard validate 兜底
```

核心变化是：

```text
不要只看 alignment.ok
要看 merged_alignment.ok
```

也就是：

```text
alignment 原始结构问题
+ segment 文案时长问题
+ script 总文案时长问题
```

一起决定是否 repair。

> 微调补充：这里的 merged_alignment 不一定要改变 `_validate_voiceover_script_against_editing_structure()` 的原始返回语义。可以新增一个 `_build_voiceover_repair_alignment()` 或 `_merge_voiceover_duration_issues_into_alignment()`，输出一个专门用于 repair 的 alignment。最终硬校验仍然调用原始 alignment + duration validate。这样风险更小。

---

## repair 循环里也要重新收集

不能只在第一轮前收集一次。

每轮 repair 之后都要重新做：

```text
_normalize_voiceover_scripts()
_sync_voiceover_narration_text_from_segments()
_attach_voiceover_script_timings()
_validate_voiceover_script_against_editing_structure()
_collect_voiceover_segment_duration_issues()
_collect_voiceover_script_duration_issues()
_merge_voiceover_duration_issues_into_alignment()
```

原因是模型修完以后：

```text
某些段补够了
某些段可能又超长了
整条文案可能够了
也可能仍然不够
```

必须重新计算，不能沿用旧 issue。

---

# 八、第四部分：TTS 后整条真实音频时长校验

## 当前基础

TTS 当前已经会：

```text
读取 voiceover_script
读取 editing_script
按 narration_segments 生成每段音频
合成 voiceover.wav
记录 actual_duration_seconds
构造 tts_outputs.json
调用 _build_tts_duration_reconcile()
```

代码里已经有 `_build_tts_duration_reconcile()`，并根据 `reconcile_ok` / `reconcile_hard_segments` 决定 TTS 是否失败。

但这里重点仍偏 segment 级，需要补整条级别。

> 微调补充：当前 `_build_tts_duration_reconcile()` 已经计算了 `video_target_total`、`video_tts_total`、`coverage_ratio`，所以开发时不建议新建完全割裂的数据结构。更合适的是在每个 video 下新增 `total_check`，并新增 `_tts_reconcile_hard_videos()`，让 video-level 覆盖比例参与 `_step_tts_impl()` 的失败判断。

---

## 新增整条 TTS 校验

建议新增：

```text
_collect_tts_script_duration_issues(editing, voiceover, tts)
_validate_tts_script_duration_or_raise(editing, voiceover, tts)
```

校验对象按 `short_video_id` 一条一条做。

每条取：

```text
target_visual_duration = editing_structure shot.target_duration_seconds 之和
actual_tts_duration = tts_output.actual_duration_seconds
```

如果是 `segment_aligned` 模式，也可以同时取：

```text
last_success_segment_target_end_seconds
last_success_segment_actual_end_seconds
```

判断规则：

```text
actual_tts_duration / target_visual_duration
```

推荐：

```text
>= 0.90：通过
0.75 ~ 0.90：偏短，生产模式失败；调试模式可 warning
< 0.75：严重失败，必须失败
> 1.15：偏长，可能压缩或失败
> 1.40：严重超长，必须失败
```

你这个问题：

```text
画面 60 秒
TTS 30 秒
比例 0.5
```

会被这里直接拦住，不能进入 render。

---

## 这里不要强行靠音频拉伸解决

当前配置有：

```toml
max_audio_speedup = 1.08
```

说明项目里已经有轻微速度调整的思想。

但是：

```text
60 秒画面，30 秒配音
```

不能靠拉伸解决。

建议规则：

```text
差距 <= 5%：可以轻微 atempo 调整
差距 5% ~ 15%：优先失败或回到文案 repair
差距 > 25%：直接失败，不能 render
```

> 微调补充：这一点对你当前问题很重要。`30 秒 TTS / 60 秒画面` 不是轻微语速问题，不能靠 `atempo` 或压缩画面糊过去。正确处理是回到 `voiceover_script` repair，或缩短/重做 `short_video_edit_plan` 的画面结构。

---

# 九、第五部分：render 前覆盖校验

这是最后一道保险。

## 为什么需要

即使 TTS 总时长接近，也可能出现：

```text
前面有字幕
中间某个 segment 失败
后面没有字幕
```

或者：

```text
compact 模式下音频是 50 秒
但画面 timeline 是 70 秒
```

所以 render 前要检查覆盖范围。

> 微调补充：当前 `_render_one()` 里 `amix=duration=first` 会以视频时长为准输出最终文件。如果 AI 音频更短，渲染仍可能成功产出视频，这正是“后半段无配音/无字幕”的风险来源。因此覆盖检查必须在 render 前阻断，不能只依赖 ffmpeg 是否成功。

---

## 校验对象

按每条 `short_video_id` 检查：

```text
visual_total_duration
voiceover_cover_end
subtitle_cover_end
```

### visual_total_duration

取：

```text
cut_plan.output_videos[].clips 的最后 target_end_seconds
```

如果 cut_plan 还没有这个字段，则用 clips 的 `duration_seconds` 累加。

### voiceover_cover_end

优先取：

```text
tts_output.segments 中最后一个成功 segment 的 target_end_seconds
```

如果没有 segments，则取：

```text
tts_output.actual_duration_seconds
```

### subtitle_cover_end

字幕如果是由 TTS segments 生成，可以和 `tts_output.segments` 一致。

如果要更严格，可以解析生成的 `.srt` 最后一条字幕 end 时间。

---

## 阈值

```text
voiceover_cover_end >= visual_total_duration * 0.90
subtitle_cover_end >= visual_total_duration * 0.90
```

不满足则 raise：

```text
render_ai_voiceover_coverage_too_short
```

用户提示建议：

```text
AI 配音/字幕覆盖范围明显短于最终视频，请从 voiceover_script 或 tts 重跑。
```

---

# 十、具体代码开发指南

## 1. `config.toml`

在 `[voiceover_script]` 下新增：

```toml
repair_on_segment_duration_issues = true
repair_on_script_duration_issues = true
script_estimated_soft_min_ratio = 0.85
script_estimated_hard_min_ratio = 0.65
```

在 `[voiceover]` 下新增：

```toml
tts_script_ok_min_ratio = 0.90
tts_script_hard_min_ratio = 0.75
tts_script_ok_max_ratio = 1.15
tts_script_hard_max_ratio = 1.40
voiceover_cover_min_ratio = 0.90
subtitle_cover_min_ratio = 0.90
```

保留现有：

```toml
pre_tts_segment_min_ratio = 0.65
tts_segment_ok_min_ratio = 0.75
tts_segment_hard_min_ratio = 0.55
```

这些继续用于 segment 级别校验。

> 微调补充：如果暂时不想大改配置结构，第一版也可以只新增 `[voiceover_script]` 下的 repair 开关和估算阈值；TTS 后阈值先在代码里通过 `voice_cfg.get(..., 默认值)` 读取。这样即使配置没写新字段，也不会影响启动。

---

## 2. `pipeline.py`：新增目标时长解析函数

建议新增：

```text
_resolve_script_visual_target_duration(edit_script, voice_script)
```

职责：

```text
返回一条短视频最终应该覆盖的画面目标时长。
```

优先级：

```text
1. sum(shot.target_duration_seconds)
2. editing_structure_duration(editing_structure)
3. edit_script.estimated_total_duration_seconds
4. voice_script.target_duration_seconds
5. self.options.target_duration_seconds
6. voiceover.default_target_duration_seconds
```

注意：

```text
只接受 > 0 且 finite 的数字。
遇到 None、空字符串、NaN、负数，跳过。

> 微调补充：这里要特别处理 `editing_structure_duration()` 与 `sum(shot.target_duration_seconds)` 的差异。如果实际最终画面是由 cut_plan 的 source clips 决定，目标时长应优先贴近 cut_plan 将要生成的视觉总时长；如果 cut_plan 尚未生成，就先用 editing_structure 的 `target_duration_seconds/duration_seconds` 汇总。
```

---

## 3. `pipeline.py`：新增文本字数统计函数

建议新增：

```text
_voiceover_text_chars(text)
```

规则：

```text
1. 先 clean_voiceover_text()
2. 去掉所有空白符
3. 统计 len()
```

不要把标点全删掉也可以，因为中文 TTS 标点会带来停顿。
但为了稳定，可以统一：

```text
字数统计去空白，不去中文标点。
```

这样和现有 `len(re.sub(r"\s+", "", text))` 风格一致。当前 alignment 校验也是这样算 text chars。

---

## 4. `pipeline.py`：新增 segment duration collect

函数：

```text
_collect_voiceover_segment_duration_issues(voiceover_output)
```

职责：

```text
扫描每个 narration_segment。
根据 target_duration_seconds 和 text chars 估算是否太短。
返回 issues，不 raise。
```

逻辑：

```text
if production_mode != "ai_voiceover":
    return []

读取：
pre_tts_segment_min_ratio
voiceover_chars_per_second

对每个 segment：
target = segment.target_duration_seconds
chars = text chars
estimated = chars / voiceover_chars_per_second
required = target * pre_tts_segment_min_ratio

如果 estimated < required：
    生成 issue
```

注意：

```text
target_duration_seconds 缺失时，不要误报。
text 为空时，交给 alignment empty_text 处理，但也可以生成 duration issue。

> 微调补充：segment collect 返回 issue 时，建议带上 `source = "pre_tts_segment_duration"`，并带 `required_chars / under_chars / estimated_text_duration_seconds / required_estimated_seconds`。后面 repair prompt 才知道要补多少，而不是只知道“太短”。
```

---

## 5. `pipeline.py`：新增 script duration collect

函数：

```text
_collect_voiceover_script_duration_issues(voiceover_output, edit_plan)
```

职责：

```text
扫描每条 short_video_id。
判断整条 narration_text 是否明显短于目标画面时长。
```

逻辑：

```text
if production_mode != "ai_voiceover":
    return []

读取：
script_estimated_soft_min_ratio = 0.85
script_estimated_hard_min_ratio = 0.65
voiceover_chars_per_second = 4.8

对每条 script：
target = _resolve_script_visual_target_duration(edit_script, voice_script)
text = narration_text；如果没有，则拼接 narration_segments[].text
chars = _voiceover_text_chars(text)
estimated = chars / cps
ratio = estimated / target

如果 ratio < hard_min_ratio:
    severity = hard
elif ratio < soft_min_ratio:
    severity = warning 或 repairable
else:
    pass
```

这里建议：**在 AI 配音模式下，soft 也进入 repair，但最终 raise 时可以只把 hard 当硬失败**。

原因是你要解决“后半段无字幕”，所以 0.85 以下就应该尽量修。

> 微调补充：这里不要只取 `voice_script.target_duration_seconds`。当前 voiceover_script 模型输出未必稳定带这个字段，且真实视觉时长更应该来自 edit_plan/editing_structure。否则会误判为 target=0，从而漏掉整条文案过短。

---

## 6. `pipeline.py`：新增 script issue 分摊函数

函数：

```text
_expand_script_duration_issue_to_segment_issues(script_issue, edit_script, voice_script, existing_segment_issues)
```

职责：

```text
把整条文案太短的问题，转成每个 shot 可修的 issue。
```

策略：

```text
1. 如果已有 segment too_short issues：
   优先把整条缺口分摊给这些 segment。

2. 如果没有：
   分摊给所有 narration_segments。

3. 分摊权重：
   shot.target_duration_seconds 越长，补得越多。

4. 每个 segment 生成 suggested_add_chars。
```

边界：

```text
如果 segments 为空：不分摊，返回 script-level issue，最后由结构校验失败。
如果 shot target 时长全为空：平均分摊。
如果 suggested_add_chars < 6：最小补 6~8 字，避免模型无动作。
```

---

## 7. `pipeline.py`：新增 merge 函数

函数：

```text
_merge_voiceover_duration_issues_into_alignment(
    alignment,
    segment_issues,
    script_segment_issues,
)
```

职责：

```text
把时长问题挂到 alignment.checks[].char_budget_issues。
```

处理逻辑：

```text
for check in alignment.checks:
    sid = check.short_video_id
    找到属于 sid 的 duration issues
    append 到 check.char_budget_issues
    如果有 issue:
        check.ok = false

alignment.ok = 所有 check.ok 都为 true
alignment.hard_issues 追加时长问题摘要
```

注意：

```text
不要覆盖原来的 char_budget_issues。
不要删除 char_budget_warnings。
duration issue 要带 source 字段，方便 debug。
```

---

## 8. `pipeline.py`：改 `_build_voiceover_repair_text_input()`

现状：

```text
issue_map 只存 issue.type
problem_segments 只知道 current_chars、min_chars、target_chars、max_chars
```

建议增强：

```text
problem_segments 增加：
issues
estimated_text_duration_seconds
required_estimated_seconds
suggested_add_chars
script_target_duration_seconds
script_estimated_duration_seconds
repair_instruction
```

比如：

```json
{
  "shot_id": "v_001_s02",
  "issues": [
    {
      "type": "segment_duration_too_short",
      "source": "pre_tts_segment_duration",
      "target_duration_seconds": 12,
      "estimated_text_duration_seconds": 8.1,
      "suggested_add_chars": 12
    },
    {
      "type": "script_duration_too_short",
      "source": "pre_tts_script_duration",
      "suggested_add_chars": 18
    }
  ],
  "repair_instruction": "适度扩写该段解说，补足上下文和画面承接，不新增事实。"
}
```

这样模型才知道不是简单“字数略短”，而是“整条视频配音覆盖不足”。

> 微调补充：这里不需要大改 prompt 输出格式，仍然保持 `repaired_segments[].shot_id/new_text`。只增强输入信息即可，降低后续解析风险。

---

## 9. `pipeline.py`：改 `_check_is_text_patch_repairable()`

当前逻辑里，`char_budget_issues` 和 `empty_text_shot_ids` 可以 repair；warning 是否 repair 受 `repair_on_warnings` 控制。

建议：

```text
只要 char_budget_issues 里有 source in:
- pre_tts_segment_duration
- pre_tts_script_duration

就认为 repairable。
```

不要依赖 `repair_on_warnings`。

原因是：

```text
时长不足不是普通 warning，
它会导致最终配音/字幕覆盖不足。

> 微调补充：实现时可以判断 `issue.get("source") in {"pre_tts_segment_duration", "pre_tts_script_duration"}`，即使 issue 的 severity 是 warning，也允许 repair；但普通 `char_budget_warnings` 仍然受 `repair_on_warnings=false` 控制。
```

---

## 10. `pipeline.py`：改 `_finalize_voiceover_script_with_repair()`

核心改法：

```text
原来：
alignment = validate_alignment()
if not alignment.ok:
    repair()
最后 validate duration or raise

改成：
alignment = validate_alignment()
segment_issues = collect_segment_duration_issues()
script_issues = collect_script_duration_issues()
script_segment_issues = expand_script_issues_to_segments()
merged_alignment = merge_duration_issues_into_alignment()

if not merged_alignment.ok:
    repair()

每轮 repair 后重新构造 merged_alignment

最后：
validate_alignment(raise_on_error=True)
validate_segment_duration_or_raise()
validate_script_duration_or_raise()
```

最终 hard validate 仍保留，作为兜底。

---

# 十一、TTS 后代码开发指南

## 1. 改 `_build_tts_duration_reconcile()`

当前它已经构造每条视频的 segment reconcile。建议在返回结构里新增：

```json
{
  "script_summary": {
    "short_video_id": "v_001",
    "target_visual_duration_seconds": 60,
    "actual_tts_duration_seconds": 52,
    "ratio": 0.867,
    "status": "too_short",
    "hard": false
  }
}
```

或者在每个 video 里新增：

```text
video.total_check
```

不要另起一个完全割裂的数据结构，避免后面排查困难。

---

## 2. 新增 `_tts_reconcile_hard_videos()`

类似当前：

```text
_tts_reconcile_hard_segments()
```

新增：

```text
_tts_reconcile_hard_videos()
```

判断：

```text
video.total_check.hard == true
或 status in {"too_short", "too_long"} 且生产模式要求 block
```

然后在 `_step_tts_impl()` 里增加：

```text
elif tts_is_hard_required and reconcile_hard_videos:
    overall_status = "failed"
    hard_error = "TTS duration has hard mismatch videos"
```

当前 `_step_tts_impl()` 已经会根据 `reconcile_ok` 和 hard segments 阻断 TTS。你只需要把整条 video 级别也纳入这个决策。

> 微调补充：注意当前 `reconcile.ok` 是根据每个 video 的 segment 状态汇总出来的。新增 video total check 后，`reconcile.ok` 也要同步考虑 `video.total_check.ok`，否则虽然写出了 total_check，但不会真正阻断。

---

## 3. TTS 后失败建议

错误类型建议：

```text
tts_script_duration_too_short
tts_script_duration_too_long
```

用户提示：

```text
TTS 生成失败：整条 AI 配音真实时长明显短于目标画面时长。
建议从 voiceover_script 重跑，让文案补足；如果素材事实不足，请缩短 short_video_edit_plan 的画面时长。
```

技术细节带：

```json
{
  "short_video_id": "v_001",
  "target_visual_duration_seconds": 60,
  "actual_tts_duration_seconds": 30,
  "ratio": 0.5,
  "threshold": 0.90,
  "hard_threshold": 0.75
}
```

---

# 十二、render 前覆盖校验开发指南

## 1. 新增函数

```text
_validate_ai_voiceover_coverage_before_render(cut_plan, tts, subtitles)
```

或者如果 render 阶段更容易拿到数据，也可以放在：

```text
step_render()
```

前面。

> 微调补充：更推荐第一版放在 `step_cut_plan()` 末尾，即 `output_videos` 生成后、写出 `cut_plan.json` 前。因为这里已经能拿到 `video_duration_seconds`、`voiceover_duration_seconds`、`voiceover.segments`、`subtitles.file`。`step_render()` 再做兜底即可。

职责：

```text
确保每条 output_video 的配音和字幕覆盖到视频末尾附近。
```

---

## 2. 检查内容

每条视频：

```text
visual_total = 最后一个 clip.target_end_seconds 或 clips.duration_seconds 总和
voiceover_end = tts segment 最大 target_end_seconds / actual end / tts actual duration
subtitle_end = srt 最后一条 end，或者 tts segment 最大 end
```

规则：

```text
voiceover_end >= visual_total * voiceover_cover_min_ratio
subtitle_end >= visual_total * subtitle_cover_min_ratio
```

不满足则失败。

---

## 3. 为什么字幕要单独检查

当前字幕生成逻辑是：

```text
如果 subtitle_follow_actual_tts 且有 tts_segments：
    用 _tts_segments_to_srt()
否则如果有 narration_segments：
    用 _segments_to_srt()
否则：
    用整条 script 切字幕
```

也就是说，字幕可能跟 TTS，也可能退回 narration_segments。

所以不能只看 TTS 成功，还要确认字幕最终也覆盖到末尾。

> 微调补充：如果短期不想解析 `.srt`，可以先用 `tts_segments` 或 `narration_segments` 的最大 `target_end_seconds` 做近似校验；后续再补一个轻量 SRT 解析函数，读取最后一条字幕结束时间。

---

# 十三、边界条件

## 1. 非 AI 配音模式不要拦

如果：

```text
production_mode != "ai_voiceover"
audio_policy == "original"
skip_tts == true
require_tts == false
```

不要触发这些硬校验。

否则会影响原声高光重组模式。

---

## 2. mixed evidence 原声片段

如果某些 shot 是：

```text
audio_mode = mixed_evidence
original_audio_required = true
```

那这段可能本来就允许保留原声，不要求 AI 文案完全覆盖。

处理方式：

```text
segment 级校验：跳过 original_audio_required 的 segment，或降低要求。
script 总时长校验：目标时长可以扣除 planned original audio evidence 秒数。
render 覆盖校验：如果该段有原声 evidence，可以视为 audio covered，但字幕是否覆盖要看产品要求。
```

不过你当前 AI 配音模式配置里：

```toml
ai_voiceover_disable_original_audio = true
allow_original_audio_evidence = false
```

说明默认是不希望原声参与的。

所以第一版可以简单处理：

```text
纯 AI 配音模式：全部要求 AI voiceover 覆盖。
mixed 模式：再做扣除逻辑。
```

---

## 3. 目标时长缺失

如果拿不到 target：

```text
不应该直接按 0 算通过。
```

建议：

```text
target 缺失时：
1. 优先从 editing_structure_duration 算。
2. 仍然没有，就写 warning debug。
3. 不做时长硬失败，但保留结构校验。
```

---

## 4. 文案为空

如果 segment text 空：

```text
alignment empty_text 已经能发现。
duration issue 可以附加，但不要重复报太多。
```

最终用户看到的错误应该聚合，不要刷屏。

---

## 5. 多条短视频

必须按 `short_video_id` 单独校验。

不能只把所有 scripts 合起来算总时长，否则会漏掉：

```text
v_001 合格
v_002 配音严重不足
```

---

## 6. 语速估算不准

TTS 前的字数估算只是粗略判断，所以：

```text
TTS 前不要要求 100%。
TTS 后才要求 90%。
```

这也是为什么：

```text
script_estimated_soft_min_ratio = 0.85
tts_script_ok_min_ratio = 0.90
```

比较合理。

---

## 7. 防止模型灌水

repair prompt 里必须强调：

```text
不能为了凑字数重复废话
不能新增事实
不能写画面没有的信息
不能改变 shot_id
不能新增/删除 segment
不能改变顺序
保持新闻口吻
一句话适合一行字幕
```

否则模型可能把文案写长，但质量变差。

---

## 8. 防止无限 repair

保留现有：

```toml
max_repair_rounds = 2
```

两轮后仍不达标就失败。

不要无限循环。

当前配置里已经有 `max_repair_rounds = 2`。

---

# 十四、推荐开发顺序

## 第一步：只做 TTS 前 repair 闭环

先解决当前报错：

```text
voiceover_script_segment_duration_too_short
```

开发内容：

```text
1. collect segment duration issues
2. collect script duration issues
3. merge issues into alignment
4. repair input 增强
5. _finalize_voiceover_script_with_repair() 接入
```

这一步完成后，你现在的任务应该不会在 TTS 前直接因为文案略短/明显短而失败，而是会先尝试修文案。

---

## 第二步：加 TTS 后整条真实时长校验

开发内容：

```text
1. _build_tts_duration_reconcile() 增加 video total check
2. _tts_reconcile_hard_videos()
3. _step_tts_impl() 纳入 hard video 判断
```

这一步解决：

```text
TTS 实际只有 30 秒，但画面 60 秒
```

不能继续 render 的问题。

---

## 第三步：加 render 前覆盖校验

开发内容：

```text
1. 计算 cut_plan 每条视频 visual_total
2. 计算 voiceover_cover_end
3. 计算 subtitle_cover_end
4. 不达标则阻断 render
```

这一步是最后保险，防止漏网。

---

# 十五、测试用例

## 用例 1：当前失败用例

```text
shot1 目标 14 秒，文案估算 11 秒
shot2 目标 12 秒，文案估算 8 秒
整条目标 60 秒，文案估算 30 秒
```

期望：

```text
进入 voiceover repair
不直接抛 voiceover_script_segment_duration_too_short
repair 后重新校验
```

---

## 用例 2：每段都勉强够，但整条不够

```text
每个 segment 都没低于 0.65
但整条 narration_text 只有目标的 0.7
```

期望：

```text
script_duration_too_short 被发现
分摊到 segments
进入 repair
```

---

## 用例 3：TTS 前估算够，TTS 后真实音频不够

```text
目标 60 秒
文案估算 54 秒
TTS 实际 39 秒
```

期望：

```text
TTS 阶段失败
错误指向 tts_script_duration_too_short
不进入 render
```

---

## 用例 4：字幕覆盖不足

```text
视频 60 秒
TTS 58 秒
但字幕只生成到 32 秒
```

期望：

```text
render 前失败
错误指向 subtitle coverage too short
```

---

## 用例 5：原声重组模式

```text
production_mode = highlight_reassembly / audio_policy = original
```

期望：

```text
不触发 AI 配音时长校验
不影响原声重组
```

---

# 十六、最终建议结论

这次不要只修一个：

```text
voiceover_script_segment_duration_too_short
```

否则只能解决当前报错，但不能完全避免：

```text
AI 配音 30 秒，画面 60 秒
```

最终应该按这个改：

```text
1. TTS 前：segment 文案估算时长校验进入 repair。
2. TTS 前：整条 narration_text 估算时长校验进入 repair。
3. TTS 后：整条真实音频时长校验，不够就失败。
4. render 前：字幕/配音覆盖范围校验，不够就失败。
```

第一版最小可落地改动是：

```text
只改 pipeline.py + config.toml。
不动 run_web.py、web_app.py、run_pipeline.py。
不改整体工作流。
不新增大步骤。
只是在现有 voiceover_script repair、tts reconcile、render 前校验里补齐时长闭环。
```

这样既能解决当前报错，也能防止跑完后生成“后半段无配音无字幕”的坏视频。

---

# 十七、本次复核后的微调结论

原方案整体方向符合预期，不需要推翻。建议只做以下微调：

```text
1. 保留原方案的 TTS 前 repair 闭环，不要简化成只打开 repair_on_warnings。
2. _attach_voiceover_segment_timing() 要优先使用 target_duration_seconds，避免目标时长挂错。
3. TTS 后整条校验应增强现有 _build_tts_duration_reconcile()，不要另起割裂结构。
4. 覆盖校验优先放在 cut_plan 写出前，render 前再兜底。
5. 新增配置第一版尽量用 self.config.raw 读取，避免同步改太多 duration_settings 结构。
6. repair prompt 的输出格式不要大改，只增强输入 problem_segments 的 issues 信息。
```

因此最终建议不是重写整套方案，而是在原方案上补齐这些落点。这样既能解决当前 `voiceover_script_segment_duration_too_short`，也能防止后续继续出现 AI 配音和字幕只覆盖前半段、后半段仍然渲染出坏视频的问题。
