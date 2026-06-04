你这个判断是对的。现在的 `VIDEO_UNDERSTANDING_PROMPT` 问题不是“字段少”，而是**任务定义太虚**：

它只是说“理解整条视频内容”，但没有告诉模型：

1. 理解到什么粒度才算合格；
2. 哪些信息是后续高光识别真正需要的；
3. `summary / key_facts / content_structure / potential_angles` 的区别是什么；
4. 遇到 timeline_digest 信息不完整时怎么处理；
5. 什么叫主主题、什么叫视频类型、什么叫角度；
6. 哪些内容不能编造。

所以模型很容易输出一堆“看似完整、实际没用”的泛化内容。

---

## 一、这个 Prompt 的真实任务应该重新定义

它不是普通摘要，而是：

> 根据 `timeline_digest`，把整条新闻视频整理成一份“后续选片、重组、AI解说规划可直接引用的视频背景档案”。

也就是说，它的核心目的不是好看，而是给后续这些模块用：

```text
highlight_detection
candidate_refine
highlight_reassembly_plan
short_video_edit_plan
voiceover_script
```

所以它应该回答几个关键问题：

```text
这条视频到底在讲什么？
它属于什么新闻类型？
核心事实有哪些？
哪些事实有时间依据？
视频结构怎么展开？
哪些段落适合被剪成短视频？
哪些角度有短视频传播价值？
哪些人物/地点/事件是确定的？
哪些内容只是推测，不能当事实？
```

---

# 二、建议优化后的输出结构

我建议不要完全沿用原来的字段，而是做一次标准化升级。

## 推荐 JSON

```json
{
  "main_topic": "",
  "video_type": "",
  "editorial_summary": "",
  "storyline": "",
  "core_facts": [
    {
      "fact": "",
      "time_range": "",
      "evidence": "",
      "confidence": "high"
    }
  ],
  "people": [
    {
      "name_or_identity": "",
      "role": "",
      "confidence": "high"
    }
  ],
  "locations": [
    {
      "name_or_clue": "",
      "type": "",
      "confidence": "high"
    }
  ],
  "content_structure": [
    {
      "start": "",
      "end": "",
      "section_type": "",
      "section_summary": "",
      "editorial_function": ""
    }
  ],
  "highlight_context": {
    "most_newsworthy_points": [],
    "conflict_or_tension": "",
    "emotional_points": [],
    "visual_hooks": [],
    "not_suitable_points": []
  },
  "potential_angles": [
    {
      "angle": "",
      "reason": "",
      "suitable_for": "ai_voiceover / original_audio / both",
      "priority": 1
    }
  ],
  "uncertainties": [],
  "do_not_infer": []
}
```

相比原版，重点变化是：

原来的 `summary` 太泛，我建议拆成：

```text
editorial_summary：主编摘要，讲清楚整条视频内容
storyline：视频内容推进逻辑
```

原来的 `key_facts` 改成：

```text
core_facts：必须带 time_range + evidence + confidence
```

这样后续写 AI 解说时，可以知道哪些事实能用，哪些不能用。

另外新增：

```text
highlight_context
```

这是给后续高光识别最重要的字段。它告诉后续模块：

```text
哪里有新闻价值？
哪里有冲突？
哪里有情绪点？
哪里有视觉钩子？
哪些内容不适合剪？
```

---

# 三、优化版 VIDEO_UNDERSTANDING_PROMPT

可以直接替换你现在的 prompt。

```python
VIDEO_UNDERSTANDING_PROMPT = NEWS_BACKGROUND + """
请只读取输入中的 timeline_digest，不要要求、不要依赖完整 merged_timeline。

你是一名资深新闻主编，当前任务不是剪片，也不是生成解说文案，
而是根据 timeline_digest 对整条视频做一次“视频内容理解与主编整理”。

你的输出会被后续模块使用，包括：
1. 高光片段识别 highlight_detection
2. 候选片段精排 candidate_refine
3. 高光重组规划 highlight_reassembly_plan
4. 短视频剪辑规划 short_video_edit_plan
5. AI 配音解说文案 voiceover_script

因此，你的目标不是写一段好看的摘要，而是把整条视频整理成一份
“后续选片、重组、剪辑、解说可以直接引用的视频背景档案”。

你必须完成以下理解任务：

一、判断整条视频的主主题
- 用一句话说明这条视频主要讲什么。
- 不要写成泛泛的“某事件报道”。
- 应尽量包含事件主体、关键动作、结果或争议点。
- 如果 timeline_digest 信息不足，允许概括，但不能编造。

二、判断视频类型
请从以下类型中选择最接近的一类：
- breaking_news：突发新闻
- political_news：政治新闻
- international_news：国际新闻
- social_news：社会新闻
- finance_news：财经新闻
- legal_case：法律 / 案件
- disaster_accident：灾害 / 事故
- military_conflict：军事 / 冲突
- press_conference：发布会 / 记者会
- interview：采访
- feature_story：专题 / 人物 / 深度报道
- live_report：现场连线 / 直播报道
- mixed_news：多主题混合新闻
- unknown：信息不足，无法判断

三、整理主编摘要 editorial_summary
- 用 2-4 句话概括整条视频。
- 必须说明：谁、发生了什么、为什么重要、视频大致如何展开。
- 不要加入 timeline_digest 中没有依据的新事实。
- 不要写宣传口号，不要写标题党表达。

四、整理 storyline
- 用一段话说明视频内容的推进逻辑。
- 例如：先交代背景，再展示现场，再引用人物表态，最后给出结果或影响。
- 如果视频是多个新闻拼接，也要说明它是多主题结构。

五、提取核心事实 core_facts
只提取对理解新闻事件真正重要的事实。
每条事实必须满足：
- 能从 timeline_digest 中找到依据；
- 尽量带时间范围；
- 不把评论、推测、情绪描述当事实；
- 不重复表达同一事实。

每条事实包含：
- fact：事实本身，简洁明确；
- time_range：事实对应的大致时间范围，如果没有则填 "";
- evidence：这个事实来自画面、ASR、字幕、人物发言、现场信息还是综合判断；
- confidence：high / medium / low。

confidence 判断标准：
- high：timeline_digest 中有明确文字、画面或多处信息支持；
- medium：有依据，但信息不完整或只出现一次；
- low：只能弱推断，不应作为强事实使用。

六、识别人物 people
只记录对新闻理解有用的人物。
可以是：
- 明确姓名；
- 职务身份；
- 群体身份；
- 画面中的关键人物。

不要编造姓名。
如果只知道身份，不知道姓名，就写身份，例如“现场记者”“警方人员”“受访居民”。

每个人物包含：
- name_or_identity：姓名或身份；
- role：在新闻中的作用，例如“发言者 / 当事人 / 记者 / 官员 / 目击者 / 专家 / 群众”；
- confidence：high / medium / low。

七、识别地点 locations
只记录对新闻理解有用的地点。
可以是：
- 明确地名；
- 国家 / 城市 / 区域；
- 现场类型，例如“医院外”“法院门口”“灾害现场”。

不要根据常识乱推地点。

每个地点包含：
- name_or_clue：地点名称或地点线索；
- type：country / city / region / venue / scene_clue / unknown；
- confidence：high / medium / low。

八、梳理内容结构 content_structure
把整条视频按内容功能拆成若干段。
不是按固定时间平均切分，而是按内容变化切分。

section_type 从以下类型选择：
- opening：开头引入
- background：背景交代
- live_scene：现场画面
- narration：旁白解释
- interview：采访 / 发言
- data_or_graphic：数据 / 图表 / 字幕信息
- conflict_or_key_moment：冲突 / 关键瞬间
- consequence：结果 / 影响
- transition：转场
- ending：结尾总结
- unrelated_or_low_value：低价值或无关内容
- unknown：无法判断

每段包含：
- start：开始时间；
- end：结束时间；
- section_type：段落类型；
- section_summary：这段讲了什么；
- editorial_function：这段在整条新闻中的作用，例如“交代背景 / 提供证据 / 增强现场感 / 引出冲突 / 补充影响”。

九、整理高光识别上下文 highlight_context
这部分专门给后续高光检测和重组使用。

你需要判断：
1. most_newsworthy_points
   - 最有新闻价值的点；
   - 可以是关键事实、现场变化、人物表态、冲突结果、罕见画面。

2. conflict_or_tension
   - 是否存在冲突、争议、反转、紧张关系；
   - 如果没有，填 ""。

3. emotional_points
   - 可能引发观众情绪反应的点；
   - 例如震惊、担忧、同情、愤怒、悬念、荒诞感；
   - 没有则为空数组。

4. visual_hooks
   - 画面上适合作为短视频开头的视觉钩子；
   - 例如现场冲突、灾害画面、人物激烈发言、醒目的字幕、特殊场景；
   - 只根据 timeline_digest 中的视觉描述判断。

5. not_suitable_points
   - 不适合剪成重点片段的内容；
   - 例如重复旁白、空镜、无关铺垫、信息不完整的段落、只有背景没有事件推进的段落。

十、提出潜在短视频角度 potential_angles
请提出 1-5 个后续可剪辑的短视频角度。
每个角度必须基于视频已有内容，不得虚构。

每个 angle 包含：
- angle：角度标题，不要标题党；
- reason：为什么这个角度值得剪；
- suitable_for：
  - ai_voiceover：更适合 AI 配音解说；
  - original_audio：更适合保留原声；
  - both：两种都适合；
- priority：1-5，1 代表优先级最高。

判断标准：
- 如果需要整合多段信息、解释背景、补充逻辑，更适合 ai_voiceover；
- 如果原声发言、现场同期声、冲突声音本身有价值，更适合 original_audio；
- 如果既有现场画面，又需要解释，可以填 both。

十一、列出不确定信息 uncertainties
如果 timeline_digest 中存在信息不足、时间不清楚、人物身份不确定、地点不确定、画面与ASR不一致等情况，
必须写入 uncertainties。
不要为了让 JSON 好看而省略不确定性。

十二、列出禁止推断 do_not_infer
列出后续模块不应该当成事实使用的内容。
例如：
- 只从画面弱推断出的身份；
- 没有明确来源的地点；
- 可能是资料画面的内容；
- ASR 中含糊不清的人名、数字、机构名；
- 无法确认的因果关系。

重要约束：
1. 只能基于 timeline_digest。
2. 不要要求完整 merged_timeline。
3. 不要编造人物、地点、数字、因果关系。
4. 不要输出 Markdown。
5. 不要输出解释文字。
6. 只输出合法 JSON。
7. 所有字段必须存在。
8. 如果某字段没有信息，填 "" 或 []。
9. 时间格式尽量沿用输入中的时间格式。
10. 输出语言使用中文。

请严格输出以下 JSON 结构：

{
  "main_topic": "",
  "video_type": "",
  "editorial_summary": "",
  "storyline": "",
  "core_facts": [
    {
      "fact": "",
      "time_range": "",
      "evidence": "",
      "confidence": "high / medium / low"
    }
  ],
  "people": [
    {
      "name_or_identity": "",
      "role": "",
      "confidence": "high / medium / low"
    }
  ],
  "locations": [
    {
      "name_or_clue": "",
      "type": "country / city / region / venue / scene_clue / unknown",
      "confidence": "high / medium / low"
    }
  ],
  "content_structure": [
    {
      "start": "",
      "end": "",
      "section_type": "opening / background / live_scene / narration / interview / data_or_graphic / conflict_or_key_moment / consequence / transition / ending / unrelated_or_low_value / unknown",
      "section_summary": "",
      "editorial_function": ""
    }
  ],
  "highlight_context": {
    "most_newsworthy_points": [],
    "conflict_or_tension": "",
    "emotional_points": [],
    "visual_hooks": [],
    "not_suitable_points": []
  },
  "potential_angles": [
    {
      "angle": "",
      "reason": "",
      "suitable_for": "ai_voiceover / original_audio / both",
      "priority": 1
    }
  ],
  "uncertainties": [],
  "do_not_infer": []
}
"""
```

---

# 四、为什么这个版本更适合你的 pipeline

## 1. 它不是单纯总结，而是服务后续剪辑

原版：

```text
请根据 timeline_digest 理解整条视频内容
```

这个太空了。

新版明确告诉模型：

```text
这份结果会给 highlight_detection、reassembly、edit_plan、voiceover 用
```

这样模型会更倾向于输出“可用于剪辑决策”的信息，而不是写新闻摘要作文。

---

## 2. `key_facts` 改成 `core_facts` 更标准

原版：

```json
"key_facts": [
  {
    "fact": "",
    "source_time_range": "",
    "confidence": ""
  }
]
```

问题是：

```text
source_time_range 是啥？
fact 的粒度多细？
confidence 怎么判断？
事实和观点怎么区分？
```

新版：

```json
{
  "fact": "",
  "time_range": "",
  "evidence": "",
  "confidence": "high"
}
```

加了 `evidence` 后，后续 AI 解说会更安全。

例如：

```json
{
  "fact": "现场出现大量烟雾，疑似事故发生后画面。",
  "time_range": "00:32-00:45",
  "evidence": "视觉摘要显示现场有浓烟，ASR未明确说明原因",
  "confidence": "medium"
}
```

后续就知道这个不能写成：

```text
事故造成严重爆炸。
```

因为只是视觉依据，不是确认事实。

---

## 3. `highlight_context` 是最关键新增字段

你这个系统后面是要做高光识别，所以整条视频理解阶段就应该提前整理：

```text
新闻价值点
冲突点
情绪点
视觉钩子
不适合剪的内容
```

否则后续 `highlight_detection` 只能重新判断一遍，浪费模型调用，而且判断容易不一致。

---

## 4. `do_not_infer` 可以减少后续乱编

新闻视频里经常有这些问题：

```text
资料画面
画面和 ASR 不完全对应
人物身份不清楚
地点只看到建筑，不知道城市
字幕可能是节目包装，不是事件事实
```

如果不在 video_understanding 阶段标出来，后续 AI 配音很容易编造。

所以新增：

```json
"uncertainties": [],
"do_not_infer": []
```

这是给后续事实安全兜底用的。

---

# 五、如果你想少改代码，也可以保留旧字段

如果你不想大改 `pipeline.py` 里读取字段的地方，可以做兼容版：

```json
{
  "main_topic": "",
  "topic": "",
  "video_type": "",
  "summary": "",
  "editorial_summary": "",
  "storyline": "",
  "key_facts": [],
  "core_facts": [],
  "people": [],
  "locations": [],
  "content_structure": [],
  "highlight_context": {},
  "potential_angles": [],
  "uncertainties": [],
  "do_not_infer": []
}
```

其中：

```text
summary = editorial_summary 的短版
key_facts = core_facts 的兼容副本
topic = main_topic 的兼容副本
```

这样原来的代码：

```python
video.get("main_topic") or video.get("topic")
video.get("summary")
```

还能继续工作。

---

# 六、建议你实际采用的兼容版 Prompt

如果你的项目里已经有很多地方依赖 `summary / key_facts`，我更建议先用这个版本，改动最小。

```python
VIDEO_UNDERSTANDING_PROMPT = NEWS_BACKGROUND + """
请只读取输入中的 timeline_digest，不要要求或依赖完整 merged_timeline。

你是一名资深新闻主编。当前任务不是选片、不是剪辑、不是写解说文案，
而是根据 timeline_digest 对整条视频做一次“视频内容理解与主编整理”。

这份结果会作为后续模块的背景输入，包括：
- highlight_detection：高光片段识别
- candidate_refine：候选片段精排
- highlight_reassembly_plan：高光重组规划
- short_video_edit_plan：短视频剪辑规划
- voiceover_script：AI 解说文案生成

因此，你必须输出一份可被后续模块直接使用的视频背景档案。

理解目标：
1. 判断整条视频的主主题；
2. 判断视频类型；
3. 概括视频讲了什么；
4. 梳理视频内容如何展开；
5. 提取有依据的核心事实；
6. 识别关键人物、地点；
7. 按内容功能拆分视频结构；
8. 判断哪些信息对高光识别有价值；
9. 提出可剪辑的短视频角度；
10. 标记不确定信息和禁止推断的信息。

字段说明：

main_topic：
- 用一句话说明整条视频最核心的新闻主题。
- 应包含事件主体、关键动作、结果或争议点。
- 不要写成泛泛的“某新闻事件报道”。

topic：
- 与 main_topic 保持一致，用于兼容旧代码。

video_type：
从以下类型中选择一个：
- breaking_news：突发新闻
- political_news：政治新闻
- international_news：国际新闻
- social_news：社会新闻
- finance_news：财经新闻
- legal_case：法律 / 案件
- disaster_accident：灾害 / 事故
- military_conflict：军事 / 冲突
- press_conference：发布会 / 记者会
- interview：采访
- feature_story：专题 / 人物 / 深度报道
- live_report：现场连线 / 直播报道
- mixed_news：多主题混合新闻
- unknown：信息不足，无法判断

summary：
- 2-4 句话概括整条视频。
- 必须说明：谁、发生了什么、为什么重要、视频大致如何展开。
- 不要加入 timeline_digest 中没有依据的新事实。

editorial_summary：
- 可以与 summary 接近，但更偏主编视角。
- 说明这条视频的新闻价值、主要看点和内容重点。

storyline：
- 说明视频内容的推进逻辑。
- 例如：先交代背景，再展示现场，再引用人物表态，最后说明影响。
- 如果是多条新闻拼接，要说明是多主题结构。

key_facts：
- 兼容旧字段。
- 内容应与 core_facts 保持一致，但字段名使用旧结构。
- 每条事实必须有依据，不得编造。

core_facts：
- 提取真正影响新闻理解的事实。
- 不要把评论、猜测、情绪描述当事实。
- 每条包含 fact、time_range、evidence、confidence。

confidence 判断：
- high：有明确 ASR、字幕、画面或多处信息支持；
- medium：有依据但信息不完整；
- low：只能弱推断，后续不应作为强事实使用。

people：
- 只记录对新闻理解有用的人物。
- 可以是姓名、职务、身份或群体。
- 不知道姓名时只写身份，不要编造姓名。

locations：
- 只记录有依据的地点。
- 可以是国家、城市、区域、建筑、现场类型。
- 不要根据常识推断地点。

content_structure：
- 按内容功能拆分整条视频，不要机械平均切分。
- section_type 从以下类型选择：
  opening / background / live_scene / narration / interview / data_or_graphic /
  conflict_or_key_moment / consequence / transition / ending /
  unrelated_or_low_value / unknown

highlight_context：
- 给后续高光识别使用。
- most_newsworthy_points：最有新闻价值的点；
- conflict_or_tension：冲突、争议、反转、紧张关系；
- emotional_points：可能引发情绪反应的点；
- visual_hooks：适合作为短视频开头的视觉钩子；
- not_suitable_points：不适合重点剪辑的内容。

potential_angles：
- 提出 1-5 个可剪辑短视频角度。
- 每个角度必须基于 timeline_digest，不得虚构。
- suitable_for 只能是 ai_voiceover / original_audio / both。
- priority 为 1-5，1 最高。

uncertainties：
- 记录信息不足、身份不明、地点不明、时间不清、画面与 ASR 不一致等问题。

do_not_infer：
- 记录后续模块不应当成事实使用的内容。
- 例如：资料画面、弱推断身份、无法确认地点、含糊数字、无法确认因果关系。

重要约束：
1. 只能基于 timeline_digest。
2. 不要要求完整 merged_timeline。
3. 不要编造人物、地点、数字、因果关系。
4. 不要输出 Markdown。
5. 不要输出解释文字。
6. 只输出合法 JSON。
7. 所有字段必须存在。
8. 没有信息的字段填 "" 或 []。
9. 时间格式尽量沿用输入。
10. 输出语言使用中文。

请严格输出以下 JSON：

{
  "main_topic": "",
  "topic": "",
  "video_type": "",
  "summary": "",
  "editorial_summary": "",
  "storyline": "",
  "key_facts": [
    {
      "fact": "",
      "source_time_range": "",
      "confidence": "high / medium / low"
    }
  ],
  "core_facts": [
    {
      "fact": "",
      "time_range": "",
      "evidence": "",
      "confidence": "high / medium / low"
    }
  ],
  "people": [
    {
      "name_or_identity": "",
      "role": "",
      "confidence": "high / medium / low"
    }
  ],
  "locations": [
    {
      "name_or_clue": "",
      "type": "country / city / region / venue / scene_clue / unknown",
      "confidence": "high / medium / low"
    }
  ],
  "content_structure": [
    {
      "start": "",
      "end": "",
      "section_type": "opening / background / live_scene / narration / interview / data_or_graphic / conflict_or_key_moment / consequence / transition / ending / unrelated_or_low_value / unknown",
      "section_summary": "",
      "editorial_function": ""
    }
  ],
  "highlight_context": {
    "most_newsworthy_points": [],
    "conflict_or_tension": "",
    "emotional_points": [],
    "visual_hooks": [],
    "not_suitable_points": []
  },
  "potential_angles": [
    {
      "angle": "",
      "reason": "",
      "suitable_for": "ai_voiceover / original_audio / both",
      "priority": 1
    }
  ],
  "uncertainties": [],
  "do_not_infer": []
}
"""
```

---

# 七、对应代码读取也建议小改一下

你说现在 `_build_highlight_reassembly_plan_text()` 只读：

```python
video.get("main_topic") or video.get("topic")
video.get("summary")
```

这个太浪费了。

建议把这些字段也喂给后续重组规划：

```python
main_topic = video.get("main_topic") or video.get("topic") or ""
summary = video.get("summary") or video.get("editorial_summary") or ""
storyline = video.get("storyline") or ""
highlight_context = video.get("highlight_context") or {}
potential_angles = video.get("potential_angles") or []
core_facts = video.get("core_facts") or video.get("key_facts") or []
uncertainties = video.get("uncertainties") or []
do_not_infer = video.get("do_not_infer") or []
```

然后拼接成更有用的文本：

```python
def _build_video_understanding_text(video: dict) -> str:
    if not isinstance(video, dict):
        return ""

    lines = []

    main_topic = video.get("main_topic") or video.get("topic") or ""
    if main_topic:
        lines.append(f"主主题：{main_topic}")

    video_type = video.get("video_type") or ""
    if video_type:
        lines.append(f"视频类型：{video_type}")

    summary = video.get("summary") or video.get("editorial_summary") or ""
    if summary:
        lines.append(f"摘要：{summary}")

    storyline = video.get("storyline") or ""
    if storyline:
        lines.append(f"内容推进：{storyline}")

    core_facts = video.get("core_facts") or video.get("key_facts") or []
    if core_facts:
        lines.append("核心事实：")
        for i, fact in enumerate(core_facts[:8], 1):
            if isinstance(fact, dict):
                text = fact.get("fact", "")
                time_range = fact.get("time_range") or fact.get("source_time_range") or ""
                confidence = fact.get("confidence", "")
                evidence = fact.get("evidence", "")
                lines.append(f"{i}. {text}｜时间：{time_range}｜依据：{evidence}｜置信度：{confidence}")
            else:
                lines.append(f"{i}. {fact}")

    highlight_context = video.get("highlight_context") or {}
    if highlight_context:
        lines.append("高光识别上下文：")

        points = highlight_context.get("most_newsworthy_points") or []
        if points:
            lines.append("最有新闻价值的点：" + "；".join(map(str, points[:6])))

        tension = highlight_context.get("conflict_or_tension") or ""
        if tension:
            lines.append(f"冲突/张力：{tension}")

        visual_hooks = highlight_context.get("visual_hooks") or []
        if visual_hooks:
            lines.append("视觉钩子：" + "；".join(map(str, visual_hooks[:6])))

        not_suitable = highlight_context.get("not_suitable_points") or []
        if not_suitable:
            lines.append("不适合重点剪辑：" + "；".join(map(str, not_suitable[:6])))

    potential_angles = video.get("potential_angles") or []
    if potential_angles:
        lines.append("潜在短视频角度：")
        for item in potential_angles[:5]:
            if isinstance(item, dict):
                angle = item.get("angle", "")
                reason = item.get("reason", "")
                suitable_for = item.get("suitable_for", "")
                priority = item.get("priority", "")
                lines.append(f"- {angle}｜原因：{reason}｜适合：{suitable_for}｜优先级：{priority}")

    uncertainties = video.get("uncertainties") or []
    if uncertainties:
        lines.append("不确定信息：" + "；".join(map(str, uncertainties[:6])))

    do_not_infer = video.get("do_not_infer") or []
    if do_not_infer:
        lines.append("禁止推断为事实：" + "；".join(map(str, do_not_infer[:6])))

    return "\n".join(lines)
```

这样 `video_understanding` 就真的能给后续模块提供价值，而不是只贡献一个 summary。

---

# 八、最终建议

你这个 Agent 不应该叫“视频摘要 Agent”，而应该定位成：

```text
视频主编理解 Agent
```

它的产物不是摘要，而是：

```text
视频背景档案 + 高光判断上下文 + 事实安全边界
```

所以我建议你采用：

1. **Prompt 用兼容版**，避免大面积改代码；
2. 保留旧字段：`topic / summary / key_facts`；
3. 新增强字段：`editorial_summary / storyline / core_facts / highlight_context / uncertainties / do_not_infer`；
4. 后续 `_build_highlight_reassembly_plan_text()` 不要只读 `summary`，要把 `highlight_context` 和 `potential_angles` 一起传下去。
