# 多视频前置分析并发与进度优化方案（不包含“两阶段 ASR 初筛再 Vision”方案）

适用仓库：`angelOnly/AutomaticEditing`  
适用分支：`clean-highlight-reassembly`  
适用入口：`python run_web.py`  
重点问题：多视频任务在 `source_analysis` 阶段耗时长、进度显示不清晰、日志难以判断是否真实并发、公共分析状态与 Web 进度偶发不同步。

---

## 0. 本方案不包含的内容

本方案**不包含**此前提到的“修改四”：

> 多源任务先只跑 ASR 初筛候选片段，再只对候选片段附近做 Vision。

这个方案属于流程重构，会改变素材分析策略、候选片段来源和后续高光判断逻辑。  
当前文档只针对现有 unified multi-source pipeline 做稳定性、并发可观测性、资源调度和进度同步优化。

---

## 1. 当前代码现状

### 1.1 Web 启动入口

当前入口是：

```bash
python run_web.py
```

`run_web.py` 的职责很轻：

1. 加载 `config.toml`。
2. 做 FunASR / FFmpeg / CUDA / 模型路径启动前检查。
3. 启动 `uvicorn.run("web_app:app", ...)`。

因此优化不应破坏这个启动方式。

---

### 1.2 Web 提交任务后的执行链路

当前 Web 提交后大致链路是：

```text
web_app.py
  /api/run
    ↓
  _build_run_command()
    ↓
  如果是 source_request / 多源 / unified source pipeline：
      调 run_multisource_pipeline.py
  否则：
      调 run_pipeline.py
```

多源或 unified source pipeline 会走：

```text
run_multisource_pipeline.py
  ↓
_ensure_common_analysis()
  ↓
pipeline_main(... --common-only)
  ↓
newsclip_agent/pipeline.py
```

---

### 1.3 当前 unified 多源公共步骤

Web 里定义的 unified AI 配音步骤是：

```python
UNIFIED_AI_VOICEOVER_STEP_ORDER = [
    "source_prepare",
    "source_analysis",
    "source_aggregate",
    "content_analysis",
    "short_video_edit_plan",
    "voiceover_script",
    "tts",
    "subtitles",
    "cut_plan",
    "render",
]
```

高光重组步骤是：

```python
UNIFIED_HIGHLIGHT_REASSEMBLY_STEP_ORDER = [
    "source_prepare",
    "source_analysis",
    "source_aggregate",
    "video_understanding",
    "highlight_detection",
    "highlight_reassembly_plan",
    "reassembly_cut_plan",
    "reassembly_render",
]
```

也就是说，多视频统一流程的公共部分是：

```text
source_prepare
source_analysis
source_aggregate
```

其中真正耗时的是 `source_analysis`。

---

### 1.4 source_analysis 实际做了什么

在 `newsclip_agent/pipeline.py` 中，`step_source_analysis()` 会对每个源视频创建一个子 `PipelineRunner`：

```python
child_options = RunOptions(
    input=str(source_path),
    task_id=source_id,
    outputs_dir=str(work_root),
    common_only=True,
    production_mode="ai_voiceover",
    ...
)
child_runner = PipelineRunner(child_options)
child_manifest = child_runner.run()
```

因为 `common_only=True`，单个源视频内部会跑：

```text
metadata
audio_extract
frame_extract
chunk_build
asr
asr_digest
vision
timeline
timeline_digest
```

所以用户界面上看到的“视频分析素材 / source_analysis”，不是简单预处理，而是完整的单视频公共分析。

---

### 1.5 当前并发配置

`config.toml` 里当前相关配置是：

```toml
[multi_source_analysis]
enabled = true
source_max_workers = 3
per_source_vision_max_workers = 2
vision_global_max_workers = 6
text_global_max_workers = 6
fail_policy = "partial_success"
```

`step_source_analysis()` 当前使用：

```python
max_workers = max(1, int(cfg.get("source_max_workers", 3) or 3))

with ThreadPoolExecutor(max_workers=min(max_workers, len(sources))) as executor:
    future_map = {executor.submit(analyze_one, item): item for item in sources}
```

所以理论上：

```text
最多 3 个源视频并发分析。
每个源视频内部 vision 最多 2 个 worker。
理论视觉并发约 3 × 2 = 6。
```

但目前 `vision_global_max_workers` 和 `text_global_max_workers` 配置值没有真正参与全局限流，属于“配置写了，但代码没有完整落地”。

---

## 2. 当前问题原因

### 2.1 用户感觉慢的根因

用户选择多个视频后，`source_analysis` 会同时对每条视频跑完整分析：

```text
抽音频
抽帧
切 chunk
FunASR 转写
ASR digest
Vision chunk 分析
timeline 融合
timeline_digest
```

这本身就是全流程中最重的部分。  
如果一次选择 5 条视频，每条视频 8-10 分钟，就可能产生几十个 vision chunk。即使有并发，11 分钟仍可能停留在 `source_analysis`。

---

### 2.2 当前并发不可见

虽然 `source_analysis` 内部使用了 `ThreadPoolExecutor`，但日志主要输出：

```text
vision chunk_0001: success
vision chunk_0002: success
...
```

缺少：

```text
source_id
source_name
source_index
当前完成第几个源视频
总共有几个源视频
```

所以用户看不出：

```text
到底是 1 个视频在跑？
还是 3 个视频同时在跑？
每个视频跑到哪个步骤？
```

---

### 2.3 source_analysis 中间进度没有写入 manifest

当前 `source_analysis` 只有在所有源视频分析完之后，才 `_record_step()` 写最终结果。

这会导致 Web 上长时间只显示：

```text
视频分析素材：处理中
```

而不是：

```text
视频分析素材：2/5 个视频完成
当前运行：source_001 vision，source_002 asr，source_003 timeline
```

---

### 2.4 多层 manifest 导致 UI 与日志可能错位

当前多源任务有两层目录：

```text
outputs/<child_task_id>/manifest.json
outputs/__common__/common_xxx/manifest.json
```

实际公共分析写在 common 目录。  
Web UI 主要读取 child task manifest。  
`run_multisource_pipeline.py` 有同步线程，但不是强一致。

如果 common manifest 更新了，child manifest 没及时同步，用户看到的进度就会滞后。

---

### 2.5 并发可能过高导致反而慢

当前配置理论上可能达到：

```text
3 个源视频并发
每源 2 个 vision worker
总共 6 路 vision 请求
```

同时还有：

```text
FunASR 使用 cuda:0
FFmpeg 抽音频 / 抽帧
文本模型 asr_digest
timeline 处理
```

如果 GPU、API QPS、磁盘 IO、CPU 同时被打满，整体会变慢，甚至出现接口限流、重试、某些步骤排队。

---

## 3. 优化目标

本轮优化目标不是改变算法，而是把当前流程做稳、做透明、做可控：

1. 明确多视频是否并发。
2. Web 进度能显示源视频级别进度。
3. 日志能看出每个 source 的开始、结束、失败和耗时。
4. `source_analysis` 中途能把进度写入 manifest。
5. 公共分析状态自动同步到子任务 manifest。
6. 并发配置真正落地，避免无效配置。
7. 默认并发更稳，不让 GPU / API / IO 被过度抢占。
8. 不破坏 `python run_web.py` 启动方式。
9. 不影响现有单视频任务。
10. 不引入大规模流程重构。

---

## 4. 修改文件总览

建议修改以下文件：

```text
1. newsclip_agent/pipeline.py
   - source_analysis 增加 source 级日志
   - source_analysis 增加中间进度写 manifest
   - source_analysis 增加每个源视频步骤摘要
   - source_analysis 增加并发配置记录
   - 可选：增加全局 Vision 限流

2. run_multisource_pipeline.py
   - 公共分析 ready 判断增强
   - 公共分析失败时写入 common manifest
   - common 输出复制后补齐 child manifest
   - 增加 common progress debug

3. web_app.py
   - /api/tasks 和 /api/tasks/{task_id}/manifest 读取时主动 hydrate common progress
   - 增加 task summary 字段：common_progress、completed_sources、total_sources
   - 保持 python run_web.py 不变

4. config.toml
   - 调整默认并发为更稳的值
   - 增加 progress / logging 开关
```

---

# 5. 详细修改方案一：source_analysis 日志增强

## 5.1 修改文件

```text
newsclip_agent/pipeline.py
```

## 5.2 修改位置

函数：

```python
def step_source_analysis(self) -> None:
```

内部子函数：

```python
def analyze_one(item: dict[str, Any]) -> dict[str, Any]:
```

## 5.3 修改目标

现在日志里只能看到：

```text
vision chunk_0001: success
```

修改后希望看到：

```text
[source_analysis] queue total=5 max_workers=2
[source_analysis] start source_001 1/5 video=A.mp4
[source_analysis] start source_002 2/5 video=B.mp4
[source_analysis] done source_001 elapsed=318.42s chunks=8 video=A.mp4
[source_analysis] failed source_003 elapsed=92.18s video=C.mp4 error=...
[source_analysis] progress 2/5 success=2 failed=0 running=2
```

这样用户和开发者能直接判断多源并发是否生效。

## 5.4 建议代码片段

在 `step_source_analysis()` 里，读取 `sources` 后先构建 source 序号：

```python
source_positions = {
    str(item.get("source_id") or f"source_{idx:03d}"): idx
    for idx, item in enumerate(sources, start=1)
}
total_sources = len(sources)
```

在创建线程池之前打印：

```python
print(
    f"[source_analysis] queue total={total_sources} "
    f"source_max_workers={min(max_workers, total_sources)} "
    f"per_source_vision_max_workers={cfg.get('per_source_vision_max_workers', self.options.vision_max_workers)}",
    flush=True,
)
```

修改 `analyze_one()`：

```python
def analyze_one(item: dict[str, Any]) -> dict[str, Any]:
    source_id = str(item.get("source_id") or f"source_{len(sources) + 1:03d}")
    source_pos = source_positions.get(source_id, 0)
    source_dir = ensure_dir(base_dir / source_id)
    work_root = ensure_dir(source_dir / "_work")
    source_path = self._resolve_source_item_path(item)
    started_at = time.perf_counter()

    print(
        f"[source_analysis] start {source_id} {source_pos}/{total_sources} "
        f"video={source_path.name}",
        flush=True,
    )

    try:
        child_options = RunOptions(
            input=str(source_path),
            task_id=source_id,
            config=str(config_path),
            outputs_dir=str(work_root),
            resume=self.options.resume,
            rerun_from="metadata" if self.options.rerun == "source_analysis" else None,
            chunk_seconds=self.options.chunk_seconds,
            frame_interval=self.options.frame_interval,
            vision_max_workers=max(
                1,
                int(
                    cfg.get(
                        "per_source_vision_max_workers",
                        self.options.vision_max_workers,
                    )
                    or self.options.vision_max_workers
                ),
            ),
            vision_max_frames_per_chunk=self.options.vision_max_frames_per_chunk,
            release_asr_after_task=self.options.release_asr_after_task,
            aspect_ratio=self.options.aspect_ratio,
            mode=self.options.mode,
            common_only=True,
            production_mode="ai_voiceover",
        )

        child_runner = PipelineRunner(child_options)
        child_manifest = child_runner.run()
        digest = child_runner._load_step_json("timeline_digest")
        summary = self._build_source_summary(
            item,
            child_runner.task_dir,
            child_manifest,
            digest,
        )
        summary_path = write_json(source_dir / "source_summary.json", summary)

        elapsed = round(time.perf_counter() - started_at, 3)
        chunk_count = len(digest.get("chunks") or [])

        print(
            f"[source_analysis] done {source_id} {source_pos}/{total_sources} "
            f"elapsed={elapsed}s chunks={chunk_count} video={source_path.name}",
            flush=True,
        )

        return {
            "source_id": source_id,
            "status": "success",
            "display_name": item.get("display_name") or source_path.name,
            "summary": relpath(summary_path, self.task_dir),
            "work_task_dir": relpath(child_runner.task_dir, self.task_dir),
            "chunk_count": chunk_count,
            "elapsed_seconds": elapsed,
        }

    except Exception as exc:
        elapsed = round(time.perf_counter() - started_at, 3)
        print(
            f"[source_analysis] failed {source_id} {source_pos}/{total_sources} "
            f"elapsed={elapsed}s video={source_path.name} error={exc}",
            flush=True,
        )
        raise
```

---

# 6. 详细修改方案二：source_analysis 中间进度写 manifest

## 6.1 修改文件

```text
newsclip_agent/pipeline.py
```

## 6.2 修改位置

`step_source_analysis()` 中：

```python
with ThreadPoolExecutor(...) as executor:
    future_map = ...
    for future in as_completed(future_map):
        ...
```

## 6.3 修改目标

Web manifest 中的 `source_analysis` 不应只显示：

```json
{
  "status": "running"
}
```

应显示：

```json
{
  "status": "running",
  "message": "多源素材分析中：2/5 个视频完成",
  "completed_sources": 2,
  "total_sources": 5,
  "success_count": 2,
  "failed_count": 0,
  "running_count": 2,
  "source_progress": [
    {
      "source_id": "source_001",
      "display_name": "A.mp4",
      "status": "success",
      "elapsed_seconds": 318.42,
      "chunk_count": 8
    },
    {
      "source_id": "source_002",
      "display_name": "B.mp4",
      "status": "running"
    }
  ]
}
```

## 6.4 建议代码片段

在 `step_source_analysis()` 里，线程池之前初始化：

```python
source_progress: dict[str, dict[str, Any]] = {}
for idx, item in enumerate(sources, start=1):
    source_id = str(item.get("source_id") or f"source_{idx:03d}")
    display_name = str(item.get("display_name") or Path(str(item.get("original_path") or "")).name)
    source_progress[source_id] = {
        "source_id": source_id,
        "source_index": item.get("source_index") or idx,
        "display_name": display_name,
        "status": "pending",
        "updated_at": now_iso(),
    }
```

提交任务后，把状态设为 running：

```python
with ThreadPoolExecutor(max_workers=min(max_workers, len(sources))) as executor:
    future_map = {}
    for item in sources:
        source_id = str(item.get("source_id") or "")
        source_progress.setdefault(source_id, {}).update({
            "status": "running",
            "updated_at": now_iso(),
        })
        future = executor.submit(analyze_one, item)
        future_map[future] = item

    self._update_step_progress(
        "source_analysis",
        f"多源素材分析启动：0/{len(sources)} 个视频完成",
        {
            "completed_sources": 0,
            "total_sources": len(sources),
            "success_count": 0,
            "failed_count": 0,
            "running_count": len(sources),
            "source_progress": list(source_progress.values()),
        },
    )
```

在 `as_completed` 循环里：

```python
completed = 0
total = len(future_map)

for future in as_completed(future_map):
    item = future_map[future]
    source_id = str(item.get("source_id") or "")
    try:
        result = future.result()
        results.append(result)
        source_progress[source_id].update({
            "status": "success",
            "elapsed_seconds": result.get("elapsed_seconds", 0),
            "chunk_count": result.get("chunk_count", 0),
            "summary": result.get("summary", ""),
            "updated_at": now_iso(),
        })
    except Exception as exc:
        failure = {
            "source_id": source_id,
            "display_name": item.get("display_name") or "",
            "status": "failed",
            "error": str(exc),
        }
        failures.append(failure)
        source_progress[source_id].update({
            "status": "failed",
            "error": str(exc),
            "updated_at": now_iso(),
        })
        if fail_policy == "fail_fast":
            raise
    finally:
        completed += 1
        running_count = sum(
            1
            for value in source_progress.values()
            if value.get("status") == "running"
        )
        self._update_step_progress(
            "source_analysis",
            f"多源素材分析中：{completed}/{total} 个视频完成",
            {
                "completed_sources": completed,
                "total_sources": total,
                "success_count": len(results),
                "failed_count": len(failures),
                "running_count": running_count,
                "source_progress": list(source_progress.values()),
            },
        )
        print(
            f"[source_analysis] progress {completed}/{total} "
            f"success={len(results)} failed={len(failures)} running={running_count}",
            flush=True,
        )
```

最终 `_record_step()` 的 `extra` 也建议带上完整统计：

```python
extra = {
    "completed_sources": len(results) + len(failures),
    "total_sources": len(sources),
    "success_count": len(results),
    "failed_count": len(failures),
    "source_progress": list(source_progress.values()),
}
if failures:
    extra["failed_sources"] = failures

self._record_step(
    step="source_analysis",
    version=version,
    status=status,
    output=relpath(out, self.task_dir),
    input_hash=input_hash,
    output_files=[out] + [self.task_dir / item["summary"] for item in results if item.get("summary")],
    extra=extra,
)
```

---

# 7. 详细修改方案三：让全局 Vision 并发配置真正生效

## 7.1 当前问题

`config.toml` 有：

```toml
vision_global_max_workers = 6
```

但当前代码主要使用：

```text
source_max_workers × per_source_vision_max_workers
```

并没有真正做全局视觉请求限流。

如果配置为：

```toml
source_max_workers = 3
per_source_vision_max_workers = 3
vision_global_max_workers = 6
```

理论上代码可能尝试 9 路 vision，但配置希望最多 6 路。当前无法保证。

---

## 7.2 推荐低风险实现：文件锁全局槽位

项目已有：

```python
from .resource_locks import file_slot_lock
```

`run_multisource_pipeline.py` 也已经使用了：

```python
with file_slot_lock(lock_name, slots=1):
```

所以可以复用这个机制，对 Vision chunk 请求加全局 slot。

---

## 7.3 修改文件

```text
newsclip_agent/pipeline.py
```

## 7.4 修改位置

找到 `step_vision()` 或实际调用视觉模型的位置。  
需要在每个 chunk 调用 `self.llm_vision.call_json(...)` 外层加锁。

伪代码：

```python
vision_global_slots = int(
    self.config.raw.get("multi_source_analysis", {}).get(
        "vision_global_max_workers",
        self.options.vision_max_workers,
    )
    or self.options.vision_max_workers
)
```

调用视觉模型时：

```python
with file_slot_lock("global_vision_llm", slots=max(1, vision_global_slots)):
    result = self.llm_vision.call_json(
        model=model,
        fallback_models=fallback,
        prompt=prompts.VISION_CHUNK_PROMPT,
        input_data=input_data,
        image_paths=frame_paths,
        ...
    )
```

这样即使有多个 `PipelineRunner`、多个源视频、多个线程池，全项目最多也只有 `vision_global_max_workers` 个视觉请求同时打出去。

---

## 7.5 注意事项

这个改动会让并发更可控，但不一定让峰值速度更快。它的价值是：

1. 避免 API 限流。
2. 避免瞬间请求过多导致失败重试。
3. 避免多视频同时跑时日志混乱。
4. 避免显存、网络、CPU、IO 被打爆。

建议默认：

```toml
vision_global_max_workers = 4
```

而不是 6。

---

# 8. 详细修改方案四：让全局文本 LLM 并发配置生效

## 8.1 当前问题

`config.toml` 有：

```toml
text_global_max_workers = 6
```

但多源并发时，每个子视频可能都在跑 `asr_digest`、后续公共步骤也会跑文本 LLM。  
如果没有全局限制，容易触发文本模型接口排队、限流或超时。

---

## 8.2 修改思路

在所有文本 LLM 调用外层加：

```python
with file_slot_lock("global_text_llm", slots=text_global_slots):
    ...
```

---

## 8.3 修改位置

`newsclip_agent/pipeline.py` 中凡是调用：

```python
self.llm_text.call_json(...)
```

或类似文本 LLM 的地方，都建议包一层。

低风险做法是先只包高频步骤：

```text
asr_digest
content_analysis
short_video_edit_plan
voiceover_script
video_understanding
highlight_detection
highlight_reassembly_plan
```

## 8.4 建议封装函数

在 `PipelineRunner` 里新增：

```python
def _global_text_llm_slots(self) -> int:
    cfg = self.config.raw.get("multi_source_analysis", {})
    return max(1, int(cfg.get("text_global_max_workers", 4) or 4))


def _global_vision_llm_slots(self) -> int:
    cfg = self.config.raw.get("multi_source_analysis", {})
    return max(1, int(cfg.get("vision_global_max_workers", 4) or 4))
```

然后文本调用处：

```python
with file_slot_lock("global_text_llm", slots=self._global_text_llm_slots()):
    result = self.llm_text.call_json(...)
```

视觉调用处：

```python
with file_slot_lock("global_vision_llm", slots=self._global_vision_llm_slots()):
    result = self.llm_vision.call_json(...)
```

---

# 9. 详细修改方案五：Web manifest 主动 hydrate common progress

## 9.1 当前问题

Web 子任务和 common 任务是两层 manifest：

```text
outputs/<task_id>/manifest.json
outputs/__common__/common_xxx/manifest.json
```

后台同步线程不是强一致。  
用户刷新页面时，如果 child manifest 没同步，UI 就可能显示旧进度。

---

## 9.2 修改文件

```text
web_app.py
```

## 9.3 新增函数

```python
def _hydrate_common_progress_for_manifest(task_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    common_task_id = str(manifest.get("common_task_id") or "").strip()

    if not common_task_id:
        source_request = read_json(task_dir / "input" / "source_request.json", {})
        common_task_id = str(source_request.get("common_task_id") or "").strip()

    if not common_task_id:
        return manifest

    common_dir = COMMON_OUTPUTS_DIR / common_task_id
    common_manifest = read_json(common_dir / "manifest.json", {})
    if not common_manifest:
        return manifest

    manifest.setdefault("steps", {})
    common_steps = common_manifest.get("steps", {}) or {}

    for step in UNIFIED_SOURCE_COMMON_REUSABLE_STEPS:
        item = common_steps.get(step)
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        copied["common_progress"] = True
        copied["common_task_id"] = common_task_id
        copied["reused_from"] = relpath(common_dir, ROOT)
        manifest["steps"][step] = copied

    for key in ("source_video", "source_mode", "source_manifest", "source_videos"):
        if common_manifest.get(key):
            manifest[key] = common_manifest[key]

    if common_manifest.get("current_versions"):
        manifest.setdefault("current_versions", {}).update(common_manifest.get("current_versions") or {})

    if common_manifest.get("status") == "failed":
        manifest["status"] = "failed"
        manifest["user_message"] = common_manifest.get("user_message") or manifest.get("user_message", "")

    manifest["common_task_id"] = common_task_id
    return manifest
```

---

## 9.4 在 get_manifest() 中调用

找到：

```python
_hydrate_manifest_options(task_dir, manifest)
```

改成：

```python
_hydrate_manifest_options(task_dir, manifest)
manifest = _hydrate_common_progress_for_manifest(task_dir, manifest)
```

---

## 9.5 在 list_tasks() 中调用

找到：

```python
manifest = read_json(task_dir / "manifest.json", {})
steps = manifest.get("steps", {})
```

改成：

```python
manifest = read_json(task_dir / "manifest.json", {})
manifest = _hydrate_common_progress_for_manifest(task_dir, manifest)
steps = manifest.get("steps", {})
```

这样任务列表和任务详情页都能看到 common 最新进度。

---

# 10. 详细修改方案六：common analysis ready 判断增强

## 10.1 当前问题

`run_multisource_pipeline.py` 里 `_common_analysis_ready()` 只看 manifest 里的状态：

```python
return all(
    steps.get(step, {}).get("status") in {"success", "partial_success", "skipped"}
    for step in COMMON_REUSABLE_STEPS
)
```

如果 `source_aggregate.json` 已经生成，但 manifest 状态没及时更新，就会误报：

```text
common analysis finished but source_aggregate is not ready
```

---

## 10.2 修改文件

```text
run_multisource_pipeline.py
```

---

## 10.3 替换 `_common_analysis_ready()`

新增：

```python
def _latest_version_dir(step_dir: Path) -> Path | None:
    if not step_dir.exists() or not step_dir.is_dir():
        return None

    versions: list[tuple[int, Path]] = []
    for item in step_dir.iterdir():
        if not item.is_dir() or not item.name.startswith("v"):
            continue
        try:
            versions.append((int(item.name[1:]), item))
        except ValueError:
            continue

    if not versions:
        return None

    return sorted(versions, key=lambda item: item[0])[-1][1]


def _common_step_output_exists(common_dir: Path, step: str) -> bool:
    vdir = _latest_version_dir(common_dir / step)
    if not vdir:
        return False

    if step == "source_analysis":
        return (vdir / "source_analysis.json").exists()

    if step == "source_aggregate":
        return (vdir / "source_aggregate.json").exists()

    return (vdir / "step_status.json").exists()


def _common_step_ready(common_dir: Path, step: str) -> bool:
    manifest = read_json(common_dir / "manifest.json", {})
    steps = manifest.get("steps", {}) or {}
    item = steps.get(step, {}) or {}

    if item.get("status") in {"success", "partial_success", "skipped"}:
        output = str(item.get("output") or "").strip()
        if output:
            return (common_dir / output).exists()
        return _common_step_output_exists(common_dir, step)

    return _common_step_output_exists(common_dir, step)


def _common_analysis_ready(common_dir: Path) -> bool:
    return all(_common_step_ready(common_dir, step) for step in COMMON_REUSABLE_STEPS)
```

---

## 10.4 ready 失败时输出 debug

把 `_ensure_common_analysis()` 里这段：

```python
if not _common_analysis_ready(common_dir):
    raise RuntimeError("common analysis finished but source_aggregate is not ready")
```

改成：

```python
if not _common_analysis_ready(common_dir):
    manifest = read_json(common_dir / "manifest.json", {})
    steps = manifest.get("steps", {}) or {}
    debug = {
        step: {
            "status": (steps.get(step) or {}).get("status"),
            "output": (steps.get(step) or {}).get("output"),
            "output_exists": _common_step_output_exists(common_dir, step),
        }
        for step in COMMON_REUSABLE_STEPS
    }
    raise RuntimeError(
        "common analysis finished but common reusable outputs are not ready; "
        f"debug={json.dumps(debug, ensure_ascii=False)}"
    )
```

这样以后日志能直接说明是哪个步骤状态或文件缺失。

---

# 11. 详细修改方案七：公共分析失败写回 manifest

## 11.1 当前问题

如果 `_ensure_common_analysis()` wrapper 抛错，只写日志，不一定写回 common manifest。  
Web 侧可能只看到任务失败，但公共步骤状态还停留在 running 或旧状态。

---

## 11.2 修改文件

```text
run_multisource_pipeline.py
```

## 11.3 新增函数

```python
def _mark_common_failed(common_dir: Path, message: str) -> None:
    manifest_path = common_dir / "manifest.json"
    manifest = read_json(manifest_path, {})
    now = datetime.now().isoformat(timespec="seconds")

    manifest.setdefault("task_id", common_dir.name)
    manifest.setdefault("created_at", now)
    manifest["updated_at"] = now
    manifest["status"] = "failed"
    manifest["user_message"] = message
    manifest["common_wrapper_error"] = {
        "status": "failed",
        "message": message,
        "updated_at": now,
    }

    write_json(manifest_path, manifest)
```

## 11.4 修改 except

原来：

```python
except Exception as exc:
    _append_common_log(common_dir, f"common analysis failed: {exc}")
    raise
```

改成：

```python
except Exception as exc:
    _append_common_log(common_dir, f"common analysis failed: {exc}")
    _mark_common_failed(common_dir, str(exc))
    raise
```

---

# 12. 详细修改方案八：配置默认值优化

## 12.1 修改文件

```text
config.toml
```

## 12.2 推荐默认配置

当前是：

```toml
[multi_source_analysis]
source_max_workers = 3
per_source_vision_max_workers = 2
vision_global_max_workers = 6
text_global_max_workers = 6
```

建议先改成更稳的：

```toml
[multi_source_analysis]
enabled = true

# 同时分析几个源视频。
# 2 通常比 3 更稳，因为每个源视频内部还会跑 ASR、Vision、FFmpeg。
source_max_workers = 2

# 每个源视频内部 Vision chunk 并发数。
# 如果接口稳定、机器性能强，可以改 3。
per_source_vision_max_workers = 2

# 全项目视觉大模型请求总并发上限。
# 防止多视频 × 多 chunk 同时打爆视觉模型接口。
vision_global_max_workers = 4

# 全项目文本大模型请求总并发上限。
# 防止 asr_digest / 内容分析 / 文案规划同时过多请求。
text_global_max_workers = 4

# 单个源失败时，其他源继续跑。
fail_policy = "partial_success"

max_candidates_per_source = 8
max_global_candidate_clips = 40

# 新增：是否在 source_analysis 中写 source 级进度。
progress_enabled = true

# 新增：是否打印 source 级别日志。
verbose_source_logs = true
```

---

## 12.3 为什么不建议一上来拉满并发

如果设置为：

```toml
source_max_workers = 4
per_source_vision_max_workers = 3
```

理论视觉请求可能达到 12 路，同时还会有 ASR 抢 GPU、FFmpeg 抢 CPU/IO。  
这很容易造成：

1. 视觉模型 API 排队。
2. 请求超时。
3. FunASR GPU 显存压力大。
4. FFmpeg 多进程抢磁盘。
5. 日志看起来并发很多，但总体完成时间反而更长。

建议先用：

```text
source_max_workers = 2
per_source_vision_max_workers = 2
vision_global_max_workers = 4
```

跑通稳定后再逐步提高。

---

# 13. 详细修改方案九：Web 前端展示建议

如果只改后端，manifest 已经有：

```json
"completed_sources": 2,
"total_sources": 5,
"source_progress": [...]
```

前端可以进一步展示：

```text
视频分析素材
多源素材分析中：2/5 个视频完成
成功 2，失败 0，运行中 2
```

每个源显示：

```text
source_001 A.mp4 成功 318s 8 chunks
source_002 B.mp4 运行中
source_003 C.mp4 等待中
```

如果前端暂时不改，也可以先让 manifest API 返回这些字段，便于右侧 JSON 查看。

---

# 14. 详细修改方案十：任务日志格式统一

建议统一日志格式：

```text
[source_analysis] queue total=5 source_max_workers=2 per_source_vision_max_workers=2
[source_analysis] start source_001 1/5 video=A.mp4
[source_analysis] start source_002 2/5 video=B.mp4
[source_analysis] done source_001 1/5 elapsed=318.4s chunks=8 video=A.mp4
[source_analysis] progress 1/5 success=1 failed=0 running=1
[source_analysis] failed source_003 3/5 elapsed=92.2s video=C.mp4 error=...
[source_analysis] final status=partial_success success=4 failed=1 elapsed=1022.8s
```

这样排查时不用打开很多 JSON 文件。

---

# 15. 详细修改方案十一：source_analysis 总耗时统计

在 `step_source_analysis()` 开头加：

```python
step_started = time.perf_counter()
```

最终写 `extra`：

```python
extra["elapsed_seconds_total"] = round(time.perf_counter() - step_started, 3)
extra["source_max_workers"] = min(max_workers, len(sources))
extra["per_source_vision_max_workers"] = int(
    cfg.get("per_source_vision_max_workers", self.options.vision_max_workers)
    or self.options.vision_max_workers
)
extra["vision_global_max_workers"] = int(cfg.get("vision_global_max_workers", 0) or 0)
extra["text_global_max_workers"] = int(cfg.get("text_global_max_workers", 0) or 0)
```

这样 manifest 里能看到真实运行参数，后续排查不用猜配置是否生效。

---

# 16. 详细修改方案十二：对失败源给出更清楚的用户提示

当前 `fail_policy = "partial_success"`，单个源失败时可以继续。  
但最终用户需要知道：

```text
哪些视频成功进入分析？
哪些视频失败？
失败原因是什么？
后续结果是否只基于成功视频？
```

建议在 `source_analysis` 最终输出里加入：

```json
{
  "source_count": 5,
  "success_count": 4,
  "failed_count": 1,
  "failed_sources": [
    {
      "source_id": "source_003",
      "display_name": "C.mp4",
      "error": "..."
    }
  ],
  "partial_success_notice": "有 1 个源视频分析失败，后续内容只基于 4 个成功源视频生成。"
}
```

代码：

```python
output_doc = {
    "version": "source_analysis_v1",
    "source_count": len(sources),
    "success_count": len(results),
    "failed_count": len(failures),
    "sources": results,
    "failed_sources": failures,
    "fail_policy": fail_policy,
}

if failures and results:
    output_doc["partial_success_notice"] = (
        f"有 {len(failures)} 个源视频分析失败，"
        f"后续内容只基于 {len(results)} 个成功源视频生成。"
    )

out = write_json(base_dir / "source_analysis.json", output_doc)
```

---

# 17. 详细修改方案十三：避免 source_analysis 重复分析已有成功源

## 17.1 当前问题

`source_analysis` 的复用粒度是整个步骤。  
如果 5 个源视频中 4 个已经成功，1 个失败，用户重跑 `source_analysis` 时，可能又重新跑所有源。

## 17.2 优化目标

按源视频复用：

```text
source_001 已有 source_summary.json 且子 manifest 成功 → 复用
source_002 已成功 → 复用
source_003 失败 → 只重跑 source_003
```

## 17.3 修改思路

在 `analyze_one()` 开头判断：

```python
summary_path = source_dir / "source_summary.json"
source_status_path = source_dir / "source_status.json"
source_status = read_json(source_status_path, {})

if (
    self.options.resume
    and not self.options.rerun
    and summary_path.exists()
    and source_status.get("status") == "success"
):
    summary_doc = read_json(summary_path, {})
    digest = summary_doc.get("timeline_digest") or {}
    return {
        "source_id": source_id,
        "status": "success",
        "display_name": item.get("display_name") or source_path.name,
        "summary": relpath(summary_path, self.task_dir),
        "work_task_dir": summary_doc.get("work_task_dir", ""),
        "chunk_count": len(digest.get("chunks") or []),
        "reused": True,
        "elapsed_seconds": 0,
    }
```

成功后写：

```python
write_json(source_dir / "source_status.json", {
    "source_id": source_id,
    "status": "success",
    "summary": relpath(summary_path, self.task_dir),
    "updated_at": now_iso(),
    "elapsed_seconds": elapsed,
    "chunk_count": chunk_count,
})
```

失败后写：

```python
write_json(source_dir / "source_status.json", {
    "source_id": source_id,
    "status": "failed",
    "error": str(exc),
    "updated_at": now_iso(),
    "elapsed_seconds": elapsed,
})
```

## 17.4 收益

这个优化很关键。  
如果多视频任务跑到第 11 分钟失败，用户重跑时不用所有视频从头来，能极大减少重复等待。

---

# 18. 详细修改方案十四：Web 提交层增加“复用公共分析”的提示

当前 `web_app.py` 已经有 common reuse 逻辑：

```python
if not force_rerun_common and _common_analysis_ready(common_dir):
    print(f"Reuse common analysis outputs: {common_dir}")
    return
```

建议在 Web job 返回里增加：

```json
{
  "common_task_id": "common_xxx",
  "common_source_key": "xxx",
  "common_reuse_possible": true
}
```

让前端可以显示：

```text
相同素材公共分析已存在，将复用 source_analysis / source_aggregate。
```

这不是必须，但能让用户理解为什么某些任务很快，某些任务很慢。

---

# 19. 实施顺序建议

建议按下面顺序做，不要一次性改太多：

## 第一阶段：可观测性修复

优先级最高，风险最低。

```text
1. pipeline.py：source_analysis 增加 source_id 日志
2. pipeline.py：source_analysis 中间进度写 manifest
3. web_app.py：manifest / task list hydrate common progress
4. run_multisource_pipeline.py：common ready 判断增强
```

完成后，用户就能看到：

```text
到底几个视频在跑
完成了几个
哪个失败
是不是卡在某个 source
```

---

## 第二阶段：稳定性修复

```text
5. run_multisource_pipeline.py：公共失败写 manifest
6. config.toml：默认并发调稳
7. pipeline.py：source_analysis 总耗时和运行参数写 manifest
```

完成后，错误状态和 UI 状态会明显一致。

---

## 第三阶段：资源调度优化

```text
8. pipeline.py：Vision 全局 file_slot_lock
9. pipeline.py：Text LLM 全局 file_slot_lock
```

完成后，多视频任务不会轻易打爆 API / GPU / IO。

---

## 第四阶段：断点复用优化

```text
10. pipeline.py：按 source 复用 source_summary.json
11. pipeline.py：失败源单独记录 source_status.json
```

完成后，失败重跑的体验会明显改善。

---

# 20. 验证方法

## 20.1 验证并发是否生效

提交 3 个视频后，查看 Web job log，应看到：

```text
[source_analysis] queue total=3 source_max_workers=2
[source_analysis] start source_001 1/3 video=A.mp4
[source_analysis] start source_002 2/3 video=B.mp4
```

如果 `source_max_workers=2`，一开始应最多看到两个 source start。  
当其中一个 done 后，才会看到第三个 start。

---

## 20.2 验证 manifest 进度

打开：

```text
outputs/<task_id>/manifest.json
```

应看到：

```json
"source_analysis": {
  "status": "running",
  "message": "多源素材分析中：1/3 个视频完成",
  "completed_sources": 1,
  "total_sources": 3,
  "success_count": 1,
  "failed_count": 0,
  "running_count": 1,
  "source_progress": [...]
}
```

---

## 20.3 验证 common hydrate

打开：

```text
outputs/__common__/common_xxx/manifest.json
```

和：

```text
outputs/<task_id>/manifest.json
```

公共步骤状态应基本一致：

```text
source_prepare
source_analysis
source_aggregate
```

如果 common 里 source_analysis 有 completed_sources，子任务里也应该能看到。

---

## 20.4 验证 ready 判断

如果日志出现：

```text
完成: source_aggregate
```

不应再误报：

```text
common analysis finished but source_aggregate is not ready
```

除非真实缺少：

```text
source_aggregate/v*/source_aggregate.json
```

---

## 20.5 验证失败源复用

准备 3 个视频，其中 1 个损坏。  
第一次运行：

```text
source_001 success
source_002 failed
source_003 success
```

修复 source_002 后重跑，预期：

```text
source_001 reused
source_002 running
source_003 reused
```

不用全部重跑。

---

# 21. 推荐最终配置

先用稳态配置：

```toml
[multi_source_analysis]
enabled = true
source_max_workers = 2
per_source_vision_max_workers = 2
vision_global_max_workers = 4
text_global_max_workers = 4
fail_policy = "partial_success"
max_candidates_per_source = 8
max_global_candidate_clips = 40
progress_enabled = true
verbose_source_logs = true
```

如果机器和接口稳定，再试：

```toml
source_max_workers = 3
per_source_vision_max_workers = 2
vision_global_max_workers = 6
text_global_max_workers = 6
```

不建议直接上：

```toml
source_max_workers = 4
per_source_vision_max_workers = 3
```

除非已经确认：

```text
GPU 显存足够
FunASR 并发无问题
视觉 API QPS 足够
文本 API QPS 足够
磁盘 IO 没压力
```

---

# 22. 最终预期效果

完成本方案后，多视频任务会有以下改善：

1. 用户能看出多个视频是否真的并发。
2. source_analysis 不再长时间只有“处理中”。
3. 日志里能看到每个视频开始、完成、失败、耗时。
4. Web 左侧任务进度和右侧运行日志更一致。
5. common 分析完成后不容易被 manifest 瞬时状态误判为失败。
6. 多视频并发更稳，不会盲目把 API / GPU 打满。
7. 失败重跑时可以只重跑失败源，减少重复等待。
8. 现有 `python run_web.py` 启动方式不变。
9. 单视频流程基本不受影响。
10. 后续如果要做“两阶段 ASR 初筛再 Vision”，可以在这个稳定基础上继续演进。

---

# 23. 最小落地版本

如果时间有限，建议至少做这 5 个改动：

```text
1. pipeline.py：source_analysis 打印 source start/done/failed 日志
2. pipeline.py：source_analysis 写 completed_sources / total_sources / source_progress
3. web_app.py：读取 manifest 时 hydrate common progress
4. run_multisource_pipeline.py：_common_analysis_ready() 增强为“状态 + 文件存在”双判断
5. config.toml：source_max_workers=2，vision_global_max_workers=4，text_global_max_workers=4
```

这 5 个改完后，你现在遇到的“11 分钟不知道在跑什么”“日志和进度对不上”“source_aggregate 明明完成却说不 ready”会明显改善。
