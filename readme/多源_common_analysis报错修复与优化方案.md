# 凤凰新闻视频智能拆条系统：多源 common analysis 报错修复与优化方案

> 适用仓库：`angelOnly/AutomaticEditing`  
> 适用分支：`clean-highlight-reassembly`  
> Web 启动方式保持不变：`python run_web.py`  
> 本文只处理当前报错与多源/统一素材分析链路，不处理 API Key 明文配置。

---

## 1. 当前代码现状

### 1.1 Web 启动入口

项目入口是：

```bash
python run_web.py
```

`run_web.py` 会读取 `config.toml`，做 ASR preflight，然后启动：

```python
uvicorn.run("web_app:app", host=host, port=port, reload=False)
```

所以 Web 启动入口本身没有问题，不需要修改。

---

### 1.2 Web 提交任务后的 runner 选择

`web_app.py` 中，任务提交后会构造命令：

```python
runner = MULTI_SOURCE_RUNNER if source_request_path else RUNNER
```

也就是说：

- 有 `source_request_path`：走 `run_multisource_pipeline.py`
- 没有 `source_request_path`：走 `run_pipeline.py`

你刚刚选了 4 个视频，属于多源任务，所以实际会走：

```text
web_app.py
→ run_multisource_pipeline.py
→ newsclip_agent.pipeline.PipelineRunner
```

不是单源入口问题。

---

### 1.3 多源任务执行链路

多源任务的核心流程是：

```text
run_multisource_pipeline.py
  ├─ _ensure_common_analysis()
  │    ├─ prepare_multi_source_manifest()
  │    └─ pipeline_main(... --common-only)
  │
  ├─ _copy_common_outputs_to_task()
  │
  └─ _run_mode_specific_pipeline()
       ├─ highlight_reassembly: 从 video_understanding 开始
       └─ ai_voiceover: 从 content_analysis 开始
```

公共分析 common analysis 先跑，后续高光重组或 AI 配音再复用 common analysis 的输出。

---

## 2. 当前报错表现

Web 页面运行日志显示：

```text
common analysis finished but source_aggregate is not ready;
```

这个错误表面上看是 `source_aggregate` 没准备好，但真正原因是：

```text
多源 common analysis 的“应该完成的步骤列表”
和
PipelineRunner --common-only 实际执行的步骤列表
不一致。
```

---

## 3. 问题根因

### 3.1 workflow_registry.py 中定义的统一 common 步骤

`newsclip_agent/workflow_registry.py` 中定义：

```python
UNIFIED_SOURCE_COMMON_REUSABLE_STEPS = [
    "source_prepare",
    "source_analysis",
    "source_quality_check",
    "source_aggregate",
]
```

这表示统一素材管线下，common analysis 完整公共步骤应该包含：

```text
source_prepare
source_analysis
source_quality_check
source_aggregate
```

---

### 3.2 run_multisource_pipeline.py 会按这个列表检查 ready

`run_multisource_pipeline.py` 中：

```python
COMMON_REUSABLE_STEPS = UNIFIED_SOURCE_COMMON_REUSABLE_STEPS
```

然后 `_common_analysis_ready()` 会检查所有 common reusable steps 是否 ready：

```python
def _common_analysis_ready(common_dir: Path) -> bool:
    return all(_common_step_ready(common_dir, step) for step in COMMON_REUSABLE_STEPS)
```

所以它会要求：

```text
source_prepare ready
source_analysis ready
source_quality_check ready
source_aggregate ready
```

---

### 3.3 但 PipelineRunner common_only 实际漏跑了 source_quality_check

`newsclip_agent/pipeline.py` 的 `_resolve_selected_steps()` 中：

```python
if self.options.common_only:
    if self._is_virtual_source_manifest():
        common_steps = [
            "source_analysis",
            "source_aggregate",
        ]
```

也就是说，多源 common-only 实际只跑：

```text
source_analysis
source_aggregate
```

漏了：

```text
source_quality_check
```

于是就出现：

```text
run_multisource_pipeline.py 认为 source_quality_check 必须存在
但 pipeline.py common_only 根本没有跑 source_quality_check
最终 common analysis ready 判断失败
```

---

### 3.4 不能只把 source_quality_check 加到列表里

当前 `step_source_quality_check()` 还有一个隐藏 bug。

现有逻辑大致是：

```python
version, vdir = self._version_dir("source_quality_check", "source_quality_check")
ensure_dir(vdir)

report = build_source_quality_report(sources_metadata)

result = {
    "version": version,
    "report": report,
}

self._overwrite_step_json("source_quality_check", result)
```

问题是 `_overwrite_step_json()` 内部第一行会执行：

```python
out_path = self.task_dir / self._step_output(step)
```

而 `_step_output(step)` 要求 manifest 里已经存在：

```json
"steps": {
  "source_quality_check": {
    "output": "..."
  }
}
```

但第一次运行 `source_quality_check` 时 manifest 里还没有 output，所以会报：

```text
步骤 source_quality_check 没有可用输出
```

因此完整修复必须同时做两件事：

```text
1. common_only 步骤列表补上 source_quality_check
2. 重写 step_source_quality_check，让它像其他 step 一样正常写 output、step_status.json 和 manifest
```

---

## 4. 修改文件清单

必须修改：

```text
newsclip_agent/pipeline.py
run_multisource_pipeline.py
```

可选修改：

```text
newsclip_agent/workflow_registry.py
```

不需要修改：

```text
run_web.py
web_app.py
config.toml
run_pipeline.py
```

说明：

- `run_web.py` 保持 `python run_web.py` 启动方式。
- `web_app.py` 的 runner 选择逻辑是对的。
- `config.toml` 本次不处理 API Key 明文问题。
- `run_pipeline.py` 只是薄入口，只有 `from newsclip_agent.pipeline import main`，不需要改。

---

## 5. 具体代码修改指南

---

# 修改一：修复 common_only 步骤选择

## 文件

```text
newsclip_agent/pipeline.py
```

## 位置

找到：

```python
def _resolve_selected_steps(self) -> list[str]:
```

里面这一段：

```python
if self.options.common_only:
    if self._is_virtual_source_manifest():
        common_steps = [
            "source_analysis",
            "source_aggregate",
        ]
    else:
        common_steps = [
            "metadata",
            "audio_extract",
            "frame_extract",
            "chunk_build",
            "asr",
            "asr_digest",
            "vision",
            "timeline",
            "timeline_digest",
        ]
    step_order = [step for step in common_steps if step in step_order]
```

## 替换为

```python
if self.options.common_only:
    if self._is_virtual_source_manifest():
        # 多源/统一素材 common analysis：
        # source_prepare 由 run_multisource_pipeline.py wrapper 负责标记；
        # PipelineRunner 负责真正的素材分析、质量检查、聚合。
        common_steps = [
            "source_analysis",
            "source_quality_check",
            "source_aggregate",
        ]
    else:
        common_steps = [
            "metadata",
            "audio_extract",
            "frame_extract",
            "chunk_build",
            "asr",
            "asr_digest",
            "vision",
            "timeline",
            "timeline_digest",
        ]
    step_order = [step for step in common_steps if step in step_order]
```

## 改动效果

修复前：

```text
common_only 实际跑：
source_analysis → source_aggregate
```

修复后：

```text
common_only 实际跑：
source_analysis → source_quality_check → source_aggregate
```

这与 `workflow_registry.py` 中定义的统一 common reusable steps 对齐。

---

# 修改二：重写 step_source_quality_check

## 文件

```text
newsclip_agent/pipeline.py
```

## 位置

找到：

```python
def step_source_quality_check(self) -> None:
```

把整个函数替换掉。

## 替换代码

```python
def step_source_quality_check(self) -> None:
    """Check source media quality and write a normal step output.

    Important:
    - This step may run inside unified multi-source common analysis.
    - It must create its own output file before calling _record_step.
    - Do not use _overwrite_step_json for first-time output creation,
      because _overwrite_step_json requires manifest.steps[step].output
      to already exist.
    """
    if self._is_virtual_multi_source():
        # Unified virtual source mode:
        # source_analysis has already analyzed each source in child runners.
        # For quality check, use source manifest items and ffprobe metadata.
        sources_metadata: list[dict[str, Any]] = []

        for item in self._iter_source_items():
            meta: dict[str, Any] = {}
            try:
                source_path = self._resolve_source_item_path(item)
                if source_path.exists():
                    meta = ffprobe_json(source_path)
            except Exception as exc:
                meta = {
                    "probe_error": str(exc),
                }

            if item.get("duration_seconds") is not None:
                meta.setdefault("duration", item.get("duration_seconds"))
                meta.setdefault("duration_seconds", item.get("duration_seconds"))

            meta.setdefault("source_id", item.get("source_id"))
            meta.setdefault("source_index", item.get("source_index"))
            meta.setdefault("display_name", item.get("display_name", ""))
            meta.setdefault("original_path", item.get("original_path", ""))

            sources_metadata.append(meta)

        input_hash = stable_hash({
            "source_analysis": self._step_content_hash("source_analysis"),
            "sources": [
                {
                    "source_id": item.get("source_id"),
                    "source_index": item.get("source_index"),
                    "duration_seconds": item.get("duration_seconds"),
                    "original_path": item.get("original_path"),
                }
                for item in self._iter_source_items()
            ],
        })
    else:
        # Legacy single-source mode:
        # metadata step writes video_metadata.json.
        metadata_info = self._load_step_json("metadata")
        if metadata_info and isinstance(metadata_info.get("sources"), list):
            sources_metadata = [
                s.get("metadata", {})
                for s in metadata_info.get("sources", [])
                if isinstance(s, dict)
            ]
        elif metadata_info:
            sources_metadata = [metadata_info]
        else:
            sources_metadata = []

        input_hash = self._step_content_hash("metadata")

    if self._can_reuse("source_quality_check", input_hash):
        print("复用缓存: source_quality_check")
        return

    version, vdir = self._version_dir("source_quality_check", "source_quality_check")
    ensure_dir(vdir)

    report = build_source_quality_report(sources_metadata)
    result = {
        "version": "source_quality_check_v1",
        "report": report,
        "source_count": len(sources_metadata),
    }

    out = write_json(vdir / "source_quality_check.json", result)

    status = "success" if report.get("is_pass", True) else "failed"
    status_doc = self._base_status("source_quality_check", version, input_hash, [out])
    status_doc["status"] = status
    self._write_status(vdir, status_doc)

    self._record_step(
        step="source_quality_check",
        version=version,
        status=status,
        output=relpath(out, self.task_dir),
        input_hash=input_hash,
        output_files=[out],
        extra={
            "summary": {
                "is_pass": report.get("is_pass", True),
                "reason": report.get("reason", ""),
                "source_count": len(sources_metadata),
            }
        },
    )

    if not report.get("is_pass", True):
        raise RuntimeError(f"Source quality check failed: {report.get('reason')}")

    print("完成: source_quality_check")
```

## 改动效果

修复后该步骤会正常生成：

```text
source_quality_check/v1/source_quality_check.json
source_quality_check/v1/step_status.json
```

并在 manifest 里记录：

```json
"source_quality_check": {
  "step_name": "source_quality_check",
  "version": "v1",
  "status": "success",
  "output": "source_quality_check/v1/source_quality_check.json",
  "output_hash": "...",
  "can_rerun": true
}
```

这样 `_common_analysis_ready()` 就能正确判断它 ready。

---

# 修改三：让 _common_step_output_exists 支持 source_quality_check

## 文件

```text
run_multisource_pipeline.py
```

## 位置

找到：

```python
def _common_step_output_exists(common_dir: Path, step: str) -> bool:
```

当前逻辑大致是：

```python
def _common_step_output_exists(common_dir: Path, step: str) -> bool:
    vdir = _latest_version_dir(common_dir / step)
    if not vdir:
        return False

    if step == "source_analysis":
        return (vdir / "source_analysis.json").exists()

    if step == "source_aggregate":
        return (vdir / "source_aggregate.json").exists()

    return (vdir / "step_status.json").exists()
```

## 替换为

```python
def _common_step_output_exists(common_dir: Path, step: str) -> bool:
    vdir = _latest_version_dir(common_dir / step)
    if not vdir:
        return False

    output_names = {
        "source_analysis": "source_analysis.json",
        "source_quality_check": "source_quality_check.json",
        "source_aggregate": "source_aggregate.json",
    }

    output_name = output_names.get(step)
    if output_name:
        return (vdir / output_name).exists()

    return (vdir / "step_status.json").exists()
```

## 改动效果

即使 manifest 写入有延迟或中间异常，只要实际目录里存在：

```text
source_quality_check/vX/source_quality_check.json
```

ready 判断也可以兜底通过。

---

# 修改四：让 _repair_common_manifest_from_outputs 支持 source_quality_check

## 文件

```text
run_multisource_pipeline.py
```

## 位置

找到：

```python
def _repair_common_manifest_from_outputs(common_dir: Path) -> None:
```

函数内部有一段：

```python
output_file = None
if step == "source_analysis":
    output_file = vdir / "source_analysis.json"
elif step == "source_aggregate":
    output_file = vdir / "source_aggregate.json"

if not output_file or not output_file.exists():
    continue
```

## 替换为

```python
output_names = {
    "source_analysis": "source_analysis.json",
    "source_quality_check": "source_quality_check.json",
    "source_aggregate": "source_aggregate.json",
}

output_name = output_names.get(step)
if not output_name:
    continue

output_file = vdir / output_name
if not output_file.exists():
    continue
```

## 改动效果

当 common pipeline 已经生成文件，但 manifest 没同步完整时，可以自动修复：

```json
"source_quality_check": {
  "output": "source_quality_check/v1/source_quality_check.json",
  "status": "success"
}
```

---

# 修改五：优化 common analysis 报错信息

## 文件

```text
run_multisource_pipeline.py
```

## 位置

`_ensure_common_analysis()` 中当前报错：

```python
raise RuntimeError(
    "common analysis finished but source_aggregate is not ready; "
    f"debug={json.dumps(debug, ensure_ascii=False)}"
)
```

## 替换为

```python
not_ready_steps = [
    step
    for step in COMMON_REUSABLE_STEPS
    if not _common_step_ready(common_dir, step)
]

raise RuntimeError(
    "common analysis finished but reusable common steps are not ready; "
    f"not_ready_steps={not_ready_steps}; "
    f"debug={json.dumps(debug, ensure_ascii=False)}"
)
```

## 改动效果

以后不会再误导成只是 `source_aggregate` 的问题。

例如会明确显示：

```text
not_ready_steps=['source_quality_check']
```

或：

```text
not_ready_steps=['source_aggregate']
```

这样排查速度会快很多。

---

## 6. 推荐额外优化：统一 common step 输出文件名映射

为了避免 `source_analysis`、`source_quality_check`、`source_aggregate` 到处写死，建议在 `run_multisource_pipeline.py` 顶部加一个常量。

## 文件

```text
run_multisource_pipeline.py
```

## 建议添加位置

在：

```python
COMMON_REUSABLE_STEPS = UNIFIED_SOURCE_COMMON_REUSABLE_STEPS
COMMON_PROGRESS_STEPS = ["source_prepare"] + COMMON_REUSABLE_STEPS
LEGACY_COMMON_REUSABLE_STEPS = LEGACY_SINGLE_COMMON_REUSABLE_STEPS
```

下面加：

```python
COMMON_STEP_OUTPUT_FILES = {
    "source_analysis": "source_analysis.json",
    "source_quality_check": "source_quality_check.json",
    "source_aggregate": "source_aggregate.json",
}
```

然后前面两个函数可以写得更干净：

```python
def _common_step_output_exists(common_dir: Path, step: str) -> bool:
    vdir = _latest_version_dir(common_dir / step)
    if not vdir:
        return False

    output_name = COMMON_STEP_OUTPUT_FILES.get(step)
    if output_name:
        return (vdir / output_name).exists()

    return (vdir / "step_status.json").exists()
```

以及：

```python
output_name = COMMON_STEP_OUTPUT_FILES.get(step)
if not output_name:
    continue

output_file = vdir / output_name
if not output_file.exists():
    continue
```

---

## 7. 可选修改：workflow_registry.py 加注释

## 文件

```text
newsclip_agent/workflow_registry.py
```

不建议删除 `source_quality_check`，它本身是合理步骤。  
可以加注释说明职责边界：

```python
UNIFIED_SOURCE_COMMON_REUSABLE_STEPS = [
    # source_prepare is marked by run_multisource_pipeline.py wrapper.
    # The following steps are executed/reused as common source analysis outputs.
    "source_prepare",
    "source_analysis",
    "source_quality_check",
    "source_aggregate",
]
```

---

## 8. 修改后预期流程

多源 4 视频任务修复后的流程应该是：

```text
web_app.py
  ↓
run_multisource_pipeline.py
  ↓
source_prepare
  ↓
pipeline_main(... --common-only)
  ↓
PipelineRunner._resolve_selected_steps()
  ↓
source_analysis
  ↓
source_quality_check
  ↓
source_aggregate
  ↓
_common_analysis_ready() == True
  ↓
_copy_common_outputs_to_task()
  ↓
_run_mode_specific_pipeline()
  ↓
highlight_reassembly:
    video_understanding
    highlight_detection
    highlight_reassembly_plan
    reassembly_cut_plan
    news_quality_gate
    news_quality_ai_review
    reassembly_render
```

---

## 9. 验证步骤

### 9.1 清理旧失败缓存

建议先删掉这次失败任务和对应 common 缓存，否则旧 manifest 可能继续干扰判断。

Windows 示例：

```bat
rmdir /s /q outputs\你的任务ID
rmdir /s /q outputs\__common__\对应common任务ID
```

如果不知道 common 任务 ID，可以先只删当前失败任务，再从 Web 重新提交。

---

### 9.2 启动 Web

保持原方式：

```bash
python run_web.py
```

或指定端口：

```bash
python run_web.py --port 7860
```

---

### 9.3 Web 重新提交 4 个视频

观察步骤应该变成：

```text
source_prepare        success
source_analysis       success
source_quality_check  success
source_aggregate      success
video_understanding   running/success
...
```

---

### 9.4 命令行验证 common-only

如果想先绕过 Web 验证，可拿 Web 生成的：

```text
outputs/<task_id>/input/source_request.json
```

然后运行：

```bat
python run_multisource_pipeline.py ^
  --task-id test_common_fix ^
  --source-request outputs\<task_id>\input\source_request.json ^
  --production-mode highlight_reassembly ^
  --chunk-seconds 60 ^
  --frame-interval 10 ^
  --aspect-ratio 16:9
```

---

## 10. 建议增加调试输出

为了下次能快速定位 common 步骤缺失，建议在 `_ensure_common_analysis()` ready 检查失败前，打印 debug。

## 文件

```text
run_multisource_pipeline.py
```

## 建议代码

```python
debug = {
    step: {
        "ready": _common_step_ready(common_dir, step),
        "status": (steps.get(step) or {}).get("status"),
        "output": (steps.get(step) or {}).get("output"),
        "output_exists": _common_step_output_exists(common_dir, step),
    }
    for step in COMMON_REUSABLE_STEPS
}
```

这样日志里能直接看到：

```json
{
  "source_quality_check": {
    "ready": false,
    "status": null,
    "output": null,
    "output_exists": false
  }
}
```

---

## 11. 为什么不建议绕过 source_quality_check

有一个临时方案是把：

```python
UNIFIED_SOURCE_COMMON_REUSABLE_STEPS = [
    "source_prepare",
    "source_analysis",
    "source_aggregate",
]
```

这样可以绕过当前报错。

但不推荐，因为：

1. `workflow_registry.py` 里 `source_aggregate` 的依赖本来就是 `source_quality_check`；
2. 质量检查是统一素材分析的一部分；
3. 跳过它会让后续 `DEPENDENCIES` 和 manifest 语义不一致；
4. 以后 Web 展示、复用、重跑时还会遇到类似不一致问题。

正确修法是：

```text
补跑 source_quality_check
并修复它的输出落盘逻辑
```

---

## 12. 风险点与兼容性说明

### 12.1 对 Web 启动无影响

不改 `run_web.py`，仍然：

```bash
python run_web.py
```

### 12.2 对 Web 参数无影响

`web_app.py` 构造命令的参数不需要改。  
已有参数仍然兼容：

```text
--task-id
--chunk-seconds
--frame-interval
--aspect-ratio
--target-duration
--audio-policy
--production-mode
...
```

### 12.3 对多源高光重组有正向影响

多源高光重组依赖：

```text
source_aggregate
→ video_understanding
→ highlight_detection
→ highlight_reassembly_plan
```

修复后 common analysis ready 才能稳定进入后半段。

### 12.4 对 AI 配音模式也有正向影响

AI 配音模式后半段从：

```text
content_analysis
```

开始，也依赖 common analysis 输出。  
因此这个修复同样能提升 AI 配音模式稳定性。

---

## 13. 最小补丁汇总

如果只想最小修复，按这个顺序改：

```text
1. pipeline.py
   _resolve_selected_steps()
   给 virtual common_only 加 source_quality_check

2. pipeline.py
   重写 step_source_quality_check()
   不再用 _overwrite_step_json() 创建首个输出

3. run_multisource_pipeline.py
   _common_step_output_exists()
   支持 source_quality_check.json

4. run_multisource_pipeline.py
   _repair_common_manifest_from_outputs()
   支持 source_quality_check.json

5. run_multisource_pipeline.py
   报错信息从 source_aggregate is not ready
   改成 reusable common steps are not ready
```

---

## 14. 最终判断

你这次 4 个视频的多源任务，入口是走对了的。

当前报错的本质是：

```text
统一多源 common analysis 的步骤注册、ready 检查、实际执行三者不一致。
```

具体是：

```text
workflow_registry.py / run_multisource_pipeline.py 要求 source_quality_check
pipeline.py common_only 漏跑 source_quality_check
step_source_quality_check 自身首次输出写法也不正确
```

修完后，多源任务应该可以稳定从：

```text
source_prepare
source_analysis
source_quality_check
source_aggregate
```

进入：

```text
video_understanding
highlight_detection
highlight_reassembly_plan
reassembly_cut_plan
reassembly_render
```
