# 完整版·去广告（full_concat）v1 · 执行方案

> 与「AI配音解说」「视频重组」并列的第三个生产模式。对用户选择的多条远程/本地素材，
> 按文件名时间戳排序、自动识别并切掉广告/宣传/片头片尾/台标垫片，再按时间顺序拼成一个完整版成片。
> 全自动直接出片，检测信号用 ASR 文案 + 画面（复用已有分析，可缓存）。

## 0. 目标与边界

- **做什么**：多素材 → 时间戳排序 → 片段级广告检测（规则 + DeepSeek 融合）→ 扣掉广告块 → 时序拼接成片 + 移除报告。
- **v1 范围**：片段级（进片内部切广告）、全自动出片、ASR+画面双信号、复用 timeline 全覆盖分析、保守去广告。
- **不做**：人工复核交互（事后给报告）、音频指纹/台标模板匹配（进阶）、广告替换补帧。

## 1. 模式定位与复用

新增 `production_mode = "full_concat"`，本质是「视频重组的反向选片策略」：
- 视频重组：LLM 挑出最精彩短片段、可重排序、限时长。
- 完整版：保留**全部新闻**、只删广告、按时间戳**时序**、不限时长、不打分。

复用现有：公共分析（`source_prepare/source_analysis/source_aggregate` 或 legacy 单源链路）产出的
聚合 timeline（`_load_current_timeline_digest()`，全覆盖、source-aware、含 speech/visual/screen_text/scene/时间码/source_id）；
渲染复用 `_render_reassembly_one()`（裁切 + ffmpeg concat），产物 schema 对齐 `reassembly_cut_plan`。

## 2. 流水线（workflow_registry.py）

```
("full_concat", True):  source_prepare → source_analysis → source_aggregate
                        → ad_detection → full_concat_plan → full_concat_render
("full_concat", False): metadata→audio_extract→frame_extract→chunk_build→asr→asr_digest
                        →vision→timeline→timeline_digest
                        → ad_detection → full_concat_plan → full_concat_render
```

DEPENDENCIES：
- `ad_detection = ["timeline_digest", "source_aggregate"]`
- `full_concat_plan = ["ad_detection"]`
- `full_concat_render = ["full_concat_plan"]`

`run_multisource_pipeline.py` 的 `start_step`：full_concat → `ad_detection`。

## 3. 广告判定标准（片段级，5 类）

基础单元 = timeline digest 的 chunk（覆盖全时长）。每段提取 `speech + screen_text + scene + visual + 时长 + 是否静音`。

| 类别 | label | 主要信号 |
|---|---|---|
| A 商业硬广 | `ad` | 行动号召：热线/电话/扫码/二维码/官网/限时/抢购/特价/钜惠/仅售…元/加盟/招商/办卡/会员；保健品话术 + 画面产品特写/LOGO/价格/二维码常驻、无主持人 |
| B 频道宣传/预告 | `promo` | 敬请收看/即将播出/锁定本台/精彩继续/不要走开/每周X晚 +《节目名》；画面节目名大字包装、栏目片头动画 |
| C 片头/片尾包装 | `trailer` | scene=片头包装、片尾鸣谢滚动、感谢收看/下期再见 |
| D 台标卡/垫片 | `station_id` | 纯台标/彩条/黑场 + ASR 空/极短 + 静音 |
| E 赞助播报 | `sponsor` | 本节目由XX赞助播出/特约播出/鸣谢XX |

判定两层：
1. **规则层**（便宜先跑）：关键词正则命中强话术 → 强标签（confidence=high）；scene=片头包装/空语音 → 中标签（med）。
2. **DeepSeek 层**：规则拿不准（None）的段批量送 `prompt_texts/ad_detection.txt`，输出 `{segment_id, label, confidence, reason}`。

**误删保护（保守默认）**：
- 财经/产经新闻提到公司/产品/股价 ≠ 广告 —— 靠「有无购买/加盟/扫码行动号召 + 有无演播室/主持人/标题条」区分；news 关键词命中则保护。
- 低置信（low）非新闻段 → **保留**并在报告标 `suspected`，不删。
- 孤立可疑段 < `min_isolated_ad_seconds`(默认3s) 不单独删。
- 相邻同类广告段合并成「广告块」整体删除；保留段与广告块交界处各内缩 `safety_margin_seconds`(默认0.4s)，避免广告帧渗入。

## 4. newsclip_agent/ad_detection.py（纯函数 + 步骤共用）

- 关键词常量：`AD_ACTION_KEYWORDS / PROMO_KEYWORDS / SPONSOR_KEYWORDS / SHOPPING_KEYWORDS / NEWS_KEYWORDS`（config 可扩展）。
- `seg_time(chunk) -> (source_id, start, end)`：从 digest chunk 稳健取本地时间码。
- `classify_by_rules(text, scene, has_speech, cfg) -> (label, confidence, reason) | None`：规则层，拿不准返回 None。
- `detect(chunks, *, cfg, llm_call=None) -> {segments[], blocks[], stats}`：规则 + 批量 LLM 兜底 + 默认保守保留。
- `build_plan(chunks, verdicts, source_order, cfg) -> output_videos`：按 source 时间戳排序、组内时序合并保留段成 clip、安全余量、丢弃过短，输出对齐 reassembly_cut_plan 的 clip schema。
- `parse_source_timestamp(name) -> sortkey`：`YYYYMMDD_HHMMSS` → 排序键，回退 create_time/source_index。

LLM 客户端复用 commentary 的 provider 解析（默认 DeepSeek，纯文本便宜）。

## 5. pipeline.py 三个步骤

- `step_ad_detection`：读 `_load_current_timeline_digest()` chunks → `ad_detection.detect()` → 写
  `edit/ad_detection/v{N}/ad_detection.json`（segments + blocks + 移除统计）。input_hash 含 timeline_digest + cfg。
- `step_full_concat_plan`：读 ad_detection + 源顺序 → `build_plan()` → 写
  `edit/full_concat_cut_plan/v{N}/full_concat_cut_plan.json`（schema 同 reassembly_cut_plan，production_mode=full_concat，单成片，时序，无 count 上限）。
- `step_full_concat_render`：复用渲染锁 + `_render_reassembly_one()` → 写
  `edit/full_concat_drafts/v{N}/.../full_concat_draft.mp4` + `full_concat_render_outputs.json` + 移除报告。
- RunOptions 新增 `full_concat_*` 字段；`--production-mode` choices 加 `full_concat`；`parse_args` 加 full_concat 的 audio/tts 兜底（同 original 原声）。

## 6. web_app.py

- `_mode_slug/_mode_aliases/_all_mode_aliases/_task_id_production_mode/_manifest_production_mode/_with_mode_suffix`：加 full_concat（中文「完整版」）。
- `_is_task_already_completed`：final_step = `full_concat_render`（when full_concat）。
- `/api/run`：full_concat 走原声分支（audio_policy=original、skip_tts、不要 require_tts），命令构建无需 reassembly 专属参数。
- 新增 `GET /api/tasks/{id}/ad-report`：读最新 ad_detection.json 返回移除报告（时间段/类别/理由/置信）。

## 7. config.toml

```toml
[full_concat]
enabled = true
default_aspect_ratio = "16:9"

[ad_detection]
enabled = true
use_llm = true
provider = "deepseek"        # 复用 [llm] text_deepseek_*
model = ""
temperature = 0.0
max_tokens = 2000
batch_size = 30
conservative = true
safety_margin_seconds = 0.4
merge_gap_seconds = 1.5
min_clip_seconds = 3.0
min_isolated_ad_seconds = 3.0
extra_ad_keywords = []
extra_promo_keywords = []
extra_news_keywords = []
```

## 8. 前端

- `index.html`：模式卡片 + `#productionMode` 加第三项「完整版（去广告）」。
- `app.js`：`UNIFIED_FULL_CONCAT_STEPS`、`STEP_LABELS`（ad_detection/full_concat_plan/full_concat_render）、`productionModeName`、模式分支（full_concat 走原声、隐藏配音/高光专属控件）、`runSelected` payload、成片预览解析 full_concat 产物、移除报告面板（拉 `/api/tasks/{id}/ad-report`）。
- 本模式下素材篮按文件名时间戳默认升序排序。
- `styles.css`：移除报告小卡样式。

## 9. tests/test_ad_detection.py（离线，无 GPU/网络）

规则层关键词命中、news 保护、片段合并与安全余量、`build_plan` 时序与跨源排序、保守保留 suspected、mock LLM 跑通 `detect`、`parse_source_timestamp` 排序。

## 10. 文件改动清单

**新增**：`newsclip_agent/ad_detection.py`、`prompt_texts/ad_detection.txt`、`tests/test_ad_detection.py`、本文件。
**改**：`config.toml`、`workflow_registry.py`、`pipeline.py`、`run_multisource_pipeline.py`、`web_app.py`、
`web_static/{index.html,app.js,styles.css}`。

## 11. 验收

1. 离线单测全绿（在 comfy_5090_313_auto 跑 `pytest tests/test_ad_detection.py`）。
2. 选多条远程素材 → 完整版模式 → 跑通 → `edit/ad_detection/`、`edit/full_concat_cut_plan/`、`edit/full_concat_drafts/` 产物齐全，成片按时间戳顺序、广告段被切掉。
3. `/api/tasks/{id}/ad-report` 返回移除清单，前端展示。
4. DeepSeek 故障时：规则层照常出片，LLM 兜底缺失只让模糊段保守保留（不误删）。
</content>
</invoke>
