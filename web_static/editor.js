const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(window.location.search);
const taskId = params.get("task_id") || "";

const state = {
  project: null,
  selectedAssetId: "",
  selectedClipId: "",
  selectedEffectId: "",
  playhead: 0,
  pixelsPerSecond: 16,
  history: [],
  future: [],
  dirty: false,
  selecting: false,
  selectionAnchor: 0,
  previewMode: "timeline",
  activePreviewClipId: "",
  activeVideoUrl: "",
  switchGuard: false,
  toastTimer: null,
  coverConfig: null,
  coverPromptDoc: null,
  coverCandidates: [],
  selectedCover: null,
  coverImage: null,
  fonts: [],
  textLayers: [],
  selectedTextLayerId: "",
  remoteConfig: null,
  remoteEnabled: false,
  remoteVideos: [],
  remotePage: 1,
  remotePageSize: 10,
  remoteTotal: 0,
  remoteTotalPages: 1,
  remoteLoaded: false,
  remoteRequestSeq: 0,
  remoteAddingKey: "",
  remoteSearchTimer: null,
};

async function api(path, options = {}) {
  const headers = options.body instanceof FormData ? {} : { "Content-Type": "application/json" };
  const response = await fetch(path, { ...options, headers: { ...headers, ...(options.headers || {}) } });
  if (!response.ok) {
    let message = await response.text();
    try {
      const parsed = JSON.parse(message);
      message = parsed.detail || parsed.message || message;
    } catch (_) {}
    throw new Error(message || response.statusText);
  }
  const type = response.headers.get("content-type") || "";
  const text = await response.text();
  if (type.includes("application/json")) return JSON.parse(text);
  const trimmed = text.trim();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try { return JSON.parse(trimmed); } catch (_) {}
  }
  return text;
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function uid(prefix) {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function num(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatTime(seconds) {
  const value = Math.max(0, num(seconds));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = Math.floor(value % 60);
  const millis = Math.floor((value - Math.floor(value)) * 1000 + 0.5);
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = String(message || "");
  toast.classList.remove("hidden", "error");
  if (isError) toast.classList.add("error");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => toast.classList.add("hidden"), isError ? 7000 : 3500);
}

function assetById(assetId) {
  return (state.project?.assets || []).find((asset) => asset.asset_id === assetId);
}

function clipById(clipId) {
  return (state.project?.clips || []).find((clip) => clip.clip_id === clipId);
}

function projectDuration() {
  const clips = state.project?.clips || [];
  return clips.length ? num(clips[clips.length - 1].timeline_end) : 0;
}

function normalizeProject(project) {
  const normalized = clone(project || {});
  normalized.schema_version = normalized.schema_version || 1;
  normalized.revision = num(normalized.revision, 0);
  normalized.settings = normalized.settings || { width: 1920, height: 1080, fps: 25 };
  normalized.assets = Array.isArray(normalized.assets) ? normalized.assets : [];
  normalized.clips = Array.isArray(normalized.clips) ? normalized.clips : [];
  normalized.effects = Array.isArray(normalized.effects) ? normalized.effects : [];
  normalized.assets.forEach((asset, index) => {
    asset.asset_id = asset.asset_id || asset.source_id || `asset_${index + 1}`;
    asset.label = asset.label || asset.display_name || asset.name || asset.asset_id;
    asset.duration_seconds = num(asset.duration_seconds || asset.original_duration_seconds);
    asset.preview_url = asset.preview_url || asset.url || "";
  });
  normalized.clips.forEach((clip, index) => {
    clip.clip_id = clip.clip_id || `clip_${index + 1}`;
    clip.asset_id = clip.asset_id || clip.source_id || normalized.assets[0]?.asset_id || "";
    clip.source_in = num(clip.source_in ?? clip.local_start_seconds ?? clip.source_start_seconds);
    clip.source_out = num(clip.source_out ?? clip.local_end_seconds ?? clip.source_end_seconds);
    if (clip.source_out <= clip.source_in) clip.source_out = clip.source_in + num(clip.duration_seconds, 0.04);
  });
  recomputeTimeline(normalized);
  return normalized;
}

function recomputeTimeline(project = state.project) {
  if (!project) return;
  let cursor = 0;
  project.clips = (project.clips || []).filter((clip) => num(clip.source_out) - num(clip.source_in) >= 0.04);
  project.clips.forEach((clip) => {
    clip.source_in = Math.max(0, num(clip.source_in));
    clip.source_out = Math.max(clip.source_in + 0.04, num(clip.source_out));
    clip.duration_seconds = clip.source_out - clip.source_in;
    clip.timeline_start = cursor;
    clip.timeline_end = cursor + clip.duration_seconds;
    cursor = clip.timeline_end;
  });
  const ids = new Set(project.clips.map((clip) => clip.clip_id));
  project.effects = (project.effects || []).filter((effect) => ids.has(effect.clip_id));
  project.duration_seconds = cursor;
}

function pushHistory() {
  if (!state.project) return;
  state.history.push(clone(state.project));
  if (state.history.length > 60) state.history.shift();
  state.future = [];
  updateUndoRedo();
}

function markDirty() {
  state.dirty = true;
  const label = $("saveState");
  label.textContent = "有未保存修改";
  label.classList.add("dirty");
  label.classList.remove("saved");
}

function mutate(callback) {
  pushHistory();
  callback();
  recomputeTimeline();
  state.playhead = clamp(state.playhead, 0, projectDuration());
  markDirty();
  renderAll();
}

function updateUndoRedo() {
  $("undoBtn").disabled = !state.history.length;
  $("redoBtn").disabled = !state.future.length;
}

function undo() {
  if (!state.history.length || !state.project) return;
  state.future.push(clone(state.project));
  state.project = state.history.pop();
  recomputeTimeline();
  markDirty();
  renderAll();
  updateUndoRedo();
}

function redo() {
  if (!state.future.length || !state.project) return;
  state.history.push(clone(state.project));
  state.project = state.future.pop();
  recomputeTimeline();
  markDirty();
  renderAll();
  updateUndoRedo();
}

async function init() {
  if (!taskId) {
    document.body.innerHTML = '<div class="empty-state">缺少 task_id，请从主页面选择完整版任务后进入。</div>';
    return;
  }
  bindEvents();
  lucide.createIcons();
  try {
    const bootstrap = await api(`/api/editor/tasks/${encodeURIComponent(taskId)}`);
    state.project = normalizeProject(bootstrap.project || bootstrap);
    state.coverConfig = bootstrap.cover_config || null;
    state.selectedAssetId = state.project.assets[0]?.asset_id || "";
    state.selectedClipId = state.project.clips[0]?.clip_id || "";
    $("taskTitle").textContent = bootstrap.title || taskId;
    const inferredTitle = bootstrap.cover_title || bootstrap.title || "";
    $("coverTitle").value = inferredTitle;
    $("coverSummary").value = bootstrap.summary || "";
    await Promise.all([loadFonts(), loadEditorRemoteConfig()]);
    ensureDefaultTextLayer();
    renderAll();
    selectAsset(state.selectedAssetId, false);
    if (state.project.clips.length) seekTimeline(0);
    updateCoverCanvasSize();
  } catch (error) {
    showToast(`编辑器初始化失败：${error.message}`, true);
    $("taskTitle").textContent = "任务加载失败";
  }
}

function bindEvents() {
  document.querySelectorAll(".editor-tab").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  });
  $("undoBtn").addEventListener("click", undo);
  $("redoBtn").addEventListener("click", redo);
  $("saveProjectBtn").addEventListener("click", () => saveProject().catch((error) => showToast(error.message, true)));
  $("exportVideoBtn").addEventListener("click", () => exportVideo().catch((error) => showToast(error.message, true)));
  $("openRemoteAssetLibraryBtn").addEventListener("click", openRemoteAssetLibrary);
  $("closeRemoteAssetLibraryBtn").addEventListener("click", closeRemoteAssetLibrary);
  $("remoteAssetDialog").addEventListener("click", (event) => {
    if (event.target === $("remoteAssetDialog")) closeRemoteAssetLibrary();
  });
  $("editorRemoteSearchBtn").addEventListener("click", () => loadEditorRemoteVideos(1));
  $("editorRemoteStation").addEventListener("change", () => loadEditorRemoteVideos(1));
  $("editorRemoteSort").addEventListener("change", () => loadEditorRemoteVideos(1));
  $("editorRemoteSearch").addEventListener("input", () => {
    clearTimeout(state.remoteSearchTimer);
    state.remoteSearchTimer = setTimeout(() => loadEditorRemoteVideos(1), 350);
  });
  $("editorRemoteSearch").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    clearTimeout(state.remoteSearchTimer);
    loadEditorRemoteVideos(1);
  });
  $("editorRemoteClearBtn").addEventListener("click", () => {
    $("editorRemoteStart").value = "";
    $("editorRemoteEnd").value = "";
    loadEditorRemoteVideos(1);
  });
  $("editorRemotePrevBtn").addEventListener("click", () => loadEditorRemoteVideos(Math.max(1, state.remotePage - 1)));
  $("editorRemoteNextBtn").addEventListener("click", () => loadEditorRemoteVideos(Math.min(state.remoteTotalPages, state.remotePage + 1)));
  $("editorRemoteJumpBtn").addEventListener("click", jumpEditorRemotePage);
  $("editorRemotePageInput").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    jumpEditorRemotePage();
  });
  $("uploadAssetBtn").addEventListener("click", () => $("assetUploadInput").click());
  $("assetUploadInput").addEventListener("change", uploadAsset);
  $("markInBtn").addEventListener("click", () => { $("sourceIn").value = num($("editorVideo").currentTime).toFixed(3); });
  $("markOutBtn").addEventListener("click", () => { $("sourceOut").value = num($("editorVideo").currentTime).toFixed(3); });
  $("insertClipBtn").addEventListener("click", insertSelectedAsset);
  $("previewTimelineBtn").addEventListener("click", () => setPreviewMode("timeline"));
  $("previewSourceBtn").addEventListener("click", () => setPreviewMode("source"));
  $("playPauseBtn").addEventListener("click", togglePlayback);
  $("timelineZoom").addEventListener("input", () => { state.pixelsPerSecond = num($("timelineZoom").value, 16); renderTimeline(); });
  $("selectionStart").addEventListener("change", renderSelection);
  $("selectionEnd").addEventListener("change", renderSelection);
  $("selectionFromPlayhead").addEventListener("click", () => {
    $("selectionStart").value = state.playhead.toFixed(3);
    $("selectionEnd").value = Math.min(projectDuration(), state.playhead + 1).toFixed(3);
    renderSelection();
  });
  $("deleteRangeBtn").addEventListener("click", deleteSelectedRange);
  $("applyTrimBtn").addEventListener("click", applyClipTrim);
  $("splitClipBtn").addEventListener("click", splitSelectedClipAtPlayhead);
  $("removeClipBtn").addEventListener("click", removeSelectedClip);
  $("addMosaicBtn").addEventListener("click", addMosaic);
  ["mosaicX", "mosaicY", "mosaicW", "mosaicH", "mosaicStart", "mosaicEnd"].forEach((id) => $(id).addEventListener("input", previewMosaicDraft));
  bindTimelinePointerEvents();
  bindVideoEvents();
  bindCoverEvents();
  window.addEventListener("beforeunload", (event) => {
    if (!state.dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("remoteAssetDialog").classList.contains("hidden")) closeRemoteAssetLibrary();
  });
}

function switchTab(tab) {
  document.querySelectorAll(".editor-tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
  $("timelineTab").classList.toggle("active", tab === "timeline");
  $("coverTab").classList.toggle("active", tab === "cover");
  if (tab === "cover") requestAnimationFrame(drawCover);
}

function renderAll() {
  if (!state.project) return;
  renderAssets();
  renderTimeline();
  renderInspector();
  renderMosaicList();
  updateTransport();
  lucide.createIcons();
}

function renderAssets() {
  const container = $("assetList");
  const assets = state.project.assets || [];
  if (!assets.length) {
    container.innerHTML = '<div class="empty-state">暂无素材，请先从远程素材库选择</div>';
    return;
  }
  container.innerHTML = assets.map((asset) => `
    <button class="asset-item ${asset.asset_id === state.selectedAssetId ? "active" : ""}" data-asset-id="${escapeHtml(asset.asset_id)}">
      <strong>${escapeHtml(asset.label || asset.asset_id)}</strong>
      <span>${formatTime(asset.duration_seconds)} · ${asset.source_type === "remote_ucms" ? "远程素材" : asset.origin === "added" ? "本地补充" : "原始素材"}</span>
    </button>`).join("");
  container.querySelectorAll(".asset-item").forEach((button) => button.addEventListener("click", () => selectAsset(button.dataset.assetId)));
}

function selectAsset(assetId, switchMode = true) {
  const asset = assetById(assetId);
  if (!asset) return;
  state.selectedAssetId = assetId;
  $("sourceDuration").textContent = formatTime(asset.duration_seconds);
  $("sourceIn").value = "0.000";
  $("sourceOut").value = num(asset.duration_seconds).toFixed(3);
  if (switchMode) setPreviewMode("source");
  if (state.previewMode === "source") setVideoSource(asset.preview_url, 0, false);
  renderAssets();
}


async function loadEditorRemoteConfig() {
  try {
    const config = await api("/api/config");
    state.remoteConfig = config.remote_ucms || {};
    state.remoteEnabled = Boolean(state.remoteConfig.enabled);
    state.remotePageSize = Math.max(1, num(state.remoteConfig.default_page_size, 10));
    renderEditorRemoteStations();
  } catch (error) {
    state.remoteConfig = {};
    state.remoteEnabled = false;
    renderEditorRemoteStations();
    console.warn("远程素材配置读取失败", error);
  }
}

function renderEditorRemoteStations() {
  const select = $("editorRemoteStation");
  if (!select) return;
  const config = state.remoteConfig || {};
  const counts = config.station_counts || {};
  const options = (config.record_station_options || []).length
    ? config.record_station_options
    : (config.record_stations || []).map((value) => ({ value, label: value }));
  select.innerHTML = options.length
    ? options.map((item) => {
      const value = String(item.value || "");
      const total = item.total ?? counts[value];
      const label = `${value || item.label || "未命名信号源"}${total !== undefined ? `（${total}）` : ""}`;
      return `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`;
    }).join("")
    : '<option value="">默认信号源</option>';
  const preferred = String(config.default_record_station || "");
  if (preferred && Array.from(select.options).some((option) => option.value === preferred)) select.value = preferred;
}

function openRemoteAssetLibrary() {
  const dialog = $("remoteAssetDialog");
  dialog.classList.remove("hidden");
  dialog.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  if (!state.remoteEnabled) {
    setEditorRemoteStatus("远程素材库未启用或配置读取失败，请检查 config.toml 的 remote_ucms 配置。", "error");
    $("editorRemoteList").innerHTML = '<div class="empty-state">当前无法读取远程素材</div>';
    updateEditorRemotePager();
    return;
  }
  if (!state.remoteLoaded) loadEditorRemoteVideos(1);
}

function closeRemoteAssetLibrary() {
  const dialog = $("remoteAssetDialog");
  dialog.classList.add("hidden");
  dialog.setAttribute("aria-hidden", "true");
  document.body.classList.remove("modal-open");
}

function setEditorRemoteStatus(message, kind = "") {
  const status = $("editorRemoteStatus");
  status.textContent = String(message || "");
  status.classList.remove("error", "busy");
  if (kind) status.classList.add(kind);
}

function editorRemoteApiTime(value) {
  if (!value) return "";
  const text = String(value).replace("T", " ");
  return (text.length === 16 ? `${text}:00` : text).slice(0, 19);
}

async function loadEditorRemoteVideos(page = 1) {
  if (!state.remoteEnabled) return;
  const requestSeq = ++state.remoteRequestSeq;
  const targetPage = Math.max(1, Math.floor(num(page, 1)));
  const params = new URLSearchParams({
    record_station: $("editorRemoteStation").value || "",
    current: String(targetPage),
    page_size: String(state.remotePageSize || 10),
    sort_order: $("editorRemoteSort").value || "desc",
  });
  const keyword = $("editorRemoteSearch").value.trim();
  const startTime = editorRemoteApiTime($("editorRemoteStart").value);
  const endTime = editorRemoteApiTime($("editorRemoteEnd").value);
  if (keyword) params.set("keyword", keyword);
  if (startTime) params.set("start_time", startTime);
  if (endTime) params.set("end_time", endTime);
  setEditorRemoteStatus("正在读取远程素材库…", "busy");
  $("editorRemoteList").innerHTML = '<div class="empty-state">正在加载远程素材…</div>';
  try {
    const data = await api(`/api/remote-videos?${params.toString()}`);
    if (requestSeq !== state.remoteRequestSeq) return;
    const pagination = data.pagination || {};
    state.remoteVideos = Array.isArray(data.items) ? data.items : [];
    state.remotePage = Math.max(1, num(pagination.current ?? pagination.page, targetPage));
    state.remotePageSize = Math.max(1, num(pagination.pageSize ?? pagination.page_size, state.remotePageSize || 10));
    state.remoteTotal = Math.max(0, num(pagination.total, state.remoteVideos.length));
    state.remoteTotalPages = Math.max(1, Math.ceil(state.remoteTotal / state.remotePageSize));
    state.remoteLoaded = true;
    renderEditorRemoteList();
    updateEditorRemotePager();
    setEditorRemoteStatus(
      state.remoteVideos.length
        ? `已加载 ${state.remoteVideos.length} 条，本页素材可直接加入精剪工程。`
        : "当前筛选条件下没有远程素材。"
    );
  } catch (error) {
    if (requestSeq !== state.remoteRequestSeq) return;
    state.remoteVideos = [];
    state.remoteLoaded = false;
    $("editorRemoteList").innerHTML = '<div class="empty-state">远程素材加载失败，请调整条件后重试</div>';
    setEditorRemoteStatus(`远程素材加载失败：${error.message}`, "error");
    updateEditorRemotePager();
  }
}

function editorRemoteVideoKey(video) {
  const station = video.record_station || video.recordStation || "";
  const identifier = video.remote_id || video.id || video.guid || video.name || "remote";
  return `${station}::${identifier}`;
}

function renderEditorRemoteList() {
  const container = $("editorRemoteList");
  if (!state.remoteVideos.length) {
    container.innerHTML = '<div class="empty-state">没有符合条件的远程素材</div>';
    return;
  }
  container.innerHTML = state.remoteVideos.map((video, index) => {
    const key = editorRemoteVideoKey(video);
    const adding = state.remoteAddingKey === key;
    const busy = Boolean(state.remoteAddingKey);
    const title = video.display_name || video.name || `远程素材 ${index + 1}`;
    const duration = video.duration_text || formatTime(video.duration || 0);
    const station = video.record_station || video.recordStation || "未标注信号源";
    const created = video.create_time || video.createTime || "时间未知";
    const remoteId = video.remote_id || video.id || video.guid || "ID 未知";
    return `<article class="remote-asset-row ${adding ? "adding" : ""}">
      <div class="remote-asset-copy">
        <strong title="${escapeHtml(title)}">${escapeHtml(title)}</strong>
        <div class="remote-asset-meta">
          <span>${escapeHtml(station)}</span><span>${escapeHtml(duration)}</span><span>${escapeHtml(created)}</span><span>ID ${escapeHtml(remoteId)}</span>
        </div>
      </div>
      <button class="button ${adding ? "secondary" : "primary"} remote-add-button" type="button" data-remote-index="${index}" ${busy ? "disabled" : ""}>
        ${adding ? '<i data-lucide="loader-circle"></i><span>下载并加入中…</span>' : '<i data-lucide="plus"></i><span>加入素材箱</span>'}
      </button>
    </article>`;
  }).join("");
  container.querySelectorAll("[data-remote-index]").forEach((button) => {
    button.addEventListener("click", () => addEditorRemoteAsset(state.remoteVideos[num(button.dataset.remoteIndex)]));
  });
  lucide.createIcons();
}

function updateEditorRemotePager() {
  const page = Math.max(1, state.remotePage || 1);
  const totalPages = Math.max(1, state.remoteTotalPages || 1);
  $("editorRemotePageInfo").textContent = `第 ${page} / ${totalPages} 页 · 共 ${state.remoteTotal || 0} 条`;
  $("editorRemotePrevBtn").disabled = page <= 1 || Boolean(state.remoteAddingKey);
  $("editorRemoteNextBtn").disabled = page >= totalPages || Boolean(state.remoteAddingKey);
  $("editorRemotePageInput").max = String(totalPages);
  if (document.activeElement !== $("editorRemotePageInput")) $("editorRemotePageInput").value = String(page);
}

function jumpEditorRemotePage() {
  const page = clamp(Math.floor(num($("editorRemotePageInput").value, 1)), 1, state.remoteTotalPages || 1);
  $("editorRemotePageInput").value = String(page);
  loadEditorRemoteVideos(page);
}

async function savePendingProjectBeforeAssetChange() {
  if (state.dirty) await saveProject();
}

function applyAddedAssetResult(result) {
  state.project = normalizeProject(result.project);
  state.history = [];
  state.future = [];
  state.dirty = false;
  state.selectedAssetId = result.asset.asset_id;
  $("saveState").textContent = `素材已保存 · v${state.project.revision}`;
  $("saveState").classList.remove("dirty");
  $("saveState").classList.add("saved");
  updateUndoRedo();
  renderAll();
  selectAsset(result.asset.asset_id);
}

async function addEditorRemoteAsset(video) {
  if (!video || state.remoteAddingKey) return;
  const key = editorRemoteVideoKey(video);
  state.remoteAddingKey = key;
  renderEditorRemoteList();
  updateEditorRemotePager();
  setEditorRemoteStatus("正在下载远程素材或读取共享缓存，请稍候…", "busy");
  try {
    await savePendingProjectBeforeAssetChange();
    const result = await api(`/api/editor/tasks/${encodeURIComponent(taskId)}/remote-assets`, {
      method: "POST",
      body: JSON.stringify({ remote_video: video, force_remote_download: false }),
    });
    applyAddedAssetResult(result);
    const action = result.duplicate ? "素材已在素材箱中，已为你选中" : (result.downloaded ? "远程素材已下载并加入素材箱" : "已从共享缓存加入素材箱");
    setEditorRemoteStatus(action);
    showToast(action);
  } catch (error) {
    setEditorRemoteStatus(`加入远程素材失败：${error.message}`, "error");
    showToast(`加入远程素材失败：${error.message}`, true);
  } finally {
    state.remoteAddingKey = "";
    renderEditorRemoteList();
    updateEditorRemotePager();
  }
}

async function uploadAsset(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    await savePendingProjectBeforeAssetChange();
    const result = await api(`/api/editor/tasks/${encodeURIComponent(taskId)}/assets`, { method: "POST", body: form });
    applyAddedAssetResult(result);
    showToast("素材已加入编辑工程");
  } catch (error) {
    showToast(`上传素材失败：${error.message}`, true);
  }
}

function selectedRange() {
  let start = clamp(num($("selectionStart").value), 0, projectDuration());
  let end = clamp(num($("selectionEnd").value), 0, projectDuration());
  if (end < start) [start, end] = [end, start];
  return [start, end];
}

function clipAtTime(time) {
  const duration = projectDuration();
  const target = time >= duration && duration > 0 ? duration - 0.0001 : time;
  return state.project.clips.find((clip) => target >= clip.timeline_start && target < clip.timeline_end) || null;
}

function selectClip(clipId, seek = true) {
  const clip = clipById(clipId);
  if (!clip) return;
  state.selectedClipId = clipId;
  $("clipSourceIn").value = clip.source_in.toFixed(3);
  $("clipSourceOut").value = clip.source_out.toFixed(3);
  $("selectedClipLabel").textContent = clipLabel(clip);
  $("mosaicStart").value = "0.000";
  $("mosaicEnd").value = clip.duration_seconds.toFixed(3);
  if (seek) seekTimeline(clip.timeline_start);
  renderTimeline();
  renderMosaicList();
}

function clipLabel(clip) {
  const asset = assetById(clip.asset_id);
  return asset?.label || clip.clip_id;
}

function renderTimeline() {
  if (!state.project) return;
  const duration = Math.max(projectDuration(), 10);
  const width = Math.max($("timelineViewport").clientWidth || 800, Math.ceil(duration * state.pixelsPerSecond));
  const content = $("timelineContent");
  content.style.width = `${width}px`;
  const ruler = $("timelineRuler");
  const tickStep = state.pixelsPerSecond >= 50 ? 1 : state.pixelsPerSecond >= 18 ? 5 : state.pixelsPerSecond >= 8 ? 10 : 30;
  const labels = [];
  for (let second = 0; second <= duration; second += tickStep) {
    labels.push(`<span class="ruler-label" style="left:${second * state.pixelsPerSecond}px">${formatTime(second).slice(0, 8)}</span>`);
  }
  ruler.style.backgroundSize = `${tickStep * state.pixelsPerSecond}px 100%`;
  ruler.innerHTML = labels.join("");

  const track = $("videoTrack");
  track.innerHTML = state.project.clips.map((clip, index) => {
    const left = clip.timeline_start * state.pixelsPerSecond;
    const clipWidth = Math.max(8, clip.duration_seconds * state.pixelsPerSecond);
    return `<div class="timeline-clip ${clip.clip_id === state.selectedClipId ? "selected" : ""}" draggable="true" data-clip-id="${escapeHtml(clip.clip_id)}" data-index="${index}" style="left:${left}px;width:${clipWidth}px">
      <strong>${escapeHtml(clipLabel(clip))}</strong>
      <span>${formatTime(clip.duration_seconds)} · ${formatTime(clip.source_in).slice(3)}–${formatTime(clip.source_out).slice(3)}</span>
    </div>`;
  }).join("");
  track.querySelectorAll(".timeline-clip").forEach((element) => {
    element.addEventListener("click", (event) => { event.stopPropagation(); selectClip(element.dataset.clipId); });
    element.addEventListener("dragstart", () => element.classList.add("dragging"));
    element.addEventListener("dragend", () => element.classList.remove("dragging"));
    element.addEventListener("dragover", (event) => event.preventDefault());
    element.addEventListener("drop", (event) => {
      event.preventDefault();
      const dragged = track.querySelector(".timeline-clip.dragging");
      if (!dragged || dragged === element) return;
      reorderClip(dragged.dataset.clipId, Number(element.dataset.index));
    });
  });

  $("effectTrack").innerHTML = (state.project.effects || []).map((effect) => {
    const clip = clipById(effect.clip_id);
    if (!clip) return "";
    const start = clip.timeline_start + num(effect.start);
    const effectWidth = Math.max(5, (num(effect.end) - num(effect.start)) * state.pixelsPerSecond);
    return `<div class="effect-block" data-effect-id="${escapeHtml(effect.effect_id)}" style="left:${start * state.pixelsPerSecond}px;width:${effectWidth}px">马赛克 ${Math.round(num(effect.rect?.x) * 100)}%,${Math.round(num(effect.rect?.y) * 100)}%</div>`;
  }).join("");
  $("effectTrack").querySelectorAll(".effect-block").forEach((block) => block.addEventListener("click", () => selectEffect(block.dataset.effectId)));
  $("clipCount").textContent = `${state.project.clips.length} 个片段`;
  $("totalDuration").textContent = formatTime(projectDuration());
  renderPlayhead();
  renderSelection();
}

function reorderClip(clipId, targetIndex) {
  const currentIndex = state.project.clips.findIndex((clip) => clip.clip_id === clipId);
  if (currentIndex < 0 || currentIndex === targetIndex) return;
  mutate(() => {
    const [clip] = state.project.clips.splice(currentIndex, 1);
    const adjusted = currentIndex < targetIndex ? targetIndex - 1 : targetIndex;
    state.project.clips.splice(clamp(adjusted, 0, state.project.clips.length), 0, clip);
  });
}

function insertSelectedAsset() {
  const asset = assetById(state.selectedAssetId);
  if (!asset) return showToast("请先选择素材", true);
  const sourceIn = clamp(num($("sourceIn").value), 0, asset.duration_seconds);
  const sourceOut = clamp(num($("sourceOut").value), 0, asset.duration_seconds);
  if (sourceOut - sourceIn < 0.04) return showToast("素材出点必须晚于入点", true);
  mutate(() => {
    let index = state.project.clips.findIndex((clip) => state.playhead <= clip.timeline_start + 0.001);
    const containing = clipAtTime(state.playhead);
    if (containing && state.playhead > containing.timeline_start + 0.04 && state.playhead < containing.timeline_end - 0.04) {
      splitClipInternal(containing, state.playhead - containing.timeline_start);
      recomputeTimeline();
      index = state.project.clips.findIndex((clip) => clip.timeline_start >= state.playhead - 0.001);
    }
    if (index < 0) index = state.project.clips.length;
    const clip = { clip_id: uid("clip"), asset_id: asset.asset_id, source_in: sourceIn, source_out: sourceOut };
    state.project.clips.splice(index, 0, clip);
    state.selectedClipId = clip.clip_id;
  });
  showToast("片段已插入时间轴");
}

function splitEffectsForClip(original, segments) {
  const effects = state.project.effects.filter((effect) => effect.clip_id === original.clip_id);
  state.project.effects = state.project.effects.filter((effect) => effect.clip_id !== original.clip_id);
  for (const segment of segments) {
    const segmentStart = segment._origin_offset;
    const segmentEnd = segmentStart + (segment.source_out - segment.source_in);
    for (const effect of effects) {
      const start = Math.max(num(effect.start), segmentStart);
      const end = Math.min(num(effect.end), segmentEnd);
      if (end - start < 0.04) continue;
      state.project.effects.push({
        ...clone(effect),
        effect_id: segment.clip_id === original.clip_id ? effect.effect_id : uid("mosaic"),
        clip_id: segment.clip_id,
        start: start - segmentStart,
        end: end - segmentStart,
      });
    }
    delete segment._origin_offset;
  }
}

function splitClipInternal(clip, offset) {
  const index = state.project.clips.findIndex((item) => item.clip_id === clip.clip_id);
  if (index < 0 || offset < 0.04 || offset > clip.duration_seconds - 0.04) return false;
  const left = { ...clone(clip), source_out: clip.source_in + offset, _origin_offset: 0 };
  const right = { ...clone(clip), clip_id: uid("clip"), source_in: clip.source_in + offset, _origin_offset: offset };
  state.project.clips.splice(index, 1, left, right);
  splitEffectsForClip(clip, [left, right]);
  return true;
}

function splitSelectedClipAtPlayhead() {
  const clip = clipById(state.selectedClipId);
  if (!clip || state.playhead <= clip.timeline_start || state.playhead >= clip.timeline_end) return showToast("播放头需要位于选中片段内部", true);
  mutate(() => splitClipInternal(clip, state.playhead - clip.timeline_start));
}

function applyClipTrim() {
  const clip = clipById(state.selectedClipId);
  if (!clip) return;
  const asset = assetById(clip.asset_id);
  const sourceIn = clamp(num($("clipSourceIn").value), 0, asset?.duration_seconds || Infinity);
  const sourceOut = clamp(num($("clipSourceOut").value), 0, asset?.duration_seconds || Infinity);
  if (sourceOut - sourceIn < 0.04) return showToast("裁剪后的片段必须至少保留一帧", true);
  mutate(() => {
    const oldIn = clip.source_in;
    const oldDuration = clip.duration_seconds;
    clip.source_in = sourceIn;
    clip.source_out = sourceOut;
    const shift = sourceIn - oldIn;
    state.project.effects.filter((effect) => effect.clip_id === clip.clip_id).forEach((effect) => {
      effect.start = clamp(num(effect.start) - shift, 0, sourceOut - sourceIn);
      effect.end = clamp(num(effect.end) - shift, 0, sourceOut - sourceIn);
    });
    state.project.effects = state.project.effects.filter((effect) => effect.clip_id !== clip.clip_id || effect.end - effect.start >= 0.04);
    if (oldDuration !== clip.duration_seconds) state.playhead = clip.timeline_start;
  });
}

function removeSelectedClip() {
  const clip = clipById(state.selectedClipId);
  if (!clip) return;
  mutate(() => {
    state.project.clips = state.project.clips.filter((item) => item.clip_id !== clip.clip_id);
    state.project.effects = state.project.effects.filter((effect) => effect.clip_id !== clip.clip_id);
    state.selectedClipId = state.project.clips[0]?.clip_id || "";
  });
}

function deleteSelectedRange() {
  const [start, end] = selectedRange();
  if (end - start < 0.04) return showToast("请先拖动时间轴或输入有效删除区间", true);
  mutate(() => {
    const nextClips = [];
    const nextEffects = [];
    for (const clip of state.project.clips) {
      const clipStart = clip.timeline_start;
      const clipEnd = clip.timeline_end;
      if (end <= clipStart || start >= clipEnd) {
        nextClips.push(clone(clip));
        nextEffects.push(...state.project.effects.filter((effect) => effect.clip_id === clip.clip_id).map(clone));
        continue;
      }
      const removeStart = clamp(start - clipStart, 0, clip.duration_seconds);
      const removeEnd = clamp(end - clipStart, 0, clip.duration_seconds);
      const segments = [];
      if (removeStart >= 0.04) segments.push({ ...clone(clip), source_out: clip.source_in + removeStart, _origin_offset: 0 });
      if (clip.duration_seconds - removeEnd >= 0.04) segments.push({ ...clone(clip), clip_id: segments.length ? uid("clip") : clip.clip_id, source_in: clip.source_in + removeEnd, _origin_offset: removeEnd });
      const effects = state.project.effects.filter((effect) => effect.clip_id === clip.clip_id);
      for (const segment of segments) {
        const originOffset = segment._origin_offset;
        const segmentEnd = originOffset + (segment.source_out - segment.source_in);
        for (const effect of effects) {
          const effectStart = Math.max(num(effect.start), originOffset);
          const effectEnd = Math.min(num(effect.end), segmentEnd);
          if (effectEnd - effectStart >= 0.04) {
            nextEffects.push({ ...clone(effect), effect_id: segment.clip_id === clip.clip_id ? effect.effect_id : uid("mosaic"), clip_id: segment.clip_id, start: effectStart - originOffset, end: effectEnd - originOffset });
          }
        }
        delete segment._origin_offset;
        nextClips.push(segment);
      }
    }
    state.project.clips = nextClips;
    state.project.effects = nextEffects;
    state.selectedClipId = state.project.clips[0]?.clip_id || "";
    state.playhead = start;
    $("selectionStart").value = start.toFixed(3);
    $("selectionEnd").value = start.toFixed(3);
  });
  showToast("已删除选中区间，后续片段自动前移");
}

function renderInspector() {
  const clip = clipById(state.selectedClipId);
  $("selectedClipLabel").textContent = clip ? clipLabel(clip) : "未选择";
  if (clip) {
    $("clipSourceIn").value = clip.source_in.toFixed(3);
    $("clipSourceOut").value = clip.source_out.toFixed(3);
    $("mosaicEnd").max = clip.duration_seconds.toFixed(3);
    $("mosaicStart").max = clip.duration_seconds.toFixed(3);
  }
}

function addMosaic() {
  const clip = clipById(state.selectedClipId);
  if (!clip) return showToast("请先选择一个时间轴片段", true);
  const start = clamp(num($("mosaicStart").value), 0, clip.duration_seconds);
  const end = clamp(num($("mosaicEnd").value), 0, clip.duration_seconds);
  if (end - start < 0.04) return showToast("马赛克结束时间必须晚于开始时间", true);
  mutate(() => {
    const effect = {
      effect_id: uid("mosaic"), type: "mosaic", clip_id: clip.clip_id, start, end,
      rect: {
        x: clamp(num($("mosaicX").value) / 100, 0, 0.99),
        y: clamp(num($("mosaicY").value) / 100, 0, 0.99),
        w: clamp(num($("mosaicW").value) / 100, 0.01, 1),
        h: clamp(num($("mosaicH").value) / 100, 0.01, 1),
      },
      strength: 18,
    };
    effect.rect.w = Math.min(effect.rect.w, 1 - effect.rect.x);
    effect.rect.h = Math.min(effect.rect.h, 1 - effect.rect.y);
    state.project.effects.push(effect);
    state.selectedEffectId = effect.effect_id;
  });
}

function selectEffect(effectId) {
  const effect = state.project.effects.find((item) => item.effect_id === effectId);
  if (!effect) return;
  state.selectedEffectId = effectId;
  state.selectedClipId = effect.clip_id;
  $("mosaicX").value = Math.round(num(effect.rect?.x) * 100);
  $("mosaicY").value = Math.round(num(effect.rect?.y) * 100);
  $("mosaicW").value = Math.round(num(effect.rect?.w) * 100);
  $("mosaicH").value = Math.round(num(effect.rect?.h) * 100);
  $("mosaicStart").value = num(effect.start).toFixed(3);
  $("mosaicEnd").value = num(effect.end).toFixed(3);
  const clip = clipById(effect.clip_id);
  if (clip) seekTimeline(clip.timeline_start + effect.start);
  renderMosaicList();
}

function renderMosaicList() {
  const list = $("mosaicList");
  const effects = (state.project?.effects || []).filter((effect) => effect.clip_id === state.selectedClipId);
  list.innerHTML = effects.length ? effects.map((effect) => `<div class="effect-row" data-effect-id="${escapeHtml(effect.effect_id)}"><span>${formatTime(effect.start).slice(3)}–${formatTime(effect.end).slice(3)} · ${Math.round(effect.rect.w * 100)}×${Math.round(effect.rect.h * 100)}%</span><button title="删除">删除</button></div>`).join("") : '<div class="empty-state">当前片段没有马赛克</div>';
  list.querySelectorAll(".effect-row").forEach((row) => {
    row.addEventListener("click", () => selectEffect(row.dataset.effectId));
    row.querySelector("button").addEventListener("click", (event) => {
      event.stopPropagation();
      mutate(() => { state.project.effects = state.project.effects.filter((effect) => effect.effect_id !== row.dataset.effectId); });
    });
  });
}

function previewMosaicDraft() {
  const rect = {
    x: num($("mosaicX").value) / 100,
    y: num($("mosaicY").value) / 100,
    w: num($("mosaicW").value) / 100,
    h: num($("mosaicH").value) / 100,
  };
  drawMosaicOverlay(rect);
}

function drawMosaicOverlay(rect = null) {
  const overlay = $("mosaicOverlay");
  if (!rect) {
    const clip = clipAtTime(state.playhead);
    if (clip) {
      const local = state.playhead - clip.timeline_start;
      const effect = state.project.effects.find((item) => item.clip_id === clip.clip_id && local >= item.start && local <= item.end);
      rect = effect?.rect || null;
    }
  }
  if (!rect) return overlay.classList.add("hidden");
  overlay.classList.remove("hidden");
  overlay.style.left = `${clamp(num(rect.x), 0, 1) * 100}%`;
  overlay.style.top = `${clamp(num(rect.y), 0, 1) * 100}%`;
  overlay.style.width = `${clamp(num(rect.w), 0.01, 1) * 100}%`;
  overlay.style.height = `${clamp(num(rect.h), 0.01, 1) * 100}%`;
}

function bindTimelinePointerEvents() {
  const viewport = $("timelineViewport");
  const content = $("timelineContent");
  const positionToTime = (event) => {
    const rect = content.getBoundingClientRect();
    return clamp((event.clientX - rect.left) / state.pixelsPerSecond, 0, projectDuration());
  };
  content.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".timeline-clip") || event.target.closest(".effect-block")) return;
    state.selecting = true;
    state.selectionAnchor = positionToTime(event);
    $("selectionStart").value = state.selectionAnchor.toFixed(3);
    $("selectionEnd").value = state.selectionAnchor.toFixed(3);
    content.setPointerCapture(event.pointerId);
    seekTimeline(state.selectionAnchor);
  });
  content.addEventListener("pointermove", (event) => {
    if (!state.selecting) return;
    const current = positionToTime(event);
    $("selectionStart").value = Math.min(state.selectionAnchor, current).toFixed(3);
    $("selectionEnd").value = Math.max(state.selectionAnchor, current).toFixed(3);
    renderSelection();
  });
  const finish = () => { state.selecting = false; };
  content.addEventListener("pointerup", finish);
  content.addEventListener("pointercancel", finish);
  viewport.addEventListener("scroll", renderPlayhead);
}

function renderSelection() {
  const [start, end] = selectedRange();
  const selection = $("timelineSelection");
  if (end - start < 0.001) return selection.classList.add("hidden");
  selection.classList.remove("hidden");
  selection.style.left = `${start * state.pixelsPerSecond}px`;
  selection.style.width = `${Math.max(1, (end - start) * state.pixelsPerSecond)}px`;
}

function renderPlayhead() {
  $("playhead").style.left = `${state.playhead * state.pixelsPerSecond}px`;
}

function updateTransport() {
  $("timeReadout").textContent = `${formatTime(state.playhead)} / ${formatTime(projectDuration())}`;
  $("previewLabel").textContent = state.previewMode === "timeline" ? `成片时间 ${formatTime(state.playhead)}` : "素材预览";
  renderPlayhead();
  drawMosaicOverlay();
}

function setPreviewMode(mode) {
  state.previewMode = mode === "source" ? "source" : "timeline";
  $("previewTimelineBtn").classList.toggle("active", state.previewMode === "timeline");
  $("previewSourceBtn").classList.toggle("active", state.previewMode === "source");
  if (state.previewMode === "source") {
    const asset = assetById(state.selectedAssetId);
    if (asset) setVideoSource(asset.preview_url, num($("sourceIn").value), false);
  } else {
    seekTimeline(state.playhead);
  }
  updateTransport();
}

function setVideoSource(url, time, autoplay) {
  const video = $("editorVideo");
  if (!url) return;
  const seek = () => {
    video.currentTime = clamp(time, 0, Number.isFinite(video.duration) ? video.duration : time);
    if (autoplay) video.play().catch(() => {});
  };
  if (state.activeVideoUrl !== url) {
    state.activeVideoUrl = url;
    video.src = url;
    video.load();
    video.addEventListener("loadedmetadata", seek, { once: true });
  } else {
    seek();
  }
}

function seekTimeline(time, autoplay = false) {
  state.playhead = clamp(num(time), 0, projectDuration());
  const clip = clipAtTime(state.playhead);
  if (clip) {
    const asset = assetById(clip.asset_id);
    const sourceTime = clip.source_in + (state.playhead - clip.timeline_start);
    state.activePreviewClipId = clip.clip_id;
    state.selectedClipId = clip.clip_id;
    if (state.previewMode === "timeline") setVideoSource(asset?.preview_url, sourceTime, autoplay);
  }
  updateTransport();
  renderTimeline();
}

function bindVideoEvents() {
  const video = $("editorVideo");
  video.addEventListener("play", () => { $("playPauseBtn").innerHTML = '<i data-lucide="pause"></i>'; lucide.createIcons(); });
  video.addEventListener("pause", () => { $("playPauseBtn").innerHTML = '<i data-lucide="play"></i>'; lucide.createIcons(); });
  video.addEventListener("timeupdate", () => {
    if (state.previewMode !== "timeline" || state.switchGuard) return;
    const clip = clipById(state.activePreviewClipId);
    if (!clip) return;
    const sourceTime = num(video.currentTime);
    const local = sourceTime - clip.source_in;
    state.playhead = clamp(clip.timeline_start + local, clip.timeline_start, clip.timeline_end);
    if (sourceTime >= clip.source_out - 0.035 && !video.paused) {
      const index = state.project.clips.findIndex((item) => item.clip_id === clip.clip_id);
      const next = state.project.clips[index + 1];
      if (next) {
        state.switchGuard = true;
        state.playhead = next.timeline_start;
        const asset = assetById(next.asset_id);
        state.activePreviewClipId = next.clip_id;
        setVideoSource(asset?.preview_url, next.source_in, true);
        setTimeout(() => { state.switchGuard = false; }, 80);
      } else {
        video.pause();
        state.playhead = projectDuration();
      }
    }
    updateTransport();
  });
  video.addEventListener("seeked", () => {
    if (state.previewMode !== "timeline") return;
    const clip = clipById(state.activePreviewClipId);
    if (!clip) return;
    state.playhead = clamp(clip.timeline_start + (video.currentTime - clip.source_in), clip.timeline_start, clip.timeline_end);
    updateTransport();
  });
}

function togglePlayback() {
  const video = $("editorVideo");
  if (video.paused) {
    if (state.previewMode === "timeline") seekTimeline(state.playhead, true);
    else video.play().catch((error) => showToast(error.message, true));
  } else {
    video.pause();
  }
}

async function saveProject() {
  if (!state.project) return;
  const response = await api(`/api/editor/tasks/${encodeURIComponent(taskId)}/project`, {
    method: "PUT",
    body: JSON.stringify(state.project),
  });
  state.project = normalizeProject(response.project || response);
  state.history = [];
  state.future = [];
  state.dirty = false;
  $("saveState").textContent = `已保存 v${state.project.revision || 1}`;
  $("saveState").classList.remove("dirty");
  $("saveState").classList.add("saved");
  updateUndoRedo();
  renderAll();
  return response;
}

async function exportVideo() {
  if (!state.project?.clips?.length) throw new Error("时间轴没有可导出的片段");
  $("exportVideoBtn").disabled = true;
  try {
    await saveProject();
    const job = await api(`/api/editor/tasks/${encodeURIComponent(taskId)}/export`, { method: "POST", body: "{}" });
    showToast("视频已进入后台导出队列");
    pollExport(job.job_id);
  } finally {
    $("exportVideoBtn").disabled = false;
  }
}

async function pollExport(jobId) {
  if (!jobId) return;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (["pending", "running"].includes(job.status)) {
      $("saveState").textContent = job.status === "pending" ? "导出排队中" : "视频导出中";
      setTimeout(() => pollExport(jobId), 2500);
      return;
    }
    if (job.status === "success") {
      $("saveState").textContent = "视频导出完成";
      showToast("视频导出完成，可在主页面输出产物中查看");
    } else {
      showToast(`视频导出失败：${job.user_message || job.status}`, true);
    }
  } catch (error) {
    showToast(`读取导出状态失败：${error.message}`, true);
  }
}

function bindCoverEvents() {
  $("generateCoverBtn").addEventListener("click", generateCoverCandidates);
  $("uploadCoverBtn").addEventListener("click", () => $("coverUploadInput").click());
  $("coverUploadInput").addEventListener("change", uploadCoverImage);
  $("aiRestoreBtn").addEventListener("click", aiRestoreCover);
  $("exportCoverBtn").addEventListener("click", exportCover);
  $("addTextLayerBtn").addEventListener("click", addTextLayer);
  $("removeTextLayerBtn").addEventListener("click", removeTextLayer);
  $("textLayerSelect").addEventListener("change", () => selectTextLayer($("textLayerSelect").value));
  $("uploadFontBtn").addEventListener("click", () => $("fontUploadInput").click());
  $("fontUploadInput").addEventListener("change", uploadFont);
  $("coverRatio").addEventListener("change", () => { updateCoverCanvasSize(); drawCover(); });
  ["cropX", "cropY", "cropW", "cropH"].forEach((id) => $(id).addEventListener("input", drawCover));
  ["coverText", "coverFont", "coverFontSize", "coverTextColor", "coverStrokeColor", "coverTextX", "coverTextY", "coverTextWidth", "coverStrokeWidth"].forEach((id) => {
    $(id).addEventListener("input", updateSelectedTextLayerFromControls);
    $(id).addEventListener("change", updateSelectedTextLayerFromControls);
  });
}

async function loadFonts() {
  try {
    state.fonts = await api(`/api/editor/tasks/${encodeURIComponent(taskId)}/fonts`);
    await Promise.all(state.fonts.map(loadBrowserFont));
    renderFontOptions();
  } catch (error) {
    console.warn("字体列表加载失败", error);
  }
}

async function loadBrowserFont(font) {
  if (!font.url) return;
  const family = `EditorFont_${String(font.name).replace(/[^A-Za-z0-9_]/g, "_")}`;
  font.family = family;
  try {
    const face = new FontFace(family, `url(${font.url})`);
    await face.load();
    document.fonts.add(face);
  } catch (error) {
    console.warn(`字体 ${font.name} 浏览器加载失败`, error);
  }
}

function renderFontOptions() {
  const current = $("coverFont").value;
  $("coverFont").innerHTML = '<option value="">系统默认</option>' + state.fonts.map((font) => `<option value="${escapeHtml(font.name)}">${escapeHtml(font.name)}</option>`).join("");
  if ([...$("coverFont").options].some((option) => option.value === current)) $("coverFont").value = current;
}

async function uploadFont(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const result = await api(`/api/editor/tasks/${encodeURIComponent(taskId)}/fonts`, { method: "POST", body: form });
    state.fonts.push(result);
    await loadBrowserFont(result);
    renderFontOptions();
    $("coverFont").value = result.name;
    updateSelectedTextLayerFromControls();
    showToast("字体已上传。请确认拥有相应使用授权。");
  } catch (error) {
    showToast(`字体上传失败：${error.message}`, true);
  }
}

function ensureDefaultTextLayer() {
  if (state.textLayers.length) return;
  state.textLayers.push({
    layer_id: uid("text"), text: $("coverTitle").value || "封面标题", font: "", font_size: 76,
    color: "#ffffff", stroke_color: "#000000", stroke_width: 3, x: 0.08, y: 0.68, max_width: 0.8,
  });
  state.selectedTextLayerId = state.textLayers[0].layer_id;
  renderTextLayerControls();
}

function addTextLayer() {
  const layer = {
    layer_id: uid("text"), text: "副标题", font: "", font_size: 42,
    color: "#ffffff", stroke_color: "#000000", stroke_width: 2, x: 0.08, y: 0.82, max_width: 0.8,
  };
  state.textLayers.push(layer);
  state.selectedTextLayerId = layer.layer_id;
  renderTextLayerControls();
  drawCover();
}

function removeTextLayer() {
  if (!state.selectedTextLayerId) return;
  state.textLayers = state.textLayers.filter((layer) => layer.layer_id !== state.selectedTextLayerId);
  state.selectedTextLayerId = state.textLayers[0]?.layer_id || "";
  renderTextLayerControls();
  drawCover();
}

function selectedTextLayer() {
  return state.textLayers.find((layer) => layer.layer_id === state.selectedTextLayerId);
}

function selectTextLayer(layerId) {
  state.selectedTextLayerId = layerId;
  renderTextLayerControls();
  drawCover();
}

function renderTextLayerControls() {
  const select = $("textLayerSelect");
  select.innerHTML = state.textLayers.map((layer, index) => `<option value="${escapeHtml(layer.layer_id)}">文字 ${index + 1} · ${escapeHtml(layer.text.slice(0, 12))}</option>`).join("");
  select.value = state.selectedTextLayerId;
  const layer = selectedTextLayer();
  if (!layer) return;
  $("coverText").value = layer.text;
  $("coverFont").value = layer.font || "";
  $("coverFontSize").value = layer.font_size;
  $("coverTextColor").value = normalizeHexColor(layer.color, "#ffffff");
  $("coverStrokeColor").value = normalizeHexColor(layer.stroke_color, "#000000");
  $("coverTextX").value = Math.round(layer.x * 100);
  $("coverTextY").value = Math.round(layer.y * 100);
  $("coverTextWidth").value = Math.round(layer.max_width * 100);
  $("coverStrokeWidth").value = layer.stroke_width;
}

function updateSelectedTextLayerFromControls() {
  const layer = selectedTextLayer();
  if (!layer) return;
  layer.text = $("coverText").value;
  layer.font = $("coverFont").value;
  layer.font_size = clamp(num($("coverFontSize").value, 76), 12, 300);
  layer.color = $("coverTextColor").value;
  layer.stroke_color = $("coverStrokeColor").value;
  layer.x = clamp(num($("coverTextX").value) / 100, 0, 1);
  layer.y = clamp(num($("coverTextY").value) / 100, 0, 1);
  layer.max_width = clamp(num($("coverTextWidth").value) / 100, 0.1, 1);
  layer.stroke_width = clamp(num($("coverStrokeWidth").value), 0, 20);
  renderTextLayerSelectLabels();
  drawCover();
}

function renderTextLayerSelectLabels() {
  [...$("textLayerSelect").options].forEach((option, index) => {
    option.textContent = `文字 ${index + 1} · ${(state.textLayers[index]?.text || "").slice(0, 12)}`;
  });
}

function normalizeHexColor(value, fallback) {
  return /^#[0-9a-f]{6}$/i.test(String(value || "")) ? value : fallback;
}

function coverCrop() {
  const x = clamp(num($("cropX").value) / 100, 0, 0.99);
  const y = clamp(num($("cropY").value) / 100, 0, 0.99);
  const w = clamp(num($("cropW").value) / 100, 0.01, 1 - x);
  const h = clamp(num($("cropH").value) / 100, 0.01, 1 - y);
  return { x, y, w, h };
}

function coverOutputSize() {
  const ratio = $("coverRatio").value;
  const fourK = $("coverSize").value === "4K";
  if (ratio === "9:16") return fourK ? [2160, 3840] : [1080, 1920];
  if (ratio === "1:1") return fourK ? [3072, 3072] : [2048, 2048];
  return fourK ? [3840, 2160] : [1920, 1080];
}

function updateCoverCanvasSize() {
  const canvas = $("coverCanvas");
  const ratio = $("coverRatio").value;
  if (ratio === "9:16") [canvas.width, canvas.height] = [540, 960];
  else if (ratio === "1:1") [canvas.width, canvas.height] = [720, 720];
  else [canvas.width, canvas.height] = [960, 540];
}

async function generateCoverCandidates() {
  const title = $("coverTitle").value.trim();
  if (!title) return showToast("请先填写封面标题", true);
  const button = $("generateCoverBtn");
  button.disabled = true;
  button.querySelector("span").textContent = "豆包生成提示词中…";
  try {
    const promptDoc = await api(`/api/editor/tasks/${encodeURIComponent(taskId)}/covers/prompt`, {
      method: "POST",
      body: JSON.stringify({
        title,
        summary: $("coverSummary").value,
        requirements: $("coverRequirements").value,
        aspect_ratio: $("coverRatio").value,
        size: $("coverSize").value,
        safe_area: $("coverSafeArea").value,
      }),
    });
    state.coverPromptDoc = promptDoc;
    $("seedreamPrompt").value = promptDoc.prompt;
    button.querySelector("span").textContent = "Seedream 生图中…";
    const generated = await api(`/api/editor/tasks/${encodeURIComponent(taskId)}/covers/generate`, {
      method: "POST",
      body: JSON.stringify({ prompt_doc: promptDoc, max_images: num($("coverCount").value, 1), watermark: false }),
    });
    addCoverCandidates(generated.files || []);
    showToast(`已生成 ${generated.files?.length || 0} 张候选封面`);
  } catch (error) {
    showToast(`封面生成失败：${error.message}`, true);
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "生成候选封面";
  }
}

function addCoverCandidates(items) {
  const existing = new Set(state.coverCandidates.map((item) => item.file));
  for (const item of items) if (item.file && !existing.has(item.file)) state.coverCandidates.unshift(item);
  renderCoverCandidates();
  if (items[0]) selectCover(items[0]);
}

function renderCoverCandidates() {
  const container = $("coverCandidates");
  container.innerHTML = state.coverCandidates.length ? state.coverCandidates.map((item) => `<button class="cover-candidate ${state.selectedCover?.file === item.file ? "active" : ""}" data-file="${escapeHtml(item.file)}"><img src="${escapeHtml(item.url)}" alt="候选封面" /></button>`).join("") : '<div class="empty-state">暂无候选图</div>';
  container.querySelectorAll(".cover-candidate").forEach((button) => button.addEventListener("click", () => selectCover(state.coverCandidates.find((item) => item.file === button.dataset.file))));
}

function selectCover(item) {
  if (!item) return;
  state.selectedCover = item;
  const image = new Image();
  image.onload = () => {
    state.coverImage = image;
    drawCover();
    $("coverStatus").textContent = `${item.width || image.naturalWidth} × ${item.height || image.naturalHeight} · ${item.file}`;
  };
  image.onerror = () => showToast("候选封面预览加载失败", true);
  image.src = item.url;
  renderCoverCandidates();
}

async function uploadCoverImage(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const result = await api(`/api/editor/tasks/${encodeURIComponent(taskId)}/covers/upload`, { method: "POST", body: form });
    addCoverCandidates([result]);
    showToast("图片已加入封面编辑器");
  } catch (error) {
    showToast(`图片上传失败：${error.message}`, true);
  }
}

async function aiRestoreCover() {
  if (!state.selectedCover) return showToast("请先选择一张封面", true);
  const button = $("aiRestoreBtn");
  button.disabled = true;
  try {
    const result = await api(`/api/editor/tasks/${encodeURIComponent(taskId)}/covers/ai-restore`, {
      method: "POST",
      body: JSON.stringify({
        source_file: state.selectedCover.file,
        requirements: $("coverRequirements").value,
        size: $("coverSize").value === "1K" ? "2K" : $("coverSize").value,
      }),
    });
    addCoverCandidates(result.files || []);
    showToast("AI 高清修复完成，请与原图对比确认新闻细节");
  } catch (error) {
    showToast(`AI 高清修复失败：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

function textLayersForServer() {
  return state.textLayers.map((layer) => ({
    text: layer.text,
    font: layer.font,
    font_size: layer.font_size * (coverOutputSize()[1] / $("coverCanvas").height),
    color: layer.color,
    stroke_color: layer.stroke_color,
    stroke_width: Math.round(layer.stroke_width * (coverOutputSize()[1] / $("coverCanvas").height)),
    x: layer.x,
    y: layer.y,
    max_width: layer.max_width,
  }));
}

async function exportCover() {
  if (!state.selectedCover) return showToast("请先选择一张封面", true);
  const [width, height] = coverOutputSize();
  const button = $("exportCoverBtn");
  button.disabled = true;
  try {
    const result = await api(`/api/editor/tasks/${encodeURIComponent(taskId)}/covers/compose`, {
      method: "POST",
      body: JSON.stringify({
        source_file: state.selectedCover.file,
        width, height,
        crop: coverCrop(),
        text_layers: textLayersForServer(),
        enhance: $("coverEnhance").value,
        output_format: "png",
      }),
    });
    $("coverStatus").innerHTML = `封面已导出：<a href="${escapeHtml(result.url)}" target="_blank">${escapeHtml(result.file)}</a>`;
    showToast(`封面已导出 ${width} × ${height}`);
  } catch (error) {
    showToast(`封面导出失败：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

function drawCover() {
  const canvas = $("coverCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#10151f";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const image = state.coverImage;
  if (!image) {
    ctx.fillStyle = "#667188";
    ctx.font = "18px Microsoft YaHei, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("生成或上传图片后在这里编辑封面", canvas.width / 2, canvas.height / 2);
    return;
  }
  const crop = coverCrop();
  const sx = crop.x * image.naturalWidth;
  const sy = crop.y * image.naturalHeight;
  const sw = crop.w * image.naturalWidth;
  const sh = crop.h * image.naturalHeight;
  ctx.drawImage(image, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
  for (const layer of state.textLayers) drawCanvasTextLayer(ctx, canvas, layer);
}

function drawCanvasTextLayer(ctx, canvas, layer) {
  const font = state.fonts.find((item) => item.name === layer.font);
  const family = font?.family || '"Microsoft YaHei", sans-serif';
  ctx.save();
  ctx.font = `800 ${layer.font_size}px ${family}`;
  ctx.textAlign = "left";
  ctx.textBaseline = "top";
  ctx.lineJoin = "round";
  ctx.strokeStyle = layer.stroke_color || "#000";
  ctx.lineWidth = layer.stroke_width * 2;
  ctx.fillStyle = layer.color || "#fff";
  const lines = wrapCanvasText(ctx, layer.text || "", layer.max_width * canvas.width);
  const lineHeight = layer.font_size * 1.22;
  lines.forEach((line, index) => {
    const x = layer.x * canvas.width;
    const y = layer.y * canvas.height + index * lineHeight;
    if (layer.stroke_width > 0) ctx.strokeText(line, x, y);
    ctx.fillText(line, x, y);
  });
  ctx.restore();
}

function wrapCanvasText(ctx, text, maxWidth) {
  const lines = [];
  for (const paragraph of String(text || "").split("\n")) {
    let line = "";
    for (const char of paragraph) {
      const candidate = line + char;
      if (line && ctx.measureText(candidate).width > maxWidth) {
        lines.push(line);
        line = char;
      } else line = candidate;
    }
    if (line) lines.push(line);
  }
  return lines;
}

init();
