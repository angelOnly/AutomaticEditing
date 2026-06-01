const AI_VOICEOVER_STEPS = [
  "metadata",
  "audio_extract",
  "frame_extract",
  "chunk_build",
  "asr",
  "vision",
  "timeline",
  "video_understanding",
  "highlight_detection",
  "short_video_planning",
  "editing_script",
  "voiceover_script",
  "tts",
  "subtitles",
  "cut_plan",
  "render",
];

const HIGHLIGHT_REASSEMBLY_STEPS = [
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

let STEPS = AI_VOICEOVER_STEPS;

const STEP_LABELS = {
  metadata: "读取视频信息",
  audio_extract: "提取音频",
  frame_extract: "抽取关键帧",
  chunk_build: "切分分析片段",
  asr: "语音转文字",
  vision: "画面识别",
  timeline: "合并时间线",
  video_understanding: "理解整条新闻",
  highlight_detection: "识别高光片段",
  short_video_planning: "规划短视频",
  editing_script: "生成剪辑脚本",
  voiceover_script: "生成配音文案",
  tts: "生成 TTS 配音",
  subtitles: "生成字幕",
  cut_plan: "生成剪辑计划",
  render: "渲染粗剪成片",
  highlight_reassembly_plan: "规划高光重组",
  reassembly_cut_plan: "生成重组剪辑计划",
  reassembly_render: "渲染原声重组视频",
};

const STATUS_LABELS = {
  pending: "待运行",
  success: "成功",
  partial_success: "部分成功",
  skipped: "已跳过",
  stale: "已过期",
  failed: "失败",
  running: "运行中",
  queued: "排队中",
};

const PRODUCTION_MODE_NAMES = {
  ai_voiceover: "AI配音解说",
  highlight_reassembly: "视频重组",
};

const AUTO_REFRESH_MS = 10 * 1000;

const state = {
  selectedVideo: null,
  selectedRemoteVideo: null,
  selectedTask: null,
  manifest: null,
  activeJob: null,
  logTimer: null,
  autoRefreshTimer: null,
  autoRefreshing: false,
  contextMenu: null,
  latestDraft: null,
  drafts: [],
  remoteVideos: [],
  remotePagination: { current: 1, pageSize: 30, total: 0 },
  remoteRecordStations: [],
  remoteStationCounts: {},
  remoteEnabled: false,
  voices: [],
  defaultVoiceId: "",
  defaults: {
    default_aspect_ratio: "16:9",
    default_chunk_seconds: 60,
    default_frame_interval: 5,
    default_run_mode: "all",
    default_target_seconds: 30,
    allow_long_video_default: true,
    tts_required_by_default: true,
    allow_original_audio_evidence: false,
  },
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  const type = res.headers.get("content-type") || "";
  if (type.includes("application/json")) return res.json();
  return res.text();
}

function init() {
  fillStepSelect();
  bindEvents();
  loadConfig().then(refreshAll);
  startAutoRefresh();
  lucide.createIcons();
}

async function loadConfig() {
  try {
    const config = await api("/api/config");
    state.defaults = { ...state.defaults, ...(config.workflow || {}) };
    state.defaults.default_run_mode = normalizeDefaultRunMode(state.defaults.default_run_mode);
    state.defaults.default_target_seconds = config.short_video?.default_target_seconds ?? state.defaults.default_target_seconds;
    state.defaults.allow_long_video_default = config.short_video?.allow_long_video_default ?? state.defaults.allow_long_video_default;
    state.defaults.tts_required_by_default = config.voiceover?.tts_required_by_default ?? state.defaults.tts_required_by_default;
    state.defaults.allow_original_audio_evidence = config.voiceover?.allow_original_audio_evidence ?? state.defaults.allow_original_audio_evidence;
    state.voices = config.voices || [];
    state.defaultVoiceId = config.voiceover?.default_voice_id || "";
    const remote = config.remote_ucms || {};
    state.remoteEnabled = Boolean(remote.enabled);
    state.remoteRecordStations = remote.record_stations || [];
    state.remoteStationCounts = remote.station_counts || {};
    state.remotePagination.pageSize = remote.default_page_size || 30;
    fillRemoteStationSelect(remote.default_record_station || "");
    fillVoiceSelect();
    applyDefaultControls();
  } catch (error) {
    console.warn("配置加载失败，使用页面默认值", error);
    fillVoiceSelect();
    applyDefaultControls();
  }
}

function fillRemoteStationSelect(defaultStation) {
  const select = $("remoteRecordStation");
  if (!select) return;
  const selected = defaultStation || select.value;
  const stations = state.remoteRecordStations.length ? state.remoteRecordStations : [defaultStation].filter(Boolean);
  select.innerHTML = stations.map((station) => `<option value="${escapeAttr(station)}">${escapeHtml(remoteStationLabel(station))}</option>`).join("");
  if (selected) select.value = selected;
}

function remoteStationLabel(station) {
  const count = state.remoteStationCounts?.[station];
  return Number.isFinite(Number(count)) ? `${station}（${count}）` : station;
}

function fillVoiceSelect() {
  const select = $("voiceSelect");
  if (!select) return;
  const voices = state.voices || [];
  if (!voices.length) {
    select.innerHTML = `<option value="">默认音色</option>`;
    return;
  }

  select.innerHTML = voices.map((voice) => {
    const label = [
      voice.name || voice.id,
      voice.gender ? genderLabel(voice.gender) : "",
      voice.style ? styleLabel(voice.style) : "",
    ].filter(Boolean).join(" · ");
    return `<option value="${escapeAttr(voice.id)}">${escapeHtml(label)}</option>`;
  }).join("");

  const defaultVoice = voices.find((voice) => voice.is_default)
    || voices.find((voice) => voice.id === state.defaultVoiceId)
    || voices[0];
  if (defaultVoice) select.value = defaultVoice.id;
}

function genderLabel(value) {
  return { male: "男声", female: "女声" }[value] || value;
}

function styleLabel(value) {
  return { news: "新闻", explainer: "解说", default: "默认" }[value] || value;
}

function applyDefaultControls() {
  $("aspectRatio").value = state.defaults.default_aspect_ratio || "16:9";
  $("chunkSeconds").value = state.defaults.default_chunk_seconds || 60;
  $("frameInterval").value = state.defaults.default_frame_interval || 5;
  $("runMode").value = state.defaults.default_run_mode || "all";
  $("targetDuration").value = state.defaults.default_target_seconds || 30;
  $("allowLongVideo").checked = Boolean(state.defaults.allow_long_video_default);
  $("requireTts").checked = Boolean(state.defaults.tts_required_by_default);
  $("productionMode").value = "ai_voiceover";
  $("audioPolicy").value = "ai_voiceover";
  $("allowOriginalAudioEvidence").checked = false;
  syncFriendlyControls();
  updateSummaryControls();
  onProductionModeChange();
}

function normalizeDefaultRunMode(mode) {
  return mode && mode !== "only_analysis" ? mode : "all";
}

function bindEvents() {
  $("uploadVideo")?.addEventListener("click", () => $("videoUploadInput")?.click());
  $("videoUploadInput")?.addEventListener("change", uploadSelectedVideo);
  document.addEventListener("click", hideContextMenu);
  document.addEventListener("scroll", hideContextMenu, true);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideContextMenu();
  });
  $("refreshVideos").addEventListener("click", loadVideos);
  $("refreshRemoteVideos")?.addEventListener("click", () => loadRemoteVideos(1));
  $("remoteRecordStation")?.addEventListener("change", () => loadRemoteVideos(1));
  $("remotePrev")?.addEventListener("click", () => {
    const current = Number(state.remotePagination.current || 1);
    if (current > 1) loadRemoteVideos(current - 1);
  });
  $("remoteNext")?.addEventListener("click", () => {
    const p = state.remotePagination || {};
    const maxPage = Math.ceil((p.total || 0) / (p.pageSize || 30));
    if (!maxPage || Number(p.current || 1) < maxPage) loadRemoteVideos(Number(p.current || 1) + 1);
  });
  $("refreshTasks").addEventListener("click", loadTasks);
  $("refreshAll").addEventListener("click", refreshAll);
  $("runSelected").addEventListener("click", runSelected);
  $("rerunOne").addEventListener("click", () => rerun(false));
  $("rerunFrom").addEventListener("click", () => rerun(true));
  $("audioPolicy").addEventListener("change", () => {
    $("audioPolicy").value = "ai_voiceover";
    $("allowOriginalAudioEvidence").checked = false;
  });
  $("allowLongVideoSelect")?.addEventListener("change", () => {
    $("allowLongVideo").checked = $("allowLongVideoSelect").value === "true";
    updateSummaryControls();
  });
  $("requireTtsSelect")?.addEventListener("change", () => {
    $("requireTts").checked = $("requireTtsSelect").value === "true";
  });
  $("previewVoice")?.addEventListener("click", previewSelectedVoice);
  document.querySelectorAll(".mode-option").forEach((button) => {
    button.addEventListener("click", () => {
      $("productionMode").value = button.dataset.productionMode;
      onProductionModeChange();
    });
  });
  ["targetDuration", "aspectRatio"].forEach((id) => {
    $(id)?.addEventListener("input", updateSummaryControls);
    $(id)?.addEventListener("change", updateSummaryControls);
  });
  $("productionMode").addEventListener("change", onProductionModeChange);
  $("rerunStep").addEventListener("change", updateChunkInputState);
  $("refreshTree").addEventListener("click", loadTree);
  $("refreshLog").addEventListener("click", refreshLog);
  $("copyViewer").addEventListener("click", () => navigator.clipboard.writeText($("fileViewer").textContent));
  $("openSource").addEventListener("click", () => {
    playSourceVideo();
  });
  $("openDraft").addEventListener("click", () => {
    if (state.drafts.length) playDraft(state.drafts[0]);
    else if (state.latestDraft?.exists) playDraft(state.latestDraft);
    else alert("当前任务还没有生成粗剪视频。");
  });
  $("confirmLongVideo").addEventListener("click", confirmLongVideo);
}

function fillStepSelect() {
  updateActiveSteps();
  $("rerunStep").innerHTML = STEPS.map((s) => `<option value="${s}">${stepLabel(s)}</option>`).join("");
  updateChunkInputState();
}

function updateActiveSteps() {
  STEPS = $("productionMode")?.value === "highlight_reassembly" ? HIGHLIGHT_REASSEMBLY_STEPS : AI_VOICEOVER_STEPS;
}

function onProductionModeChange() {
  const isReassembly = $("productionMode").value === "highlight_reassembly";
  syncSuggestedTaskIdForMode();
  ["reassemblyMaxClipCountWrap"].forEach((id) => {
    $(id)?.classList.toggle("hidden", !isReassembly);
  });
  $("reassemblyOutputModeWrap")?.classList.add("hidden");
  $("reassemblySortModeWrap")?.classList.add("hidden");
  $("reassemblyOutputMode").value = "single";
  $("reassemblySortMode").value = "editorial";
  $("audioPolicy").value = isReassembly ? "original" : "ai_voiceover";
  $("requireTts").checked = !isReassembly && Boolean(state.defaults.tts_required_by_default);
  $("requireTts").closest("label")?.classList.toggle("hidden", isReassembly);
  $("allowOriginalAudioEvidence").checked = isReassembly;
  $("allowOriginalAudioEvidence").closest("label")?.classList.toggle("hidden", !isReassembly);
  $("requireTtsSelect")?.closest("label")?.classList.toggle("hidden", isReassembly);
  $("voiceSelectWrap")?.classList.toggle("hidden", isReassembly);
  $("previewVoice")?.classList.toggle("hidden", isReassembly);
  if (isReassembly && Number($("targetDuration").value || 0) < 60) $("targetDuration").value = 90;
  if (!isReassembly && Number($("targetDuration").value || 0) > 60) $("targetDuration").value = state.defaults.default_target_seconds || 30;
  syncFriendlyControls();
  updateSummaryControls();
  fillStepSelect();
  renderManifest();
}

async function previewSelectedVoice() {
  const voiceId = $("voiceSelect")?.value;
  if (!voiceId) {
    alert("请先选择一个音色。");
    return;
  }

  const voice = (state.voices || []).find((item) => item.id === voiceId);
  const url = voice?.preview_url || `/api/voices/${encodeURIComponent(voiceId)}/preview`;
  const audio = $("voicePreviewAudio");
  audio.src = url;
  audio.load();

  try {
    await audio.play();
  } catch (error) {
    alert(`试听失败：${cleanError(error)}`);
  }
}

function syncFriendlyControls() {
  if ($("allowLongVideoSelect")) $("allowLongVideoSelect").value = $("allowLongVideo").checked ? "true" : "false";
  if ($("requireTtsSelect")) $("requireTtsSelect").value = $("requireTts").checked ? "true" : "false";
  syncModePicker();
}

function syncModePicker() {
  const value = $("productionMode")?.value || "ai_voiceover";
  document.querySelectorAll(".mode-option").forEach((button) => {
    button.classList.toggle("active", button.dataset.productionMode === value);
  });
}

function updateSummaryControls() {
  if ($("metricAspect")) $("metricAspect").textContent = $("aspectRatio")?.value || state.defaults.default_aspect_ratio || "16:9";
}

function updateChunkInputState() {
  const input = $("chunkInput");
  const enabled = $("rerunStep").value === "vision";
  input.disabled = !enabled;
  input.placeholder = enabled ? "可选：chunk_0007" : "仅重跑 vision 时可填写";
  if (!enabled) input.value = "";
}

async function refreshAll() {
  return refreshWorkspace();
}

async function refreshWorkspace(options = {}) {
  const {
    refreshDrafts = true,
    restoreControls = true,
    updatePreview = true,
    refreshFiles = false,
    refreshRemote = true,
  } = options;
  await Promise.all([
    loadVideos().catch((error) => console.warn("本地视频加载失败", error)),
    loadTasks().catch((error) => console.warn("任务加载失败", error)),
    state.remoteEnabled && refreshRemote ? loadRemoteVideos(state.remotePagination.current || 1).catch(renderRemoteError) : Promise.resolve(),
  ]);
  if (state.selectedTask) {
    await loadManifest(state.selectedTask, { refreshDrafts, restoreControls, updatePreview });
    if (refreshFiles) await loadTree().catch(() => {});
  }
  lucide.createIcons();
}

function startAutoRefresh() {
  if (state.autoRefreshTimer) clearInterval(state.autoRefreshTimer);
  state.autoRefreshTimer = setInterval(autoRefreshWorkspace, AUTO_REFRESH_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) autoRefreshWorkspace();
  });
}

async function autoRefreshWorkspace() {
  if (document.hidden || state.autoRefreshing) return;
  state.autoRefreshing = true;
  try {
    await refreshWorkspace({
      refreshDrafts: true,
      restoreControls: false,
      updatePreview: false,
      refreshFiles: true,
      refreshRemote: false,
    });
  } catch (error) {
    console.warn("Auto refresh failed", error);
  } finally {
    state.autoRefreshing = false;
  }
}

async function loadVideos() {
  const videos = await api("/api/videos");
  const box = $("videoList");
  box.innerHTML = videos.length ? "" : `<div class="list-item"><strong>没有找到视频</strong><span>请放入 videos 目录</span></div>`;
  videos.forEach((video) => {
    const item = document.createElement("button");
    item.className = `list-item ${state.selectedVideo?.path === video.path ? "active" : ""}`;
    item.type = "button";
    item.innerHTML = `<strong>${escapeHtml(video.name)}</strong><span>${video.size_mb} MB · ${video.updated_at}</span>`;
    item.addEventListener("click", () => {
      state.selectedVideo = video;
      state.selectedRemoteVideo = null;
      state.selectedTask = null;
      state.manifest = null;
      applyDefaultControls();
      $("taskIdInput").value = suggestedTaskId(video.name, $("productionMode").value);
      $("activeTitle").textContent = video.name;
      updateTaskSubtitle();
      clearTaskPanels();
      $("videoPreview").src = `/api/videos/preview?path=${encodeURIComponent(video.path)}`;
      $("videoPreview").load();
      loadVideos();
      renderRemoteVideos();
      loadTasks();
    });
    attachContextDelete(item, () => deleteVideo(video));
    box.appendChild(item);
  });
  lucide.createIcons();
}

async function loadRemoteVideos(page = 1) {
  const box = $("remoteVideoList");
  if (!box) return;
  if (!state.remoteEnabled) {
    box.innerHTML = `<div class="list-item"><strong>远程素材未启用</strong><span>请检查 config.toml 的 remote_ucms 配置</span></div>`;
    updateRemotePager();
    return;
  }
  const station = $("remoteRecordStation")?.value || "";
  box.innerHTML = `<div class="list-item"><strong>正在加载远程素材...</strong><span>${escapeHtml(station || "-")}</span></div>`;
  const url = `/api/remote-videos?record_station=${encodeURIComponent(station)}&current=${page}&page_size=${state.remotePagination.pageSize || 30}`;
  const data = await api(url);
  state.remoteVideos = data.items || [];
  state.remotePagination = data.pagination || { current: page, pageSize: 30, total: state.remoteVideos.length };
  state.remoteStationCounts = { ...state.remoteStationCounts, ...(data.station_counts || {}) };
  if (station && state.remotePagination.total !== undefined) {
    state.remoteStationCounts[station] = Number(state.remotePagination.total || 0);
  }
  fillRemoteStationSelect(station);
  renderRemoteVideos();
}

function renderRemoteVideos() {
  const box = $("remoteVideoList");
  if (!box) return;
  updateRemotePager();
  box.innerHTML = state.remoteVideos.length
    ? ""
    : `<div class="list-item"><strong>没有远程素材</strong><span>请更换信号源或稍后刷新</span></div>`;
  state.remoteVideos.forEach((video) => {
    const item = document.createElement("button");
    item.className = `list-item ${state.selectedRemoteVideo?.remote_id === video.remote_id ? "active" : ""}`;
    item.type = "button";
    const title = video.display_name || video.name || "远程素材";
    item.innerHTML = `<strong>${escapeHtml(title)}</strong><span>${escapeHtml(video.record_station || "-")} · ${escapeHtml(video.duration_text || "-")} · ${escapeHtml(video.create_time || "-")}</span>`;
    item.addEventListener("click", () => selectRemoteVideo(video));
    box.appendChild(item);
  });
  lucide.createIcons();
}

function updateRemotePager() {
  const p = state.remotePagination || {};
  const maxPage = Math.max(1, Math.ceil((p.total || 0) / (p.pageSize || 30)));
  if ($("remotePageInfo")) $("remotePageInfo").textContent = `${p.current || 1} / ${maxPage} · 共 ${p.total || 0}`;
}

function renderRemoteError(error) {
  const box = $("remoteVideoList");
  if (!box) return;
  updateRemotePager();
  box.innerHTML = `<div class="list-item"><strong>远程素材加载失败</strong><span>${escapeHtml(cleanError(error))}</span></div>`;
}

function selectRemoteVideo(video) {
  state.selectedRemoteVideo = video;
  state.selectedVideo = null;
  state.selectedTask = null;
  state.manifest = null;
  applyDefaultControls();
  $("taskIdInput").value = suggestedTaskId(video.name || "remote_video", $("productionMode").value);
  $("activeTitle").textContent = video.display_name || video.name || "远程素材";
  updateTaskSubtitle();
  clearTaskPanels();
  if (video.preview_url) {
    $("videoPreview").src = video.preview_url;
    $("videoPreview").load();
    $("fileViewer").textContent = "正在预览远程原片。";
  } else {
    $("fileViewer").textContent = "该远程素材没有可预览地址。";
  }
  loadVideos();
  loadTasks();
  renderRemoteVideos();
}

async function loadTasks() {
  const tasks = await api("/api/tasks");
  const box = $("taskList");
  box.innerHTML = tasks.length ? "" : `<div class="list-item"><strong>暂无任务</strong><span>选择素材后开始分析</span></div>`;
  tasks.forEach((task) => {
    const item = document.createElement("button");
    const jobClass = task.job_status ? ` job-${task.job_status}` : "";
    item.className = `list-item${jobClass} ${state.selectedTask === task.task_id ? "active" : ""}`;
    item.type = "button";
    const jobBadge = task.job_status ? `<em class="task-job-badge ${escapeAttr(task.job_status)}">${escapeHtml(jobStatusLabel(task.job_status))}</em>` : "";
    item.innerHTML = `<strong>${escapeHtml(displayTaskId(task.task_id))}</strong><span>${task.done_steps}/${task.step_count} 完成 · ${task.failed_steps} 失败 · ${task.updated_at || "-"}</span>${jobBadge}`;
    item.addEventListener("click", async () => {
      state.selectedTask = task.task_id;
      state.selectedVideo = null;
      state.selectedRemoteVideo = null;
      $("taskIdInput").value = task.task_id;
      $("activeTitle").textContent = displayTaskId(task.task_id);
      updateTaskSubtitle(task.task_id);
      await loadManifest(task.task_id);
      await loadTree();
      loadVideos();
      renderRemoteVideos();
      loadTasks();
    });
    attachContextDelete(item, () => deleteTask(task));
    box.appendChild(item);
  });
  lucide.createIcons();
}

async function uploadSelectedVideo(event) {
  const input = event.target;
  const file = input.files?.[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  const uploadButton = $("uploadVideo");
  uploadButton.disabled = true;
  try {
    const res = await fetch("/api/videos/upload", { method: "POST", body: form });
    if (!res.ok) throw new Error(await res.text() || res.statusText);
    const video = await res.json();
    state.selectedVideo = video;
    state.selectedRemoteVideo = null;
    state.selectedTask = null;
    state.manifest = null;
    applyDefaultControls();
    $("taskIdInput").value = suggestedTaskId(video.name, $("productionMode").value);
    $("activeTitle").textContent = video.name;
    updateTaskSubtitle();
    clearTaskPanels();
    $("videoPreview").src = `/api/videos/preview?path=${encodeURIComponent(video.path)}`;
    $("videoPreview").load();
    await refreshWorkspace({ updatePreview: false });
  } catch (error) {
    alert(`上传失败：${cleanError(error)}`);
  } finally {
    input.value = "";
    uploadButton.disabled = false;
    lucide.createIcons();
  }
}

async function deleteVideo(video) {
  if (!confirm(`确定删除视频素材？\n${video.name}`)) return;
  await runListDelete(async () => {
    await api(`/api/videos?path=${encodeURIComponent(video.path)}`, { method: "DELETE" });
    if (state.selectedVideo?.path === video.path) {
      state.selectedVideo = null;
      clearTaskPanels();
      $("activeTitle").textContent = "请选择视频或任务";
    }
    await loadVideos();
  });
}

async function deleteTask(task) {
  if (!confirm(`确定删除任务及其全部输出？\n${displayTaskId(task.task_id)}`)) return;
  await runListDelete(async () => {
    await api(`/api/tasks/${encodeURIComponent(task.task_id)}`, { method: "DELETE" });
    if (state.selectedTask === task.task_id) {
      state.selectedTask = null;
      state.manifest = null;
      clearTaskPanels();
      $("activeTitle").textContent = "请选择视频或任务";
    }
    await loadTasks();
  });
}

async function runListDelete(action) {
  try {
    await action();
  } catch (error) {
    alert(`删除失败：${cleanError(error)}`);
  }
}

function attachContextDelete(item, onDelete) {
  item.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    event.stopPropagation();
    showContextMenu(event.clientX, event.clientY, onDelete);
  });
}

function showContextMenu(x, y, onDelete) {
  hideContextMenu();
  const menu = document.createElement("div");
  menu.className = "context-menu";
  menu.innerHTML = `<button type="button" class="context-menu-item danger"><i data-lucide="trash-2"></i><span>删除</span></button>`;
  menu.querySelector("button").addEventListener("click", async (event) => {
    event.stopPropagation();
    hideContextMenu();
    await onDelete();
  });
  document.body.appendChild(menu);
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(x, window.innerWidth - rect.width - 8)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - rect.height - 8)}px`;
  state.contextMenu = menu;
  lucide.createIcons();
}

function hideContextMenu() {
  state.contextMenu?.remove();
  state.contextMenu = null;
}

async function loadManifest(taskId, options = {}) {
  const { refreshDrafts = true, restoreControls = true, updatePreview = true, adoptJob = true } = options;
  const manifest = await api(`/api/tasks/${taskId}/manifest`);
  state.manifest = manifest;
  if (restoreControls) restoreRunControls(manifest);
  renderManifest();
  if (refreshDrafts) await loadLatestDraft(taskId, { updatePreview });
  if (adoptJob) await adoptRunningJob(taskId);
}

async function loadLatestDraft(taskId, options = {}) {
  const { updatePreview = true } = options;
  const drafts = await api(`/api/tasks/${taskId}/drafts`);
  state.drafts = drafts;
  renderDraftList();
  state.latestDraft = drafts[0] || { exists: false, file: "", url: "" };
  if (!updatePreview) return;
  if (drafts.length) {
    playDraft(drafts[0]);
  } else {
    playSourceVideo();
    $("fileViewer").textContent = "当前任务还没有生成粗剪视频，正在预览源视频。";
  }
}

function playSourceVideo() {
  if (state.selectedTask) {
    $("videoPreview").src = `/api/tasks/${state.selectedTask}/preview`;
    $("videoPreview").load();
    $("fileViewer").textContent = "正在预览原片。";
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
  document.querySelectorAll(".draft-item").forEach((item) => item.classList.remove("active"));
}

function renderDraftList() {
  const box = $("draftList");
  if (!state.drafts.length) {
    box.textContent = "当前任务还没有生成粗剪视频。";
    return;
  }
  box.innerHTML = state.drafts
    .map(
      (draft, index) => `<button class="draft-item ${index === 0 ? "active" : ""}" data-index="${index}">
        <strong>${escapeHtml(draft.file)}</strong>
        <span>${draft.size_mb} MB</span>
      </button>`,
    )
    .join("");
  document.querySelectorAll(".draft-item").forEach((item) => {
    item.addEventListener("click", () => playDraft(state.drafts[Number(item.dataset.index)]));
  });
}

function playDraft(draft) {
  if (!draft?.url) return;
  $("videoPreview").src = draft.url;
  $("videoPreview").load();
  $("fileViewer").textContent = `正在预览粗剪成片：\n${draft.file}`;
  document.querySelectorAll(".draft-item").forEach((item) => {
    const current = state.drafts[Number(item.dataset.index)];
    item.classList.toggle("active", current?.file === draft.file);
  });
}

function restoreRunControls(manifest) {
  applyDefaultControls();
  const opts = manifest.last_run_options || manifest.last_web_run_options || {};
  if (opts.aspect_ratio) $("aspectRatio").value = opts.aspect_ratio;
  if (opts.chunk_seconds) $("chunkSeconds").value = opts.chunk_seconds;
  if (opts.frame_interval) $("frameInterval").value = opts.frame_interval;
  if (opts.target_duration_seconds) $("targetDuration").value = opts.target_duration_seconds;
  if (typeof opts.allow_long_video === "boolean") $("allowLongVideo").checked = opts.allow_long_video;
  if (typeof opts.require_tts === "boolean") $("requireTts").checked = opts.require_tts;
  $("productionMode").value = opts.production_mode || "ai_voiceover";
  $("audioPolicy").value = opts.production_mode === "highlight_reassembly" ? "original" : "ai_voiceover";
  $("allowOriginalAudioEvidence").checked = opts.production_mode === "highlight_reassembly";
  $("reassemblyOutputMode").value = "single";
  $("reassemblySortMode").value = "editorial";
  if (opts.reassembly_max_clip_count) $("reassemblyMaxClipCount").value = opts.reassembly_max_clip_count;
  if (opts.voice_id && $("voiceSelect")) $("voiceSelect").value = opts.voice_id;
  onProductionModeChange();
  if (opts.voice_id && $("voiceSelect")) $("voiceSelect").value = opts.voice_id;
  if (typeof opts.require_tts === "boolean" && $("productionMode").value !== "highlight_reassembly") $("requireTts").checked = opts.require_tts;
  $("runMode").value = "all";
  syncFriendlyControls();
  updateSummaryControls();
}

function renderManifest() {
  const manifest = state.manifest;
  if (!manifest) return;
  updateActiveSteps();
  const steps = manifest.steps || {};
  const stepDurations = buildStepDurationMap(manifest, STEPS);
  const rows = STEPS.map((step, index) => {
    const item = steps[step] || {};
    const status = item.status || "pending";
    const output = item.output || "";
    const note = stepNote(step, item);
    const duration = stepDurationText(step, item, stepDurations);
    const desc = [duration, output || note || "等待流程推进"].filter(Boolean).join(" · ");
    const marker = ["success", "partial_success"].includes(status) ? "✓" : index + 1;
    const outputAttr = output ? ` data-path="${escapeAttr(output)}"` : "";
    return `<div class="step-card ${escapeAttr(status)}"${outputAttr}>
      <div class="step-index">${marker}</div>
      <div>
        <div class="step-title" title="${escapeAttr(step)}">${escapeHtml(stepLabel(step))}</div>
        <div class="step-desc" title="${escapeAttr(desc)}">${escapeHtml(desc)}</div>
      </div>
      <span class="status ${status}" title="${escapeAttr(status)}">${escapeHtml(statusLabel(status))}</span>
    </div>`;
  }).join("");
  $("stepTable").innerHTML = rows;
  document.querySelectorAll(".step-card[data-path]").forEach((card) => {
    card.addEventListener("click", () => openTaskFile(card.dataset.path));
  });
  const visibleEntries = STEPS.map((step) => [step, steps[step] || {}]);
  const values = visibleEntries.map(([, item]) => item);
  const done = values.filter((x) => ["success", "partial_success", "skipped"].includes(x.status)).length;
  const completed = values.filter((x) => ["success", "partial_success"].includes(x.status)).length;
  const skipped = values.filter((x) => x.status === "skipped").length;
  const skippedNames = visibleEntries
    .filter(([, x]) => x.status === "skipped")
    .map(([name]) => name);
  const skippedDetails = visibleEntries
    .filter(([, x]) => x.status === "skipped")
    .map(([name, item]) => `${stepLabel(name)}：${stepNote(name, item) || "已跳过"}`);
  const failed = values.filter((x) => x.status === "failed").length;
  const stale = values.filter((x) => x.status === "stale").length;
  const renderStatus = steps.render?.status;
  const manifestStatus = manifest.status;
  const currentEntry =
    visibleEntries.find(([, item]) => item.status === "running") ||
    visibleEntries.find(([, item]) => ["pending", "stale"].includes(item.status || "pending")) ||
    visibleEntries[visibleEntries.length - 1];
  renderActionRequired(manifest);
  if (!state.activeJob) updateJobMessage(manifest.user_message || "");
  updateProgress(done, STEPS.length, completed, skipped, failed);
  $("metricSteps").textContent = `${done}/${STEPS.length}（成功 ${completed}，跳过 ${skipped}）`;
  if ($("metricCurrentStep")) $("metricCurrentStep").textContent = currentEntry ? stepLabel(currentEntry[0]) : "-";
  $("metricIssues").textContent = skippedDetails.length ? `${failed} / ${stale}，跳过：${skippedDetails.join("；")}` : `${failed} / ${stale}`;
  if (!state.activeJob) {
    if (manifestStatus === "running") {
      $("metricJob").textContent = "Running";
      return;
    }
    if (failed) $("metricJob").textContent = "有失败步骤";
    else if (renderStatus === "skipped") $("metricJob").textContent = "已完成，未生成粗剪";
    else if (done >= STEPS.length) $("metricJob").textContent = "已完成";
  }
}

function updateProgress(done, total, completed = 0, skipped = 0, failed = 0) {
  if (!$("progressLabel") || !$("progressPercent") || !$("progressFill")) return;
  const safeTotal = Math.max(Number(total) || 0, 0);
  const safeDone = Math.min(Math.max(Number(done) || 0, 0), safeTotal);
  const percent = safeTotal ? Math.round((safeDone / safeTotal) * 100) : 0;
  $("progressLabel").textContent = safeTotal ? `${safeDone} / ${safeTotal}` : "-";
  if ($("progressBreakdown")) $("progressBreakdown").textContent = `成功 ${completed} · 跳过 ${skipped} · 失败 ${failed}`;
  $("progressPercent").textContent = `${percent}%`;
  $("progressFill").style.width = `${percent}%`;
  if ($("progressRing")) $("progressRing").style.background = `conic-gradient(var(--brand) 0 ${percent * 3.6}deg, var(--line) ${percent * 3.6}deg 360deg)`;
  const track = document.querySelector(".progress-track");
  if (track) track.setAttribute("aria-valuenow", String(percent));
}

function stepLabel(step) {
  return STEP_LABELS[step] || step;
}

function statusLabel(status) {
  return STATUS_LABELS[status] || status || "-";
}

function jobStatusLabel(status) {
  if (status === "pending" || status === "queued") return "排队中";
  return statusLabel(status);
}

function stepNote(step, item = {}) {
  if (!item || item.status === "pending") return "";
  if (item.status === "skipped") return skippedReasonText(item.reason, step);
  return item.error || item.reason || "";
}

function buildStepDurationMap(manifest, orderedSteps) {
  const steps = manifest.steps || {};
  const result = {};
  const runStart = parseTime(
    manifest.last_run_options?.updated_at ||
      manifest.last_web_run_options?.updated_at ||
      manifest.created_at,
  );
  orderedSteps.forEach((step, index) => {
    const item = steps[step] || {};
    const explicit = explicitDurationSeconds(item);
    if (explicit !== null) {
      result[step] = explicit;
      return;
    }
    const finishedAt = parseTime(item.updated_at || item.finished_at || item.completed_at);
    if (!finishedAt || !["success", "partial_success", "failed", "skipped"].includes(item.status)) return;
    const dependencyTimes = (item.depends_on || [])
      .map((name) => parseTime(steps[name]?.updated_at || steps[name]?.finished_at || steps[name]?.completed_at))
      .filter(Boolean);
    const previousStep = orderedSteps[index - 1];
    const previousFinishedAt = previousStep
      ? parseTime(steps[previousStep]?.updated_at || steps[previousStep]?.finished_at || steps[previousStep]?.completed_at)
      : null;
    const startedAt =
      parseTime(item.started_at || item.created_at) ||
      (dependencyTimes.length ? new Date(Math.max(...dependencyTimes.map((date) => date.getTime()))) : null) ||
      previousFinishedAt ||
      runStart;
    if (!startedAt) return;
    result[step] = Math.max(0, (finishedAt.getTime() - startedAt.getTime()) / 1000);
  });
  return result;
}

function explicitDurationSeconds(item = {}) {
  const keys = ["elapsed_seconds", "duration_seconds", "runtime_seconds", "cost_seconds"];
  for (const key of keys) {
    const value = Number(item[key]);
    if (Number.isFinite(value) && value >= 0) return value;
  }
  const ms = Number(item.elapsed_ms || item.duration_ms || item.runtime_ms);
  if (Number.isFinite(ms) && ms >= 0) return ms / 1000;
  return null;
}

function stepDurationText(step, item, stepDurations) {
  const seconds = stepDurations[step];
  if (!Number.isFinite(seconds)) return "";
  return `耗时 ${formatDuration(seconds)}`;
}

function formatDuration(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  if (value < 1) return "<1秒";
  if (value < 60) return `${Math.round(value)}秒`;
  const minutes = Math.floor(value / 60);
  const rest = Math.round(value % 60);
  if (minutes < 60) return rest ? `${minutes}分${rest}秒` : `${minutes}分`;
  const hours = Math.floor(minutes / 60);
  const minuteRest = minutes % 60;
  return minuteRest ? `${hours}小时${minuteRest}分` : `${hours}小时`;
}

function parseTime(value) {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function skippedReasonText(reason, step) {
  const labels = {
    only_analysis: "当前运行范围是“分步 1：只做内容分析”，后续生成步骤暂不执行",
    skip_tts: "本次运行设置了跳过 TTS",
    skip_render: "本次运行设置了跳过粗剪渲染",
    "audio_policy=original or skip_tts": "当前音频策略使用原声，或设置了跳过 TTS",
  };
  if (reason) return labels[reason] || reason;
  if (["tts", "subtitles", "cut_plan", "render"].includes(step)) return "本次运行范围未包含该后续生成步骤";
  return "已跳过";
}

async function adoptRunningJob(taskId) {
  if (!taskId || state.activeJob) return;
  const jobs = await api("/api/jobs").catch(() => []);
  const job = jobs.find((item) => item.task_id === taskId && ["pending", "running"].includes(item.status));
  if (!job) return;
  state.activeJob = job.job_id;
  setRunControlsBusy(false);
  $("metricJob").textContent = jobStatusLabel(job.status);
  startJobPolling();
  await refreshLog().catch(() => {});
}

function renderActionRequired(manifest) {
  const banner = $("actionRequiredBanner");
  const action = manifest.action_required;
  if (!action || action.type !== "confirm_long_video") {
    banner.classList.add("hidden");
    return;
  }
  banner.classList.remove("hidden");
  const seconds = Number(action.estimated_duration_seconds || 0).toFixed(1);
  $("actionRequiredTitle").textContent = `长版确认：预计 ${seconds} 秒`;
  $("actionRequiredText").textContent = action.reason || "当前方案超过 60 秒，请审核分析结果、剪辑脚本和 AI 文案后确认是否继续。";
  const files = action.review_files || {};
  const labels = {
    video_analysis: "视频分析",
    short_video_plan: "短视频规划",
    editing_script: "剪辑脚本",
    voiceover_script: "AI 文案",
  };
  $("actionRequiredFiles").innerHTML = Object.entries(files)
    .map(([key, path]) => `<button class="review-link" data-path="${escapeAttr(path)}">${labels[key] || key}</button>`)
    .join("");
  document.querySelectorAll(".review-link").forEach((button) => {
    button.addEventListener("click", () => openTaskFile(button.dataset.path));
  });
  lucide.createIcons();
}

async function confirmLongVideo() {
  if (!state.selectedTask) return;
  const action = state.manifest?.action_required || {};
  const req = baseRunRequest();
  req.task_id = state.selectedTask;
  req.allow_long_video = true;
  req.only_analysis = false;
  req.skip_render = false;
  req.rerun_from = action.continue_from_step || "tts";
  const job = await api("/api/run", { method: "POST", body: JSON.stringify(req) });
  afterJobStarted(job);
}

async function loadTree() {
  if (!state.selectedTask) return;
  const files = await api(`/api/tasks/${state.selectedTask}/tree`);
  const important = files.filter((f) =>
    /manifest\.json|merged_timeline\.json|candidate_clips\.json|short_video_plan\.json|editing_script\.json|voiceover_script\.json|review_report\.json|highlight_reassembly_plan\.json|reassembly_cut_plan\.json|cut_plan\.json|subtitle.*\.srt|tts_outputs\.json|render_outputs\.json|reassembly_render_outputs\.json|step_status\.json|\.log$|\.mp4$/i.test(f.path) &&
      !f.path.includes("/_clips/"),
  );
  $("fileList").innerHTML = important
    .map((f) => `<div class="file-row" data-path="${escapeAttr(f.path)}"><span>${escapeHtml(f.path)}</span><span>${f.size_kb} KB</span></div>`)
    .join("");
  document.querySelectorAll(".file-row").forEach((row) => row.addEventListener("click", () => openTaskFile(row.dataset.path)));
}

async function openTaskFile(path) {
  if (!state.selectedTask || !path) return;
  const url = `/api/tasks/${state.selectedTask}/file?path=${encodeURIComponent(path)}`;
  if (/\.(mp4|mov|mkv|wav|mp3|jpg|jpeg|png)$/i.test(path)) {
    if (/\.(mp4|mov|mkv)$/i.test(path)) {
      $("videoPreview").src = url;
      $("videoPreview").load();
      $("fileViewer").textContent = `正在预览视频：\n${path}`;
    } else {
      window.open(url, "_blank");
    }
    return;
  }
  const data = await api(url);
  $("fileViewer").textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2);
}

async function runSelected() {
  const taskId = $("taskIdInput").value.trim();
  const req = baseRunRequest();
  const mode = $("runMode").value;
  req.task_id = taskId || null;
  if (mode === "render_only") {
    const existingTaskId = state.selectedTask || taskId;
    if (!existingTaskId) {
      alert("继续生成粗剪视频需要先选择已有任务，因为它依赖已经生成的 cut_plan。");
      return;
    }
    const manifest = await api(`/api/tasks/${existingTaskId}/manifest`).catch(() => null);
    const cutStepName = req.production_mode === "highlight_reassembly" ? "reassembly_cut_plan" : "cut_plan";
    const cutPlan = manifest?.steps?.[cutStepName] || {};
    if (!["success", "partial_success"].includes(cutPlan.status)) {
      alert(`当前任务还不能生成粗剪视频：${cutStepName} 状态是 ${cutPlan.status || "missing"}。请先运行到 ${cutStepName} 成功。`);
      return;
    }
    req.task_id = existingTaskId;
    req.input_video = null;
    req.rerun_from = cutStepName;
    state.selectedTask = existingTaskId;
    state.selectedVideo = null;
    state.selectedRemoteVideo = null;
  } else if (state.selectedVideo) {
    req.input_video = state.selectedVideo.path;
    req.remote_video = null;
    req.task_id = null;
  } else if (state.selectedRemoteVideo) {
    req.input_video = null;
    req.remote_video = state.selectedRemoteVideo;
    req.remote_video_id = state.selectedRemoteVideo.remote_id || String(state.selectedRemoteVideo.id || "");
    req.remote_record_station = state.selectedRemoteVideo.record_station || $("remoteRecordStation")?.value || "";
    req.task_id = null;
  } else if (state.selectedTask) {
    req.task_id = state.selectedTask;
  } else {
    alert("请选择一个本地视频、远程素材或已有任务。");
    return;
  }
  const job = await api("/api/run", { method: "POST", body: JSON.stringify(req) });
  afterJobStarted(job);
}
async function rerun(fromStep) {
  if (!state.selectedTask) {
    alert("请先选择一个已有任务。");
    return;
  }
  const step = $("rerunStep").value;
  const req = baseRunRequest();
  req.task_id = state.selectedTask;
  if (await selectedTaskHasActiveJob(state.selectedTask)) {
    alert("当前任务已有运行中或排队中的 job，不能对同一个任务并发重跑。");
    return;
  }
  if (fromStep) req.rerun_from = step;
  else req.rerun = step;
  if (fromStep || step === "render") {
    req.only_analysis = false;
    req.skip_render = false;
  }
  const chunk = $("chunkInput").value.trim();
  if (chunk && step === "vision") req.chunk = chunk;
  const job = await api("/api/run", { method: "POST", body: JSON.stringify(req) });
  afterJobStarted(job);
}

function baseRunRequest() {
  const mode = $("runMode").value;
  const targetDuration = Number($("targetDuration").value || state.defaults.default_target_seconds || 30);
  const allowLongVideo = $("allowLongVideo").checked;
  const productionMode = $("productionMode").value;
  return {
    production_mode: productionMode,
    reassembly_output_mode: $("reassemblyOutputMode").value,
    reassembly_sort_mode: $("reassemblySortMode").value,
    reassembly_target_seconds: productionMode === "highlight_reassembly" ? targetDuration : null,
    reassembly_max_clip_count: Number($("reassemblyMaxClipCount").value || 8),
    chunk_seconds: Number($("chunkSeconds").value || state.defaults.default_chunk_seconds || 60),
    frame_interval: Number($("frameInterval").value || state.defaults.default_frame_interval || 5),
    aspect_ratio: $("aspectRatio").value,
    target_duration_seconds: targetDuration,
    target_duration_mode: allowLongVideo && targetDuration === 30 ? "auto_within_60" : "fixed",
    allow_long_video: allowLongVideo,
    require_tts: productionMode === "highlight_reassembly" ? false : $("requireTts").checked,
    voice_id: productionMode === "highlight_reassembly" ? null : ($("voiceSelect")?.value || null),
    audio_policy: productionMode === "highlight_reassembly" ? "original" : "ai_voiceover",
    allow_original_audio_evidence: productionMode === "highlight_reassembly",
    only_analysis: mode === "only_analysis",
    skip_tts: productionMode === "highlight_reassembly",
    skip_render: mode === "skip_render",
    mode: "normal",
    client_id: getClientId(),
  };
}

function afterJobStarted(job) {
  state.activeJob = job.job_id || null;
  state.selectedTask = job.task_id;
  setRunControlsBusy(false);
  $("taskIdInput").value = job.task_id;
  $("activeTitle").textContent = displayTaskId(job.task_id);
  updateTaskSubtitle(job.task_id);
  $("metricJob").textContent = jobStatusLabel(job.status || "pending");
  updateJobMessage(job.message || "");
  if (job.deduplicated) {
    alert(job.message || "该任务正在执行，已切换到现有任务。");
    $("logViewer").textContent = job.message || "已切换到现有任务。";
  } else {
    updateProgress(0, STEPS.length, 0, 0, 0);
    if ($("metricCurrentStep")) $("metricCurrentStep").textContent = stepLabel(STEPS[0]);
    $("logViewer").textContent = "任务已提交，等待日志输出...";
  }
  if (state.activeJob) startJobPolling();
  refreshActiveTaskSnapshot(job.task_id);
  if (state.activeJob) refreshJobAndLog();
}

function startJobPolling() {
  if (state.logTimer) clearInterval(state.logTimer);
  state.logTimer = setInterval(refreshJobAndLog, 2500);
}

function setRunControlsBusy(isBusy) {
  ["runSelected", "rerunOne", "rerunFrom"].forEach((id) => {
    const button = $(id);
    if (button) button.disabled = false;
  });
}

async function refreshJobAndLog() {
  if (!state.activeJob) return;
  let job;
  try {
    job = await api(`/api/jobs/${state.activeJob}`);
  } catch (error) {
    state.activeJob = null;
    setRunControlsBusy(false);
    $("metricJob").textContent = "空闲";
    updateJobMessage("上一次运行状态已失效，可能是服务重启后浏览器保留了旧任务锁。现在可以重新启动任务。");
    if (state.logTimer) {
      clearInterval(state.logTimer);
      state.logTimer = null;
    }
    return;
  }
  $("metricJob").textContent = jobStatusLabel(job.status);
  updateJobMessage(job.user_message || "");
  await refreshActiveTaskSnapshot(job.task_id);
  await refreshLog();
  if (!["pending", "running"].includes(job.status)) {
    clearInterval(state.logTimer);
    state.logTimer = null;
    state.activeJob = null;
    setRunControlsBusy(false);
    await loadTasks();
    await loadManifest(job.task_id).catch(() => {});
    await loadTree().catch(() => {});
  }
}

async function refreshActiveTaskSnapshot(taskId) {
  if (!taskId) return;
  await loadTasks().catch(() => {});
  if (state.selectedTask === taskId) {
    await loadManifest(taskId, {
      refreshDrafts: false,
      restoreControls: false,
      updatePreview: false,
      adoptJob: false,
    }).catch(() => {});
  }
}

function updateJobMessage(message) {
  const box = $("jobMessage");
  if (!box) return;
  const value = String(message || "").trim();
  if (!value) {
    box.classList.add("hidden");
    box.textContent = "";
    return;
  }
  box.classList.remove("hidden");
  box.textContent = value;
}

async function refreshLog() {
  if (!state.activeJob) {
    $("logViewer").textContent = "暂无运行日志。";
    return;
  }
  const text = await api(`/api/jobs/${state.activeJob}/log`);
  $("logViewer").textContent = text || "日志暂未写入。";
  $("logViewer").scrollTop = $("logViewer").scrollHeight;
}

async function selectedTaskHasActiveJob(taskId) {
  if (!taskId) return false;
  const jobs = await api("/api/jobs").catch(() => []);
  return jobs.some((job) => job.task_id === taskId && ["pending", "running"].includes(job.status));
}

function getClientId() {
  let id = localStorage.getItem("phoenix_client_id");
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem("phoenix_client_id", id);
  }
  return id;
}

function clearTaskPanels() {
  if (state.logTimer) clearInterval(state.logTimer);
  state.logTimer = null;
  state.activeJob = null;
  $("stepTable").innerHTML = "";
  $("fileList").innerHTML = "";
  $("fileViewer").textContent = "选择左侧任务或文件后显示内容。";
  $("videoPreview").removeAttribute("src");
  state.latestDraft = null;
  state.drafts = [];
  $("draftList").textContent = "当前任务还没有生成粗剪视频。";
  updateProgress(0, 0);
  $("metricSteps").textContent = "-";
  $("metricIssues").textContent = "-";
  $("metricJob").textContent = "空闲";
  updateJobMessage("");
  $("actionRequiredBanner").classList.add("hidden");
  updateTaskSubtitle();
}

function syncSuggestedTaskIdForMode() {
  const selectedName = state.selectedVideo?.name || state.selectedRemoteVideo?.name;
  if (!selectedName || state.selectedTask) return;
  const input = $("taskIdInput");
  const value = input.value.trim();
  if (!value || isAutoTaskIdForVideo(value, selectedName)) {
    input.value = suggestedTaskId(selectedName, $("productionMode").value);
    updateTaskSubtitle();
  }
}

function updateTaskSubtitle(taskId = $("taskIdInput")?.value?.trim()) {
  const box = $("taskSubtitle");
  if (!box) return;
  box.textContent = taskId ? `任务 ID：${displayTaskId(taskId)}` : "任务 ID：等待选择视频后自动生成";
}

function suggestedTaskId(name, productionMode = "ai_voiceover") {
  const base = taskIdBase(name);
  const mode = productionModeName(productionMode);
  const now = new Date();
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  return `${base}_${mode}_${stamp}`;
}

function isAutoTaskIdForVideo(value, name) {
  const base = escapeRegExp(taskIdBase(name));
  return new RegExp(`^${base}_(?:(?:ai_voiceover|highlight_reassembly|AI配音解说|视频重组)_)?\\d{8}_\\d{4,6}$`).test(value);
}

function productionModeName(productionMode) {
  return PRODUCTION_MODE_NAMES[productionMode] || PRODUCTION_MODE_NAMES.ai_voiceover;
}

function displayTaskId(taskId) {
  return String(taskId || "")
    .replace(/highlight_reassembly/g, PRODUCTION_MODE_NAMES.highlight_reassembly)
    .replace(/ai_voiceover/g, PRODUCTION_MODE_NAMES.ai_voiceover);
}

function taskIdBase(name) {
  const base = name.replace(/\.[^.]+$/, "").replace(/[^\p{L}\p{N}]+/gu, "_").replace(/^_+|_+$/g, "").slice(0, 24) || "task";
  return base;
}

function escapeRegExp(str) {
  return String(str).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function pad(n) {
  return String(n).padStart(2, "0");
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeAttr(str) {
  return escapeHtml(str).replace(/`/g, "&#096;");
}

function cleanError(error) {
  return String(error?.message || error || "未知错误").replace(/^Error:\s*/, "");
}

init();

