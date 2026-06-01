# AutomaticEditing 多任务并发优化方案

## 1. 结论先说

当前服务“一次只能跑一个任务”，主要不是因为 `run_pipeline.py` 的 pipeline 天然不能并发，而是 Web 工作台前端主动做了单任务锁：`web_static/app.js` 中 `state.activeJob` 只允许保存一个正在运行的 job，`runSelected()` 和 `rerun()` 一旦发现 `state.activeJob` 存在，就直接弹窗阻止新的运行。因此用户侧表现就是“只能等当前任务结束”。

后端 `web_app.py` 的 `/api/run` 实际是每次请求都 `subprocess.Popen(...)` 启动一个新的 `run_pipeline.py` 子进程。也就是说，后端已经有“多进程启动能力”，但没有并发上限、没有 GPU 显存保护、没有同一 task_id 的互斥锁、没有排队机制。如果只是把前端锁删掉，短期看似能并发，长期很容易出现显存爆、同一个任务目录互相覆盖、日志和 manifest 竞争写入等问题。

推荐方案：

- 第一阶段：做“受控并发队列”，支持多个任务同时跑，但限制最大运行数。
- 第二阶段：把 ASR / TTS 的 GPU 资源加锁或分槽，避免 3090 被多个模型同时抢爆。
- 第三阶段：优化 TTS 内部并行和模型常驻，减少每个任务重复加载模型的开销。

对于 RTX 3090 24GB，建议初始配置：

- `MAX_RUNNING_JOBS = 2`，最稳。
- 如果只跑短视频、TTS 文案较短、`nvidia-smi` 显存长期低于 18GB，可以升到 `3`。
- 不建议一开始开到 `4+`，因为当前架构是“每个 job 一个 Python 子进程”，ASR / TTS 模型不会跨进程共享，会重复占显存。
- 豆包大模型调用可以几十并发，但它不是瓶颈；本地瓶颈主要是 GPU 显存、TTS 生成、ASR、ffmpeg CPU/磁盘 IO。

---

## 2. 当前代码现状分析

## 2.1 Web 后端：每次运行都会启动一个独立子进程

位置：`web_app.py`

当前 `/api/run` 的核心逻辑是：

```python
process = subprocess.Popen(
    cmd,
    cwd=str(ROOT),
    stdout=log_file,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="replace",
    env=env,
    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
)
```

这说明每个任务都是独立 Python 进程。优点是隔离性好，某个任务崩了不影响 Web 服务；缺点是每个任务都会单独加载模型，显存不能共享。

当前 `JOBS` 是内存字典：

```python
JOBS: dict[str, dict[str, Any]] = {}
```

它只记录运行状态，没有队列状态，也没有并发限制。服务重启后，内存里的 `JOBS` 会丢失，只能从任务目录里的 `last_web_job.json` 找到最后一次记录，但无法恢复真实进程状态。

## 2.2 前端：主动禁止同时启动多个任务

位置：`web_static/app.js`

当前 `runSelected()` 有单任务锁：

```javascript
async function runSelected() {
  if (state.activeJob) {
    alert("当前已有任务在运行，请等待完成后再启动新的运行。");
    return;
  }
  ...
}
```

`rerun()` 也有同样限制：

```javascript
async function rerun(fromStep) {
  if (state.activeJob) {
    alert("当前已有任务在运行，请等待完成后再重跑。");
    return;
  }
  ...
}
```

并且 `afterJobStarted(job)` 会把当前 job 写成全局唯一活动任务：

```javascript
state.activeJob = job.job_id;
```

这导致页面只能追踪一个 job。即使后端可以开多个进程，前端也不允许用户继续点。

## 2.3 Pipeline 内部：单个任务步骤是串行执行

位置：`newsclip_agent/pipeline.py`

当前 `PipelineRunner.run()` 是顺序执行步骤：

```python
for step in selected:
    handler = getattr(self, f"step_{step}")
    handler()
```

这没问题，因为一个任务内部有依赖关系：ASR 要等音频提取，timeline 要等 ASR 和 vision，TTS 要等配音文案。这里不建议把所有 step 乱并发。

但有些子步骤已经可以并发，比如 vision chunk：

```python
with ThreadPoolExecutor(max_workers=max_workers) as executor:
    futures = [executor.submit(process_one_chunk, chunk) for chunk in chunks_to_process]
```

这说明项目已经有“步骤内部并发”的思路，只是多任务层面还没有做好。

## 2.4 ASR：每个 PipelineRunner 内部懒加载一个 FunASR 引擎

位置：`newsclip_agent/pipeline.py`

```python
def _get_asr_engine(self):
    if self._asr_engine is not None:
        return self._asr_engine
    ...
    self._asr_engine = FunASREngine(...)
    return self._asr_engine
```

这表示一个子进程里只加载一次 ASR。但如果同时启动 3 个 job，就会有 3 个 Python 子进程，每个子进程各自加载一份 ASR 模型。

## 2.5 TTS：OmniVoice 有进程内模型缓存，但不能跨进程共享

位置：`newsclip_agent/tts_omnivoice.py`

```python
_MODEL_CACHE: dict[tuple[str, str], Any] = {}
```

以及：

```python
model = _MODEL_CACHE.get(cache_key)
if model is None:
    model = OmniVoice.from_pretrained(str(model_path), device_map=device, dtype=dtype)
    if keep_model_loaded:
        _MODEL_CACHE[cache_key] = model
```

这表示 TTS 模型在单个 Python 进程内可以复用；但 Web 当前是多子进程架构，所以不同任务之间无法共享 `_MODEL_CACHE`。并发任务越多，重复加载越多。

## 2.6 TTS 当前是一个任务内逐条生成

位置：`newsclip_agent/pipeline.py` 的 `step_tts()`

当前逻辑是：

```python
for item in scripts:
    ...
    generate_omnivoice_audio(...)
```

如果一个任务拆出多个短视频，或者一个短视频里有多个 segment，TTS 是串行跑的。这里可以优化，但要注意：同一个模型对象并发调用 `model.generate()` 未必线程安全，所以不能简单无脑开很多线程共用一个模型。

---

## 3. 为什么不能直接开几十并发

你的判断里“豆包模型几十并发没问题”是对的，但当前整体任务并发不是由豆包决定的，主要由本地资源决定。

当前一个完整任务大致会占用：

1. ASR：FunASR / SenseVoiceSmall / VAD / Punc，主要占 GPU 显存和 CPU 内存。
2. TTS：OmniVoice，通常比 ASR 更吃显存，也更容易成为瓶颈。
3. ffmpeg：抽音频、抽帧、渲染，占 CPU、磁盘 IO、部分显卡编码资源。
4. LLM：豆包远程调用，占网络和 API 并发，一般不是本机瓶颈。
5. 多进程重复加载：当前架构下，2 个任务就可能加载 2 份 ASR + 2 份 TTS。

所以推荐按“本地模型并发”来定上限，而不是按豆包并发来定。

---

## 4. 推荐并发策略

## 4.1 默认并发上限

建议配置：

```toml
[web_concurrency]
max_running_jobs = 2
max_pending_jobs = 20
same_task_policy = "reject"   # reject / queue / cancel_previous
poll_interval_seconds = 2

[gpu_limits]
asr_slots = 1
tts_slots = 1
render_slots = 1
```

解释：

- `max_running_jobs = 2`：同一时间最多 2 个完整 pipeline 子进程。
- `asr_slots = 1`：同一时间只允许 1 个任务跑 ASR。
- `tts_slots = 1`：同一时间只允许 1 个任务跑 TTS。
- `render_slots = 1`：同一时间只允许 1 个任务做最终渲染。

这看起来保守，但很稳。因为一个任务不可能全程都在 ASR/TTS，它有大量 LLM 和文件处理时间。两个任务交错运行时，吞吐会提升，但不会让 GPU 同时被多个 TTS 直接打爆。

## 4.2 激进一点的配置

如果实测显存足够，可以改成：

```toml
[web_concurrency]
max_running_jobs = 3
max_pending_jobs = 30
same_task_policy = "reject"

[gpu_limits]
asr_slots = 2
tts_slots = 1
render_slots = 1
```

也就是允许 3 个任务同时推进，但 TTS 仍然单槽。原因是 TTS 更容易显存暴涨，ASR 可以先尝试 2 槽。

## 4.3 不建议的配置

不建议：

```toml
max_running_jobs = 6
tts_slots = 3
```

除非你已经改成模型服务化、模型单例常驻、请求排队生成。否则每个子进程都加载 OmniVoice，3090 很容易 OOM。

---

## 5. 第一阶段：后端增加受控并发队列

目标：支持多个任务提交；如果运行槽满了，任务进入 pending 队列；有任务完成后自动启动下一个。

## 5.1 修改 `config.toml`

新增：

```toml
[web_concurrency]
max_running_jobs = 2
max_pending_jobs = 20
same_task_policy = "reject"
```

## 5.2 修改 `web_app.py`：增加队列状态

在 imports 增加：

```python
from collections import deque
from threading import Lock
```

在全局变量区域增加：

```python
JOB_LOCK = Lock()
PENDING_JOB_IDS: deque[str] = deque()

WEB_CONCURRENCY = PROJECT_CONFIG.raw.get("web_concurrency", {})
MAX_RUNNING_JOBS = int(WEB_CONCURRENCY.get("max_running_jobs", 2))
MAX_PENDING_JOBS = int(WEB_CONCURRENCY.get("max_pending_jobs", 20))
SAME_TASK_POLICY = str(WEB_CONCURRENCY.get("same_task_policy", "reject"))
```

注意：这里需要确认 `ProjectConfig` 是否暴露 `raw`。当前代码里已经多处使用 `self.config.raw`，所以这个字段大概率存在。

## 5.3 把 `/api/run` 拆成“创建 job”和“启动 job”

当前 `/api/run` 是创建命令后立即 `Popen`。建议拆成：

```python
def _build_run_command(req: RunRequest, task_id: str) -> list[str]:
    cmd = [
        sys.executable,
        "-u",
        str(RUNNER),
        "--task-id",
        task_id,
        ...
    ]
    return cmd
```

然后新增：

```python
def _running_jobs_count() -> int:
    count = 0
    for job_id, job in list(JOBS.items()):
        public = _refresh_job(job_id)
        if public.get("status") == "running":
            count += 1
    return count
```

新增同一任务互斥检查：

```python
def _has_active_job_for_task(task_id: str) -> bool:
    for job_id, job in list(JOBS.items()):
        status = _refresh_job(job_id).get("status")
        if job.get("task_id") == task_id and status in {"pending", "running"}:
            return True
    return False
```

新增启动函数：

```python
def _start_job(job: dict[str, Any]) -> None:
    log_path = Path(job["log_path"])
    log_file = log_path.open("a", encoding="utf-8", errors="replace")
    log_file.write("\n=== job started ===\n")
    log_file.flush()

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"

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
    )

    job["pid"] = process.pid
    job["process"] = process
    job["log_file"] = log_file
    job["status"] = "running"
    job["started_at"] = datetime.now().isoformat(timespec="seconds")
    job["returncode"] = None
    _persist_job(job)
```

新增持久化函数：

```python
def _persist_job(job: dict[str, Any]) -> None:
    public = {k: v for k, v in job.items() if k not in {"process", "log_file"}}
    write_json(OUTPUTS_DIR / job["task_id"] / "last_web_job.json", public)
```

新增调度函数：

```python
def _schedule_jobs() -> None:
    with JOB_LOCK:
        running = _running_jobs_count()
        while running < MAX_RUNNING_JOBS and PENDING_JOB_IDS:
            next_job_id = PENDING_JOB_IDS.popleft()
            job = JOBS.get(next_job_id)
            if not job or job.get("status") != "pending":
                continue
            _start_job(job)
            running += 1
```

修改 `_refresh_job()`：任务结束后触发调度。

```python
def _refresh_job(job_id: str) -> dict[str, Any]:
    job = JOBS[job_id]
    process = job.get("process")
    changed_to_finished = False

    if process and job["status"] == "running":
        code = process.poll()
        if code is not None:
            job["returncode"] = code
            job["status"] = "success" if code == 0 else "failed"
            job["finished_at"] = datetime.now().isoformat(timespec="seconds")
            log_file = job.get("log_file")
            if log_file:
                log_file.close()
            job["user_message"] = _extract_user_message_from_log(Path(job["log_path"]))
            _persist_job(job)
            changed_to_finished = True

    elif Path(job.get("log_path", "")).exists():
        job["user_message"] = _extract_user_message_from_log(Path(job["log_path"]))

    if changed_to_finished:
        _schedule_jobs()

    return _public_job(job)
```

## 5.4 修改 `/api/run`：满了就 pending

把原来立即 `Popen` 的部分替换为：

```python
@app.post("/api/run")
def run_pipeline(req: RunRequest) -> dict[str, Any]:
    ...
    task_id = _resolve_run_task_id(req)

    with JOB_LOCK:
        if SAME_TASK_POLICY == "reject" and _has_active_job_for_task(task_id):
            raise HTTPException(409, f"任务 {task_id} 已有运行中或排队中的 job，请不要对同一任务并发运行")

        pending_count = sum(1 for j in JOBS.values() if j.get("status") == "pending")
        if pending_count >= MAX_PENDING_JOBS:
            raise HTTPException(429, "等待队列已满，请稍后再提交")

        task_dir = ensure_dir(OUTPUTS_DIR / task_id)
        _seed_reusable_outputs(req, task_id, task_dir)
        job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        log_dir = ensure_dir(task_dir / "web_jobs")
        log_path = log_dir / f"{job_id}.log"
        cmd = _build_run_command(req, task_id)

        _save_web_run_options(task_dir, req)

        log_path.write_text(" ".join(cmd) + "\n\n=== job queued ===\n", encoding="utf-8", errors="replace")

        job = {
            "job_id": job_id,
            "task_id": task_id,
            "pid": None,
            "cmd": cmd,
            "log": relpath(log_path, ROOT),
            "log_path": str(log_path),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "started_at": "",
            "status": "pending",
            "returncode": None,
        }
        JOBS[job_id] = job
        PENDING_JOB_IDS.append(job_id)
        _persist_job(job)

    _schedule_jobs()
    return _public_job(JOBS[job_id])
```

注意：这里 `task_dir` 需要在 `_seed_reusable_outputs` 前创建，和原逻辑保持一致。

## 5.5 修改 `/api/jobs`：返回 pending / running / success / failed

当前可以继续用：

```python
@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    return [_refresh_job(job_id) for job_id in sorted(JOBS, reverse=True)]
```

但建议排序改成：

```python
return sorted(
    [_refresh_job(job_id) for job_id in JOBS],
    key=lambda x: x.get("created_at") or x.get("started_at") or "",
    reverse=True,
)
```

这样 pending job 也会显示。

---

## 6. 第二阶段：加入同一任务互斥，避免 manifest 写坏

这是非常重要的。不能允许两个 job 同时写同一个 `outputs/<task_id>/manifest.json`。

风险场景：

- 用户对同一个任务同时点“重跑 tts”和“从 vision 后重跑”。
- 两个进程同时写 `manifest.json`，后写的覆盖先写的。
- `current_versions` 被互相覆盖，导致步骤版本错乱。
- `tts/omnivoice/v2`、`edit/drafts/v3` 目录版本号冲突。

建议策略：

```toml
same_task_policy = "reject"
```

也就是同一个 `task_id` 只能有一个 pending/running job。不同 `task_id` 可以并发。

如果以后想做高级一点，可以支持：

- `reject`：直接拒绝。
- `queue`：同一任务排队，但不能同时跑。
- `cancel_previous`：新任务提交时取消旧任务。

第一版只做 `reject` 就够。

---

## 7. 第三阶段：前端支持多个运行任务

## 7.1 删除单任务锁

位置：`web_static/app.js`

把 `runSelected()` 里的：

```javascript
if (state.activeJob) {
  alert("当前已有任务在运行，请等待完成后再启动新的运行。");
  return;
}
```

改成：

```javascript
// 允许多个任务并发。是否能启动由后端队列和并发限制决定。
```

把 `rerun()` 里的同类判断也删除。

但是要注意：对于同一个任务的重跑，后端会返回 409，所以前端需要展示后端错误。

## 7.2 `state.activeJob` 改为“当前查看的 job”，不是全局锁

当前：

```javascript
state.activeJob = job.job_id;
```

可以保留，但语义改成“当前页面日志面板正在看的 job”。不要再用它阻止新任务。

建议新增：

```javascript
state.runningJobs = new Map();
```

或者简单一点，直接每 10 秒调用 `/api/jobs`，从后端拿所有 running/pending job。

## 7.3 按钮不要全局 disabled

当前：

```javascript
function setRunControlsBusy(isBusy) {
  ["runSelected", "rerunOne", "rerunFrom"].forEach((id) => {
    const button = $(id);
    if (button) button.disabled = isBusy;
  });
}
```

建议改成：

```javascript
function setRunControlsBusy(isBusy) {
  // 多任务并发后，不再因为某个 job 运行就禁用全局运行按钮。
  // 如果当前选中的 task 正在运行，可以只禁用 rerunOne/rerunFrom。
}
```

更实用的写法：

```javascript
async function selectedTaskHasActiveJob(taskId) {
  if (!taskId) return false;
  const jobs = await api("/api/jobs").catch(() => []);
  return jobs.some((job) => job.task_id === taskId && ["pending", "running"].includes(job.status));
}
```

在重跑当前任务时判断：

```javascript
if (await selectedTaskHasActiveJob(state.selectedTask)) {
  alert("当前任务已有运行中或排队中的 job，不能对同一个任务并发重跑。");
  return;
}
```

但对于选择新视频启动新任务，不要拦。

## 7.4 任务列表显示运行状态

后端 `/api/tasks` 当前只读 `manifest.json`，不显示 job 状态。建议在 `list_tasks()` 里补充：

```python
def _active_job_status_for_task(task_id: str) -> str:
    statuses = []
    for job_id, job in list(JOBS.items()):
        public = _refresh_job(job_id)
        if public.get("task_id") == task_id and public.get("status") in {"pending", "running"}:
            statuses.append(public.get("status"))
    if "running" in statuses:
        return "running"
    if "pending" in statuses:
        return "pending"
    return ""
```

然后在 task item 中增加：

```python
"job_status": _active_job_status_for_task(task_dir.name),
```

前端 `loadTasks()` 中展示：

```javascript
const jobBadge = task.job_status ? ` · ${task.job_status === "running" ? "运行中" : "排队中"}` : "";
item.innerHTML = `<strong>${escapeHtml(displayTaskId(task.task_id))}</strong><span>${task.done_steps}/${task.step_count} 完成 · ${task.failed_steps} 失败${jobBadge} · ${task.updated_at || "-"}</span>`;
```

---

## 8. 第四阶段：GPU 资源槽控制

只做 Web 并发队列还不够，因为两个 pipeline 可能同时进入 TTS。当前 TTS 可能是最吃显存的环节。

有两种实现方式。

## 8.1 简单方案：任务级并发限制

只设置：

```toml
max_running_jobs = 2
```

优点：实现简单。

缺点：两个任务仍可能同时跑 TTS。

这个方案可以先用，但建议配合监控。

## 8.2 稳定方案：步骤级 GPU 文件锁

新增文件：`newsclip_agent/resource_locks.py`

```python
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_DIR = Path("outputs/.locks")

@contextmanager
def file_slot_lock(name: str, slots: int = 1, poll_seconds: float = 1.0):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    acquired = None
    try:
        while acquired is None:
            for i in range(max(1, int(slots))):
                path = LOCK_DIR / f"{name}_{i}.lock"
                try:
                    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, str(os.getpid()).encode("utf-8"))
                    os.close(fd)
                    acquired = path
                    break
                except FileExistsError:
                    continue
            if acquired is None:
                time.sleep(poll_seconds)
        yield
    finally:
        if acquired and acquired.exists():
            try:
                acquired.unlink()
            except FileNotFoundError:
                pass
```

然后在 `pipeline.py` 引入：

```python
from .resource_locks import file_slot_lock
```

在 `step_asr()` 中包起来：

```python
def step_asr(self) -> None:
    ...
    asr_slots = int(self.config.raw.get("gpu_limits", {}).get("asr_slots", 1))
    with file_slot_lock("asr", slots=asr_slots):
        engine = self._get_asr_engine()
        result = engine.transcribe(str(audio))
    ...
```

在 `step_tts()` 生成音频部分包起来：

```python
tts_slots = int(self.config.raw.get("gpu_limits", {}).get("tts_slots", 1))
with file_slot_lock("tts", slots=tts_slots):
    result = generate_omnivoice_audio(...)
```

注意：如果 `step_tts()` 内有多个 segment，不要每个 segment 都排队很久；可以把整个 `step_tts()` 包在一个 TTS 锁里，避免一个任务的多个 segment 被其他任务插队，导致单任务耗时过长。

推荐第一版写法：

```python
def step_tts(self) -> None:
    tts_slots = int(self.config.raw.get("gpu_limits", {}).get("tts_slots", 1))
    with file_slot_lock("tts", slots=tts_slots):
        self._step_tts_impl()
```

然后把当前 `step_tts()` 的原始内容移动到 `_step_tts_impl()`。

---

## 9. 第五阶段：TTS 内部并发优化

这个阶段不是必须先做。因为当前多任务并发后，整体吞吐已经会提升。TTS 内部并发要谨慎。

## 9.1 不建议直接多线程共用一个 OmniVoice 模型

不要直接这样写：

```python
with ThreadPoolExecutor(max_workers=4) as executor:
    executor.submit(generate_omnivoice_audio, ...)
```

原因：

- `generate_omnivoice_audio()` 内部会访问 `_MODEL_CACHE`。
- 多线程同时首次加载模型可能重复加载。
- 同一个 `model.generate()` 是否线程安全不确定。
- 多个 generate 同时跑，显存峰值可能比单任务多很多。

## 9.2 推荐做法：TTS 服务化

长期最优方案：把 OmniVoice 从 pipeline 子进程里拆出来，变成一个本地 TTS 服务。

架构：

```text
Web 服务
  ├─ job queue
  ├─ pipeline 子进程 1
  ├─ pipeline 子进程 2
  └─ 本地 OmniVoice TTS Server
       ├─ 启动时加载一次模型
       ├─ 请求队列
       ├─ max_concurrent_generate = 1 或 2
       └─ 返回 wav 文件路径 / 二进制音频
```

优点：

- OmniVoice 模型只加载一份。
- 多任务共享同一个 TTS 服务。
- 可以独立控制 TTS 并发。
- 减少显存重复占用。

第一版可以新增 `tts_server.py`：

```python
from fastapi import FastAPI
from pydantic import BaseModel
from threading import Lock
from pathlib import Path

from newsclip_agent.tts_omnivoice import generate_omnivoice_audio

app = FastAPI()
GENERATE_LOCK = Lock()

class TTSRequest(BaseModel):
    text: str
    output_path: str
    model_path: str
    reference_audio: str
    reference_text: str | None = None
    speed: float | None = None

@app.post("/generate")
def generate(req: TTSRequest):
    with GENERATE_LOCK:
        result = generate_omnivoice_audio(
            text=req.text,
            output_path=Path(req.output_path),
            model_path=req.model_path,
            reference_audio=req.reference_audio,
            reference_text=req.reference_text,
            keep_model_loaded=True,
            speed=req.speed,
        )
    return result.to_dict()
```

然后 `pipeline.py` 中不要直接 `generate_omnivoice_audio()`，而是调用本地服务：

```python
import requests

resp = requests.post("http://127.0.0.1:7871/generate", json={...}, timeout=600)
resp.raise_for_status()
seg_result = TTSResult(**resp.json())
```

如果你想保持离线无服务，也可以暂时不做这一步。

---

## 10. 第六阶段：ASR 服务化，可选

ASR 也可以服务化，但优先级低于 TTS。

原因：

- ASR 通常只在任务前半段跑一次。
- TTS 可能对每个短视频、每个 segment 跑多次。
- TTS 对最终耗时和显存的影响更明显。

如果要做 ASR 服务化，类似：

```text
FunASR Server
  ├─ 启动加载 SenseVoiceSmall
  ├─ /transcribe 接收 audio_path
  ├─ 内部 Lock 或 Semaphore 控制并发
  └─ 返回 text + segments
```

---

## 11. 推荐落地顺序

## 11.1 第一优先级：Web 受控并发队列

改动文件：

- `config.toml`
- `web_app.py`
- `web_static/app.js`

目标：

- 可以连续提交多个不同视频任务。
- 最多同时跑 2 个。
- 超过 2 个自动排队。
- 同一个 `task_id` 不能并发重跑。

这是最应该先做的，因为改动小、收益大。

## 11.2 第二优先级：GPU 步骤锁

改动文件：

- 新增 `newsclip_agent/resource_locks.py`
- 修改 `newsclip_agent/pipeline.py`
- 修改 `config.toml`

目标：

- 多任务可以并发推进。
- ASR / TTS / render 不会同时挤爆 GPU。
- 出现多个任务时，任务会在 GPU 重步骤前等待。

## 11.3 第三优先级：TTS 服务化

改动文件：

- 新增 `tts_server.py`
- 修改 `run_web.py` 或新增启动脚本
- 修改 `newsclip_agent/pipeline.py`
- 修改 `config.toml`

目标：

- OmniVoice 模型只加载一次。
- 多任务共享 TTS 模型。
- 可以把 `max_running_jobs` 从 2 更安全地提升到 3。

---

## 12. 具体代码修改清单

## 12.1 `config.toml`

新增：

```toml
[web_concurrency]
max_running_jobs = 2
max_pending_jobs = 20
same_task_policy = "reject"

[gpu_limits]
asr_slots = 1
tts_slots = 1
render_slots = 1
```

## 12.2 `web_app.py`

需要改：

1. 增加 `deque`、`Lock`。
2. 增加 `PENDING_JOB_IDS`、`JOB_LOCK`。
3. 增加 `MAX_RUNNING_JOBS`、`MAX_PENDING_JOBS`、`SAME_TASK_POLICY`。
4. 把 `/api/run` 从立即启动改成创建 pending job。
5. 增加 `_schedule_jobs()`。
6. 增加 `_start_job(job)`。
7. 增加 `_persist_job(job)`。
8. 修改 `_refresh_job()`，任务结束后自动启动下一个 pending job。
9. 修改 `/api/tasks`，给任务补 `job_status`。

## 12.3 `web_static/app.js`

需要改：

1. 删除 `runSelected()` 的 `state.activeJob` 全局阻止。
2. 删除 `rerun()` 的 `state.activeJob` 全局阻止，改成只判断当前 `task_id` 是否已有 active job。
3. `setRunControlsBusy()` 不再全局禁用按钮。
4. 任务列表展示 pending/running 状态。
5. 日志面板可以继续追踪最近启动的 job，但不再代表全局唯一 job。

## 12.4 `newsclip_agent/pipeline.py`

建议改：

1. `step_asr()` 增加 ASR slot lock。
2. `step_tts()` 增加 TTS slot lock。
3. `step_render()` 和 `step_reassembly_render()` 增加 render slot lock。
4. `parse_args()` 可以新增 `--release-asr-after-task` 已经存在，不需要新增。

## 12.5 新增 `newsclip_agent/resource_locks.py`

用于跨进程文件锁。因为当前每个 job 是独立子进程，普通 Python `threading.Lock` 不能跨进程生效，必须用文件锁、端口锁、数据库锁或系统锁。

---

## 13. 并发测试方案

## 13.1 基础功能测试

准备 3 个视频：

```text
videos/a.mp4
videos/b.mp4
videos/c.mp4
```

设置：

```toml
max_running_jobs = 2
max_pending_jobs = 20
```

连续提交 3 个任务。

预期：

- 任务 A：running
- 任务 B：running
- 任务 C：pending
- A 或 B 完成后，C 自动变 running。

## 13.2 同一任务互斥测试

对同一个 task 连续点两次运行或重跑。

预期：

- 第二次请求返回 409。
- 前端提示：该任务已有运行中或排队中的 job。
- `manifest.json` 不被两个进程同时写。

## 13.3 显存测试

Windows / WSL / Linux 下观察：

```bash
nvidia-smi -l 1
```

重点看：

- 显存使用是否超过 22GB。
- 是否出现 CUDA OOM。
- 同时 TTS 时是否显存峰值过高。
- GPU 利用率是否长期 0，若长期 0 说明瓶颈在 LLM/API/CPU/IO，不需要盲目提高 GPU 并发。

## 13.4 压测建议

阶段 1：

```toml
max_running_jobs = 2
asr_slots = 1
tts_slots = 1
```

跑 5 个视频，看是否稳定。

阶段 2：

```toml
max_running_jobs = 3
asr_slots = 2
tts_slots = 1
```

跑 5 个视频，看显存峰值。

阶段 3：

只有当阶段 2 稳定，才考虑：

```toml
max_running_jobs = 3
tts_slots = 2
```

如果 TTS 一开 2 就 OOM，就退回 `tts_slots = 1`。

---

## 14. 最终推荐版本

第一版建议做到这个程度：

```toml
[web_concurrency]
max_running_jobs = 2
max_pending_jobs = 20
same_task_policy = "reject"

[gpu_limits]
asr_slots = 1
tts_slots = 1
render_slots = 1
```

这版的体验会明显变好：

- 你可以一次提交多个视频。
- 系统自动排队。
- 最多同时处理 2 个任务。
- 同一任务不会被重复重跑写坏。
- 3090 不容易因为 TTS 并发直接爆显存。

等这个稳定后，再考虑把 TTS 服务化。TTS 服务化完成后，`max_running_jobs` 才更适合提高到 3，甚至 4。当前多子进程架构下，不建议盲目开高并发。

---

## 15. 一句话总结

当前真正的问题不是“模型不能并发”，而是“Web 前端人为锁死单任务 + 后端缺少并发队列和 GPU 资源保护”。正确优化方向不是简单删除前端限制，而是做一个受控并发调度器：不同任务可以并发，同一任务互斥，GPU 重步骤限流，先从 2 并发稳定跑起，再根据 3090 显存实测逐步提高。
