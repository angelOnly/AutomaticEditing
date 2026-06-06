对，这个思路需要优化。你现在看到的问题，本质不是“AI 文案写得不够准”，而是 **AI 配音模式的架构还没有形成一个稳定的闭环**。

现在链路像这样：

```text
候选片段 → AI 写解说 → TTS 出真实时长 → 视频被迫适配 TTS
```

这个链路能跑，但不够稳。更合理的架构应该是：

```text
候选片段池
→ 成片故事规划
→ 画面 shot 计划
→ 每个 shot 分配时长预算
→ 根据时长预算生成 AI 配音文案
→ TTS
→ 检查 TTS 实际时长和画面预算是否匹配
→ 不匹配就修复：改文案 / 调画面 / 换片 / 补片 / 轻微变速
→ 最终 cut_plan
→ 渲染
```

核心变化是：**不能只让视频迁就 TTS，也不能只让文案迁就画面，而是要让“画面计划”和“配音计划”共同收敛。**

---

# 一、先明确当前 AI 配音模式为什么容易错

现在你的工程和你分析的 NarratoAI 都有一个共同问题：

```text
文案生成阶段和最终 TTS 时长之间没有强闭环。
```

大模型写文案时只能估算：

```text
这句话大概 5 秒
这段大概 10 秒
整条大概 60 秒
```

但真实 TTS 出来以后，可能完全不是这个长度。

例如：

```text
画面计划：8 个 shot，总共 60 秒
AI 文案估算：58 秒
TTS 实际：39 秒
最终视频：被压到 39 秒
```

或者：

```text
候选片段总长：275 秒
AI 文案实际只够 45 秒
cut_plan 还拿 275 秒当目标
最后报时长严重不足
```

你之前那个 0 秒问题更极端：代码在 `cut_plan` 阶段会根据 `editing_structure` 构造 clips，如果时间字段无效，clip 会被跳过；如果全被跳过，最终就变成 0 秒。

所以根因不是单纯“文案短”，而是这几套东西没有稳定绑定：

```text
候选片段时间
剪辑计划 shot
AI 解说 segment
TTS segment
最终 cut_plan clip
```

---

# 二、AI 配音模式应该重新定义成“shot contract”架构

我建议引入一个核心概念：

```text
shot contract / 镜头契约
```

每个最终成片镜头都必须有一个稳定 ID，并且贯穿后续所有环节。

例如：

```json
{
  "shot_id": "v_001_s03",
  "source_id": "source_1",
  "source_start": "00:01:20.000",
  "source_end": "00:01:35.000",
  "min_duration_seconds": 4.0,
  "target_duration_seconds": 7.0,
  "max_duration_seconds": 12.0,
  "visual_summary": "特朗普在白宫回答记者提问",
  "narration_role": "解释关键矛盾",
  "allow_extend": true,
  "allow_trim": true,
  "fallback_source_ids": []
}
```

这个东西就是后面所有步骤的主键。

后面 AI 文案不能随便自己发明 shot_id，只能基于这个：

```json
{
  "shot_id": "v_001_s03",
  "target_duration_seconds": 7.0,
  "text": "特朗普一边推动停火框架，一边又面临国内外压力。"
}
```

TTS 输出也必须继承这个：

```json
{
  "shot_id": "v_001_s03",
  "audio_file": "...",
  "actual_duration_seconds": 6.4
}
```

最终 cut_plan 也还是它：

```json
{
  "shot_id": "v_001_s03",
  "source_id": "source_1",
  "source_start": "00:01:20.000",
  "source_end": "00:01:26.400",
  "target_start": 18.3,
  "duration_seconds": 6.4
}
```

这样系统就不会乱。

---

# 三、整体链路应该改成 8 个阶段

## 阶段 1：公共分析只负责“素材事实”，不要决定成片

公共分析应该产出：

```text
ASR 文本
视觉 chunk
人物 / 地点 / 事件
画面类型
可用性标签
素材时间范围
```

这一阶段不要急着生成最终文案。

它的目标是建立素材池：

```text
哪些画面能用
哪些画面不能用
哪些画面适合开头
哪些画面适合解释背景
哪些画面适合做冲突点
哪些画面只是过渡
```

---

## 阶段 2：候选片筛选只负责“可用片段池”

候选片段不要直接等于最终视频。

候选片段应该像这样：

```json
{
  "candidate_id": "clip_001",
  "source_id": "source_1",
  "start": "00:00:35.000",
  "end": "00:00:52.000",
  "duration_seconds": 17,
  "visual_value": "high",
  "content_value": "high",
  "usage": "voiceover",
  "role": "conflict_intro",
  "summary": "特朗普谈停火条件"
}
```

它只是告诉系统：

```text
这段素材值得进入成片规划。
```

但它不是最终剪辑计划。

---

## 阶段 3：先做“成片故事规划”，不要直接写完整文案

这里应该让大模型先回答：

```text
这条 AI 解说视频讲什么？
分几段？
开头怎么抓人？
中间解释什么？
结尾落在哪个判断上？
目标时长是多少？
```

输出类似：

```json
{
  "video_id": "v_001",
  "target_duration_seconds": 60,
  "angle": "特朗普推动停火协议，但现实压力让协议陷入两难",
  "structure": [
    {
      "section": "hook",
      "target_seconds": 8,
      "purpose": "抛出核心矛盾"
    },
    {
      "section": "context",
      "target_seconds": 18,
      "purpose": "解释协议框架"
    },
    {
      "section": "conflict",
      "target_seconds": 24,
      "purpose": "说明各方压力"
    },
    {
      "section": "ending",
      "target_seconds": 10,
      "purpose": "收束判断"
    }
  ]
}
```

这里不要写最终配音稿。

因为一上来就写完整文案，很容易写飞、写长、写短，后面难修。

---

## 阶段 4：根据故事规划生成 shot plan

这一步才从候选片里选最终画面。

输出应该是 `editing_structure`，也就是最终画面计划：

```json
{
  "editing_structure": [
    {
      "shot_id": "v_001_s01",
      "candidate_id": "clip_003",
      "source_id": "source_1",
      "source_start": "00:00:12.000",
      "source_end": "00:00:20.000",
      "target_duration_seconds": 8,
      "min_duration_seconds": 5,
      "max_duration_seconds": 10,
      "section": "hook",
      "visual_summary": "现场新闻标题与冲突画面",
      "narration_intent": "引出美伊边谈边打的矛盾"
    }
  ]
}
```

这个阶段的重点是：

```text
先定画面，再定每个画面的目标时长。
```

不是先写文案。

---

## 阶段 5：代码层计算每段文案字数预算

这一层不要完全交给大模型，应该由代码确定。

例如中文新闻配音可以先按：

```text
普通语速：4.5 - 5.5 字/秒
偏快短视频：5.5 - 6.5 字/秒
严肃新闻：4.2 - 5.0 字/秒
```

假设某个 shot 目标 8 秒，可以给：

```text
最少：28 字
目标：40 字
最多：50 字
```

每个 shot 传给大模型时应该带上：

```json
{
  "shot_id": "v_001_s01",
  "target_duration_seconds": 8,
  "min_chars": 28,
  "target_chars": 40,
  "max_chars": 50,
  "visual_summary": "现场新闻标题与冲突画面",
  "narration_intent": "引出美伊边谈边打的矛盾"
}
```

这样大模型不是自由发挥，而是在一个明确的预算里写。

---

## 阶段 6：生成 AI 配音文案时，必须强约束 shot_id

文案 prompt 的职责应该变窄。

不要让它同时做：

```text
选片
重组
写文案
决定时长
决定画面
```

它只做：

```text
根据已有 shot plan，为每个 shot 写对应配音文本。
```

提示词应该要求：

```text
1. 不允许新增 shot_id
2. 不允许删除 shot_id
3. narration_segments 数量必须等于 editing_structure 数量
4. 每段 text 必须贴合 visual_summary 和 narration_intent
5. 每段字数必须在 min_chars/max_chars 之间
6. 每句话短，适合字幕
7. 不写画面中没有依据的事实
8. 不重复上一段内容
```

输出：

```json
{
  "video_id": "v_001",
  "narration_text": "...",
  "narration_segments": [
    {
      "shot_id": "v_001_s01",
      "text": "美伊一边谈判，一边仍在战场和制裁压力中互相试探。",
      "estimated_duration_seconds": 7.2
    }
  ]
}
```

这样后面才能稳定对齐。

---

## 阶段 7：TTS 后做“时长 reconciliation”，不是直接裁视频

这是最关键的一步。

TTS 出来以后，不要立刻：

```text
视频终点 = 起点 + TTS 实际时长
```

而是先做一个对齐判断。

每个 shot 有三个时长：

```text
visual_available_duration：原始可用画面时长
target_duration_seconds：计划时长
tts_actual_duration_seconds：真实配音时长
```

然后按规则处理。

### 情况 A：TTS 在合理范围内

例如：

```text
shot 目标：8 秒
允许范围：5 - 10 秒
TTS 实际：7.4 秒
```

直接让画面适配 TTS：

```text
最终画面 = 7.4 秒
```

这是合理的。

---

### 情况 B：TTS 比画面略短

例如：

```text
目标 8 秒
TTS 实际 5.5 秒
```

可以裁短画面：

```text
source_start 不变
source_end = source_start + 5.5
```

这也合理。

---

### 情况 C：TTS 比画面略长，但不超过 max_duration

例如：

```text
source 原片段 8 秒
max_duration 12 秒
TTS 实际 10 秒
```

可以延展画面：

```text
source_end = source_start + 10
```

但必须满足：

```text
不能超过 source 原视频长度
不能跨到禁用画面
不能跨到不同语义段太远
```

---

### 情况 D：TTS 明显太长

例如：

```text
shot 可用画面最多 8 秒
TTS 实际 15 秒
```

这时候不能硬裁到 15 秒。

应该进入修复策略：

```text
优先缩写该段文案
或者拆成两个 shot
或者从候选池补一个相关 B-roll
或者轻微加速 TTS
```

推荐顺序是：

```text
1. 文案缩写
2. 补充画面
3. 拆分 shot
4. 轻微 TTS 变速
5. 最后才允许画面越界
```

---

### 情况 E：TTS 明显太短

例如：

```text
目标 10 秒
TTS 实际 3 秒
```

不能简单把画面压成 3 秒，因为新闻短视频会变得很碎。

应该修复：

```text
1. 扩写该段文案
2. 合并相邻 shot
3. 延长画面并允许少量无配音停顿
4. 或者删除这个 shot
```

---

# 四、视频和 TTS 的匹配方式应该从“单向适配”改成“双向闭环”

NarratoAI 那种方式是：

```text
TTS 多长，视频裁多长
```

这适合简单自动化，但不适合新闻解说质量。

更好的方式是：

```text
先有画面预算
再生成文案
再用 TTS 校验
不匹配就修复
最后才生成 cut_plan
```

可以理解成三轮：

## 第一轮：计划时长

```text
shot plan 决定每个镜头目标时长
```

例如：

```text
s01 7 秒
s02 8 秒
s03 6 秒
s04 10 秒
```

---

## 第二轮：文案估算时长

```text
AI 根据每个 shot 的目标时长写对应字数
```

例如：

```text
s01 36 字 ≈ 7 秒
s02 42 字 ≈ 8 秒
s03 30 字 ≈ 6 秒
```

---

## 第三轮：TTS 实测时长

```text
TTS 出来后测真实长度
```

例如：

```text
s01 6.8 秒，通过
s02 11.5 秒，过长
s03 3.2 秒，过短
```

然后只修复不合格的 segment。

不要整条视频重来。

---

# 五、提示词应该拆成这些功能

你现在不应该指望一个 prompt 同时解决所有事。建议拆成 6 类 prompt。

---

## 1. 候选片段评估 prompt

职责：

```text
判断候选片段是否适合 AI 解说视频。
```

输出：

```json
{
  "candidate_id": "clip_001",
  "keep": 1,
  "usage": "voiceover",
  "role": "hook/context/conflict/evidence/ending/transition",
  "visual_value": "high",
  "content_value": "high",
  "independent": 1,
  "needs_context": 0,
  "reason": "..."
}
```

它不写文案。

---

## 2. 成片故事规划 prompt

职责：

```text
决定这条视频讲什么，以及结构怎么分。
```

输出：

```json
{
  "title": "",
  "angle": "",
  "target_duration_seconds": 60,
  "sections": [
    {
      "section_id": "sec_01",
      "type": "hook",
      "target_seconds": 8,
      "goal": ""
    }
  ]
}
```

它也不写完整文案。

---

## 3. Shot plan prompt

职责：

```text
从候选片里选最终镜头，并分配每个镜头的作用和目标时长。
```

输出：

```json
{
  "shots": [
    {
      "shot_id": "v_001_s01",
      "candidate_id": "clip_001",
      "source_id": "source_1",
      "source_start": "00:00:12.000",
      "source_end": "00:00:22.000",
      "target_duration_seconds": 7,
      "min_duration_seconds": 5,
      "max_duration_seconds": 10,
      "narration_intent": ""
    }
  ]
}
```

---

## 4. AI 配音文案 prompt

职责：

```text
只根据 shot plan 写 narration_segments。
```

核心要求：

```text
shot_id 必须完全复用
不能新增
不能删除
每段字数必须符合预算
一句话适合一行字幕
不写记者报道
不营销号
不编造事实
```

输出：

```json
{
  "narration_segments": [
    {
      "shot_id": "v_001_s01",
      "text": "",
      "estimated_duration_seconds": 0
    }
  ]
}
```

---

## 5. 文案时长修复 prompt

职责：

```text
只修复 TTS 过长或过短的段落。
```

输入：

```json
{
  "shot_id": "v_001_s02",
  "old_text": "...",
  "target_duration_seconds": 8,
  "tts_actual_duration_seconds": 11.5,
  "max_chars": 42,
  "problem": "too_long"
}
```

输出：

```json
{
  "shot_id": "v_001_s02",
  "new_text": "...",
  "reason": "缩短到更接近 8 秒"
}
```

这个 prompt 很重要。
不要每次整条文案重写，只修局部。

---

## 6. 对齐质量检查 prompt

职责：

```text
检查文案和画面是否语义匹配。
```

它不决定剪辑，只输出问题：

```json
{
  "ok": 1,
  "issues": [
    {
      "shot_id": "v_001_s03",
      "type": "visual_mismatch",
      "problem": "文案说发布会，但画面是街景",
      "severity": "medium"
    }
  ]
}
```

---

# 六、代码层应该有 4 个强校验

## 1. shot plan 校验

在生成文案前检查：

```text
shots 不为空
shot_id 唯一
source_id 存在
source_start/source_end 有效
source_end > source_start
target_duration 在 min/max 之间
总 target_duration 接近用户目标
```

如果这里失败，不要进入文案生成。

---

## 2. voiceover script 校验

在 TTS 前检查：

```text
narration_segments 数量是否等于 shots 数量
shot_id 是否完全匹配
是否有陌生 shot_id
是否漏掉 shot_id
text 是否为空
字符数是否超预算
估算总时长是否接近目标
```

如果失败，先修文案，不要 TTS。

---

## 3. TTS 校验

TTS 后检查：

```text
每个 shot_id 是否有音频
actual_duration_seconds 是否 > 0
总 TTS 时长是否合理
单段是否过长 / 过短
```

如果有失败段：

```text
只重跑失败段 TTS
不要整条视频重跑
```

---

## 4. cut_plan 校验

最终生成 cut_plan 前检查：

```text
clip 数量 > 0
总视频时长 > 0
clip shot_id 和 tts shot_id 匹配率 > 90%
clip 没有 source_end <= source_start
clip 没有越界
clip 没有落到禁用画面
```

如果 clip 数量为 0，错误信息应该是：

```text
剪辑计划无有效画面片段
```

而不是：

```text
配音文案太短
```

你之前那个 0 秒报错，就应该在这里被更准确地拦截。

---

# 七、最终时长匹配策略应该这样设计

可以设置一个分层容错。

假设目标是 60 秒。

## 总时长容错

```text
优秀：54 - 66 秒
可接受：48 - 72 秒
需要修复：低于 48 或高于 72
阻断：低于 40 或高于 85
```

不要直接用素材总长当目标。

---

## 单 shot 容错

每个 shot：

```text
TTS 实际时长 / shot 目标时长
```

推荐规则：

```text
0.75 - 1.25：直接通过
0.55 - 0.75：偏短，尝试扩写或合并
1.25 - 1.6：偏长，尝试缩写或延展画面
> 1.6：必须修复
< 0.55：必须修复
```

---

## 修复优先级

### TTS 过长

```text
1. 缩写当前段文案
2. 如果内容不能删，拆成两个 shot
3. 从候选池找相关补画面
4. 轻微加速音频，最多 1.08x 或 1.10x
5. 仍失败则降低该视频通过状态
```

### TTS 过短

```text
1. 扩写当前段文案
2. 合并下一个短 shot
3. 延长画面，允许 0.5 - 1.5 秒无配音缓冲
4. 删除该 shot
```

### 画面不够

```text
1. 优先在同 candidate 内延展
2. 再找同 source 相邻画面
3. 再找同 story_group 的候选片
4. 最后用通用 B-roll
```

---

# 八、AI 配音新闻视频的最佳链路

我建议你的最终链路设计成这样：

```text
公共分析
  ↓
候选片段池 candidate_pool
  ↓
候选片二次筛选 candidate_refine
  ↓
成片故事规划 story_plan
  ↓
镜头计划 shot_plan / editing_structure
  ↓
代码生成 duration_budget
  ↓
AI 配音文案 voiceover_script
  ↓
文案与 shot 对齐校验
  ↓
TTS
  ↓
TTS 时长校验
  ↓
时长修复循环
    ├─ 修文案
    ├─ 补画面
    ├─ 合并 shot
    ├─ 拆分 shot
    └─ 轻微变速
  ↓
最终 cut_plan
  ↓
渲染
```

重点是中间这个循环：

```text
voiceover_script ↔ TTS duration ↔ shot_plan
```

没有这个闭环，AI 配音模式一定会经常时长不稳定。

---

# 九、你现在工程里应该优先改的方向

不是立刻学 NarratoAI 那种“视频适配 TTS”，而是吸收它的一部分：

```text
TTS 实际时长必须参与最终 cut_plan
```

但不能完全让视频无条件迁就 TTS。

你应该改成：

```text
TTS 实际时长参与校正，但必须受 shot 的 min/max 时长约束。
```

也就是：

```text
final_duration = clamp(tts_actual_duration, shot.min_duration, shot.max_duration)
```

如果 clamp 后差距太大，就进入修复，而不是硬裁。

---

# 十、最终推荐方案

我建议你把 AI 配音模式升级成：

```text
候选片驱动选材
故事规划驱动结构
shot contract 绑定画面与文案
duration budget 控制文案长度
TTS 实测驱动微调
repair loop 解决大偏差
cut_plan 只接收校验通过的稳定结构
```

一句话：

```text
不要让 AI 文案自由生成后再让视频被迫适配；
要先建立 shot 计划和时长预算，再让文案、TTS、视频共同收敛。
```

这样才能解决你现在反复遇到的：

```text
候选片有了，但文案对不上
文案有了，但 TTS 时长不对
TTS 有了，但 cut_plan 变短
目标时长莫名变成素材时长
最后实际时长 0 秒
```

真正稳定的 AI 配音视频，核心不是“文案生成”，而是：

```text
shot_id 主键统一
时间字段统一
每段有时长预算
TTS 后有局部修复
cut_plan 前有强校验
```

这套做好以后，AI 配音模式才会从“能跑”变成“可控”。
