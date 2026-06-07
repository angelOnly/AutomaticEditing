# AI 配音文案超字数自动修复优化方案（完善版）

## 1. 问题背景

当前任务在 `voiceover_script` 阶段失败，错误信息类似：

```text
AI voiceover script does not match editing_structure; blocked before TTS:
- v_001: shot/voiceover mismatch (shots=4, segments=4, missing=[], extra=[], empty=[], char_issues=1)
```

这类错误容易误解成 `editing_structure` 和 `narration_segments` 数量不一致，但从错误内容看：

```text
shots=4
segments=4
missing=[]
extra=[]
empty=[]
char_issues=1
```

真正失败原因不是 shot 数量不一致，也不是 shot_id 缺失，而是某一个 `narration_segment.text` 超过了对应 shot 的字数上限，并且超过比例达到 hard 级别。

本次具体表现是：

```text
v_001_s04 要求最多 28 字
模型生成了 43 字
超出 15 字
超出比例 15 / 28 = 0.536
超过 hard 阈值 0.25
因此被判定为 hard char_budget_issue
```

当前系统因此直接阻断，不进入 TTS。

本次优化重点不是放宽所有限制，而是让系统在 TTS 之前先尝试自动修复明显可修复的 AI 配音文案问题。也就是说：

```text
TTS 前校验仍然保留；
但 hard char issue 不应该第一时间直接终止任务；
应先自动压缩或补写，再重新校验。
```

## 2. 当前代码中的实际处理链路

### 2.1 Web 启动链路

入口是：

```text
python run_web.py
```

`run_web.py` 只负责加载配置、执行启动前 ASR preflight，然后通过 uvicorn 启动：

```python
uvicorn.run("web_app:app", host=host, port=port, reload=False)
```

`web_app.py` 中加载 `config.toml`，并将 Web 请求参数传入后端任务。`RunRequest` 中已经包含 AI 配音相关运行参数，例如：

```python
target_duration_seconds
max_output_video_seconds
allow_long_video
require_tts
voice_id
audio_policy
production_mode
```

`run_pipeline.py` 只是调用 `newsclip_agent.pipeline.main`。

因此本次优化应优先在 `pipeline.py` 内部处理，不要求 Web 页面新增必填参数。只要配置项有默认值，就可以兼容现有 `python run_web.py` 启动方式。

### 2.2 short_video_edit_plan 阶段如何生成 shot 字数预算

`step_short_video_edit_plan()` 会先生成短视频选片规划，然后调用：

```python
_materialize_short_video_edit_plan(raw_result)
```

在物化过程中，每个被选中的 clip 会转成 `editing_structure` 中的一个 shot。每个 shot 包含：

```text
shot_id
source_clip_id
source_id
source_start/source_end
source_start_seconds/source_end_seconds
duration_seconds
section
visual / visual_summary
fact / news_fact_to_explain
narration_intent
```

随后代码会做两件关键事情：

```python
editing_structure = self._assign_shot_duration_budget(editing_structure, target_duration)
editing_structure = self._attach_voiceover_char_budget(editing_structure)
```

其中：

```python
_assign_shot_duration_budget()
```

负责给每个 shot 分配：

```text
target_duration_seconds
min_duration_seconds
max_duration_seconds
```

然后：

```python
_attach_voiceover_char_budget()
```

根据每个 shot 的目标配音时长，计算：

```text
narration_min_chars
narration_target_chars
narration_max_chars
```

当前字数预算计算逻辑是：

```python
min_chars = max(global_min, int(target * min_cps))
target_chars = max(global_min, int(target * cps))
max_chars = min(global_max, max(min_chars + 4, int(target * max_cps)))
```

其中配置来自 `config.toml` 的 `[voiceover]`：

```toml
voiceover_chars_per_second = 4.8
voiceover_min_chars_per_second = 3.8
voiceover_max_chars_per_second = 5.8
shot_min_text_chars = 10
shot_max_text_chars = 90
```

所以 `v_001_s04 max_chars=28` 的根本原因是：这个 shot 的目标配音时长较短，按当前 `voiceover_max_chars_per_second` 换算后最多只能容纳 28 个字。

### 2.3 voiceover_script 阶段如何让模型生成文案

`step_voiceover_script()` 会读取：

```python
edit_plan = self._load_step_json("short_video_edit_plan")
```

然后对每个 script 并发调用大模型：

```python
input_text = self._build_voiceover_script_text_input(script)
result = self.llm_text.call_json(
    model=model,
    fallback_models=fallback,
    prompt=prompts.VOICEOVER_SCRIPT_TEXT_PROMPT,
    input_data=input_text,
    temperature=float(cfg.get("temperature", 0.2) or 0.2),
    max_tokens=int(cfg.get("max_tokens", 1200) or 1200),
    debug_dir=vdir / "_llm_debug" / short_video_id,
)
```

`_build_voiceover_script_text_input()` 会把每个 shot 的字数预算传给模型：

```text
shot_id=xxx
target=xxx s
chars=min/target/max
section=xxx
visual=xxx
fact=xxx
narration_intent=xxx
```

也就是说，模型在生成文案时已经知道每个 shot 的字数范围。问题不是没有传预算，而是模型没有严格遵守预算；而当前代码没有自动修复，只是直接失败。

### 2.4 当前双向对齐逻辑

生成 `voiceover_script.json` 后，代码会执行：

```python
self._normalize_voiceover_scripts(final_output)
self._attach_voiceover_script_timings(final_output, edit_plan)
self._validate_voiceover_script_against_editing_structure(final_output, edit_plan)
self._validate_voiceover_script_duration_or_raise(final_output)
self._enforce_long_video_confirmation_after_voiceover(final_output)
```

其中：

```python
_attach_voiceover_script_timings()
```

负责把 `editing_structure` 的 shot 时长补到 `narration_segments` 中。它会从 0 秒开始累计每个 shot 的目标时长，给对应 segment 写入：

```text
target_start
target_end
target_start_seconds
target_end_seconds
target_duration_seconds
```

这就是当前的时间对齐。

```python
_validate_voiceover_script_against_editing_structure()
```

负责结构和字数对齐，检查内容包括：

```text
1. editing_structure 中的 shot_id 是否都在 narration_segments 中出现
2. narration_segments 中是否有 editing_structure 不存在的 shot_id
3. narration_segments 是否有空文本
4. narration_segments 数量是否等于 shots 数量
5. 每个 segment.text 是否低于 narration_min_chars
6. 每个 segment.text 是否高于 narration_max_chars
```

当前判断结果分两类：

```text
char_budget_warnings：轻微超字数或少字数，只打印 warning
char_budget_issues：严重超字数或少字数，直接 raise RuntimeError
```

因此现在的策略是：

```text
轻微不合格：放行
严重不合格：直接失败
```

缺少的能力是：

```text
严重不合格：先让大模型自动修复，再重新校验
```

### 2.5 现有 voiceover repair prompt 的状态

仓库里已经有：

```python
prompts.VOICEOVER_REPAIR_TEXT_PROMPT
```

并且对应文件是：

```text
newsclip_agent/prompt_texts/voiceover_repair_text.txt
```

它当前的定位是局部文案修复，输出格式是 patch：

```json
{
  "repaired_segments": [
    {
      "shot_id": "v_001_s02",
      "new_text": "修复后的中文配音文案"
    }
  ]
}
```

这说明项目里已经有“局部修复”的提示词基础。后续不建议再新设计一套完全不同的输出格式，否则会出现 `text` / `new_text`、完整脚本 / patch 输出混用的问题。

## 3. 当前方案的问题

当前硬阻断有几个问题。

### 3.1 用户体验不好

像 `43 字压到 28 字` 这种问题，大模型完全可以重写成更短表达。直接失败会导致整个任务中断，用户只能手动重新跑，不符合自动剪辑系统的预期。

### 3.2 阻断发生得太早

当前失败发生在 TTS 之前，这是正确的，因为可以避免 TTS 生成后才发现塞不进画面。

但问题是：

```text
TTS 前校验是对的；
校验失败后直接退出是不合理的。
```

更合理的处理是：

```text
TTS 前校验失败 → 自动修复 voiceover_script → 再校验 → 仍失败才退出
```

### 3.3 配置项 ai_voiceover_mismatch_policy 没有真正覆盖这个问题

`config.toml` 中已有：

```toml
ai_voiceover_mismatch_policy = "repair_then_warn"
```

但当前 `_validate_voiceover_script_against_editing_structure()` 对 hard char issue 仍然直接 raise，没有看到有效 repair 流程。因此配置语义和实际行为不一致。

### 3.4 模型输出结构仍可进一步收敛

修复阶段不应该让模型重新输出完整复杂结构，也不应该输出下游不用的字段。

对于本次 `char_issues=1` 这种问题，下游真正需要修复的只有：

```text
指定 shot_id 对应的 segment.text
```

因此优先采用 patch 输出，而不是让模型重新输出整条脚本。

推荐局部修复输出：

```json
{
  "short_video_id": "v_001",
  "repaired_segments": [
    {
      "shot_id": "v_001_s04",
      "new_text": "压缩后的文案"
    }
  ]
}
```

不推荐在局部修复阶段让模型输出：

```text
target_start
target_end
target_duration_seconds
estimated_duration_seconds
duration_fit
text_policy_check
visual
fact
reason
score
rewrite_notes
```

这些字段可以由代码补充或根本不用。

### 3.5 局部 patch 修复和完整结构重建不能混在一起

本次报错是：

```text
missing=[]
extra=[]
empty=[]
char_issues=1
```

这种场景最适合局部 patch 修复。

但如果未来出现：

```text
missing_shot_ids 不为空
extra_shot_ids 不为空
segment_count != shot_count
```

只输出 patch 就不够了，因为这时不只是改文字，而是要补 segment、删除多余 segment，或者按 `editing_structure` 的 shot 顺序重建 `narration_segments`。

因此建议把 repair 分成两类：

```text
A. 局部文本修复：char_budget_issues / empty_text / 可选 warnings
B. 完整结构重建：missing / extra / segment_count mismatch
```

第一版可以优先实现 A，先解决这次真实失败。B 作为后续增强，避免第一版过度复杂。

## 4. 优化目标

本次优化目标：

```text
当 voiceover_script 与 editing_structure 不匹配时，不直接失败；
先自动调用大模型修复可修复的 narration_segments；
修复成功后继续 TTS；
多轮修复仍失败时，才明确失败。
```

具体目标：

1. 支持超字数自动压缩。
2. 支持少字数自动补足。
3. 支持空文本自动补写。
4. 优先使用局部 patch 修复，不重写整条脚本。
5. 修复后必须重新同步 `narration_text`，保证它和 `narration_segments` 一致。
6. 修复后仍走现有 normalize、timing attach、validate、duration check。
7. 不破坏 Web 启动方式 `python run_web.py`。
8. 不影响原声高光重组流程。
9. 对 missing / extra / segment 数量不一致问题，先保留完整结构重建扩展点，不建议第一版和局部修复混在一起。

## 5. 推荐优化方向

推荐采用：

```text
validate → repair → validate → retry repair → validate → final fail
```

即：

```text
初次生成 voiceover_script
  ↓
归一化
  ↓
同步 narration_text
  ↓
补 target timing
  ↓
校验 editing_structure 对齐
  ↓
如果通过：继续 duration check / TTS
  ↓
如果失败：根据失败类型选择 repair 策略
  ↓
局部文本问题：调用 patch repair，只修复 problem segments
结构问题：后续可走完整结构重建 repair
  ↓
合并修复结果
  ↓
重新同步 narration_text、补 timing、校验
  ↓
最多修复 N 轮
  ↓
仍失败才 raise
```

推荐默认：

```toml
[voiceover_script]
repair_enabled = true
max_repair_rounds = 2
repair_temperature = 0.1
repair_max_tokens = 1000
repair_on_warnings = false
repair_structural_mismatch = false
```

说明：

```text
repair_enabled：是否启用自动修复。
max_repair_rounds：最多修复几轮，建议 2。
repair_temperature：修复任务以严格遵守格式为主，温度应低。
repair_max_tokens：修复输出结构很小，不需要太大。
repair_on_warnings：默认不修复 warning，只修复 hard issues，避免过度重写。
repair_structural_mismatch：默认 false，第一版先不自动重建结构，避免误删或错排 segment。
```

这里建议第一版把目标收敛为：

```text
优先解决本次 char_issues=1 的 hard 超字数问题。
```

## 6. 需要修改的文件

主要修改：

```text
newsclip_agent/pipeline.py
config.toml
```

可选修改：

```text
newsclip_agent/prompt_texts/voiceover_repair_text.txt
newsclip_agent/prompts.py
```

说明：

```text
仓库已有 VOICEOVER_REPAIR_TEXT_PROMPT 和 voiceover_repair_text.txt。
如果沿用现有 prompt，只需要微调 prompt 内容和调用方式。
如果新增专门用于 TTS 前字数校验的 prompt，可以在 prompts.py 中新增常量。
```

不建议本次修改：

```text
web_app.py
run_web.py
run_pipeline.py
```

原因：

```text
run_web.py 只是启动 Web。
web_app.py 已经从 config.toml 读取默认配置，并把请求参数传入 pipeline。
run_pipeline.py 只是调用 newsclip_agent.pipeline.main。
本次修复属于 pipeline 内部质量控制，不需要 Web 增加参数。
```

## 7. 代码修改思路

### 7.1 将校验函数改成“可返回结果、可选择不抛错”

当前函数：

```python
def _validate_voiceover_script_against_editing_structure(...):
    ...
    if hard_issues:
        raise RuntimeError(...)
    return result
```

建议改成支持参数：

```python
def _validate_voiceover_script_against_editing_structure(
    self,
    voiceover_output: dict[str, Any],
    edit_plan: dict[str, Any],
    *,
    raise_on_error: bool = True,
) -> dict[str, Any]:
```

行为：

```text
raise_on_error=True：保持现有行为，失败直接抛错。
raise_on_error=False：只返回 alignment_result，不抛错。
```

这样 repair 流程可以先拿到完整错误报告，再决定是否修复。

建议返回结构继续保持当前语义，并补充 `hard_issues`：

```json
{
  "ok": false,
  "checks": [
    {
      "short_video_id": "v_001",
      "ok": false,
      "shot_count": 4,
      "segment_count": 4,
      "missing_shot_ids": [],
      "extra_shot_ids": [],
      "empty_text_shot_ids": [],
      "char_budget_issues": [],
      "char_budget_warnings": []
    }
  ],
  "hard_issues": ["..."]
}
```

### 7.2 新增统一的 voiceover 后处理函数

当前 `step_voiceover_script()` 中新生成和复用缓存各写了一遍：

```python
self._normalize_voiceover_scripts(final_output)
self._attach_voiceover_script_timings(final_output, edit_plan)
self._validate_voiceover_script_against_editing_structure(final_output, edit_plan)
self._validate_voiceover_script_duration_or_raise(final_output)
self._enforce_long_video_confirmation_after_voiceover(final_output)
```

建议抽成：

```python
def _finalize_voiceover_script_with_repair(
    self,
    final_output: dict[str, Any],
    edit_plan: dict[str, Any],
    *,
    vdir: Path | None = None,
    input_hash: str | None = None,
) -> dict[str, Any]:
```

职责：

```text
1. normalize voiceover 文本
2. 同步 narration_text 与 narration_segments
3. attach timings
4. validate，不立即 raise
5. 如果 ok，继续后续 duration check
6. 如果 not ok，根据配置和失败类型决定是否 repair
7. repair 后再次 normalize / sync narration_text / attach timings / validate
8. 成功则覆盖 voiceover_script.json
9. 多轮失败才 raise
```

这样缓存复用和新生成都能走同一套逻辑。

### 7.3 新增 narration_text 同步函数

当前 `_normalize_voiceover_scripts()` 会清理 `narration_text`，但不保证它和 `narration_segments[].text` 一致。

建议新增一个小函数，专门在 repair 后和最终保存前调用：

```python
def _sync_voiceover_narration_text_from_segments(self, voiceover_output: dict[str, Any]) -> None:
    ...
```

规则：

```text
如果 narration_segments 存在，则用 segments 按顺序重新拼接 narration_text。
每个 segment.text 先做 clean_voiceover_text。
拼接时可以用空格或中文标点做轻量连接，但不要让模型再次生成 narration_text。
```

原因：

```text
TTS、字幕、调试文件、duration check 都可能读取 narration_text。
如果只改 segments，不同步 narration_text，会产生“分段文案已修复，但完整文案仍是旧超长文本”的隐患。
```

### 7.4 新增 repair 判断函数

新增：

```python
def _voiceover_alignment_needs_repair(
    self,
    alignment: dict[str, Any],
    *,
    repair_on_warnings: bool = False,
) -> bool:
```

判断逻辑：

```text
如果 alignment.ok == True：不修复。
如果存在 empty_text_shot_ids / char_budget_issues：修复。
如果 repair_on_warnings=True 且存在 char_budget_warnings：修复。
如果存在 missing / extra / segment_count mismatch：默认不走局部 patch，除非 repair_structural_mismatch=true。
```

建议再拆一个失败分类函数：

```text
text_patch_repairable：char_budget_issues、empty_text_shot_ids、可选 char_budget_warnings
structural_repairable：missing_shot_ids、extra_shot_ids、segment_count mismatch
```

这样可以避免局部修复函数误处理结构问题。

### 7.5 新增局部 repair 输入构造函数

新增：

```python
def _build_voiceover_repair_text_input(
    self,
    *,
    edit_script: dict[str, Any],
    voice_script: dict[str, Any],
    alignment_check: dict[str, Any],
) -> str:
```

输入内容要克制，只给修复所需信息。

建议包含：

```text
任务说明
short_video_id
主题 topic
讲述重点 news_angle
必须保留事实 must_keep_fact_points
错误报告 alignment_check 中该 short_video_id 的 check
problem_segments 列表
当前 narration_segments 的前后文
输出 JSON 格式
```

重点：不要把完整 candidate_clips、完整 timeline_digest、无关字段再塞给模型。

每个 problem segment 建议给：

```text
shot_id
issue_type: too_long / too_short / empty
current_chars
min_chars
target_chars
max_chars
current_text
previous_text
next_text
section
target_duration_seconds
visual_summary
fact
narration_intent
```

不要把所有无关 shot 都塞给模型。最多给当前问题片段和相邻片段，用于语气衔接。

### 7.6 修复提示词设计

优先沿用并微调现有：

```text
newsclip_agent/prompt_texts/voiceover_repair_text.txt
```

不要再新建一套互相冲突的输出格式。

修复提示词必须非常明确，核心是“只改文本，不改结构”。

建议修复 prompt 的输出格式统一为：

```json
{
  "short_video_id": "v_001",
  "repaired_segments": [
    {
      "shot_id": "v_001_s04",
      "new_text": "修复后的文案"
    }
  ]
}
```

字段约束：

```text
1. 只输出合法 JSON。
2. 不要输出 markdown。
3. 不要输出代码块。
4. 不要输出 reason、score、rewrite_notes。
5. 不要输出完整 narration_text。
6. 不要输出完整 narration_segments。
7. repaired_segments 里只允许 shot_id 和 new_text。
8. 不要新增、删除或改写 shot_id。
9. new_text 不能为空。
10. new_text 字数必须落在该 shot 的 min_chars 和 max_chars 之间，优先接近 target_chars。
```

说明：

```text
当前仓库已有 voiceover_repair_text.txt 使用 new_text 字段。
因此文档和实现都应统一用 new_text，不要混用 text。
```

### 7.7 新增 repair 调用函数

新增：

```python
def _repair_one_voiceover_script_patch(
    self,
    *,
    edit_script: dict[str, Any],
    voice_script: dict[str, Any],
    alignment_check: dict[str, Any],
    model: str,
    fallback: list[str],
    vdir: Path,
    round_index: int,
) -> dict[str, Any]:
```

职责：

```text
1. 构造局部 repair input
2. 写入 repair_round_x/input.txt
3. 调用 llm_text.call_json
4. 写入 raw_response.json / model_output.json
5. 归一化输出，只接受 repaired_segments[].shot_id 和 repaired_segments[].new_text
6. 返回 patch，不直接替换整条 voice_script
```

输出规范化建议：

```python
patch = {
    "short_video_id": sid,
    "repaired_segments": [
        {
            "shot_id": str(seg.get("shot_id") or ""),
            "new_text": clean_voiceover_text(seg.get("new_text") or seg.get("text") or ""),
        }
        for seg in parsed.get("repaired_segments") or []
        if isinstance(seg, dict)
    ],
}
```

兼容说明：

```text
为了兼容模型偶尔输出 text，可以代码层兜底读取 seg.get("new_text") or seg.get("text")。
但 prompt 和文档统一要求 new_text。
```

### 7.8 新增 patch 合并函数

新增：

```python
def _apply_voiceover_repair_patch(
    self,
    voice_script: dict[str, Any],
    patch: dict[str, Any],
) -> bool:
```

逻辑：

```text
1. 根据 short_video_id 校验 patch 是否对应当前脚本。
2. 建立 narration_segments 的 shot_id 索引。
3. 只更新 patch 中存在且原脚本也存在的 shot_id。
4. 只改 segment.text，不改 timing 字段。
5. 不接受空 new_text。
6. 合并后重新用 segments 拼接 narration_text。
```

这样可以避免模型误删、误改、重排整个脚本。

### 7.9 完整结构重建作为后续扩展

对于以下问题：

```text
missing_shot_ids 不为空
extra_shot_ids 不为空
segment_count != shot_count
```

局部 patch 不一定够用。

建议第一版策略：

```text
如果只有 char_budget_issues / empty_text：自动 patch repair。
如果存在 missing / extra / segment_count mismatch：默认仍然 fail，并在错误里提示这是结构类 mismatch。
```

后续如果要支持完整结构重建，再新增单独函数：

```python
def _repair_one_voiceover_script_structure(...):
    ...
```

完整结构重建输出才使用：

```json
{
  "short_video_id": "v_001",
  "narration_segments": [
    {"shot_id": "v_001_s01", "text": "..."},
    {"shot_id": "v_001_s02", "text": "..."}
  ]
}
```

但不建议第一版和局部 patch 混在一个 prompt 里。

### 7.10 repair 主循环

在 `_finalize_voiceover_script_with_repair()` 中实现：

```python
self._normalize_voiceover_scripts(final_output)
self._sync_voiceover_narration_text_from_segments(final_output)
self._attach_voiceover_script_timings(final_output, edit_plan)
alignment = self._validate_voiceover_script_against_editing_structure(
    final_output,
    edit_plan,
    raise_on_error=False,
)

if not alignment.get("ok") and repair_enabled:
    for round_index in range(1, max_repair_rounds + 1):
        for check in alignment.get("checks", []):
            if check.get("ok"):
                continue
            if not self._check_is_text_patch_repairable(check, repair_on_warnings=repair_on_warnings):
                continue

            sid = check.get("short_video_id")
            edit_script = edit_by_id[sid]
            voice_script = voice_by_id[sid]
            patch = self._repair_one_voiceover_script_patch(...)
            self._apply_voiceover_repair_patch(voice_script, patch)

        self._normalize_voiceover_scripts(final_output)
        self._sync_voiceover_narration_text_from_segments(final_output)
        self._attach_voiceover_script_timings(final_output, edit_plan)
        alignment = self._validate_voiceover_script_against_editing_structure(
            final_output,
            edit_plan,
            raise_on_error=False,
        )
        write_json(vdir / f"repair_round_{round_index}" / "alignment_after_repair.json", alignment)
        if alignment.get("ok"):
            break

if not alignment.get("ok"):
    self._validate_voiceover_script_against_editing_structure(
        final_output,
        edit_plan,
        raise_on_error=True,
    )

self._validate_voiceover_script_duration_or_raise(final_output)
self._enforce_long_video_confirmation_after_voiceover(final_output)
```

修复成功后，要覆盖最终输出：

```python
out = self.task_dir / self._step_output("voiceover_script")
write_json(out, final_output)
```

并建议额外写：

```text
voiceover_repair_summary.json
```

内容包括：

```json
{
  "enabled": true,
  "rounds_used": 1,
  "status": "repaired",
  "repair_type": "text_patch",
  "before": {},
  "after": {}
}
```

这样 Web 运行日志和排查文件都能看清楚是自动修复过的。

## 8. config.toml 修改建议

在 `[voiceover_script]` 下新增：

```toml
[voiceover_script]
max_workers = 2
model_name = "ep-20260604155430-pt5bq"
fallback_models = []
temperature = 0.2
max_tokens = 1200

# 自动修复 voiceover_script 与 editing_structure 的局部文案问题
repair_enabled = true
max_repair_rounds = 2
repair_temperature = 0.1
repair_max_tokens = 1000
repair_on_warnings = false
repair_structural_mismatch = false
```

说明：

```text
repair_enabled=true：默认启用，不再因为 hard 超字数直接失败。
max_repair_rounds=2：第一轮通常足够，第二轮兜底。
repair_temperature=0.1：降低随机性，让模型严格按预算改。
repair_max_tokens=1000：输出 patch 结构很小，够用。
repair_on_warnings=false：轻微超字数不修复，避免过度重写。
repair_structural_mismatch=false：第一版不自动重建结构，避免误删或错排 segment。
```

## 9. prompt 修改建议

优先修改现有：

```text
newsclip_agent/prompt_texts/voiceover_repair_text.txt
```

建议保留它当前的局部修复定位，并补充以下约束：

```text
1. 本 prompt 同时用于 TTS 前字数预算修复和后续可能的 TTS 时长修复。
2. 输入中会明确提供 issue_type、min_chars、target_chars、max_chars、current_chars。
3. 当 issue_type=too_long 时，new_text 必须小于等于 max_chars。
4. 当 issue_type=too_short 时，new_text 必须大于等于 min_chars。
5. 当 issue_type=empty 时，new_text 必须非空，并在 min_chars/max_chars 范围内。
6. 输出 repaired_segments[].new_text，不输出 repaired_segments[].text。
7. 不输出完整 narration_text，不输出完整 narration_segments。
```

不建议新增一个输出格式不同的 `VOICEOVER_SCRIPT_REPAIR_PROMPT`，除非明确把它命名为结构重建专用 prompt。

## 10. pipeline.py 具体修改点

### 10.1 修改 `_validate_voiceover_script_against_editing_structure()`

位置：

```text
newsclip_agent/pipeline.py
_validate_voiceover_script_against_editing_structure
```

改动：

```text
增加 raise_on_error 参数。
把 hard_issues 写入 result。
当 raise_on_error=False 时，不抛异常。
```

建议返回结构：

```json
{
  "ok": false,
  "checks": [
    {
      "short_video_id": "v_001",
      "ok": false,
      "shot_count": 4,
      "segment_count": 4,
      "missing_shot_ids": [],
      "extra_shot_ids": [],
      "empty_text_shot_ids": [],
      "char_budget_issues": [],
      "char_budget_warnings": []
    }
  ],
  "hard_issues": ["..."]
}
```

### 10.2 新增 `_finalize_voiceover_script_with_repair()`

位置建议：放在 `step_voiceover_script()` 附近，和 voiceover 相关函数放一起。

职责：统一处理：

```text
normalize
sync narration_text
timing attach
alignment check
repair loop
duration check
long video confirmation
最终写回 voiceover_script.json
```

### 10.3 修改 `step_voiceover_script()`

当前新生成分支最后是：

```python
self._normalize_voiceover_scripts(final_output)
self._attach_voiceover_script_timings(final_output, edit_plan)
self._validate_voiceover_script_against_editing_structure(final_output, edit_plan)
self._validate_voiceover_script_duration_or_raise(final_output)
self._enforce_long_video_confirmation_after_voiceover(final_output)
```

改成统一调用：

```python
self._finalize_voiceover_script_with_repair(
    final_output,
    edit_plan,
    vdir=vdir,
    input_hash=input_hash,
)
```

缓存复用分支也要改。当前缓存复用分支也是直接 validate，建议同样改成统一 finalize：

```python
self._finalize_voiceover_script_with_repair(
    final_output,
    edit_plan,
    vdir=Path(self.task_dir / self._step_output("voiceover_script")).parent,
    input_hash=input_hash,
)
return
```

这样旧缓存中如果存在超字数文案，也可以自动修复，而不是继续失败。

### 10.4 新增 `_repair_one_voiceover_script_patch()`

位置建议：放在 `_build_voiceover_script_text_input()` 后面。

职责：

```text
输入 edit_script + 当前 voice_script + alignment check
输出 repaired_segments patch
```

重点约束：

```text
只接受 short_video_id / repaired_segments。
repaired_segments 只接受 shot_id / new_text。
不接受模型输出的 timing 字段。
不接受模型输出的 reason / score / note。
```

### 10.5 新增 `_normalize_voiceover_repair_patch()`

建议单独做一个小函数：

```python
def _normalize_voiceover_repair_patch(self, parsed: Any, *, expected_sid: str) -> dict[str, Any]:
```

兼容模型可能输出：

```json
{"repaired_segments": [{"shot_id": "v_001_s04", "new_text": "..."}]}
```

也可能多输出：

```json
{"short_video_id": "v_001", "repaired_segments": [...]}
```

归一化后只返回：

```json
{
  "short_video_id": "v_001",
  "repaired_segments": [
    {"shot_id": "v_001_s04", "new_text": "..."}
  ]
}
```

如果模型输出的是 `text`，代码可以兼容读入，但最终统一成 `new_text`。

### 10.6 新增 `_apply_voiceover_repair_patch()`

职责：

```text
把 patch 合并回原 voice_script。
只改已有 shot_id 的 text。
不改变 narration_segments 顺序。
不保留模型额外字段。
合并后重新同步 narration_text。
```

### 10.7 新增 `_sync_voiceover_narration_text_from_segments()`

职责：

```text
根据 narration_segments[].text 重建 narration_text。
保证完整文案和分段文案一致。
```

这是本次完善后必须保留的点。

### 10.8 新增 `_build_voiceover_repair_text_input()`

推荐输入格式：

```text
任务：修复 AI 配音文案，使其严格适配每个 shot 的字数预算。

short_video_id: v_001
主题: ...
讲述重点: ...

必须保留事实：
- ...

校验失败原因：
{
  "missing_shot_ids": [],
  "extra_shot_ids": [],
  "empty_text_shot_ids": [],
  "char_budget_issues": [
    {"shot_id":"v_001_s04","chars":43,"max_chars":28,"type":"too_long"}
  ]
}

problem_segments：
[
  {
    "shot_id": "v_001_s04",
    "issue_type": "too_long",
    "current_chars": 43,
    "min_chars": 18,
    "target_chars": 23,
    "max_chars": 28,
    "current_text": "当前超字数文本...",
    "previous_text": "上一段文案...",
    "next_text": "下一段文案...",
    "section": "ending",
    "target_duration_seconds": 4.9,
    "visual_summary": "...",
    "fact": "...",
    "narration_intent": "..."
  }
]

只输出 JSON：
{
  "short_video_id": "v_001",
  "repaired_segments": [
    {"shot_id": "v_001_s04", "new_text": "修复后的文案"}
  ]
}
```

### 10.9 不建议修改 TTS 阶段

不要把这个问题推到 TTS 阶段解决。

原因：

```text
TTS 只负责把文本转音频。
如果文本已经远超 shot 时长，TTS 再做压缩会导致语速异常、停顿不自然、字幕不同步。
```

正确位置就是 `voiceover_script` 后、TTS 前。

## 11. 下游兼容性说明

### 11.1 TTS 兼容

TTS 当前读取：

```python
text = item.get("narration_text") or ...
timeline_segments = self._voiceover_segments_for_tts(item)
```

只要修复后的结构仍然包含：

```text
short_video_id
narration_text
narration_segments[].shot_id
narration_segments[].text
```

并在 finalize 阶段重新 attach timing，TTS 不需要改。

注意：修复后必须同步 `narration_text`，否则 TTS 调试文件、时长估算和分段文本会不一致。

### 11.2 subtitles 兼容

字幕阶段优先使用 TTS segments；否则使用 `narration_segments`。修复后仍保留 `narration_segments`，因此兼容。

### 11.3 cut_plan/render 兼容

cut_plan 依赖 `editing_script`、`voiceover_script`、`tts`。修复只改变 voiceover 文本，不改变 `editing_structure` 和源视频时间，因此不会影响 cut_plan 的源片段选择。

### 11.4 Web 兼容

Web 不需要新增参数。`config.toml` 增加默认配置后，现有 `python run_web.py` 自动生效。

## 12. 运行与重跑建议

这个问题发生在 `voiceover_script` 阶段，并且 TTS 没开始。

修复代码后，建议从：

```text
voiceover_script
```

重新跑。

如果命令行支持：

```bash
python run_pipeline.py --task-id <任务ID> --rerun-from voiceover_script
```

如果从 Web 操作，则选择从失败步骤或 `voiceover_script` 之后重新运行。

不建议从头跑全部步骤，因为前面的：

```text
source_analysis
source_aggregate
asr_micro_segment
content_analysis
ai_voiceover_candidate_materialize
short_video_edit_plan
```

已经成功，可以复用。

## 13. 测试步骤

### 13.1 单元级测试

构造一个最小 edit_plan：

```json
{
  "scripts": [
    {
      "short_video_id": "v_001",
      "editing_structure": [
        {
          "shot_id": "v_001_s01",
          "target_duration_seconds": 5,
          "narration_min_chars": 18,
          "narration_target_chars": 24,
          "narration_max_chars": 28
        }
      ]
    }
  ]
}
```

构造一个超字数 voiceover：

```json
{
  "scripts": [
    {
      "short_video_id": "v_001",
      "narration_text": "这是一个明显超过二十八个字的测试文案，用来触发自动修复流程。",
      "narration_segments": [
        {
          "shot_id": "v_001_s01",
          "text": "这是一个明显超过二十八个字的测试文案，用来触发自动修复流程。"
        }
      ]
    }
  ]
}
```

预期：

```text
第一次 validate 不通过。
repair 自动调用。
模型输出 repaired_segments patch。
patch 合并回原 voice_script。
修复后 segment.text 字数 <= 28。
narration_text 被重新拼接。
最终 validate 通过。
```

### 13.2 真实任务回归测试

使用本次失败任务，重新跑：

```text
voiceover_script
```

检查：

```text
voiceover_alignment_check.json 最终 ok=true
voiceover_repair_summary.json 存在
agents/voiceover_script/v*/repair_round_1/ 存在
repair_round_1/model_output.json 是 repaired_segments patch
voiceover_script.json 中 v_001_s04 text 已压缩到预算内
narration_text 和 narration_segments 拼接结果一致
TTS 开始执行
最终生成字幕和视频
```

### 13.3 边界测试

需要覆盖：

```text
1. 只有超字数。
2. 只有少字数。
3. 有空文本。
4. 多个 segment 同时超字数。
5. 多个 short_video 同时修复。
6. 模型 repair 后仍不合格。
7. 模型输出 text 而不是 new_text。
8. 模型输出额外字段。
9. narration_text 和 narration_segments 原本不一致。
10. 存在 missing / extra / segment_count mismatch。
```

预期：

```text
1-9：可修复的问题自动修复。
10：第一版默认不自动结构重建，应清晰失败并提示结构类 mismatch。
修复不了的问题在 max_repair_rounds 后明确失败。
失败报告包含最后一轮 alignment check。
```

## 14. 风险点

### 14.1 大模型可能为了压缩字数丢事实

解决：repair prompt 中明确：

```text
压缩表达，但不得改变事实；
必须保留 must_keep_fact_points；
只允许删冗余，不允许删关键事实。
```

### 14.2 大模型可能输出额外字段

解决：代码层归一化，只保留：

```text
short_video_id
repaired_segments[].shot_id
repaired_segments[].new_text
```

模型输出其他字段全部丢弃。

### 14.3 大模型可能仍然超字数

解决：最多修复 2 轮。第二轮输入中加入上一轮失败报告。如果仍失败，再阻断。

### 14.4 修复后 narration_text 和 narration_segments 不一致

解决：不要相信模型的 `narration_text`。局部 patch 修复后，代码必须用 segments 重新拼接：

```python
narration_text = " ".join(seg["text"] for seg in narration_segments if seg.get("text"))
```

这样保证完整文案和分段文案一致。

### 14.5 修复后 timing 丢失

解决：patch 只改 `text`，不改 timing。修复后统一重新调用：

```python
_attach_voiceover_script_timings(final_output, edit_plan)
```

由代码补回或校正 timing。

### 14.6 结构类 mismatch 被局部修复误处理

解决：局部 patch repair 只处理：

```text
char_budget_issues
empty_text_shot_ids
可选 char_budget_warnings
```

对于：

```text
missing_shot_ids
extra_shot_ids
segment_count mismatch
```

第一版默认不自动处理，除非后续单独实现结构重建 repair。

## 15. 回滚方案

新增配置：

```toml
repair_enabled = false
```

当出现问题时，可以直接关闭自动修复，恢复原有硬阻断逻辑。

同时保留：

```text
voiceover_alignment_check.json
repair_round_x/input.txt
repair_round_x/model_output.json
repair_round_x/alignment_after_repair.json
voiceover_repair_summary.json
```

方便排查。

如果只是结构重建策略有风险，可以单独保持：

```toml
repair_structural_mismatch = false
```

继续只启用局部文本修复。

## 16. 最终建议

建议采用该方案，但第一版要收敛：

```text
优先实现 TTS 前 hard char_budget_issue 的自动局部修复。
```

最终行为应从：

```text
模型文案超字数 → 直接失败
```

改成：

```text
模型文案超字数 → 自动让大模型按 shot 字数预算压缩 → 合并 patch → 同步 narration_text → 校验通过后继续 TTS
```

对于本次 `v_001_s04` 这种：

```text
max_chars=28
actual_chars=43
```

正确处理不应该是直接退出，而应该是让 repair 模型把这句压缩到 28 字以内，并保持 `shot_id=v_001_s04` 不变。

需要特别注意：

```text
局部修复输出统一用 repaired_segments[].new_text；
不要混用 text / new_text；
不要让局部修复模型输出完整 narration_segments；
修复后必须用 segments 重新生成 narration_text。
```

这会显著提升自动化成功率，也符合 AI 配音剪辑流程的实际需求。
