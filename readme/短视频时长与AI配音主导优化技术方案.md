# 短视频时长与 AI 配音主导优化技术方案

> 本文是方案文档，不直接修改代码和线上 Prompt。目标是结合 1714、1856 两个任务的实际日志，定位当前成片“AI 配音没讲透、原声占比过高、30 秒卡得太死、像没讲完”的原因，并给出具体到文件和 Prompt 的优化方案。

## 1. 核心结论

当前问题不是单纯的 TTS 或渲染问题，而是“规划 Prompt、剪辑 Prompt、配音 Prompt、代码兜底校验”共同造成的：

1. `allow_long_video=true` 现在更多只是解除 60 秒以上的阻断逻辑，并没有让模型主动把复杂新闻扩展到 45-60 秒内讲清楚。
2. `SHORT_VIDEO_DURATION_POLICY` 仍强烈暗示“默认 30 秒左右”，导致模型即使看到复杂新闻，也倾向压缩到 25-35 秒。
3. `EDITING_DIRECTOR_PROMPT` 允许模型把核心讲话设成 `original_sound`，且没有限制原声时长，于是出现 20-25 秒整段原声。
4. `VOICEOVER_PROMPT` 明确要求 `original_sound` 镜头“不写或少写 AI 解说”，导致 AI 配音主动让出主体叙事空间。
5. `duration_policy.py` 与 `pipeline.py` 对 `original_sound` 的处理是“只要模型说要保留证据声，就给原声满音量”，缺少总时长和比例限制。
6. `pipeline.py` 已经检测到了画面和 AI 配音时长严重不一致，但 `step_cut_plan` 中超过 1 秒阻断的逻辑被注释，最终仍允许渲染出明显 mismatch 的成片。

优化方向应从“30 秒优先”调整为：

```text
普通快讯仍可 25-35 秒；
复杂新闻、外交表态、需要背景解释的新闻，在用户勾选允许长视频时，默认允许 45-60 秒；
60 秒以内不需要再卡死 30 秒；
AI 配音必须承担主线叙事；
原声只能作为短证据声，不能成为主体；
如果视频和 AI 配音时长严重不一致，必须阻断而不是继续渲染。
```

## 2. 两个任务的日志复盘

### 2.1 1856 任务：生成 1 条剪辑，但像没讲完

任务路径：

```text
outputs/欧盟成员国外长会议讨论多议题_20260525_1856
```

关键结果：

```text
short_video_plan：1 条视频，target_duration_seconds=30，audio_strategy=ai_voiceover_main
editing_script：总时长 30 秒
  - 0-6 秒：ai_voiceover
  - 6-26 秒：original_sound，持续 20 秒
  - 26-30 秒：ai_voiceover
voiceover_script：narration_text 只有 43 个字，估算约 10.24 秒
cut_plan：video_duration=30.0 秒，voiceover_duration=8.04 秒，delta=21.96 秒，status=mismatch
```

直接问题：

1. 用户选择的是 AI 配音，但 30 秒成片中有 20 秒是 `original_sound`，原声占比约 66.7%。
2. AI 解说只有 43 个字，只能说清“外长会召开、比利时外相表态、内部有分歧”三个极粗的信息点。
3. 结尾只有 4 秒，缺少完整收束，观众会感觉新闻刚铺开就结束。
4. 系统已经识别 `duration_status=mismatch`，但渲染仍继续执行，说明 mismatch 没有成为硬阻断。

### 2.2 1714 任务：生成 2 条剪辑，但两条都割裂

任务路径：

```text
outputs/欧盟成员国外长会议讨论多议题_20260525_1714
```

关键结果：

```text
short_video_plan：推荐 2 条
  - sv_01：25 秒，会议开幕与议题背景
  - sv_02：35 秒，比利时外交大臣表态

sv_01：
  - editing_script 约 22 秒，全部 ai_voiceover
  - voiceover 约 101 字，但内容主要是会议召开、外长入场、议题罗列
  - 问题：缺少核心冲突与新闻推进，像“会议预告”

sv_02：
  - editing_script 35 秒
  - 中间 25 秒 original_sound
  - voiceover 约 51 字，TTS 实际约 9.25 秒
  - cut_plan v3：video=35.0 秒，voice=9.25 秒，delta=25.75 秒，status=mismatch
```

直接问题：

1. 规划模型把同一条新闻拆成“会议背景”和“外相表态”两条，导致 `sv_01` 信息价值偏低，`sv_02` 又缺少足够背景。
2. `sv_02` 同样被大段原声占据，AI 解说只剩开头和结尾几句话。
3. 原本应当融合成一条 45-60 秒的完整新闻：会议背景 -> 外相指控 -> 为什么重要 -> 欧盟内部为何分歧 -> 收尾。

## 3. 当前代码与 Prompt 的问题定位

### 3.1 `config.toml`：默认目标仍是 30 秒

位置：

```text
config.toml
```

现状：

```toml
[short_video]
default_target_seconds = 30
normal_max_seconds = 35
context_max_seconds = 45
complex_max_seconds = 60
hard_max_without_confirmation = 60
allow_long_video_default = false
```

问题：

1. 30 秒作为默认值没有问题，但现在它被模型理解为强目标。
2. 用户勾选“允许长版”后，Web 仍把 `target_duration_seconds=30` 传入后端；如果 Prompt 没有明确说明“allow_long_video=true 时 30 秒只是默认参考，不是硬限制”，模型仍会继续按 30 秒压缩。

### 3.2 `web_static/index.html` 与 `web_static/app.js`：交互语义容易误导

位置：

```text
web_static/index.html
web_static/app.js
web_app.py
```

现状：

```html
<span>目标时长</span>
<input id="targetDuration" type="number" min="20" max="90" value="30" />

<input id="allowLongVideo" type="checkbox" />
<span>允许长版</span>
```

`app.js` 每次请求都会传：

```js
target_duration_seconds: Number($("targetDuration").value || 30),
allow_long_video: $("allowLongVideo").checked
```

问题：

1. 用户勾选“允许长版”后，系统仍同时传 `target_duration_seconds=30`。
2. 对模型来说，最强的数值信号仍是 30 秒。
3. “允许长版”在用户心里是“60 秒以内讲清楚”，但系统语义更像“超过 60 秒时允许继续”，两者不一致。

### 3.3 `newsclip_agent/prompts.py`：时长策略仍强推 30 秒

位置：

```text
newsclip_agent/prompts.py
```

现状：

```text
短视频时长策略：
1. 默认优先生成 30 秒左右的新闻高光短视频。
2. 快讯或单点新闻控制在 20-30 秒。
3. 普通新闻高光控制在 28-35 秒，默认目标 30 秒。
4. 如果缺少背景会造成误解，可扩展到 35-45 秒，但必须说明原因。
5. 如果是复杂多线事件，可扩展到 45-60 秒，但必须说明为什么 30-45 秒无法讲清。
6. 超过 60 秒默认不允许，除非用户明确确认生成长版。
```

问题：

1. 这段策略没有把 `run_options.allow_long_video` 作为强分支。
2. 它仍把 30 秒设为默认心理锚点。
3. 对于外交、战争、复杂政策、发布会表态这类新闻，模型会优先尝试压到 30 秒，导致背景、转折和结尾被牺牲。

### 3.4 `SHORT_VIDEO_PLANNER_PROMPT`：反碎片化规则不够硬

位置：

```text
newsclip_agent/prompts.py
SHORT_VIDEO_PLANNER_PROMPT
```

问题：

1. Prompt 说了“不要强行拆低价值片段”，但没有明确禁止把“会议开幕空镜”和“同一会议中的外相表态”拆成两条。
2. 没有要求模型判断多个候选片段是否属于同一新闻链条。
3. 没有要求输出“为什么不合并”或“为什么必须拆条”。

### 3.5 `EDITING_DIRECTOR_PROMPT`：原声证据没有时长上限

位置：

```text
newsclip_agent/prompts.py
EDITING_DIRECTOR_PROMPT
```

现状字段允许：

```json
"audio_mode": "ai_voiceover / original_sound / mixed_evidence"
```

问题：

1. Prompt 没有规定 `audio_policy=ai_voiceover` 时，`original_sound` 的单段最大时长和总占比。
2. 模型只要认为“权威讲话有证据价值”，就会把 20-25 秒讲话设为 `original_sound`。
3. 对 AI 配音新闻来说，权威讲话画面可以保留，但声音应由 AI 解说转述，原声只保留极短证据声。

### 3.6 `VOICEOVER_PROMPT`：主动让 AI 配音空掉主干段

位置：

```text
newsclip_agent/prompts.py
VOICEOVER_PROMPT
```

现状：

```text
如果某个 shot 的 audio_mode 是 original_sound，该段不写或少写 AI 解说，并说明保留原声原因。
```

问题：

1. 这条规则直接造成 1856 任务中 20 秒主干段 `text=""`。
2. AI 配音稿无法形成完整叙事，只剩开头和结尾的碎片句。
3. 由于 `narration_text` 是所有非空 segment 拼起来的，TTS 真实时长自然远短于画面时长。

### 3.7 `duration_policy.py`：原声只按模式给音量，没有按比例限制

位置：

```text
newsclip_agent/duration_policy.py
original_audio_volume_for_clip
validate_audio_policy
```

现状：

```python
if mode in {"original_sound", "mixed_evidence"} or clip.get("must_keep_original_audio") or clip.get("original_audio_required"):
    return settings.evidence_original_audio_volume
```

问题：

1. 只要模型输出 `original_sound` 或 `mixed_evidence`，原声就是 1.0 满音量。
2. `validate_audio_policy` 只检查普通片段原声是否为 0、`mixed_evidence` 是否有 reason，没有检查：
   - 原声总时长是否超过上限；
   - 原声占比是否超过上限；
   - `ai_voiceover_main` 下是否存在过长 `original_sound`；
   - AI 配音实际时长是否足够支撑目标时长。

### 3.8 `pipeline.py`：mismatch 被记录但未阻断

位置：

```text
newsclip_agent/pipeline.py
step_cut_plan
```

现状：

```python
# if self.options.audio_policy != "original" and tts_success and duration_delta > 1.0:
#     blocked_reasons.append(f"画面总时长 {video_duration:.1f}s 与 AI 配音 {voiceover_duration:.1f}s 误差超过 1 秒")
duration_status = "blocked" if blocked_reasons else ("mismatch" if duration_delta > 1.0 else "ok")
```

问题：

1. 1856 和 1714 都已经出现严重 mismatch，但因为阻断逻辑被注释，只是标记为 `mismatch`。
2. `step_render` 只阻断 `duration_status=="blocked"`，不会阻断 `mismatch`。
3. 结果就是明知配音只有 8-9 秒，仍渲染 30-35 秒画面。

## 4. 优化目标

### 4.1 产品目标

1. 用户选择 AI 配音时，成片必须以 AI 解说为第一音轨和主叙事。
2. 用户勾选“允许长版”时，含义应是“允许在 60 秒以内讲清楚”，而不是仍强卡 30 秒。
3. 对外交、战争、政策、发布会、复杂争议新闻，默认允许 45-60 秒。
4. 原声只能作为短证据声，不允许占据主体。
5. 成片必须有完整的开头、背景、核心事实、解释和结尾。

### 4.2 可量化验收指标

```text
AI 配音策略下：
- AI 解说覆盖时长 >= 成片时长的 80%
- 原声总时长 <= min(6 秒, 成片时长的 15%)
- 单段原声 <= 4 秒；特殊情况最多 6 秒，必须有 reason
- narration_text 字数应匹配目标时长：
  45 秒：约 150-190 字
  50 秒：约 170-210 字
  60 秒：约 200-250 字
- video_duration 与 voiceover_duration 差值 <= 1 秒，否则阻断
- 成片必须包含 ending_sentence，且结尾镜头不能只靠淡出替代新闻收束
```

## 5. 具体修改方案

### 5.1 修改 `newsclip_agent/prompts.py` 的 `SHORT_VIDEO_DURATION_POLICY`

建议把现有时长策略改为“按 `allow_long_video` 分支”的版本。

替换方向：

```text
短视频时长策略：

你必须读取输入中的 run_options 和 duration_strategy，尤其是：
- run_options.target_duration_seconds
- run_options.allow_long_video
- run_options.audio_policy
- duration_strategy.hard_max_without_confirmation
- duration_strategy.chars_per_second

1. 如果 allow_long_video=false：
   - 快讯/单点新闻：20-30 秒。
   - 普通新闻高光：28-35 秒，默认目标 30 秒。
   - 需要少量背景：35-45 秒，但必须说明为什么 30 秒讲不清。
   - 超过 60 秒必须设置 requires_user_long_video_confirmation=true。

2. 如果 allow_long_video=true：
   - target_duration_seconds=30 只表示默认参考，不是硬限制。
   - 你的目标是“在 60 秒以内讲清楚”，不得为了贴近 30 秒牺牲背景、原因、冲突和结尾。
   - 快讯仍可 25-35 秒。
   - 外交表态、政策争议、战争冲突、发布会交锋、复杂多主体新闻，优先选择 45-60 秒。
   - 除非用户另有明确要求，不要输出超过 60 秒的方案。
   - 如果 60 秒以内仍讲不清，才允许设置 requires_user_long_video_confirmation=true，并说明原因。

3. 时长不是越短越好。必须先判断“讲清楚所需的最低时长”，再决定目标时长。

4. 不得因为 30 秒默认值而删除以下内容：
   - 事件发生的时间、地点、主体；
   - 核心表态或动作；
   - 必要背景和冲突原因；
   - 分歧、后果或最新状态；
   - 明确收尾句。
```

这段修改解决的问题：

1. 让模型明确知道 `allow_long_video=true` 时 30 秒不是硬限制。
2. 将用户这次的真实诉求落到 Prompt：60 秒以内讲透即可。
3. 避免把复杂新闻压缩成“几句 AI 解说 + 大段原声”。

### 5.2 修改 `newsclip_agent/prompts.py` 的 AI 配音主导策略

建议在 `SHORT_VIDEO_DURATION_POLICY` 或单独新增 `AI_VOICEOVER_AUDIO_POLICY` 公共片段。

新增内容：

```text
AI 配音主导策略：

1. 如果 run_options.audio_policy == "ai_voiceover"：
   - AI 旁白是第一音轨，也是新闻叙事主线。
   - 原视频声音默认静音。
   - 画面中人物讲话、发布会、采访、外长表态等，都可以作为 B-roll 画面使用，但其内容应由 AI 配音转述、翻译、解释。

2. 不得把长段权威讲话直接设置为 original_sound。
   - “权威人物讲话有新闻价值”不等于“必须保留长原声”。
   - 权威讲话的证据价值可以通过画面、身份字幕、关键字幕、AI 转述共同完成。

3. 如果确实需要保留原声作为证据：
   - audio_mode 优先使用 mixed_evidence，而不是 original_sound。
   - 单段原声控制在 3-4 秒，极限不超过 6 秒。
   - 全片原声总时长不得超过 min(6 秒, 成片时长的 15%)。
   - 必须填写 original_audio_reason，并说明为什么 AI 转述不足以替代这几秒原声。
   - 原声结束后 AI 解说必须立即承接解释，不得出现长时间叙事空白。

4. 如果模型计划保留超过 6 秒原声，必须改写为 AI 配音转述。

5. 在 ai_voiceover 策略下，禁止出现 20 秒以上 original_sound 主干段。
```

这段修改解决的问题：

1. 防止 1856 的 20 秒原声、1714 的 25 秒原声再次出现。
2. 保留权威画面的新闻证据价值，但把声音主导权交还给 AI 配音。
3. 让 “AI 配音” 在用户感知中名实相符。

### 5.3 修改 `SHORT_VIDEO_PLANNER_PROMPT`：增加反碎片化和高价值融合规则

位置：

```text
newsclip_agent/prompts.py
SHORT_VIDEO_PLANNER_PROMPT
```

建议在“原则”后加入：

```text
反碎片化与高价值融合规则：

1. 不得为了增加视频数量，把同一条新闻链条拆成多个低完整度短视频。

2. 如果多个 candidate_clips 共同构成同一事件的“背景 -> 核心表态 -> 分歧/后果”，应优先合并为一条更完整的视频，而不是拆成：
   - 一条会议开幕/空镜快讯；
   - 一条缺少背景的单独表态。

3. 以下内容通常不能单独成片，除非它本身就是唯一核心新闻：
   - 会议开场；
   - 外景空镜；
   - 代表入场；
   - 议题罗列；
   - 无明确进展的背景铺垫。

4. 对外交、政策、战争、发布会类新闻，如果核心表态需要背景才能理解，应优先生成 1 条 45-60 秒的完整解释型短视频。

5. 只有当多个片段存在清晰独立新闻主题、独立事实增量、独立受众价值时，才推荐多条视频。

6. 输出 recommended_video_count 前，必须先判断：
   - 这些片段是否属于同一新闻链条；
   - 拆开后是否会导致任一条没头没尾；
   - 合并后是否更能讲清新闻。
```

建议扩展 `short_videos` 字段：

```json
{
  "minimum_explainable_seconds": 0,
  "recommended_reasonable_seconds": 0,
  "merge_related_clips": true,
  "merge_reason": "",
  "narrative_completeness_score": 0,
  "voiceover_required_seconds": 0,
  "original_sound_budget_seconds": 0,
  "expected_ai_voiceover_ratio": 0
}
```

对 1714/1856 这类欧盟外长会素材，期望规划变为：

```text
recommended_video_count=1
target_duration_seconds=50-60
duration_tier=complex
audio_strategy=ai_voiceover_main
source_clip_ids 同时包含会议背景和外相表态
reason：会议背景单独成片价值不足，外相表态脱离背景又难以理解，应合并为一条完整新闻
```

### 5.4 修改 `EDITING_DIRECTOR_PROMPT`：限制原声并强制完整结构

位置：

```text
newsclip_agent/prompts.py
EDITING_DIRECTOR_PROMPT
```

建议加入：

```text
AI 配音模式下的剪辑规则：

1. 如果 short_video_plan.audio_strategy == "ai_voiceover_main" 或 run_options.audio_policy == "ai_voiceover"：
   - 默认每个镜头 audio_mode=ai_voiceover。
   - 原视频声音默认不进入成片。
   - 人物讲话画面可以保留，但作为 AI 解说的背景画面。

2. 原声证据预算：
   - 全片 original_sound/mixed_evidence 总时长不得超过 original_sound_budget_seconds。
   - 如果计划字段没有给出预算，默认最多 6 秒。
   - 单段原声最多 4 秒，极特殊情况最多 6 秒。
   - 禁止输出 20 秒以上 original_sound。

3. 对权威讲话画面：
   - 不要直接保留完整讲话原声。
   - 应选择 2-4 秒最有代表性的画面作为证据画面；
   - 讲话内容由 AI 配音转述、解释、补背景。

4. 每条视频必须包含完整叙事段落：
   - opening_hook：引入核心新闻；
   - context：交代必要背景；
   - main_fact：讲清核心事实或表态；
   - analysis_or_conflict：解释为什么重要、分歧在哪里；
   - ending：明确收尾。

5. 如果目标时长是 45-60 秒，不能只给 3 个镜头，建议拆成 5-7 个镜头，让 AI 解说有足够空间完成叙事。
```

针对欧盟外长会素材，推荐镜头结构：

```text
0-6 秒：会议地点、外长会背景，AI 引入
6-14 秒：比利时外相采访画面，AI 交代人物身份和表态对象
14-22 秒：讲话特写/字幕画面，AI 转述“定居者暴力”核心指控
22-26 秒：可选 3-4 秒 mixed_evidence 原声，仅保留最关键一句
26-40 秒：会议/外交场景画面，AI 解释欧以协定、权利与价值原则、为什么该表态重要
40-52 秒：成员国/会议画面，AI 讲欧盟内部存在分歧
52-58 秒：收尾镜头，AI 给出明确结尾
```

### 5.5 修改 `VOICEOVER_PROMPT`：不再让主干段空白，改为“新闻起承转合”

位置：

```text
newsclip_agent/prompts.py
VOICEOVER_PROMPT
```

需要删除或改写现有规则：

```text
如果某个 shot 的 audio_mode 是 original_sound，该段不写或少写 AI 解说，并说明保留原声原因。
```

建议替换为：

```text
如果某个 shot 是 mixed_evidence 或 original_sound：
- 只有在原声实际播放的 3-6 秒内可以不写 AI 解说；
- 不能因为一个长镜头被标记为 original_sound，就让整段 AI 解说空白；
- 如果该 shot 超过 6 秒，必须把超出部分视为 ai_voiceover 画面，由 AI 解说承接；
- 原声前后必须有 AI 解说解释其背景、含义和后续影响。
```

新增新闻解说完整性规则：

```text
AI 新闻解说必须满足“起、承、转、合”：

1. 起：用 1-2 句交代事件、地点、人物和核心看点。
2. 承：讲清核心事实，尤其是谁说了什么、针对什么问题。
3. 转：解释为什么这件事重要，背后有哪些分歧、原则或风险。
4. 合：给出明确收尾，不得突然结束。

如果 target_duration_seconds >= 45：
- narration_text 不得少于 target_duration_seconds * chars_per_second * 0.75 个字。
- 50 秒视频建议 170-210 字。
- 60 秒视频建议 200-250 字。
- 不得只写 40-60 字的摘要式文案。

如果无法写满，是上游剪辑脚本的问题，应在 revise_suggestion 中明确要求重写 editing_script，而不是输出过短文案。
```

建议扩展输出字段：

```json
{
  "narrative_sections": {
    "opening": "",
    "background": "",
    "core_fact": "",
    "analysis_or_conflict": "",
    "ending": ""
  },
  "completion_check": {
    "has_opening": true,
    "has_background": true,
    "has_core_fact": true,
    "has_analysis_or_conflict": true,
    "has_ending": true,
    "ai_voiceover_ratio_expected": 0.85,
    "original_sound_total_seconds": 0
  }
}
```

### 5.6 修改 `RISK_REVIEW_PROMPT`：加入成片观感风险

位置：

```text
newsclip_agent/prompts.py
RISK_REVIEW_PROMPT
```

建议加入检查项：

```text
除新闻风险外，还必须检查成片完整性风险：
- 是否像没讲完；
- 是否只有开头几句 AI 解说，中间长时间原声，结尾匆忙；
- AI 配音是否足够解释背景、核心事实、分歧和结尾；
- 原声是否在 AI 配音模式下占比过高；
- 是否为了拆多条导致单条信息价值不足；
- 是否存在 video_duration 与 voiceover_duration 明显不一致。
```

若发现上述问题，`can_publish` 必须为 `false`，并给出修复建议：

```text
重写 short_video_plan：合并相关片段，改为 45-60 秒；
重写 editing_script：减少 original_sound，增加 ai_voiceover 镜头；
重写 voiceover_script：补齐起承转合；
阻断 render：等待时长和音轨一致后再渲染。
```

## 6. 必要的代码兜底方案

虽然本次主要是 Prompt 优化，但只靠 Prompt 不够稳定。建议后续代码增加兜底，防止模型再次输出不合格方案。

### 6.1 `duration_policy.py` 增加 AI 配音原声比例校验

建议新增配置：

```toml
[voiceover]
ai_voiceover_min_ratio = 0.80
ai_voiceover_original_audio_max_seconds = 6
ai_voiceover_original_audio_max_ratio = 0.15
ai_voiceover_original_audio_single_max_seconds = 4
```

建议在 `validate_audio_policy` 中增加：

```text
如果 audio_policy=ai_voiceover_main：
- 统计 clips 中 original_sound/mixed_evidence 的总时长；
- 原声总时长 > 6 秒则 blocked；
- 原声占比 > 15% 则 blocked；
- 单段 original_sound > 4 秒则 blocked；
- original_sound 没有 reason 则 blocked；
- voiceover_duration_seconds / video_duration_seconds < 0.80 则 blocked。
```

这样即使 Prompt 偶尔失控，也不会生成“AI 配音名义下的大段原声视频”。

### 6.2 `pipeline.py` 恢复时长 mismatch 阻断

位置：

```text
newsclip_agent/pipeline.py
step_cut_plan
```

建议恢复并加强这段逻辑：

```text
如果 audio_policy != original 且 TTS 成功：
- duration_delta > 1.0 秒时，直接 blocked；
- 不允许只是标记 mismatch 后继续 render；
- blocked_reasons 写清 video_duration、voiceover_duration、delta。
```

对 1856 来说：

```text
video=30.0 秒，voice=8.04 秒，delta=21.96 秒
```

应在 `cut_plan` 阶段阻断，而不是继续渲染。

### 6.3 `pipeline.py` 的 `_duration_strategy_payload` 增加更明确的策略字段

位置：

```text
newsclip_agent/pipeline.py
_duration_strategy_payload
```

建议增加：

```json
{
  "allow_long_video_meaning": "allow_complete_story_within_60_seconds",
  "when_allow_long_video_target_range": [45, 60],
  "target_duration_is_soft_when_allow_long_video": true,
  "ai_voiceover_original_audio_max_seconds": 6,
  "ai_voiceover_original_audio_max_ratio": 0.15,
  "ai_voiceover_min_ratio": 0.8,
  "block_on_duration_mismatch": true
}
```

这能让 Prompt 输入更清晰，不再让模型只看到 `target_duration_seconds=30`。

### 6.4 `pipeline.py` 的 `_render_one` 避免再次放大原声

位置：

```text
newsclip_agent/pipeline.py
_render_one
```

当前渲染流程是先按片段 `original_audio_volume` 导出音频，再与 AI 配音混合：

```text
[0:a]volume=1.0[a0];[1:a]volume=voiceover[a1];amix=duration=first
```

建议分阶段优化：

第一阶段：

```text
当 audio_policy=ai_voiceover_main 且没有 mixed_evidence 片段时：
- 最终音频直接使用 AI 配音；
- 不再把原视频音轨输入 amix；
- 视频时长按 voiceover_duration 对齐。
```

第二阶段：

```text
只有 mixed_evidence 片段才精细混入原声；
原声片段播放时 AI 配音暂停或 ducking；
原声总时长仍受 validate_audio_policy 控制。
```

### 6.5 `web_static/index.html` 和 `web_static/app.js` 调整交互语义

建议把“允许长版”改为更贴近用户理解的文案：

```text
允许讲清楚（最长 60 秒）
```

交互建议：

```text
1. 当用户勾选该项且目标时长仍是默认 30：
   - 前端可传 target_duration_mode="auto_within_60"；
   - 或后端把 target_duration_seconds 解释为软目标。

2. 目标时长输入旁提示：
   - 30 秒：适合快讯；
   - 45-60 秒：适合需要背景解释的新闻；
   - 勾选“允许讲清楚”后，系统会在 60 秒内自动选择更合适时长。
```

如果暂时不加新字段，至少要在 Prompt 中明确：

```text
allow_long_video=true 且 target_duration_seconds=30 时，30 秒不是硬限制。
```

## 7. 针对欧盟外长会素材的期望输出示例

### 7.1 规划层期望

```json
{
  "recommended_video_count": 1,
  "overall_reason": "会议开幕本身独立新闻价值不足，外相表态脱离会议背景又难以理解，应合并为一条完整的外交新闻解释型短视频。",
  "short_videos": [
    {
      "short_video_id": "sv_01",
      "topic": "欧盟外长会聚焦中东 比利时外相严批定居者暴力",
      "video_type": "解读型",
      "duration_tier": "complex",
      "target_duration_seconds": 55,
      "max_allowed_seconds": 60,
      "minimum_explainable_seconds": 48,
      "recommended_reasonable_seconds": 55,
      "audio_strategy": "ai_voiceover_main",
      "original_sound_budget_seconds": 4,
      "expected_ai_voiceover_ratio": 0.9,
      "merge_related_clips": true,
      "merge_reason": "会议背景、外相表态和成员国分歧属于同一新闻链条，拆开会导致一条空、一条突兀。"
    }
  ]
}
```

### 7.2 剪辑层期望

```text
总时长：50-58 秒
原声：最多 3-4 秒 mixed_evidence
AI 配音：覆盖其余全部镜头

镜头结构：
1. 会议现场/外景：6 秒，AI 介绍卢森堡欧盟外长会背景。
2. 外相采访特写：8 秒，AI 介绍普雷沃身份和表态对象。
3. 讲话/字幕画面：10 秒，AI 转述定居者暴力和欧盟价值原则冲突。
4. 关键原声证据：3-4 秒，仅保留最有代表性一句。
5. 会议/成员国画面：12 秒，AI 解释欧盟内部对以政策分歧。
6. 收尾画面：8 秒，AI 总结欧盟统一立场面临考验。
```

### 7.3 配音文案方向

示例不是最终稿，只说明结构：

```text
在卢森堡举行的欧盟外长会上，中东局势再次成为焦点。
比利时外交大臣普雷沃在会场外公开表态，将矛头指向约旦河西岸定居者暴力。
他认为，这类行为已经触及支撑欧盟与以色列关系的权利和价值原则。
这番话之所以引发关注，不只是措辞强硬，更在于它把欧盟内部长期存在的分歧推到台前。
一方面，部分成员国希望对以色列采取更明确的限制措施；另一方面，暂停或调整相关协定仍需要成员国形成足够共识。
因此，这场外长会真正考验的，不只是欧盟对中东局势的态度，也包括它能否在重大外交议题上维持统一立场。
```

这类文案约 180-220 字，适合 45-60 秒，不会再出现 40 多字讲不清的问题。

## 8. 推荐实施顺序

### 阶段一：只改 Prompt，快速验证

涉及文件：

```text
newsclip_agent/prompts.py
```

修改内容：

```text
1. SHORT_VIDEO_DURATION_POLICY：加入 allow_long_video=true 的 45-60 秒分支。
2. AI 配音主导策略：限制原声总时长、单段时长和占比。
3. SHORT_VIDEO_PLANNER_PROMPT：加入反碎片化、高价值融合规则。
4. EDITING_DIRECTOR_PROMPT：禁止 AI 配音模式下输出长段 original_sound。
5. VOICEOVER_PROMPT：删除 original_sound 主干空白规则，加入起承转合和最低字数要求。
6. RISK_REVIEW_PROMPT：加入“像没讲完”“原声占比过高”“配音时长不匹配”检查。
```

验证方式：

```text
对 1714 和 1856 两个任务从 short_video_planning 重新跑：
--rerun-from short_video_planning
```

期望：

```text
recommended_video_count 从 2 或 1 的 30 秒方案，变为 1 条 50-60 秒完整方案；
editing_script 中 original_sound 总时长 <= 6 秒；
voiceover_script 字数 >= 170 字；
cut_plan 中 video_duration 与 voiceover_duration 差值 <= 1 秒。
```

### 阶段二：加代码兜底，防止 Prompt 失控

涉及文件：

```text
newsclip_agent/duration_policy.py
newsclip_agent/pipeline.py
config.toml
tests/test_audio_policy.py
tests/test_editing_duration_validation.py
tests/test_voiceover_duration_policy.py
```

修改内容：

```text
1. 增加 AI 配音模式下原声总时长、单段时长、占比校验。
2. 恢复 duration_delta > 1 秒的阻断。
3. cut_plan 阶段检查 voiceover_duration / video_duration 比例。
4. tests 增加“20 秒 original_sound 在 ai_voiceover_main 下必须 blocked”的用例。
5. tests 增加“video=30 秒、voiceover=8 秒必须 blocked”的用例。
```

### 阶段三：调整 Web 语义

涉及文件：

```text
web_static/index.html
web_static/app.js
web_app.py
```

修改内容：

```text
1. “允许长版”改成“允许讲清楚（最长 60 秒）”。
2. 如果勾选该项且目标时长是默认 30，后端或 Prompt 将其解释为 soft target。
3. 可新增 target_duration_mode=auto_within_60，避免 30 秒数值继续误导模型。
```

## 9. 回归测试清单

### 9.1 针对 1856

必须满足：

```text
不再生成 30 秒、20 秒原声的方案；
不再生成 43 字 AI 文案；
不再允许 video=30 秒、voiceover=8 秒继续渲染；
最终方案应为 45-60 秒，AI 配音主导，有明确结尾。
```

### 9.2 针对 1714

必须满足：

```text
不再把会议开幕空镜单独拆成一条低价值快讯；
不再把外相表态做成 25 秒原声；
优先合并为一条完整解释型视频；
如果仍拆两条，必须证明两条都有独立事实增量和完整叙事。
```

### 9.3 通用测试

新增或更新测试：

```text
test_ai_voiceover_blocks_long_original_sound
test_ai_voiceover_blocks_high_original_sound_ratio
test_cut_plan_blocks_voiceover_video_duration_mismatch
test_allow_long_video_softens_default_30_seconds
test_voiceover_requires_narrative_completion_for_complex_news
```

## 10. 验收标准

本问题修复后，合格成片应满足：

```text
1. 用户选择 AI 配音时，观众主要听到 AI 解说，而不是原片原声。
2. 复杂新闻在允许长版时可自然扩展到 45-60 秒，不再被 30 秒卡死。
3. AI 解说能讲清背景、核心事实、分歧和结尾。
4. 原声只作为短证据，不再占据 60%-70% 成片时长。
5. 同一新闻链条不会被拆成“空背景”和“突兀表态”两条低完成度视频。
6. 如果画面时长和 AI 配音时长明显不一致，系统必须阻断，而不是生成一个明知不匹配的成片。
```

## 11. 最终建议

这次不要把方案理解成“把默认 30 秒改成默认 60 秒”。真正要改的是决策逻辑：

```text
30 秒适合快讯；
45-60 秒适合需要讲清背景和冲突的新闻；
AI 配音模式下，原声不是主叙事；
允许长视频时，目标是在 60 秒内讲透，而不是硬凑 30 秒。
```

对当前欧盟外长会样本，最合理的目标不是 25 秒或 30 秒，也不是 90 秒，而是一条 50-60 秒的 AI 解说主导型完整新闻短视频。
