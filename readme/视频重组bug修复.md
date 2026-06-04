你这 3 个需求，建议一次性按“远程素材工作台体验修复”来改，不要只修单点。现在问题本质有三个：

1. **浏览器兼容/展示不一致**：远程信源下拉框用的是原生 `<select>`，不同浏览器、不同系统对 option 的样式渲染差异很大，所以你浏览器正常，别人浏览器可能出现白底、灰字、选项高度/宽度不一致。
2. **远程素材分页能力不够**：现在只有上一页/下一页这类分页，不支持直接跳到第几页，查历史素材很麻烦。
3. **原片预览逻辑不清晰**：远程素材应该优先播放远程 URL，本地素材播放本地路径；现在任务详情里容易把远程素材下载后的缓存路径 `data/remote_videos` 或 `../data/remote_videos` 当成“原片”展示，导致预览黑屏或路径看起来很奇怪。

---

# 一、浏览器展示不一致：不要再依赖原生 select

## 现状

远程信源现在看起来是一个原生下拉框。原生 `<select><option>` 的弹出层是浏览器/操作系统控制的，不完全受 CSS 控制。Chrome、Edge、不同 Windows 主题、不同缩放比例，都会导致样式不同。

你截图里对方浏览器的下拉出现：

```text
白底
灰字
选中项蓝色
禁用项很淡
整体和左侧黑色面板不一致
```

这就是原生 select 的典型兼容问题。

## 优化目标

把“信源选择”改成自定义下拉组件：

```text
button 当前信源
div dropdown
button option
```

这样所有浏览器看到的样式一致。

## 修改文件

```text
web_static/index.html
web_static/app.js
web_static/style.css
```

---

## 1. 修改 index.html

找到远程信源的 select，大概类似：

```html
<select id="remoteRecordStation"></select>
```

改成：

```html
<div class="custom-select" id="remoteStationSelect">
  <button class="custom-select-trigger" id="remoteStationTrigger" type="button">
    请选择信源
  </button>
  <div class="custom-select-menu hidden" id="remoteStationMenu"></div>
</div>

<input type="hidden" id="remoteRecordStation" value="">
```

这样 JS 仍然可以从 `remoteRecordStation` 拿当前值，不影响后续接口参数。

---

## 2. 修改 app.js：渲染自定义信源下拉

新增状态：

```js
state.remoteStationOptions = [];
state.remoteStationOpen = false;
```

新增函数：

```js
function renderRemoteStationSelect() {
  const trigger = $("remoteStationTrigger");
  const menu = $("remoteStationMenu");
  const input = $("remoteRecordStation");
  if (!trigger || !menu || !input) return;

  const options = state.remoteStationOptions || [];
  const current = input.value || options[0]?.value || "";

  const currentOption = options.find((item) => item.value === current);
  trigger.textContent = currentOption
    ? `${currentOption.label || currentOption.value}${currentOption.total != null ? `（${currentOption.total}）` : ""}`
    : "请选择信源";

  menu.innerHTML = options
    .map((item) => {
      const disabled = Number(item.total || 0) <= 0;
      const active = item.value === current;
      return `
        <button
          type="button"
          class="custom-select-option ${active ? "active" : ""} ${disabled ? "disabled" : ""}"
          data-value="${escapeAttr(item.value)}"
          ${disabled ? "disabled" : ""}
        >
          <span>${escapeHtml(item.label || item.value)}</span>
          <em>${item.total ?? ""}</em>
        </button>
      `;
    })
    .join("");

  menu.querySelectorAll(".custom-select-option").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const value = btn.dataset.value || "";
      input.value = value;
      menu.classList.add("hidden");
      state.remoteStationOpen = false;

      state.remotePage = 1;
      await loadRemoteVideos();
      renderRemoteStationSelect();
    });
  });
}
```

初始化时绑定：

```js
function bindRemoteStationSelect() {
  const trigger = $("remoteStationTrigger");
  const menu = $("remoteStationMenu");
  if (!trigger || !menu) return;

  trigger.addEventListener("click", () => {
    state.remoteStationOpen = !state.remoteStationOpen;
    menu.classList.toggle("hidden", !state.remoteStationOpen);
  });

  document.addEventListener("click", (event) => {
    if (!event.target.closest("#remoteStationSelect")) {
      state.remoteStationOpen = false;
      menu.classList.add("hidden");
    }
  });
}
```

在页面初始化时调用：

```js
bindRemoteStationSelect();
```

加载配置时，把后端返回的信源选项保存起来。`/api/config` 里已经返回了 `remote_ucms.record_station_options`，这个字段是在 `remote_ucms_public_config()` 中生成的，包括 value、label、total。

例如：

```js
async function loadConfig() {
  const config = await api("/api/config");
  state.config = config;

  state.remoteStationOptions = config.remote_ucms?.record_station_options || [];
  const input = $("remoteRecordStation");
  if (input && !input.value && state.remoteStationOptions.length) {
    input.value = state.remoteStationOptions[0].value;
  }

  renderRemoteStationSelect();
}
```

---

## 3. 修改 style.css

新增：

```css
.custom-select {
  position: relative;
  width: 100%;
}

.custom-select-trigger {
  width: 100%;
  height: 40px;
  border: 1px solid rgba(248, 113, 113, 0.45);
  border-radius: 10px;
  background: rgba(17, 24, 39, 0.95);
  color: #f9fafb;
  padding: 0 36px 0 12px;
  text-align: left;
  font-size: 14px;
  cursor: pointer;
}

.custom-select-trigger::after {
  content: "⌄";
  position: absolute;
  right: 14px;
  top: 9px;
  color: #f9fafb;
}

.custom-select-menu {
  position: absolute;
  left: 0;
  right: 0;
  top: calc(100% + 6px);
  z-index: 1000;
  max-height: 360px;
  overflow-y: auto;
  border: 1px solid rgba(148, 163, 184, 0.28);
  border-radius: 12px;
  background: #111827;
  box-shadow: 0 18px 45px rgba(0, 0, 0, 0.35);
  padding: 6px;
}

.custom-select-menu.hidden {
  display: none;
}

.custom-select-option {
  width: 100%;
  min-height: 36px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: #e5e7eb;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  text-align: left;
  cursor: pointer;
}

.custom-select-option:hover {
  background: rgba(255, 255, 255, 0.08);
}

.custom-select-option.active {
  background: rgba(185, 28, 28, 0.95);
  color: white;
}

.custom-select-option.disabled {
  opacity: 0.38;
  cursor: not-allowed;
}
```

这样不同浏览器都用你自己的 div/button 样式，不再受系统 select 样式影响。

---

# 二、远程素材增加“跳到第几页”

## 现状

后端 `/api/remote-videos` 已经支持：

```python
current: int = 1
page_size: int | None = None
keyword: str | None = None
sort_order: str = "desc"
```

也就是说后端已经能接收页码。

真正缺的是前端 UI：输入页码并跳转。

---

## 修改文件

```text
web_static/index.html
web_static/app.js
web_static/style.css
```

---

## 1. index.html 增加跳页输入框

在远程素材分页区域旁边加：

```html
<div class="remote-pager-jump">
  <span>跳至</span>
  <input id="remoteJumpPage" type="number" min="1" inputmode="numeric" />
  <span>页</span>
  <button id="remoteJumpBtn" type="button" class="ghost-btn">跳转</button>
</div>
```

---

## 2. app.js 增加状态

```js
state.remotePage = 1;
state.remotePageSize = 10;
state.remoteTotal = 0;
state.remoteTotalPages = 1;
```

---

## 3. 修改 loadRemoteVideos()

确保请求时传 current：

```js
async function loadRemoteVideos() {
  const station = $("remoteRecordStation")?.value || "";
  const keyword = $("remoteKeyword")?.value || "";
  const sortOrder = $("remoteSortOrder")?.value || "desc";

  const params = new URLSearchParams();
  if (station) params.set("record_station", station);
  params.set("current", String(state.remotePage || 1));
  params.set("page_size", String(state.remotePageSize || 10));
  if (keyword) params.set("keyword", keyword);
  params.set("sort_order", sortOrder);

  const data = await api(`/api/remote-videos?${params.toString()}`);

  state.remoteVideos = data.items || data.videos || [];
  const pagination = data.pagination || {};
  state.remoteTotal = Number(pagination.total || 0);
  state.remotePage = Number(pagination.current || state.remotePage || 1);
  state.remotePageSize = Number(pagination.pageSize || pagination.page_size || state.remotePageSize || 10);
  state.remoteTotalPages = Math.max(1, Math.ceil(state.remoteTotal / state.remotePageSize));

  renderRemoteVideos();
  updateRemotePager();
}
```

---

## 4. 新增跳页绑定

```js
function bindRemotePagerJump() {
  const input = $("remoteJumpPage");
  const btn = $("remoteJumpBtn");
  if (!input || !btn) return;

  const jump = async () => {
    let page = Number(input.value || 1);
    if (!Number.isFinite(page)) page = 1;
    page = Math.max(1, Math.min(page, state.remoteTotalPages || 1));

    state.remotePage = page;
    input.value = String(page);
    await loadRemoteVideos();
  };

  btn.addEventListener("click", jump);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") jump();
  });
}
```

初始化时调用：

```js
bindRemotePagerJump();
```

---

## 5. 修改 updateRemotePager()

```js
function updateRemotePager() {
  const info = $("remotePagerInfo");
  const input = $("remoteJumpPage");
  const prev = $("remotePrevPage");
  const next = $("remoteNextPage");

  const page = state.remotePage || 1;
  const totalPages = state.remoteTotalPages || 1;

  if (info) info.textContent = `${page} / ${totalPages} 页，共 ${state.remoteTotal || 0} 条`;
  if (input) {
    input.max = String(totalPages);
    if (!document.activeElement || document.activeElement !== input) {
      input.value = String(page);
    }
  }

  if (prev) prev.disabled = page <= 1;
  if (next) next.disabled = page >= totalPages;
}
```

---

## 6. 切换信源/搜索时重置页码

切信源、点搜索、改排序时都要：

```js
state.remotePage = 1;
await loadRemoteVideos();
```

否则用户在第 80 页切到一个只有 3 页的信源，会空列表。

---

# 三、原片预览异常修复

## 现状

现在前端播放原片时逻辑是：

```js
setVideoPreviewSource(source.url);
```

这没问题。

问题在后端 `/api/tasks/{task_id}/source-videos` 返回的 `source.url` 不准确。

当前后端逻辑是：

```python
source = manifest.get("source_video")
if not source:
    pending_previews = _source_request_preview_items(task_dir, task_id)
    if pending_previews:
        return pending_previews
if source:
    source_url = f"/api/tasks/{task_id}/file?path={source}" if (task_dir / source).exists() else ""
```

也就是说，只有 `manifest.source_video` 为空，才会去读 `source_request.json`。

但远程素材任务里，`manifest.source_video` 往往是下载后的本地缓存文件，例如：

```text
../data/remote_videos/xxx.mp4
```

而真正的“用户原始素材来源”在：

```text
outputs/<task_id>/input/source_request.json
```

里面有 remote 的：

```text
preview_url
media_low_url
media_high_url
download_url
```

代码里 `_source_request_preview_items()` 已经支持按本地/远程生成预览地址。远程素材会优先取 `preview_url / media_low_url / mediaLow / media_high_url / mediaHigh / download_url`。 

所以正确逻辑应该是：

```text
先读 source_request.json
能生成原片预览，就直接返回
只有老任务没有 source_request.json，才 fallback 到 manifest.source_video
```

---

## 修改文件

```text
web_app.py
web_static/app.js
```

---

## 1. 修改 web_app.py 的 list_source_videos()

找到：

```python
@app.get("/api/tasks/{task_id}/source-videos")
def list_source_videos(task_id: str) -> list[dict[str, Any]]:
```

把函数开头改成：

```python
@app.get("/api/tasks/{task_id}/source-videos")
def list_source_videos(task_id: str) -> list[dict[str, Any]]:
    task_dir = _task_dir(task_id)
    manifest = get_manifest(task_id)

    # 优先展示用户选择的原始素材来源。
    # 本地素材走 /api/videos/preview，远程素材走 remote preview/media URL。
    request_previews = _source_request_preview_items(task_dir, task_id)
    if request_previews:
        return request_previews

    sources: list[dict[str, Any]] = []
    source = manifest.get("source_video")
```

也就是把：

```python
if not source:
    pending_previews = _source_request_preview_items(task_dir, task_id)
    if pending_previews:
        return pending_previews
```

改成 **无论 source 是否存在，都优先读 source_request**。

---

## 2. fallback 到 manifest.source_video 时，不要展示奇怪路径

对于老任务 fallback，可以把 file 显示名处理一下：

```python
if source:
    source_path = ""
    source_url = ""
    try:
        resolved_source = _resolve_task_source(task_dir, str(source))
        source_path = relpath(resolved_source, ROOT)
        source_url = f"/api/videos/preview?path={quote(source_path, safe='/')}"
    except Exception:
        source_path = str(source)
        source_url = ""

    sources.append(
        {
            "label": "拼接原片" if manifest.get("source_mode") == "multi_source_concat_proxy" else "原片",
            "file": source_path,
            "url": source_url,
            "source_index": 0,
            "source_type": "concat" if manifest.get("source_mode") == "multi_source_concat_proxy" else "single",
        }
    )
```

注意：这里用 `/api/videos/preview?path=...` 是因为 `_resolve_input_video()` 允许访问项目目录下文件，包括 `data/remote_videos`；但更好的情况是前面已经通过 `source_request.json` 返回远程 URL，不会走到这里。`/api/videos/preview` 后端会把相对 ROOT 的路径解析为项目内文件并做安全校验。 

---

## 3. 单远程素材也要写 source_request.json

现在多源任务会在 `/api/run` 中写 `input/source_request.json`，但单源任务路径不一定写。多源分支写入逻辑在 `run_pipeline()` 里，`len(raw_source_items) > 1` 时保存 `source_items`。

为了让单远程素材任务也能稳定预览，建议所有任务都写一份 source_request。

在 `run_pipeline()` 中，解析 `raw_source_items = _collect_source_items(req)` 后，无论单源多源，都应在确定 `task_dir` 后写：

```python
def _write_source_request_for_preview(task_dir: Path, task_id: str, req: RunRequest, raw_source_items: list[dict[str, Any]]) -> None:
    if not raw_source_items:
        return

    path = task_dir / "input" / "source_request.json"
    ensure_dir(path.parent)

    existing = read_json(path, {})
    payload = {
        **existing,
        "task_id": task_id,
        "production_mode": req.production_mode,
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
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    payload.setdefault("created_at", payload["updated_at"])
    write_json(path, payload)
```

然后在单源分支确定 `task_dir` 后调用：

```python
_write_source_request_for_preview(task_dir, task_id, req, raw_source_items)
```

这样后续 `/api/tasks/{task_id}/source-videos` 一定能知道：这个原片到底是本地还是远程。

---

## 4. 前端增加视频错误提示

现在 video 播放失败可能只是黑屏，用户不知道是路径错还是编码不支持。

新增：

```js
function bindVideoPreviewErrors() {
  const video = $("videoPreview");
  if (!video) return;

  video.addEventListener("error", () => {
    const error = video.error;
    const code = error?.code || "";
    $("fileViewer").textContent =
      `原片预览失败。\n` +
      `可能原因：视频地址不可访问、远程素材链接过期、浏览器不支持该编码，或文件路径异常。\n` +
      `错误码：${code || "-"}`;
  });
}
```

初始化时调用：

```js
bindVideoPreviewErrors();
```

---

# 四、这三个需求的最终推荐实现顺序

## 第一步：修原片预览

先改 `web_app.py`：

```text
list_source_videos 优先返回 source_request_preview_items
单源任务也写 source_request.json
fallback 时用 /api/videos/preview?path=...
```

这是最影响使用的问题。

## 第二步：做远程素材跳页

后端基本不用改，因为 `/api/remote-videos` 已经支持 current/page_size。主要改前端的 input、button、state、loadRemoteVideos。

## 第三步：替换原生 select

这个是兼容性和体验优化。建议改成自定义下拉，不要继续调原生 select 的 CSS，因为原生 option 在不同浏览器里控制不了。

---

# 五、给 Codex 的完整任务说明

你可以直接把下面这段发给 Codex：

```text
请修复凤凰新闻剪辑 Web 工作台的远程素材体验问题，分三部分：

一、修复远程素材原片预览异常

问题：
任务详情里“原片”预览显示 ../data/remote_videos/xxx.mp4，并且视频黑屏。
当前 web_app.py 的 /api/tasks/{task_id}/source-videos 先读取 manifest.source_video，只在 source_video 为空时才读取 input/source_request.json。
但远程素材任务的 manifest.source_video 是后台下载缓存路径，不应该作为用户原片来源展示。
真正的原始素材来源应该来自 input/source_request.json 中的 source_items。

修改：
1. 修改 web_app.py 的 list_source_videos(task_id)：
   - 函数开始先调用 _source_request_preview_items(task_dir, task_id)
   - 如果返回非空，直接 return
   - 只有没有 source_request 预览项时，才 fallback 到 manifest.source_video
2. fallback 到 manifest.source_video 时：
   - 使用 _resolve_task_source 解析真实路径
   - 返回 /api/videos/preview?path=<相对ROOT路径>
   - 不要返回 /api/tasks/{task_id}/file?path=../data/remote_videos/...
3. 新增 _write_source_request_for_preview(task_dir, task_id, req, raw_source_items)：
   - 所有任务，只要 raw_source_items 非空，都写 input/source_request.json
   - 单源远程任务也要写，方便后续原片预览识别 remote_url
4. 保持 python run_web.py 启动方式不变。

二、远程素材增加跳转到第几页

现状：
后端 /api/remote-videos 已支持 current/page_size，但前端没有跳页输入框。

修改：
1. web_static/index.html 增加：
   - input#remoteJumpPage type=number
   - button#remoteJumpBtn
2. web_static/app.js 增加：
   - state.remotePage
   - state.remotePageSize
   - state.remoteTotal
   - state.remoteTotalPages
3. loadRemoteVideos 请求带上 current/page_size/keyword/sort_order/record_station
4. updateRemotePager 显示 当前页/总页数/总条数，并同步 jump input
5. bindRemotePagerJump：
   - 点击跳转按钮或 Enter 后跳到指定页
   - 页码 clamp 到 1-totalPages
6. 切换信源、搜索、排序时重置 remotePage=1。

三、修复信源下拉框浏览器展示不一致

问题：
原生 select/option 在不同浏览器/系统下样式不一致，有的白底灰字，有的黑底正常，影响使用。

修改：
1. 不再依赖原生 select 展示信源。
2. index.html 将 select 改为：
   - div.custom-select
   - button#remoteStationTrigger
   - div#remoteStationMenu
   - hidden input#remoteRecordStation
3. app.js 增加：
   - renderRemoteStationSelect()
   - bindRemoteStationSelect()
   - 选择信源后更新 hidden input，remotePage=1，loadRemoteVideos()
4. style.css 增加自定义下拉样式：
   - 黑色背景
   - 白色文字
   - active 红色
   - disabled 灰色
   - max-height + overflow-y
5. 不要再依赖 option 的颜色样式。

四、前端增加视频预览错误提示

修改 web_static/app.js：
1. 新增 bindVideoPreviewErrors()
2. videoPreview error 时，在 fileViewer 显示：
   “原片预览失败：视频地址不可访问、远程链接过期、浏览器不支持编码或路径异常。”
3. 页面初始化时调用 bindVideoPreviewErrors()。
```

---

# 六、验收标准

改完后验证这几项：

1. 在你的浏览器和同事浏览器打开，信源下拉样式一致，不再出现白底灰字。
2. 远程素材列表可以输入页码，点击跳转后正确加载对应页。
3. 选择单个远程素材后，原片预览列表显示远程素材名称，而不是 `../data/remote_videos/...`。
4. 远程素材原片播放使用 `preview_url/media_low_url/media_high_url/download_url`。
5. 本地素材原片播放仍使用 `/api/videos/preview?path=...`。
6. 多源任务仍能展示每个原始素材。
7. 老任务没有 `source_request.json` 时，仍能 fallback 播放 `manifest.source_video`。
8. 视频预览失败时不再黑屏无提示，而是在右侧显示可读错误。
