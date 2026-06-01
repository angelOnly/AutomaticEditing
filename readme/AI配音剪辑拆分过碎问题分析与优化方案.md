# AI 配音剪辑“视频拆分过碎”问题分析与代码优化方案

## 0. 结论先说

本次案例中，原视频被拆成了 2 条成片：

- `v_001`：最终 13.00 秒
- `v_002`：最终 21.36 秒

从内容结构和传播完整性看，这个案例 **不应该拆成 2 条**，更适合合成 **1 条 50-60 秒左右的完整 AI 新闻解说视频**。

当前问题不是单一环节导致的，而是以下几个设计叠加造成的：

1. `short_video_edit_plan` 阶段过度相信大模型拆条判断，把同一新闻链拆成了多条。
2. prompt 虽然要求“完整新闻事实链路”，但没有足够硬的“同链不拆”规则。
3. `voiceover_script` 阶段目标 45/50 秒，但实际配音文案只有 70/113 个字，严重不足。
4. `cut_plan` 阶段使用 `compact_segmented` 紧凑时间线，最终视频时长跟着 TTS 实际音频走。
5. 代码已经发现“实际成片明显低于目标时长”，但只是 warning，没有阻断渲染。

因此优化方向应该是：

> **默认优先合成 1 条完整 AI 解说视频；只有多个片段属于不同新闻事件或独立传播角度时，才允许拆多条；同时目标 45-60 秒但文案/成片明显不足时必须阻断。**

---

## 1. 本次案例复盘

### 1.1 候选片段情况

系统识别出了 4 个候选片段：

| clip_id | 时间范围 | 时长 | 内容摘要 |
|---|---:|---:|---|
| `clip_001` | `00:00:00.000-00:01:00.000` | 60 秒 | 美伊边谈边打，伊朗革命卫队警告若停火协议遭违反将对等回应 |
| `clip_002` | `00:01:00.000-00:02:00.000` | 60 秒 | 白宫关于霍尔木兹海峡通航相关协议的表态，以及卢比奥关于谈判仍继续的发言 |
| `clip_003` | `00:02:00.000-00:03:00.000` | 60 秒 | 伊朗核问题是美伊谈判核心分歧，触及伊朗核心利益 |
| `clip_004` | `00:03:00.000-00:04:35.342` | 95.342 秒 | 特朗普政府推动对伊谈判背后的政治诉求，以及后续局势预判 |

这些片段不是互相独立的新闻，而是同一条新闻的完整链路：

```text
美伊边谈边打
↓
伊朗警告停火若被违反将回应
↓
白宫/卢比奥释放谈判仍在推进的信号
↓
核问题是核心分歧
↓
特朗普政府希望形成外交成果
↓
后续局势风险判断
```

所以，内容上应该合并成 1 条完整解说，而不是拆成多个碎片。

---

### 1.2 当前短视频规划结果

`short_video_edit_plan.json` 中，大模型输出：

```json
{
  "recommended_video_count": 2,
  "overall_reason": "本次从美伊局势时政评论素材中筛选2条具备独立传播价值的高光短视频，分别聚焦谈判核心分歧、最新局势及美方谈判动机，信息完整无断章取义风险，符合时政新闻短视频传播要求"
}
```

两条视频分别是：

| short_video_id | topic | 目标时长 | source_clip_ids | 问题 |
|---|---|---:|---|---|
| `v_001` | 美伊停火谈判核心分歧解析 | 45 秒 | `clip_003` | 只讲核问题，缺少前情、谈判状态、美方动机 |
| `v_002` | 美伊最新局势及美方谈判政治考量 | 50 秒 | `clip_001`, `clip_004` | 缺少 `clip_002` 和核心分歧承接，链路不完整 |

这里的核心问题是：模型把“同一新闻链中的不同段落”误判成了“两个独立短视频角度”。

---

### 1.3 配音文案严重偏短

`voiceover_script.json` 中，实际文案为：

| short_video_id | 目标时长 | 实际字数 | 估算时长 | duration_fit |
|---|---:|---:|---:|---|
| `v_001` | 45 秒 | 70 字 | 16.67 秒 | `too_short` |
| `v_002` | 50 秒 | 113 字 | 26.9 秒 | `too_short` |

`v_001` 的文案：

```text
当前美伊正处于边谈边打状态，双方停火谈判的核心难点集中在伊朗核问题。是否放弃高浓缩铀、拆除核能力触及伊朗核心利益，是目前谈判推进的主要障碍。
```

它只有 70 字，明显不可能支撑 45 秒视频。

`v_002` 的文案约 113 字，也明显不够支撑 50 秒。

这说明：

1. 目标时长已经给到了 45/50 秒；
2. 大模型自己也标记了 `duration_fit=too_short`；
3. 但代码流程仍然继续执行 TTS、字幕、剪辑和渲染；
4. 最终导致成片只有 13 秒和 21 秒。

---

### 1.4 最终渲染结果

`render_outputs.json` 中最终输出：

```json
{
  "outputs": [
    {
      "short_video_id": "v_001",
      "quality_check": {
        "final_duration_seconds": 13.0,
        "voiceover_duration_seconds": 12.96,
        "duration_status": "ok",
        "voiceover_timeline_mode": "compact_segmented"
      }
    },
    {
      "short_video_id": "v_002",
      "quality_check": {
        "final_duration_seconds": 21.36,
        "voiceover_duration_seconds": 21.32,
        "duration_status": "ok",
        "voiceover_timeline_mode": "compact_segmented"
      }
    }
  ]
}
```

最终视频时长几乎等于 TTS 时长：

```text
v_001：TTS 12.96 秒 → 成片 13.00 秒
v_002：TTS 21.32 秒 → 成片 21.36 秒
```

这说明当前 AI 配音模式下，`compact_segmented` 时间线会把画面压到配音音频时长附近。

所以只要配音文案过短，最终视频必然变短。

---

## 2. 当前代码流程中的问题点

### 2.1 `step_short_video_edit_plan` 没有后处理兜底

当前代码位置：`newsclip_agent/pipeline.py`

当前逻辑：

```python
def step_short_video_edit_plan(self) -> None:
    result = self._run_text_agent("short_video_edit_plan", {
        "content_analysis": self._load_step_json("content_analysis"),
        "timeline_digest": self._load_step_json("timeline_digest"),
        "duration_strategy": self._duration_strategy_payload(),
        "run_options": self._run_options_payload(),
    })
    self._write_compat_short_video_plan_and_editing_script(result)
```

问题：

- 大模型输出几条，系统就接受几条。
- 没有判断这些 scripts 是否属于同一新闻链。
- 没有判断拆出来的每条是否足够完整。
- 没有判断是否存在“目标 45 秒但事实点只有 1-2 个”的情况。
- 没有自动合并逻辑。

建议：

```python
result = self._normalize_short_video_split_decision(result)
```

在写入兼容脚本前，对拆条结果做一次代码兜底。

---

### 2.2 `SHORT_VIDEO_EDIT_PLAN_PROMPT` 约束不够硬

当前 prompt 位置：`newsclip_agent/prompts.py`

当前核心要求：

```text
1. 输出 scripts，每条短视频必须有完整新闻事实链路。
2. 每个镜头必须使用原视频时间码 source_start/source_end，不得编造不存在的时间。
3. AI 配音解说模式下，默认所有镜头 audio_mode=ai_voiceover。
4. 不要输出原声价值判断、risk_review 或发布审核字段。
5. editing_structure 只保留后续剪辑和配音需要的轻量字段。
```

这些要求没有明确告诉模型：

- 默认应该合成 1 条；
- 同一新闻链不能拆；
- 外交、战争、政策类新闻优先完整解说；
- 预计低于 30 秒的内容不能单独成片；
- 拆成多条必须说明 `split_reason`。

因此模型容易为了“短视频数量”而拆出多个碎片。

---

### 2.3 `step_voiceover_script` 没有校验文案长度

当前代码：

```python
def step_voiceover_script(self) -> None:
    edit_plan = self._load_step_json("short_video_edit_plan")
    editing = self._load_step_json("editing_script")
    plan = self._load_step_json("short_video_planning")
    plan_by_id = {x.get("short_video_id"): x for x in plan.get("short_videos", []) if isinstance(x, dict)}
    contracts = [
        build_voiceover_timing_contract(
            script,
            plan_by_id.get(script.get("short_video_id"), {}),
            chars_per_second=self.duration_settings.chars_per_second,
        )
        for script in editing.get("scripts", [])
    ]
    final_output = self._run_text_agent("voiceover_script", {
        "short_video_edit_plan": edit_plan,
        "editing_script": {"scripts": editing.get("scripts", [])},
        "voiceover_timing_contracts": contracts,
        "duration_strategy": self._duration_strategy_payload(),
        "run_options": self._run_options_payload(),
    })
    self._normalize_voiceover_scripts(final_output)
    self._enforce_long_video_confirmation_after_voiceover(final_output)
```

问题：

- `voiceover_script` 输出 `duration_fit=too_short`，系统仍继续。
- `target_duration_seconds=45`，但 `narration_text=70` 字，系统仍继续。
- 没有校验 `narrative_sections` 是否完整。
- 没有把“目标时长”和“配音字数”强绑定。

建议：

```python
self._validate_voiceover_script_duration_or_raise(final_output)
```

放在 `_normalize_voiceover_scripts` 后面。

---

### 2.4 `compact video duration below target` 只是 warning

当前代码中有类似逻辑：

```python
if (
    tts_success
    and tts_item.get("timeline_mode") == "compact_segmented"
    and min_compact_duration > 0
    and video_duration < min_compact_duration
):
    repair_reasons.append(
        f"compact video duration {video_duration:.1f}s is below "
        f"{self.duration_settings.compact_min_target_ratio:.0%} of target {target_duration:.1f}s; "
        "rerun voiceover_script with richer narration or add supporting clips"
    )
    warnings.append(repair_reasons[-1])
```

问题：

- 代码已经知道最终视频明显低于目标时长。
- 但是只写入 warning，没有阻断。
- 后续仍然渲染，最终用户看到 13 秒、21 秒成片。

建议：

- 对 AI 配音模式，低于目标比例时应该 `raise RuntimeError`。
- 不要让明显不合格的视频进入最终输出列表。

---

## 3. 拆几个视频的判断标准

### 3.1 默认策略

AI 配音剪辑模式建议默认策略：

```text
默认生成 1 条完整新闻解说视频。
只有明确满足拆分条件时，才允许输出多条。
```

原因：

- AI 配音剪辑的价值是“重新组织事实链，讲清楚一件事”；
- 不是简单截取几个碎片；
- 新闻解说尤其需要背景、事实、分歧、动机、影响的完整结构；
- 过度拆分会让每条视频都变成“观点碎片”，信息不完整。

---

### 3.2 允许拆多条的条件

只有满足下面任意一种，才允许拆成多条：

#### 条件一：多个片段属于不同新闻事件

例如：

```text
clip_001-clip_002：美伊谈判
clip_003-clip_004：俄乌冲突
clip_005：美国大选
```

这种必须拆，因为不是同一件事。

#### 条件二：同一大事件下有不同独立传播角度

例如：

```text
视频 1：停火协议三阶段框架是什么
视频 2：霍尔木兹海峡为什么重要
视频 3：特朗普政府的中东外交算盘
```

但前提是：每条都可以独立讲清，不能只截一个事实点。

#### 条件三：合并后明显超过允许时长

例如：

```text
用户目标：60 秒以内
完整内容：至少需要 120 秒
```

这种可以拆成 2 条。

#### 条件四：每条都有独立受众价值

用户即使不看其他视频，也能理解这一条的背景、核心事实、冲突和结论。

---

### 3.3 必须合并的情况

满足下面任意一种，应该强制合并：

1. 多个片段共享同一组核心实体，例如“美伊、伊朗、美国、特朗普”。
2. 多个片段构成同一新闻链条：背景 → 表态 → 分歧 → 动机 → 风险。
3. 某条短视频预计低于 30 秒。
4. 某条短视频只有 1-2 个事实点。
5. 合并后总时长仍在 45-75 秒内。
6. 属于外交、战争、政策、国际冲突类新闻，且没有明显不同事件。

本次案例完全符合“必须合并”。

---

## 4. 推荐的最终成片结构

本次案例建议输出 1 条视频：

```json
{
  "recommended_video_count": 1,
  "target_duration_seconds": 55,
  "max_allowed_seconds": 60,
  "source_clip_ids": ["clip_001", "clip_002", "clip_003", "clip_004"],
  "topic": "美伊边谈边打，停火谈判卡在伊朗核问题",
  "news_angle": "梳理美伊停火谈判的现状、核心分歧和特朗普政府的政治考量"
}
```

建议叙事结构：

| 时间 | 内容 |
|---:|---|
| 0-6 秒 | 美伊边谈边打，停火谈判仍在推进，但局势并不稳定 |
| 6-15 秒 | 伊朗革命卫队警告，如果停火被违反，将对等回应 |
| 15-25 秒 | 白宫和卢比奥释放谈判仍在推进的信号 |
| 25-38 秒 | 真正难点是伊朗核问题，高浓缩铀和核能力触及伊朗核心利益 |
| 38-50 秒 | 特朗普政府希望把谈判结果包装成中东外交胜利 |
| 50-58 秒 | 收尾：能否真正停火，取决于双方能否在核问题上找到让步空间 |

---

## 5. 具体修改方案

## 5.1 修改 `newsclip_agent/prompts.py`

### 5.1.1 修改 `SHORT_VIDEO_EDIT_PLAN_PROMPT`

在“要求”部分加入：

```text
拆条决策硬规则：

1. 默认优先输出 1 条完整短视频。
2. 只有满足以下条件之一，才允许 recommended_video_count > 1：
   - candidate_clips 属于不同新闻事件；
   - candidate_clips 属于同一大事件下的不同独立传播角度，且每条都能独立讲清背景、核心事实、冲突/分歧和结论；
   - 合并后会明显超过 max_allowed_seconds。
3. 如果多个片段共同构成同一新闻链条，例如：
   背景/现状 -> 官方表态 -> 核心分歧 -> 政治动机 -> 后续风险，
   必须合并为 1 条，不得拆成多个低完整度短视频。
4. 外交、战争、政策、国际冲突类新闻，默认合并为 1 条 45-60 秒完整解说。
5. 不允许为了增加视频数量，把同一新闻事件拆成多个 15-25 秒碎片。
6. 如果某条短视频预计无法写出至少 30 秒的完整解说，不要单独成片，应合并到相邻主题。
7. 如果决定拆成多条，每条必须说明 split_reason，解释为什么它可以独立成片。
8. 如果决定合并多个片段，每条 script 需要说明 merge_with_other_clips_reason。
```

把输出 JSON 中每条 script 增加两个字段：

```json
{
  "split_reason": "",
  "merge_with_other_clips_reason": ""
}
```

完整结构建议改成：

```json
{
  "recommended_video_count": 0,
  "overall_reason": "",
  "scripts": [
    {
      "short_video_id": "",
      "topic": "",
      "news_angle": "",
      "video_type": "解说型",
      "title": "",
      "cover_text": "",
      "target_duration_seconds": 30,
      "max_allowed_seconds": 35,
      "source_clip_ids": [],
      "must_keep_fact_points": [],
      "split_reason": "",
      "merge_with_other_clips_reason": "",
      "voiceover_brief": "",
      "subtitle_keywords": [],
      "editing_structure": []
    }
  ],
  "discarded_clips": [
    {"clip_id": "", "reason": ""}
  ]
}
```

---

### 5.1.2 修改 `VOICEOVER_LIGHT_PROMPT`

在要求中加入：

```text
AI 配音文案时长硬规则：

1. narration_text 必须尽量贴近 target_duration_seconds。
2. 中文新闻解说按每秒 3.0-3.6 个汉字估算。
3. 如果 target_duration_seconds >= 40，actual_char_count 不得低于 target_duration_seconds * 3.0。
4. 45 秒视频 narration_text 至少 135 个汉字。
5. 50 秒视频 narration_text 至少 150 个汉字。
6. 60 秒视频 narration_text 至少 180 个汉字。
7. 不允许 target_duration_seconds=45，但只输出 70 字左右的短文案。
8. 如果事实不足以写够目标时长，duration_fit 必须输出 too_short，并在 revise_suggestion 中明确建议合并更多 source_clip_ids 或降低 target_duration_seconds。
9. duration_fit=too_short 不能当作成功结果，后续流程会阻断。
10. target_duration_seconds >= 40 时，narrative_sections 必须包含 opening、background、core_fact、analysis_or_conflict、ending。
```

---

## 5.2 修改 `newsclip_agent/pipeline.py`

### 5.2.1 在 `step_short_video_edit_plan` 增加后处理

把当前代码：

```python
def step_short_video_edit_plan(self) -> None:
    result = self._run_text_agent("short_video_edit_plan", {
        "content_analysis": self._load_step_json("content_analysis"),
        "timeline_digest": self._load_step_json("timeline_digest"),
        "duration_strategy": self._duration_strategy_payload(),
        "run_options": self._run_options_payload(),
    })
    self._write_compat_short_video_plan_and_editing_script(result)
```

改成：

```python
def step_short_video_edit_plan(self) -> None:
    result = self._run_text_agent("short_video_edit_plan", {
        "content_analysis": self._load_step_json("content_analysis"),
        "timeline_digest": self._load_step_json("timeline_digest"),
        "duration_strategy": self._duration_strategy_payload(),
        "run_options": self._run_options_payload(),
    })

    result = self._normalize_short_video_split_decision(result)

    self._write_compat_short_video_plan_and_editing_script(result)
```

---

### 5.2.2 新增 `_normalize_short_video_split_decision`

建议放在 `_write_compat_short_video_plan_and_editing_script` 附近。

```python
def _normalize_short_video_split_decision(self, edit_plan: dict[str, Any]) -> dict[str, Any]:
    """
    防止 AI 配音剪辑模式把同一新闻链拆成多个低完整度碎片。
    默认优先合成 1 条；只有多个 script 明显独立时才保留多条。
    """
    scripts = edit_plan.get("scripts", [])
    if not isinstance(scripts, list) or len(scripts) <= 1:
        return edit_plan

    run_options = self._run_options_payload()
    audio_policy = str(run_options.get("audio_policy") or self.options.audio_policy or "")

    # 只对 AI 配音模式强约束；原声高光模式可以更自由地拆。
    if audio_policy not in {"voiceover", "ai_voiceover", "tts"}:
        return edit_plan

    if self._should_merge_short_video_scripts(scripts):
        merged_script = self._merge_short_video_scripts(scripts)
        new_plan = dict(edit_plan)
        new_plan["recommended_video_count"] = 1
        new_plan["overall_reason"] = (
            "系统后处理：检测到多个短视频属于同一新闻链条，"
            "为避免 AI 配音成片过碎，已合并为 1 条完整解说视频。"
        )
        new_plan["scripts"] = [merged_script]
        new_plan["discarded_clips"] = edit_plan.get("discarded_clips", [])
        new_plan["auto_merge_applied"] = True
        new_plan["auto_merge_reason"] = "same_news_chain_or_too_short"
        return new_plan

    return edit_plan
```

---

### 5.2.3 新增 `_should_merge_short_video_scripts`

```python
def _should_merge_short_video_scripts(self, scripts: list[dict[str, Any]]) -> bool:
    """
    判断多条规划是否应该合并。

    合并条件：
    1. 存在过短视频；
    2. 多条视频共享核心实体/关键词；
    3. 多条视频事实链互补，而不是互相独立；
    4. 总目标时长仍适合一条 AI 解说视频。
    """
    if len(scripts) <= 1:
        return False

    target_durations = [
        float(s.get("target_duration_seconds") or 0)
        for s in scripts
        if isinstance(s, dict)
    ]
    total_target = sum(target_durations)

    has_too_short_target = any(d > 0 and d < 30 for d in target_durations)

    all_text = "\n".join(
        [
            str(s.get("topic", ""))
            + "\n"
            + str(s.get("news_angle", ""))
            + "\n"
            + "\n".join(map(str, s.get("must_keep_fact_points", []) or []))
            + "\n"
            + "\n".join(map(str, s.get("source_clip_ids", []) or []))
            for s in scripts
            if isinstance(s, dict)
        ]
    )

    same_chain_keywords = [
        "谈判", "停火", "冲突", "核问题", "制裁", "白宫", "特朗普",
        "伊朗", "美国", "美方", "伊方", "外交", "协议", "分歧", "政治考量"
    ]
    keyword_hits = sum(1 for k in same_chain_keywords if k in all_text)

    shared_entity_groups = [
        ["伊朗", "美伊", "美国", "特朗普"],
        ["俄乌", "俄罗斯", "乌克兰"],
        ["中美", "中国", "美国"],
        ["巴以", "以色列", "哈马斯", "加沙"],
    ]
    has_shared_entity_group = any(
        sum(1 for k in group if k in all_text) >= 2
        for group in shared_entity_groups
    )

    chain_structure_words = ["背景", "现状", "表态", "核心", "分歧", "原因", "动机", "风险", "后续"]
    structure_hits = sum(1 for k in chain_structure_words if k in all_text)

    # AI 配音模式下，合并到 60-75 秒通常比拆碎更好。
    merge_duration_ok = total_target <= 75

    if has_too_short_target:
        return True

    if merge_duration_ok and has_shared_entity_group and keyword_hits >= 3:
        return True

    if merge_duration_ok and keyword_hits >= 4 and structure_hits >= 2:
        return True

    return False
```

注意：这版规则是轻量规则，不引入新依赖。后续如果需要更智能，可以再引入实体抽取、主题相似度或 LLM 二次裁决。

---

### 5.2.4 新增 `_merge_short_video_scripts`

```python
def _merge_short_video_scripts(self, scripts: list[dict[str, Any]]) -> dict[str, Any]:
    """
    把多条同链短视频规划合并为 1 条。
    合并时保留 source_clip_ids、must_keep_fact_points、editing_structure。
    """
    first = scripts[0]

    source_clip_ids: list[str] = []
    fact_points: list[str] = []
    editing_structure: list[dict[str, Any]] = []
    subtitle_keywords: list[Any] = []

    for script in scripts:
        for clip_id in script.get("source_clip_ids", []) or []:
            if clip_id and clip_id not in source_clip_ids:
                source_clip_ids.append(clip_id)

        for fact in script.get("must_keep_fact_points", []) or []:
            fact = str(fact).strip()
            if fact and fact not in fact_points:
                fact_points.append(fact)

        for shot in script.get("editing_structure", []) or []:
            if isinstance(shot, dict):
                editing_structure.append(dict(shot))

        for kw in script.get("subtitle_keywords", []) or []:
            if kw not in subtitle_keywords:
                subtitle_keywords.append(kw)

    normalized_structure: list[dict[str, Any]] = []
    for idx, shot in enumerate(editing_structure, start=1):
        new_shot = dict(shot)
        new_shot["order"] = idx
        new_shot["shot_id"] = f"v_001_s{idx:02d}"
        normalized_structure.append(new_shot)

    total_target = sum(float(s.get("target_duration_seconds") or 0) for s in scripts)
    target_duration = min(max(total_target, 45), 60)
    max_allowed = min(max(target_duration + 5, 50), 75)

    topics = [str(s.get("topic", "")).strip() for s in scripts if s.get("topic")]
    angles = [str(s.get("news_angle", "")).strip() for s in scripts if s.get("news_angle")]

    return {
        "short_video_id": "v_001",
        "topic": topics[0] if topics else str(first.get("topic", "")),
        "news_angle": "；".join(angles[:3]) if angles else str(first.get("news_angle", "")),
        "video_type": first.get("video_type", "解说型"),
        "title": first.get("title", ""),
        "cover_text": first.get("cover_text", ""),
        "target_duration_seconds": round(target_duration, 1),
        "max_allowed_seconds": round(max_allowed, 1),
        "source_clip_ids": source_clip_ids,
        "must_keep_fact_points": fact_points,
        "split_reason": "",
        "merge_with_other_clips_reason": "多个规划片段属于同一新闻链条，合并为一条完整 AI 解说视频。",
        "voiceover_brief": (
            "请按完整新闻链条写成一条 AI 解说：先交代背景和现状，"
            "再说明关键表态与核心分歧，最后补充政治动机或后续风险。"
        ),
        "subtitle_keywords": subtitle_keywords,
        "editing_structure": normalized_structure,
    }
```

---

## 5.3 修改配音文案校验

### 5.3.1 在 `step_voiceover_script` 增加校验调用

把当前结尾：

```python
self._normalize_voiceover_scripts(final_output)
self._enforce_long_video_confirmation_after_voiceover(final_output)
```

改成：

```python
self._normalize_voiceover_scripts(final_output)
self._validate_voiceover_script_duration_or_raise(final_output)
self._enforce_long_video_confirmation_after_voiceover(final_output)
```

---

### 5.3.2 新增 `_validate_voiceover_script_duration_or_raise`

```python
def _validate_voiceover_script_duration_or_raise(self, voiceover_output: dict[str, Any]) -> None:
    """
    阻断明显低于目标时长的 AI 配音文案。
    防止 target=45s 但 narration_text 只有几十个字的结果继续进入 TTS/剪辑。
    """
    scripts = voiceover_output.get("scripts", [])
    if not isinstance(scripts, list):
        return

    issues: list[str] = []

    for script in scripts:
        if not isinstance(script, dict):
            continue

        short_video_id = script.get("short_video_id", "")
        target = float(script.get("target_duration_seconds") or 0)
        narration_text = str(script.get("narration_text") or "")
        actual_chars = len(narration_text.replace(" ", "").replace("\n", ""))

        # 中文新闻解说粗略按 3 字/秒做最低门槛。
        min_chars = int(target * 3.0)

        if target >= 40 and actual_chars < min_chars:
            issues.append(
                f"{short_video_id} AI配音文案过短：目标 {target:.0f}s，"
                f"至少需要 {min_chars} 字，实际 {actual_chars} 字。"
            )

        duration_fit = str(script.get("duration_fit") or "")
        if duration_fit == "too_short":
            issues.append(
                f"{short_video_id} duration_fit=too_short，不能继续生成。"
            )

        sections = script.get("narrative_sections") or {}
        if target >= 40 and isinstance(sections, dict):
            required = ["opening", "background", "core_fact", "analysis_or_conflict", "ending"]
            for key in required:
                if not str(sections.get(key) or "").strip():
                    issues.append(f"{short_video_id} 缺少叙事段落：{key}")

    if issues:
        raise RuntimeError(
            "AI配音文案未达到目标时长要求，已阻断后续 TTS/剪辑流程：\n"
            + "\n".join(f"- {x}" for x in issues)
        )
```

---

## 5.4 修改 compact duration warning 为强阻断

把当前 warning 逻辑：

```python
if (
    tts_success
    and tts_item.get("timeline_mode") == "compact_segmented"
    and min_compact_duration > 0
    and video_duration < min_compact_duration
):
    repair_reasons.append(
        f"compact video duration {video_duration:.1f}s is below "
        f"{self.duration_settings.compact_min_target_ratio:.0%} of target {target_duration:.1f}s; "
        "rerun voiceover_script with richer narration or add supporting clips"
    )
    warnings.append(repair_reasons[-1])
```

改成：

```python
if (
    tts_success
    and tts_item.get("timeline_mode") == "compact_segmented"
    and min_compact_duration > 0
    and video_duration < min_compact_duration
):
    reason = (
        f"compact video duration {video_duration:.1f}s is below "
        f"{self.duration_settings.compact_min_target_ratio:.0%} of target {target_duration:.1f}s; "
        "rerun voiceover_script with richer narration or merge supporting clips"
    )
    repair_reasons.append(reason)
    warnings.append(reason)

    if self.options.audio_policy != "original":
        raise RuntimeError(
            "最终成片时长明显低于目标时长，已阻断渲染：\n"
            + reason
        )
```

说明：

- 只对非原声模式强阻断。
- 原声模式可能有自己的高光截取逻辑，不一定要按 AI 配音目标时长处理。
- AI 配音模式下，如果目标 45 秒但 compact 后只有 13 秒，必须阻断。

---

## 5.5 建议新增配置项

如果希望更灵活，可以在 `config.toml` 或 duration settings 中加：

```toml
[duration]
min_voiceover_chars_per_second = 3.0
ai_voiceover_min_video_seconds = 30
ai_voiceover_prefer_single_video = true
ai_voiceover_auto_merge_max_seconds = 75
block_short_compact_video = true
```

对应 Python 默认值：

```python
min_voiceover_chars_per_second: float = 3.0
ai_voiceover_min_video_seconds: float = 30.0
ai_voiceover_prefer_single_video: bool = True
ai_voiceover_auto_merge_max_seconds: float = 75.0
block_short_compact_video: bool = True
```

第一版也可以先不加配置，直接写死规则，等稳定后再配置化。

---

## 6. UI 优化建议

当前界面对“拆几条”没有明确控制。建议在 Web UI 增加一个选项：

```text
成片数量策略：
- 优先合成 1 条完整视频
- 自动判断
- 允许多条高光
```

默认值建议：

```text
优先合成 1 条完整视频
```

后端字段：

```json
{
  "video_count_mode": "prefer_single"
}
```

传入 prompt：

```text
用户选择 video_count_mode=prefer_single。
除非 candidate_clips 属于多个完全独立新闻事件，否则必须输出 1 条完整 AI 解说视频。
```

这可以避免用户每次都需要手动判断“为什么又拆成多个”。

---

## 7. 测试方案

建议新增测试文件：

```text
tests/test_ai_voiceover_split_policy.py
```

---

### 7.1 测试：同一新闻链自动合并

```python
import pytest


def test_same_news_chain_scripts_are_merged(pipeline):
    edit_plan = {
        "recommended_video_count": 2,
        "scripts": [
            {
                "short_video_id": "v_001",
                "topic": "美伊停火谈判核心分歧",
                "news_angle": "核问题是谈判障碍",
                "target_duration_seconds": 45,
                "max_allowed_seconds": 50,
                "source_clip_ids": ["clip_003"],
                "must_keep_fact_points": ["伊朗核问题是核心分歧"],
                "editing_structure": [],
            },
            {
                "short_video_id": "v_002",
                "topic": "美伊边谈边打",
                "news_angle": "特朗普推动停火协议",
                "target_duration_seconds": 50,
                "max_allowed_seconds": 55,
                "source_clip_ids": ["clip_001", "clip_004"],
                "must_keep_fact_points": ["美伊边谈边打", "特朗普政府存在政治考量"],
                "editing_structure": [],
            },
        ],
    }

    result = pipeline._normalize_short_video_split_decision(edit_plan)

    assert result["recommended_video_count"] == 1
    assert len(result["scripts"]) == 1
    assert set(result["scripts"][0]["source_clip_ids"]) == {"clip_001", "clip_003", "clip_004"}
    assert result["auto_merge_applied"] is True
```

---

### 7.2 测试：配音文案过短会阻断

```python
import pytest


def test_voiceover_script_too_short_is_blocked(pipeline):
    voiceover_output = {
        "scripts": [
            {
                "short_video_id": "v_001",
                "target_duration_seconds": 45,
                "duration_fit": "ok",
                "narration_text": "美伊谈判仍在继续，但核问题仍是核心分歧。",
                "narrative_sections": {
                    "opening": "美伊谈判仍在继续。",
                    "background": "",
                    "core_fact": "核问题是核心分歧。",
                    "analysis_or_conflict": "",
                    "ending": "",
                },
            }
        ]
    }

    with pytest.raises(RuntimeError):
        pipeline._validate_voiceover_script_duration_or_raise(voiceover_output)
```

---

### 7.3 测试：独立新闻不合并

```python
def test_different_news_events_are_not_merged(pipeline):
    edit_plan = {
        "recommended_video_count": 2,
        "scripts": [
            {
                "short_video_id": "v_001",
                "topic": "美伊停火谈判",
                "news_angle": "核问题是核心分歧",
                "target_duration_seconds": 45,
                "source_clip_ids": ["clip_001"],
                "must_keep_fact_points": ["美伊谈判仍在继续"],
                "editing_structure": [],
            },
            {
                "short_video_id": "v_002",
                "topic": "欧洲央行降息",
                "news_angle": "市场关注后续货币政策",
                "target_duration_seconds": 45,
                "source_clip_ids": ["clip_002"],
                "must_keep_fact_points": ["欧洲央行宣布降息"],
                "editing_structure": [],
            },
        ],
    }

    result = pipeline._normalize_short_video_split_decision(edit_plan)

    assert result["recommended_video_count"] == 2
    assert len(result["scripts"]) == 2
```

---

## 8. Codex 可执行任务单

可以直接把下面这段发给 Codex：

```text
请修复 AI 配音剪辑模式下短视频拆分过碎的问题。

背景案例：
同一条美伊新闻链被拆成 2 条视频，目标分别是 45 秒和 50 秒，但最终只生成 13 秒和 21 秒。原因是 short_video_edit_plan 过度拆条，voiceover_script 文案过短，compact_segmented 又按 TTS 实际时长压缩，最终 warning 没阻断。

目标：
1. 默认优先生成 1 条完整 AI 解说视频。
2. 同一新闻链条不得拆成多个 15-25 秒碎片。
3. 如果模型规划出多条，但它们共享核心事件、实体和事实链，后处理自动合并为 1 条。
4. 如果目标 45-60 秒，但 voiceover_script 实际文案明显过短，必须阻断后续 TTS/剪辑。
5. 如果 compact_segmented 最终成片时长低于目标时长的 compact_min_target_ratio，不能只 warning，必须 raise RuntimeError 阻断渲染。

请重点修改：
- newsclip_agent/prompts.py
- newsclip_agent/pipeline.py
- tests/test_ai_voiceover_split_policy.py 或新增同类测试

具体实现：
1. 在 SHORT_VIDEO_EDIT_PLAN_PROMPT 中加入“默认合并、同链不拆、拆条必须有 split_reason”的规则。
2. 在 SHORT_VIDEO_EDIT_PLAN_PROMPT 的 script JSON 中新增 split_reason 和 merge_with_other_clips_reason。
3. 在 VOICEOVER_LIGHT_PROMPT 中加入中文配音字数下限规则：target>=40 时，narration_text 至少 target_duration_seconds * 3.0 个汉字。
4. 在 step_short_video_edit_plan 中，对 _run_text_agent 返回结果调用 _normalize_short_video_split_decision。
5. 新增 _normalize_short_video_split_decision、_should_merge_short_video_scripts、_merge_short_video_scripts。
6. 在 step_voiceover_script 中增加 _validate_voiceover_script_duration_or_raise。
7. 将 compact video duration below target 的 warning 改为 RuntimeError；至少在 AI 配音模式下必须阻断。
8. 补充单元测试，确保同一美伊新闻链会合并、短配音稿会阻断、不同新闻事件不会合并。

验收标准：
1. 本次美伊案例应输出 1 条完整视频规划，而不是 2 条碎片视频。
2. 如果 voiceover_script 仍输出 70 字支撑 45 秒，流程必须报错阻断，不允许继续生成视频。
3. 如果最终 compact 成片低于目标时长比例，流程必须报错阻断。
4. pytest 相关测试通过。
```

---

## 9. 最终建议

这次问题的本质不是“能不能拆多个”，而是缺少明确拆分标准和代码兜底。

建议最终落地三条硬规则：

```text
1. AI 配音模式默认一条完整视频。
2. 同一新闻链不拆。
3. 单条最终成片低于 25-30 秒，禁止输出，必须合并或重跑。
```

这样可以显著减少 13 秒、21 秒这种碎片化成片，也更符合新闻解说类视频的实际传播逻辑。
