# 图文解说（Commentary）v1 · 执行方案

> 在「视频重组」成片基础上，用逐段语音文本自动生成同事件的图文新闻解说（标题 + 正文），
> 并把整条成片单点嵌入到最匹配的段落之后。前端「图文解说」按钮直接预览预生成结果。

## 0. 目标与边界

- **做什么**：重组成片渲染完即预生成图文解说，点按钮直接预览；不做多模态识图，只用已有的逐段语音/摘要文本。
- **v1 范围**：单点嵌入、文案/排版两次 DeepSeek 调用、弹窗只读 + 复制 Markdown、预生成 + 接口兜底。
- **不做**：多模态识图、分镜分段嵌入、弹窗内编辑保存、全局换 DeepSeek（另起改动）。

## 1. 数据流与输入组装

对「某任务 + 某 rid」组装喂给提示词的素材，全部来自已有产物：

| 字段 | 来源 | 取法 |
|---|---|---|
| 成片文件 | `edit/reassembly_drafts/v*/reassembly_render_outputs.json` | `outputs[].{reassembly_id, file}` |
| 分镜顺序/时长/作用 | `edit/reassembly_cut_plan/v*/reassembly_cut_plan.json` | `output_videos[rid].clips[]` → `source_clip_id / role / selection_reason / duration_seconds` |
| 逐段原声 speech | 候选池 `candidate_filter` / `asr_event_candidate` | 按 `clip_id == source_clip_id` join，取 `speech / asr_text / summary / visual` |
| 整体主题/摘要/关键事实 | `agents/video_understanding/v*/video_analysis.json` | `main_topic / summary / key_facts` |

**鲁棒性**：`speech` 缺失回退 `summary` / `selection_reason`；每段 speech 截断到 `max_speech_chars_per_clip`，整体设上限避免超 LLM 输入预算。

## 2. 两个提示词

放 `newsclip_agent/prompt_texts/commentary_script.txt`、`commentary_layout.txt`，`.txt` 可随时调词不改代码。

- **文案**：凤凰图文资讯编辑口吻，只用输入事实，标题 ≤22 字，正文 3–5 段，第三人称客观书面，输出 `{title, paragraphs}`。
- **排版**：根据 `paragraphs` + `video_speech` 选最匹配段落，输出 `{insert_after_index, reason}`，下标 0..len-1。

## 3. 配置（config.toml）

```toml
# [llm] 段内新增：DeepSeek（腾讯 tokenhub，OpenAI 兼容，纯文本更便宜）
text_deepseek_model_name = "ep-uk0h8uil"
text_deepseek_fallback_models = []
text_deepseek_api_key = "<key>"
text_deepseek_base_url = "https://tokenhub.tencentmaas.com/v1"

[commentary]
enabled = true
provider = "deepseek"            # 留空则回退全局 text 模型
model = ""                       # 空则用 text_{provider}_model_name
temperature = 0.3
max_tokens = 1200
max_paragraphs = 5
min_paragraphs = 3
title_max_chars = 22
max_speech_chars_per_clip = 300
max_total_input_chars = 6000
layout_temperature = 0.0
generate_in_pipeline = true
```

## 4. newsclip_agent/commentary.py（步骤与接口共用）

- `build_commentary_material(task_dir, reassembly_id)` → 组装素材 dict（无可用成片返回 None）。
- `generate_one(config, material)` → 两次 LLM 调用 + normalize + index 钳制 + markdown 组装 → `commentary_v1` 工件。
- `generate_for_task(config, task_dir, reassembly_id=None, only_missing=False)` → 遍历可渲染 rid，写
  `edit/reassembly_commentary/v{N}/commentary_{rid}.json` + `commentary_outputs.json` 索引。
- `load_latest_commentary(task_dir, reassembly_id=None)` → 读最新版本索引取工件。

**工件 schema `commentary_v1`**：`{version, reassembly_id, title, paragraphs[], insert_after_index,
layout_reason, video{file}, markdown, source{provider,model,clip_count,content_hash,generated_at}, warnings[], status}`。
markdown 用相对文件名占位 `<video>`；接口返回时补可播放 `video.url`。前端用结构化字段渲染，复制用 markdown 串。

## 5. 流水线步骤 reassembly_commentary（预生成）

- `workflow_registry.py`：两条 highlight_reassembly 流程在 `reassembly_render` 后各加 `reassembly_commentary`；
  `DEPENDENCIES["reassembly_commentary"] = ["reassembly_render"]`。
- `pipeline.py` 加 `step_reassembly_commentary`：dispatch 走现成 `getattr(self, f"step_{step}")`。
- **关键**：run loop 对 handler 异常会中断整条任务，因此本步骤**内部吞掉所有异常**，做成尽力而为，
  DeepSeek 失败只让图文解说缺失，不影响成片。缓存用 `_can_reuse` 按 cut_plan 内容哈希。

## 6. 后端接口 GET /api/tasks/{id}/commentary

参数 `reassembly_id?`、`refresh?=0/1`。流程：mode 守卫（非重组 400）→ refresh=0 命中缓存直出 →
未命中/refresh 现生成（老任务补生成 / 重新生成）→ 补 `video.url` 返回。LLM 失败 503。

## 7. 前端

- `index.html`：预览面板加 `#generateCommentary` 按钮 + 弹窗骨架。
- `app.js`：`STEP_LABELS.reassembly_commentary`；两条 step 常量末尾加该步；按钮显隐（重组且有成片）；
  点击解析 rid → 拉接口 → 渲染标题/正文/单点嵌入成片 + 复制/重新生成/关闭。
- `styles.css`：弹窗遮罩与卡片样式。

## 8. 错误处理与边界

成片全 blocked → 空索引 / 接口 404；speech 全空 → 回退 summary；文案空/段数不足 → normalize 兜底 + warning；
index 越界/非数字 → clamp 回退 0；LLM 失败（步骤内）→ partial_success 不影响成片；（接口内）→ 503；
老任务无缓存 → 首访补生成；多成片 → 每 rid 各生成；非重组任务 → 400 + 不显示按钮。

## 9. 测试 tests/test_commentary_layout.py（不依赖 LLM）

index 钳制、`build_commentary_material` join、markdown 组装、normalize、mock LLM 跑通 `generate_one`。

## 10. 文件改动清单

**新增**：`prompt_texts/commentary_script.txt`、`prompt_texts/commentary_layout.txt`、
`newsclip_agent/commentary.py`、`tests/test_commentary_layout.py`、本文件。
**修改**：`config.toml`、`workflow_registry.py`、`pipeline.py`、`web_app.py`、
`web_static/index.html`、`web_static/app.js`、`web_static/styles.css`。

## 11. 验收与联调

1. 离线单测全绿（无需 GPU/网络）。
2. 接口联调：已完成的重组任务 → 起 web → 点按钮 → 看标题/正文/视频位置与复制（DeepSeek 走公网）。
3. 预生成联调（comfy 环境）：跑新重组任务 → `reassembly_commentary` 自动产出 → 点开秒显。
4. 验收：文案不编造输入外事实；视频嵌在语义相关段后；DeepSeek 故障时成片照常且按钮可重试。
