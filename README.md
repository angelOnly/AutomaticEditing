下面这份可以直接作为项目介绍文档的初稿使用。它不是“市场宣传版”，而是偏**产品 + 技术 + 交互 + 任务体系**的详细介绍。

---

# 凤凰新闻视频智能拆条系统项目简介

## 一、项目定位

这个项目是一个面向凤凰新闻长视频素材的**离线 AI 智能拆条与短视频生产系统**。它的核心目标不是简单“剪一段视频”，而是把新闻长视频经过自动化分析后，生成适合短视频发布的成片方案。

项目当前主要覆盖两类生产模式：

第一类是 **AI 配音解说模式**。系统会自动完成视频预处理、语音转写、画面理解、内容分析、高光识别、短视频规划、AI 解说文案生成、OmniVoice 配音、字幕生成、剪辑方案生成和 FFmpeg 粗剪输出。

第二类是 **高光片段原声重组模式**。系统会识别视频里的新闻高光片段，但不生成 AI 配音，而是保留原视频原声，按照新闻逻辑把高价值片段重新组合成一个或多个短视频。

README 中对当前能力的概括是：视频预处理、FunASR 转写、大模型逐 chunk 画面理解、ASR 与画面理解融合时间轴、新闻理解、高光识别、短视频规划、剪辑脚本、解说文案、风险审核、OmniVoice 配音、SRT 字幕、FFmpeg 粗剪，以及 step 级缓存、版本管理、manifest、局部重跑和版本对比。

从工程设计上看，它不是单一脚本，而是一个带 Web 工作台的生产流水线。`run_web.py` 启动 FastAPI Web 工作台；`run_pipeline.py` 是命令行流水线入口；`run_multisource_pipeline.py` 负责多源素材拼接和公共分析复用；核心执行逻辑集中在 `newsclip_agent/pipeline.py`。  

---

## 二、项目主要功能

## 1. 视频素材管理

系统支持两类素材来源：本地素材和远程素材。

本地素材来自项目 `videos` 目录，也支持通过 Web 页面上传。后端会扫描 `videos` 目录下的 `.mp4`、`.mov`、`.mkv`、`.m4v`、`.avi` 等视频文件，并返回文件名、路径、大小、更新时间等信息。

Web 页面左侧有“本地素材”区域，提供上传按钮和刷新按钮；上传控件支持多文件上传。

远程素材主要通过 UCMS 接口接入。Web 后端提供远程素材列表、分页、搜索、排序、按信号源筛选，以及远程视频下载接口。 页面上也有“远程素材”区域，包含信号源选择、关键词搜索、时间正序/倒序排序、上一页/下一页分页。

这意味着项目可以适配两种使用场景：

一种是编辑人员已经把视频放到本地 `videos` 目录，直接选择素材运行。

另一种是通过远程新闻素材系统选择视频，系统先把远程视频下载到本地缓存，再进入分析流程。

---

## 2. 单源视频智能拆条

单源视频是最基础的处理方式。用户选择一个视频，系统直接创建一个任务目录，然后执行完整 pipeline。

命令行入口是：

```bash
python run_pipeline.py --input ./videos/xxx.mp4 --task-id task_xxx
```

README 中也说明，运行后任务结果会落到 `outputs/<task_id>/` 目录。

单源任务首次运行时，`PipelineRunner` 会把输入视频复制或链接到任务目录下的 `input/source.xxx`，同时初始化 `task.json` 和 `manifest.json`。manifest 会记录任务 ID、创建时间、源视频、source_mode、source_manifest、source_videos、当前版本和每一步状态。

单源任务不需要额外的 source prepare，因此 `source_prepare` 会被标记为 skipped，原因是 `single_source`。

---

## 3. 多源视频混合剪辑

多源剪辑是这个分支里比较重要的能力。用户可以把多个本地素材、远程素材混合加入“本次剪辑素材”列表。Web 页面明确写着：支持本地素材、远程素材混合拖拽，并按列表顺序拼接分析。

多源任务不会直接进入普通 `run_pipeline.py`，而是先进入 `run_multisource_pipeline.py`。这个脚本会读取 `source_request.json`，解析多个 source item，把素材统一准备为一个可分析的拼接代理视频，同时生成 source manifest。

多源任务至少需要 2 个 source item，否则会报错 `multi-source run requires at least 2 source items`。每个 source item 可以是本地视频，也可以是远程 UCMS 视频；远程视频会先下载缓存，再转成本地路径参与拼接。

多源处理的核心逻辑是：

先把多个源视频规范化为 `MultiSourceItem`。

再调用 `build_multi_source_video` 生成拼接代理视频和 source manifest。

然后用这个拼接视频跑后续分析。

这样做的好处是：后面的 ASR、视觉理解、时间轴融合、内容分析都可以把多段素材当成一个连续视频来处理，同时 manifest 仍保留每个原始素材的信息，方便后续追踪片段来源。

---

## 4. 公共分析复用

多源任务还有一个重要设计：**公共分析目录**。

当多个任务使用同一组素材时，系统会根据素材请求生成 `common_source_key`，再得到一个公共任务 ID：`common_<common_source_key>`。公共分析结果会放在：

```text
outputs/__common__/common_xxx
```

Web 层定义了一组可复用的公共步骤：

```text
source_prepare
metadata
audio_extract
frame_extract
chunk_build
asr
vision
timeline
timeline_digest
```

这些步骤在 `web_app.py` 中被定义为 `COMMON_REUSABLE_STEPS`。

`run_multisource_pipeline.py` 会先确保公共分析已经完成。如果公共目录里的这些步骤已经成功、部分成功或跳过，就直接复用；否则会加锁运行公共分析，避免同一组素材被多个任务重复分析。

公共分析完成后，系统会把 `input`、`metadata`、`preprocess`、`asr`、`vision`、`timeline` 等目录复制到具体子任务目录，并在子任务 manifest 中标记 `reused_common_steps`。

这个设计非常关键，因为两种生产模式都依赖前半段分析。如果同一组素材既要跑“AI 配音解说”，又要跑“高光原声重组”，就没有必要重复跑抽帧、ASR、视觉理解和时间轴融合。

---

## 5. AI 配音解说视频生成

AI 配音解说模式的完整步骤顺序是：

```text
metadata
audio_extract
frame_extract
chunk_build
asr
vision
timeline
timeline_digest
content_analysis
short_video_edit_plan
voiceover_script
tts
subtitles
cut_plan
render
```

这个顺序定义在 `AI_VOICEOVER_STEP_ORDER`。

它的大致实现逻辑是：

先通过 FFprobe 读取视频元信息。

再用 FFmpeg 提取音频，统一为单声道 16k wav。

然后按固定间隔抽帧，并按 chunk 秒数切分视频段。

接着用 FunASR 对音频做语音识别。

再对每个 chunk 做视觉理解，把画面信息和对应 ASR 文本交给视觉大模型。

之后融合 ASR 和视觉理解结果，形成统一时间轴。

时间轴再被压缩为 `timeline_digest`，作为后续文本 Agent 的轻量输入。

文本 Agent 会先做内容分析，再生成短视频编辑方案，随后生成 AI 解说文案。

TTS 步骤调用 OmniVoice 生成配音音频。

字幕步骤根据解说文案或 TTS 分段生成 SRT。

cut_plan 步骤把剪辑结构、配音、字幕、时长校验整合为可渲染方案。

render 步骤最终调用 FFmpeg 输出粗剪视频。

---

## 6. 高光片段原声重组

高光重组模式的步骤顺序是：

```text
metadata
audio_extract
frame_extract
chunk_build
asr
vision
timeline
timeline_digest
video_understanding
highlight_detection
highlight_reassembly_plan
reassembly_cut_plan
reassembly_render
```

这个顺序定义在 `HIGHLIGHT_REASSEMBLY_STEP_ORDER`。

与 AI 配音模式相比，它不走 `voiceover_script`、`tts`、`subtitles`、`cut_plan`、`render` 这条链路，而是改走：

```text
video_understanding
highlight_detection
highlight_reassembly_plan
reassembly_cut_plan
reassembly_render
```

Web 后端在接收到 `production_mode == "highlight_reassembly"` 时，会自动把音频策略改成 `original`，关闭 TTS，允许原声证据使用。

也就是说，高光重组模式的产品逻辑是：

不重新写新闻解说。

不生成 AI 配音。

不烧录 AI 解说字幕。

保留原视频里的声音。

根据新闻逻辑、高光评分、素材顺序或编辑策略，把片段重新组合成一个或多个视频。

Web 页面中这个模式叫“视频重组”，说明是“保留原声，按新闻逻辑重组高光片段”。

---

# 三、核心流水线实现逻辑

## 1. metadata：视频元信息读取

`step_metadata` 使用 `ffprobe_json` 读取源视频元信息，并把结果写入：

```text
metadata/v*/video_metadata.json
```

它会根据源视频路径、文件大小、修改时间生成 input hash。如果 hash 一致且已有成功结果，就复用缓存。

这个步骤的作用是获得视频时长、编码、分辨率等基础信息，为后续 chunk 切分、时长控制、渲染参数提供基础数据。

---

## 2. audio_extract：音频提取

`step_audio_extract` 使用 FFmpeg 从源视频提取音频，输出为：

```text
preprocess/audio/v*/audio.wav
```

音频格式固定为：

```text
单声道
16000 Hz
wav
```

对应命令中使用了 `-vn -ac 1 -ar 16000`。

这个格式主要是为了适配 ASR 模型，降低后续语音识别的不确定性。

---

## 3. frame_extract：抽帧

`step_frame_extract` 使用 FFmpeg 按间隔抽帧。普通模式下使用用户配置的 `frame_interval`，dense 模式下强制使用 1 秒间隔。

输出包括：

```text
preprocess/frames/v*/frames/
preprocess/frames/v*/frames.json
```

`frames.json` 里会记录每一帧的 index、time、seconds 和文件路径。

这个步骤是视觉理解的输入基础。

---

## 4. chunk_build：视频分段

`step_chunk_build` 根据视频总时长和 `chunk_seconds` 把视频切成多个 chunk。每个 chunk 包含：

```text
chunk_id
start
end
time_range
frames
```

输出为：

```text
preprocess/chunks/v*/chunks.json
```

chunk 数量由视频时长除以 chunk 秒数得到。

这个设计的好处是，大模型不用一次看完整视频，而是逐段理解。这样既控制输入规模，也方便某个 chunk 失败后局部重跑。

---

## 5. asr：FunASR 转写

`step_asr` 读取 `audio.wav`，调用 `services.asr.FunASREngine` 进行语音识别。FunASR 引擎加载 SenseVoiceSmall、VAD 模型和标点模型。

输出包括：

```text
asr/v*/asr_segments.json
asr/v*/full_text.txt
```

`asr_segments.json` 会保存分段文本、完整文本和原始识别结果。

ASR 是后续新闻理解、高光识别、解说文案生成的重要文本基础。

---

## 6. vision：逐 chunk 画面理解

`step_vision` 是视频理解的关键步骤。它会读取 chunk、ASR 和抽帧结果，然后对每个 chunk 调用视觉大模型。

每个 chunk 的输入包括：

```text
chunk 信息
chunk 内 ASR 文本
prompt_version
model
若干抽帧图片
```

系统会使用 `select_frames_for_vision` 控制每个 chunk 的最大帧数，避免输入过大。

vision 步骤支持并发处理。代码中使用 `ThreadPoolExecutor`，并根据 `vision_max_workers` 并发提交多个 chunk 分析任务。

它还支持局部重跑。例如只重跑 `chunk_0007`，其他 chunk 会从上一个 vision 版本复制复用。

每个 chunk 会输出：

```text
vision/v*/chunk_xxxx/input.json
vision/v*/chunk_xxxx/prompt.txt
vision/v*/chunk_xxxx/raw_response.json
vision/v*/chunk_xxxx/parsed_result.json
vision/v*/chunk_xxxx/step_status.json
```

总结果汇总到：

```text
vision/v*/visual_analysis.json
```

最终状态如果全部成功就是 success；如果有失败但部分成功，则是 partial_success。

---

## 7. timeline：ASR 与视觉结果融合

`step_timeline` 会把 ASR 分段、chunk 信息和视觉理解结果合并成统一时间轴。

每个时间轴条目包含：

```text
chunk_id
start / end
start_seconds / end_seconds
asr_text
asr_segments
scene_type
visual_summary
screen_text
visible_people
is_live_scene
is_archive_footage
visual_value_score
hook_score
risk_tags
notes
```

输出为：

```text
timeline/v*/merged_timeline.json
```



这个文件是后续所有 Agent 的核心素材，它把“说了什么”和“画面是什么”合并到了同一个时间结构里。

---

## 8. timeline_digest：压缩时间轴

`step_timeline_digest` 会把完整时间轴压缩成适合 LLM 输入的轻量版本。它会保留每个 chunk 的时间、文本摘要、视觉摘要、屏幕文字、人物、场景、视觉评分、hook 评分和 flags。

输出为：

```text
timeline/v*/llm_timeline_digest.json
```

这个步骤的意义很大：它避免把完整 ASR、完整视觉结果、完整 JSON 全部丢给文本大模型，从而减少 token 成本、提升稳定性，也降低超出输入限制的风险。

代码里还有 `_log_llm_input_size`，如果文本 Agent 输入超过 `max_text_agent_input_chars`，会直接报错并提示应该改用 `timeline_digest`。

---

# 四、Agent 功能与实现逻辑

## 1. Agent 注册表

项目用 `AGENT_INFO` 统一注册各个文本 Agent，包括输出目录、输出文件名、prompt 和默认 prompt version。

当前包括：

```text
video_understanding
highlight_detection
short_video_planning
editing_script
content_analysis
short_video_edit_plan
voiceover_script
risk_review
highlight_reassembly_plan
```

虽然当前 AI 配音主流程已经改为 `content_analysis -> short_video_edit_plan -> voiceover_script`，但代码里仍保留了兼容旧流程的 `short_video_planning`、`editing_script`、`risk_review` 等 Agent。

---

## 2. 通用文本 Agent 执行器

所有文本 Agent 最终都会通过 `_run_text_agent` 执行。

它会：

读取模型配置。

确定 provider、model、fallback_models、max_tokens。

计算 input hash。

如果已有成功缓存则复用。

创建版本目录。

记录 input.json 和 prompt.txt。

调用文本 LLM，要求返回 JSON。

保存 raw_response.json、model_output.json。

合并 manual_override.json。

保存 final_output.json 和最终业务输出文件。

更新 step_status 和 manifest。

这说明项目不是简单调用一次 LLM，而是为每个 Agent 都保留了完整的可追溯信息。后续排查问题时，可以看到输入、prompt、原始响应、解析结果、人工修订和最终输出。

---

## 3. content_analysis：内容分析

AI 配音模式下，`content_analysis` 读取 `timeline_digest` 和简化后的运行参数，生成视频主题、新闻类型、摘要、关键事实、人物、地点、内容结构、潜在角度、候选片段等信息。

执行后，它还会写兼容输出：把 content_analysis 结果转换成旧流程需要的 `video_understanding` 和 `highlight_detection` 输出。这样旧下游模块仍然可以复用。

这个设计说明项目正在从“多个 Agent 串联”向“合并 Agent、减少调用次数”的方向优化，但仍保留旧结构兼容性。

---

## 4. short_video_edit_plan：短视频方案生成

`short_video_edit_plan` 读取：

```text
content_analysis
timeline_digest
duration_strategy
run_options
source_mode
source_videos
source_manifest
```

然后生成短视频编辑计划。

它会处理单条视频或多条视频输出的规划问题，包括是否拆分、每条视频的主题、时长、结构、片段选择、字幕关键词、解说方向等。

生成后还会调用 `_normalize_short_video_split_decision` 标准化拆分决策，并写兼容的 `short_video_plan` 和 `editing_script`，供后续 voiceover、cut_plan 等步骤使用。

---

## 5. voiceover_script：AI 解说文案生成

`voiceover_script` 负责把编辑方案转成可配音的新闻解说稿。

它会读取：

```text
short_video_edit_plan
editing_script
voiceover_timing_contracts
duration_strategy
run_options
```

其中 `voiceover_timing_contracts` 会根据剪辑脚本和时长设置构建，用于约束文案长度和节奏。

生成后系统会做几类校验：

清洗文案。

检查文案时长是否匹配目标。

检查是否超过 60 秒，如果需要长版确认，会把 manifest 状态改为 `action_required`。

这也是 Web 上“需要确认长版”的来源。页面里有 `actionRequiredBanner` 和“同意生成长版”按钮。

---

## 6. video_understanding 与 highlight_detection

高光重组模式会使用 `video_understanding` 和 `highlight_detection`。

`video_understanding` 读取 `timeline_digest`，理解整条新闻素材的主题、结构、人物、地点、事件线索等。

`highlight_detection` 读取 `timeline_digest` 和 `video_analysis`，识别候选高光片段。

这些候选片段后续会进入 `highlight_reassembly_plan`，用于生成原声重组方案。

---

## 7. highlight_reassembly_plan：高光重组方案

`highlight_reassembly_plan` 读取：

```text
run_options
reassembly_options
timeline_digest
video_analysis
candidate_clips
source_mode
source_videos
source_manifest
```

用于生成高光重组方案。

重组参数包括：

```text
output_mode
sort_mode
target_seconds
min_clip_seconds
max_clip_seconds
max_clip_count
export_individual_clips
keep_original_audio
```

这些参数由 `_reassembly_options_payload` 生成。

Web 页面中也提供了对应控件：重组方式、排序方式、最多片段数。重组方式支持单条综合片、多条拆分片、单片段预览；排序方式支持新闻逻辑优先、保持原始顺序、按高光评分。 

---

# 五、配音、字幕与渲染逻辑

## 1. TTS：OmniVoice 配音

`step_tts` 会根据配置中的音色信息调用 OmniVoice。它支持多个 voice_id，Web 后端会从配置中读取音色列表，暴露给前端，前端可以选择音色并试听。

TTS 逻辑支持三种情况：

如果 `audio_policy == original` 或 `skip_tts`，则跳过 TTS。

如果解说稿有 narration_segments，则按分段生成音频。

如果没有分段，则整段文本生成一个 voiceover.wav。

分段模式下还会根据是否需要证据原声，决定使用 `segment_aligned` 还是 `compact_segmented`。

TTS 生成完后，会输出：

```text
tts/omnivoice/v*/tts_outputs.json
tts/omnivoice/v*/SVxxx/voiceover.wav
tts/omnivoice/v*/SVxxx/segments/*.wav
```

如果 `require_tts=true` 且 TTS 失败，会阻断后续 cut_plan/render。

---

## 2. subtitles：字幕生成

`step_subtitles` 读取 `voiceover_script` 和 TTS 输出，生成 SRT 字幕。

字幕来源优先级大致是：

如果配置要求跟随实际 TTS，且有 TTS segments，就按 TTS segments 生成字幕。

否则如果文案里有 narration_segments，就按文案分段生成字幕。

否则把完整解说文本拆成字幕块。

字幕切分会按句号、问号、感叹号、逗号、顿号等中文标点切分，并根据横版/竖版设置每行最大字数。

字幕样式也通过配置控制，包括字体、字号、颜色、描边、背景、边距、对齐方式等。

---

## 3. cut_plan：剪辑方案生成

`step_cut_plan` 是 AI 配音模式里非常关键的一步。它会把 editing_script、voiceover_script、TTS 输出、字幕输出整合成最终可渲染的剪辑计划。

每条 output video 会包含：

```text
short_video_id
output_file
aspect_ratio
target_resolution
target_duration_seconds
max_allowed_seconds
voiceover_duration_seconds
video_duration_seconds
duration_delta_seconds
duration_status
blocked_reasons
warnings
clips
voiceover
subtitles
cover
```

代码中可以看到 cut_plan 会校验：

剪辑结构时长是否合理。

TTS 是否成功。

AI 配音时长和视频画面时长是否匹配。

是否存在证据原声窗口。

是否需要 segment_aligned TTS。

是否低于最低合格时长。

是否需要长版确认。

如果某条视频被阻断，会记录 blocked_reasons；如果所有候选都被阻断，cut_plan 会失败。 

---

## 4. render：FFmpeg 粗剪输出

`render` 会读取 cut_plan，逐条视频渲染。

渲染逻辑包括：

按 clips 中的 source_start/source_end 从原视频切片。

按横版或竖版生成视频滤镜。

把片段 concat 到一起。

如果启用 AI 配音，就把原声和 voiceover 混音。

如果有证据原声窗口，会在对应时间段保留或增强原声，并在对应窗口降低 AI 配音。

如果有字幕，就用 FFmpeg subtitles 滤镜烧录字幕。

最终输出粗剪 mp4。

输出会记录在：

```text
edit/drafts/v*/render_outputs.json
edit/drafts/v*/short_video_xxx_draft.mp4
```

Web 后端会查找最新 draft 视频，用于页面预览。

---

# 六、任务模式如何区分

## 1. production_mode 区分主流程

项目通过 `production_mode` 区分任务类型。

当前主要有两个值：

```text
ai_voiceover
highlight_reassembly
```

在 pipeline 内部，`_active_step_order` 会根据 production_mode 返回不同步骤顺序。`highlight_reassembly` 返回高光重组步骤；其他默认返回 AI 配音解说步骤。

Web 后端也根据 production_mode 构建不同 step order。AI 配音模式包含 `content_analysis`、`short_video_edit_plan`、`voiceover_script`、`tts`、`subtitles`、`cut_plan`、`render`；高光重组模式包含 `video_understanding`、`highlight_detection`、`highlight_reassembly_plan`、`reassembly_cut_plan`、`reassembly_render`。

---

## 2. task_id 通过模式后缀区分

Web 层创建任务时，会根据输入视频、production_mode、fingerprint 自动生成任务 ID，并通过 `_with_mode_suffix` 加上模式后缀。虽然具体函数实现未在截取片段里完整展开，但 `run_pipeline` 中可以看到创建任务后会调用 `_with_mode_suffix(task_id, req.production_mode)`。

任务列表中还会根据 manifest 或 source_request 判断 production_mode，并显示对应模式标题。

这样同一素材可以同时存在：

```text
xxx_ai_voiceover
xxx_highlight_reassembly
```

类似的两个任务，避免不同模式之间互相覆盖。

---

## 3. 已完成任务自动去重

Web 后端会检查是否已有相同 task_id 的活跃 job。如果已有 pending 或 running job，会返回已存在任务，不重复启动。

如果 manifest 显示同一模式的最终步骤已经成功，系统也会直接返回“相同任务已完成，直接复用结果”。AI 配音模式最终步骤是 `render`，高光重组模式最终步骤是 `reassembly_render`。 

---

# 七、复用、缓存与版本管理

## 1. step 级缓存

每个 step 都有 input_hash。执行前会调用 `_can_reuse` 判断是否可以复用：

状态必须是 success。

input_hash 必须一致。

输出文件必须存在。

当前不是 rerun 或 rerun_from 强制重跑。

resume 必须开启。

这让系统具备断点续跑能力。README 中也说明，`--resume` 会复用已有成功步骤，只跑缺失或失败步骤。

---

## 2. 版本目录

每个 step 都不是覆盖旧输出，而是写入 `v1`、`v2`、`v3` 这种版本目录。

`_version_dir` 会扫描已有版本号，然后创建下一个版本目录。

manifest 中会记录：

```text
current_versions
steps[step].version
steps[step].output
steps[step].input_hash
steps[step].output_hash
```

`_record_step` 会统一记录这些信息，并在重跑某一步时把下游步骤标记为 stale。

---

## 3. 单步重跑和从某步重跑

命令行支持：

```bash
--rerun highlight_detection
--rerun-from highlight_detection
```

README 已列出重跑某一步、从某一步开始重跑后续、指定版本继续跑等操作。

pipeline 中 `_resolve_selected_steps` 也明确实现了：

`rerun`：只跑指定步骤。

`rerun_from`：从指定步骤开始跑后续所有步骤。

`stop_after`：跑到某一步就停止。

`common_only`：只跑公共分析步骤。

---

## 4. vision chunk 局部重跑

视觉理解是最耗时也最容易局部失败的步骤，因此它支持 chunk 级重跑。

用户可以指定：

```bash
--rerun vision --chunk chunk_0007
```

README 中也专门列出“只重跑某个视觉 chunk”和 dense 模式。

代码逻辑是：如果当前 chunk 不需要重跑，就从上一版 vision 目录复制对应 chunk 结果；只有指定 chunk 或失败 chunk 会重新调用视觉模型。

---

## 5. 模式切换复用

项目还支持从一个模式复用另一个模式的前置结果。Web 层定义了 `REUSABLE_MODE_SWITCH_STEPS`：

```text
metadata
audio_extract
frame_extract
chunk_build
asr
vision
timeline
video_understanding
highlight_detection
```

如果创建新任务时传入 `reuse_from_task_id`，并且目标任务 manifest 不存在，就会从源任务复制这些已经成功的步骤输出。

这个能力适合这样的场景：

先跑了 AI 配音模式，后来想再跑高光重组模式。

或者先跑高光重组，后来想生成 AI 解说版。

前面的素材分析结果不需要重复计算。

---

# 八、公共模块说明

## 1. config：项目配置

`newsclip_agent.config` 负责加载 `config.toml`，Web 和 pipeline 都会使用它。Web 启动时会读取 workflow、short_video、voiceover、remote_ucms、web_concurrency 等配置。

配置影响：

默认 chunk 秒数。

默认抽帧间隔。

默认画面比例。

默认目标时长。

是否允许长版。

TTS 是否必须成功。

音色列表。

远程素材接口。

Web 并发数和排队数。

---

## 2. utils：通用文件与命令工具

pipeline 中大量使用 `newsclip_agent.utils`，包括：

```text
copy_or_link
ensure_dir
ffprobe_json
now_iso
output_hash
read_json
relpath
run_cmd
seconds_to_timecode
srt_time
stable_hash
timecode_to_seconds
write_json
write_text
```

这些工具负责目录创建、JSON 读写、路径转换、哈希、时间码转换、FFmpeg/FFprobe 命令执行等基础能力。

---

## 3. llm 与 prompts

`OpenAICompatibleClient` 用来兼容 OpenAI 风格接口，也支持配置不同 provider，比如 openai 或 doubao。pipeline 会分别创建 text LLM 和 vision LLM。

`prompts` 里保存各个 Agent 的提示词，`AGENT_INFO` 会把 step 与对应 prompt 绑定。

---

## 4. llm_digest：LLM 输入压缩

`llm_digest` 提供：

```text
build_chunk_flags
compact_asr_for_llm
compact_chunk_for_vision
compact_list
compact_text
select_frames_for_vision
```

这些函数用于压缩 ASR 文本、视觉 chunk、列表字段、时间轴字段，并控制送入视觉模型的帧数量。

这个模块是控制大模型输入规模的关键。

---

## 5. duration_policy：时长与音频策略

`duration_policy` 是控制成片时长、配音时长、原声证据、音频窗口、时长阻断逻辑的重要模块。

pipeline 导入了大量相关函数，例如：

```text
build_voiceover_timing_contract
compact_voiceover_segment_times
editing_structure_duration
duration_mismatch_block_reason
evidence_audio_windows_from_clips
validate_audio_policy
validate_voiceover_gaps
validate_editing_duration
voiceover_duration_fit
```



这个模块决定了：

解说稿是否过长。

视频结构是否满足目标时长。

AI 配音和视频画面是否匹配。

是否需要保留证据原声。

原声窗口和配音窗口是否冲突。

是否应该阻断渲染。

---

## 6. resource_locks：资源锁

项目会使用 `file_slot_lock` 控制 GPU 或公共资源并发。例如 ASR 和 TTS 都会根据配置中的 slots 获取锁，避免多个任务同时抢同一类模型资源。 

多源公共分析也使用 `file_slot_lock(lock_name, slots=1)`，避免同一组公共素材被并发重复分析。

---

## 7. multisource 与 remote_ucms

`newsclip_agent.multisource` 负责多源素材对象和拼接代理视频生成。Web 和多源 pipeline 都会调用 `MultiSourceItem` 和 `build_multi_source_video`。 

`newsclip_agent.remote_ucms` 负责远程 UCMS 视频列表、数量统计、下载、本地缓存、公有配置输出等。Web 页面远程素材列表和下载接口都基于它。

---

# 九、Web UI 交互设计

## 1. 页面整体结构

Web 页面叫“凤凰智剪”，副标题是“断点续跑 · 高光识别 · 程序导出”。

整体布局是：

左侧 sidebar：素材和任务队列。

中间 workspace：任务标题、运行按钮、进度状态、参数、AI 处理流程。

右侧 right-stack：视频预览、输出产物、文件内容、运行日志。

---

## 2. 左侧：素材与任务

左侧包含：

本地素材列表。

远程素材列表。

任务队列。

本地素材支持上传和刷新。远程素材支持信号源、搜索、排序、分页。任务队列支持刷新。

用户的典型操作是：

先从本地或远程选择素材。

素材会加入“本次剪辑素材”篮子。

如果只加入一个素材，就是单源任务。

如果加入多个素材，就是多源任务。

然后设置任务参数，点击开始分析。

---

## 3. 顶部：任务状态与控制

顶部显示当前任务标题、任务 ID、刷新按钮、终止任务按钮、开始分析按钮。

下方有状态概览：

当前运行进度。

运行状态。

画面比例。

当前步骤。

成功/跳过/失败统计。



如果任务进入 `action_required`，例如超过 60 秒需要用户确认长版，页面会显示确认横幅，并提供“同意生成长版”按钮。

---

## 4. 中间：素材篮子和参数区

“本次剪辑素材”区域支持拖拽多个视频，也支持从左侧本地/远程素材点击加入。页面提示“按列表顺序拼接分析”。

任务参数区包含：

生产模式：AI 配音解说 / 视频重组。

画面比例：16:9 / 9:16。

chunk 秒数。

抽帧间隔。

是否允许详述，最长 60 秒或严格控制。

TTS 是否必须成功。

输出方式：一个视频 / 多个视频。

最多成片数。

配音音色。

试听音色。

重组方式。

重组排序方式。

最多片段数。

 

这说明 UI 不是单纯“上传并运行”，而是允许编辑人员在生产前控制视频比例、分析粒度、成片数量、配音策略和重组策略。

---

## 5. AI 处理流程区

页面有“AI 处理流程”区，展示每个步骤状态。用户可以选择某个步骤，执行：

重跑步骤。

从此步后续重跑。

vision 步骤还可以填写 chunk，例如 `chunk_0007`。

这对应后端的 `rerun`、`rerun_from`、`chunk` 参数。

---

## 6. 右侧：预览、产物、日志

右侧包括：

视频预览：可切换“最新成片”和“原片”。

成片列表。

输出产物文件树。

文件内容查看器。

复制文件内容按钮。

运行日志查看器。

错误信息展示。



后端提供：

`/api/tasks/{task_id}/latest-video`

`/api/tasks/{task_id}/drafts`

`/api/tasks/{task_id}/tree`

`/api/tasks/{task_id}/file`

`/api/jobs/{job_id}/log`

用于支撑这些 UI 功能。  

---

# 十、任务执行与队列机制

Web 后端不是直接阻塞请求执行 pipeline，而是创建 job，写入队列，再由 `_schedule_jobs` 控制并发启动。

每个 job 会包含：

```text
job_id
task_id
pid
cmd
log
created_at
started_at
status
returncode
submitted_by
task_fingerprint
```

job 日志写入：

```text
outputs/<task_id>/web_jobs/<job_id>.log
```



系统支持：

最大运行任务数 `MAX_RUNNING_JOBS`。

最大等待队列数 `MAX_PENDING_JOBS`。

同一任务并发策略 `SAME_TASK_POLICY`。

这些配置来自 `web_concurrency`。

任务启动时使用 `subprocess.Popen`，设置 UTF-8 环境变量，并把 stdout/stderr 写入日志。

终止任务时，Windows 下使用 `taskkill /PID /T /F`，非 Windows 下使用进程组 SIGTERM。

---

# 十一、主要输出目录结构

README 中列出了关键输出。整理后大致如下：

```text
outputs/<task_id>/
  manifest.json
  task.json

  input/
    source.mp4
    source_request.json
    source_manifest.json

  metadata/v*/
    video_metadata.json

  preprocess/
    audio/v*/audio.wav
    frames/v*/frames.json
    chunks/v*/chunks.json

  asr/v*/
    asr_segments.json
    full_text.txt

  vision/v*/
    visual_analysis.json
    chunk_0001/
      input.json
      prompt.txt
      raw_response.json
      parsed_result.json

  timeline/v*/
    merged_timeline.json
    llm_timeline_digest.json

  agents/
    content_analysis/v*/content_analysis.json
    short_video_edit_plan/v*/short_video_edit_plan.json
    voiceover_script/v*/voiceover_script.json
    video_understanding/v*/video_analysis.json
    highlight_detection/v*/candidate_clips.json
    highlight_reassembly/v*/highlight_reassembly_plan.json

  tts/omnivoice/v*/
    tts_outputs.json
    SV001/voiceover.wav

  subtitles/v*/
    subtitle_outputs.json
    SV001/subtitle.srt

  edit/
    cut_plan/v*/cut_plan.json
    drafts/v*/render_outputs.json
    drafts/v*/short_video_xxx_draft.mp4
    reassembly_drafts/v*/reassembly_render_outputs.json
```

README 中也明确列出了 metadata、audio、frames、chunks、ASR、vision、timeline、Agent 输出、cut_plan 和 draft mp4 等关键产物。

---

# 十二、人工修订机制

项目支持在关键步骤版本目录下放置 `manual_override.json`，后续步骤会优先使用人工修订结果。README 给出的例子是修正某个视觉 chunk 的 scene_type、is_live_scene、is_archive_footage 和 notes。

文本 Agent 的 `_run_text_agent` 也会读取当前版本目录下的 `manual_override.json`，并把它合并到模型输出中，再写出 final_output。

这对于新闻编辑场景很重要，因为 AI 识别可能误判“现场画面”和“资料画面”，人工修订后不必重跑所有前置步骤。

---

# 十三、项目整体工作流总结

## AI 配音解说模式

完整链路可以概括为：

```text
选择素材
→ 创建任务
→ 视频元信息
→ 提取音频
→ 抽帧
→ chunk 分段
→ FunASR 转写
→ 逐 chunk 视觉理解
→ ASR + 画面融合时间轴
→ 压缩 timeline_digest
→ 内容分析
→ 短视频编辑方案
→ AI 解说文案
→ OmniVoice 配音
→ SRT 字幕
→ cut_plan 时长与音频校验
→ FFmpeg 渲染粗剪
→ Web 预览成片
```

适合做：

新闻事件解释型短视频。

多片段组合的 AI 解说视频。

需要统一口播、统一字幕、统一包装的新闻短视频。

---

## 高光原声重组模式

完整链路可以概括为：

```text
选择素材
→ 创建任务
→ 视频元信息
→ 提取音频
→ 抽帧
→ chunk 分段
→ FunASR 转写
→ 逐 chunk 视觉理解
→ ASR + 画面融合时间轴
→ 压缩 timeline_digest
→ 整体视频理解
→ 高光候选片段识别
→ 高光重组方案
→ 原声剪辑计划
→ FFmpeg 重组渲染
→ Web 预览成片
```

适合做：

发布会、采访、直播连线的原声精华。

保留现场声音的新闻高光片段。

不需要 AI 解说，只需要自动筛选和重组的短视频。

---

# 十四、这个项目的核心价值

这个项目的核心价值不是单点 AI 能力，而是把新闻短视频生产拆成了一条可追踪、可重跑、可复用、可人工修订的工程流水线。

它的优势主要有：

第一，**把长视频处理成结构化时间轴**。ASR 只知道“说了什么”，视觉模型只知道“看到了什么”，timeline 把两者融合，成为后续新闻判断的基础。

第二，**把大模型调用拆成可控步骤**。视觉按 chunk 并发处理，文本 Agent 使用 digest 输入，降低单次上下文压力。

第三，**支持两种生产模式**。同一套前置分析既能生成 AI 配音解说视频，也能生成原声高光重组视频。

第四，**支持多源素材**。多个本地或远程素材可以混合加入，先拼接代理视频，再统一分析。

第五，**支持公共分析复用**。同一组素材的前置分析可以放到 `outputs/__common__`，不同任务只跑模式专属步骤。

第六，**具备生产级调试能力**。每一步都有 input、prompt、raw response、parsed result、final output、status、hash、版本目录和 manifest 状态。

第七，**Web 工作台对编辑人员友好**。用户可以上传素材、选择远程素材、拖拽多源素材、设置参数、查看进度、重跑步骤、预览成片、查看产物和日志。

第八，**适合逐步优化**。由于每一步都是独立模块，后续可以替换 ASR、替换视觉模型、优化 prompt、减少 Agent 数量、提升并发、调整字幕样式、改变时长策略，而不必推翻整体架构。

---

# 十五、一句话介绍

**凤凰新闻视频智能拆条系统，是一个面向新闻长视频的 AI 短视频生产工作台：它可以把本地或远程新闻素材经过 ASR、视觉理解、时间轴融合和大模型 Agent 分析，自动生成 AI 配音解说短视频，或保留原声重组高光片段，并通过 step 缓存、版本管理、公共分析复用和 Web 工作台支撑可追踪、可重跑、可调试的生产流程。**
