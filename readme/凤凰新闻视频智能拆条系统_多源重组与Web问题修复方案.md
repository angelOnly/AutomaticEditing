# 凤凰新闻视频智能拆条系统：多源重组、预览、任务控制、字幕与任务命名修复方案

> 仓库：`angelOnly/AutomaticEditing`  
> 分支：`clean-highlight-reassembly`  
> 入口：`python run_web.py`  
> 本文基于已按路径读取的核心文件：`README.md`、`run_web.py`、`web_app.py`、`config.toml`、`run_pipeline.py`、`run_multisource_pipeline.py`、`newsclip_agent/pipeline.py`、`newsclip_agent/llm_digest.py`、`newsclip_agent/prompts.py`、`web_static/app.js`。

---

## 0. 这几个问题现在是否已经修复？

结论：**没有完全修复**。当前代码里有一部分“看起来已经做过”的修复，但你截图暴露的问题仍然能从代码里找到未闭环的地方。

| 问题 | 当前代码状态 | 是否彻底修复 |
|---|---|---|
| 视频重组多视频任务在 `video_understanding` 超 token | `timeline_digest` 已存在，但高光重组链路没有使用它，仍把完整 `timeline` 给文本 Agent | 未修复 |
| AI 配音解说模式原片预览黑屏/无预览 | Web 端有 `/api/tasks/{task_id}/source-videos` 和 `playSourceVideo()`，但运行中、多源准备阶段、无成片时的预览切换逻辑不够稳 | 部分修复，仍需补 |
| 顶部没有“杀死任务”按钮 | 后端只有 `GET /api/jobs`、`GET /api/jobs/{job_id}`、`GET /api/jobs/{job_id}/log`，没有取消/终止接口；前端也没有按钮 | 未修复 |
| 字幕太大 | `config.toml` 当前 16:9 字幕字号 30、9:16 字号 28，且 `bold=1`、背景框开启 | 未按你的新要求修复 |
| 同一素材不同生产模式，任务名都显示 AI配音解说 | 前后端已有生产模式名和任务 ID 拼接逻辑，但多源运行/运行中 manifest fallback/前端复用旧 taskId 仍可能把错误模式名带入 | 部分修复，仍需加强 |

---

## 1. 问题一：视频重组多视频报错，LLM 输入过长

### 1.1 当前现状

`newsclip_agent/pipeline.py` 中，高光重组模式步骤为：

```python
HIGHLIGHT_REASSEMBLY_STEP_ORDER = [
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "vision",
    "timeline",
    "video_understanding",
    "highlight_detection",
    "highlight_reassembly_plan",
    "reassembly_cut_plan",
    "reassembly_render",
]
```

这里没有 `timeline_digest`。

但 `timeline_digest` 已经在 AI 配音链路里存在，并且会把完整 timeline 压缩成：

```python
{
    "chunk_id": "...",
    "time": "...",
    "start_seconds": ...,
    "end_seconds": ...,
    "speech": "...",
    "visual": "...",
    "screen_text": [...],
    "people": [...],
    "scene": "...",
    "visual_score": ...,
    "hook_score": ...,
    "flags": [...]
}
```

问题是高光重组链路没有用它。当前高光重组三个关键 Agent 仍然直接读取完整 timeline：

```python
def step_video_understanding(self) -> None:
    self._run_text_agent("video_understanding", {
        "merged_timeline": self._load_step_json("timeline"),
    })

def step_highlight_detection(self) -> None:
    self._run_text_agent("highlight_detection", {
        "merged_timeline": self._load_step_json("timeline"),
        "video_analysis": self._load_step_json("video_understanding"),
    })

def step_highlight_reassembly_plan(self) -> None:
    self._run_text_agent("highlight_reassembly_plan", {
        "run_options": self._run_options_payload(),
        "reassembly_options": self._reassembly_options_payload(),
        "timeline": self._load_step_json("timeline"),
        "video_analysis": self._load_step_json("video_understanding"),
        "candidate_clips": self._load_step_json("highlight_detection"),
    })
```

你的日志已经证明实际输入达到了：

```text
LLM input size [video_understanding]: 619781 chars
Total tokens of image and text exceed max message tokens
```

### 1.2 原因

完整 `timeline` 中包含每个 chunk 的：

```python
asr_text
asr_segments
visual_summary
screen_text
visible_people
notes
...
```

其中 `asr_segments` 是最大膨胀源。长视频、多源拼接后，每个 ASR 片段都带时间戳、文本、speaker 等字段，JSON 结构重复非常多。

`_run_text_agent()` 目前只是打印 warning：

```python
def _log_llm_input_size(self, step: str, input_data: dict[str, Any]) -> None:
    text = json.dumps(input_data, ensure_ascii=False)
    print(f"LLM input size [{step}]: {len(text)} chars")
    if len(text) > 10000:
        print(f"warning: {step} input exceeds 10000 chars; check digest compaction")
```

但它不会自动压缩，也不会阻断，所以还是会直接调用大模型。

### 1.3 修改文件

需要改：

```text
newsclip_agent/pipeline.py
newsclip_agent/prompts.py
web_static/app.js
```

### 1.4 具体改法

#### 修改 1：高光重组步骤加入 `timeline_digest`

文件：`newsclip_agent/pipeline.py`

把：

```python
HIGHLIGHT_REASSEMBLY_STEP_ORDER = [
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "vision",
    "timeline",
    "video_understanding",
    "highlight_detection",
    "highlight_reassembly_plan",
    "reassembly_cut_plan",
    "reassembly_render",
]
```

改成：

```python
HIGHLIGHT_REASSEMBLY_STEP_ORDER = [
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "vision",
    "timeline",
    "timeline_digest",
    "video_understanding",
    "highlight_detection",
    "highlight_reassembly_plan",
    "reassembly_cut_plan",
    "reassembly_render",
]
```

`DEPENDENCIES` 里已经有：

```python
"timeline_digest": ["timeline"],
```

所以不用新增依赖。

#### 修改 2：前端步骤列表也加入 `timeline_digest`

文件：`web_static/app.js`

把：

```javascript
const HIGHLIGHT_REASSEMBLY_STEPS = [
  "source_prepare",
  "metadata",
  "audio_extract",
  "frame_extract",
  "chunk_build",
  "asr",
  "vision",
  "timeline",
  "video_understanding",
  "highlight_detection",
  "highlight_reassembly_plan",
  "reassembly_cut_plan",
  "reassembly_render",
];
```

改成：

```javascript
const HIGHLIGHT_REASSEMBLY_STEPS = [
  "source_prepare",
  "metadata",
  "audio_extract",
  "frame_extract",
  "chunk_build",
  "asr",
  "vision",
  "timeline",
  "timeline_digest",
  "video_understanding",
  "highlight_detection",
  "highlight_reassembly_plan",
  "reassembly_cut_plan",
  "reassembly_render",
];
```

并在 `STEP_LABELS` 增加：

```javascript
timeline_digest: "压缩分析时间线",
```

#### 修改 3：新增统一读取 digest 的方法

文件：`newsclip_agent/pipeline.py`

放在 `step_video_understanding()` 前面：

```python
def _load_llm_timeline_digest(self) -> dict[str, Any]:
    """
    文本 Agent 专用压缩时间轴。
    优先读取 timeline_digest；兼容老任务：没有该步骤时，临时从 timeline 构造轻量 digest。
    """
    try:
        return self._load_step_json("timeline_digest")
    except Exception:
        timeline_data = self._load_step_json("timeline")
        timeline = timeline_data.get("timeline", [])
        llm_input_cfg = self.config.raw.get("llm_input", {})

        chunks = []
        for item in timeline:
            chunks.append({
                "chunk_id": item.get("chunk_id", ""),
                "time": f"{item.get('start', '')}-{item.get('end', '')}",
                "start_seconds": item.get("start_seconds", 0),
                "end_seconds": item.get("end_seconds", 0),
                "speech": compact_asr_for_llm(
                    item.get("asr_text", ""),
                    max_chars=int(llm_input_cfg.get("max_asr_chars_per_chunk", 320)),
                ),
                "visual": compact_text(
                    item.get("visual_summary", ""),
                    max_chars=int(llm_input_cfg.get("max_visual_chars_per_chunk", 160)),
                ),
                "screen_text": compact_list(
                    item.get("screen_text", []),
                    max_items=int(llm_input_cfg.get("max_screen_text_items", 5)),
                ),
                "people": compact_list(
                    item.get("visible_people", []),
                    max_items=int(llm_input_cfg.get("max_people_items", 5)),
                ),
                "scene": item.get("scene_type", ""),
                "visual_score": item.get("visual_value_score", 0),
                "hook_score": item.get("hook_score", 0),
                "flags": build_chunk_flags(item),
            })

        max_chunks = int(llm_input_cfg.get("max_digest_chunks_for_text_agent", 120))
        if max_chunks > 0 and len(chunks) > max_chunks:
            chunks = chunks[:max_chunks]

        return {
            "version": "inline_llm_timeline_digest_v1",
            "source_versions": {"timeline": self._step_version("timeline")},
            "video_duration_seconds": max(
                (float(x.get("end_seconds") or 0) for x in chunks),
                default=0.0,
            ),
            "chunks": chunks,
        }
```

#### 修改 4：三个 Agent 全部改用 digest

文件：`newsclip_agent/pipeline.py`

替换为：

```python
def step_video_understanding(self) -> None:
    self._run_text_agent("video_understanding", {
        "timeline_digest": self._load_llm_timeline_digest(),
        "source_mode": self.manifest.get("source_mode", "single_source"),
        "source_videos": self.manifest.get("source_videos", []),
    })


def step_highlight_detection(self) -> None:
    self._run_text_agent("highlight_detection", {
        "timeline_digest": self._load_llm_timeline_digest(),
        "video_analysis": self._load_step_json("video_understanding"),
    })


def step_highlight_reassembly_plan(self) -> None:
    self._run_text_agent("highlight_reassembly_plan", {
        "run_options": self._run_options_payload(),
        "reassembly_options": self._reassembly_options_payload(),
        "timeline_digest": self._load_llm_timeline_digest(),
        "video_analysis": self._load_step_json("video_understanding"),
        "candidate_clips": self._load_step_json("highlight_detection"),
    })
```

#### 修改 5：Prompt 兼容 `timeline_digest`

文件：`newsclip_agent/prompts.py`

在 `VIDEO_UNDERSTANDING_PROMPT` 增加：

```text
输入可能提供 timeline_digest，而不是完整 merged_timeline。
timeline_digest.chunks 是压缩时间轴，每个 chunk 包含：
chunk_id、time、start_seconds、end_seconds、speech、visual、screen_text、people、scene、visual_score、hook_score、flags。
你必须基于这些字段完成新闻主题、事实结构、关键人物、风险和高光方向判断，不要要求完整 ASR 原文。
```

在 `HIGHLIGHT_DETECTION_PROMPT` 增加：

```text
优先读取 timeline_digest.chunks 进行高光识别。
speech 是该 chunk 的压缩语音摘要，visual 是画面摘要。
候选片段的 start/end 必须基于 chunk 的 start_seconds/end_seconds 或相邻 chunk 合并推断。
```

在 `HIGHLIGHT_REASSEMBLY_PROMPT` 增加：

```text
输入使用 timeline_digest 作为时间轴，不提供完整 merged_timeline。
selected_clips 的时间边界必须落在 timeline_digest.chunks 的 start_seconds/end_seconds 范围内。
如果需要合并相邻 chunk，优先合并新闻逻辑连续、speech/visual 连续的 chunk。
```

#### 修改 6：加硬性保护，避免以后再次爆 token

文件：`newsclip_agent/pipeline.py`

在 `_run_text_agent()` 里，`self._log_llm_input_size(step, input_data)` 后面加：

```python
max_input_chars = int(self.config.raw.get("llm_input", {}).get("max_text_agent_input_chars", 60000))
input_chars = len(json.dumps(input_data, ensure_ascii=False))
if input_chars > max_input_chars:
    raise RuntimeError(
        f"{step} LLM 输入过大：{input_chars} chars，超过 max_text_agent_input_chars={max_input_chars}。"
        "请检查是否误传完整 timeline/asr_segments，应改用 timeline_digest。"
    )
```

文件：`config.toml`

新增：

```toml
[llm_input]
max_asr_chars_per_chunk = 240
max_visual_chars_per_chunk = 120
max_screen_text_items = 3
max_people_items = 3
max_digest_chunks_for_text_agent = 120
max_text_agent_input_chars = 60000
```

---

## 2. 问题二：AI 配音解说模式下原片预览没有，视频重组模式能预览

### 2.1 当前现状

前端在没有成片时，会执行：

```javascript
if (drafts.length) {
  playDraft(drafts[0]);
} else {
  playSourceVideo();
  $("fileViewer").textContent = "当前任务还没有生成粗剪视频，正在预览源视频。";
}
```

`playSourceVideo()` 在选中任务时调用：

```javascript
if (state.selectedTask) {
  loadSourceVideos(state.selectedTask);
}
```

`loadSourceVideos()` 读取：

```javascript
/api/tasks/{task_id}/source-videos
```

后端 `list_source_videos()` 会返回：

```python
source = manifest.get("source_video")
if source:
    sources.append({
        "label": "拼接原片" if manifest.get("source_mode") == "multi_source_concat_proxy" else "原片",
        "file": source,
        "url": f"/api/tasks/{task_id}/file?path={source}",
        "source_index": 0,
    })
```

多源任务还会返回 `source_videos` 里的每个原始素材。

### 2.2 问题原因

AI 配音解说模式运行中，尤其是多源任务刚开始时，可能出现：

1. `source_prepare` 还没成功，`manifest.source_video` 为空。
2. `get_manifest()` 的 fallback 里 `source_mode = "multi_source_pending"`，`source_video = ""`，导致 `/source-videos` 返回空列表。
3. 前端 `loadSourceVideos()` 如果 sources 为空，会直接设置 `/api/tasks/{task_id}/preview`，但这个接口要求 `manifest.source_video` 存在；此时可能 404，视频区域就是黑屏。
4. 前端 `playSourceVideo()` 没有 await `loadSourceVideos()`，失败时也没有兜底提示。

所以重组模式能看到，AI 模式看不到，不一定是后端没有文件，而是**运行中预览兜底链路不稳**。

### 2.3 修改文件

```text
web_app.py
web_static/app.js
run_multisource_pipeline.py
```

### 2.4 具体改法

#### 修改 1：source request 阶段也能返回待预览素材

文件：`web_app.py`

修改 `list_source_videos()`，在 `manifest.source_video` 为空时，读取 `input/source_request.json`，返回本地原片或远程 preview_url。

新增辅助函数：

```python
def _source_request_preview_items(task_dir: Path, task_id: str) -> list[dict[str, Any]]:
    request = read_json(task_dir / "input" / "source_request.json", {})
    items = request.get("source_items") or []
    previews: list[dict[str, Any]] = []

    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        source_type = str(item.get("source_type") or item.get("type") or "local")
        if source_type in {"local", "video"}:
            path_value = item.get("path") or item.get("input_video")
            if not path_value:
                continue
            try:
                path = _resolve_input_video(str(path_value))
                previews.append({
                    "label": item.get("display_name") or path.name,
                    "file": relpath(path, ROOT),
                    "url": f"/api/videos/preview?path={relpath(path, ROOT)}",
                    "source_index": index,
                })
            except Exception:
                continue
        elif source_type in {"remote", "remote_ucms"}:
            remote = item.get("remote_video") or item
            if not isinstance(remote, dict):
                continue
            url = remote.get("preview_url") or remote.get("media_low_url") or remote.get("media_high_url") or ""
            previews.append({
                "label": item.get("display_name") or remote.get("display_name") or remote.get("name") or f"远程素材 {index}",
                "file": remote.get("name") or remote.get("remote_id") or "",
                "url": url,
                "source_index": index,
            })

    return previews
```

然后在 `list_source_videos()` 最前面加：

```python
if not manifest.get("source_video"):
    pending_previews = _source_request_preview_items(task_dir, task_id)
    if pending_previews:
        return pending_previews
```

这样即使多源拼接还没完成，Web 也能预览原始素材。

#### 修改 2：前端预览失败时显示原因，而不是黑屏

文件：`web_static/app.js`

修改 `loadSourceVideos()`：

```javascript
async function loadSourceVideos(taskId) {
  const sources = await api(`/api/tasks/${taskId}/source-videos`).catch((error) => {
    $("fileViewer").textContent = `源视频列表加载失败：${cleanError(error)}`;
    return [];
  });

  if (sources.length) {
    const previewItems = renderSourceVideoList(sources);
    playSourceItem(previewItems[0]);
    return;
  }

  const fallbackUrl = `/api/tasks/${taskId}/preview`;
  $("videoPreview").src = fallbackUrl;
  $("videoPreview").load();
  $("fileViewer").textContent = "正在预览原片。";

  $("videoPreview").onerror = () => {
    $("fileViewer").textContent =
      "原片暂时无法预览：任务可能还在准备多源素材，source_video 尚未写入 manifest。请等待 source_prepare 完成后刷新。";
  };
}
```

#### 修改 3：`playSourceVideo()` 改成 async 并 await

文件：`web_static/app.js`

```javascript
async function playSourceVideo() {
  if (state.selectedTask) {
    await loadSourceVideos(state.selectedTask);
  } else if (state.sourceBasket.length) {
    const sources = state.sourceBasket.map(sourceBasketPreviewItem);
    const previewItems = renderSourceVideoList(sources);
    playSourceItem(previewItems[previewItems.length - 1] || previewItems[0]);
  } else if (state.selectedRemoteVideo) {
    const url = state.selectedRemoteVideo.preview_url || state.selectedRemoteVideo.media_low_url || state.selectedRemoteVideo.media_high_url;
    if (url) {
      $("videoPreview").src = url;
      $("videoPreview").load();
      $("fileViewer").textContent = "正在预览远程原片。";
    }
  } else if (state.selectedVideo) {
    $("videoPreview").src = `/api/videos/preview?path=${encodeURIComponent(state.selectedVideo.path)}`;
    $("videoPreview").load();
    $("fileViewer").textContent = "正在预览原片。";
  }
}
```

调用处可以不全部 await，但在 `loadLatestDraft()` 里建议改：

```javascript
await playSourceVideo();
```

---

## 3. 问题三：顶部需要新增“杀死任务”按钮

### 3.1 当前现状

后端现在只有：

```python
@app.get("/api/jobs")
@app.get("/api/jobs/{job_id}")
@app.get("/api/jobs/{job_id}/log")
```

没有 `DELETE /api/jobs/{job_id}` 或 `/api/jobs/{job_id}/cancel`。

前端顶部只有：

```javascript
$("runSelected").addEventListener("click", () => runSelected().catch(handleRunError));
```

没有 stop/cancel 按钮，也没有调用终止接口。

### 3.2 修改文件

```text
web_app.py
web_static/index.html
web_static/app.js
web_static/style.css
```

### 3.3 后端新增取消接口

文件：`web_app.py`

新增 import：

```python
import signal
```

如果 Windows 下要杀子进程树，建议用 `taskkill`。新增函数：

```python
def _terminate_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
```

为了 Linux/macOS 可杀进程组，`Popen` 建议改：

```python
process = subprocess.Popen(
    job["cmd"],
    cwd=str(ROOT),
    stdout=log_file,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="replace",
    env=env,
    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    start_new_session=(os.name != "nt"),
)
```

新增接口：

```python
@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str) -> dict[str, Any]:
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")

    with JOB_LOCK:
        job = JOBS[job_id]
        status = job.get("status")

        if status == "pending":
            try:
                PENDING_JOB_IDS.remove(job_id)
            except ValueError:
                pass
            job["status"] = "cancelled"
            job["returncode"] = -9
            job["finished_at"] = datetime.now().isoformat(timespec="seconds")
            job["user_message"] = "任务已取消。"
            _persist_job(job)
            _schedule_jobs()
            return _public_job(job)

        if status != "running":
            return _public_job(job)

        pid = int(job.get("pid") or 0)
        _terminate_process_tree(pid)

        process = job.get("process")
        if process:
            try:
                process.wait(timeout=3)
            except Exception:
                pass

        log_file = job.get("log_file")
        if log_file:
            try:
                log_file.write("\n=== job cancelled by user ===\n")
                log_file.close()
            except Exception:
                pass

        job["status"] = "cancelled"
        job["returncode"] = -9
        job["finished_at"] = datetime.now().isoformat(timespec="seconds")
        job["user_message"] = "任务已手动终止。"
        _persist_job(job)
        _schedule_jobs()
        return _public_job(job)
```

同时 `STATUS_LABELS` 前端要加 `cancelled`。

### 3.4 前端新增按钮

文件：`web_static/index.html`

在顶部按钮区，`刷新` 和 `开始分析` 旁边加：

```html
<button id="stopJob" class="btn danger hidden" type="button">
  <i data-lucide="square"></i>
  终止任务
</button>
```

文件：`web_static/app.js`

绑定事件：

```javascript
$("stopJob")?.addEventListener("click", stopActiveJob);
```

新增函数：

```javascript
async function stopActiveJob() {
  if (!state.activeJob) {
    alert("当前没有运行中的任务。");
    return;
  }
  if (!confirm("确定要终止当前任务吗？已完成的步骤会保留，后续可从失败/中断步骤重跑。")) return;

  try {
    const job = await api(`/api/jobs/${state.activeJob}`, { method: "DELETE" });
    updateJobMessage(job.user_message || "任务已终止。");
    $("metricJob").textContent = jobStatusLabel(job.status);
    if (state.logTimer) {
      clearInterval(state.logTimer);
      state.logTimer = null;
    }
    state.activeJob = null;
    setRunControlsBusy(false);
    await loadTasks();
    if (job.task_id) {
      await loadManifest(job.task_id).catch(() => {});
      await loadTree().catch(() => {});
    }
  } catch (error) {
    alert(`终止任务失败：${cleanError(error)}`);
  }
}
```

修改 `setRunControlsBusy()`：

```javascript
function setRunControlsBusy(isBusy) {
  ["runSelected", "rerunOne", "rerunFrom"].forEach((id) => {
    const button = $(id);
    if (button) button.disabled = Boolean(isBusy);
  });
  $("stopJob")?.classList.toggle("hidden", !state.activeJob);
}
```

文件：`web_static/style.css`

```css
.btn.danger {
  background: #b1122a;
  color: #fff;
  border-color: #b1122a;
}

.btn.danger:hover {
  filter: brightness(0.95);
}
```

---

## 4. 问题四：字幕太大，希望比原视频字幕还小

### 4.1 当前现状

文件：`config.toml`

当前字幕配置：

```toml
[subtitle]
font_size_16_9 = 30
font_size_9_16 = 28
bold = 1
margin_v_16_9 = 52
margin_v_9_16 = 150
border_style = 3
box_padding_chars = 1
```

`pipeline.py` 中 `_subtitle_force_style()` 会读取这些配置并传给 FFmpeg subtitles filter：

```python
Fontsize={font_size}
Bold={bold}
BorderStyle={border_style}
MarginV={margin_v}
```

所以字幕大，是配置导致的，不是前端问题。

### 4.2 修改文件

```text
config.toml
newsclip_agent/pipeline.py
```

### 4.3 推荐修改

如果目标是“比原视频字幕还小一点”，建议：

```toml
[subtitle]
mode = "sentence"
max_chars_per_line_16_9 = 26
max_chars_per_line_9_16 = 18
max_lines = 1
min_cue_seconds = 1.0
max_cue_seconds = 4.0
prefer_clause_split = true

font_name = "Microsoft YaHei"
font_size_16_9 = 20
font_size_9_16 = 22
bold = 0

margin_v_16_9 = 38
margin_v_9_16 = 120

primary_colour = "&H00FFFFFF"
outline_colour = "&H00000000"
back_colour = "&H70000000"
border_style = 3
outline = 1
shadow = 0
blur = 0
box_padding_chars = 0
```

说明：

- 16:9 用 20，比现在 30 小很多。
- 9:16 用 22，因为竖屏画面高，太小会读不清。
- `bold=0`，避免视觉上更大。
- `box_padding_chars=0`，减少背景框宽度。
- `margin_v_16_9=38`，让字幕靠底部一点，不压住原片已有字幕太多。
- 如果原片本身底部有字幕，AI 字幕可能仍然遮挡，后续可以加 `subtitle_position = "upper"` 或 `margin_v` 动态策略。

---

## 5. 问题五：任务名不对，同一数据源一个是视频重组，一个是 AI 配音，但都叫 AI配音解说

### 5.1 当前现状

前端已经有：

```javascript
const PRODUCTION_MODE_NAMES = {
  ai_voiceover: "AI配音解说",
  highlight_reassembly: "视频重组",
};
```

并且 `suggestedTaskId()` 会拼：

```javascript
return `${base}_${mode}_${stamp}`;
```

后端也有：

```python
def _mode_slug(production_mode: str) -> str:
    return "视频重组" if production_mode == "highlight_reassembly" else "AI配音解说"
```

按当前代码，理论上新任务应该能区分模式。

但你的截图里，两个任务都叫：

```text
多源剪辑_2段_AI配音解说_...
```

说明还有一种情况没有兜住：**用户界面里的 `taskIdInput` 已经有旧的 AI 配音任务名，切换生产模式后没有强制改，或者本地服务跑的还是旧代码。**

另外，`get_manifest()` 的 source_request fallback 里仍然硬编码：

```python
"last_web_run_options": {
    "production_mode": "ai_voiceover",
    ...
}
```

这会导致多源任务在 manifest 还没生成完整前，页面恢复控制时误判为 AI 配音。

### 5.2 修改文件

```text
web_app.py
web_static/app.js
run_multisource_pipeline.py
```

### 5.3 后端强制修正任务 ID

文件：`web_app.py`

在 `run_pipeline(req)` 里，算出 task_id 后，无论前端传了什么，都做一次：

```python
task_id = _with_mode_suffix(task_id, req.production_mode)
task_dir = ensure_dir(OUTPUTS_DIR / task_id)
```

建议检查这几个分支都要调用：

```python
if len(raw_source_items) > 1:
    ...
    task_id = _normalize_new_task_id(req.task_id, input_name_for_task, req.production_mode, fingerprint)
    task_id = _with_mode_suffix(task_id, req.production_mode)

elif source_items:
    ...
    task_id = ...
    task_id = _with_mode_suffix(task_id, req.production_mode)

elif not raw_source_items:
    ...
    task_id = ...
    task_id = _with_mode_suffix(task_id, req.production_mode)
```

当前 `_with_mode_suffix()` 已经具备替换旧 alias 的能力：

```python
if marker in task_id:
    return task_id.replace(marker, f"_{mode}_", 1)
```

但要确保所有入口都调用它。

### 5.4 source_request 保存 production_mode

文件：`web_app.py`

写 `source_request.json` 时，现在保存了 output/mode/时长等参数，但需要明确保存：

```python
"production_mode": req.production_mode,
"task_id": task_id,
```

修改：

```python
write_json(
    source_request_path,
    {
        "task_id": task_id,
        "production_mode": req.production_mode,
        "source_items": raw_source_items,
        "force_remote_download": req.force_remote_download,
        "output_mode": req.output_mode,
        "max_output_videos": req.max_output_videos,
        "min_output_video_seconds": req.min_output_video_seconds,
        "max_output_video_seconds": req.max_output_video_seconds,
        "aspect_ratio": req.aspect_ratio,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    },
)
```

### 5.5 修复 `get_manifest()` fallback 的硬编码

文件：`web_app.py`

把：

```python
"last_web_run_options": {
    "production_mode": "ai_voiceover",
    "output_mode": source_request.get("output_mode", "single"),
    ...
}
```

改成：

```python
"last_web_run_options": {
    "production_mode": source_request.get("production_mode", _task_id_production_mode(task_id) or "ai_voiceover"),
    "output_mode": source_request.get("output_mode", "single"),
    "max_output_videos": source_request.get("max_output_videos", 1),
    "aspect_ratio": source_request.get("aspect_ratio", "16:9"),
}
```

### 5.6 `run_multisource_pipeline.py` 保留 production_mode 到 manifest

文件：`run_multisource_pipeline.py`

`_mark_source_prepare()` 中加入：

```python
production_mode = request.get("production_mode")
if production_mode in {"ai_voiceover", "highlight_reassembly"}:
    manifest.setdefault("last_web_run_options", {})
    manifest["last_web_run_options"]["production_mode"] = production_mode
    manifest["production_mode"] = production_mode
```

这样任务运行中，前端也能正确显示“视频重组”。

### 5.7 前端切换生产模式时强制更新自动任务名

文件：`web_static/app.js`

当前逻辑：

```javascript
if (!value || isAutoTaskIdForVideo(value, selectedName)) {
  input.value = suggestedTaskId(selectedName, $("productionMode").value);
}
```

建议增强：只要当前输入包含任意生产模式别名，且当前不是用户手写自定义名，就替换。

新增：

```javascript
function hasModeAlias(value) {
  return /_(AI配音解说|视频重组|ai_voiceover|highlight_reassembly)_/.test(value)
    || /_(AI配音解说|视频重组|ai_voiceover|highlight_reassembly)$/.test(value);
}
```

修改 `syncSuggestedTaskIdForMode()`：

```javascript
function syncSuggestedTaskIdForMode() {
  const selectedName = currentSuggestedTaskName();
  if (!selectedName || state.selectedTask) return;

  const input = $("taskIdInput");
  const value = input.value.trim();
  const shouldReplace =
    !value ||
    isAutoTaskIdForVideo(value, selectedName) ||
    hasModeAlias(value);

  if (shouldReplace) {
    input.value = suggestedTaskId(selectedName, $("productionMode").value);
    updateTaskSubtitle();
  }
}
```

---

## 6. Web 启动兼容性

这些改动不改变入口。

仍然是：

```bash
python run_web.py
```

`run_web.py` 只负责找空闲端口并启动：

```python
uvicorn.run("web_app:app", host=host, port=port, reload=False)
```

所以 Web 启动方式不受影响。

---

## 7. 修改后怎么重跑你当前失败任务

你这个失败任务前面步骤已经跑到 timeline 了，失败在 `video_understanding`。

代码改完后，推荐重跑：

```bash
python run_multisource_pipeline.py ^
  --task-id 多源剪辑_2段_AI配音解说_20260602_115334 ^
  --source-request outputs\多源剪辑_2段_AI配音解说_20260602_115334\input\source_request.json ^
  --production-mode highlight_reassembly ^
  --audio-policy original ^
  --skip-tts ^
  --no-require-tts ^
  --allow-long-video ^
  --output-mode multiple ^
  --max-output-videos 5 ^
  --min-output-video-seconds 30 ^
  --max-output-video-seconds 90 ^
  --reassembly-output-mode multiple ^
  --reassembly-sort-mode editorial ^
  --reassembly-max-clip-count 8 ^
  --reassembly-target-seconds 90 ^
  --rerun-from timeline_digest
```

如果 Web 页面已经支持 `timeline_digest` 这个步骤，可以直接选“从压缩分析时间线开始重跑”。

---

## 8. 建议按这个顺序改

第一批必须改：

```text
1. pipeline.py：高光重组链路加入 timeline_digest
2. pipeline.py：三个文本 Agent 改用 timeline_digest
3. prompts.py：Prompt 兼容 timeline_digest
4. web_static/app.js：高光重组步骤列表加入 timeline_digest
```

这能解决你现在的视频重组报错。

第二批改体验问题：

```text
5. web_app.py + app.js：修复 AI 配音模式运行中原片预览
6. web_app.py + app.js + index.html：新增终止任务按钮
7. config.toml：缩小字幕
8. web_app.py + run_multisource_pipeline.py + app.js：强制任务名按生产模式纠正
```

---

## 9. 验收标准

### 9.1 LLM 输入压缩

运行日志中应该看到：

```text
完成: timeline_digest
LLM input size [video_understanding]: < 60000 chars
```

不应该再出现：

```text
Total tokens of image and text exceed max message tokens
```

### 9.2 原片预览

AI 配音解说模式运行中，即使还没生成成片，也应该：

- 能预览拼接原片，或
- 能预览 source_request 中的原始素材，或
- 明确显示“source_prepare 还在准备，暂时无法预览”，不能黑屏无提示。

### 9.3 终止任务

点击“终止任务”后：

- 运行中 job 状态变为 `cancelled`
- 日志写入 `=== job cancelled by user ===`
- 按钮恢复可点击
- 可从失败/中断步骤重跑

### 9.4 字幕

AI 配音解说成片字幕：

- 字号明显小于当前版本
- 不加粗
- 背景框更窄
- 一行字幕不超过配置字符数

### 9.5 任务名

同一批素材分别创建两个模式时，任务名必须是：

```text
多源剪辑_2段_AI配音解说_xxx
多源剪辑_2段_视频重组_xxx
```

不能两个都叫 AI配音解说。

---

# 10. 新增问题：AI 配音解说多视频模式下，AI 不应把“最多 N 条”理解成“必须生成 N 条”

> 本节是在前面方案基础上的新增补充。前文所有方案保持不变，本节只新增“AI 配音解说模式下，多视频数量自动判断与过度拆分修复”的优化方案。  
> 触发场景：用户选择 2 个视频源，AI 配音解说模式，输出方式为“生成多个视频”，最多成片数为 4，但系统生成到 `voiceover_script` 后报错：多条视频解说文案太短，无法满足目标时长。

---

## 10.1 现象

用户本次运行命令为：

```bash
C:\Users\13222\anaconda3\envs\comfy_5090_313_auto\python.exe -u E:\ai\skills\AutomaticEditing\run_multisource_pipeline.py ^
  --task-id 多源剪辑_2段_AI配音解说_20260602_124755 ^
  --chunk-seconds 60 ^
  --frame-interval 10 ^
  --aspect-ratio 16:9 ^
  --target-duration 30 ^
  --audio-policy ai_voiceover ^
  --production-mode ai_voiceover ^
  --output-mode multiple ^
  --max-output-videos 4 ^
  --min-output-video-seconds 30 ^
  --max-output-video-seconds 90 ^
  --voice-id default_female ^
  --source-request E:\ai\skills\AutomaticEditing\outputs\多源剪辑_2段_AI配音解说_20260602_124755\input\source_request.json ^
  --mode normal ^
  --allow-long-video ^
  --require-tts
```

日志显示前面步骤已成功：

```text
完成: timeline_digest
LLM input size [content_analysis]: 20810 chars
完成: content_analysis
LLM input size [short_video_edit_plan]: 27809 chars
完成: short_video_edit_plan
LLM input size [voiceover_script]: 21132 chars
完成: voiceover_script
```

失败发生在 `voiceover_script` 后、TTS 前：

```text
运行失败：
AI voiceover script does not satisfy target duration requirements; blocked before TTS/cut/render:
- sv_002 AI voiceover narration is severely too short: target 55s requires hard minimum 137 chars, got 131.
- sv_002 duration_fit=too_short; rerun with richer narration or merge more source clips.
- sv_003 duration_fit=too_short; rerun with richer narration or merge more source clips.
- sv_004 AI voiceover narration is severely too short: target 45s requires hard minimum 112 chars, got 100.
- sv_004 duration_fit=too_short; rerun with richer narration or merge more source clips.
```

---

## 10.2 用户侧真实诉求

用户选择的是：

```text
输出方式：生成多个视频
最多成片数：4
```

这句话的正确产品语义应该是：

```text
最多 4 条 = 上限，不是必须生成 4 条。
```

用户不应该提前知道素材到底适合生成几条，也不应该手动猜“最多 2 条”还是“最多 4 条”。

正确逻辑应该是：

```text
用户只给上限：最多 4 条
AI 自己判断：适合 1 条、2 条、3 条还是 4 条
系统兜底校验：如果某条内容撑不住，就自动合并、减少条数、缩短目标时长，而不是直接报错
```

所以本问题的核心不是“用户应该把最多 4 条改成最多 2 条”，而是：

```text
系统必须把 max_output_videos 理解为上限。
AI 不能为了接近 max_output_videos 强拆。
后处理必须能发现过度拆分，并自动修复。
```

---

## 10.3 当前代码问题

### 10.3.1 `max_output_videos` 目前被模型误用为“尽量生成这么多条”

在 `web_static/app.js` 中，前端请求会传：

```javascript
max_output_videos: outputMode === "multiple" ? Number($("maxOutputVideos")?.value || 5) : 1,
```

在 `web_app.py` 中，命令构建会传：

```python
"--output-mode", req.output_mode,
"--max-output-videos", str(req.max_output_videos),
```

在 `pipeline.py` 的 `_run_options_payload()` 中，会把这些参数给 LLM：

```python
return {
    "production_mode": self.options.production_mode,
    "output_mode": self.options.output_mode,
    "max_output_videos": self.options.max_output_videos,
    ...
}
```

Prompt 里虽然写了 `output_mode == "multiple" 时，可以输出 2 到 max_output_videos 条视频`，但模型仍可能倾向于多拆，因为它看到 max=4，就尽量给 4 条。

### 10.3.2 短视频规划后缺少“过度拆分修复”

当前流程：

```text
content_analysis
short_video_edit_plan
voiceover_script
tts
subtitles
cut_plan
render
```

在 `short_video_edit_plan` 输出多条视频后，系统没有做如下判断：

```text
这些视频是否真的都能独立成片？
每条信息量是否足够支撑 30-60 秒？
是否出现 3-4 条都很短、很碎、需要合并的情况？
```

因此模型一旦强拆出 4 条，后续 `voiceover_script` 就会被迫给 4 条都写解说。某些条目事实点不足，就会写得很短。

### 10.3.3 `voiceover_script` 校验只会阻断，不会自动修复

当前 `_validate_voiceover_script_duration_or_raise()` 里有硬规则：

```python
hard_min_chars = int(target * 2.5)
soft_min_chars = int(target * 3.0)

if target >= 40 and actual_chars < hard_min_chars:
    hard_issues.append(...)

if str(script.get("duration_fit") or "") == "too_short":
    hard_issues.append(...)
```

这导致两种问题：

1. `sv_002` 这种只差 6 个字的轻微不足，也会被阻断。
2. `duration_fit=too_short` 被一律当硬错误，没有分级处理。
3. 系统不会尝试自动补一句背景/影响/结尾，也不会自动降低目标时长。
4. 更不会回退到短视频规划阶段进行合并。

---

## 10.4 根因分析

这次不是 LLM 输入超长，也不是 TTS 失败，更不是 FFmpeg 渲染失败。

根因是：

```text
AI 规划阶段过度拆分
+
voiceover 文案没有写够目标时长
+
系统后处理没有自动合并/降级/补写
+
校验逻辑过硬，直接阻断
```

具体表现为：

```text
sv_002 target 55s，但 narration_text 只有 131 字
sv_003 duration_fit=too_short
sv_004 target 45s，但 narration_text 只有 100 字
```

这说明：

```text
模型把素材拆成了 4 条，但后 2-3 条的信息密度不足以支撑 45-55 秒 AI 解说视频。
```

正确处理不是让用户手动把最多条数改成 2，而是系统自动判断：

```text
这批素材最多允许 4 条，但实际只适合 1-2 条。
```

---

## 10.5 目标效果

修改后，多视频 AI 配音解说模式应该满足：

```text
用户设置：最多 4 条
系统允许：输出 1、2、3、4 条
AI 判断：根据素材自动决定实际条数
系统兜底：发现强拆则自动合并或减少条数
最终结果：不因为某几条太短直接失败
```

最终正确流程：

```text
用户选择最多 4 条
        ↓
AI 第一次规划可能给出 4 条
        ↓
系统检查每条视频的信息密度和文案可支撑度
        ↓
发现 sv_002 / sv_003 / sv_004 太短
        ↓
自动判断为过度拆分
        ↓
合并成 1-2 条，或缩短部分目标时长
        ↓
重新生成 voiceover_script
        ↓
TTS → 字幕 → cut_plan → render
```

---

## 10.6 需要修改的文件

本节新增优化涉及以下文件：

```text
newsclip_agent/prompts.py
newsclip_agent/pipeline.py
newsclip_agent/duration_policy.py
config.toml
web_static/app.js
web_static/index.html
```

其中与前文已有方案冲突或重叠的文件：

```text
newsclip_agent/pipeline.py
newsclip_agent/prompts.py
config.toml
web_static/app.js
```

处理方式：**不要覆盖前文修改，只在对应文件中追加本节新增逻辑。**

---

## 10.7 修改一：明确 `max_output_videos` 是上限，不是目标数量

### 文件

```text
newsclip_agent/prompts.py
```

### 修改位置

在多视频相关 prompt 中补充规则，尤其是：

```python
MULTI_OUTPUT_PROGRAM_SPLIT_POLICY
SHORT_VIDEO_PLANNER_PROMPT
```

### 建议替换/增强 `MULTI_OUTPUT_PROGRAM_SPLIT_POLICY`

把原有策略保留，在后面追加：

```text
数量上限解释：
1. max_output_videos 是“最多允许输出的视频数量”，不是目标数量，也不是必须输出数量。
2. output_mode == "multiple" 时，你可以输出 1 到 max_output_videos 条；只有当每条都具备独立新闻主题、足够事实密度、足够画面支撑和完整叙事时，才输出多条。
3. 不得为了接近 max_output_videos 强行拆分。
4. 如果一条视频的事实点、画面证据或上下文不足以支撑至少 min_output_video_seconds，则必须合并到相邻主题，或不输出该条。
5. 如果多个候选片段属于同一新闻链条，优先合并为一条完整视频，而不是拆成多个短摘要。
6. 输出中必须给出 actual_output_count_reason，说明为什么最终输出这个数量。
```

### 在 `SHORT_VIDEO_PLANNER_PROMPT` 输出 JSON 中新增字段

原结构：

```json
{
  "recommended_video_count": 0,
  "overall_reason": "",
  "short_videos": []
}
```

建议改为：

```json
{
  "requested_max_output_videos": 4,
  "recommended_video_count": 0,
  "actual_output_count_reason": "",
  "over_split_risk": "low / medium / high",
  "merge_decision": {
    "should_merge": false,
    "reason": "",
    "merged_from": [],
    "merged_to_count": 0
  },
  "short_videos": []
}
```

每个 `short_videos[]` 里新增：

```json
{
  "can_stand_alone": true,
  "standalone_reason": "",
  "fact_density_score": 0,
  "visual_support_score": 0,
  "estimated_voiceover_char_capacity": 0,
  "merge_if_weak_with": ""
}
```

---

## 10.8 修改二：在 `short_video_edit_plan` 后增加“过度拆分修复”

### 文件

```text
newsclip_agent/pipeline.py
```

### 目标

在 `short_video_edit_plan` 完成后、`voiceover_script` 之前，增加一层自动修复：

```text
如果 AI 规划了 3-4 条，但多条视频信息量不足，则自动合并或减少条数。
```

### 新增方法一：判断弱视频

放到 `PipelineRunner` 类中：

```python
def _is_weak_short_video_plan_item(self, item: dict[str, Any]) -> bool:
    target = float(
        item.get("target_duration_seconds")
        or item.get("recommended_duration_seconds")
        or self.options.target_duration_seconds
        or self.duration_settings.default_target_seconds
        or 30
    )
    source_clip_ids = item.get("source_clip_ids") or []
    fact_points = item.get("must_keep_fact_points") or []
    optional_facts = item.get("optional_fact_points") or []
    can_stand_alone = item.get("can_stand_alone", True)
    fact_density_score = float(item.get("fact_density_score") or 0)
    visual_support_score = float(item.get("visual_support_score") or 0)

    if can_stand_alone is False:
        return True

    if target >= 40 and len(source_clip_ids) <= 1 and len(fact_points) <= 2:
        return True

    if target >= 40 and fact_density_score > 0 and fact_density_score < 5:
        return True

    if target >= 40 and visual_support_score > 0 and visual_support_score < 5:
        return True

    if target >= 45 and len(fact_points) + len(optional_facts) <= 2:
        return True

    return False
```

### 新增方法二：合并过度拆分的视频

```python
def _repair_over_split_short_video_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return plan

    videos = plan.get("short_videos") or plan.get("scripts") or []
    if not isinstance(videos, list) or len(videos) <= 2:
        return plan

    weak_indexes = []
    for idx, item in enumerate(videos):
        if isinstance(item, dict) and self._is_weak_short_video_plan_item(item):
            weak_indexes.append(idx)

    # 3 条以上，且至少 2 条偏弱，判定为过度拆分
    if len(videos) >= 3 and len(weak_indexes) >= 2:
        merged = self._merge_short_video_plan_items(videos, max_count=2)
        repaired = dict(plan)
        repaired["short_videos"] = merged
        repaired["recommended_video_count"] = len(merged)
        repaired["actual_output_count_reason"] = (
            f"原计划输出 {len(videos)} 条，但检测到 {len(weak_indexes)} 条信息密度不足，"
            f"已自动合并为 {len(merged)} 条，避免为了 max_output_videos 强拆。"
        )
        repaired["over_split_repaired"] = True
        repaired["over_split_original_count"] = len(videos)
        repaired["over_split_weak_indexes"] = weak_indexes
        return repaired

    return plan
```

### 新增方法三：合并具体字段

```python
def _merge_short_video_plan_items(self, videos: list[dict[str, Any]], max_count: int = 2) -> list[dict[str, Any]]:
    valid = [v for v in videos if isinstance(v, dict)]
    if len(valid) <= max_count:
        return valid

    groups: list[list[dict[str, Any]]] = [[] for _ in range(max_count)]
    for idx, item in enumerate(valid):
        groups[idx % max_count].append(item)

    merged_items: list[dict[str, Any]] = []
    for group_idx, group in enumerate(groups, start=1):
        if not group:
            continue

        first = group[0]
        source_clip_ids: list[str] = []
        must_keep_fact_points: list[str] = []
        optional_fact_points: list[str] = []
        topics: list[str] = []
        angles: list[str] = []

        for item in group:
            for clip_id in item.get("source_clip_ids", []) or []:
                if clip_id and clip_id not in source_clip_ids:
                    source_clip_ids.append(clip_id)
            for fact in item.get("must_keep_fact_points", []) or []:
                text = str(fact).strip()
                if text and text not in must_keep_fact_points:
                    must_keep_fact_points.append(text)
            for fact in item.get("optional_fact_points", []) or []:
                text = str(fact).strip()
                if text and text not in optional_fact_points:
                    optional_fact_points.append(text)
            if item.get("topic"):
                topics.append(str(item.get("topic")))
            if item.get("news_angle"):
                angles.append(str(item.get("news_angle")))

        target = min(
            max(
                sum(float(x.get("target_duration_seconds") or 30) for x in group),
                self.options.min_output_video_seconds,
            ),
            self.options.max_output_video_seconds,
            self.duration_settings.complex_max_seconds if self.options.allow_long_video else self.duration_settings.hard_max_without_confirmation,
        )

        merged_items.append({
            **first,
            "short_video_id": f"sv_{group_idx:03d}",
            "topic": " / ".join(topics[:2]) or first.get("topic", ""),
            "news_angle": "；".join(angles[:3]) or first.get("news_angle", ""),
            "target_duration_seconds": round(target, 1),
            "max_allowed_seconds": min(round(target + 8, 1), self.options.max_output_video_seconds),
            "source_clip_ids": source_clip_ids,
            "must_keep_fact_points": must_keep_fact_points,
            "optional_fact_points": optional_fact_points,
            "can_stand_alone": True,
            "merge_reason": f"由 {len(group)} 条弱拆分视频自动合并，避免信息密度不足。",
            "merged_from_short_video_ids": [x.get("short_video_id", "") for x in group],
        })

    return merged_items
```

### 在 `step_short_video_edit_plan()` 中调用

找到生成 `short_video_edit_plan` 的地方，逻辑类似：

```python
final_output = self._run_text_agent("short_video_edit_plan", input_data)
```

之后追加：

```python
final_output = self._repair_over_split_short_video_plan(final_output)

# 如果修复过，需要覆盖该步骤输出文件，保证后续 voiceover_script 读取修复后的版本
if final_output.get("over_split_repaired"):
    out = self.task_dir / self._step_output("short_video_edit_plan")
    write_json(out, final_output)
```

如果当前 `step_short_video_edit_plan()` 没有单独函数，而是通用 `_run_text_agent()`，则在对应 step 方法里包一层，不要直接裸调用 `_run_text_agent()`。

---

## 10.9 修改三：voiceover_script 轻微不足自动补写，不直接失败

### 文件

```text
newsclip_agent/pipeline.py
```

### 当前问题

当前逻辑只要：

```python
duration_fit == "too_short"
```

就直接 hard block。

### 新增方法一：自动补一句

```python
def _auto_expand_short_voiceover_scripts(self, payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False

    scripts = payload.get("scripts", [])
    if not isinstance(scripts, list):
        return False

    changed = False
    for script in scripts:
        if not isinstance(script, dict):
            continue

        target = float(script.get("target_duration_seconds") or 0)
        if target < 40:
            continue

        narration = clean_voiceover_text(script.get("narration_text"))
        actual_chars = len(narration)
        hard_min = int(target * 2.5)

        if actual_chars >= hard_min:
            continue

        shortage = hard_min - actual_chars

        # 只对轻微不足自动补写；严重不足交给合并/重写
        if shortage <= 20:
            addition = self._voiceover_auto_extension_sentence(script)
            script["narration_text"] = narration + addition
            script["ending_sentence"] = addition
            script.setdefault("auto_repair_notes", [])
            script["auto_repair_notes"].append(
                f"narration_text shorter than hard minimum by {shortage} chars; auto appended ending/context sentence."
            )
            changed = True

    return changed
```

### 新增方法二：生成补写句

```python
def _voiceover_auto_extension_sentence(self, script: dict[str, Any]) -> str:
    topic = str(script.get("topic") or script.get("title") or "").strip()
    angle = str(script.get("news_angle") or "").strip()

    if "日本" in topic or "日本" in angle:
        return "这一调整后续如何影响地区安全格局，仍有待持续观察。"
    if "外交" in topic or "会谈" in angle:
        return "相关表态接下来能否转化为实际行动，仍是外界关注焦点。"
    if "冲突" in topic or "军事" in angle or "防卫" in angle:
        return "局势后续是否继续升温，也将牵动周边各方判断。"
    return "这一变化的后续影响，仍有待相关各方进一步观察。"
```

### 在 `step_voiceover_script()` 中调用

当前逻辑大概率是：

```python
final_output = self._run_text_agent("voiceover_script", input_data)
self._normalize_voiceover_scripts(final_output)
self._validate_voiceover_script_duration_or_raise(final_output)
```

改成：

```python
final_output = self._run_text_agent("voiceover_script", input_data)
self._normalize_voiceover_scripts(final_output)

if self._auto_expand_short_voiceover_scripts(final_output):
    self._normalize_voiceover_scripts(final_output)
    out = self.task_dir / self._step_output("voiceover_script")
    write_json(out, final_output)

self._validate_voiceover_script_duration_or_raise(final_output)
```

---

## 10.10 修改四：`duration_fit=too_short` 分级处理

### 文件

```text
newsclip_agent/pipeline.py
```

### 修改 `_validate_voiceover_script_duration_or_raise()`

把当前这一段：

```python
if str(script.get("duration_fit") or "") == "too_short":
    hard_issues.append(f"{short_video_id} duration_fit=too_short; rerun with richer narration or merge more source clips.")
```

替换为：

```python
fit = str(script.get("duration_fit") or "")
if fit == "too_short":
    min_char_count = int(script.get("min_char_count") or 0)
    shortage = max(0, min_char_count - actual_chars)
    shortage_ratio = shortage / max(min_char_count, 1)

    if shortage_ratio <= 0.10:
        soft_warnings.append(
            f"{short_video_id} duration_fit=too_short but only slightly short; "
            f"actual {actual_chars}, min {min_char_count}, shortage {shortage}. "
            "Allowing render with compact duration."
        )
    elif shortage_ratio <= 0.25:
        soft_warnings.append(
            f"{short_video_id} duration_fit=too_short; target may be reduced in cut_plan. "
            f"actual {actual_chars}, min {min_char_count}, shortage {shortage}."
        )
        script.setdefault("auto_repair_notes", [])
        script["auto_repair_notes"].append(
            "duration_fit too_short but not severe; downstream cut_plan should compact video duration."
        )
    else:
        hard_issues.append(
            f"{short_video_id} duration_fit=too_short; actual {actual_chars}, "
            f"min {min_char_count}, shortage_ratio {shortage_ratio:.0%}. "
            "Rerun with richer narration or merge more source clips."
        )
```

这样：

```text
只差几个字：warning，继续
差 10%-25%：warning，后续压缩目标时长
差太多：hard block
```

---

## 10.11 修改五：让 cut_plan 可根据实际配音时长压缩目标时长

### 文件

```text
newsclip_agent/pipeline.py
```

### 目标

如果 voiceover 文案略短，不应该失败，而是：

```text
目标 55 秒
实际 TTS 约 42 秒
cut_plan 自动把画面压缩到 42-45 秒
```

### 建议策略

在构建 cut_plan 时，如果已有：

```python
voiceover_duration_seconds
target_duration_seconds
duration_fit
```

则执行：

```python
effective_target = target_duration_seconds
if duration_fit == "too_short" and voiceover_duration > 0:
    effective_target = max(
        self.options.min_output_video_seconds,
        min(target_duration_seconds, voiceover_duration + 3.0),
    )
```

输出中保留：

```json
{
  "target_duration_seconds": 55,
  "effective_target_duration_seconds": 44,
  "target_duration_adjusted_reason": "voiceover shorter than planned; compacted to actual voiceover duration + buffer"
}
```

这样前端和调试文件里能看出：

```text
原计划 55 秒，但配音只有 41 秒，所以实际按 44 秒剪。
```

---

## 10.12 修改六：配置化，不要把阈值写死

### 文件

```text
config.toml
newsclip_agent/duration_policy.py
```

### `config.toml` 新增

```toml
[voiceover]
# 多视频模式下，是否允许系统自动合并过度拆分的短视频规划
auto_repair_over_split = true

# 多视频模式下，如果 3 条以上视频中有至少 2 条弱视频，则判定为过度拆分
over_split_min_video_count = 3
over_split_min_weak_count = 2

# 轻微文案不足时允许自动补写
auto_expand_short_voiceover = true
auto_expand_max_missing_chars = 20

# duration_fit=too_short 分级阈值
duration_fit_soft_shortage_ratio = 0.10
duration_fit_medium_shortage_ratio = 0.25

# 文案偏短时，是否允许 cut_plan 按实际配音时长压缩目标时长
allow_compact_target_to_voiceover = true
voiceover_compact_buffer_seconds = 3.0
```

### `duration_policy.py` 的 `DurationSettings` 增加字段

```python
auto_repair_over_split: bool = True
over_split_min_video_count: int = 3
over_split_min_weak_count: int = 2
auto_expand_short_voiceover: bool = True
auto_expand_max_missing_chars: int = 20
duration_fit_soft_shortage_ratio: float = 0.10
duration_fit_medium_shortage_ratio: float = 0.25
allow_compact_target_to_voiceover: bool = True
voiceover_compact_buffer_seconds: float = 3.0
```

在 `load_duration_settings()` 里读取：

```python
auto_repair_over_split=bool(voiceover.get("auto_repair_over_split", True)),
over_split_min_video_count=int(voiceover.get("over_split_min_video_count", 3)),
over_split_min_weak_count=int(voiceover.get("over_split_min_weak_count", 2)),
auto_expand_short_voiceover=bool(voiceover.get("auto_expand_short_voiceover", True)),
auto_expand_max_missing_chars=int(voiceover.get("auto_expand_max_missing_chars", 20)),
duration_fit_soft_shortage_ratio=float(voiceover.get("duration_fit_soft_shortage_ratio", 0.10)),
duration_fit_medium_shortage_ratio=float(voiceover.get("duration_fit_medium_shortage_ratio", 0.25)),
allow_compact_target_to_voiceover=bool(voiceover.get("allow_compact_target_to_voiceover", True)),
voiceover_compact_buffer_seconds=float(voiceover.get("voiceover_compact_buffer_seconds", 3.0)),
```

---

## 10.13 修改七：前端文案解释清楚“最多成片数”的含义

### 文件

```text
web_static/index.html
web_static/app.js
```

### UI 文案建议

把当前：

```text
最多成片数
```

改成：

```text
最多成片数（上限）
```

说明文字加：

```text
AI 会自动判断实际生成 1-N 条，不会强制生成满这个数量。
```

### 前端新增提示

在 `maxOutputVideosWrap` 下方加：

```html
<small class="field-help">
  这是数量上限，不是必须生成数量。AI 会根据素材信息密度自动决定实际条数。
</small>
```

---

## 10.14 与前文方案的冲突文件补充说明

### `newsclip_agent/pipeline.py`

前文已经要求修改：

```text
高光重组链路加入 timeline_digest
三个高光重组 Agent 改用 timeline_digest
新增任务终止无关逻辑
```

本节新增：

```text
AI 配音多视频规划后增加过度拆分修复
voiceover_script 增加轻微不足自动补写
duration_fit=too_short 改为分级处理
cut_plan 根据实际 voiceover 时长压缩目标时长
```

处理方式：

```text
不要覆盖前文函数。
新增函数放在 PipelineRunner 类里。
只修改 voiceover_script / short_video_edit_plan / cut_plan 相关方法。
```

### `newsclip_agent/prompts.py`

前文已经要求：

```text
高光重组 prompt 兼容 timeline_digest
```

本节新增：

```text
MULTI_OUTPUT_PROGRAM_SPLIT_POLICY 强调 max_output_videos 是上限
SHORT_VIDEO_PLANNER_PROMPT 输出 actual_output_count_reason / over_split_risk
VOICEOVER_PROMPT 禁止 duration_fit=too_short，要求不足时补写或下调目标时长
```

处理方式：

```text
在原 prompt 后追加规则，不删除前文 timeline_digest 兼容说明。
```

### `config.toml`

前文已经要求：

```text
llm_input 压缩配置
subtitle 字幕缩小配置
```

本节新增：

```text
voiceover 自动合并/自动补写/duration_fit 分级阈值
```

处理方式：

```text
在 [voiceover] 下追加新增配置，不覆盖 [llm_input] 和 [subtitle]。
```

### `web_static/app.js`

前文已经要求：

```text
高光重组步骤列表加入 timeline_digest
新增终止任务按钮逻辑
修复任务名同步
修复原片预览
```

本节新增：

```text
最多成片数 UI 文案解释
```

处理方式：

```text
只新增字段帮助文案，不影响前文按钮/预览/任务名逻辑。
```

---

## 10.15 修改后的验收标准

### 场景一：用户设置最多 4 条，但素材只适合 2 条

输入：

```text
output_mode = multiple
max_output_videos = 4
```

期望：

```text
short_video_edit_plan 输出 recommended_video_count = 2
actual_output_count_reason 说明为什么只输出 2 条
后续 voiceover_script 不再为 4 条分别生成过短文案
```

### 场景二：AI 初始规划 4 条，但后处理发现过度拆分

期望：

```text
short_video_edit_plan.json 中出现：
over_split_repaired = true
over_split_original_count = 4
recommended_video_count = 2
```

日志出现：

```text
检测到过度拆分：原计划 4 条，2 条以上信息密度不足，已自动合并为 2 条。
```

### 场景三：配音文案只差几个字

例如：

```text
target 55s hard minimum 137 chars, got 131
```

期望：

```text
系统自动补一句，不阻断
或 soft warning 后继续进入 TTS
```

不应再出现：

```text
blocked before TTS/cut/render
```

### 场景四：配音文案明显不足

例如：

```text
target 55s min 166 chars, got 80
```

期望：

```text
系统标记为严重不足
回退建议：合并视频 / 重跑 short_video_edit_plan
```

如果自动修复开启，则应先尝试合并，而不是直接失败。

---

## 10.16 推荐本次任务的重跑方式

代码修复后，建议从短视频规划阶段重跑，而不是只重跑 voiceover_script。

因为本次根因是：

```text
规划阶段拆太多条
```

所以应该从：

```bash
--rerun-from short_video_edit_plan
```

开始。

命令示例：

```bash
python run_multisource_pipeline.py ^
  --task-id 多源剪辑_2段_AI配音解说_20260602_124755 ^
  --source-request outputs\多源剪辑_2段_AI配音解说_20260602_124755\input\source_request.json ^
  --production-mode ai_voiceover ^
  --audio-policy ai_voiceover ^
  --output-mode multiple ^
  --max-output-videos 4 ^
  --target-duration 30 ^
  --allow-long-video ^
  --require-tts ^
  --voice-id default_female ^
  --rerun-from short_video_edit_plan
```

注意：仍然保留 `--max-output-videos 4`，因为这是用户给的上限。修复后，系统应该自己决定实际输出几条。

---

## 10.17 最终产品逻辑总结

用户不应该被迫判断生成几条。

正确产品逻辑是：

```text
用户：最多 4 条
AI：判断素材适合几条
系统：发现强拆就合并
系统：文案轻微不足就补写
系统：文案明显不足就缩短目标或合并
最终：输出 1-4 条质量合格的视频
```

这才符合“凤凰新闻视频智能拆条系统”的产品预期。

---

# 11. 新增问题：同一数据源不同生产模式的任务目录与公共中间产物复用机制

> 本节是在前面方案基础上的新增补充。前文所有方案保持不变，本节只新增“任务目录结构、公共产物复用、排队等待、资源管理”的优化方案。  
> 触发场景：同一批视频源，例如视频 A、B、C，用户分别运行 `AI配音解说` 和 `视频重组` 两种生产模式。当前系统会为每个任务创建独立目录，导致任务列表重复、公共步骤可能重复执行，且无法优雅等待前一个任务的公共产物完成后再复用。

---

## 11.1 当前现象

任务列表里会出现多个非常相似的任务：

```text
多源剪辑_2段_AI配音解说_20260602_12755
多源剪辑_2段_AI配音解说_20260602_115402
多源剪辑_2段_AI配音解说_20260602_115334
台密纯...
```

用户选择相同数据源，只是切换生产模式，例如：

```text
同一批视频源：A + B
任务一：AI 配音解说
任务二：视频重组
```

当前系统会创建两个独立任务目录：

```text
outputs/
  多源剪辑_2段_AI配音解说_xxx/
  多源剪辑_2段_视频重组_xxx/
```

这样有几个问题：

1. 任务列表看起来很乱，同一批素材被拆成多个同级任务。
2. 用户无法一眼看出哪些任务来自同一批素材。
3. 公共步骤是否真的复用不够清晰。
4. 如果两个任务同时跑，同样的 source_prepare、metadata、frame_extract、asr、vision、timeline 可能重复占用 GPU/LLM/磁盘资源。
5. 如果前一个任务还没跑完，后一个任务无法自动“等待公共产物完成后继续跑差异步骤”。

---

## 11.2 当前代码现状与问题

### 11.2.1 当前已有“跨模式复用”的雏形

`web_app.py` 中已经存在：

```python
REUSABLE_MODE_SWITCH_STEPS = (
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "vision",
    "timeline",
    "video_understanding",
    "highlight_detection",
)
```

也有 `_seed_reusable_outputs()`，用于在模式切换时复制旧任务的部分中间产物：

```python
def _seed_reusable_outputs(req: RunRequest, task_id: str, task_dir: Path) -> None:
    ...
    for step in REUSABLE_MODE_SWITCH_STEPS:
        ...
        _copy_reusable_path(source_copy_root, target_copy_root)
```

这说明代码已经意识到：

```text
不同生产模式之间，metadata/audio/frame/asr/vision/timeline 等步骤可以复用。
```

但当前方式是：

```text
从旧任务目录复制一份到新任务目录。
```

这不是最理想的“公共产物复用”，而是“复制式复用”。

### 11.2.2 复制式复用的问题

复制式复用有几个缺点：

1. 占磁盘空间：帧、音频、timeline、vision 输出可能重复存多份。
2. 状态不一致：旧任务公共步骤更新了，新任务复制出来的旧产物不会自动更新。
3. 排队等待困难：如果旧任务还在跑，新任务创建时还没有可复制文件，只能重新跑或失败。
4. 并发浪费：两个任务几乎同时创建时，公共步骤可能各跑一遍。
5. 目录结构不表达“同一批源素材”的关系。

### 11.2.3 当前任务 ID 仍以任务为中心，不以数据源为中心

当前任务 ID 大致是：

```text
{素材名}_{生产模式}_{时间戳/指纹}
```

或者：

```text
{素材名}_{生产模式}_{fingerprint}
```

这样每个模式都是一个独立顶层任务目录。

但更合理的是：

```text
同一批数据源 = 一个 source workspace
不同生产模式 = source workspace 下的不同 run
```

---

## 11.3 目标设计

新的设计目标：

```text
同一批数据源，只创建一个公共素材工作区。
公共步骤只执行一次。
不同生产模式只创建各自的模式任务目录。
模式任务可以等待公共步骤完成后继续执行。
```

也就是：

```text
source_workspace = 由视频 A+B+C 的内容指纹、顺序、关键参数决定
run_task = 在 source_workspace 下，按生产模式和输出参数创建
```

用户视角：

```text
素材组：多源剪辑_2段 / 日本防卫装备转移三原则
  ├─ AI 配音解说：运行中 / 已完成 / 失败
  ├─ 视频重组：等待公共分析 / 运行中 / 已完成
  └─ 公共分析：metadata、audio、frame、asr、vision、timeline 已完成
```

而不是现在这样：

```text
多源剪辑_2段_AI配音解说_xxx
多源剪辑_2段_视频重组_xxx
多源剪辑_2段_AI配音解说_yyy
```

---

## 11.4 推荐目录结构

### 11.4.1 新目录结构

建议新增 `outputs/source_workspaces/`：

```text
outputs/
  source_workspaces/
    srcgrp_{source_hash}/
      source_request.json
      source_manifest.json
      workspace.json
      locks/
        source_prepare.lock
        asr.lock
        vision.lock

      common/
        input/
          normalized_sources/
            source_001.mp4
            source_002.mp4
          source_multi.mp4
          source_manifest.json

        metadata/
          v1/
            video_metadata.json
            step_status.json

        preprocess/
          audio/
            v1/
              audio.wav
              step_status.json
          frames/
            v1/
              frames.json
              frames/
                chunk_0001_000.jpg
                ...

        asr/
          v1/
            asr_segments.json
            step_status.json

        vision/
          v1/
            chunk_0001/
              vision_chunk.json
              step_status.json
            chunk_0002/
              ...

        timeline/
          v1/
            merged_timeline.json
            step_status.json

        timeline_digest/
          v1/
            timeline_digest.json
            step_status.json

      runs/
        ai_voiceover_{run_hash}/
          run.json
          manifest.json
          analysis/
            content_analysis/
            short_video_edit_plan/
            voiceover_script/
          tts/
          subtitles/
          edit/
            cut_plan/
            drafts/

        highlight_reassembly_{run_hash}/
          run.json
          manifest.json
          analysis/
            video_understanding/
            highlight_detection/
            highlight_reassembly_plan/
          edit/
            reassembly_cut_plan/
            reassembly_drafts/
```

### 11.4.2 目录职责

```text
source_workspaces/srcgrp_xxx/
```

代表同一批数据源。只要视频源完全一致，顺序一致，源文件内容/远程素材 ID 一致，就进入同一个 workspace。

```text
common/
```

保存与生产模式无关的公共中间产物：

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

```text
runs/
```

保存与生产模式、输出参数有关的产物：

AI 配音解说：

```text
content_analysis
short_video_edit_plan
voiceover_script
tts
subtitles
cut_plan
render
```

视频重组：

```text
video_understanding
highlight_detection
highlight_reassembly_plan
reassembly_cut_plan
reassembly_render
```

---

## 11.5 哪些步骤可以复用，哪些不应该复用

### 11.5.1 强可复用步骤

这些步骤只依赖源视频和基础抽帧/ASR 参数：

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

只要以下参数一致，就可以复用：

```text
source_items
source order
aspect_ratio
chunk_seconds
frame_interval
vision_max_frames_per_chunk
ASR model/version
vision model/version
prompt_version for vision
```

### 11.5.2 条件可复用步骤

这些步骤虽然看似公共，但不同模式 prompt 或目标可能不同，需要谨慎：

```text
video_understanding
highlight_detection
content_analysis
```

建议第一版不要强行共用它们，而是作为各 run 自己的分析步骤。

原因：

- `content_analysis` 是 AI 配音解说模式的新链路。
- `video_understanding` / `highlight_detection` 是高光重组模式链路。
- 两者虽然都基于 timeline_digest，但输出结构和 prompt 目标不同。

后续可以抽象出更上层的：

```text
common/news_understanding
common/highlight_candidates
```

但第一版不建议混在一起，避免引入兼容风险。

### 11.5.3 不可复用步骤

这些步骤必须按 run 区分：

```text
short_video_edit_plan
voiceover_script
tts
subtitles
cut_plan
render
highlight_reassembly_plan
reassembly_cut_plan
reassembly_render
```

因为它们依赖：

```text
production_mode
output_mode
max_output_videos
target_duration
voice_id
audio_policy
allow_long_video
subtitle settings
```

---

## 11.6 source_hash 和 run_hash 设计

### 11.6.1 source_hash

`source_hash` 只描述“这一批源素材是谁”。

输入字段建议：

```python
{
    "sources": [
        {
            "order": 1,
            "source_type": "local",
            "path": "...",
            "size": 123,
            "mtime": 123456.789
        },
        {
            "order": 2,
            "source_type": "remote_ucms",
            "remote_id": "...",
            "guid": "...",
            "download_url": "..."
        }
    ],
    "source_order_sensitive": True
}
```

生成：

```python
source_hash = sha256(json.dumps(payload, sort_keys=True)).hexdigest()[:16]
```

注意：多源顺序必须参与 hash，因为 A+B 和 B+A 的虚拟时间轴不同。

### 11.6.2 common_hash

`common_hash` 描述“公共分析参数是否一致”。

输入字段：

```python
{
    "source_hash": source_hash,
    "aspect_ratio": req.aspect_ratio,
    "chunk_seconds": req.chunk_seconds,
    "frame_interval": req.frame_interval,
    "vision_max_frames_per_chunk": req.vision_max_frames_per_chunk,
    "asr_model": config.asr.model,
    "vision_model": config.llm.vision_model,
    "vision_prompt_version": prompt_version
}
```

第一版可以直接把这些参数纳入 workspace 的 `common_profile_hash`。

如果用户同一批源素材但切换 chunk_seconds 或 frame_interval，就创建新的 common profile：

```text
source_workspaces/srcgrp_xxx/common_profiles/common_{common_hash}/...
```

为了降低改造复杂度，第一版可以先让 `srcgrp_{source_hash}` 下只有一个 common profile；如果参数不一致，则新建另一个 workspace：

```text
srcgrp_{source_hash}_{common_hash}
```

### 11.6.3 run_hash

`run_hash` 描述“本次生产任务参数”。

输入字段：

```python
{
    "source_hash": source_hash,
    "common_hash": common_hash,
    "production_mode": req.production_mode,
    "output_mode": req.output_mode,
    "max_output_videos": req.max_output_videos,
    "target_duration_seconds": req.target_duration_seconds,
    "allow_long_video": req.allow_long_video,
    "voice_id": req.voice_id,
    "audio_policy": req.audio_policy,
    "subtitle_profile": subtitle_config_hash
}
```

输出目录：

```text
runs/ai_voiceover_{run_hash}/
runs/highlight_reassembly_{run_hash}/
```

---

## 11.7 任务状态模型

需要从“单任务状态”升级为“两层状态”：

```text
SourceWorkspaceStatus
RunTaskStatus
```

### 11.7.1 workspace 状态

`workspace.json`：

```json
{
  "source_workspace_id": "srcgrp_abcd1234",
  "source_hash": "abcd1234",
  "display_name": "多源剪辑_2段",
  "source_count": 2,
  "created_at": "",
  "updated_at": "",
  "common_status": "pending / running / success / failed / partial_success",
  "common_steps": {
    "source_prepare": {"status": "success", "output": "..."},
    "metadata": {"status": "success", "output": "..."},
    "audio_extract": {"status": "success", "output": "..."},
    "frame_extract": {"status": "success", "output": "..."},
    "chunk_build": {"status": "success", "output": "..."},
    "asr": {"status": "success", "output": "..."},
    "vision": {"status": "success", "output": "..."},
    "timeline": {"status": "success", "output": "..."},
    "timeline_digest": {"status": "success", "output": "..."}
  },
  "runs": [
    {
      "run_id": "ai_voiceover_xxx",
      "production_mode": "ai_voiceover",
      "status": "running"
    },
    {
      "run_id": "highlight_reassembly_yyy",
      "production_mode": "highlight_reassembly",
      "status": "waiting_common"
    }
  ]
}
```

### 11.7.2 run 状态

`runs/{run_id}/manifest.json`：

```json
{
  "task_id": "srcgrp_abcd1234/runs/ai_voiceover_xxx",
  "source_workspace_id": "srcgrp_abcd1234",
  "production_mode": "ai_voiceover",
  "status": "waiting_common / queued / running / success / failed / cancelled",
  "common_dependency": {
    "required_until_step": "timeline_digest",
    "status": "success",
    "workspace_common_path": "../../common"
  },
  "steps": {
    "content_analysis": {"status": "pending"},
    "short_video_edit_plan": {"status": "pending"},
    "voiceover_script": {"status": "pending"},
    "tts": {"status": "pending"},
    "subtitles": {"status": "pending"},
    "cut_plan": {"status": "pending"},
    "render": {"status": "pending"}
  }
}
```

---

## 11.8 排队与等待机制设计

### 11.8.1 当前队列问题

当前 `web_app.py` 中有：

```python
JOBS = {}
PENDING_JOB_IDS = deque()
MAX_RUNNING_JOBS
```

它只知道 job，不知道：

```text
这个 job 是否依赖某个 common job
这个 job 是否可以先排队但不执行
多个 job 是否共享同一个 common 阶段
```

### 11.8.2 新增 job 类型

建议新增三类 job：

```text
common_job：负责 source_workspace 的公共步骤
run_job：负责某个 production mode 的差异步骤
waiting_job：已创建 run，但等待 common_job 完成
```

job 示例：

```json
{
  "job_id": "job_xxx",
  "job_type": "common / run",
  "source_workspace_id": "srcgrp_abcd1234",
  "run_id": "ai_voiceover_xxx",
  "depends_on_job_id": "job_common_xxx",
  "status": "pending / waiting_common / running / success / failed / cancelled",
  "cmd": []
}
```

### 11.8.3 提交流程

用户选择 A+B+C，第一次运行 AI 配音：

```text
1. 计算 source_hash
2. 创建 source_workspace
3. common 不存在，创建 common_job
4. 创建 ai_voiceover run
5. ai_voiceover run_job 标记 waiting_common
6. common_job 跑 source_prepare 到 timeline_digest
7. common_job 成功后，自动释放 ai_voiceover run_job
8. ai_voiceover run_job 从 content_analysis 开始跑
```

用户在 common_job 尚未完成时，又基于 A+B+C 创建视频重组：

```text
1. 计算 source_hash，发现同一个 source_workspace
2. 创建 highlight_reassembly run
3. 发现 common 正在 running
4. 不重复跑 common
5. highlight_reassembly run_job 标记 waiting_common
6. common_job 完成后，自动释放 highlight_reassembly run_job
7. highlight_reassembly run_job 从 video_understanding/highlight_detection 开始跑
```

用户在 common 已完成后创建另一个模式：

```text
1. 发现 source_workspace common_status=success
2. 直接创建 run_job
3. run_job 立即从差异步骤开始跑
```

---

## 11.9 资源管理与锁设计

### 11.9.1 为什么必须加锁

公共步骤里有重资源：

```text
ASR：GPU/模型加载
vision：多模态 LLM 并发
frame_extract：大量图片写盘
timeline：依赖上游完整输出
```

如果两个任务同时命中同一批源素材，必须保证：

```text
同一个 workspace 的 common 步骤只跑一次
其他任务等待它完成
```

### 11.9.2 workspace 级锁

建议使用文件锁：

```text
outputs/source_workspaces/srcgrp_xxx/locks/common.lock
```

伪代码：

```python
from filelock import FileLock

def acquire_workspace_common_lock(workspace_dir: Path):
    lock_path = workspace_dir / "locks" / "common.lock"
    ensure_dir(lock_path.parent)
    return FileLock(str(lock_path), timeout=1)
```

使用：

```python
try:
    with acquire_workspace_common_lock(workspace_dir):
        if common_status == "success":
            return "reuse"
        run_common_steps()
except Timeout:
    return "waiting_common"
```

### 11.9.3 step 级锁

如果后续支持单 step 重跑，可进一步加：

```text
locks/asr.lock
locks/vision.lock
locks/timeline.lock
```

第一版可以只做 workspace common 总锁，降低复杂度。

### 11.9.4 GPU/LLM 资源限制

建议资源池：

```python
RESOURCE_LIMITS = {
    "asr_gpu": 1,
    "vision_llm": 3,
    "tts_gpu": 1,
    "ffmpeg": 2
}
```

job 标记需要的资源：

```json
{
  "required_resources": {
    "asr_gpu": 1,
    "vision_llm": 3
  }
}
```

调度器只有在资源足够时才启动 job。

第一版可以简化为：

```text
common_job 串行
run_job 可并行，但 TTS 限制 1 个
vision 仍走已有 vision_max_workers
```

---

## 11.10 Pipeline 改造方案

### 11.10.1 新增公共步骤模式

当前 `pipeline.py` 根据 production_mode 决定步骤：

```python
AI_VOICEOVER_STEP_ORDER = [...]
HIGHLIGHT_REASSEMBLY_STEP_ORDER = [...]
```

建议新增：

```python
COMMON_ANALYSIS_STEP_ORDER = [
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "vision",
    "timeline",
    "timeline_digest",
]
```

AI 配音 run 只跑：

```python
AI_VOICEOVER_RUN_STEP_ORDER = [
    "content_analysis",
    "short_video_edit_plan",
    "voiceover_script",
    "tts",
    "subtitles",
    "cut_plan",
    "render",
]
```

视频重组 run 只跑：

```python
HIGHLIGHT_REASSEMBLY_RUN_STEP_ORDER = [
    "video_understanding",
    "highlight_detection",
    "highlight_reassembly_plan",
    "reassembly_cut_plan",
    "reassembly_render",
]
```

### 11.10.2 新增运行参数

`run_pipeline.py` / `run_multisource_pipeline.py` 增加：

```bash
--workspace-id
--workspace-dir
--run-id
--run-dir
--common-dir
--run-scope common
--run-scope mode
--reuse-common
--wait-common
```

含义：

```text
--run-scope common：只跑公共步骤
--run-scope mode：只跑当前模式差异步骤
--reuse-common：差异步骤从 common_dir 读取公共产物
--wait-common：如果 common 未完成，则等待或退出为 waiting_common
```

### 11.10.3 让 PipelineRunner 支持 common_dir 和 run_dir

当前 `PipelineRunner` 基于：

```python
self.task_dir
```

读写所有产物。

建议新增：

```python
self.workspace_dir
self.common_dir
self.run_dir
```

路径规则：

```python
def _step_root(self, step: str) -> Path:
    if step in COMMON_ANALYSIS_STEP_ORDER:
        return self.common_dir
    return self.run_dir
```

修改 `_version_dir()`：

```python
def _version_dir(self, step: str, base: str) -> tuple[str, Path]:
    root = self._step_root(step)
    ...
    return version, root / base / version
```

修改 `_step_output()`、`_load_step_json()`：

```python
def _step_output_path(self, step: str) -> Path:
    output = self.manifest["steps"][step]["output"]
    if step in COMMON_ANALYSIS_STEP_ORDER:
        return self.common_dir / output
    return self.run_dir / output
```

为了降低第一版改造风险，也可以不大改 `PipelineRunner`，而是使用“挂载/引用”方式：

```text
run_dir/common_ref.json 指向 common_dir
读取公共步骤时，如果 run_dir 里没有，就去 common_dir 找
写公共步骤时，只写 common_dir
```

---

## 11.11 Web 后端改造方案

### 11.11.1 新增 workspace 相关函数

文件：

```text
web_app.py
```

新增：

```python
def _source_workspace_fingerprint(req: RunRequest, raw_source_items: list[dict[str, Any]]) -> str:
    payload = []
    for index, item in enumerate(raw_source_items, start=1):
        source_type = str(item.get("source_type") or item.get("type") or "local")
        if source_type in {"local", "video"}:
            path = _resolve_input_video(str(item.get("path") or item.get("input_video")))
            payload.append({
                "order": index,
                "source_type": "local",
                **_video_fingerprint(path),
            })
        else:
            remote = item.get("remote_video") or item
            payload.append({
                "order": index,
                "source_type": "remote_ucms",
                "remote": _remote_video_fingerprint(remote),
            })

    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
```

新增：

```python
def _common_profile_fingerprint(req: RunRequest, source_hash: str) -> str:
    payload = {
        "source_hash": source_hash,
        "aspect_ratio": req.aspect_ratio,
        "chunk_seconds": req.chunk_seconds,
        "frame_interval": req.frame_interval,
        "vision_max_frames_per_chunk": getattr(req, "vision_max_frames_per_chunk", None),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
```

新增：

```python
def _resolve_workspace_dir(source_hash: str, common_hash: str) -> Path:
    return ensure_dir(OUTPUTS_DIR / "source_workspaces" / f"srcgrp_{source_hash}_{common_hash}")
```

### 11.11.2 提交任务时先创建 workspace，再创建 run

当前：

```python
task_dir = ensure_dir(OUTPUTS_DIR / task_id)
```

建议改成：

```python
source_hash = _source_workspace_fingerprint(req, raw_source_items)
common_hash = _common_profile_fingerprint(req, source_hash)
workspace_dir = _resolve_workspace_dir(source_hash, common_hash)

run_hash = _run_fingerprint_for_workspace(req, source_hash, common_hash)
run_id = f"{req.production_mode}_{run_hash}"
run_dir = ensure_dir(workspace_dir / "runs" / run_id)
```

前端显示名可以仍然是中文：

```python
display_task_id = f"{workspace_display_name}_{_mode_slug(req.production_mode)}_{run_hash}"
```

但实际路径建议使用稳定 ID，避免中文路径和嵌套路径带来的兼容风险。

### 11.11.3 创建 common_job 和 run_job

伪代码：

```python
common_status = _workspace_common_status(workspace_dir)

if common_status == "missing":
    common_job = _create_common_job(workspace_dir, req, raw_source_items)
    run_job = _create_run_job(run_dir, req, depends_on=common_job["job_id"], status="waiting_common")
elif common_status == "running":
    common_job = _find_running_common_job(workspace_dir)
    run_job = _create_run_job(run_dir, req, depends_on=common_job["job_id"], status="waiting_common")
elif common_status == "success":
    run_job = _create_run_job(run_dir, req, depends_on="", status="pending")
elif common_status == "failed":
    raise HTTPException(409, "公共分析失败，请先重跑公共分析")
```

### 11.11.4 调度器释放 waiting_common

修改 `_schedule_jobs()`：

```python
def _schedule_jobs() -> None:
    with JOB_LOCK:
        _release_waiting_common_jobs()

        running = _running_job_count()
        while running < MAX_RUNNING_JOBS and PENDING_JOB_IDS:
            ...
```

新增：

```python
def _release_waiting_common_jobs() -> None:
    for job in JOBS.values():
        if job.get("status") != "waiting_common":
            continue

        depends_on = job.get("depends_on_job_id")
        if not depends_on:
            job["status"] = "pending"
            PENDING_JOB_IDS.append(job["job_id"])
            continue

        dep = JOBS.get(depends_on)
        if not dep:
            job["status"] = "failed"
            job["user_message"] = "依赖的公共分析任务不存在"
            continue

        dep_public = _refresh_job(depends_on, schedule_next=False)
        if dep_public.get("status") == "success":
            job["status"] = "pending"
            PENDING_JOB_IDS.append(job["job_id"])
        elif dep_public.get("status") in {"failed", "cancelled"}:
            job["status"] = "failed"
            job["user_message"] = "依赖的公共分析失败或被取消"
```

---

## 11.12 前端任务列表改造方案

### 11.12.1 当前列表问题

现在任务列表是扁平的：

```text
任务1
任务2
任务3
```

建议改成两层：

```text
素材组：多源剪辑_2段
  公共分析：15/16 完成
  AI配音解说：运行中
  视频重组：等待公共分析
```

### 11.12.2 API 设计

新增：

```text
GET /api/workspaces
GET /api/workspaces/{workspace_id}
GET /api/workspaces/{workspace_id}/runs
GET /api/runs/{run_id}/manifest
GET /api/runs/{run_id}/tree
GET /api/runs/{run_id}/source-videos
```

为了兼容旧前端，保留：

```text
GET /api/tasks
GET /api/tasks/{task_id}/manifest
```

但新 UI 优先用 workspaces。

### 11.12.3 前端展示结构

```html
<div class="workspace-card">
  <div class="workspace-title">多源剪辑_2段</div>
  <div class="workspace-subtitle">2 个源视频 · 公共分析 15/16 完成</div>

  <button class="run-item">
    <strong>AI配音解说</strong>
    <span>运行中 · voiceover_script</span>
  </button>

  <button class="run-item">
    <strong>视频重组</strong>
    <span>等待公共分析完成</span>
  </button>
</div>
```

### 11.12.4 状态文案

```javascript
const RUN_STATUS_LABELS = {
  waiting_common: "等待公共分析",
  pending: "排队中",
  running: "运行中",
  success: "已完成",
  failed: "失败",
  cancelled: "已取消",
};
```

---

## 11.13 兼容旧任务目录

不能一下子破坏已有 `outputs/<task_id>` 目录。

建议分两期：

### 第一期：兼容旧任务 + 新任务走 workspace

```text
旧任务：仍然从 outputs/<task_id> 读取
新任务：写入 outputs/source_workspaces/srcgrp_xxx/
```

`GET /api/tasks` 同时返回：

```text
legacy tasks
workspace runs
```

每个返回项加：

```json
{
  "task_layout": "legacy / workspace",
  "workspace_id": "",
  "run_id": ""
}
```

### 第二期：提供迁移脚本

新增：

```bash
python migrate_outputs_to_workspaces.py
```

用于把旧任务中可识别的公共产物迁移到 workspace。

第一版不建议做自动迁移，避免误删或误移动历史任务。

---

## 11.14 推荐分阶段实施

### 阶段一：轻量复用，不大改 PipelineRunner

目标：

```text
不改变 pipeline.py 大结构，只在 web_app.py 做任务依赖和复制/链接复用。
```

做法：

1. 计算 source_workspace_id。
2. 新建 workspace 目录。
3. 公共任务先跑完整 pipeline 到 `timeline_digest`，可以通过新增 `--rerun-from` 或 `--only-common`。
4. 模式任务启动前，把 common 目录中的公共步骤复制或硬链接到 run_dir。
5. run 任务从模式差异步骤开始跑。

优点：

```text
改造小，风险低。
```

缺点：

```text
仍有复制/链接成本，不是真正单份读写。
```

### 阶段二：PipelineRunner 原生支持 common_dir/run_dir

目标：

```text
公共步骤真正只写 common_dir。
模式步骤真正只写 run_dir。
```

做法：

1. `PipelineRunner` 新增 `common_dir`、`run_dir`。
2. `_step_root(step)` 决定读写路径。
3. manifest 拆成 workspace manifest 和 run manifest。
4. 所有 `_load_step_json()` 支持从 common_dir 读取公共步骤。

优点：

```text
结构清晰，复用彻底。
```

缺点：

```text
改动大，需要完整回归测试。
```

### 建议

先做阶段一，等流程稳定后再做阶段二。

---

## 11.15 第一阶段可直接落地的折中方案

如果现在不想大改 `PipelineRunner`，可以这样做：

```text
公共 workspace 负责产物缓存。
每个 run 仍然有自己的 task_dir。
run 启动前，如果 common 已完成，就把公共步骤目录 hardlink/copy 到 run task_dir。
如果 common 未完成，run job 进入 waiting_common。
```

目录：

```text
outputs/
  source_workspaces/
    srcgrp_xxx/
      common_task/
        manifest.json
        metadata/
        preprocess/
        asr/
        vision/
        timeline/
        timeline_digest/
      runs/
        ai_voiceover_xxx -> outputs/多源剪辑_AI配音解说_xxx
        highlight_reassembly_xxx -> outputs/多源剪辑_视频重组_xxx

  多源剪辑_AI配音解说_xxx/
  多源剪辑_视频重组_xxx/
```

这样：

```text
用户看到的任务目录还兼容旧逻辑。
公共产物来自 common_task。
不同模式任务启动前 seed 公共产物。
如果 common_task 还在跑，则等待。
```

这是最安全的过渡方案。

---

## 11.16 第一阶段详细流程

### 第一次提交 A+B，AI 配音

```text
1. 计算 source_hash
2. 创建 workspace/srcgrp_xxx/common_task
3. 创建 AI 配音 task_dir
4. 创建 common_job：跑到 timeline_digest
5. 创建 ai_voiceover job：waiting_common
6. common_job 成功
7. seed common outputs 到 ai_voiceover task_dir
8. ai_voiceover job 从 content_analysis 跑
```

### 第二次提交 A+B，视频重组，且 common 正在跑

```text
1. 计算同一个 source_hash
2. 找到 workspace/srcgrp_xxx/common_task
3. 创建 视频重组 task_dir
4. 创建 highlight_reassembly job：waiting_common
5. 不重复创建 common_job
6. common_job 成功后，seed common outputs
7. highlight_reassembly job 从 video_understanding 跑
```

### 第三次提交 A+B，另一个 AI 配音参数，common 已完成

```text
1. 计算同一个 source_hash
2. common_status=success
3. 创建新 AI 配音 task_dir
4. seed common outputs
5. 直接从 content_analysis 或 short_video_edit_plan 跑
```

---

## 11.17 需要新增的配置

`config.toml`：

```toml
[workspace_cache]
enabled = true
root = "outputs/source_workspaces"
reuse_common_steps = true
reuse_mode = "hardlink_or_copy" # hardlink_or_copy / copy / symlink
wait_for_common = true
common_wait_timeout_seconds = 7200
common_poll_interval_seconds = 5
cleanup_orphan_waiting_jobs = true

# 参与公共缓存的步骤
common_steps = [
  "metadata",
  "audio_extract",
  "frame_extract",
  "chunk_build",
  "asr",
  "vision",
  "timeline",
  "timeline_digest"
]

# 第一阶段：公共任务实际跑完整 pipeline 的前半段
common_until_step = "timeline_digest"
```

---

## 11.18 本节与前文方案的冲突文件补充说明

### `web_app.py`

前文已涉及：

```text
任务终止按钮后端接口
任务命名修复
原片预览修复
```

本节新增：

```text
source_workspace_id / common_job / waiting_common / run_job
workspace API
任务列表聚合
```

处理方式：

```text
不要覆盖前文接口。
在现有 job 结构上新增字段：
job_type
source_workspace_id
run_id
depends_on_job_id
status=waiting_common
```

### `run_multisource_pipeline.py`

前文已涉及：

```text
source_request 保存 production_mode
manifest 保留 production_mode
```

本节新增：

```text
支持 --run-scope common / mode
支持 --common-dir / --run-dir
```

第一阶段可以先不改它，只通过 web_app.py 创建 common_task 和 run_task 两类普通任务。

### `newsclip_agent/pipeline.py`

前文已涉及：

```text
timeline_digest
过度拆分修复
voiceover 文案自动补写
字幕配置
```

本节第二阶段才需要大改：

```text
common_dir / run_dir
_step_root(step)
```

第一阶段不强制改 pipeline.py，避免和前文改动叠加过大。

### `web_static/app.js`

前文已涉及：

```text
任务终止按钮
原片预览
任务名同步
最多成片数提示
```

本节新增：

```text
workspace 分组任务列表
waiting_common 状态展示
```

处理方式：

```text
先兼容旧 /api/tasks。
新增 /api/workspaces 后，再把任务列表改成分组展示。
```

---

## 11.19 验收标准

### 验收一：同一批源素材只跑一次公共分析

操作：

```text
选择 A+B，运行 AI 配音解说
在 ASR 或 vision 还没结束时，再选择 A+B，运行视频重组
```

期望：

```text
只存在一个 common_job 在跑 ASR/vision/timeline
第二个任务状态为 waiting_common
不会重复加载 FunASR
不会重复调用 vision chunk 分析
```

### 验收二：公共分析完成后自动释放等待任务

期望：

```text
common_job 成功后
AI 配音任务进入 content_analysis
视频重组任务进入 video_understanding
```

无需用户手动重跑。

### 验收三：同一素材组任务列表合并展示

期望 UI：

```text
多源剪辑_2段
  公共分析：完成
  AI配音解说：运行中 / 已完成
  视频重组：等待公共分析 / 已完成
```

而不是多个重复的顶层任务。

### 验收四：复用公共产物

第二个模式任务日志中应出现：

```text
复用公共步骤: metadata
复用公共步骤: audio_extract
复用公共步骤: frame_extract
复用公共步骤: chunk_build
复用公共步骤: asr
复用公共步骤: vision
复用公共步骤: timeline
复用公共步骤: timeline_digest
```

并且直接从模式差异步骤开始。

### 验收五：参数不一致时不误复用

如果用户改了：

```text
chunk_seconds
frame_interval
aspect_ratio
vision_max_frames_per_chunk
```

系统应该创建新的 common profile，不应误用旧公共产物。

---

## 11.20 最终建议

这块确实比较复杂，不建议一次性把 `PipelineRunner` 全改掉。

推荐路线：

```text
第一阶段：
保留现有任务目录结构。
新增 source_workspace + common_task。
不同模式任务通过 waiting_common 等待公共任务完成。
公共产物完成后 seed 到各自任务目录。
任务列表按 workspace 聚合展示。

第二阶段：
PipelineRunner 原生支持 common_dir/run_dir。
公共产物真正只保存一份。
run 目录只保存差异步骤。
```

这样能在较低风险下先解决用户最痛的问题：

```text
同一批视频不同模式不应重复解析。
后创建的模式任务应等待前一个公共分析完成。
任务列表应按素材组聚合。
公共步骤应可复用。
```

并且不会破坏前文已经规划的：

```text
timeline_digest 修复
Web 预览修复
终止任务按钮
字幕缩小
任务名修复
AI 多视频过度拆分修复
```
