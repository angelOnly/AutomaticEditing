你判断是对的。这个 `VISION_CHUNK_PROMPT` 现在的问题，本质不是“写得不够详细”，而是**职责边界不清楚**：

它一边想做“画面识别”，一边又想做“新闻价值判断”“风险判断”“开头价值判断”“ASR匹配判断”，导致模型输出很多字段，但后续真正能稳定使用的字段很少。

我建议把它改成：**视觉 chunk 只负责提供可靠的画面事实 + 少量辅助判断，不负责最终选题和高光决策。**

---

## 一、你提的 3 个问题逐条分析

### 1. 为什么让大模型做视觉识别，还要求具体识别类型？

这个问题非常关键。

现在 prompt 里写：

> 画面类型：主持人口播 / 记者连线 / 现场画面 / 资料画面 / 嘉宾评论 / 发布会 / 图表数据 / 其他

这不是不能做，而是**不应该让模型只在这些硬分类里选一个**。

新闻视频里很多画面是混合的，比如：

* 主持人口播 + 背景大屏新闻画面
* 记者连线 + 下方字幕条
* 资料画面 + 当前新闻解说
* 发布会现场 + 图表画面
* 主持人转场 + B-roll 画面

如果只让模型选一个 `scene_type`，很容易误判。尤其是“资料画面”和“现场画面”，这个对新闻剪辑非常关键，一旦误判，后续高光检测会把资料画面当成一线现场。

所以更好的设计是：

```json
"visual_type": {
  "primary": "",
  "secondary": [],
  "confidence": 0
}
```

或者更简单一点：

```json
"shot_type": "",
"visual_labels": []
```

让模型可以输出多个标签，而不是硬选一个。

---

### 2. 视觉识别规则太简单，没把重点核心说清楚

是的。现在 prompt 只是列了“识别什么”，但没有告诉模型**怎么判断重要信息**。

新闻视频视觉分析真正要抓的不是“画面里有什么”，而是：

1. 这个 chunk 是不是可用画面？
2. 是人物口播、现场、资料、图表，还是纯过场？
3. 画面中有没有能支撑新闻事实的视觉证据？
4. 有没有字幕、人名、地点、机构、数据？
5. 这个画面是否适合作为短视频开头？
6. ASR 说的内容和画面是否明显不一致？
7. 是否有“画面看起来很有冲击力，但其实是资料/背景/旧画面”的风险？

现在的 prompt 没有明确这些判断优先级，所以模型可能会输出一些很泛的东西，比如：

```json
"visual_summary": "画面中出现一名主持人在演播室播报新闻"
```

这种结果对后续没啥用。

更有用的是：

```json
"visual_facts": [
  "演播室内一名女主持人正面对镜头播报",
  "画面左侧大屏出现战机/军事相关画面",
  "下方字幕条出现某地冲突相关标题"
],
"edit_value": "low",
"reason": "主要是主持人口播，画面信息有限，适合作为上下文，不适合作为高光开头"
```

这样后续 `timeline`、`highlight_detection`、`content_analysis` 才能真的用。

---

### 3. 输出字段太多，没有解释，也不知道有什么用

完全正确。

现在字段很多：

```json
chunk_id
time_range
scene_type
visual_summary
screen_text
visible_people
location_clues
event_clues
is_live_scene
is_archive_footage
visual_value_score
hook_score
risk_tags
asr_visual_consistency
notes
```

里面有些字段是低价值的。

比如：

### `chunk_id` / `time_range`

这两个一般 input 已经有了，模型没必要再输出。
让模型重复输出，还可能出错。

建议：**由代码保留，不让模型生成。**

---

### `visible_people`

如果只是输出人名，风险很高，因为模型容易瞎认人。
除非画面字幕明确出现人名，否则不应该让模型识别人名。

建议改成：

```json
"people": [
  {
    "role": "anchor / reporter / guest / official / crowd / uncertain",
    "identity_text": "",
    "description": ""
  }
]
```

意思是：只能根据字幕和画面线索判断身份，不能凭脸认人。

---

### `location_clues` / `event_clues`

这两个可以保留，但不要太分散。可以合并到一个更清晰的字段：

```json
"evidence": {
  "screen_text": [],
  "people_or_org": [],
  "place": [],
  "event": []
}
```

---

### `visual_value_score` / `hook_score`

这两个容易伪精确。

模型给 7 分、8 分，其实没那么稳定。尤其 chunk 很短，抽帧有限，分数会抖动。

建议不要用 0-10 分，改成枚举：

```json
"edit_value": "high / medium / low",
"hook_value": "strong / usable / weak"
```

后续代码更好用，也更稳定。

---

### `is_live_scene` / `is_archive_footage`

这两个重要，但不能只输出 boolean。

因为很多时候不确定。建议改成三态：

```json
"footage_status": "live_or_current / archive_or_file / unclear"
```

并加原因：

```json
"footage_reason": ""
```

例如：

```json
"footage_status": "archive_or_file",
"footage_reason": "画面角落或字幕条出现资料画面/画面来源标识"
```

---

## 二、建议保留的核心字段

我建议视觉 chunk 输出控制在 7 个字段以内。

### 推荐新版结构

```json
{
  "shot_type": "",
  "visual_facts": [],
  "screen_text": [],
  "key_entities": [],
  "footage_status": "",
  "edit_value": "",
  "asr_match": "",
  "warnings": []
}
```

这 8 个字段已经够用了。

---

## 三、每个字段的用途说明

### 1. `shot_type`

表示画面主要类型。

可选值：

```text
anchor_studio
reporter_live
interview_or_guest
press_conference
field_scene
archive_or_file_footage
chart_or_graphic
social_media_or_phone_video
transition_or_broll
unclear
```

它给后续做基础判断：

* 主持人口播：通常不适合作为视觉高光
* 现场画面：高光价值可能更高
* 发布会：信息价值高，但画面冲击力不一定强
* 图表数据：适合解释，不一定适合开头
* 资料画面：要谨慎，不能当成现场

---

### 2. `visual_facts`

这是最重要字段。

只写画面中**确实看得到的事实**，不要推理过度。

例如：

```json
"visual_facts": [
  "一名主持人在演播室面对镜头播报",
  "背景大屏出现军事装备画面",
  "下方字幕条显示与地区冲突相关标题"
]
```

这个字段给后续 `timeline` 和 `video_understanding` 使用。

---

### 3. `screen_text`

只提取画面文字。

包括：

* 标题条
* 人名
* 地点
* 机构
* 数据
* 时间
* “资料画面”
* “直播”
* “连线”
* “独家”
* 新闻台标
* 来源标识

这个字段很重要，因为它比模型视觉猜测更可靠。

---

### 4. `key_entities`

从画面文字和可见线索里提取实体。

例如：

```json
"key_entities": [
  "凤凰卫视",
  "美国国务院",
  "加沙",
  "以色列",
  "特朗普"
]
```

注意：这个字段应该只基于画面和字幕，不应该从 ASR 里乱抄。
如果要允许 ASR 辅助，可以明确标注来源。

---

### 5. `footage_status`

判断这段画面是不是当前现场、资料画面，还是不确定。

可选值：

```text
current_or_live
archive_or_file
mixed
unclear
```

这个比原来的：

```json
"is_live_scene": false,
"is_archive_footage": false
```

更好。

因为两个 boolean 很容易出现矛盾：

```json
"is_live_scene": false,
"is_archive_footage": false
```

这到底是普通口播？还是不确定？还是图表？不清楚。

---

### 6. `edit_value`

视觉剪辑价值。

建议用：

```text
high
medium
low
```

判断标准：

* `high`：现场冲突、关键人物、强情绪、强动作、重要字幕、稀缺画面
* `medium`：有明确新闻信息，但画面普通
* `low`：普通口播、重复画面、转场、纯背景、信息弱

---

### 7. `asr_match`

ASR 和画面是否匹配。

可选值：

```text
match
partial
mismatch
unclear
```

这个字段很有用。比如 ASR 在说“美国大选”，画面却是“财经图表”，就可能说明画面是 B-roll，不能强关联。

---

### 8. `warnings`

风险标签，不要太多，但要有核心风险。

例如：

```json
"warnings": [
  "possible_archive_misread",
  "identity_uncertain",
  "screen_text_unclear"
]
```

建议可选值：

```text
possible_archive_misread
identity_uncertain
screen_text_unclear
graphic_only
low_visual_information
sensitive_image
asr_visual_mismatch
```

---

## 四、推荐新版 VISION_CHUNK_PROMPT

可以改成这样：

```python
VISION_CHUNK_PROMPT = """你是一名凤凰卫视新闻短视频画面分析编辑。

你的任务不是总结整条新闻，而是根据当前 chunk 的关键帧和 ASR 文本，判断这段画面本身能为后续剪辑提供什么可靠信息。

请重点识别：
1. 画面主要类型
2. 画面中确实可见的事实
3. 画面文字，包括标题条、人名、地点、机构、数据、时间、直播/连线/资料画面/来源标识
4. 可从画面文字或明显视觉线索确认的关键实体
5. 这段画面是当前现场、直播/连线、资料画面、混合画面，还是无法判断
6. 这段画面对短视频剪辑是否有价值
7. ASR 内容与画面是否明显匹配
8. 是否存在误判风险，例如把资料画面误当现场画面、把背景图误当事件现场、把不确定人物当成确定人物

重要规则：
- 只描述画面中能看到的内容，不要根据常识脑补。
- 不能仅凭长相识别人名；只有画面文字、字幕、台标、职务牌明确出现时，才可以写具体身份。
- 如果画面文字不清楚，写 unclear，不要猜。
- 如果 ASR 提到的信息画面中没有出现，不要把它写进 visual_facts，可以只在 asr_match 中体现。
- 如果画面是主持人口播但背景大屏出现新闻画面，要同时说明“主持人口播”和“背景画面内容”。
- 如果出现“资料画面”“画面来源”“视频来源”“File”“Archive”等标识，必须在 screen_text 和 footage_status 中体现。
- 不确定的信息使用 uncertain。
- 只输出 JSON，不要输出解释性文字。

字段说明：
- shot_type：当前 chunk 的主要画面类型。只能从以下值中选择：
  anchor_studio / reporter_live / interview_or_guest / press_conference / field_scene / archive_or_file_footage / chart_or_graphic / social_media_or_phone_video / transition_or_broll / mixed / unclear

- visual_facts：画面中确实可见的事实，数组，每条一句话。不要写 ASR 才有、画面看不到的信息。

- screen_text：画面中可读文字，数组。包括标题条、人名、地点、机构、数据、时间、直播、连线、资料画面、来源标识等。看不清则写 unclear，不要猜。

- key_entities：从画面文字或明确视觉线索中提取的关键人物、机构、地点、国家、事件名。没有则为空数组。

- footage_status：画面时效属性。只能从以下值中选择：
  current_or_live / archive_or_file / mixed / unclear

- edit_value：画面对短视频剪辑的视觉价值。只能从以下值中选择：
  high / medium / low

- asr_match：ASR 与画面的匹配程度。只能从以下值中选择：
  match / partial / mismatch / unclear

- warnings：风险标签数组。只能使用以下值：
  possible_archive_misread / identity_uncertain / screen_text_unclear / graphic_only / low_visual_information / sensitive_image / asr_visual_mismatch

输出格式：
{
  "shot_type": "",
  "visual_facts": [],
  "screen_text": [],
  "key_entities": [],
  "footage_status": "",
  "edit_value": "",
  "asr_match": "",
  "warnings": []
}
"""
```

---

## 五、进一步压缩版：如果你想让模型更快、更稳定

如果你特别在意速度和 token，可以进一步压成 6 个字段：

```json
{
  "type": "",
  "facts": [],
  "text": [],
  "footage": "",
  "value": "",
  "risk": []
}
```

对应 prompt：

```python
VISION_CHUNK_PROMPT = """你是新闻短视频画面分析编辑。请根据当前 chunk 的关键帧和 ASR 文本，只输出这段画面对后续剪辑有用的视觉信息。

判断重点：
1. 画面是什么类型
2. 画面中确实看得见什么
3. 画面文字有什么
4. 是现场/当前画面、资料画面、混合画面，还是不确定
5. 这段画面是否有剪辑价值
6. 是否有误判风险

规则：
- 只写画面能看到的事实，不要脑补。
- 不要凭脸识别人名，除非字幕或画面文字明确出现。
- ASR 只能辅助判断匹配关系，不能把 ASR 内容当成画面事实。
- 出现“资料画面/来源/File/Archive”等标识时，必须标记为资料风险。
- 看不清或不确定写 uncertain。
- 只输出 JSON。

字段：
- type：anchor / reporter / guest / press / field / archive / chart / social_video / broll / mixed / unclear
- facts：画面可见事实，数组
- text：画面文字，数组
- footage：current / archive / mixed / unclear
- value：high / medium / low
- risk：数组，可选 archive_misread / identity_uncertain / text_unclear / low_info / sensitive / asr_mismatch

输出：
{
  "type": "",
  "facts": [],
  "text": [],
  "footage": "",
  "value": "",
  "risk": []
}
"""
```

这个版本更适合批量并发跑视觉 chunk，token 更少，后续也更容易消费。

---

## 六、我建议你代码里对应改成这样

原来后续可能在读：

```python
visual_summary
screen_text
visible_people
location_clues
event_clues
visual_value_score
hook_score
risk_tags
```

建议改为统一读：

```python
facts = result.get("visual_facts") or result.get("facts") or []
screen_text = result.get("screen_text") or result.get("text") or []
shot_type = result.get("shot_type") or result.get("type") or "unclear"
footage_status = result.get("footage_status") or result.get("footage") or "unclear"
edit_value = result.get("edit_value") or result.get("value") or "low"
warnings = result.get("warnings") or result.get("risk") or []
```

然后在 timeline 里拼成更紧凑的视觉摘要：

```python
visual_digest = {
    "type": shot_type,
    "facts": facts[:3],
    "text": screen_text[:5],
    "footage": footage_status,
    "value": edit_value,
    "risk": warnings
}
```

这样后续给 `timeline_digest`、`video_understanding`、`highlight_detection` 的输入会小很多。

---

## 七、最终建议

我建议不要继续用原来的大字段版本。

更推荐这个版本：

```json
{
  "type": "",
  "facts": [],
  "text": [],
  "footage": "",
  "value": "",
  "risk": []
}
```

原因是：

1. **字段少，成本低**
2. **后续好消费**
3. **避免模型输出一堆伪精确分数**
4. **把视觉模型限制在“看图说事实”范围内**
5. **高光判断交给后面的 highlight/content agent，不让视觉 chunk 越权**
6. **资料画面误判风险被单独保留下来**

视觉 chunk 的定位应该是：

> 它不是“新闻选题模型”，而是“画面事实提取器 + 视觉可用性粗判器”。

这样后面整个 pipeline 会更稳定。
