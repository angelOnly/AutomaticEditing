可以，这个链路要优化，核心不是简单“放宽阈值”，而是要把现在的：

```text
规划画面 -> 生成文案 -> 生成 TTS -> 发现不匹配 -> 失败
```

改成：

```text
规划画面 -> 文案预算 -> 文案预检/修复 -> TTS -> 真实时长对齐 -> 自动补救 -> 最终成片
```

## 一、先明确目标：AI 配音不是“画面定死”，而是“画面和配音共同收敛”

你现在的问题是：画面先定成 136 秒，TTS 只有 92 秒，然后系统直接失败。

但 AI 配音解说模式里，画面不应该是绝对固定的。它应该有弹性：

```text
画面是素材池
文案是叙事主线
TTS 是真实节奏
cut_plan 是最终调和层
```

所以优化目标应该是：

```text
优先让文案覆盖核心事实；
如果文案可以自然补长，就补文案；
如果补长会啰嗦，就缩画面；
如果部分 shot 太长，就局部缩画面；
如果整体偏短，再全局压缩画面；
最后保证 TTS 与视频时长接近。
```

不是一发现 mismatch 就失败。

---

# 二、建议拆成 4 层对齐

## 第 1 层：规划阶段就不要让画面目标失控

现在 6 个 shot 合计 136 秒，这对 AI 配音短视频来说已经偏长。除非明确是长版解说，否则默认不应该让 AI 配音模式选到 136 秒。

这里应该先做一个 **画面规划约束**：

```text
AI 配音普通模式：
目标 30-60 秒

AI 配音长版模式：
目标 60-120 秒

超过 120 秒：
必须证明有必要，或者拆成多条
```

所以第一层优化是：

```text
short_video_edit_plan 不只是选片
还要判断：
1. 这个故事需要多少秒讲清楚？
2. 当前选片是否过长？
3. 哪些 shot 是核心，哪些只是背景/补充？
4. 每个 shot 是否可以缩短？
```

每个 shot 应该带一个角色：

```json
{
  "shot_id": "v_001_s01",
  "role": "opening / core_evidence / context / visual_support / optional",
  "importance": "must / should / optional",
  "min_duration_seconds": 6,
  "target_duration_seconds": 12,
  "max_duration_seconds": 18,
  "can_trim": true
}
```

这样后面发现 TTS 不够时，系统知道该先缩谁，而不是整条失败。

---

# 三、第 2 层：文案生成前要有“字符预算合同”

在生成 voiceover_script 之前，系统应该先把每个 shot 的目标时长换算成文案字符预算。

比如中文新闻解说大概：

```text
4.5 - 5.5 字 / 秒
```

如果一个 shot 目标 30 秒，那文案至少要：

```text
30 * 4.5 = 135 字左右
```

但你这次的问题很可能是：

```text
shot 目标 28-30 秒
模型只写了 80-100 字
TTS 真实只读了 17 秒
```

所以 voiceover_script 的输入里应该明确告诉模型：

```text
这个 shot 目标 28 秒
建议 125-150 字
最低不能低于 110 字
最高不要超过 170 字
```

文案生成后立刻做预检：

```text
每段文案字符数 / 目标字符数
每段估算时长 / 目标时长
总文案估算时长 / 总画面目标时长
```

如果估算就明显不足，不要进入 TTS，先修文案。

---

# 四、第 3 层：文案修复要从 warning 升级成“必须修复”

现在的问题是：偏短可能只是 warning，所以没有触发修复。

这里应该调整策略：

```text
轻微偏短：warning，不修
明显偏短：自动修
严重偏短：必须修，否则不进入 TTS
```

建议分三档：

```text
估算文案时长 >= 90% 目标时长：
通过

估算文案时长 75% - 90%：
触发文案补强修复

估算文案时长 < 75%：
强制修复；修复失败则回退到缩短画面
```

也就是说，文案修复不是只修结构错误，而是要修：

```text
1. shot 文案过短
2. 整条文案过短
3. 某些核心事实没讲
4. 文案覆盖不了画面长度
5. 分段文案和 shot 不匹配
```

修复时不要让模型自由发挥，而是给它明确任务：

```text
只补长这些 shot：
- v_001_s01：当前 82 字，目标 130 字
- v_001_s04：当前 90 字，目标 145 字

补充方向：
- 增加背景
- 增加因果解释
- 增加人物/机构立场
- 增加风险/影响
不要编造新事实
不要重复废话
```

这样修复才稳定。

---

# 五、第 4 层：TTS 之后要做真实时长二次对齐

文案估算永远不可能 100% 准，因为 TTS 速度、停顿、标点都会影响真实时长。

所以 TTS 后还要做真实对齐，但不能直接失败。

TTS 后应该进入一个 `tts_video_reconcile` 决策层：

```text
输入：
- 每个 shot 的目标画面时长
- 每个 shot 的 TTS 实际时长
- 每段文案字符数
- 每个 shot 是否可裁剪
- 每个 shot 重要性
- 总视频目标时长
```

然后按策略处理。

---

# 六、TTS 后不匹配时的处理策略

## 情况 A：TTS 只短一点

比如：

```text
画面 100 秒
TTS 92 秒
覆盖率 92%
```

这种不用重跑文案，直接微调：

```text
1. 缩短结尾空镜
2. 缩短 optional shot
3. 每个 shot 小幅裁 0.5-1 秒
4. 字幕跟随 TTS
```

这个属于正常误差。

---

## 情况 B：TTS 中度偏短

比如：

```text
画面 136 秒
TTS 110 秒
覆盖率 80%
```

这时先判断文案有没有补长空间。

如果新闻事实丰富，文案可以自然补充：

```text
优先补长文案 -> 重跑 TTS
```

如果文案已经完整，再补会啰嗦：

```text
缩短画面 -> 重建 cut_plan
```

也可以混合：

```text
补 10 秒文案
缩 15 秒画面
最后收敛到 120 秒左右
```

---

## 情况 C：TTS 严重偏短

比如你这次：

```text
画面 136.6 秒
TTS 92.2 秒
覆盖率 67.5%
```

这种不应该简单补文案到 136 秒，因为会有两个风险：

```text
1. 文案为了凑时长开始啰嗦
2. 大模型可能编造事实
```

所以这类应该进入“重规划”：

```text
先判断：
这条新闻真的需要 136 秒吗？
如果不需要：
    缩短画面到 90-105 秒
如果需要：
    转成长版解说，并要求补充背景/分析文案
```

对于你的项目，我建议默认策略是：

```text
AI 配音模式下，TTS 明显短于画面时：
优先缩画面，而不是强行补文案。
```

因为新闻短视频最怕为了凑时长讲废话。

---

# 七、最终推荐的完整链路

我建议改成这个流程：

```text
1. short_video_edit_plan
   选片 + 给每个 shot 标注 min/target/max + importance + can_trim

2. voiceover_budget
   根据 shot 时长生成字符预算
   例如每秒 4.8 字

3. voiceover_script
   按预算生成文案

4. voiceover_precheck
   检查：
   - 每段字符数是否够
   - 总估算时长是否够
   - 是否缺事实
   - 是否有空段

5. voiceover_repair
   如果估算不足，先修文案
   最多修 1-2 轮

6. tts
   生成真实音频

7. tts_video_reconcile
   对比：
   - 每段 TTS 时长 vs shot 目标时长
   - 总 TTS 时长 vs 视频目标时长

8. auto_decision
   根据差异选择：
   A. 轻微差异：直接调整 cut_plan
   B. 文案偏短且可补：补文案，重跑 TTS
   C. 画面过长：缩短 shot，重建 cut_plan
   D. 差异严重：重新规划视频长度

9. cut_plan
   用真实 TTS 时长作为最终主时间轴

10. render
   最终渲染
```

---

# 八、最关键的原则：AI 配音模式应该以 TTS 为主时间轴

原声重组模式是：

```text
原片画面/声音为主
```

但 AI 配音模式应该是：

```text
TTS 解说为主
画面围绕解说服务
```

所以最终 cut_plan 应该优先跟随 TTS：

```text
每段 TTS 17 秒
对应画面就尽量裁成 17-19 秒

每段 TTS 12 秒
对应画面就裁成 12-14 秒

如果画面必须保留 30 秒
那文案就必须补到 25-30 秒
```

不能让画面固定 136 秒，然后要求 TTS 硬凑。

---

# 九、这次问题按优化链路应该怎么处理

你这次是：

```text
画面目标：136.557 秒
TTS 实际：92.16 秒
差距：44.397 秒
覆盖率：67.5%
```

我的判断：

```text
不应该直接补文案到 136 秒
应该先判断 136 秒是否必要
```

更合理的自动策略是：

```text
1. 判断文案是否已经覆盖核心事实
2. 如果核心事实已覆盖：
   把视频目标从 136 秒压到 95-105 秒
   优先裁掉 optional/context shot
   每个 shot 根据 TTS 实际时长重新分配画面长度

3. 如果核心事实没覆盖：
   先补文案到 105-115 秒
   再根据真实 TTS 重建 cut_plan

4. 除非用户明确选择长版：
   不要强行做 136 秒 AI 解说
```

也就是说，这次最佳修复方向大概率是：

```text
画面缩短为主
文案补强为辅
```

---

# 十、最终策略建议

我建议你把 AI 配音模式的默认策略定成：

```text
文案优先完整，不强行凑长；
TTS 是最终时间轴；
画面围绕 TTS 自动裁剪；
只有核心事实缺失时才补文案；
只有用户选择长版时才允许强行扩写到 120 秒以上。
```

这样可以避免两种极端：

```text
画面太长，TTS 太短，最后失败；
为了凑时长，文案啰嗦甚至编造。
```

你这个链路真正要补的不是一个阈值，而是一个 **TTS 后自动决策层**：

```text
tts_video_reconcile -> auto_decision -> repair_voiceover / shrink_cut_plan / replan
```

这才是完整闭环。


可以。下面这个方案先按**产品逻辑 + 架构设计 + 流程设计 + 数据设计 + 策略细节**来讲，不进入具体代码实现。目标是先把“为什么这么做、链路怎么闭环、每一步职责是什么、失败时怎么决策”确定下来。

---

# 一、核心目标

现在 AI 配音解说模式的问题是：

```text
画面规划给了 136 秒
文案生成偏短
TTS 真实只有 92 秒
系统发现不匹配
直接失败
```

优化后的目标是：

```text
不要直接失败
而是形成一个自动闭环：

画面规划
  -> 文案预算
  -> 文案生成
  -> 文案预检
  -> 文案修复
  -> TTS
  -> TTS真实时长对齐
  -> 自动决策：补文案 / 缩画面 / 重规划
  -> 重新收敛
  -> cut_plan
  -> render
```

最终要做到：

```text
AI 配音模式下，TTS 是最终主时间轴。
画面不是完全固定的，而是围绕配音节奏自动裁剪。
```

---

# 二、整体架构建议

建议把 AI 配音模式拆成 5 个逻辑层。

```text
一、素材与候选层
  负责找可用片段

二、短视频规划层
  负责决定讲哪条故事、选哪些画面、每个 shot 的最小/目标/最大时长

三、文案生成与修复层
  负责根据画面时长生成合适长度的解说稿，并在 TTS 前先修正明显偏短的问题

四、TTS 真实时长对齐层
  负责根据真实音频结果，判断是否需要补文案、缩画面或重规划

五、最终剪辑层
  以 TTS 真实时间轴为准，生成 cut_plan 和最终视频
```

也就是说，不能再把 `tts_duration_mismatch` 只当成“错误”，而应该把它升级成一个**自动决策节点**。

---

# 三、新增一个核心概念：Timing Contract

建议引入一个统一的“时长合同”，我先叫它：

```text
voiceover_timing_contract
```

它不是最终视频，也不是文案，而是整个 AI 配音链路的约束协议。

它应该贯穿：

```text
short_video_edit_plan
voiceover_script
voiceover_repair
tts
tts_video_reconcile
cut_plan
```

## 这个合同要解决什么？

它要回答这些问题：

```text
这条视频目标多长？
每个 shot 最少多长？
每个 shot 理想多长？
每个 shot 最长多长？
每个 shot 最少需要多少字？
每个 shot 建议多少字？
每个 shot 最多多少字？
这个 shot 能不能裁？
这个 shot 是核心还是可选？
如果 TTS 不够，应该先补文案还是先缩画面？
```

## 每个 shot 应该有这些信息

```json
{
  "shot_id": "v_001_s01",
  "source_clip_id": "clip_001",
  "role": "opening",
  "importance": "must",
  "can_trim": true,

  "source_duration_seconds": 30.2,

  "min_duration_seconds": 8,
  "target_duration_seconds": 18,
  "max_duration_seconds": 24,

  "narration_min_chars": 70,
  "narration_target_chars": 95,
  "narration_max_chars": 125,

  "repair_priority": "high",
  "trim_priority": "low"
}
```

这个信息后面非常关键。否则系统不知道：

```text
文案短了，是补这个 shot？
还是裁这个 shot？
还是丢掉这个 shot？
```

---

# 四、短视频规划层要先优化

目前看，136 秒的画面目标偏长。AI 配音短视频不应该随便规划到 136 秒，除非明确是长版解说。

所以第一步不是文案修复，而是先让 `short_video_edit_plan` 的输出更“可控”。

## 1. 规划阶段要区分视频类型

建议分三档：

```text
普通短解说：
30 - 60 秒

中等解释型：
60 - 90 秒

长版深度解说：
90 - 150 秒
```

默认不要直接进入长版。超过 90 秒，系统应该有明确理由：

```text
为什么这条新闻需要长版？
是否有多个阶段？
是否有多方立场？
是否有背景、原因、影响、后续？
是否适合拆成多条？
```

## 2. 每个 shot 不再只有 duration，而要有弹性范围

现在的问题是：shot 时长像是被固定了。

建议改成：

```text
每个 shot 有 min / target / max 三个时长
```

例如：

```text
s01 开头背景：
min 8s
target 15s
max 22s

s02 核心事实：
min 12s
target 20s
max 30s

s03 补充画面：
min 5s
target 10s
max 15s
```

这样 TTS 真实偏短时，就可以把 shot 从 target 裁到 min，而不是直接失败。

## 3. 每个 shot 要标注重要性

建议有三类：

```text
must：
必须保留，承载核心事实

should：
最好保留，增强理解

optional：
可裁剪、可删除，用来补画面或过渡
```

TTS 不够时处理顺序应该是：

```text
先裁 optional
再裁 should
最后才动 must
```

## 4. 每个 shot 要标注“可修文案”还是“可裁画面”

有些画面适合补文案，比如：

```text
复杂国际关系背景
多方表态
政策影响
冲突原因
```

有些画面不适合补文案，比如：

```text
重复会场镜头
走路镜头
空镜
新闻资料画面
同一人物重复讲话
```

所以每个 shot 可以有两个策略字段：

```text
repair_priority：文案补长优先级
trim_priority：画面裁剪优先级
```

例如：

```json
{
  "shot_id": "v_001_s04",
  "role": "context",
  "importance": "should",
  "repair_priority": "high",
  "trim_priority": "medium"
}
```

意思是：这个 shot 可以补背景文案，也可以适度裁剪。

---

# 五、文案生成前要做字符预算

这是这套链路的关键。

现在不能只告诉模型：

```text
给我生成新闻解说文案
```

而应该告诉它：

```text
这条视频目标 95 秒
每个 shot 的文案目标如下：
s01 15 秒，建议 70-95 字
s02 20 秒，建议 95-120 字
s03 12 秒，建议 55-75 字
```

## 字符预算怎么来？

可以根据中文 TTS 的平均语速估算。

建议默认：

```text
中文新闻解说：4.5 - 5.2 字/秒
```

可以先保守取：

```text
target_chars = target_duration_seconds * 4.8
min_chars = target_duration_seconds * 3.8
max_chars = target_duration_seconds * 5.8
```

你现在配置里其实已经有类似参数：

```text
voiceover_chars_per_second = 4.8
voiceover_min_chars_per_second = 3.8
voiceover_max_chars_per_second = 5.8
```

这个方向是对的。后续要做的是：**让这些预算真正成为强约束，而不是只做提示。**

## 文案生成后必须预检

在进入 TTS 前，先检查：

```text
每个 shot 文案是否低于 min_chars
整条文案是否低于总 min_chars
是否有空段
是否有 shot 缺文案
是否有文案段落和 shot_id 不匹配
```

如果预检不通过，不要进入 TTS。

---

# 六、文案修复层要升级

现在文案修复有，但触发条件太保守。

优化后建议把文案问题分成三档。

## 1. 轻微偏短

```text
估算时长 >= 90% 目标时长
```

处理方式：

```text
允许继续，不修复
```

因为后面真实 TTS 可能刚好够。

## 2. 中度偏短

```text
估算时长 75% - 90% 目标时长
```

处理方式：

```text
自动修复文案
修复 1 轮
再检查
```

修复目标不是“随便扩写”，而是：

```text
只补指定 shot
只补事实、背景、因果、影响
禁止编造
禁止重复废话
禁止营销号
```

## 3. 严重偏短

```text
估算时长 < 75% 目标时长
```

处理方式：

```text
先修文案
如果修复后还是不够
不要继续强补
进入视频缩短 / 重规划
```

严重偏短时，不应该无限让模型扩写，因为新闻会变水，甚至编造。

---

# 七、TTS 之后新增一个决策层

现在 TTS 后只是判断：

```text
够不够？
不够就 failed
```

建议改成：

```text
TTS 后先生成 tts_video_reconcile
然后进入 auto_decision
```

## tts_video_reconcile 应该输出什么？

它应该按 shot 和整条视频分别输出。

```json
{
  "short_video_id": "v_001",
  "target_video_seconds": 136.6,
  "tts_actual_seconds": 92.2,
  "coverage_ratio": 0.675,
  "status": "severe_short",

  "segments": [
    {
      "shot_id": "v_001_s01",
      "target_duration_seconds": 28.3,
      "tts_actual_duration_seconds": 17.8,
      "gap_seconds": 10.5,
      "coverage_ratio": 0.63,
      "status": "severe_short",
      "importance": "must",
      "can_trim": true,
      "repair_priority": "medium",
      "trim_priority": "medium"
    }
  ]
}
```

这个不是最终错误，而是给自动决策层用的诊断数据。

---

# 八、自动决策层怎么判断？

建议新增一个逻辑层：

```text
voiceover_video_auto_decision
```

它接收：

```text
short_video_edit_plan
voiceover_script
tts_outputs
tts_video_reconcile
timing_contract
```

输出一个决策：

```text
continue
repair_voiceover
shrink_video
hybrid_repair_and_shrink
replan_video
action_required
```

## 决策规则建议

### 情况 1：基本匹配

```text
coverage_ratio >= 0.90
```

处理：

```text
直接继续 cut_plan
小幅裁剪画面或补空隙
```

### 情况 2：轻微偏短

```text
0.82 <= coverage_ratio < 0.90
```

处理：

```text
优先缩画面
不重跑 TTS
```

因为差距不大，裁一些 optional 镜头即可。

### 情况 3：中度偏短

```text
0.70 <= coverage_ratio < 0.82
```

处理：

```text
判断文案是否可补：

如果核心事实没讲完：
    补文案 -> 重跑 TTS

如果文案已经完整：
    缩画面 -> 重建 cut_plan

如果两者都有：
    混合策略：补一点文案 + 缩一点画面
```

### 情况 4：严重偏短

```text
coverage_ratio < 0.70
```

你这次就是这个情况。

处理建议：

```text
不要优先强行补文案
先判断画面是否规划过长
```

默认策略：

```text
如果不是长版模式：
    缩短视频目标
    重建 shot duration budget
    重新生成 cut_plan

如果是长版模式：
    尝试补文案
    但最多补到合理上限
    仍不够则提示需要人工确认
```

---

# 九、补文案和缩画面怎么选择？

这是这个方案最重要的策略。

## 优先补文案的情况

适合补文案：

```text
1. 新闻事实还没讲完整
2. 存在明显背景缺失
3. 有复杂因果关系没解释
4. 有多方立场需要补充
5. 有影响、风险、后续没有讲
6. 当前文案只是标题式/摘要式
```

比如中美关系、国际政治、科技政策类新闻，通常有补充空间。

## 优先缩画面的情况

适合缩画面：

```text
1. 文案已经把事情讲清楚
2. 画面重复
3. 多个 shot 承载同一事实
4. 有大量空镜、会场、走路、握手、资料画面
5. 补文案会显得啰嗦
6. 超过默认短视频时长太多
```

你这次 136 秒画面、92 秒 TTS，很可能属于：

```text
画面规划偏长
应该缩画面为主
```

## 混合策略

很多时候最合理的是混合：

```text
TTS 92 秒
画面 136 秒
差距 44 秒

系统可以决策：
补文案 10-15 秒
缩画面 25-30 秒
最终视频 105 秒左右
```

这个比强行补到 136 秒自然得多。

---

# 十、最终 cut_plan 应该怎么生成？

AI 配音模式的最终 cut_plan 不应该继续以原始 shot target 为唯一依据，而应该以：

```text
TTS 实际时长 + shot 重要性 + 可裁剪范围
```

共同决定。

## 每个 shot 的最终画面时长建议

可以按这个规则：

```text
final_shot_duration = clamp(
    tts_actual_duration + visual_tail_padding,
    min_duration_seconds,
    max_duration_seconds
)
```

其中：

```text
visual_tail_padding = 0.3 - 1.5 秒
```

也就是画面可以比 TTS 稍微长一点，留给转场、呼吸、字幕收尾。

例如：

```text
shot 目标：28 秒
TTS 实际：17.8 秒
shot min：12 秒
shot max：30 秒

最终画面可以改成：
18.8 秒
```

不需要硬保留 28 秒。

## 但是核心画面不能裁太狠

如果是 `must` shot：

```text
最多裁到 min_duration_seconds
不能删
```

如果是 `optional` shot：

```text
可以裁短
可以删除
可以作为 B-roll 插入其他段落
```

---

# 十一、重试次数和防死循环

这个链路一定要控制重试次数，否则可能陷入：

```text
修文案 -> TTS -> 仍不匹配 -> 修文案 -> TTS -> 仍不匹配
```

建议设置最大轮次：

```text
voiceover_pre_tts_repair_max_rounds = 2
post_tts_reconcile_max_rounds = 2
```

完整策略：

```text
TTS 前最多修文案 2 次
TTS 后最多自动补救 2 次
超过后不再自动处理，进入 action_required
```

action_required 里告诉用户：

```text
当前画面 136 秒，TTS 92 秒。
系统已尝试修复/缩短，但仍不匹配。
建议：
1. 接受 95 秒版本
2. 生成长版解说
3. 重新选片
```

---

# 十二、建议引入三种用户可选策略

后续 Web 上可以给用户一个模式选择。

## 1. 自动平衡模式，推荐默认

```text
系统自动判断补文案还是缩画面。
优先保证自然、紧凑、不啰嗦。
```

适合大多数任务。

## 2. 保画面模式

```text
尽量保留规划画面。
TTS 不够时优先补文案。
```

适合画面很重要的新闻，比如现场冲突、灾害、发布会关键镜头。

## 3. 保文案节奏模式

```text
以 TTS 解说自然节奏为准。
TTS 多长，视频就裁到接近多长。
```

适合短视频平台，避免拖沓。

我建议默认用：

```text
自动平衡模式
```

并且在 AI 配音模式里偏向：

```text
文案完整即可，不强行凑长；
画面围绕 TTS 裁剪。
```

---

# 十三、关键产物设计

建议链路中新增或强化这些中间产物。

## 1. voiceover_timing_contract.json

职责：

```text
保存每个 shot 的时长预算、字符预算、重要性、裁剪策略、修文案策略。
```

用途：

```text
给 voiceover_script
给 voiceover_repair
给 tts_video_reconcile
给 cut_plan
```

## 2. voiceover_precheck.json

职责：

```text
TTS 前检查文案是否满足预算。
```

内容：

```text
每段字符数
估算时长
是否低于最低值
是否需要 repair
repair 目标
```

## 3. voiceover_repair_summary.json

职责：

```text
记录修复了哪些 shot。
```

内容：

```text
修复前字符数
修复后字符数
修复原因
是否达标
```

## 4. tts_video_reconcile.json

职责：

```text
TTS 后真实时长对齐诊断。
```

内容：

```text
目标画面时长
TTS 实际时长
差距
覆盖率
shot 级别差异
```

## 5. auto_duration_decision.json

职责：

```text
保存系统最终选择。
```

例如：

```json
{
  "decision": "shrink_video",
  "reason": "TTS only covers 67.5% of planned video; narration already covers key facts, video plan is too long for AI voiceover mode.",
  "target_video_seconds_before": 136.6,
  "tts_seconds": 92.2,
  "target_video_seconds_after": 101.5,
  "actions": [
    {
      "type": "trim_shot",
      "shot_id": "v_001_s01",
      "from": 28.3,
      "to": 18.8
    }
  ]
}
```

## 6. adjusted_cut_plan.json

职责：

```text
基于 TTS 真实时长调整后的最终剪辑计划。
```

---

# 十四、失败不应该直接失败，而是分级

现在 `tts_duration_mismatch` 是 failed。

优化后建议改成三类状态。

## 1. 可自动修复

```text
auto_repairing
```

系统自动补文案或缩画面。

## 2. 可继续但有提醒

```text
warning
```

轻微不匹配，但在可接受范围内，继续出片。

## 3. 需要用户确认

```text
action_required
```

例如：

```text
系统判断这条视频要做 130 秒长版，但你当前没有开启长版确认。
```

只有真正无法处理时才 failed：

```text
TTS 模型报错
没有生成音频
文案为空
cut_plan 无有效画面
素材时间码错误
```

`tts_duration_mismatch` 本身不应该默认是 failed，而应该先进入 auto_decision。

---

# 十五、你这次案例的理想处理流程

以你这次数据为例：

```text
画面目标：136.557 秒
TTS 实际：92.16 秒
coverage_ratio：0.675
```

系统应该这样走：

## Step 1：TTS 前文案预检

发现：

```text
文案估算时长可能低于目标
部分 shot 字符数偏少
```

先触发修复。

## Step 2：修复文案

补充：

```text
背景
关键事实
因果解释
各方表态
影响/风险
```

修完后假设估算变成 105 秒。

## Step 3：生成 TTS

真实 TTS 可能变成 103 秒。

## Step 4：TTS 后对齐

发现：

```text
画面 136 秒
TTS 103 秒
coverage 75%
仍偏短
```

## Step 5：自动决策

系统判断：

```text
补到 136 秒会啰嗦
画面规划偏长
应该缩画面
```

## Step 6：重建最终 cut_plan

把画面从 136 秒压到：

```text
105 - 110 秒
```

每个 shot 根据 TTS 真实时长重新分配。

## Step 7：继续字幕和渲染

最终输出一条 105 秒左右的 AI 配音解说视频。

---

# 十六、整体实施优先级

建议分三期做，不要一次性做太复杂。

## 第一期：先补 TTS 前强校验和文案修复

目标：

```text
明显文案偏短时，不再进入 TTS。
先修文案。
```

做到：

```text
文案低于 75%-85% 目标时长，强制 repair
repair 后再进入 TTS
```

这一步能解决一部分问题。

## 第二期：TTS 后不要直接失败，先自动缩画面

目标：

```text
TTS 真实偏短时，自动根据 TTS 重建 cut_plan。
```

做到：

```text
TTS 92 秒
画面 136 秒
自动把视频压到 95-105 秒
```

这是最关键的一期。

## 第三期：加入混合策略和重规划

目标：

```text
系统能判断补文案还是缩画面，或者二者结合。
```

做到：

```text
文案缺事实 -> 补文案
画面重复 -> 缩画面
严重偏差 -> 重规划
长版 -> 用户确认
```

---

# 十七、最终推荐方案总结

我建议最终方案是：

```text
1. AI 配音模式以 TTS 为主时间轴。
2. short_video_edit_plan 输出 shot 的 min/target/max，而不是固定时长。
3. voiceover_script 前引入字符预算合同。
4. TTS 前做强预检，明显偏短必须修文案。
5. TTS 后做真实时长 reconcile。
6. reconcile 不通过时不要直接失败，进入 auto_duration_decision。
7. auto_duration_decision 在补文案、缩画面、混合、重规划之间选择。
8. 最终 cut_plan 使用 TTS 真实时长重建画面节奏。
9. 只有无法自动处理时才 action_required 或 failed。
```

一句话概括：

```text
以前是“画面定死，TTS 不够就失败”。
优化后要变成“文案先按预算生成，TTS 出来后反向驱动画面收敛”。
```

这套方案确定后，第二步再进入代码设计时，就可以围绕这几个核心产物展开：

```text
voiceover_timing_contract.json
voiceover_precheck.json
tts_video_reconcile.json
auto_duration_decision.json
adjusted_cut_plan.json
```

这几个产物定住，后面具体开发会清晰很多。


下面给你一个**可落地的代码设计方案**。我会具体到“改哪里、职责怎么拆、函数怎么组织、产物怎么落盘、怎么保证鲁棒”，但不会写完整 patch。后面你确认后，再可以进入“逐文件代码开发指南”。

---

# 一、先定整体改造方向

当前代码里其实已经有一部分基础能力：

1. `voiceover_script` 阶段已有文案修复链路，核心入口是 `_finalize_voiceover_script_with_repair()`，里面会做 voiceover 对齐检查、收集时长问题、必要时 repair，再做长版确认。
2. `tts` 阶段已有真实时长对账 `_build_tts_duration_reconcile()`，它会把每个 shot 的 `target_duration_seconds` 和 TTS 真实时长对比，并计算整条 `coverage_ratio`。
3. 现在的问题是：`tts` 之后对账失败，直接把 `tts` 标成 failed，并抛 `UserFacingPipelineError`，没有进入自动补救。
4. Web 启动链路是 `run_web.py -> web_app.py -> run_pipeline.py -> newsclip_agent.pipeline.main`，Web 构造命令时会把 `--target-duration`、`--max-output-video-seconds`、`--allow-long-video`、`--require-tts` 等传给 pipeline。 

所以改造重点不是从零写，而是补三块：

```text
1. TTS 前：把 warning 级文案过短也纳入可修复逻辑。
2. TTS 后：不要直接 failed，先进入自动决策。
3. cut_plan 前：允许根据 TTS 真实时长重建画面时长。
```

---

# 二、建议新增/强化 4 个产物

这四个产物是整个链路能闭环的关键。

## 1. `voiceover_timing_contract.json`

位置建议：

```text
agents/voiceover_script/v*/voiceover_timing_contract.json
```

或者更通用一点：

```text
duration/voiceover_timing_contract.json
```

职责：

```text
把 short_video_edit_plan / editing_script 里的 shot 时长，转换成文案预算和裁剪预算。
```

每个 shot 至少包含：

```json
{
  "short_video_id": "v_001",
  "shot_id": "v_001_s01",
  "source_clip_id": "clip_001",

  "source_start_seconds": 0,
  "source_end_seconds": 28.3,
  "source_duration_seconds": 28.3,

  "min_duration_seconds": 12.0,
  "target_duration_seconds": 18.0,
  "max_duration_seconds": 28.3,

  "narration_min_chars": 70,
  "narration_target_chars": 95,
  "narration_max_chars": 125,

  "importance": "must",
  "can_trim": true,
  "repair_priority": "medium",
  "trim_priority": "medium"
}
```

注意：你当前代码里已经有一部分字段，比如 `target_duration_seconds`、`min_duration_seconds`、`max_duration_seconds`、`narration_min_chars` 这类东西已经在使用。TTS 对账阶段也已经用 `min_duration_seconds / max_duration_seconds` 做每段校验。
所以这里不是完全新增概念，而是把它正式化、落盘化、全链路复用。

---

## 2. `voiceover_precheck.json`

位置建议：

```text
agents/voiceover_script/v*/voiceover_precheck.json
```

职责：

```text
TTS 前检查文案是否够长。
```

它应该记录：

```json
{
  "ok": false,
  "status": "needs_repair",
  "target_total_duration_seconds": 136.557,
  "estimated_total_duration_seconds": 102.3,
  "estimated_ratio": 0.749,
  "segments": [
    {
      "shot_id": "v_001_s01",
      "target_duration_seconds": 28.301,
      "chars": 95,
      "min_chars": 107,
      "target_chars": 135,
      "estimated_duration_seconds": 19.8,
      "status": "too_short",
      "severity": "repair"
    }
  ]
}
```

这一步要解决你这次的问题：`voiceover_alignment_check.json` 里 `ok=true`，但有 `char_budget_warnings`，所以没有触发修复。

你上传的实际产物里就是：

```json
{
  "ok": true,
  "char_budget_warnings": [
    {
      "shot_id": "v_001_s01",
      "chars": 95,
      "min_chars": 107,
      "severity": "warning"
    },
    {
      "shot_id": "v_001_s04",
      "chars": 99,
      "min_chars": 115,
      "severity": "warning"
    }
  ]
}
```

这种以后不能只当 warning，要根据全片估算比例决定是否升级为 repair。

---

## 3. `tts_video_reconcile.json`

你现在已有：

```text
tts_duration_reconcile.json
```

可以保留这个名字，也可以新增一个更明确的：

```text
tts_video_reconcile.json
```

我建议先复用现有 `tts_duration_reconcile.json`，不要额外增加太多文件。

但要增强它的用途：它不再只是“判断失败的证据”，而是自动决策的输入。

你这次实际数据是：

```json
{
  "ok": false,
  "target_total_duration_seconds": 136.557,
  "tts_total_duration_seconds": 92.16,
  "coverage_ratio": 0.675,
  "total_check": {
    "status": "too_short",
    "hard": true
  }
}
```

这应该进入下一步 `auto_duration_decision`，而不是直接 failed。

---

## 4. `auto_duration_decision.json`

位置建议：

```text
duration/auto_duration_decision.json
```

或者：

```text
tts/omnivoice/v*/auto_duration_decision.json
```

职责：

```text
记录 TTS 后不匹配时，系统选择了什么补救策略。
```

示例：

```json
{
  "decision": "shrink_video",
  "reason": "TTS covers only 67.5% of planned video. Planned video is too long for AI voiceover compact mode.",
  "target_video_seconds_before": 136.557,
  "tts_seconds": 92.16,
  "target_video_seconds_after": 101.5,
  "repair_voiceover": false,
  "rerun_tts": false,
  "rebuild_cut_plan": true,
  "segments": [
    {
      "shot_id": "v_001_s01",
      "before_target_seconds": 28.301,
      "tts_actual_seconds": 17.84,
      "after_target_seconds": 18.84,
      "action": "trim_to_tts"
    }
  ]
}
```

后面 `cut_plan` 根据这个文件调整画面长度。

---

# 三、具体改造点总览

建议按下面这些文件/模块改。

```text
config.toml
newsclip_agent/pipeline.py
newsclip_agent/duration_policy.py
newsclip_agent/prompts.py
web_app.py
newsclip_agent/workflow_registry.py
```

其中最核心是：

```text
newsclip_agent/pipeline.py
newsclip_agent/duration_policy.py
```

---

# 四、config.toml 要新增一组策略配置

在 `[voiceover]` 下新增或者强化这些配置。

```toml
[voiceover]
# TTS 前文案预检
pre_tts_repair_enabled = true
pre_tts_repair_warning_to_repair = true
pre_tts_estimated_ok_min_ratio = 0.90
pre_tts_estimated_repair_min_ratio = 0.75
pre_tts_estimated_hard_min_ratio = 0.65

# TTS 后自动补救
post_tts_auto_reconcile_enabled = true
post_tts_max_repair_rounds = 1
post_tts_max_adjust_rounds = 1

# TTS 后决策阈值
post_tts_ok_min_ratio = 0.90
post_tts_shrink_min_ratio = 0.82
post_tts_hybrid_min_ratio = 0.70
post_tts_severe_short_ratio = 0.70

# 默认策略
duration_reconcile_strategy = "auto_balance"
# 可选：auto_balance / prefer_voiceover_repair / prefer_video_shrink

# AI配音最终视频裁剪
tts_driven_cut_plan = true
tts_visual_tail_padding_seconds = 1.0
tts_visual_head_padding_seconds = 0.0
tts_min_visual_padding_seconds = 0.3
tts_max_visual_padding_seconds = 1.5

# 安全限制
max_voiceover_expansion_ratio = 1.25
max_video_shrink_ratio = 0.65
min_final_video_seconds = 30
```

这些配置的作用：

```text
pre_tts_*：控制 TTS 前是否修文案。
post_tts_*：控制 TTS 后是否自动补救。
duration_reconcile_strategy：决定补文案优先还是缩视频优先。
tts_driven_cut_plan：决定 cut_plan 是否以真实 TTS 为主。
```

鲁棒性考虑：

```text
1. 所有配置都要有默认值，不依赖 toml 一定存在。
2. 老任务没有这些字段也能继续跑。
3. 如果自动补救出错，不要污染已有 voiceover_script/tts 原产物，写新版本或新产物。
```

---

# 五、pipeline.py 里建议新增的核心函数

## 1. 新增 `_build_voiceover_timing_contract()`

位置建议：

```text
PipelineRunner 中，靠近 voiceover 相关函数
```

输入：

```text
editing_script
short_video_edit_plan
duration_settings
```

输出：

```text
contract dict
```

职责：

```text
从 editing_structure 里读取每个 shot 的 duration_seconds。
补齐 min/target/max。
补齐 narration_min/target/max_chars。
补齐 can_trim / importance / repair_priority / trim_priority。
```

伪逻辑：

```python
def _build_voiceover_timing_contract(self, edit_plan):
    for script in edit_plan["scripts"]:
        for shot in script["editing_structure"]:
            source_duration = shot.duration_seconds
            target = shot.target_duration_seconds or shot.duration_seconds

            min_d = shot.min_duration_seconds or target * 0.65
            max_d = shot.max_duration_seconds or min(source_duration, target * 1.35)

            min_chars = target * voiceover_min_chars_per_second
            target_chars = target * voiceover_chars_per_second
            max_chars = target * voiceover_max_chars_per_second

            importance = infer_importance(shot)
            can_trim = infer_can_trim(shot)

            contract.add(...)
```

这里要注意：

```text
如果 shot 原始 source_duration 小于 target，要以 source_duration 为上限。
如果 min > max，要自动修正。
如果 target <= 0，回退到 duration_seconds。
如果 duration 缺失，不直接崩，记录 warning，并跳过该 shot 或给保守默认值。
```

这一步可以放在 `voiceover_script` 阶段生成前，也可以放在 `_finalize_voiceover_script_with_repair()` 里。我的建议：

```text
先放在 _finalize_voiceover_script_with_repair() 内部使用，改动小。
稳定后再独立成 step。
```

---

## 2. 新增 `_precheck_voiceover_duration_budget()`

位置：

```text
pipeline.py，voiceover repair 相关函数附近
```

输入：

```text
final_output  # voiceover_script
edit_plan
contract
```

输出：

```text
precheck dict
```

职责：

```text
TTS 前检查每个 shot 文案是否足够。
```

核心判断：

```python
estimated_duration = chars / voiceover_chars_per_second
segment_ratio = estimated_duration / target_duration
total_ratio = estimated_total / target_total
```

分级：

```text
ratio >= 0.90：ok
0.75 <= ratio < 0.90：repair
0.65 <= ratio < 0.75：hard_repair
< 0.65：severe_short
```

鲁棒性：

```text
空 text：hard issue
缺 shot_id：hard issue
多余 shot_id：hard issue
字符预算缺失：用 target_duration * cps 现算
中文标点、空白：统一清理后计数
```

---

## 3. 修改 `_finalize_voiceover_script_with_repair()`

这是第一个关键修改点。

当前逻辑大致是：

```text
normalize
sync narration_text
attach timings
alignment check
collect duration issues
merge into alignment
如果 merged_alignment.ok=false 且 repair_enabled，则 repair
最后 validate
```

你要改成：

```text
normalize
sync narration_text
attach timings
build timing contract
precheck voiceover budget
把 precheck 的 repair 级问题合并进 alignment
如果 alignment 有 warning 但 total_ratio 低，也升级成 repair
repair
repair 后重新 precheck
仍不通过再决定是否继续或 action_required
```

关键点是：**不能只看 `merged_alignment.ok`，还要看 precheck 是否要求修复。**

伪流程：

```python
contract = self._build_voiceover_timing_contract(edit_plan)
write_json(vdir / "voiceover_timing_contract.json", contract)

precheck = self._precheck_voiceover_duration_budget(final_output, edit_plan, contract)
write_json(vdir / "voiceover_precheck.json", precheck)

need_repair = (
    not merged_alignment.get("ok")
    or precheck["status"] in {"needs_repair", "hard_repair"}
)

if need_repair and repair_enabled:
    repair...
```

同时 `_check_is_text_patch_repairable()` 要能识别 precheck 产生的问题。

你现在 `_check_is_text_patch_repairable()` 主要看：

```text
char_budget_issues
empty_text_shot_ids
repair_on_warnings 时才看 char_budget_warnings
```

这里要改成：

```text
如果 precheck 把 warning 升级为 repair，则进入 char_budget_issues，而不是还放在 warnings。
```

这样不用依赖 `repair_on_warnings=true`，避免把所有轻微 warning 都修一遍。

---

# 六、TTS 后自动决策怎么写

## 1. 修改 `_step_tts_impl()`

当前 `_step_tts_impl()` 在生成 TTS 后：

```text
reconcile = _build_tts_duration_reconcile(...)
reconcile_ok = ...
reconcile_hard_segments = ...
reconcile_hard_videos = ...

如果 hard mismatch -> overall_status = failed
然后 raise UserFacingPipelineError
```

这个地方要改。

不要马上 failed，而是：

```text
如果 TTS 模型本身失败、没有音频、缺 segment：
    仍然 failed

如果只是 duration mismatch：
    调用 _decide_post_tts_duration_action()
```

也就是说，区分两类失败：

```text
真正 TTS 失败：
- no output
- segment missing
- model error
- audio file not exists

时长不匹配：
- TTS 成功了
- 只是短了/长了
```

你这次属于第二类，不能直接失败。

伪逻辑：

```python
if tts_is_hard_required and model_failed:
    raise UserFacingPipelineError("tts_failed")

if tts_is_hard_required and duration_mismatch:
    decision = self._decide_post_tts_duration_action(editing, voiceover, outputs, reconcile)
    write_json(... auto_duration_decision.json ...)

    if decision["decision"] == "repair_voiceover":
        mark action for rerun voiceover/tts
    elif decision["decision"] in {"shrink_video", "continue_with_adjusted_cut_plan"}:
        overall_status = "success"
    elif decision["decision"] == "action_required":
        manifest action_required
    else:
        failed
```

鲁棒性要求：

```text
1. 只要 TTS 音频存在且可读，duration mismatch 不应归类为 TTS 生成失败。
2. 不要把 tts step 标 failed，否则后续 cut_plan/subtitle 无法继续。
3. 可以把 tts step 标 success_with_duration_adjustment，或者 status 仍 success，extra 里记录 decision。
```

当前 manifest/status 可能只支持 success/failed/skipped/partial_success，不建议贸然加太多新状态。稳妥做法：

```text
tts step status = success
extra.duration_reconcile.status = adjusted / needs_adjustment
auto_duration_decision 单独记录
```

这样兼容现有 Web。

---

## 2. 新增 `_decide_post_tts_duration_action()`

输入：

```text
editing
voiceover
tts_outputs
reconcile
```

输出：

```json
{
  "decision": "shrink_video",
  "rebuild_cut_plan": true,
  "rerun_tts": false,
  "repair_voiceover": false,
  "reason": "...",
  "adjusted_segments": []
}
```

决策逻辑建议：

```python
ratio = reconcile.coverage_ratio

if ratio >= post_tts_ok_min_ratio:
    decision = "continue"

elif ratio >= post_tts_shrink_min_ratio:
    decision = "shrink_video"

elif ratio >= post_tts_hybrid_min_ratio:
    if voiceover_has_missing_core_facts or script_too_thin:
        decision = "repair_voiceover"
    else:
        decision = "shrink_video"

else:
    if allow_long_video and strategy == "prefer_voiceover_repair":
        decision = "repair_voiceover"
    else:
        decision = "shrink_video"
```

你这次 ratio = 0.675，小于 0.70。默认应该：

```text
decision = shrink_video
```

因为强行补到 136 秒风险高。

---

## 3. 新增 `_build_adjusted_tts_driven_editing_plan()`

这一步负责把：

```text
原 editing_structure
TTS 每段真实时长
timing contract
auto decision
```

合成一个新的 editing plan。

产物建议：

```text
duration/adjusted_editing_script.json
```

或者：

```text
edit/adjusted_editing_script.json
```

职责：

```text
把每个 shot 的 target_duration_seconds 调整成接近 TTS 实际时长。
```

核心公式：

```text
new_duration = clamp(
  tts_actual_duration_seconds + tail_padding,
  min_duration_seconds,
  max_duration_seconds
)
```

但是你这次有一个坑：

```text
s01 target 28.301
min 18.396
TTS 17.84
加 1 秒 = 18.84，刚好可用

s04 target 30.281
min 19.683
TTS 17.88
加 1 秒 = 18.88，小于 min
这时应该取 min 19.683
```

调整后总时长会大概从 136 秒压到 100 秒左右。

鲁棒性：

```text
如果某段 TTS 缺失，不调整该段，保留原 target，但记录 warning。
如果 adjusted_total 低于 min_final_video_seconds，不做过度缩短。
如果 must shot 被裁得太狠，至少保留 min_duration_seconds。
如果 optional shot 的 TTS 很短且画面重复，可以允许删除，但第一期先不要做删除，只做裁剪，更稳。
```

---

# 七、cut_plan 要怎么接这个 adjusted plan

## 当前问题

`step_cut_plan` 你没让我继续完整追踪，但从现有代码看，`_build_clips_from_editing_structure()` 会根据 `editing_structure` 生成 clips，而且它读取：

```text
target_duration_seconds or duration_seconds
```

并用这个作为最终裁剪时长。

所以最小改法是：

```text
在 cut_plan 构建前，让它优先读取 adjusted_editing_script。
```

不要大改 render。

## 建议设计

新增一个函数：

```python
def _load_effective_editing_for_cut_plan(self):
    adjusted = self._load_optional_json("duration/adjusted_editing_script.json")
    if adjusted exists and enabled:
        return adjusted
    return self._load_step_json("editing_script")
```

然后 `step_cut_plan` 里原本读取 `editing_script` 的地方，替换成：

```text
effective_editing = _load_effective_editing_for_cut_plan()
```

这样：

```text
原始 editing_script 不被覆盖
adjusted_editing_script 作为后处理版本
cut_plan 只在需要时使用 adjusted 版本
```

鲁棒性好，回滚也简单。

---

# 八、是否要自动补文案并重跑 TTS？

第一期我建议**不要先做复杂的 post-TTS 自动补文案重跑**，先做“自动缩画面”。原因：

```text
1. post-TTS 补文案需要重新调用大模型，再重跑 TTS，链路复杂。
2. 容易死循环。
3. 你的这次案例更像画面规划偏长，而不是文案必须补到 136 秒。
4. AI 配音短视频应以 TTS 节奏为主，缩画面更稳。
```

所以实施优先级：

```text
第一阶段：
TTS 前文案预检 + 修复 warning 升级
TTS 后不直接失败
TTS 后自动生成 adjusted_editing_script
cut_plan 使用 adjusted_editing_script

第二阶段：
再做 post-TTS repair_voiceover + rerun TTS
```

---

# 九、需要重点改的函数清单

## 1. `newsclip_agent/pipeline.py`

### A. `_finalize_voiceover_script_with_repair()`

改造点：

```text
加入 timing contract
加入 voiceover_precheck
precheck 失败时触发 repair
repair 后重新 precheck
precheck 仍严重失败时，不进入 TTS
```

### B. `_check_is_text_patch_repairable()`

改造点：

```text
支持 precheck 升级出来的 duration_budget_issues
不要只依赖 char_budget_issues
```

### C. `_build_voiceover_repair_text_input()`

改造点：

```text
把 target_chars、min_chars、current_chars、target_duration_seconds、estimated_duration_seconds 明确放进去
告诉模型“补到多少字”，不要泛泛修复
```

当前这个函数已经会把 problem_segments 放进 prompt。
后面只需要增强 problem_segments 信息。

### D. `_step_tts_impl()`

改造点：

```text
duration mismatch 不再直接 failed
先调用 post-TTS decision
如果 decision 是 shrink_video，则 tts step 成功
写 auto_duration_decision.json
写 adjusted_editing_script.json
```

### E. `_build_tts_duration_reconcile()`

改造点：

```text
保留现有逻辑
增强输出字段：
- decision_hint
- gap_seconds
- can_shrink
- can_repair_voiceover
- severity
```

这个函数目前已经输出每段和整条的 ratio、status、hard。
不需要推倒重写。

### F. 新增 `_decide_post_tts_duration_action()`

职责：

```text
根据 reconcile 决策 continue / shrink_video / repair_voiceover / action_required。
```

### G. 新增 `_build_adjusted_editing_script_from_tts()`

职责：

```text
基于 TTS 实际时长重写每个 shot 的 target_duration_seconds/source_end。
```

注意这里不要改 `source_start`，只改 `source_end`：

```text
new_source_end = source_start + new_duration
```

并且不能超过原始 `source_max_end_seconds` 或原 `source_end_seconds`。

### H. `step_cut_plan`

改造点：

```text
读取 editing_script 时，改成读取 effective_editing。
如果 adjusted_editing_script 存在并且 auto_duration_decision.rebuild_cut_plan=true，就用 adjusted。
```

---

## 2. `newsclip_agent/duration_policy.py`

这里适合放纯策略函数，避免 pipeline.py 越来越大。

建议新增：

```python
class DurationDecision:
    ...
```

或者先不用 dataclass，直接 dict。

建议放这些纯函数：

```text
classify_duration_ratio()
build_duration_budget()
decide_post_tts_action()
adjust_segment_duration_to_tts()
```

好处：

```text
1. 便于单元测试。
2. 不依赖 PipelineRunner。
3. 后续可以复用给多源模式。
```

---

## 3. `newsclip_agent/prompts.py`

需要增强 `VOICEOVER_REPAIR_TEXT_PROMPT`。

重点不是让模型自由扩写，而是要求：

```text
只修复 listed problem_segments
每段输出 repaired_segments
必须接近 target_chars
不能编造
不能重复
不能增加不存在事实
保持新闻口吻
```

修复 prompt 输入里要有：

```text
current_chars
min_chars
target_chars
max_chars
target_duration_seconds
current_text
visual_summary
fact
narration_intent
must_keep_fact_points
```

---

## 4. `web_app.py`

第一期可以不动 Web。

因为 Web 当前只是构造命令和显示步骤。命令构造在 `_build_run_command()`，已经会传 `--require-tts`、`--allow-long-video` 等参数。

如果后面想在 Web 上显示更清楚，再加：

```text
auto_duration_decision.json 预览
“系统已自动缩短画面到 xx 秒”
```

但第一期不必改。

---

# 十、关键鲁棒性设计

## 1. 不覆盖原始产物

不要直接覆盖：

```text
editing_script.json
voiceover_script.json
tts_outputs.json
```

建议新增：

```text
adjusted_editing_script.json
auto_duration_decision.json
voiceover_precheck.json
```

这样出问题可以回看原始数据。

---

## 2. 区分“模型失败”和“时长不匹配”

这个很重要。

TTS step 失败只应该用于：

```text
TTS 没有生成音频
音频文件不存在
某个 required segment 没生成
模型报错
```

时长不匹配应该是：

```text
duration_adjusted
duration_action_required
duration_warning
```

不要混成 `tts_failed_or_empty`。

---

## 3. 防止无限重试

新增 manifest 记录：

```json
{
  "duration_reconcile_round": 1,
  "voiceover_repair_round": 1,
  "post_tts_adjust_round": 1
}
```

如果超过配置：

```text
post_tts_max_adjust_rounds
post_tts_max_repair_rounds
```

就进入 `action_required`，不要继续自动跑。

---

## 4. adjusted duration 不能越界

每个 shot 新时长必须满足：

```text
new_duration >= min_duration_seconds
new_duration <= max_duration_seconds
new_duration <= original_source_duration_seconds
```

如果做不到：

```text
保留原时长
记录 warning
不要直接崩
```

---

## 5. 多源任务 source_id 不能丢

调整 `editing_structure` 时必须保留：

```text
source_id
source_index
source_clip_id
local_start
local_end
source_start
source_end
source_start_seconds
source_end_seconds
```

因为后面 `_build_clips_from_editing_structure()` 和渲染依赖 source/time 字段。

---

## 6. 只裁尾部，先不动开头

第一期建议：

```text
source_start 不变
只缩短 source_end
```

不要做复杂的中间裁剪。因为新闻片段通常开头更容易承接上下文，动 start 风险更大。

---

## 7. 保底策略

如果自动缩画面后总时长仍然和 TTS 差太多：

```text
final_video / tts < 0.85 或 > 1.20
```

不要继续硬跑，进入 `action_required`。

---

# 十一、你这次案例按新代码会怎么走

你的数据：

```text
目标画面：136.557
TTS：92.16
ratio：0.675
```

新逻辑应该是：

```text
1. voiceover_precheck 阶段：
   s01/s04 warning 会被升级为 repair。
   如果总估算不足，也触发 repair。

2. repair 后进入 TTS。

3. TTS 后 reconcile：
   如果仍然是 0.675 左右，判定 severe_short。

4. auto_duration_decision：
   默认 auto_balance 下选择 shrink_video。

5. build_adjusted_editing_script：
   每段按 TTS 实际 + 1s padding 调整。
   s01：28.3 -> 18.8
   s02：22.1 -> 16.9
   s03：15.9 -> 13.0
   s04：30.3 -> 19.7
   s05：24.0 -> 18.0
   s06：16.0 -> 12.6

6. adjusted 总时长大约 99 秒左右。

7. cut_plan 使用 adjusted_editing_script。

8. render 输出 99 秒左右的视频。
```

这比直接失败合理很多。

---

# 十二、推荐开发顺序

## 第 1 步：只做 TTS 前文案预检增强

目标：

```text
warning 不再轻易放过。
```

改：

```text
_finalize_voiceover_script_with_repair()
_build_voiceover_repair_text_input()
_check_is_text_patch_repairable()
```

产物：

```text
voiceover_precheck.json
voiceover_timing_contract.json
```

测试：

```text
用你这次任务，从 voiceover_script 重跑。
确认 s01/s04 会进入 repair。
```

---

## 第 2 步：TTS 后 duration mismatch 不直接 failed

目标：

```text
TTS 成功但时长偏短时，不标记 TTS failed。
```

改：

```text
_step_tts_impl()
_build_tts_duration_reconcile()
新增 _decide_post_tts_duration_action()
```

产物：

```text
auto_duration_decision.json
```

测试：

```text
用你这次任务，从 tts 重跑。
确认 tts step 不再 failed。
```

---

## 第 3 步：生成 adjusted_editing_script

目标：

```text
根据 TTS 真实时长压缩画面。
```

改：

```text
新增 _build_adjusted_editing_script_from_tts()
```

产物：

```text
adjusted_editing_script.json
```

测试：

```text
确认 136 秒被压到约 95-105 秒。
确认每个 shot source_start/source_end 合法。
```

---

## 第 4 步：cut_plan 读取 adjusted_editing_script

目标：

```text
后续剪辑真正使用调整后的时长。
```

改：

```text
step_cut_plan 或其读取 editing_script 的函数
新增 _load_effective_editing_for_cut_plan()
```

测试：

```text
从 cut_plan 重跑。
确认 cut_plan 输出 clips 的总 duration 接近 TTS。
```

---

## 第 5 步：Web 显示优化，非必须

目标：

```text
用户能看到“系统已自动缩短画面”。
```

改：

```text
web_app.py 文件树/状态显示不用大改。
前端只要能打开 auto_duration_decision.json 即可。
```

---

# 十三、回滚方案

必须设计好回滚。

所有新逻辑都受配置控制：

```toml
pre_tts_repair_enabled = false
post_tts_auto_reconcile_enabled = false
tts_driven_cut_plan = false
```

关闭后回到旧逻辑。

另外：

```text
不覆盖原 editing_script
不覆盖原 voiceover_script
不覆盖原 tts_outputs
```

所以即使 adjusted 出问题，也能删除 adjusted 文件后回到旧结果。

---

# 十四、最终推荐的代码架构

我建议最后形成这个结构：

```text
pipeline.py
  _finalize_voiceover_script_with_repair()
  _build_voiceover_timing_contract()
  _precheck_voiceover_duration_budget()
  _decide_post_tts_duration_action()
  _build_adjusted_editing_script_from_tts()
  _load_effective_editing_for_cut_plan()

duration_policy.py
  classify_duration_ratio()
  compute_char_budget()
  clamp_adjusted_duration()
  decide_duration_action()

prompts.py
  VOICEOVER_REPAIR_TEXT_PROMPT 增强
```

最重要的是这条新主链路：

```text
voiceover_script
  -> voiceover_timing_contract
  -> voiceover_precheck
  -> voiceover_repair
  -> tts
  -> tts_duration_reconcile
  -> auto_duration_decision
  -> adjusted_editing_script
  -> cut_plan
  -> render
```

这样才是真正完成你说的：

```text
画面目标 136 秒
文案估算不够 -> 先修文案
TTS 真实仍不够 -> 自动补救
要么补文案
要么缩画面
要么混合策略
```

我的建议是第一版优先实现：

```text
TTS 前修文案 + TTS 后缩画面
```

不要第一版就做“自动补文案重跑 TTS”，那个复杂度更高，容易引入死循环。先把这次 `136 秒 vs 92 秒` 的问题稳定解决。
