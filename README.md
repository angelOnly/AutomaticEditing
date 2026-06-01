# 凤凰新闻视频智能拆条系统

这是一个面向凤凰新闻长视频的离线 AI 拆条流水线。当前版本重点实现：

- 视频预处理：元信息、音频提取、抽帧、chunk 分段
- FunASR 转写
- 大模型逐 chunk 画面理解
- ASR + 画面理解融合时间轴
- 新闻理解、高光识别、短视频规划、剪辑脚本、解说文案、风险审核 Agent
- OmniVoice 配音、SRT 字幕、FFmpeg 粗剪
- step 级缓存、版本管理、manifest、局部重跑、版本对比

## 环境

使用项目指定环境：

```powershell
conda activate comfy_5090_313_auto
```

不要额外创建环境。FFmpeg / FFprobe 需要在 PATH 中可用。

## 从头运行

```powershell
python run_pipeline.py --input .\videos\日本大分县坦克训练事故.mp4 --task-id task_tank_001
```

## 启动 Web 工作台

```powershell
python run_web.py
```

默认地址：

```text
http://127.0.0.1:7860
```

页面支持：

- 从 `videos` 目录选择素材并创建任务
- 一键启动完整流水线或只跑分析
- 查看任务 `manifest.json` 和每一步状态
- 重跑指定步骤
- 从指定步骤开始重跑后续
- 对 `vision` 步骤指定 `chunk_0007` 这类分段局部重跑
- 查看关键 JSON、日志和粗剪视频

任务结果会落到：

```text
outputs/task_tank_001/
```

## 只跑分析，不生成配音和视频

```powershell
python run_pipeline.py --input .\videos\日本大分县坦克训练事故.mp4 --task-id task_tank_001 --only-analysis
```

## 断点续跑

```powershell
python run_pipeline.py --task-id task_tank_001 --resume
```

已有成功步骤会复用，只跑缺失或失败步骤。

## 重跑某一步

```powershell
python run_pipeline.py --task-id task_tank_001 --rerun highlight_detection
```

## 从某一步开始重跑后续

```powershell
python run_pipeline.py --task-id task_tank_001 --rerun-from highlight_detection
```

## 只重跑某个视觉 chunk

```powershell
python run_pipeline.py --task-id task_tank_001 --rerun vision --chunk chunk_0007
```

如果画面变化快，可以用 dense 模式：

```powershell
python run_pipeline.py --task-id task_tank_001 --rerun vision --chunk chunk_0007 --mode dense
```

## 指定版本继续跑

```powershell
python run_pipeline.py --task-id task_tank_001 --use-version vision=v2 --rerun-from timeline
```

## 对比版本

```powershell
python compare_versions.py --task-id task_tank_001 --step highlight_detection --v1 v1 --v2 v2
```

## 关键输出

```text
manifest.json
metadata/v*/video_metadata.json
preprocess/audio/v*/audio.wav
preprocess/frames/v*/frames.json
preprocess/chunks/v*/chunks.json
asr/v*/asr_segments.json
vision/v*/chunk_0001/input.json
vision/v*/chunk_0001/prompt.txt
vision/v*/chunk_0001/raw_response.json
vision/v*/chunk_0001/parsed_result.json
vision/v*/visual_analysis.json
timeline/v*/merged_timeline.json
agents/highlight_detection/v*/candidate_clips.json
agents/short_video_planning/v*/short_video_plan.json
agents/editing_script/v*/editing_script.json
agents/voiceover_script/v*/voiceover_script.json
agents/risk_review/v*/review_report.json
edit/cut_plan/v*/cut_plan.json
edit/drafts/*/short_video_*_draft.mp4
```

## 人工修订

关键步骤保留模型输出和最终输出。你可以在对应版本目录添加 `manual_override.json`，后续步骤会优先使用人工修订结果。

示例：

```json
{
  "fields_overridden": {
    "scene_type": "资料画面",
    "is_live_scene": false,
    "is_archive_footage": true,
    "notes": "画面左上角有资料字样，不能按现场画面处理。"
  }
}
```

## 注意

`config.toml` 已修正为合法 TOML，并指向当前仓库里的 `models\OmniVoice` 与 `models\iic`。如果视觉模型额度不足，程序会按 `vision_openai_fallback_models` 继续尝试；所有失败会写入对应 step 的状态文件和原始响应文件。
