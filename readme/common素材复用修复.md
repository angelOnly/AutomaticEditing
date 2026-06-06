可以进入第二阶段了。优化核心不是“再加一个素材指纹”，因为现在已经有了；而是要把 **common 从“每个子任务自己抢着跑”改成“同一批素材只允许一个 common owner 跑，其他模式等待/复用”**。

## 总体优化思路

现在代码链路是：

```text
/api/run
  → 生成 common_task_id
  → 分别创建 ai_voiceover / highlight_reassembly 子任务
  → 每个子任务都启动 run_multisource_pipeline.py
  → 每个进程都进入 _ensure_common_analysis()
  → 理论上靠 file_slot_lock 防重复
```

问题是 Web 层只按 `task_id` 去重，不按 `common_task_id` 去重，所以 AI 配音和视频重组会各起一个 job。代码里 `_find_active_job_by_task_id(task_id)` 只查当前子任务，不查同一 common。

所以优化要做三层：

```text
第一层：Web 提交层按 common_task_id 去重，避免重复启动 common。
第二层：common runner 层加强锁，防止 Web 层漏掉后仍重复跑。
第三层：子任务 manifest 正确显示“等待 common / 复用 common / 执行独有步骤”。
```

---

# 1. 需要改哪些文件

主要改 4 个文件：

```text
web_app.py
run_multisource_pipeline.py
newsclip_agent/resource_locks.py
config.toml
```

可选改：

```text
newsclip_agent/job_store.py
web_static/index.html
```

如果只做后端稳定性，前 4 个就够。

---

# 2. web_app.py 怎么改

## 2.1 增加 common 级别 active job 查询

现在只有：

```python
_find_active_job_by_task_id(task_id)
```

它只认子任务。需要新增一个函数，按 `common_task_id` 查当前有没有 job 正在跑同一个 common。

建议新增：

```python
def _find_active_job_by_common_task_id(common_task_id: str) -> dict[str, Any] | None:
    if not common_task_id:
        return None

    for job_id, job in list(JOBS.items()):
        public = _refresh_job(job_id)
        if public.get("status") not in {"pending", "running"}:
            continue

        cmd = job.get("cmd") or []
        task_id = job.get("task_id") or ""
        task_dir = OUTPUTS_DIR / task_id

        source_request = read_json(task_dir / "input" / "source_request.json", {})
        if source_request.get("common_task_id") == common_task_id:
            return job

    return None
```

原因：当前 job 结构里只有 `task_id`、`cmd`、`task_fingerprint`，没有 `common_task_id` 字段。创建 job 时可以顺手加进去，这样后续查更稳。当前 job 创建位置在 `/api/run` 里。

---

## 2.2 job 里保存 common_task_id / common_source_key

在创建 job 的 dict 里增加：

```python
"common_task_id": common_task_id if should_use_source_request else "",
"common_source_key": common_source_key if should_use_source_request else "",
```

位置在这里：

```python
job = {
    "job_id": job_id,
    "task_id": task_id,
    ...
}
```

当前 job 创建逻辑见 `web_app.py`。

这样后面就不用每次读 `source_request.json`。

---

## 2.3 在 `/api/run` 里拦截同 common 的运行中任务

当前代码是先查当前 `task_id` 有没有 active job：

```python
existing_job = _find_active_job_by_task_id(task_id)
```

这个只能拦截同一个模式任务。

应该在 `should_use_source_request` 分支里，生成 `common_task_id` 后，增加 common active 检查。

逻辑建议：

```python
if should_use_source_request:
    active_common_job = _find_active_job_by_common_task_id(common_task_id)

    if active_common_job and active_common_job.get("task_id") != task_id:
        # 这里不要直接返回旧 job，因为用户点的是另一个模式，
        # 需要创建当前模式的 child manifest，让 UI 能看到这个模式任务。
        _init_multisource_child_manifest(...)
        _write_source_request_for_preview(...)

        # 方案 A：返回 waiting_common，不启动新进程
        # 由前一个 common 完成后，用户再点继续/自动续跑当前模式
```

但我更建议做“自动接续”，不让用户手动点第二次。做法是：

```text
如果同 common 正在跑：
1. 创建当前模式 child task manifest
2. 标记 status = waiting_common
3. 创建一个轻量 job，不跑 common，只等待 common ready 后跑 mode-specific pipeline
```

不过这会稍复杂。更稳的第一版可以先做：

```text
仍然启动当前子任务 job；
但传一个参数告诉 run_multisource_pipeline.py：
--wait-common-only 或默认进入等待模式；
run_multisource_pipeline.py 看到 common 正在 running，就等待，不重新跑 common。
```

也就是说 Web 层先做标记，真正等待仍放在 runner 里实现。

---

# 3. run_multisource_pipeline.py 怎么改

这是最关键的文件。

当前 `_ensure_common_analysis()` 的逻辑是：

```python
with file_slot_lock(lock_name, slots=1):
    if _common_analysis_ready(common_dir):
        reuse
        return

    start common analysis
    prepare_multi_source_manifest(...)
    pipeline_main(... --common-only)
```

这个设计本来应该能挡住重复跑，但你这次日志证明没挡住。

## 3.1 增加 common 状态文件

建议在 common 目录里新增：

```text
outputs/__common__/common_xxx/common_state.json
```

内容：

```json
{
  "common_task_id": "common_xxx",
  "status": "running",
  "owner_pid": 12345,
  "owner_task_id": "xxx_highlight_reassembly",
  "started_at": "...",
  "updated_at": "..."
}
```

状态枚举：

```text
pending
running
success
failed
```

作用：不要只靠 lock 文件判断，还要靠 common 自己的状态判断。

---

## 3.2 `_ensure_common_analysis()` 开始时先判断 common_state

新增辅助函数：

```python
def _read_common_state(common_dir: Path) -> dict[str, Any]:
    return read_json(common_dir / "common_state.json", {})


def _write_common_state(common_dir: Path, data: dict[str, Any]) -> None:
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_json(common_dir / "common_state.json", data)
```

在 `_ensure_common_analysis()` 一开始：

```python
if _common_analysis_ready(common_dir):
    _write_common_state(common_dir, {
        "common_task_id": common_dir.name,
        "status": "success",
    })
    return
```

然后进入锁。

---

## 3.3 锁内二次判断

进入锁以后必须再次判断 ready，因为可能前一个进程刚完成：

```python
with file_slot_lock(lock_name, slots=1):
    if not force_rerun_common and _common_analysis_ready(common_dir):
        _append_common_log(common_dir, "reuse common analysis outputs after lock")
        _write_common_state(common_dir, {
            "common_task_id": common_dir.name,
            "status": "success",
        })
        return
```

当前代码锁内已经有 ready 判断，但没有稳定状态文件。需要保留并增强。

---

## 3.4 common running 时等待，而不是重跑

新增函数：

```python
def _wait_for_common_ready(common_dir: Path, timeout_seconds: int = 7200, poll_seconds: float = 3.0) -> bool:
    started = time.time()

    while time.time() - started < timeout_seconds:
        if _common_analysis_ready(common_dir):
            return True

        manifest = read_json(common_dir / "manifest.json", {})
        if manifest.get("status") == "failed":
            raise RuntimeError(manifest.get("user_message") or "common analysis failed")

        time.sleep(poll_seconds)

    raise RuntimeError(f"waiting common analysis timeout: {common_dir.name}")
```

然后在发现 common 已经 running 时，第二个子任务不要自己跑 common，而是等待：

```python
state = _read_common_state(common_dir)
if state.get("status") == "running" and not force_rerun_common:
    _append_common_log(common_dir, f"wait existing common owner: {state.get('owner_task_id')}")
    _wait_for_common_ready(common_dir)
    return
```

注意：这个判断最好放在锁外和锁内都做。锁外可以减少等待锁的时间；锁内是兜底。

---

## 3.5 common 真正开跑前写 running 状态

在当前这段之前：

```python
_append_common_log(common_dir, "start common analysis")
_mark_source_prepare(common_dir, "running", request=request)
```

增加：

```python
_write_common_state(common_dir, {
    "common_task_id": common_dir.name,
    "status": "running",
    "owner_pid": os.getpid(),
    "owner_task_id": request.get("task_id") or "",
    "started_at": datetime.now().isoformat(timespec="seconds"),
})
```

需要引入 `os`。

---

## 3.6 common 成功后写 success

在：

```python
_append_common_log(common_dir, "common analysis ready")
```

后面增加：

```python
_write_common_state(common_dir, {
    "common_task_id": common_dir.name,
    "status": "success",
    "owner_pid": os.getpid(),
    "owner_task_id": request.get("task_id") or "",
    "finished_at": datetime.now().isoformat(timespec="seconds"),
})
```

当前成功路径在 `_ensure_common_analysis()` 末尾。

---

## 3.7 common 失败后写 failed

当前失败时会 `_mark_common_failed()`。

在 except 里加：

```python
_write_common_state(common_dir, {
    "common_task_id": common_dir.name,
    "status": "failed",
    "owner_pid": os.getpid(),
    "owner_task_id": request.get("task_id") or "",
    "error": str(exc),
    "finished_at": datetime.now().isoformat(timespec="seconds"),
})
```

这样第二个模式等待 common 时，如果 common 失败，可以同步失败，不会无限等。

---

# 4. resource_locks.py 怎么改

当前锁最大风险是 stale lock 清理逻辑。它用：

```python
os.kill(pid, 0)
```

来判断 pid 是否还活着。

在 Windows 下建议改成更保守：

```python
if os.name == "nt":
    # Windows 下不要轻易根据 os.kill(pid, 0) 删除锁
    # 只按超长 stale_after_seconds 清理
    is_dead = False
else:
    try:
        os.kill(pid, 0)
    except OSError:
        is_dead = True
```

也就是说：

```text
Windows 上不要因为 pid 判断失败就删锁；
只有锁文件超过 stale_after_seconds 才删。
```

否则两个 Python 子进程同时启动时，第二个可能误删第一个的 lock。

更稳一点可以加参数：

```python
def file_slot_lock(..., enable_pid_stale_check: bool = True):
```

然后 common 场景调用：

```python
file_slot_lock(lock_name, slots=1, stale_after_seconds=6 * 60 * 60, enable_pid_stale_check=False)
```

这样 common 不会被误判 stale。

---

# 5. config.toml 增加配置项

建议加：

```toml
[common_analysis]
wait_existing_common = true
wait_timeout_seconds = 7200
wait_poll_seconds = 3
disable_pid_stale_check_on_windows = true
state_file = "common_state.json"
```

默认行为：

```text
wait_existing_common = true
```

也就是同一批素材 common 正在跑时，第二个模式等待，不重复跑。

---

# 6. 兼容现有 Web 启动方式

现有启动方式：

```bash
python run_web.py
```

不用变。

因为 `run_web.py` 只是启动 FastAPI，并读取 `config.toml`。它支持 Web 并发配置，当前配置里 `max_running_jobs = 4`，所以两个模式任务本来可能并发跑。

优化后兼容逻辑是：

```text
Web 可以并发提交两个模式任务；
两个子任务可以同时存在；
但 common 只允许一个 owner 真正跑；
另一个任务等待 common ready 后，只跑自己的模式独有步骤。
```

---

# 7. 关键伪代码

## 7.1 run_multisource_pipeline.py

```python
def _ensure_common_analysis(*, common_dir, request, args, passthrough):
    ensure_dir(common_dir)

    force_rerun_common = ...
    lock_name = f"common_source_{common_dir.name}"

    if not force_rerun_common and _common_analysis_ready(common_dir):
        mark_common_state_success(common_dir)
        return

    state = _read_common_state(common_dir)
    if not force_rerun_common and state.get("status") == "running":
        _append_common_log(common_dir, "wait existing common analysis")
        _wait_for_common_ready(common_dir)
        return

    with file_slot_lock(lock_name, slots=1, enable_pid_stale_check=False):
        if not force_rerun_common and _common_analysis_ready(common_dir):
            mark_common_state_success(common_dir)
            return

        state = _read_common_state(common_dir)
        if not force_rerun_common and state.get("status") == "running":
            _wait_for_common_ready(common_dir)
            return

        mark_common_state_running(common_dir, request)

        try:
            run_real_common_pipeline()
            if not _common_analysis_ready(common_dir):
                raise RuntimeError(...)
            mark_common_state_success(common_dir)
        except Exception as exc:
            mark_common_state_failed(common_dir, exc)
            raise
```

---

## 7.2 web_app.py

```python
job = {
    ...
    "common_task_id": common_task_id if should_use_source_request else "",
    "common_source_key": common_source_key if should_use_source_request else "",
}
```

然后：

```python
def _find_active_job_by_common_task_id(common_task_id):
    for job in JOBS.values():
        public = _refresh_job(...)
        if public["status"] in {"pending", "running"}:
            if job.get("common_task_id") == common_task_id:
                return job
```

这个不是替代 runner 锁，而是提前提示/减少重复进程。

---

# 8. 开发顺序

建议按这个顺序做：

```text
1. 先改 resource_locks.py，让 Windows 不轻易误删锁。
2. 再改 run_multisource_pipeline.py，增加 common_state.json 和等待逻辑。
3. 再改 web_app.py，把 common_task_id 写入 job，并增加 common active 检查。
4. 最后改 config.toml，增加 common_analysis 配置。
5. 可选改前端 UI，显示“等待公共分析完成”。
```

不要一上来先改前端。后端先稳定。

---

# 9. 测试步骤

## 测试 1：同一批 4 个视频，连续点两个模式

操作：

```text
1. 选 4 个视频
2. 点视频重组
3. 立刻点 AI 配音解说
```

预期：

```text
outputs/__common__/common_xxx/web_jobs/common_analysis.log
```

只应该出现一次：

```text
start common analysis
run common pipeline
```

第二个任务应该出现：

```text
wait existing common analysis
```

或者：

```text
reuse common analysis outputs
```

---

## 测试 2：等第一个 common 完成后再点第二个模式

预期直接复用：

```text
Reuse common analysis outputs
```

不能新建 `source_analysis/v2`、`source_aggregate/v2`。

---

## 测试 3：不同素材组合

换一批视频，应该生成新的：

```text
common_xxx
```

不能错误复用旧 common。

---

## 测试 4：参数变化

同一批素材，但修改：

```text
aspect_ratio
chunk_seconds
frame_interval
mode
```

应该生成新的 `common_source_key`，因为这些字段现在参与 common 指纹计算。

---

## 测试 5：common 失败

故意让一个视频路径失效。

预期：

```text
common_state.json = failed
两个子任务都显示 common failed
不会无限等待
```

---

# 10. 回滚方案

最简单回滚：

```text
1. config.toml 里关闭 wait_existing_common
2. 保留原 file_slot_lock 行为
3. 删除 common_state.json 相关逻辑
```

建议配置保留开关：

```toml
[common_analysis]
wait_existing_common = true
```

如果线上出问题，可以临时改成：

```toml
wait_existing_common = false
```

恢复旧逻辑。

---

# 11. 风险点

## 风险 1：等待任务被误判卡死

如果第一个 common 进程异常退出，但 `common_state.json` 还停在 `running`，第二个任务可能一直等。

解决：

```text
等待时同时检查 manifest.status
检查 owner_pid 是否存在
增加 timeout_seconds
超时后标记 failed，而不是无限等
```

---

## 风险 2：Windows 锁清理过于保守

如果进程崩了，lock 文件可能残留。

解决：

```text
不要完全禁用 stale 清理；
只是不使用不可靠 pid 判断；
超过 6 或 12 小时仍可清理。
```

---

## 风险 3：rerun common 时被等待逻辑挡住

如果用户手动 rerun `source_analysis` / `source_aggregate`，应该允许强制重跑。

当前代码已经有：

```python
force_rerun_common = bool(_is_common_rerun_step(rerun) or _is_common_rerun_step(rerun_from))
```

这个要保留。

---

## 最终判断

你之前设计的方向没错：

```text
素材指纹：已经有
common_task_id：已经有
common目录：已经有
锁：也已经有
```

真正要补的是：

```text
1. Web 层 common 级别去重
2. common runner 层 running 状态等待
3. Windows 下锁 stale 判断加固
```

这样优化后，你这个操作：

```text
选 4 个视频 → 点视频重组 → 再点 AI 配音解说
```

应该变成：

```text
common 只跑一次
两个模式共用 common 结果
各自只跑后半段专属流程
```
