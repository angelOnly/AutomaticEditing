下面是一份**合并版文档**，把你这次提到的几个问题放在一起分析，并明确修改顺序。你可以直接复制给 Codex。

````md
# AutomaticEditing：AI配音Bug、任务分组展示、终止按钮、公共模块日志优化方案

仓库：`angelOnly/AutomaticEditing`  
分支：`clean-highlight-reassembly`  
入口：`python run_web.py`  
项目：凤凰新闻视频智能拆条系统

---

## 0. 本次要解决的问题

这次集中修 4 个问题：

1. **AI配音解说有 bug**  
   当前 AI 配音解说在 `content_analysis` 后可能报错：

   ```text
   'video_understanding' is not in list
   ```

2. **任务列表展示不好看**  
   同一批视频源现在被展示成两个平级任务：

   ```text
   多源剪辑_2段_AI配音解说_xxx
   多源剪辑_2段_视频重组_xxx
   ```

   但用户期望是：

   ```text
   多源剪辑（2段）
     ├─ AI配音解说
     └─ 视频重组
   ```

3. **终止任务按钮交互不对**  
   当前按钮只在运行时显示。用户希望：

   ```text
   终止任务按钮一直显示
   无运行任务时：灰色 disabled
   有 pending/running 任务时：红色可点击
   点击后：终止中，不可重复点击
   ```

4. **公共模块没有 web_jobs 日志目录**  
   单任务下面有：

   ```text
   web_jobs/job_xxx.log
   last_web_job.json
   ```

   但公共目录：

   ```text
   outputs/__common__/common_xxx/
   ```

   下面没有 `web_jobs` 日志，排查公共分析时不方便。

---

# 1. 当前代码现状

## 1.1 Web 入口

`run_web.py` 仍然是项目入口，只负责启动 `web_app:app`：

```python
uvicorn.run("web_app:app", host=host, port=port, reload=False)
```

所以本次所有修改必须兼容：

```bash
python run_web.py
```

不要新增启动命令，不要改变入口。:contentReference[oaicite:0]{index=0}

---

## 1.2 多源任务现在已经有 common 机制

`web_app.py` 的 `/api/run` 多源分支现在已经会计算：

```python
common_source_key = _common_source_key(req, raw_source_items)
common_task_id = f"common_{common_source_key}"
```

并写入 `source_request.json`：

```python
"common_source_key": common_source_key,
"common_task_id": common_task_id,
"production_mode": req.production_mode,
```

也就是说，同一批视频源已经具备“同源任务分组”的基础字段。:contentReference[oaicite:1]{index=1}

---

## 1.3 `run_multisource_pipeline.py` 当前 common 执行方式

当前多源 runner 的主逻辑是：

```python
common_task_id = str(request.get("common_task_id") or "").strip()
if common_task_id:
    common_dir = ensure_dir(COMMON_OUTPUTS_DIR / common_task_id)
    _ensure_common_analysis(common_dir=common_dir, request=request, args=args)
    _copy_common_outputs_to_task(common_dir=common_dir, task_dir=task_dir, request=request)
    return _run_mode_specific_pipeline(...)
```

也就是：

```text
先执行 outputs/__common__/common_xxx 公共分析
再把公共产物复制到具体生产任务
最后生产任务从 content_analysis 或 video_understanding 继续
```

这说明功能层面已经实现了 common 复用，但展示层还是平铺任务。:contentReference[oaicite:2]{index=2}

---

## 1.4 AI 配音步骤链路现状

`AI_VOICEOVER_STEP_ORDER` 当前是新版：

```python
AI_VOICEOVER_STEP_ORDER = [
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "vision",
    "timeline",
    "timeline_digest",
    "content_analysis",
    "short_video_edit_plan",
    "voiceover_script",
    "tts",
    "subtitles",
    "cut_plan",
    "render",
]
```

里面没有：

```text
video_understanding
highlight_detection
short_video_planning
editing_script
```

这是合理的，因为新版 AI 配音链路已经用：

```text
content_analysis
short_video_edit_plan
```

替代了旧链路。:contentReference[oaicite:3]{index=3}

---

## 1.5 但是 AI 配音仍会写旧链路兼容文件

`step_content_analysis()` 会执行：

```python
result = self._run_text_agent("content_analysis", ...)
self._write_compat_content_analysis(result)
```

而 `_write_compat_content_analysis()` 会额外写：

```text
video_understanding
highlight_detection
```

用于兼容旧 UI / 旧文件结构。:contentReference[oaicite:4]{index=4} :contentReference[oaicite:5]{index=5}

这就是 AI 配音 bug 的根源。

---

## 1.6 终止按钮现状

`index.html` 里按钮现在是：

```html
<button id="stopJob" class="button danger hidden" type="button">
  <i data-lucide="square"></i><span>终止任务</span>
</button>
```

它默认有 `hidden`。:contentReference[oaicite:6]{index=6}

`app.js` 中 `updateStopJobButton()` 当前逻辑是根据 `state.activeJob` 控制隐藏/显示：

```javascript
button.classList.toggle("hidden", !active);
button.disabled = !active;
```

所以没运行任务时按钮直接消失。用户期望不是这样。:contentReference[oaicite:7]{index=7}

---

## 1.7 common 没有 web_jobs 日志的原因

单个生产任务的 `web_jobs` 日志是由 `web_app.py` 的 `/api/run` 创建的：

```python
log_dir = ensure_dir(task_dir / "web_jobs")
log_path = log_dir / f"{job_id}.log"
```

这个 `task_dir` 是生产任务目录，比如：

```text
outputs/多源剪辑_2段_AI配音解说_xxx/
outputs/多源剪辑_2段_视频重组_xxx/
```

所以生产任务有：

```text
web_jobs/job_xxx.log
```

但 common 目录不是由 Web 直接创建 job 执行的。common 是在 `run_multisource_pipeline.py` 的子流程里被内部调用：

```python
_ensure_common_analysis(common_dir=common_dir, ...)
```

它没有走 `web_app.py` 的 `_start_job()`，所以不会自动创建：

```text
outputs/__common__/common_xxx/web_jobs/
```

这就是你截图里 common 目录没有 `web_jobs` 的原因。:contentReference[oaicite:8]{index=8} :contentReference[oaicite:9]{index=9}

---

# 2. 修改顺序

必须按下面顺序改：

```text
第一步：修 AI 配音解说 bug
  1. 修 _mark_downstream_stale 防御
  2. 修兼容输出不触发 stale
  3. 给 HIGHLIGHT_REASSEMBLY_STEP_ORDER 补 timeline_digest
  4. 修 timeline_digest 乱码日志

第二步：补 common 日志
  5. run_multisource_pipeline.py 给 common_dir 写 web_jobs/common_analysis.log
  6. web_app.py 的文件树展示 common 日志入口，或者至少 common 目录里实际存在日志

第三步：任务列表分组展示
  7. web_app.py /api/tasks 返回 group_id/group_title/mode_title/source_names
  8. web_static/app.js 按 group_id 分组渲染父任务和子任务
  9. web_static/styles.css 增加分组样式

第四步：终止按钮交互
  10. index.html 去掉 stopJob 的 hidden，默认 disabled
  11. app.js 修改 updateStopJobButton，按钮永远显示
  12. styles.css 增加 disabled 灰色样式

第五步：验收
  13. 验证 AI 配音不再报 video_understanding is not in list
  14. 验证 common 有日志
  15. 验证同源任务聚合展示
  16. 验证终止按钮始终显示
```

---

# 3. 第一部分：修 AI 配音解说 bug

---

## 3.1 问题原因

AI 配音模式下，当前 active step order 是：

```python
AI_VOICEOVER_STEP_ORDER
```

里面没有：

```python
video_understanding
highlight_detection
```

但 `content_analysis` 成功后会写兼容输出：

```python
_write_compat_agent_output("video_understanding", ...)
_write_compat_agent_output("highlight_detection", ...)
```

这些兼容输出调用 `_record_step()`。

`_record_step()` 成功后会调用：

```python
self._mark_downstream_stale(step)
```

`_mark_downstream_stale()` 里执行：

```python
idx = step_order.index(step)
```

当 step 是 `video_understanding`，但当前 `step_order` 是 AI 配音链路，就会报：

```text
'video_understanding' is not in list
```

---

## 3.2 修改文件

```text
newsclip_agent/pipeline.py
```

---

## 3.3 修改 `_mark_downstream_stale()`

找到：

```python
def _mark_downstream_stale(self, step: str) -> None:
    if self.options.rerun != step and not self.options.rerun_from:
        return
    step_order = self._active_step_order()
    idx = step_order.index(step)
    for downstream in step_order[idx + 1 :]:
        entry = self.manifest.setdefault("steps", {}).get(downstream)
        if entry and entry.get("status") == "success":
            entry["status"] = "stale"
            entry["reason"] = f"upstream {step} updated"
```

替换为：

```python
def _mark_downstream_stale(self, step: str) -> None:
    if self.options.rerun != step and not self.options.rerun_from:
        return

    step_order = self._active_step_order()

    # 兼容输出可能不属于当前生产模式的 active step order。
    # 例如 AI 配音模式会兼容写 video_understanding/highlight_detection，
    # 但 AI_VOICEOVER_STEP_ORDER 不包含这两个 step。
    # 此时不能做 step_order.index(step)，否则会报：
    # ValueError: 'video_understanding' is not in list
    if step not in step_order:
        return

    idx = step_order.index(step)
    for downstream in step_order[idx + 1 :]:
        entry = self.manifest.setdefault("steps", {}).get(downstream)
        if entry and entry.get("status") == "success":
            entry["status"] = "stale"
            entry["reason"] = f"upstream {step} updated"
```

这是最小必要修复。

---

## 3.4 修改 `_record_step()`，支持不触发下游 stale

找到函数签名：

```python
def _record_step(
    self,
    *,
    step: str,
    version: str,
    status: str,
    output: str | None,
    input_hash: str,
    output_files: list[Path] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
```

改为：

```python
def _record_step(
    self,
    *,
    step: str,
    version: str,
    status: str,
    output: str | None,
    input_hash: str,
    output_files: list[Path] | None = None,
    extra: dict[str, Any] | None = None,
    mark_downstream_stale: bool = True,
) -> None:
```

找到：

```python
if status in {"success", "partial_success"}:
    self.manifest.setdefault("current_versions", {})[step] = version
    self._mark_downstream_stale(step)
```

替换为：

```python
if status in {"success", "partial_success"}:
    self.manifest.setdefault("current_versions", {})[step] = version
    if mark_downstream_stale:
        self._mark_downstream_stale(step)
```

---

## 3.5 修改 `_write_compat_agent_output()`

找到：

```python
self._record_step(
    step=step,
    version=version,
    status="success",
    output=relpath(out, self.task_dir),
    input_hash=input_hash,
    output_files=[out],
    extra={"compat_source_step": source_step, "prompt_version": prompt_version},
)
```

替换为：

```python
self._record_step(
    step=step,
    version=version,
    status="success",
    output=relpath(out, self.task_dir),
    input_hash=input_hash,
    output_files=[out],
    extra={
        "compat_source_step": source_step,
        "prompt_version": prompt_version,
        "compat_output": True,
    },
    mark_downstream_stale=False,
)
```

---

## 3.6 修改 `HIGHLIGHT_REASSEMBLY_STEP_ORDER`

当前代码里 `DEPENDENCIES` 已经让 `video_understanding` 依赖 `timeline_digest`，但 `HIGHLIGHT_REASSEMBLY_STEP_ORDER` 里没有 `timeline_digest`。这会导致视频重组完整独立运行时有潜在错误。:contentReference[oaicite:10]{index=10}

找到：

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

替换为：

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

---

## 3.7 修 `step_timeline_digest()` 乱码

找到：

```python
print("澶嶇敤缂撳瓨: timeline_digest")
```

替换为：

```python
print("复用缓存: timeline_digest")
```

找到：

```python
print("瀹屾垚: timeline_digest")
```

替换为：

```python
print("完成: timeline_digest")
```

---

# 4. 第二部分：补 common 公共模块日志

---

## 4.1 问题原因

生产任务日志是 Web job 的日志：

```text
outputs/<task_id>/web_jobs/job_xxx.log
```

但 common 不是独立 Web job，它是 `run_multisource_pipeline.py` 内部执行的公共分析，所以没有自动生成：

```text
outputs/__common__/common_xxx/web_jobs/
```

这不影响功能，但不利于排查 common 里的 ASR、vision、timeline 执行情况。

---

## 4.2 修改文件

```text
run_multisource_pipeline.py
```

---

## 4.3 新增 common 日志工具函数

在 `run_multisource_pipeline.py` 里新增：

```python
def _append_common_log(common_dir: Path, message: str) -> None:
    log_dir = ensure_dir(common_dir / "web_jobs")
    log_path = log_dir / "common_analysis.log"
    timestamp = datetime.now().isoformat(timespec="seconds")
    with log_path.open("a", encoding="utf-8", errors="replace") as f:
        f.write(f"[{timestamp}] {message}\n")
```

---

## 4.4 修改 `_ensure_common_analysis()`

找到：

```python
print(f"Reuse common analysis outputs: {common_dir}")
return
```

改为：

```python
print(f"Reuse common analysis outputs: {common_dir}")
_append_common_log(common_dir, "reuse common analysis outputs")
return
```

找到：

```python
print(f"Start common analysis: {common_dir}")
_mark_source_prepare(common_dir, "running", request=request)
```

改为：

```python
print(f"Start common analysis: {common_dir}")
_append_common_log(common_dir, "start common analysis")
_mark_source_prepare(common_dir, "running", request=request)
```

在 `build_multi_source_video()` 成功后加：

```python
_append_common_log(common_dir, f"source_prepare success: {built.get('input_video')}")
```

在 `pipeline_main(pipeline_args)` 前加：

```python
_append_common_log(common_dir, "run common pipeline: " + " ".join(map(str, pipeline_args)))
```

在 `pipeline_main()` 后加：

```python
_append_common_log(common_dir, f"common pipeline finished with return code {result}")
```

异常处：

```python
except Exception as exc:
    _mark_source_prepare(common_dir, "failed", request=request, error=str(exc))
    raise
```

改为：

```python
except Exception as exc:
    _append_common_log(common_dir, f"common analysis failed: {exc}")
    _mark_source_prepare(common_dir, "failed", request=request, error=str(exc))
    raise
```

---

## 4.5 更推荐的完整 `_ensure_common_analysis()` 结构

```python
def _ensure_common_analysis(*, common_dir: Path, request: dict[str, Any], args: argparse.Namespace) -> None:
    ensure_dir(common_dir)
    ensure_dir(common_dir / "web_jobs")

    lock_name = f"common_source_{common_dir.name}"

    with file_slot_lock(lock_name, slots=1):
        if _common_analysis_ready(common_dir):
            print(f"Reuse common analysis outputs: {common_dir}")
            _append_common_log(common_dir, "reuse common analysis outputs")
            return

        print(f"Start common analysis: {common_dir}")
        _append_common_log(common_dir, "start common analysis")
        _mark_source_prepare(common_dir, "running", request=request)

        try:
            config = load_config(ROOT / "config.toml")
            sources = _resolve_sources(request, config)
            multi_source_config = config.raw.get("multi_source", {})

            built = build_multi_source_video(
                sources=sources,
                task_dir=common_dir,
                root_dir=ROOT,
                aspect_ratio=str(request.get("aspect_ratio") or args.aspect_ratio),
                fps=int(multi_source_config.get("fps", 25)),
                sample_rate=int(multi_source_config.get("sample_rate", 48000)),
            )

            _append_common_log(common_dir, f"source_prepare success: {built.get('input_video')}")
            _mark_source_prepare(common_dir, "success", request=request, built=built)

        except Exception as exc:
            _append_common_log(common_dir, f"source_prepare failed: {exc}")
            _mark_source_prepare(common_dir, "failed", request=request, error=str(exc))
            raise

        pipeline_args = [
            "--task-id",
            common_dir.name,
            "--outputs-dir",
            str(common_dir.parent),
            "--input",
            str(built["input_video"]),
            "--source-manifest",
            str(built["source_manifest"]),
            "--aspect-ratio",
            str(request.get("aspect_ratio") or args.aspect_ratio),
            "--chunk-seconds",
            str(request.get("chunk_seconds") or 60),
            "--frame-interval",
            str(request.get("frame_interval") or 10),
            "--mode",
            str(request.get("mode") or "normal"),
            "--production-mode",
            "ai_voiceover",
            "--common-only",
        ]

        _append_common_log(common_dir, "run common pipeline: " + " ".join(map(str, pipeline_args)))
        result = pipeline_main(pipeline_args)
        _append_common_log(common_dir, f"common pipeline finished with return code {result}")

        if result != 0:
            raise RuntimeError(f"common analysis failed with return code {result}")

        if not _common_analysis_ready(common_dir):
            raise RuntimeError("common analysis finished but timeline_digest is not ready")

        _append_common_log(common_dir, "common analysis ready")
```

---

## 4.6 验收

重新跑多源任务后，common 目录应该出现：

```text
outputs/__common__/common_xxx/web_jobs/common_analysis.log
```

内容类似：

```text
[2026-06-02T22:51:01] start common analysis
[2026-06-02T22:51:02] source_prepare success: ...
[2026-06-02T22:51:02] run common pipeline: ...
[2026-06-02T22:58:40] common pipeline finished with return code 0
[2026-06-02T22:58:40] common analysis ready
```

---

# 5. 第三部分：任务列表分组展示

---

## 5.1 展示目标

当前实际目录可以继续保持：

```text
outputs/
  __common__/common_xxx/
  多源剪辑_2段_AI配音解说_xxx/
  多源剪辑_2段_视频重组_xxx/
```

但 Web 左侧任务队列展示成：

```text
多源剪辑（2段）
  ├─ AI配音解说    10/16 完成 · 失败 0
  └─ 视频重组      13/13 完成 · 失败 0
```

不要现在强行把物理目录改成父子目录。原因是当前大量 API 都是按：

```python
OUTPUTS_DIR / task_id
```

找任务目录，强改物理结构风险很大。

---

## 5.2 修改文件

```text
web_app.py
web_static/app.js
web_static/styles.css
```

---

## 5.3 后端增加任务分组字段

### 5.3.1 在 `web_app.py` 新增 `_task_group_meta()`

放在 `list_tasks()` 附近：

```python
def _task_group_meta(task_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    source_request = read_json(task_dir / "input" / "source_request.json", {})
    opts = manifest.get("last_web_run_options") or manifest.get("last_run_options") or {}

    production_mode = (
        manifest.get("production_mode")
        or opts.get("production_mode")
        or source_request.get("production_mode")
        or _task_id_production_mode(task_dir.name)
        or "ai_voiceover"
    )

    common_source_key = (
        manifest.get("common_source_key")
        or opts.get("common_source_key")
        or source_request.get("common_source_key")
        or ""
    )

    common_task_id = (
        manifest.get("common_task_id")
        or opts.get("common_task_id")
        or source_request.get("common_task_id")
        or ""
    )

    source_items = source_request.get("source_items") or []
    source_names: list[str] = []

    for index, item in enumerate(source_items, start=1):
        if not isinstance(item, dict):
            continue

        if item.get("display_name"):
            source_names.append(str(item["display_name"]))
            continue

        if item.get("path"):
            source_names.append(Path(str(item["path"])).name)
            continue

        remote = item.get("remote_video") if isinstance(item.get("remote_video"), dict) else item
        source_names.append(
            str(
                remote.get("display_name")
                or remote.get("name")
                or remote.get("title")
                or remote.get("id")
                or f"素材 {index}"
            )
        )

    source_count = len(source_names) or len(manifest.get("source_videos") or []) or 1

    if common_source_key:
        group_id = f"group_{common_source_key}"
    else:
        source_video = str(manifest.get("source_video") or "")
        group_id = (
            f"group_{hashlib.sha256(source_video.encode('utf-8')).hexdigest()[:16]}"
            if source_video
            else f"group_{task_dir.name}"
        )

    if source_count > 1:
        group_title = f"多源剪辑（{source_count}段）"
    elif source_names:
        group_title = Path(source_names[0]).stem
    else:
        group_title = _display_title_from_task_id(task_dir.name)

    return {
        "group_id": group_id,
        "group_title": group_title,
        "source_count": source_count,
        "source_names": source_names,
        "production_mode": production_mode,
        "mode_title": _mode_slug(production_mode),
        "common_source_key": common_source_key,
        "common_task_id": common_task_id,
    }


def _display_title_from_task_id(task_id: str) -> str:
    value = task_id
    for alias in _all_mode_aliases():
        value = value.replace(f"_{alias}_", "_")
        if value.endswith(f"_{alias}"):
            value = value[: -len(alias) - 1]
    return value
```

---

### 5.3.2 修改 `list_tasks()` 返回字段

在构造每个 task dict 前加：

```python
group_meta = _task_group_meta(task_dir, manifest)
```

然后在返回 dict 中增加：

```python
"common_task_id": group_meta["common_task_id"],
"common_source_key": group_meta["common_source_key"],
"group_id": group_meta["group_id"],
"group_title": group_meta["group_title"],
"source_count": group_meta["source_count"],
"source_names": group_meta["source_names"],
"production_mode": group_meta["production_mode"],
"mode_title": group_meta["mode_title"],
```

最终每个任务对象至少要包含：

```python
{
    "task_id": task_dir.name,
    "done_steps": done,
    "failed_steps": failed,
    "step_count": len(steps),
    "job_status": job_status,
    "common_task_id": group_meta["common_task_id"],
    "common_source_key": group_meta["common_source_key"],
    "group_id": group_meta["group_id"],
    "group_title": group_meta["group_title"],
    "source_count": group_meta["source_count"],
    "source_names": group_meta["source_names"],
    "production_mode": group_meta["production_mode"],
    "mode_title": group_meta["mode_title"],
}
```

---

## 5.4 前端按 group_id 分组渲染

### 5.4.1 新增 `groupTasks()`

在 `web_static/app.js` 的 `loadTasks()` 前新增：

```javascript
function groupTasks(tasks) {
  const groups = new Map();

  tasks.forEach((task) => {
    const groupId = task.group_id || task.common_source_key || task.task_id;

    if (!groups.has(groupId)) {
      groups.set(groupId, {
        group_id: groupId,
        group_title: task.group_title || displayTaskId(task.task_id),
        source_count: task.source_count || 1,
        source_names: task.source_names || [],
        common_task_id: task.common_task_id || "",
        common_source_key: task.common_source_key || "",
        updated_at: task.updated_at || "",
        job_status: "",
        failed_steps: 0,
        done_steps: 0,
        step_count: 0,
        children: [],
      });
    }

    const group = groups.get(groupId);
    group.children.push(task);

    if ((task.updated_at || "") > (group.updated_at || "")) {
      group.updated_at = task.updated_at;
    }

    group.failed_steps += Number(task.failed_steps || 0);
    group.done_steps += Number(task.done_steps || 0);
    group.step_count += Number(task.step_count || 0);

    if (task.job_status === "running") {
      group.job_status = "running";
    } else if (task.job_status === "pending" && group.job_status !== "running") {
      group.job_status = "pending";
    }
  });

  return Array.from(groups.values()).sort((a, b) =>
    String(b.updated_at || "").localeCompare(String(a.updated_at || ""))
  );
}


function preferredModeOrder(mode) {
  if (mode === "ai_voiceover") return 1;
  if (mode === "highlight_reassembly") return 2;
  return 99;
}
```

---

### 5.4.2 替换 `loadTasks()`

找到当前 `loadTasks()`，替换为：

```javascript
async function loadTasks() {
  const tasks = await api("/api/tasks");
  const box = $("taskList");

  box.innerHTML = tasks.length
    ? ""
    : `<div class="list-item"><strong>暂无任务</strong><span>选择素材后开始分析</span></div>`;

  const groups = groupTasks(tasks);

  groups.forEach((group) => {
    const groupEl = document.createElement("div");
    groupEl.className = `task-group ${group.job_status ? `job-${group.job_status}` : ""}`;

    const runningBadge = group.job_status
      ? `<em class="task-job-badge ${escapeAttr(group.job_status)}">${escapeHtml(jobStatusLabel(group.job_status))}</em>`
      : "";

    const sourceTitle = group.source_names?.length
      ? group.source_names.slice(0, 2).join(" / ") + (group.source_names.length > 2 ? ` 等 ${group.source_names.length} 段` : "")
      : `${group.source_count || 1} 段素材`;

    groupEl.innerHTML = `
      <div class="task-group-header">
        <div>
          <strong>${escapeHtml(group.group_title || "任务组")}</strong>
          <span>${escapeHtml(sourceTitle)} · ${group.done_steps}/${group.step_count} 完成 · ${group.failed_steps} 失败 · ${group.updated_at || "-"}</span>
        </div>
        ${runningBadge}
      </div>
      <div class="task-group-children"></div>
    `;

    const childrenBox = groupEl.querySelector(".task-group-children");
    const children = [...group.children].sort(
      (a, b) => preferredModeOrder(a.production_mode) - preferredModeOrder(b.production_mode)
    );

    children.forEach((task) => {
      const child = document.createElement("button");
      child.className = `task-child ${state.selectedTask === task.task_id ? "active" : ""} ${task.job_status ? `job-${task.job_status}` : ""}`;
      child.type = "button";

      const childBadge = task.job_status
        ? `<em class="task-job-badge ${escapeAttr(task.job_status)}">${escapeHtml(jobStatusLabel(task.job_status))}</em>`
        : "";

      child.innerHTML = `
        <span class="task-child-mode">${escapeHtml(task.mode_title || productionModeName(task.production_mode))}</span>
        <span class="task-child-status">${task.done_steps}/${task.step_count} · 失败 ${task.failed_steps}</span>
        ${childBadge}
      `;

      child.addEventListener("click", async (event) => {
        event.stopPropagation();

        state.selectedTask = task.task_id;
        state.selectedVideo = null;
        state.selectedRemoteVideo = null;
        state.sourceBasket = [];

        renderSourceBasket();

        $("taskIdInput").value = task.task_id;
        $("activeTitle").textContent = `${group.group_title || displayTaskId(task.task_id)} / ${task.mode_title || productionModeName(task.production_mode)}`;
        updateTaskSubtitle(task.task_id);

        await loadManifest(task.task_id);
        await loadTree();

        loadVideos();
        renderRemoteVideos();
        loadTasks();
      });

      attachContextDelete(child, () => deleteTask(task));
      childrenBox.appendChild(child);
    });

    box.appendChild(groupEl);
  });

  lucide.createIcons();
}
```

---

## 5.5 增加 CSS

在 `web_static/styles.css` 增加：

```css
.task-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.75);
  border: 1px solid rgba(148, 163, 184, 0.25);
  margin-bottom: 10px;
}

.task-group-header {
  width: 100%;
  display: flex;
  justify-content: space-between;
  gap: 8px;
  align-items: center;
  text-align: left;
  padding: 4px;
}

.task-group-header strong {
  display: block;
  font-size: 14px;
  color: #0f172a;
  margin-bottom: 3px;
}

.task-group-header span {
  display: block;
  font-size: 12px;
  color: #64748b;
  line-height: 1.4;
}

.task-group-children {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.task-child {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  gap: 8px;
  border: 1px solid rgba(148, 163, 184, 0.22);
  background: #ffffff;
  border-radius: 10px;
  padding: 8px 10px;
  text-align: left;
  cursor: pointer;
}

.task-child:hover {
  background: #f8fafc;
}

.task-child.active {
  border-color: #2563eb;
  background: #eff6ff;
}

.task-child-mode {
  font-weight: 700;
  color: #0f172a;
}

.task-child-status {
  font-size: 12px;
  color: #64748b;
}

.task-group.job-running,
.task-child.job-running {
  border-color: rgba(37, 99, 235, 0.55);
}

.task-group.job-pending,
.task-child.job-pending {
  border-color: rgba(245, 158, 11, 0.55);
}
```

---

# 6. 第四部分：终止任务按钮始终显示

---

## 6.1 修改目标

按钮显示逻辑改成：

```text
永远显示终止任务按钮
无 activeJob：灰色 disabled
有 activeJob：红色可点击
点击后：按钮 disabled，文字可以显示“终止中...”
终止完成：按钮恢复灰色 disabled
```

---

## 6.2 修改 `web_static/index.html`

找到：

```html
<button id="stopJob" class="button danger hidden" type="button">
  <i data-lucide="square"></i><span>终止任务</span>
</button>
```

替换为：

```html
<button id="stopJob" class="button danger" type="button" disabled>
  <i data-lucide="square"></i><span>终止任务</span>
</button>
```

---

## 6.3 修改 `web_static/app.js`

### 6.3.1 修改 `updateStopJobButton()`

找到：

```javascript
function updateStopJobButton() {
  const button = $("stopJob");
  if (!button) return;
  const active = Boolean(state.activeJob);
  button.classList.toggle("hidden", !active);
  button.disabled = !active;
}
```

替换为：

```javascript
function updateStopJobButton() {
  const button = $("stopJob");
  if (!button) return;

  const active = Boolean(state.activeJob);

  // 按钮永远显示，只控制可点击状态
  button.classList.remove("hidden");
  button.disabled = !active;
  button.classList.toggle("disabled", !active);
}
```

---

### 6.3.2 修改 `cancelActiveJob()`

找到当前函数，替换为：

```javascript
async function cancelActiveJob() {
  if (!state.activeJob) return;

  const jobId = state.activeJob;
  const button = $("stopJob");
  const label = button?.querySelector("span");

  if (button) button.disabled = true;
  if (label) label.textContent = "终止中...";

  try {
    const job = await api(`/api/jobs/${jobId}`, { method: "DELETE" });

    state.activeJob = null;

    if (state.logTimer) {
      clearInterval(state.logTimer);
      state.logTimer = null;
    }

    $("metricJob").textContent = jobStatusLabel(job.status);
    updateJobMessage(job.user_message || "任务已取消。");

    await refreshActiveTaskSnapshot(job.task_id).catch(() => {});
    await refreshLog().catch(() => {});
    await loadTasks().catch(() => {});
  } finally {
    if (label) label.textContent = "终止任务";
    setRunControlsBusy(false);
    updateStopJobButton();
  }
}
```

---

### 6.3.3 修改 `init()`

找到：

```javascript
function init() {
  fillStepSelect();
  bindEvents();
  syncPreviewTabs();
  renderSourceBasket();
  loadConfig().then(refreshAll);
  startAutoRefresh();
  lucide.createIcons();
}
```

替换为：

```javascript
function init() {
  fillStepSelect();
  bindEvents();
  syncPreviewTabs();
  renderSourceBasket();
  updateStopJobButton();
  loadConfig().then(refreshAll);
  startAutoRefresh();
  lucide.createIcons();
}
```

---

### 6.3.4 修改 `clearTaskPanels()`

找到：

```javascript
state.activeJob = null;
```

后面加：

```javascript
updateStopJobButton();
```

完整局部：

```javascript
function clearTaskPanels() {
  if (state.logTimer) clearInterval(state.logTimer);
  state.logTimer = null;
  state.activeJob = null;
  updateStopJobButton();

  ...
}
```

---

## 6.4 修改 `web_static/styles.css`

增加：

```css
.button.danger:disabled,
.button.danger.disabled {
  background: #e5e7eb;
  border-color: #d1d5db;
  color: #9ca3af;
  cursor: not-allowed;
  filter: none;
  opacity: 1;
}

.button.danger:not(:disabled) {
  background: #b1122a;
  border-color: #b1122a;
  color: #fff;
}

.button.danger:not(:disabled):hover {
  filter: brightness(0.95);
}
```

---

# 7. 验收清单

---

## 7.1 AI 配音解说验收

启动 AI 配音解说任务。

预期：

```text
content_analysis 成功
兼容写 video_understanding 成功
兼容写 highlight_detection 成功
short_video_edit_plan 继续执行
```

不应该再出现：

```text
'video_understanding' is not in list
```

---

## 7.2 视频重组验收

启动视频重组任务。

预期步骤包含：

```text
timeline_digest
video_understanding
highlight_detection
highlight_reassembly_plan
```

`video_understanding` 不应该因为找不到 `timeline_digest` 失败。

---

## 7.3 common 日志验收

跑多源任务后，应该存在：

```text
outputs/__common__/common_xxx/web_jobs/common_analysis.log
```

里面能看到：

```text
start common analysis
source_prepare success
run common pipeline
common pipeline finished with return code 0
common analysis ready
```

---

## 7.4 任务列表分组验收

同一批素材分别启动：

```text
AI配音解说
视频重组
```

左侧任务队列显示：

```text
多源剪辑（2段）
  AI配音解说
  视频重组
```

而不是两个平铺任务。

---

## 7.5 终止按钮验收

页面刷新后：

```text
终止任务按钮始终可见
无任务运行：灰色 disabled
有 pending/running job：红色可点击
点击后：显示终止中...
终止完成：按钮恢复灰色 disabled
```

---

# 8. 建议 commit 顺序

建议分 4 个 commit：

```text
commit 1: fix ai voiceover compat step stale bug
commit 2: add common analysis log under common task directory
commit 3: group same-source tasks in web task list
commit 4: keep stop button visible but disabled when idle
```

不要把 4 类修改混在一个 commit 里，否则后面不好回滚。

---

# 9. 最小优先修复

如果时间紧，先改这 3 个：

```text
1. pipeline.py 的 _mark_downstream_stale 加 step not in step_order 防御
2. pipeline.py 的 HIGHLIGHT_REASSEMBLY_STEP_ORDER 补 timeline_digest
3. index.html/app.js 改终止按钮始终显示
```

这三个能最快解决当前最明显的问题。
````
