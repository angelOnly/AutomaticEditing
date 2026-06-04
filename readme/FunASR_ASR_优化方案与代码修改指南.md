# FunASR / ASR 流程优化方案与代码修改指南

> 项目：凤凰新闻视频智能拆条系统  
> 分支：`clean-highlight-reassembly`  
> 入口：`python run_web.py`  
> 目标：解决 Web 任务卡在“语音转文字”、长音频一次性输入 FunASR、多源视频 ASR 粒度不合理、启动前缺少环境与模型检查等问题。

---

## 0. 当前问题结论

当前代码的 ASR 流程存在三个核心问题：

1. **服务启动前没有环境自检**  
   `run_web.py` 只负责找端口并启动 `uvicorn`，没有检查 FFmpeg、CUDA、FunASR、模型目录、模型能否加载、样例音频能否转写。

2. **ASR 对整条音频一次性调用 FunASR**  
   `newsclip_agent/pipeline.py` 的 `step_asr()` 读取 `audio_extract` 输出的整条 `audio.wav`，然后直接：

   ```python
   result = engine.transcribe(str(audio))
   ```

   这会导致长视频、多源拼接视频一次性进入模型，耗时不可控，也没有中间进度。

3. **多源任务先拼接成一个公共视频，再对拼接后的总音频做 ASR**  
   `run_multisource_pipeline.py` 会先 `build_multi_source_video()` 生成公共拼接视频，再跑公共分析 `--common-only`。这意味着多个源视频会被合并成一个音频后再 ASR。对于新闻拆条系统，这不是最优方案。

---

## 1. 设计目标

优化后的 ASR 体系应满足：

1. 启动 Web 前可选择执行环境自检。
2. 自检包含：
   - Python 包是否存在。
   - FFmpeg / FFprobe 是否可用。
   - CUDA 是否可用。
   - FunASR 模型目录是否完整。
   - FunASR 模型是否能加载。
   - 可选：使用极短音频做一次 smoke test。
3. 单源视频不要整条输入 FunASR，而是按 chunk 或固定窗口分段 ASR。
4. 多源视频优先按“原始素材单个转写”，再映射到拼接时间轴，而不是拼接后整条转写。
5. ASR 过程中要有 Web 可见的进度：等待锁、加载模型、转写第几段、已完成多少段、当前音频长度。
6. ASR 失败时能定位到具体 source / chunk，而不是只显示“ASR失败”。
7. 兼容现有 Web 启动方式：`python run_web.py`。
8. 兼容现有后续流程：`vision -> timeline -> timeline_digest` 仍然读取统一的 `asr_segments.json`。

---

## 2. FunASR 最长能解析多长音频？应该怎么处理？

### 2.1 不建议依赖“最长支持时长”

FunASR 的实际可处理音频长度取决于：

- 模型类型，例如 SenseVoiceSmall。
- 是否启用 VAD。
- `batch_size_s` 参数。
- GPU 显存。
- 音频采样率、声道数、静音比例。
- 当前机器上是否还有其他模型占用 GPU。

即使某些情况下可以处理十几分钟甚至更长音频，也不适合在生产流水线中一次性输入。

### 2.2 推荐策略

**不要截断整条音频。** 直接截断会丢内容，影响高光识别。正确做法是：

1. 将音频按时间窗口切分。
2. 每段分别调用 FunASR。
3. 把每段 ASR 结果的时间戳加上 offset。
4. 合并成统一的 `asr_segments.json`。

推荐默认参数：

```toml
[funasr]
segment_seconds = 60
segment_overlap_seconds = 1.0
max_segment_seconds = 90
smoke_test_seconds = 3
startup_check = true
startup_smoke_test = false
```

说明：

- `segment_seconds = 60`：每 60 秒转写一次，稳定性和速度比较均衡。
- `segment_overlap_seconds = 1.0`：段间保留 1 秒重叠，减少切断句子的问题。
- `max_segment_seconds = 90`：强制保护，避免单段太长。
- `startup_smoke_test = false`：默认不做真实推理，只检查模型可加载；需要排查时再打开。

---

## 3. 单源视频 ASR 新流程

当前流程：

```text
source video
  -> audio_extract 得到整条 audio.wav
  -> FunASR 一次性 transcribe(audio.wav)
  -> asr_segments.json
```

优化后：

```text
source video
  -> audio_extract 得到整条 audio.wav
  -> chunk_build 得到 chunk 时间段
  -> 按 chunk 或 fixed segment 切 audio_0001.wav / audio_0002.wav ...
  -> FunASR 逐段转写
  -> 每段结果加 offset
  -> 合并 asr_segments.json
```

最终对后续步骤仍然输出：

```text
asr/v*/asr_segments.json
asr/v*/full_text.txt
```

这样 `vision`、`timeline` 基本不用改。

---

## 4. 多源视频 ASR 新流程

### 4.1 当前多源问题

当前多源流程大致是：

```text
source_items 多个原始视频
  -> build_multi_source_video 拼接 / 规范化
  -> 得到 common input/source.mp4
  -> audio_extract 提取拼接后整条 audio.wav
  -> FunASR 转写整条拼接音频
```

这有几个问题：

1. 多个视频合并后音频更长，ASR更容易慢或卡。
2. 某一个源视频 ASR 失败，会影响全部多源任务。
3. 不能复用单个源视频的 ASR 缓存。
4. 不方便定位是哪一个原始素材导致 ASR 变慢。
5. 拼接边界处可能影响 VAD 和标点。

### 4.2 推荐多源策略

多源视频应该优先：**单个源视频独立 ASR，然后映射到公共拼接时间轴。**

优化后流程：

```text
source_items
  -> source_001.mp4 单独提音频 -> 分段 ASR -> source_001_asr.json
  -> source_002.mp4 单独提音频 -> 分段 ASR -> source_002_asr.json
  -> source_003.mp4 单独提音频 -> 分段 ASR -> source_003_asr.json
  -> build_multi_source_video 生成 source_manifest.json
  -> 读取每个 source 的 virtual_start / virtual_end / source_index
  -> 把单源 ASR 时间戳映射到拼接时间轴
  -> common/asr/v*/asr_segments.json
```

映射公式：

```python
merged_start = source_virtual_start + source_local_start
merged_end = source_virtual_start + source_local_end
```

每个 segment 建议保留这些字段：

```json
{
  "start": 123.4,
  "end": 127.9,
  "text": "……",
  "source_index": 1,
  "source_id": "src_001",
  "source_start": 3.4,
  "source_end": 7.9,
  "source_display_name": "浦项高清卫星-凤凰咨询台-xxx.mp4"
}
```

这样后续 timeline 既能按拼接时间轴处理，也能回溯原始素材。

---

## 5. 需要修改的文件清单

```text
run_web.py
config.toml
services/asr/FunASREngine.py
newsclip_agent/asr_preflight.py          新增
newsclip_agent/asr_segments.py           新增，可选
newsclip_agent/pipeline.py
run_multisource_pipeline.py              中期改造
newsclip_agent/multisource.py            视 source_manifest 字段情况少量调整
web_static/index.html                    可选，仅用于展示 ASR message
```

优先级：

1. `config.toml`：增加 ASR 参数。
2. `services/asr/FunASREngine.py`：增加模型路径校验、加载日志、转写日志。
3. `newsclip_agent/asr_preflight.py`：新增环境自检。
4. `run_web.py`：启动前调用自检。
5. `newsclip_agent/pipeline.py`：单源分段 ASR + 进度更新。
6. `run_multisource_pipeline.py`：多源单独 ASR + 时间轴映射。

---

## 6. config.toml 修改

新增或修改：

```toml
[funasr]
asr_model_path = "models/iic"
device = "cuda:0"
segment_seconds = 60
segment_overlap_seconds = 1.0
max_segment_seconds = 90
startup_check = true
startup_smoke_test = false
smoke_test_seconds = 3

[web_concurrency]
max_running_jobs = 1
max_pending_jobs = 20
same_task_policy = "reject"

[gpu_limits]
asr_slots = 1
tts_slots = 1
render_slots = 1
```

说明：

- `max_running_jobs` 建议先设为 1，等稳定后再恢复 2 或 3。
- `asr_slots = 1` 保持不变，避免多个 ASR 同时占 GPU。
- `startup_smoke_test = false` 是为了避免每次启动 Web 都真的跑一次 ASR 推理，启动太慢。排查环境时可改成 true。

---

## 7. 新增 newsclip_agent/asr_preflight.py

新增文件：`newsclip_agent/asr_preflight.py`

```python
from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any


def _check_cmd(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    if not path:
        return {"ok": False, "name": name, "error": f"{name} not found in PATH"}
    try:
        proc = subprocess.run(
            [name, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        first_line = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
        return {"ok": proc.returncode == 0, "name": name, "path": path, "version": first_line}
    except Exception as exc:
        return {"ok": False, "name": name, "path": path, "error": str(exc)}


def _check_python_imports() -> list[dict[str, Any]]:
    results = []
    for module in ["torch", "funasr"]:
        try:
            __import__(module)
            results.append({"ok": True, "module": module})
        except Exception as exc:
            results.append({"ok": False, "module": module, "error": str(exc)})
    return results


def _check_cuda() -> dict[str, Any]:
    try:
        import torch

        available = bool(torch.cuda.is_available())
        return {
            "ok": available,
            "available": available,
            "device_count": torch.cuda.device_count() if available else 0,
            "device_name": torch.cuda.get_device_name(0) if available else "",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _required_model_dirs(model_base: Path) -> dict[str, Path]:
    return {
        "SenseVoiceSmall": model_base / "SenseVoiceSmall",
        "vad": model_base / "speech_fsmn_vad_zh-cn-16k-common-pytorch",
        "punc": model_base / "punc_ct-transformer_cn-en-common-vocab471067-large",
    }


def _check_model_dirs(model_base: Path) -> list[dict[str, Any]]:
    results = []
    for name, path in _required_model_dirs(model_base).items():
        ok = path.exists() and path.is_dir()
        results.append({
            "ok": ok,
            "name": name,
            "path": str(path),
            "error": "missing model directory" if not ok else "",
        })
    return results


def _make_silent_wav(path: Path, seconds: float = 1.0, sample_rate: int = 16000) -> None:
    frame_count = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * frame_count)


def run_asr_preflight(config, *, smoke_test: bool = False) -> dict[str, Any]:
    root = config.root_dir
    funasr_cfg = config.funasr
    model_base = config.resolve_path(funasr_cfg.get("asr_model_path"), "models/iic")
    device = str(funasr_cfg.get("device", "cuda:0"))

    report: dict[str, Any] = {
        "ok": True,
        "ffmpeg": _check_cmd("ffmpeg"),
        "ffprobe": _check_cmd("ffprobe"),
        "imports": _check_python_imports(),
        "cuda": _check_cuda(),
        "model_base": str(model_base),
        "models": _check_model_dirs(model_base),
        "model_load": None,
        "smoke_test": None,
    }

    checks = [report["ffmpeg"], report["ffprobe"], *report["imports"], *report["models"]]
    if any(not item.get("ok") for item in checks):
        report["ok"] = False
        return report

    try:
        from services.asr.FunASREngine import FunASREngine

        engine = FunASREngine(
            model_name=str(model_base / "SenseVoiceSmall"),
            vad_model=str(model_base / "speech_fsmn_vad_zh-cn-16k-common-pytorch"),
            punc_model=str(model_base / "punc_ct-transformer_cn-en-common-vocab471067-large"),
            device=device,
        )
        engine._load_model(device)
        report["model_load"] = {"ok": True, "device": device}

        if smoke_test:
            with tempfile.TemporaryDirectory() as td:
                wav_path = Path(td) / "silent.wav"
                _make_silent_wav(wav_path, seconds=float(funasr_cfg.get("smoke_test_seconds", 3)))
                result = engine.transcribe(str(wav_path))
                report["smoke_test"] = {
                    "ok": bool(result.get("success")),
                    "elapsed": result.get("elapsed"),
                    "error": result.get("error"),
                }
                if not result.get("success"):
                    report["ok"] = False

        engine.release()
    except Exception as exc:
        report["model_load"] = {"ok": False, "error": str(exc)}
        report["ok"] = False

    return report
```

---

## 8. 修改 run_web.py：启动前自检

当前 `run_web.py` 只启动 Web。建议改成：

```python
import socket
import sys

import uvicorn

from newsclip_agent.config import load_config
from newsclip_agent.asr_preflight import run_asr_preflight


def find_free_port(host: str = "127.0.0.1", start: int = 7860, limit: int = 20) -> int:
    for port in range(start, start + limit):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) != 0:
                return port
    raise RuntimeError(f"没有找到可用端口: {start}-{start + limit - 1}")


def preflight() -> None:
    config = load_config("config.toml")
    funasr_cfg = config.funasr
    if not bool(funasr_cfg.get("startup_check", True)):
        print("跳过启动前 ASR 环境自检")
        return

    print("启动前检查：FFmpeg / CUDA / FunASR / 模型路径 ...")
    report = run_asr_preflight(
        config,
        smoke_test=bool(funasr_cfg.get("startup_smoke_test", False)),
    )

    if not report.get("ok"):
        print("启动前检查失败：")
        print(report)
        raise SystemExit(2)

    print("启动前检查通过")


if __name__ == "__main__":
    # 支持临时跳过：python run_web.py --no-preflight
    if "--no-preflight" not in sys.argv:
        preflight()
    else:
        sys.argv.remove("--no-preflight")

    host = "127.0.0.1"
    port = find_free_port(host=host, start=7860)
    print(f"Web 工作台地址: http://{host}:{port}")
    uvicorn.run("web_app:app", host=host, port=port, reload=False)
```

这样保持兼容：

```bash
python run_web.py
```

也支持排查时跳过：

```bash
python run_web.py --no-preflight
```

---

## 9. 修改 services/asr/FunASREngine.py

### 9.1 增加模型目录校验

在 class 里新增：

```python
def _assert_model_path(self, path: str, label: str):
    if not path:
        raise FileNotFoundError(f"{label} path is empty")
    if not os.path.exists(path):
        raise FileNotFoundError(f"{label} not found: {path}")
    logger.info(f"{label}: {path}")
```

### 9.2 在 _load_model() 里加载前检查

在 `from funasr import AutoModel` 前后加入：

```python
self._assert_model_path(self.model_name, "asr_model")
self._assert_model_path(self.vad_model_name, "vad_model")
self._assert_model_path(self.punc_model_name, "punc_model")
```

完整关键片段：

```python
logger.info(f"Loading FunASR model on {target_device}...")
logger.info(f"Resolved ASR model: {self.model_name}")
logger.info(f"Resolved VAD model: {self.vad_model_name}")
logger.info(f"Resolved PUNC model: {self.punc_model_name}")

self._assert_model_path(self.model_name, "asr_model")
self._assert_model_path(self.vad_model_name, "vad_model")
self._assert_model_path(self.punc_model_name, "punc_model")

from funasr import AutoModel
self._model = AutoModel(
    model=self.model_name,
    vad_model=self.vad_model_name,
    punc_model=self.punc_model_name,
    device=target_device,
    trust_remote_code=True,
)
```

### 9.3 在 transcribe() 增加日志

```python
logger.info("FunASR transcribe: loading model...")
self._load_model(device)
logger.info("FunASR transcribe: model loaded, start generate...")

start_time = time.time()
res = self._model.generate(
    input=audio_path,
    batch_size_s=batch_size_s,
    language=language,
)
logger.info("FunASR transcribe: generate finished")
```

---

## 10. 修改 newsclip_agent/pipeline.py：单源分段 ASR

### 10.1 新增进度更新函数

放在 `PipelineRunner` 类里：

```python
def _update_step_progress(self, step: str, message: str, extra: dict[str, Any] | None = None) -> None:
    data = {
        "step_name": step,
        "status": "running",
        "updated_at": now_iso(),
        "can_rerun": False,
        "message": message,
    }
    if extra:
        data.update(extra)
    self.manifest.setdefault("steps", {}).setdefault(step, {}).update(data)
    self._save_manifest()
```

### 10.2 新增音频切段函数

放在 `PipelineRunner` 类里：

```python
def _cut_audio_segment(self, audio: Path, out: Path, start: float, end: float) -> None:
    ensure_dir(out.parent)
    run_cmd([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(max(0.0, start)),
        "-to", str(max(start, end)),
        "-i", str(audio),
        "-ac", "1",
        "-ar", "16000",
        str(out),
    ])
```

### 10.3 新增 ASR 分段生成函数

```python
def _asr_windows_from_chunks(self, audio_duration: float | None = None) -> list[dict[str, Any]]:
    funasr_cfg = self.config.funasr
    segment_seconds = float(funasr_cfg.get("segment_seconds", 60))
    overlap = float(funasr_cfg.get("segment_overlap_seconds", 0))
    max_segment_seconds = float(funasr_cfg.get("max_segment_seconds", 90))
    segment_seconds = min(segment_seconds, max_segment_seconds)

    try:
        chunks = self._load_step_json("chunk_build").get("chunks", [])
    except Exception:
        chunks = []

    if chunks:
        windows = []
        for chunk in chunks:
            start = float(chunk.get("start", 0))
            end = float(chunk.get("end", start))
            if end <= start:
                continue
            windows.append({
                "id": chunk.get("chunk_id") or f"asr_{len(windows)+1:04d}",
                "start": max(0.0, start - overlap),
                "end": end + overlap,
                "chunk_id": chunk.get("chunk_id", ""),
            })
        return windows

    if not audio_duration:
        return [{"id": "asr_0001", "start": 0.0, "end": 0.0, "chunk_id": ""}]

    windows = []
    t = 0.0
    while t < audio_duration:
        end = min(audio_duration, t + segment_seconds)
        windows.append({"id": f"asr_{len(windows)+1:04d}", "start": t, "end": end, "chunk_id": ""})
        t = max(end - overlap, end)
    return windows
```

### 10.4 替换 step_asr()

将原来的整条音频 ASR 替换为：

```python
def step_asr(self) -> None:
    audio = self.task_dir / self._step_output("audio_extract")
    input_hash = stable_hash({
        "audio": self._step_output("audio_extract"),
        "funasr": self.config.funasr,
        "mode": "segmented_asr_v1",
        "chunks": self._step_version("chunk_build"),
    })
    if self._can_reuse("asr", input_hash):
        print("复用缓存: asr")
        return

    version, vdir = self._version_dir("asr", "asr")
    ensure_dir(vdir)
    write_json(vdir / "input.json", {"audio": relpath(audio, self.task_dir), "config": self.config.funasr})

    all_segments: list[dict[str, Any]] = []
    full_text_parts: list[str] = []
    raw_results: list[dict[str, Any]] = []
    err = ""
    out = vdir / "asr_segments.json"

    try:
        asr_slots = int(self.config.raw.get("gpu_limits", {}).get("asr_slots", 1))
        windows = self._asr_windows_from_chunks()

        self._update_step_progress("asr", "等待 ASR GPU 锁", {
            "total_segments": len(windows),
            "processed_segments": 0,
        })
        print("ASR: waiting gpu slot...", flush=True)

        with file_slot_lock("asr", slots=asr_slots):
            self._update_step_progress("asr", "加载 FunASR 模型", {
                "total_segments": len(windows),
                "processed_segments": 0,
            })
            print("ASR: loading FunASR model...", flush=True)
            engine = self._get_asr_engine()

            for index, window in enumerate(windows, start=1):
                wid = str(window["id"])
                start = float(window["start"])
                end = float(window["end"])
                seg_audio = vdir / "segments_audio" / f"{wid}.wav"

                self._update_step_progress("asr", f"切分音频 {index}/{len(windows)}", {
                    "current_segment": wid,
                    "processed_segments": index - 1,
                    "total_segments": len(windows),
                    "current_range": [start, end],
                })
                self._cut_audio_segment(audio, seg_audio, start, end)

                self._update_step_progress("asr", f"FunASR 转写中 {index}/{len(windows)}", {
                    "current_segment": wid,
                    "processed_segments": index - 1,
                    "total_segments": len(windows),
                    "current_range": [start, end],
                })
                print(f"ASR: transcribing {wid} {index}/{len(windows)} {start:.1f}-{end:.1f}s", flush=True)

                result = engine.transcribe(str(seg_audio))
                raw_results.append({"id": wid, "range": [start, end], "result": result})

                if not result.get("success"):
                    raise RuntimeError(f"{wid} ASR failed: {result.get('error')}")

                full_text_parts.append(result.get("text", ""))
                for seg in result.get("segments", []):
                    local_start = float(seg.get("start") or 0)
                    local_end = float(seg.get("end") or 0)
                    text = str(seg.get("text") or "").strip()
                    if not text:
                        continue
                    all_segments.append({
                        "start": start + local_start,
                        "end": start + local_end if local_end > 0 else end,
                        "text": text,
                        "asr_segment_id": wid,
                        "chunk_id": window.get("chunk_id", ""),
                    })

                self._update_step_progress("asr", f"FunASR 已完成 {index}/{len(windows)}", {
                    "current_segment": wid,
                    "processed_segments": index,
                    "total_segments": len(windows),
                })

        full_text = " ".join(x for x in full_text_parts if x).strip()
        segments = self._normalize_asr_segments(all_segments, full_text)
        asr = {
            "language": "zh",
            "segments": segments,
            "full_text": full_text,
            "raw_result": {
                "mode": "segmented_asr_v1",
                "segment_count": len(windows),
                "results": raw_results,
            },
        }
        out = write_json(vdir / "asr_segments.json", asr)
        write_text(vdir / "full_text.txt", asr["full_text"])
        status = "success"
    except Exception as exc:
        err = str(exc)
        asr = {"language": "zh", "segments": [], "full_text": "", "error": err, "raw_result": raw_results}
        out = write_json(vdir / "asr_segments.json", asr)
        write_text(vdir / "full_text.txt", "")
        status = "failed"

    step_status = self._base_status("asr", version, input_hash, [out])
    if err:
        step_status["error"] = err
    self._write_status(vdir, step_status)
    self._record_step(step="asr", version=version, status=status, output=relpath(out, self.task_dir), input_hash=input_hash, output_files=[out])
    if status != "success":
        raise RuntimeError(f"ASR 失败: {err}")
    print("完成: asr", flush=True)
```

---

## 11. 多源 ASR 改造方案

多源建议分两阶段改。

### 11.1 第一阶段：仍然拼接视频，但 ASR 分段

这是最小改动。只要完成第 10 节的 `pipeline.py` 分段 ASR，多源任务即使还是对拼接后音频 ASR，也不会整条一次性输入模型。

优点：改动小。  
缺点：不能复用单源 ASR，定位问题仍不够精细。

### 11.2 第二阶段：多源单独 ASR，再映射时间轴

需要改 `run_multisource_pipeline.py`。

新增函数：

```python
def _run_source_level_asr(
    *,
    common_dir: Path,
    sources: list[MultiSourceItem],
    built: dict[str, Any],
    config: Any,
) -> Path:
    """对每个原始 source 单独 ASR，并合并到 common timeline。"""
    # 1. 读取 built source_manifest，拿到每个源在拼接视频中的 virtual_start
    # 2. 每个源单独提取 audio
    # 3. 调用 FunASR 分段转写
    # 4. local timestamp + virtual_start
    # 5. 写 common_dir/asr/v1/asr_segments.json
    raise NotImplementedError
```

建议输出目录：

```text
outputs/__common__/common_xxx/asr_sources/source_001/audio.wav
outputs/__common__/common_xxx/asr_sources/source_001/asr_segments.json
outputs/__common__/common_xxx/asr_sources/source_002/audio.wav
outputs/__common__/common_xxx/asr_sources/source_002/asr_segments.json
outputs/__common__/common_xxx/asr/v1/asr_segments.json
```

合并后的 `asr/v1/asr_segments.json` 仍保持兼容：

```json
{
  "language": "zh",
  "segments": [],
  "full_text": "",
  "raw_result": {
    "mode": "multi_source_source_level_asr_v1",
    "source_count": 2
  }
}
```

### 11.3 source_manifest 需要包含时间映射

如果当前 `build_multi_source_video()` 的 manifest 里已经有类似字段，直接复用：

```json
{
  "source_index": 1,
  "source_id": "src_001",
  "normalized_file": "...",
  "duration_seconds": 123.4,
  "virtual_start": 0.0,
  "virtual_end": 123.4
}
```

如果没有，需要在 `newsclip_agent/multisource.py` 中补充。

建议字段：

```python
{
    "source_index": index,
    "source_id": item.source_id,
    "source_type": item.source_type,
    "display_name": item.display_name,
    "original_path": str(item.path),
    "normalized_file": relpath(normalized_path, task_dir),
    "duration_seconds": duration,
    "virtual_start_seconds": timeline_cursor,
    "virtual_end_seconds": timeline_cursor + duration,
}
```

### 11.4 多源 ASR 的落地顺序

推荐先别直接大改 `_ensure_common_analysis()`，而是：

1. 先实现单源分段 ASR。
2. 确认多源拼接后分段 ASR 稳定。
3. 再新增 source-level ASR。
4. 在 config 中加开关：

```toml
[multi_source]
asr_mode = "source_level" # concat_level / source_level
```

然后在 `run_multisource_pipeline.py` 中：

```python
multi_source_config = config.raw.get("multi_source", {})
asr_mode = str(multi_source_config.get("asr_mode", "concat_level"))

if asr_mode == "source_level":
    # build source manifest
    # run source-level asr
    # pipeline_main 从 vision 开始，而不是从 metadata/audio/asr 开始
else:
    # 走现有 common pipeline，但 ASR 已经是 segmented
```

---

## 12. Web 展示优化

当前后端 manifest 里只要增加了：

```json
{
  "steps": {
    "asr": {
      "status": "running",
      "message": "FunASR 转写中 3/11",
      "processed_segments": 2,
      "total_segments": 11,
      "current_segment": "chunk_0003"
    }
  }
}
```

前端如果已经展示 step 详情，就能看到。若前端只显示固定文案，需要在 `web_static/index.html` 找到步骤渲染逻辑，把 `step.message` 展示出来。

伪代码：

```javascript
const sub = step.message || step.reason || step.error || "等待流程推进";
```

这样“语音转文字”不再只是运行中，而是能看到具体进度。

---

## 13. 回滚与兼容策略

建议加配置开关，避免一次性切死：

```toml
[funasr]
asr_mode = "segmented" # full / segmented

[multi_source]
asr_mode = "concat_level" # concat_level / source_level
```

在 `step_asr()` 中：

```python
asr_mode = str(self.config.funasr.get("asr_mode", "segmented"))
if asr_mode == "full":
    # 保留旧逻辑
else:
    # 新分段逻辑
```

这样如果新逻辑有问题，可以临时改配置恢复旧逻辑。

---

## 14. 测试步骤

### 14.1 启动前自检

```bash
python run_web.py
```

预期：

```text
启动前检查：FFmpeg / CUDA / FunASR / 模型路径 ...
启动前检查通过
Web 工作台地址: http://127.0.0.1:7860
```

如果失败，会打印具体模型目录或依赖错误。

### 14.2 单源 ASR 测试

```bash
python run_pipeline.py \
  --input videos/test.mp4 \
  --task-id test_asr_segmented \
  --rerun-from asr \
  --skip-tts \
  --skip-render
```

查看：

```text
outputs/test_asr_segmented/asr/v1/asr_segments.json
outputs/test_asr_segmented/asr/v1/full_text.txt
outputs/test_asr_segmented/manifest.json
```

### 14.3 多源测试

Web 中选择两个视频跑多源任务，观察：

```text
outputs/__common__/common_xxx/manifest.json
outputs/__common__/common_xxx/asr/v1/asr_segments.json
```

manifest 里 ASR 应该出现：

```json
"message": "FunASR 转写中 3/11",
"processed_segments": 2,
"total_segments": 11
```

---

## 15. 最终建议实施顺序

### 第一步：立即做

1. `config.toml` 把 `max_running_jobs` 改成 1。
2. `FunASREngine.py` 增加模型路径校验和日志。
3. `pipeline.py` 的 `step_asr()` 增加进度 message。
4. 新增 `asr_preflight.py`，`run_web.py` 启动前检查。

### 第二步：短期做

5. `pipeline.py` 把整条 ASR 改成分段 ASR。
6. 输出 `processed_segments / total_segments / current_segment`。
7. 确保后续 `vision / timeline` 无需改动。

### 第三步：中期做

8. 多源任务改为 source-level ASR。
9. 单个源视频 ASR 结果可缓存复用。
10. 合并时映射到公共拼接时间轴。

---

## 16. 关键判断

你的三个判断方向都是对的：

1. **服务启动前必须检查环境和模型能否正常加载。**  
   否则任务跑起来后才发现模型缺失，会表现成 Web 卡死。

2. **FunASR 不应该直接吃超长音频。**  
   不应该截断，而应该分段转写，再合并时间戳。

3. **多源视频不应该优先合并后再做整条 ASR。**  
   更合理的是单源独立转写，再映射到拼接时间轴。这样更快、更稳、更容易缓存，也更容易定位问题。
