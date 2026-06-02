# AutomaticEditing 公共中间产物复用机制代码优化指导

仓库：`angelOnly/AutomaticEditing`  
分支：`clean-highlight-reassembly`  
入口：`python run_web.py`  
目标：解决“同一批视频源，同时启动 AI 配音解说模式和视频重组模式时，会创建两个完全不相关任务，公共步骤无法复用”的问题。

---

## 0. 本次改造目标

当前现状是：

```text
outputs/
  多源剪辑_2段_AI配音解说_xxx/
    input/
    metadata/
    preprocess/
    asr/
    vision/
    timeline/
    agents/
    edit/

  多源剪辑_2段_视频重组_yyy/
    input/
    metadata/
    preprocess/
    asr/
    vision/
    timeline/
    agents/
    edit/
```

两个任务虽然使用同一批视频源，但因为 `production_mode` 不同，会生成不同 `task_id` 和不同目录，导致下面这些公共步骤重复执行：

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

本次要改成：

```text
outputs/
  __common__/
    common_<source_key>/
      input/
      metadata/
      preprocess/
      asr/
      vision/
      timeline/
      manifest.json

  多源剪辑_2段_AI配音解说_xxx/
    manifest.json
    agents/content_analysis/
    agents/short_video_edit_plan/
    agents/voiceover_script/
    tts/
    subtitles/
    edit/drafts/

  多源剪辑_2段_视频重组_yyy/
    manifest.json
    agents/video_understanding/
    agents/highlight_detection/
    agents/highlight_reassembly/
    edit/reassembly_drafts/
```

最终效果：

```text
同一批视频源 + 相同公共分析参数
    ↓
只跑一次公共分析目录 outputs/__common__/common_xxx
    ↓
AI 配音解说任务复用公共步骤，从 content_analysis 继续
视频重组任务复用公共步骤，从 video_understanding 继续
```

---

## 1. 改造原则

### 1.1 公共步骤只包含“与生产模式无关”的步骤

公共步骤固定为：

```python
COMMON_REUSABLE_STEPS = [
    "source_prepare",
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

这些步骤的输出只依赖：

```text
视频源列表
视频顺序
画面比例 aspect_ratio
chunk_seconds
frame_interval
run mode normal/dense
```

不依赖：

```text
production_mode
output_mode
max_output_videos
target_duration_seconds
voice_id
reassembly_target_seconds
reassembly_max_clip_count
```

### 1.2 模式专属步骤不能进入公共目录

AI 配音解说模式专属：

```text
content_analysis
short_video_edit_plan
voiceover_script
tts
subtitles
cut_plan
render
```

视频重组模式专属：

```text
video_understanding
highlight_detection
highlight_reassembly_plan
reassembly_cut_plan
reassembly_render
```

注意：虽然 `video_understanding`、`highlight_detection` 看起来像“公共分析”，但在当前项目里它们的语义已经和模式有关，不要放进公共层。

---

# 2. 修改文件总览

本次建议修改这些文件：

```text
web_app.py
run_multisource_pipeline.py
newsclip_agent/pipeline.py
web_static/app.js
config.toml
```

可选修改：

```text
web_static/styles.css
README.md
```

---

# 3. 修改 web_app.py

## 3.1 新增公共目录常量

文件：`web_app.py`

找到：

```python
OUTPUTS_DIR = ROOT / "outputs"
VIDEOS_DIR = ROOT / "videos"
STATIC_DIR = ROOT / "web_static"
```

下面新增：

```python
COMMON_OUTPUTS_DIR = OUTPUTS_DIR / "__common__"
```

---

## 3.2 新增公共 source key 计算函数

文件：`web_app.py`

建议放在 `_source_request_fingerprint()` 前面。

新增：

```python
def _common_source_key(req: RunRequest, items: list[dict[str, Any]]) -> str:
    """
    计算公共中间产物复用 key。

    这个 key 只包含会影响 source_prepare / metadata / frame_extract / asr / vision / timeline 的参数。
    不允许包含 production_mode / output_mode / max_output_videos / target_duration_seconds，
    否则 AI 配音和视频重组会生成不同 common key，无法复用。
    """
    payload_items: list[dict[str, Any]] = []

    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue

        source_type = str(item.get("source_type") or item.get("type") or "local")

        if source_type in {"local", "video"}:
            path_value = item.get("path") or item.get("input_video")
            path = _resolve_input_video(str(path_value))
            payload_items.append(
                {
                    "order": index,
                    "source_type": "local",
                    **_video_fingerprint(path),
                }
            )
            continue

        if source_type in {"remote", "remote_ucms"}:
            remote_video = item.get("remote_video") or item
            payload_items.append(
                {
                    "order": index,
                    "source_type": "remote_ucms",
                    "remote": _remote_video_fingerprint(remote_video),
                }
            )
            continue

        payload_items.append(
            {
                "order": index,
                "source_type": source_type,
                "raw": item,
            }
        )

    payload = {
        "sources": payload_items,
        "aspect_ratio": req.aspect_ratio,
        "chunk_seconds": req.chunk_seconds,
        "frame_interval": req.frame_interval,
        "mode": req.mode,
    }

    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
```

---

## 3.3 修改多源分支，写入 common_task_id

文件：`web_app.py`

找到 `/api/run` 的多源分支：

```python
if len(raw_source_items) > 1:
    _validate_source_request_items(raw_source_items)
    fingerprint = _source_request_fingerprint(req, raw_source_items)
    input_name_for_task = _display_source_request_name(raw_source_items)
    task_id = _normalize_new_task_id(req.task_id, input_name_for_task, req.production_mode, fingerprint)
    task_dir = ensure_dir(OUTPUTS_DIR / task_id)
    source_request_path = task_dir / "input" / "source_request.json"
    ensure_dir(source_request_path.parent)
    write_json(
        source_request_path,
        {
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
    input_path = None
```

替换为：

```python
if len(raw_source_items) > 1:
    _validate_source_request_items(raw_source_items)

    # 生产任务 fingerprint：仍然保留 production_mode / output_mode 等参数。
    # 用于区分 AI 配音任务和视频重组任务。
    fingerprint = _source_request_fingerprint(req, raw_source_items)

    # 公共 source key：只描述公共中间产物是否可复用。
    common_source_key = _common_source_key(req, raw_source_items)
    common_task_id = f"common_{common_source_key}"

    input_name_for_task = _display_source_request_name(raw_source_items)
    task_id = _normalize_new_task_id(
        req.task_id,
        input_name_for_task,
        req.production_mode,
        fingerprint,
    )
    task_id = _with_mode_suffix(task_id, req.production_mode)

    task_dir = ensure_dir(OUTPUTS_DIR / task_id)
    source_request_path = task_dir / "input" / "source_request.json"
    ensure_dir(source_request_path.parent)

    write_json(
        source_request_path,
        {
            "task_id": task_id,
            "production_mode": req.production_mode,
            "common_source_key": common_source_key,
            "common_task_id": common_task_id,
            "source_items": raw_source_items,
            "force_remote_download": req.force_remote_download,
            "output_mode": req.output_mode,
            "max_output_videos": req.max_output_videos,
            "min_output_video_seconds": req.min_output_video_seconds,
            "max_output_video_seconds": req.max_output_video_seconds,
            "aspect_ratio": req.aspect_ratio,
            "chunk_seconds": req.chunk_seconds,
            "frame_interval": req.frame_interval,
            "mode": req.mode,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    input_path = None
```

---

## 3.4 单源分支也强制补 `_with_mode_suffix`

文件：`web_app.py`

找到：

```python
if input_name_for_task and not req.task_id:
    task_id = _make_task_id_from_fingerprint(input_name_for_task, req.production_mode, fingerprint)
else:
    task_id = _resolve_run_task_id(req)
task_dir = ensure_dir(OUTPUTS_DIR / task_id)
```

替换为：

```python
if input_name_for_task and not req.task_id:
    task_id = _make_task_id_from_fingerprint(input_name_for_task, req.production_mode, fingerprint)
else:
    task_id = _resolve_run_task_id(req)

task_id = _with_mode_suffix(task_id, req.production_mode)
task_dir = ensure_dir(OUTPUTS_DIR / task_id)
```

`elif not raw_source_items` 分支里同样处理。

---

## 3.5 修复 get_manifest fallback 的 production_mode 硬编码

文件：`web_app.py`

找到：

```python
"last_web_run_options": {
    "production_mode": "ai_voiceover",
    "output_mode": source_request.get("output_mode", "single"),
    "max_output_videos": source_request.get("max_output_videos", 1),
    "aspect_ratio": source_request.get("aspect_ratio", "16:9"),
},
```

替换为：

```python
"last_web_run_options": {
    "production_mode": source_request.get(
        "production_mode",
        _task_id_production_mode(task_id) or "ai_voiceover",
    ),
    "output_mode": source_request.get("output_mode", "single"),
    "max_output_videos": source_request.get("max_output_videos", 1),
    "min_output_video_seconds": source_request.get("min_output_video_seconds", 30),
    "max_output_video_seconds": source_request.get("max_output_video_seconds", 90),
    "aspect_ratio": source_request.get("aspect_ratio", "16:9"),
    "common_task_id": source_request.get("common_task_id", ""),
    "common_source_key": source_request.get("common_source_key", ""),
},
```

同时 fallback manifest 顶层也补：

```python
"common_task_id": source_request.get("common_task_id", ""),
"common_source_key": source_request.get("common_source_key", ""),
```

完整 fallback 建议改成：

```python
manifest = {
    "task_id": task_id,
    "created_at": source_request.get("created_at", ""),
    "updated_at": datetime.fromtimestamp(task_dir.stat().st_mtime).isoformat(timespec="seconds"),
    "source_video": "",
    "source_mode": "multi_source_pending",
    "source_manifest": "",
    "source_videos": [],
    "common_task_id": source_request.get("common_task_id", ""),
    "common_source_key": source_request.get("common_source_key", ""),
    "last_web_run_options": {
        "production_mode": source_request.get(
            "production_mode",
            _task_id_production_mode(task_id) or "ai_voiceover",
        ),
        "output_mode": source_request.get("output_mode", "single"),
        "max_output_videos": source_request.get("max_output_videos", 1),
        "min_output_video_seconds": source_request.get("min_output_video_seconds", 30),
        "max_output_video_seconds": source_request.get("max_output_video_seconds", 90),
        "aspect_ratio": source_request.get("aspect_ratio", "16:9"),
        "common_task_id": source_request.get("common_task_id", ""),
        "common_source_key": source_request.get("common_source_key", ""),
    },
    "steps": {
        "source_prepare": {
            "status": "pending",
            "source_count": len(source_request.get("source_items") or []),
        }
    },
}
```

---

## 3.6 list_tasks 跳过公共目录

文件：`web_app.py`

找到：

```python
for task_dir in sorted([p for p in OUTPUTS_DIR.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True):
```

替换为：

```python
for task_dir in sorted(
    [
        p
        for p in OUTPUTS_DIR.iterdir()
        if p.is_dir() and p.name != "__common__" and not p.name.startswith("common_")
    ],
    key=lambda p: p.stat().st_mtime,
    reverse=True,
):
```

如果你采用 `outputs/__common__/common_xxx` 结构，主要跳过 `__common__` 就够了，但保留 `not p.name.startswith("common_")` 可兼容旧调试目录。

---

## 3.7 可选：任务列表返回 common_task_id

文件：`web_app.py`

在 `list_tasks()` 的返回字典里：

```python
{
    "task_id": task_dir.name,
    "source_video": manifest.get("source_video", ""),
    ...
}
```

补充：

```python
"common_task_id": manifest.get("common_task_id", ""),
"reused_common_steps": manifest.get("reused_common_steps", []),
```

用于前端显示“复用公共分析”。

---

# 4. 修改 run_multisource_pipeline.py

这是本次改造的核心文件。

## 4.1 增加 import

文件顶部当前是：

```python
import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any
```

替换为：

```python
import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
```

再新增：

```python
from newsclip_agent.resource_locks import file_slot_lock
```

---

## 4.2 增加公共目录常量与公共步骤

在：

```python
ROOT = Path(__file__).resolve().parent
OUTPUTS_DIR = ROOT / "outputs"
```

下面新增：

```python
COMMON_OUTPUTS_DIR = OUTPUTS_DIR / "__common__"

COMMON_REUSABLE_STEPS = [
    "source_prepare",
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

---

## 4.3 重写 main()

当前 `main()` 是直接在生产任务目录里做 `source_prepare` 和后续 pipeline。

建议整体替换为：

```python
def main(argv: list[str] | None = None) -> int:
    args, passthrough = parse_args(argv)
    task_dir = ensure_dir(OUTPUTS_DIR / args.task_id)
    request_path = Path(args.source_request).resolve()
    request = read_json(request_path, {})

    common_task_id = str(request.get("common_task_id") or "").strip()

    if common_task_id:
        common_dir = ensure_dir(COMMON_OUTPUTS_DIR / common_task_id)

        # 1. 确保公共分析目录已经准备完成
        _ensure_common_analysis(
            common_dir=common_dir,
            request=request,
            args=args,
        )

        # 2. 把公共产物复制到当前生产任务目录
        _copy_common_outputs_to_task(
            common_dir=common_dir,
            task_dir=task_dir,
            request=request,
        )

        # 3. 从当前生产任务的模式专属步骤继续跑
        return _run_mode_specific_pipeline(
            args=args,
            passthrough=passthrough,
            task_dir=task_dir,
            request=request,
        )

    # 兼容旧 source_request：没有 common_task_id 时走原逻辑
    _mark_source_prepare(task_dir, "running", request=request)
    try:
        config = load_config(ROOT / "config.toml")
        sources = _resolve_sources(request, config)
        multi_source_config = config.raw.get("multi_source", {})
        built = build_multi_source_video(
            sources=sources,
            task_dir=task_dir,
            root_dir=ROOT,
            aspect_ratio=str(request.get("aspect_ratio") or args.aspect_ratio),
            fps=int(multi_source_config.get("fps", 25)),
            sample_rate=int(multi_source_config.get("sample_rate", 48000)),
        )
        _mark_source_prepare(task_dir, "success", request=request, built=built)
    except Exception as exc:
        _mark_source_prepare(task_dir, "failed", request=request, error=str(exc))
        raise

    pipeline_args = [
        "--task-id",
        args.task_id,
        "--input",
        str(built["input_video"]),
        "--source-manifest",
        str(built["source_manifest"]),
        "--aspect-ratio",
        str(request.get("aspect_ratio") or args.aspect_ratio),
        *passthrough,
    ]
    return pipeline_main(pipeline_args)
```

---

## 4.4 新增 `_ensure_common_analysis()`

放在 `_resolve_sources()` 前面或后面均可。

```python
def _ensure_common_analysis(
    *,
    common_dir: Path,
    request: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    """
    确保公共分析目录已经完成 source_prepare 到 timeline_digest。

    同一批素材同时启动 AI 配音和视频重组时：
    - 第一个任务进入这里，获取锁，执行公共分析；
    - 第二个任务进入这里，等待锁；
    - 等第一个完成后，第二个直接复用 common_dir。
    """
    ensure_dir(common_dir)

    lock_name = f"common_source_{common_dir.name}"
    with file_slot_lock(lock_name, slots=1):
        if _common_analysis_ready(common_dir):
            print(f"复用公共分析产物: {common_dir}")
            return

        print(f"开始公共分析: {common_dir}")
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
            _mark_source_prepare(common_dir, "success", request=request, built=built)

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
                "--audio-policy",
                "ai_voiceover",
                "--no-require-tts",
                "--skip-tts",
                "--skip-render",
                "--stop-after",
                "timeline_digest",
            ]

            code = pipeline_main(pipeline_args)
            if code != 0:
                raise RuntimeError(f"公共分析 pipeline 失败，exit_code={code}")

            if not _common_analysis_ready(common_dir):
                raise RuntimeError("公共分析执行结束，但必要步骤未全部成功")

            print(f"完成公共分析: {common_dir}")

        except Exception as exc:
            _mark_source_prepare(common_dir, "failed", request=request, error=str(exc))
            raise
```

---

## 4.5 新增 `_common_analysis_ready()`

```python
def _common_analysis_ready(common_dir: Path) -> bool:
    manifest = read_json(common_dir / "manifest.json", {})
    steps = manifest.get("steps", {})
    if not isinstance(steps, dict):
        return False

    for step in COMMON_REUSABLE_STEPS:
        info = steps.get(step) or {}
        if info.get("status") not in {"success", "partial_success", "skipped"}:
            return False

    return True
```

---

## 4.6 新增 `_copy_common_outputs_to_task()`

```python
def _copy_common_outputs_to_task(
    *,
    common_dir: Path,
    task_dir: Path,
    request: dict[str, Any],
) -> None:
    """
    把公共分析产物复制到生产任务目录。

    注意：
    这里先用 copytree/copy2，保证兼容 Windows 和现有 Web 预览逻辑。
    后续如果磁盘占用太大，可改成硬链接或相对路径引用。
    """
    common_manifest = read_json(common_dir / "manifest.json", {})
    if not common_manifest:
        raise RuntimeError(f"公共 manifest 不存在: {common_dir / 'manifest.json'}")

    ensure_dir(task_dir)

    for name in ["input", "metadata", "preprocess", "asr", "vision", "timeline"]:
        src = common_dir / name
        dst = task_dir / name
        _copy_path_if_missing(src, dst)

    task_manifest = read_json(task_dir / "manifest.json", {})
    now = datetime.now().isoformat(timespec="seconds")

    task_manifest.setdefault("task_id", task_dir.name)
    task_manifest.setdefault("created_at", request.get("created_at") or now)
    task_manifest["updated_at"] = now

    for key in ["source_video", "source_mode", "source_manifest", "source_videos"]:
        if key in common_manifest:
            task_manifest[key] = common_manifest[key]

    task_manifest["common_task_id"] = common_dir.name
    task_manifest["common_source_key"] = request.get("common_source_key", "")
    task_manifest["reused_common_steps"] = list(COMMON_REUSABLE_STEPS)

    production_mode = request.get("production_mode")
    if production_mode in {"ai_voiceover", "highlight_reassembly"}:
        task_manifest["production_mode"] = production_mode
        task_manifest.setdefault("last_web_run_options", {})
        task_manifest["last_web_run_options"]["production_mode"] = production_mode
        task_manifest["last_web_run_options"]["output_mode"] = request.get("output_mode", "single")
        task_manifest["last_web_run_options"]["max_output_videos"] = request.get("max_output_videos", 1)
        task_manifest["last_web_run_options"]["min_output_video_seconds"] = request.get("min_output_video_seconds", 30)
        task_manifest["last_web_run_options"]["max_output_video_seconds"] = request.get("max_output_video_seconds", 90)
        task_manifest["last_web_run_options"]["aspect_ratio"] = request.get("aspect_ratio", "16:9")
        task_manifest["last_web_run_options"]["common_task_id"] = common_dir.name
        task_manifest["last_web_run_options"]["common_source_key"] = request.get("common_source_key", "")

    task_manifest.setdefault("steps", {})
    task_manifest.setdefault("current_versions", {})

    for step in COMMON_REUSABLE_STEPS:
        common_step = (common_manifest.get("steps") or {}).get(step)
        if common_step:
            copied = dict(common_step)
            copied["reused_from_common_task_id"] = common_dir.name
            task_manifest["steps"][step] = copied

        common_version = (common_manifest.get("current_versions") or {}).get(step)
        if common_version:
            task_manifest["current_versions"][step] = common_version

    write_json(task_dir / "manifest.json", task_manifest)
    print(f"已复制公共分析产物到生产任务: {task_dir}")
```

---

## 4.7 新增 `_copy_path_if_missing()`

```python
def _copy_path_if_missing(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if dst.exists():
        return

    ensure_dir(dst.parent)

    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
```

---

## 4.8 新增 `_run_mode_specific_pipeline()`

```python
def _run_mode_specific_pipeline(
    *,
    args: argparse.Namespace,
    passthrough: list[str],
    task_dir: Path,
    request: dict[str, Any],
) -> int:
    manifest = read_json(task_dir / "manifest.json", {})
    source_video = manifest.get("source_video")
    source_manifest = manifest.get("source_manifest")

    if not source_video:
        raise RuntimeError("生产任务缺少 source_video，无法继续执行模式专属 pipeline")

    input_path = (task_dir / source_video).resolve()
    source_manifest_path = (task_dir / source_manifest).resolve() if source_manifest else None

    production_mode = str(request.get("production_mode") or "ai_voiceover")

    rerun_from = "content_analysis" if production_mode == "ai_voiceover" else "video_understanding"

    pipeline_args = [
        "--task-id",
        args.task_id,
        "--input",
        str(input_path),
        "--aspect-ratio",
        str(request.get("aspect_ratio") or args.aspect_ratio),
        "--rerun-from",
        rerun_from,
    ]

    if source_manifest_path and source_manifest_path.exists():
        pipeline_args.extend(["--source-manifest", str(source_manifest_path)])

    pipeline_args.extend(passthrough)

    return pipeline_main(pipeline_args)
```

注意：这里强制追加 `--rerun-from content_analysis/video_understanding`，目的是跳过公共步骤，直接跑模式专属步骤。

如果 `passthrough` 里用户已经带了 `--rerun` 或 `--rerun-from`，要避免重复参数。更稳的写法是先过滤掉已有的 `--rerun-from`：

```python
def _strip_passthrough_option(passthrough: list[str], option: str) -> list[str]:
    result = []
    skip_next = False
    for item in passthrough:
        if skip_next:
            skip_next = False
            continue
        if item == option:
            skip_next = True
            continue
        result.append(item)
    return result
```

然后：

```python
passthrough = _strip_passthrough_option(passthrough, "--rerun-from")
passthrough = _strip_passthrough_option(passthrough, "--rerun")
```

建议加入这个函数：

```python
def _strip_passthrough_option(passthrough: list[str], option: str) -> list[str]:
    result: list[str] = []
    skip_next = False

    for item in passthrough:
        if skip_next:
            skip_next = False
            continue
        if item == option:
            skip_next = True
            continue
        result.append(item)

    return result
```

然后 `_run_mode_specific_pipeline()` 里改为：

```python
clean_passthrough = _strip_passthrough_option(passthrough, "--rerun-from")
clean_passthrough = _strip_passthrough_option(clean_passthrough, "--rerun")
pipeline_args.extend(clean_passthrough)
```

---

## 4.9 修改 `_mark_source_prepare()`

当前 `_mark_source_prepare()` 只记录 source_prepare。建议补充公共字段。

找到：

```python
manifest.setdefault("task_id", task_dir.name)
manifest.setdefault("created_at", now)
manifest["updated_at"] = now
manifest.setdefault("steps", {})
```

后面加：

```python
if request.get("common_task_id"):
    manifest["common_task_id"] = request.get("common_task_id")
if request.get("common_source_key"):
    manifest["common_source_key"] = request.get("common_source_key")

production_mode = request.get("production_mode")
if production_mode in {"ai_voiceover", "highlight_reassembly"}:
    manifest["production_mode"] = production_mode
    manifest.setdefault("last_web_run_options", {})
    manifest["last_web_run_options"]["production_mode"] = production_mode
    manifest["last_web_run_options"]["output_mode"] = request.get("output_mode", "single")
    manifest["last_web_run_options"]["max_output_videos"] = request.get("max_output_videos", 1)
    manifest["last_web_run_options"]["aspect_ratio"] = request.get("aspect_ratio", "16:9")
```

---

# 5. 修改 newsclip_agent/pipeline.py

## 5.1 高光重组步骤加入 timeline_digest

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

## 5.2 修改依赖关系

找到：

```python
"video_understanding": ["timeline"],
"highlight_detection": ["timeline", "video_understanding"],
"highlight_reassembly_plan": ["highlight_detection", "video_understanding", "timeline"],
```

替换为：

```python
"video_understanding": ["timeline_digest"],
"highlight_detection": ["timeline_digest", "video_understanding"],
"highlight_reassembly_plan": ["highlight_detection", "video_understanding", "timeline_digest"],
```

---

## 5.3 RunOptions 增加 stop_after

找到 `@dataclass class RunOptions`，在字段末尾新增：

```python
stop_after: str | None = None
```

建议放在：

```python
reassembly_export_individual_clips: bool = False
```

后面。

---

## 5.4 parse_args 增加 --stop-after

在 `main()` 附近找到 argparse 配置位置，新增：

```python
parser.add_argument("--stop-after", default=None, help="运行到指定步骤后停止，用于公共分析任务")
```

然后构造 `RunOptions(...)` 时补：

```python
stop_after=args.stop_after,
```

如果当前代码是通过 `RunOptions(**vars(args))` 构造，则不需要额外补。

---

## 5.5 修改 `_resolve_selected_steps()`

找到：

```python
def _resolve_selected_steps(self) -> list[str]:
    step_order = self._active_step_order()
    if self.options.rerun and self.options.rerun_from:
        raise ValueError("--rerun 与 --rerun-from 不能同时使用")
    if self.options.rerun:
        if self.options.rerun not in step_order:
            raise ValueError(f"未知步骤: {self.options.rerun}")
        return [self.options.rerun]
    if self.options.rerun_from:
        if self.options.rerun_from not in step_order:
            raise ValueError(f"未知步骤: {self.options.rerun_from}")
        return step_order[step_order.index(self.options.rerun_from) :]
    return step_order
```

替换为：

```python
def _resolve_selected_steps(self) -> list[str]:
    step_order = self._active_step_order()

    if self.options.rerun and self.options.rerun_from:
        raise ValueError("--rerun 与 --rerun-from 不能同时使用")

    if self.options.rerun:
        if self.options.rerun not in step_order:
            raise ValueError(f"未知步骤: {self.options.rerun}")
        selected = [self.options.rerun]
    elif self.options.rerun_from:
        if self.options.rerun_from not in step_order:
            raise ValueError(f"未知步骤: {self.options.rerun_from}")
        selected = step_order[step_order.index(self.options.rerun_from) :]
    else:
        selected = step_order

    if self.options.stop_after:
        if self.options.stop_after not in selected:
            raise ValueError(f"--stop-after 不在当前执行步骤中: {self.options.stop_after}")
        selected = selected[: selected.index(self.options.stop_after) + 1]

    return selected
```

---

## 5.6 新增 `_load_llm_timeline_digest()`

放在 `step_content_analysis()` 前面。

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

        chunks: list[dict[str, Any]] = []

        for item in timeline:
            if not isinstance(item, dict):
                continue

            chunks.append(
                {
                    "chunk_id": item.get("chunk_id", ""),
                    "time": f"{item.get('start', '')}-{item.get('end', '')}",
                    "start_seconds": item.get("start_seconds", 0),
                    "end_seconds": item.get("end_seconds", 0),
                    "speech": compact_asr_for_llm(
                        item.get("asr_text", ""),
                        max_chars=int(llm_input_cfg.get("max_asr_chars_per_chunk", 240)),
                    ),
                    "visual": compact_text(
                        item.get("visual_summary", ""),
                        max_chars=int(llm_input_cfg.get("max_visual_chars_per_chunk", 120)),
                    ),
                    "screen_text": compact_list(
                        item.get("screen_text", []),
                        max_items=int(llm_input_cfg.get("max_screen_text_items", 3)),
                    ),
                    "people": compact_list(
                        item.get("visible_people", []),
                        max_items=int(llm_input_cfg.get("max_people_items", 3)),
                    ),
                    "scene": item.get("scene_type", ""),
                    "visual_score": item.get("visual_value_score", 0),
                    "hook_score": item.get("hook_score", 0),
                    "flags": build_chunk_flags(item),
                }
            )

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

---

## 5.7 替换高光重组三个 Agent 输入

找到：

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
        "source_mode": self.manifest.get("source_mode", "single_source"),
        "source_videos": self.manifest.get("source_videos", []),
        "source_manifest": self.manifest.get("source_manifest", ""),
    })
```

替换为：

```python
def step_video_understanding(self) -> None:
    self._run_text_agent(
        "video_understanding",
        {
            "timeline_digest": self._load_llm_timeline_digest(),
            "source_mode": self.manifest.get("source_mode", "single_source"),
            "source_videos": self.manifest.get("source_videos", []),
            "source_manifest": self.manifest.get("source_manifest", ""),
        },
    )


def step_highlight_detection(self) -> None:
    self._run_text_agent(
        "highlight_detection",
        {
            "timeline_digest": self._load_llm_timeline_digest(),
            "video_analysis": self._load_step_json("video_understanding"),
        },
    )


def step_highlight_reassembly_plan(self) -> None:
    self._run_text_agent(
        "highlight_reassembly_plan",
        {
            "run_options": self._run_options_payload(),
            "reassembly_options": self._reassembly_options_payload(),
            "timeline_digest": self._load_llm_timeline_digest(),
            "video_analysis": self._load_step_json("video_understanding"),
            "candidate_clips": self._load_step_json("highlight_detection"),
            "source_mode": self.manifest.get("source_mode", "single_source"),
            "source_videos": self.manifest.get("source_videos", []),
            "source_manifest": self.manifest.get("source_manifest", ""),
        },
    )
```

---

## 5.8 `_run_text_agent()` 增加输入大小硬保护

找到：

```python
self._log_llm_input_size(step, input_data)
write_json(vdir / "input.json", input_data)
```

改成：

```python
self._log_llm_input_size(step, input_data)

max_input_chars = int(
    self.config.raw.get("llm_input", {}).get("max_text_agent_input_chars", 60000)
)
input_chars = len(json.dumps(input_data, ensure_ascii=False))
if input_chars > max_input_chars:
    raise RuntimeError(
        f"{step} LLM 输入过大：{input_chars} chars，超过 max_text_agent_input_chars={max_input_chars}。"
        "请检查是否误传完整 timeline/asr_segments，应改用 timeline_digest。"
    )

write_json(vdir / "input.json", input_data)
```

---

# 6. 修改 web_static/app.js

## 6.1 修正步骤列表

当前 AI 配音步骤列表还是旧的 `video_understanding/highlight_detection/short_video_planning/editing_script`，与当前 pipeline 不完全一致。

建议替换为：

```javascript
const AI_VOICEOVER_STEPS = [
  "source_prepare",
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
];

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

---

## 6.2 STEP_LABELS 增加新步骤

找到：

```javascript
const STEP_LABELS = {
```

补充：

```javascript
timeline_digest: "压缩分析时间线",
content_analysis: "内容分析",
short_video_edit_plan: "短视频剪辑规划",
```

---

## 6.3 STATUS_LABELS 增加公共复用状态

找到：

```javascript
const STATUS_LABELS = {
```

补充：

```javascript
waiting_common: "等待公共分析",
reusing_common: "复用公共分析",
cancelled: "已终止",
```

---

## 6.4 切换生产模式时强制刷新自动任务名

新增函数，放在 `syncSuggestedTaskIdForMode()` 附近：

```javascript
function hasModeAlias(value) {
  return /_(AI配音解说|视频重组|ai_voiceover|highlight_reassembly)_/.test(value)
    || /_(AI配音解说|视频重组|ai_voiceover|highlight_reassembly)$/.test(value);
}
```

找到 `syncSuggestedTaskIdForMode()`，替换为：

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

这样从 AI 配音切到视频重组时，任务名不会继续沿用旧的 `AI配音解说`。

---

# 7. 修改 config.toml

## 7.1 增加 LLM 输入硬上限

找到 `[llm_input]`：

```toml
[llm_input]
max_asr_chars_per_chunk = 320
max_visual_chars_per_chunk = 160
max_screen_text_items = 5
max_people_items = 5
max_candidate_clips_for_edit_plan = 8
candidate_neighbor_chunks = 1
```

建议替换为：

```toml
[llm_input]
max_asr_chars_per_chunk = 240
max_visual_chars_per_chunk = 120
max_screen_text_items = 3
max_people_items = 3
max_candidate_clips_for_edit_plan = 8
candidate_neighbor_chunks = 1
max_digest_chunks_for_text_agent = 120
max_text_agent_input_chars = 60000
```

---

# 8. 需要注意的兼容问题

## 8.1 `--outputs-dir` 必须确认 pipeline 支持

公共任务会调用：

```bash
--outputs-dir outputs/__common__
```

如果 `newsclip_agent/pipeline.py` 的 argparse 没有暴露 `--outputs-dir`，需要补：

```python
parser.add_argument("--outputs-dir", default="outputs")
```

`RunOptions` 里已经有：

```python
outputs_dir: str = "outputs"
```

所以只要 argparse 补上即可。

---

## 8.2 公共目录复制后，source_video 路径必须有效

公共 manifest 中的：

```json
"source_video": "input/source.mp4"
```

复制到生产任务后，生产任务目录也必须有：

```text
input/source.mp4
```

所以 `_copy_common_outputs_to_task()` 必须复制 `input/` 目录，不能只复制 metadata/asr/vision/timeline。

---

## 8.3 不建议跨目录引用 common 文件

短期不要让生产任务 manifest 的 `source_video` 指向：

```text
../__common__/common_xxx/input/source.mp4
```

虽然省空间，但会影响：

```text
Web 预览
任务打包
删除任务
路径安全检查
```

第一版建议直接复制。稳定后再考虑硬链接。

---

## 8.4 同时启动两个模式时，公共锁必须生效

如果不用锁，会出现：

```text
AI 配音任务正在跑 common_xxx
视频重组任务也开始跑 common_xxx
两个进程同时写同一个 manifest / 同一个 timeline 目录
```

所以 `_ensure_common_analysis()` 必须用：

```python
with file_slot_lock(f"common_source_{common_dir.name}", slots=1):
```

---

# 9. 推荐改造后的执行链路

## 9.1 用户同时启动 AI 配音解说

Web 请求：

```json
{
  "production_mode": "ai_voiceover",
  "source_items": [...],
  "output_mode": "multiple",
  "max_output_videos": 4
}
```

后端生成：

```text
task_id = 多源剪辑_2段_AI配音解说_xxx
common_task_id = common_abcd1234
source_request.json 写入 common_task_id
```

`run_multisource_pipeline.py`：

```text
1. 获取 common_abcd1234 锁
2. 发现 common 不存在
3. build_multi_source_video
4. run_pipeline --stop-after timeline_digest
5. 复制 common 到 AI 任务目录
6. run_pipeline --rerun-from content_analysis
```

## 9.2 用户同时启动视频重组

Web 请求：

```json
{
  "production_mode": "highlight_reassembly",
  "source_items": [...],
  "output_mode": "multiple",
  "max_output_videos": 4
}
```

后端生成：

```text
task_id = 多源剪辑_2段_视频重组_yyy
common_task_id = common_abcd1234
```

`run_multisource_pipeline.py`：

```text
1. 等待 common_abcd1234 锁
2. 发现 common 已完成
3. 复制 common 到视频重组任务目录
4. run_pipeline --rerun-from video_understanding
```

---

# 10. 验收标准

## 10.1 目录结构

运行同一批素材两个模式后，应该看到：

```text
outputs/
  __common__/
    common_abcd1234/
      input/
      metadata/
      preprocess/
      asr/
      vision/
      timeline/
      manifest.json

  多源剪辑_2段_AI配音解说_xxx/
      input/
      metadata/
      preprocess/
      asr/
      vision/
      timeline/
      agents/content_analysis/
      agents/short_video_edit_plan/
      agents/voiceover_script/

  多源剪辑_2段_视频重组_yyy/
      input/
      metadata/
      preprocess/
      asr/
      vision/
      timeline/
      agents/video_understanding/
      agents/highlight_detection/
      agents/highlight_reassembly/
```

---

## 10.2 日志

第一个任务日志应该出现：

```text
开始公共分析: outputs/__common__/common_abcd1234
完成: source_prepare
完成: metadata
完成: audio_extract
完成: frame_extract
完成: chunk_build
完成: asr
完成: vision
完成: timeline
完成: timeline_digest
完成公共分析: outputs/__common__/common_abcd1234
已复制公共分析产物到生产任务
从 content_analysis 继续执行
```

第二个任务日志应该出现：

```text
复用公共分析产物: outputs/__common__/common_abcd1234
已复制公共分析产物到生产任务
从 video_understanding 继续执行
```

不应该再次出现完整的：

```text
build_multi_source_video
完成: asr
完成: vision
完成: timeline
```

---

## 10.3 manifest

AI 配音任务 manifest 应有：

```json
{
  "common_task_id": "common_abcd1234",
  "reused_common_steps": [
    "source_prepare",
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "vision",
    "timeline",
    "timeline_digest"
  ],
  "last_web_run_options": {
    "production_mode": "ai_voiceover"
  }
}
```

视频重组任务 manifest 应有：

```json
{
  "common_task_id": "common_abcd1234",
  "reused_common_steps": [
    "source_prepare",
    "metadata",
    "audio_extract",
    "frame_extract",
    "chunk_build",
    "asr",
    "vision",
    "timeline",
    "timeline_digest"
  ],
  "last_web_run_options": {
    "production_mode": "highlight_reassembly"
  }
}
```

---

## 10.4 高光重组 LLM 输入大小

高光重组日志中应该看到：

```text
完成: timeline_digest
LLM input size [video_understanding]: < 60000 chars
```

不应该再看到：

```text
Total tokens of image and text exceed max message tokens
```

---

# 11. 建议提交顺序

建议分 4 个 commit：

## Commit 1：pipeline 支持 timeline_digest 和 stop-after

```text
newsclip_agent/pipeline.py
config.toml
web_static/app.js
```

提交信息：

```text
feat: add timeline digest to reassembly pipeline and support stop-after
```

## Commit 2：Web 写入 common_task_id

```text
web_app.py
```

提交信息：

```text
feat: generate common source key for multi-source jobs
```

## Commit 3：多源 runner 支持公共分析目录

```text
run_multisource_pipeline.py
```

提交信息：

```text
feat: reuse common intermediate outputs across production modes
```

## Commit 4：Web 显示与任务名修复

```text
web_static/app.js
web_app.py
```

提交信息：

```text
fix: preserve production mode in pending manifests and task names
```

---

# 12. 回滚方案

如果公共目录机制出问题，可以临时关闭复用。

在 `web_app.py` 写 `source_request.json` 时，先注释：

```python
"common_source_key": common_source_key,
"common_task_id": common_task_id,
```

或者新增配置：

```toml
[common_reuse]
enabled = false
```

然后 `web_app.py` 判断：

```python
common_reuse_enabled = bool(PROJECT_CONFIG.raw.get("common_reuse", {}).get("enabled", True))
if common_reuse_enabled:
    common_source_key = _common_source_key(req, raw_source_items)
    common_task_id = f"common_{common_source_key}"
else:
    common_source_key = ""
    common_task_id = ""
```

这样 `run_multisource_pipeline.py` 会自动走旧逻辑。

---


---

# 14. 补充：视频源指纹设计，兼容本地素材与远程 UCMS 素材

前面文档中提到 `common_source_key` 可以基于：

```text
视频 A 的绝对路径 / 文件大小 / 修改时间
视频 B 的绝对路径 / 文件大小 / 修改时间
视频顺序
aspect_ratio
chunk_seconds
frame_interval
```

这个说法只适合本地视频。实际项目里同时存在：

```text
local / video
remote / remote_ucms
```

所以 `common_source_key` 不能只用本地路径指纹，而应该基于“每个 source_item 的稳定身份指纹”。

最终规则应改为：

```text
common_source_key =
  每个 source_item 的稳定身份指纹
  + source_items 顺序
  + 公共分析参数
```

其中：

```text
本地素材：
  用本地文件指纹

远程素材：
  优先用远程系统稳定 ID / guid / 素材编号
  其次用标准化后的下载 URL / 媒体 URL
  最后才用标题、信号源、时长、创建时间等元信息兜底
```

---

## 14.1 为什么不能只用本地路径

本地素材有：

```text
path
size
mtime
```

但远程素材在创建任务时，可能还没有下载到本地，只有：

```text
remote_id
id
guid
download_url
media_high_url
media_low_url
preview_url
name
record_station
duration
created_at
```

如果强行等远程素材下载后再用本地路径算 key，会出现几个问题：

```text
1. 同一个远程素材重新下载后，本地缓存路径可能变化
2. mtime 每次下载都可能变化
3. 下载 URL 可能带临时 token，每次列表接口返回都不同
4. 任务还没 source_prepare 时无法计算本地文件指纹
```

所以远程素材必须在 Web 提交阶段就能计算稳定 identity。

---

## 14.2 source identity 分层策略

每个 source item 都应该先归一化成一个 `source_identity`。

### 本地素材 identity

本地素材第一版推荐：

```python
def _local_video_source_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "source_type": "local",
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime": round(stat.st_mtime, 3),
    }
```

这个方案优点是快，适合第一版。

缺点是：

```text
同一个视频复制到另一个路径，会被认为是不同素材。
```

如果希望“复制文件也能识别为同一素材”，可以增加 `quick_hash`。

```python
def _quick_file_hash(path: Path, block_size: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    size = path.stat().st_size

    with path.open("rb") as f:
        h.update(f.read(block_size))
        if size > block_size:
            f.seek(max(0, size - block_size))
            h.update(f.read(block_size))

    h.update(str(size).encode("utf-8"))
    return h.hexdigest()[:16]


def _local_video_source_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "source_type": "local",
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime": round(stat.st_mtime, 3),
        "quick_hash": _quick_file_hash(path),
    }
```

第一版建议仍然保留 `path`，避免误复用。后续如果要支持跨路径识别同一素材，可以增加配置：

```toml
[common_reuse]
local_identity_mode = "path"       # path / quick_hash
```

---

### 远程素材 identity

远程素材不能依赖本地路径。建议按优先级取身份信息：

```text
优先级 1：远程系统稳定 ID
  remote_id / id / guid / video_id / material_id

优先级 2：标准化后的媒体 URL
  download_url / media_high_url / mediaHigh / media_low_url / mediaLow / preview_url

优先级 3：元信息兜底
  name / display_name / record_station / duration / created_at
```

新增函数：

```python
def _remote_video_source_identity(remote_video: dict[str, Any]) -> dict[str, Any]:
    """
    远程素材 source identity。

    优先使用远程系统稳定 ID。
    如果没有稳定 ID，再用标准化后的媒体 URL。
    最后才用 name + record_station + duration + created_at 兜底。
    """
    stable_id = (
        remote_video.get("remote_id")
        or remote_video.get("id")
        or remote_video.get("guid")
        or remote_video.get("video_id")
        or remote_video.get("material_id")
    )

    record_station = str(
        remote_video.get("record_station")
        or remote_video.get("recordStation")
        or ""
    )

    if stable_id:
        return {
            "source_type": "remote_ucms",
            "identity_type": "stable_id",
            "stable_id": str(stable_id),
            "record_station": record_station,
        }

    url = (
        remote_video.get("download_url")
        or remote_video.get("media_high_url")
        or remote_video.get("mediaHigh")
        or remote_video.get("media_low_url")
        or remote_video.get("mediaLow")
        or remote_video.get("preview_url")
    )

    if url:
        return {
            "source_type": "remote_ucms",
            "identity_type": "url",
            "url": _normalize_remote_url(str(url)),
        }

    return {
        "source_type": "remote_ucms",
        "identity_type": "metadata_fallback",
        "name": str(remote_video.get("name") or remote_video.get("display_name") or ""),
        "record_station": record_station,
        "duration": str(remote_video.get("duration") or remote_video.get("duration_seconds") or ""),
        "created_at": str(remote_video.get("created_at") or remote_video.get("createTime") or ""),
    }
```

---

## 14.3 远程 URL 必须标准化

远程视频 URL 可能带临时签名参数，例如：

```text
https://example.com/video.mp4?token=aaa&expires=111
https://example.com/video.mp4?token=bbb&expires=222
```

这可能是同一个视频，但如果直接把完整 URL 放进 key，每次都会生成不同 `common_source_key`。

所以要新增 URL 标准化函数。

### 需要新增 import

文件：`web_app.py`

顶部 import 区新增：

```python
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
```

### 新增 URL 标准化函数

```python
_VOLATILE_QUERY_KEYS = {
    "token",
    "signature",
    "sign",
    "expires",
    "expire",
    "expiration",
    "ts",
    "timestamp",
    "access_token",
    "auth_key",
    "x-oss-signature",
    "x-oss-expires",
    "x-oss-credential",
    "x-oss-date",
    "x-amz-signature",
    "x-amz-expires",
    "x-amz-credential",
    "x-amz-date",
}


def _normalize_remote_url(url: str) -> str:
    parts = urlsplit(url.strip())

    query_items = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in _VOLATILE_QUERY_KEYS:
            continue
        query_items.append((key, value))

    normalized_query = urlencode(sorted(query_items))

    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            normalized_query,
            "",
        )
    )
```

---

## 14.4 修改 `_common_source_key()`

前文中的 `_common_source_key()` 需要替换为下面这个版本。

```python
def _common_source_key(req: RunRequest, items: list[dict[str, Any]]) -> str:
    """
    计算公共中间产物复用 key。

    common_source_key 不等于本地路径指纹。
    它应该由：
    1. 每个 source_item 的稳定身份指纹
    2. source_items 顺序
    3. 公共分析参数
    共同决定。

    不允许包含 production_mode / output_mode / max_output_videos / target_duration_seconds，
    否则 AI 配音和视频重组会生成不同 common key，无法复用。
    """
    payload_items: list[dict[str, Any]] = []

    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue

        source_type = str(item.get("source_type") or item.get("type") or "local")

        if source_type in {"local", "video"}:
            path_value = item.get("path") or item.get("input_video")
            path = _resolve_input_video(str(path_value))

            payload_items.append(
                {
                    "order": index,
                    **_local_video_source_identity(path),
                }
            )
            continue

        if source_type in {"remote", "remote_ucms"}:
            remote_video = item.get("remote_video") or item

            if not isinstance(remote_video, dict):
                remote_video = {}

            payload_items.append(
                {
                    "order": index,
                    **_remote_video_source_identity(remote_video),
                }
            )
            continue

        payload_items.append(
            {
                "order": index,
                "source_type": source_type,
                "raw": item,
            }
        )

    payload = {
        "sources": payload_items,
        "aspect_ratio": req.aspect_ratio,
        "chunk_seconds": req.chunk_seconds,
        "frame_interval": req.frame_interval,
        "mode": req.mode,
    }

    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
```

---

## 14.5 修改 `_source_request_fingerprint()`

`_source_request_fingerprint()` 是生产任务 key 的基础，它可以继续包含 `production_mode`、`output_mode` 等模式参数。

但它内部对 source item 的描述，也建议使用统一 identity，不要远程素材一会儿用 URL、一会儿用 name。

找到旧逻辑：

```python
if source_type in {"local", "video"}:
    path = _resolve_input_video(str(item.get("path") or item.get("input_video")))
    payload_items.append({"order": index, "source_type": "local", **_video_fingerprint(path)})
else:
    remote_video = item.get("remote_video") or item
    payload_items.append({"order": index, "source_type": "remote_ucms", "remote": _remote_video_fingerprint(remote_video)})
```

建议替换为：

```python
if source_type in {"local", "video"}:
    path = _resolve_input_video(str(item.get("path") or item.get("input_video")))
    payload_items.append(
        {
            "order": index,
            **_local_video_source_identity(path),
        }
    )
else:
    remote_video = item.get("remote_video") or item
    if not isinstance(remote_video, dict):
        remote_video = {}
    payload_items.append(
        {
            "order": index,
            **_remote_video_source_identity(remote_video),
        }
    )
```

后面的 payload 仍然保留：

```python
payload = {
    "sources": payload_items,
    "production_mode": req.production_mode,
    "output_mode": req.output_mode,
    "max_output_videos": req.max_output_videos,
    "aspect_ratio": req.aspect_ratio,
    "chunk_seconds": req.chunk_seconds,
    "frame_interval": req.frame_interval,
    "target_duration_seconds": req.target_duration_seconds,
    "reassembly_target_seconds": req.reassembly_target_seconds,
    "reassembly_max_clip_count": req.reassembly_max_clip_count,
}
```

---

## 14.6 下载后的远程文件只做校验，不改变 common_source_key

远程素材下载后，可以记录本地文件指纹，但不要重新计算 `common_source_key`。

原因：

```text
common_source_key 在 Web 提交阶段已经确定。
如果下载后再改 key，会导致任务目录、公共目录、索引全部错位。
```

建议在 `source_manifest.json` 或 manifest 的 `source_videos` 中增加：

```json
{
  "remote_identity": {
    "source_type": "remote_ucms",
    "identity_type": "stable_id",
    "stable_id": "123456",
    "record_station": "浦项高清卫星-凤凰咨询台"
  },
  "downloaded_file": {
    "path": "data/remote_videos/xxx.mp4",
    "size": 123456789,
    "quick_hash": "abc123"
  }
}
```

这部分用于排查和校验，不参与已有任务目录命名。

---

## 14.7 顺序是否参与 key

第一版必须顺序敏感：

```text
A + B
B + A
```

这两组素材应该生成不同 `common_source_key`。

原因是多源拼接后时间线不同：

```text
A 后接 B：0-10 分钟是 A，10-20 分钟是 B
B 后接 A：0-10 分钟是 B，10-20 分钟是 A
```

所以 `payload_items` 必须包含：

```python
"order": index
```

后续如果想提醒“素材相同但顺序不同”，可以额外做一个不带 order 的 `source_set_key`，只用于 UI 提醒，不用于公共分析复用。

---

## 14.8 最终规则总结

最终设计应改为：

```text
本地素材：
  默认用 path + size + mtime
  可选增强 quick_hash

远程素材：
  优先 stable_id / guid / remote_id / video_id / material_id
  其次 normalized media url
  最后才用 name + record_station + duration + created_at 兜底

common_source_key：
  source identity + source order + 公共分析参数

production_task_key：
  common_source_key + production_mode + 输出参数
```

这比“本地路径 + 文件大小 + 修改时间”更准确，能同时兼容本地视频和远程 UCMS 视频。

---

## 14.9 建议同步更新任务索引

如果你已经按照前文增加了：

```text
outputs/task_index.json
```

建议在索引里记录每个 source 的 identity，而不是只记录 source_names。

例如：

```json
{
  "version": 1,
  "common_sources": {
    "common_abcd1234": {
      "common_source_key": "abcd1234",
      "common_task_id": "common_abcd1234",
      "source_count": 2,
      "source_names": ["A.mp4", "B.mp4"],
      "source_identities": [
        {
          "order": 1,
          "source_type": "local",
          "path": "E:/ai/skills/AutomaticEditing/videos/A.mp4",
          "size": 123456,
          "mtime": 1780000000.123
        },
        {
          "order": 2,
          "source_type": "remote_ucms",
          "identity_type": "stable_id",
          "stable_id": "987654",
          "record_station": "浦项高清卫星-凤凰咨询台"
        }
      ],
      "common_dir": "outputs/__common__/common_abcd1234",
      "created_at": "2026-06-02T16:10:00",
      "updated_at": "2026-06-02T16:20:00"
    }
  }
}
```

这样下次排查“为什么这个任务复用/没复用”时，可以直接看索引。


---

# 15. 补充：原片预览刷新后消失问题修复方案

## 15.0 问题现象

当前 Web 页面“视频预览”区域有两个 Tab：

```text
最新成片
原片
```

你现在遇到的问题是：

```text
1. 点击“原片”后，能看到拼接原片和原始素材列表；
2. 页面自动刷新或手动刷新任务状态后；
3. 原片列表立刻消失；
4. 页面变成“当前任务还没有生成粗剪视频。”；
5. 需要再次点击“原片”才可能恢复。
```

截图中可以看到：

```text
正常状态：
  成片列表里有：
    拼接原片 input/source_multi.mp4
    日本事故躲避与武器出口保禁.mp4
    日本大分县坦克训练事故.mp4

刷新后异常状态：
  成片列表变成：
    当前任务还没有生成粗剪视频。
```

这个问题本质不是视频文件不存在，而是前端刷新逻辑把“原片模式”覆盖回了“最新成片模式”，并且在没有 draft 成片时，用错误提示覆盖了原片列表。

---

## 15.1 当前问题原因

### 原因一：前端没有保存当前预览 Tab 状态

目前页面虽然有“最新成片 / 原片”两个按钮，但前端状态里没有一个稳定字段记录：

```javascript
state.previewMode = "draft" | "source"
```

导致自动刷新时，`loadManifest()` / `loadLatestDraft()` 默认总是尝试加载最新成片。

当任务还没有生成粗剪视频时，`loadLatestDraft()` 会写：

```text
当前任务还没有生成粗剪视频。
```

于是把原片列表覆盖掉。

---

### 原因二：`playSourceVideo()` 没有返回“当前正在原片预览”的状态

现在点击原片按钮，大概率只是临时调用：

```javascript
playSourceVideo()
```

但刷新后 `loadLatestDraft()` 不知道用户当前选择的是“原片”，所以它继续执行“最新成片”的兜底逻辑。

---

### 原因三：`loadSourceVideos()` 对 source-videos 返回空时兜底不稳

后端 `/api/tasks/{task_id}/source-videos` 依赖 manifest：

```python
source = manifest.get("source_video")
```

如果任务运行中、`source_prepare` 尚未完成，或者 manifest fallback 还没写入 `source_video`，接口可能返回空列表。

前端看到空列表后，可能设置：

```text
当前任务还没有生成粗剪视频。
```

这会让用户误以为原片不存在。

---

### 原因四：多源任务 pending 阶段没有从 source_request.json 返回预览素材

多源任务创建后，最早存在的是：

```text
outputs/<task_id>/input/source_request.json
```

里面已经有 `source_items`，包括本地视频 path 或远程 preview_url / media_url。

即使 `manifest.source_video` 还没生成，也应该能用 `source_request.json` 返回原始素材预览。

---

# 15.2 修复目标

修复后应该满足：

```text
1. 用户点击“原片”后，previewMode 固定为 source；
2. 自动刷新任务状态时，不再把原片列表覆盖成“没有粗剪视频”；
3. 原片 Tab 下优先加载 /api/tasks/{task_id}/source-videos；
4. source-videos 即使在 source_prepare 未完成时，也能从 input/source_request.json 兜底返回；
5. “最新成片” Tab 下才显示“当前任务还没有生成粗剪视频”；
6. 原片 Tab 下如果还没准备好，应显示“原片正在准备中”，不能显示“没有粗剪视频”；
7. 刷新 manifest、刷新任务列表、自动刷新时，都不能改变用户当前选择的预览 Tab。
```

---

# 15.3 修改顺序

建议按这个顺序改：

```text
第一步：web_static/app.js 增加 state.previewMode
第二步：修改“最新成片 / 原片”两个按钮的点击事件
第三步：新增 setPreviewMode() / syncPreviewTabs()
第四步：修改 loadManifest() 调用最新视频加载的逻辑
第五步：修改 loadLatestDraft()，只在 draft 模式下显示无成片提示
第六步：修改 playSourceVideo() / loadSourceVideos()，让它们稳定渲染原片列表
第七步：web_app.py 增加 source_request.json 兜底预览
第八步：可选补充 CSS，保证 active Tab 状态稳定
```

---

# 15.4 修改 web_static/app.js

## 15.4.1 在 state 中增加 previewMode

文件：`web_static/app.js`

找到：

```javascript
const state = {
  selectedVideo: null,
  selectedRemoteVideo: null,
  sourceBasket: [],
  selectedTask: null,
  manifest: null,
  activeJob: null,
  logTimer: null,
  autoRefreshTimer: null,
  autoRefreshing: false,
  contextMenu: null,
  latestDraft: null,
  drafts: [],
```

改为：

```javascript
const state = {
  selectedVideo: null,
  selectedRemoteVideo: null,
  sourceBasket: [],
  selectedTask: null,
  manifest: null,
  activeJob: null,
  logTimer: null,
  autoRefreshTimer: null,
  autoRefreshing: false,
  contextMenu: null,
  latestDraft: null,
  drafts: [],
  previewMode: "draft", // draft = 最新成片，source = 原片
```

---

## 15.4.2 新增 preview mode 管理函数

在 `playSourceVideo()` 前面或 `loadLatestDraft()` 前面新增：

```javascript
function setPreviewMode(mode) {
  state.previewMode = mode === "source" ? "source" : "draft";
  syncPreviewTabs();
}


function syncPreviewTabs() {
  const draftButton = $("openDraft");
  const sourceButton = $("openSource");

  if (draftButton) {
    draftButton.classList.toggle("active", state.previewMode === "draft");
    draftButton.classList.toggle("primary", state.previewMode === "draft");
  }

  if (sourceButton) {
    sourceButton.classList.toggle("active", state.previewMode === "source");
    sourceButton.classList.toggle("primary", state.previewMode === "source");
  }
}
```

如果你的按钮样式不是 `.primary`，只保留 `.active` 也可以：

```javascript
draftButton.classList.toggle("active", state.previewMode === "draft");
sourceButton.classList.toggle("active", state.previewMode === "source");
```

---

## 15.4.3 修改“原片”按钮事件

找到：

```javascript
$("openSource").addEventListener("click", () => {
  playSourceVideo();
});
```

替换为：

```javascript
$("openSource").addEventListener("click", async () => {
  setPreviewMode("source");
  await playSourceVideo();
});
```

---

## 15.4.4 修改“最新成片”按钮事件

找到：

```javascript
$("openDraft").addEventListener("click", () => {
  if (state.drafts.length) {
    renderDraftList();
    playDraft(state.drafts[0]);
  }
  else if (state.latestDraft?.exists) playDraft(state.latestDraft);
  else alert("当前任务还没有生成粗剪视频。");
});
```

替换为：

```javascript
$("openDraft").addEventListener("click", async () => {
  setPreviewMode("draft");
  await loadLatestDraft();
});
```

这样“最新成片”的逻辑统一交给 `loadLatestDraft()`，避免多处写不同提示。

---

## 15.4.5 修改选择本地视频时的 previewMode

找到本地视频点击事件里类似：

```javascript
state.selectedTask = null;
state.manifest = null;
applyDefaultControls();
clearTaskPanels();
updateActiveTitleForSources();
playSourceVideo();
```

改为：

```javascript
state.selectedTask = null;
state.manifest = null;
state.previewMode = "source";
applyDefaultControls();
clearTaskPanels();
updateActiveTitleForSources();
setPreviewMode("source");
playSourceVideo();
```

目的：用户正在选素材时，默认应该预览原片，不应该进入“最新成片”。

---

## 15.4.6 修改选择远程视频时的 previewMode

找到 `selectRemoteVideo(video)` 或远程视频点击事件，里面如果有：

```javascript
playSourceVideo();
```

前面加：

```javascript
setPreviewMode("source");
```

推荐逻辑：

```javascript
function selectRemoteVideo(video) {
  state.selectedRemoteVideo = video;
  state.selectedVideo = null;
  state.selectedTask = null;
  state.manifest = null;
  setPreviewMode("source");
  updateActiveTitleForSources();
  playSourceVideo();
}
```

如果你的函数结构不同，就把 `setPreviewMode("source")` 加到远程素材选中后、预览前。

---

## 15.4.7 修改选择任务时不要重置 previewMode

选择任务时，很多代码会做：

```javascript
state.selectedTask = task.task_id;
state.selectedVideo = null;
state.selectedRemoteVideo = null;
```

这里不要强制：

```javascript
state.previewMode = "draft";
```

建议策略：

```text
用户选任务时：
  如果之前没有选择过 previewMode，默认 draft；
  如果用户已经点过原片，则保持 source。
```

可以这样写：

```javascript
if (!state.previewMode) {
  state.previewMode = "draft";
}
syncPreviewTabs();
```

---

# 15.5 修改 loadManifest()

找到 `loadManifest(taskId, options = {})` 函数。你需要检查里面是否有类似：

```javascript
if (refreshDrafts) await loadLatestDraft();
```

或者：

```javascript
if (updatePreview) await loadLatestDraft();
```

将其改成根据 `state.previewMode` 分流。

推荐写法：

```javascript
if (updatePreview) {
  if (state.previewMode === "source") {
    await playSourceVideo();
  } else {
    await loadLatestDraft();
  }
}
```

如果当前代码是：

```javascript
if (refreshDrafts) await loadLatestDraft();
```

替换为：

```javascript
if (refreshDrafts || updatePreview) {
  await refreshPreviewForCurrentMode();
}
```

并新增：

```javascript
async function refreshPreviewForCurrentMode() {
  syncPreviewTabs();

  if (state.previewMode === "source") {
    await playSourceVideo();
    return;
  }

  await loadLatestDraft();
}
```

---

# 15.6 修改 loadLatestDraft()

当前 `loadLatestDraft()` 大概率会在没有 draft 时写：

```javascript
$("fileViewer").textContent = "当前任务还没有生成粗剪视频。";
```

这个提示只应该在 `previewMode === "draft"` 时出现。

推荐把 `loadLatestDraft()` 改成：

```javascript
async function loadLatestDraft() {
  if (!state.selectedTask) {
    return;
  }

  const drafts = await api(`/api/tasks/${state.selectedTask}/drafts`).catch((error) => {
    $("fileViewer").textContent = `成片列表加载失败：${cleanError(error)}`;
    return [];
  });

  state.drafts = drafts || [];

  const latest = await api(`/api/tasks/${state.selectedTask}/latest-video`).catch(() => ({
    exists: false,
    file: "",
    url: "",
  }));

  state.latestDraft = latest;

  if (state.previewMode !== "draft") {
    return;
  }

  if (state.drafts.length) {
    renderDraftList();
    playDraft(state.drafts[0]);
    return;
  }

  if (state.latestDraft?.exists) {
    playDraft(state.latestDraft);
    return;
  }

  renderDraftList([]);
  $("fileViewer").textContent = "当前任务还没有生成粗剪视频。";
}
```

如果你不想整体替换，至少加这个保护：

```javascript
if (state.previewMode !== "draft") {
  return;
}
```

放在写“当前任务还没有生成粗剪视频”之前。

---

# 15.7 修改 renderDraftList()

当前 `renderDraftList()` 可能默认把列表区写成“当前任务还没有生成粗剪视频”。

建议改成支持参数：

```javascript
function renderDraftList(items = state.drafts) {
  const box = $("draftList");
  if (!box) return;

  const drafts = items || [];

  if (!drafts.length) {
    box.innerHTML = `<div class="empty-row">当前任务还没有生成粗剪视频。</div>`;
    return;
  }

  box.innerHTML = drafts
    .map((item) => {
      return `
        <button class="draft-item" type="button" data-url="${escapeAttr(item.url)}">
          <strong>${escapeHtml(item.name || item.file || "成片")}</strong>
          <span>${escapeHtml(item.file || "")}</span>
        </button>
      `;
    })
    .join("");

  box.querySelectorAll(".draft-item").forEach((button, index) => {
    button.addEventListener("click", () => playDraft(drafts[index]));
  });
}
```

但是注意：在原片模式下，不应该调用 `renderDraftList([])`，否则会覆盖原片列表。  
所以 `loadLatestDraft()` 里要先判断：

```javascript
if (state.previewMode !== "draft") return;
```

---

# 15.8 修改 playSourceVideo()

建议把当前 `playSourceVideo()` 整体替换为：

```javascript
async function playSourceVideo() {
  setPreviewMode("source");

  if (state.selectedTask) {
    await loadSourceVideos(state.selectedTask);
    return;
  }

  if (state.sourceBasket.length) {
    const sources = state.sourceBasket.map(sourceBasketPreviewItem);
    const previewItems = renderSourceVideoList(sources);
    playSourceItem(previewItems[previewItems.length - 1] || previewItems[0]);
    return;
  }

  if (state.selectedRemoteVideo) {
    const url =
      state.selectedRemoteVideo.preview_url ||
      state.selectedRemoteVideo.media_low_url ||
      state.selectedRemoteVideo.mediaLow ||
      state.selectedRemoteVideo.media_high_url ||
      state.selectedRemoteVideo.mediaHigh;

    if (url) {
      $("videoPreview").src = url;
      $("videoPreview").load();
      $("fileViewer").textContent = "正在预览远程原片。";
    } else {
      $("fileViewer").textContent = "该远程素材没有可预览地址。";
    }
    return;
  }

  if (state.selectedVideo) {
    $("videoPreview").src = `/api/videos/preview?path=${encodeURIComponent(state.selectedVideo.path)}`;
    $("videoPreview").load();
    $("fileViewer").textContent = "正在预览原片。";
    return;
  }

  $("fileViewer").textContent = "请先选择一个原片或任务。";
}
```

关键点：

```javascript
setPreviewMode("source");
```

必须放在函数开头，确保后续刷新不会把它当成 draft 模式。

---

# 15.9 修改 loadSourceVideos()

建议整体替换为：

```javascript
async function loadSourceVideos(taskId) {
  const sources = await api(`/api/tasks/${taskId}/source-videos`).catch((error) => {
    $("fileViewer").textContent = `源视频列表加载失败：${cleanError(error)}`;
    return [];
  });

  if (state.previewMode !== "source") {
    return;
  }

  if (sources.length) {
    const previewItems = renderSourceVideoList(sources);
    playSourceItem(previewItems[0]);
    return;
  }

  const video = $("videoPreview");
  video.onerror = () => {
    if (state.previewMode === "source") {
      $("fileViewer").textContent =
        "原片暂时无法预览：任务可能还在准备多源素材，source_video 尚未写入 manifest。请等待 source_prepare 完成后刷新。";
    }
  };

  video.src = `/api/tasks/${taskId}/preview`;
  video.load();
  $("fileViewer").textContent = "正在预览原片。";
}
```

这里的关键点：

```javascript
if (state.previewMode !== "source") {
  return;
}
```

防止异步请求返回较慢时，把 draft 模式的列表覆盖掉。

---

# 15.10 修改 renderSourceVideoList()

如果当前已有 `renderSourceVideoList(sources)`，要确保它只负责渲染原片列表，不要写“当前任务没有成片”。

推荐版本：

```javascript
function renderSourceVideoList(sources) {
  const box = $("draftList");
  if (!box) return [];

  const items = (sources || []).filter((item) => item && item.url);

  if (!items.length) {
    box.innerHTML = `<div class="empty-row">当前任务还没有可预览的原片，可能还在准备多源素材。</div>`;
    return [];
  }

  box.innerHTML = items
    .map((item, index) => {
      const label = item.label || item.name || `原片 ${index + 1}`;
      const time = item.virtual_start && item.virtual_end
        ? `${item.virtual_start} - ${item.virtual_end}`
        : "";
      return `
        <button class="draft-item source-item ${index === 0 ? "active" : ""}" type="button">
          <strong>${escapeHtml(label)}</strong>
          <span>${escapeHtml(time || item.file || "")}</span>
        </button>
      `;
    })
    .join("");

  const buttons = [...box.querySelectorAll(".source-item")];
  buttons.forEach((button, index) => {
    button.addEventListener("click", () => playSourceItem(items[index]));
  });

  return items;
}
```

注意：这里仍然复用 `draftList` 容器可以，但文案必须根据当前模式变化。  
如果以后要更清晰，可以把容器 id 改成 `previewList`，但第一版不必重构 HTML。

---

# 15.11 修改 playSourceItem()

确保点击原片列表时，仍然保持 source mode。

```javascript
function playSourceItem(item) {
  if (!item || !item.url) {
    $("fileViewer").textContent = "该原片没有可预览地址。";
    return;
  }

  setPreviewMode("source");

  const video = $("videoPreview");
  video.onerror = () => {
    if (state.previewMode === "source") {
      $("fileViewer").textContent = "原片加载失败，请检查文件是否存在或远程预览地址是否可访问。";
    }
  };

  video.src = item.url;
  video.load();

  $("fileViewer").textContent = `正在预览：${item.label || item.file || "原片"}`;
}
```

---

# 15.12 修改 web_app.py：source-videos 接口增加 source_request 兜底

## 15.12.1 新增 `_source_request_preview_items()`

文件：`web_app.py`

建议放在 `_resolve_task_source()` 后面。

```python
def _source_request_preview_items(task_dir: Path, task_id: str) -> list[dict[str, Any]]:
    """
    多源任务 source_prepare 尚未完成时，manifest.source_video 可能为空。
    此时从 input/source_request.json 返回原始素材预览，避免前端原片 Tab 显示为空。
    """
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
            except Exception:
                continue

            previews.append(
                {
                    "label": item.get("display_name") or path.name,
                    "file": relpath(path, ROOT),
                    "url": f"/api/videos/preview?path={relpath(path, ROOT)}",
                    "source_index": index,
                    "source_type": "local",
                }
            )
            continue

        if source_type in {"remote", "remote_ucms"}:
            remote = item.get("remote_video") or item
            if not isinstance(remote, dict):
                continue

            url = (
                remote.get("preview_url")
                or remote.get("media_low_url")
                or remote.get("mediaLow")
                or remote.get("media_high_url")
                or remote.get("mediaHigh")
                or remote.get("download_url")
            )

            if not url:
                continue

            previews.append(
                {
                    "label": item.get("display_name")
                    or remote.get("display_name")
                    or remote.get("name")
                    or remote.get("title")
                    or f"远程素材 {index}",
                    "file": remote.get("name") or remote.get("remote_id") or remote.get("id") or "",
                    "url": url,
                    "source_index": index,
                    "source_type": "remote_ucms",
                }
            )

    return previews
```

---

## 15.12.2 修改 `list_source_videos()`

找到：

```python
@app.get("/api/tasks/{task_id}/source-videos")
def list_source_videos(task_id: str) -> list[dict[str, Any]]:
    task_dir = _task_dir(task_id)
    manifest = get_manifest(task_id)
    sources: list[dict[str, Any]] = []
    source = manifest.get("source_video")
    if source:
        sources.append(
            {
                "label": "拼接原片" if manifest.get("source_mode") == "multi_source_concat_proxy" else "原片",
                "file": source,
                "url": f"/api/tasks/{task_id}/file?path={source}",
                "source_index": 0,
            }
        )
```

替换开头为：

```python
@app.get("/api/tasks/{task_id}/source-videos")
def list_source_videos(task_id: str) -> list[dict[str, Any]]:
    task_dir = _task_dir(task_id)
    manifest = get_manifest(task_id)
    sources: list[dict[str, Any]] = []

    source = manifest.get("source_video")

    # source_prepare 尚未完成时，从 source_request.json 兜底返回原始素材预览。
    if not source:
        pending_previews = _source_request_preview_items(task_dir, task_id)
        if pending_previews:
            return pending_previews

    if source:
        sources.append(
            {
                "label": "拼接原片" if manifest.get("source_mode") == "multi_source_concat_proxy" else "原片",
                "file": source,
                "url": f"/api/tasks/{task_id}/file?path={source}",
                "source_index": 0,
                "source_type": "concat" if manifest.get("source_mode") == "multi_source_concat_proxy" else "single",
            }
        )
```

后面的 `for index, item in enumerate(manifest.get("source_videos") or [], start=1):` 保留。

---

## 15.12.3 给 manifest source_videos 的远程项补 URL 兜底

在 `list_source_videos()` 的循环里，当前大概是：

```python
file_ref = item.get("normalized_file") or ""
url = f"/api/tasks/{task_id}/file?path={file_ref}" if file_ref else ""
original = item.get("original_path") or ""
if not url and original:
    try:
        original_path = _resolve_input_video(str(original))
        url = f"/api/videos/preview?path={relpath(original_path, ROOT)}"
    except HTTPException:
        url = ""
```

建议在这段后面补：

```python
remote = item.get("remote_video") or item.get("remote") or {}
if not url and isinstance(remote, dict):
    url = (
        remote.get("preview_url")
        or remote.get("media_low_url")
        or remote.get("mediaLow")
        or remote.get("media_high_url")
        or remote.get("mediaHigh")
        or remote.get("download_url")
        or ""
    )
```

最终 append 里补：

```python
"source_type": item.get("source_type", ""),
```

---

# 15.13 可选：修复按钮 active 样式

如果点击“原片”后按钮状态没有保持，可以在 `web_static/styles.css` 里增加：

```css
.segmented button.active,
.tab-button.active {
  background: #b1122a;
  color: #fff;
  border-color: #b1122a;
}
```

实际类名按你的 HTML 调整。  
如果当前按钮使用的是 `.button.primary`，那前面的 JS 里切换 `primary` class 即可。

---

# 15.14 验收标准

## 15.14.1 原片 Tab 刷新不消失

操作：

```text
1. 打开一个正在运行中的多源任务；
2. 点击“原片”；
3. 看到“拼接原片 + 原始素材列表”；
4. 等待自动刷新一次，或点击刷新；
5. 仍然停留在“原片”；
6. 列表不能变成“当前任务还没有生成粗剪视频。”
```

## 15.14.2 最新成片 Tab 仍然正常

操作：

```text
1. 点击“最新成片”；
2. 如果有成片，播放最新成片；
3. 如果没有成片，显示“当前任务还没有生成粗剪视频。”；
4. 这条提示只允许出现在“最新成片”模式下。
```

## 15.14.3 source_prepare 未完成时也能看原片

操作：

```text
1. 刚提交多源任务；
2. source_prepare 仍在 running；
3. 点击“原片”；
4. 页面应从 input/source_request.json 返回原始素材；
5. 至少能看到本地原始素材或远程 preview_url；
6. 不应该黑屏无提示。
```

## 15.14.4 异步请求不能互相覆盖

快速连续点击：

```text
原片 → 最新成片 → 原片
```

最终页面应该以最后一次点击为准。

如果最后点的是“原片”，即使 `loadLatestDraft()` 后返回，也不能覆盖原片列表。  
如果最后点的是“最新成片”，即使 `loadSourceVideos()` 后返回，也不能覆盖成片列表。

关键保护是：

```javascript
if (state.previewMode !== "source") return;
if (state.previewMode !== "draft") return;
```

---

# 15.15 最终结论

这次原片预览消失，不是 FFmpeg、视频文件或后端路径本身的问题，而是：

```text
前端没有持久化当前预览模式；
自动刷新默认回到最新成片逻辑；
最新成片为空时覆盖了原片列表；
后端在 source_prepare 未完成时没有 source_request 兜底。
```

正确修法是：

```text
前端：
  增加 state.previewMode
  所有刷新按 previewMode 分流
  原片模式下永远调用 loadSourceVideos()
  draft 模式下才调用 loadLatestDraft()

后端：
  /source-videos 在 manifest.source_video 为空时，读取 input/source_request.json 返回预览项
```

改完后，“原片”Tab 会像一个真正的状态，而不是一次性的按钮动作。刷新不会再把它覆盖掉。

# 13. 最终结论

本次优化不要再继续增强 `_seed_reusable_outputs()`。

`_seed_reusable_outputs()` 适合“从一个已有任务手动切换模式”的小场景，不适合“同一数据源同时启动两个生产模式”的并发场景。

真正需要的是：

```text
公共 source key
公共 common task
公共步骤只跑一次
模式任务复制公共产物后继续执行专属步骤
```

改完后，你的两个任务仍然是两个独立生产任务，但它们的前置分析会共享同一个公共中间产物目录，既不会互相覆盖，也不会重复跑 ASR、视觉分析和 timeline。
