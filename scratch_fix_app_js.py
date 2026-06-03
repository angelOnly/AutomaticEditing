import sys

path = r'e:\ai\skills\AutomaticEditing\web_static\app.js'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

s1 = """function renderManifest() {
  const manifest = state.manifest;
  if (!manifest) return;
  updateActiveSteps();
  const steps = manifest.steps || {};
  const stepDurations = buildStepDurationMap(manifest, STEPS);
  const rows = STEPS.map((step, index) => {"""

r1 = """function stepsForManifest(manifest) {
  const mode =
    manifest?.production_mode ||
    manifest?.last_run_options?.production_mode ||
    manifest?.last_web_run_options?.production_mode ||
    $("productionMode")?.value ||
    "ai_voiceover";
  return mode === "highlight_reassembly" ? HIGHLIGHT_REASSEMBLY_STEPS : AI_VOICEOVER_STEPS;
}

function renderManifest() {
  const manifest = state.manifest;
  if (!manifest) return;
  const activeSteps = stepsForManifest(manifest);
  const steps = manifest.steps || {};
  const stepDurations = buildStepDurationMap(manifest, activeSteps);
  const rows = activeSteps.map((step, index) => {"""

content = content.replace(s1, r1)

s2 = """  const visibleEntries = STEPS.map((step) => [step, steps[step] || {}]);"""
r2 = """  const visibleEntries = activeSteps.map((step) => [step, steps[step] || {}]);"""
content = content.replace(s2, r2)

s3 = """  updateProgress(done, STEPS.length, completed, skipped, failed);
  $("metricSteps").textContent = `${done}/${STEPS.length}（成功 ${completed}，跳过 ${skipped}）`;"""
r3 = """  updateProgress(done, activeSteps.length, completed, skipped, failed);
  $("metricSteps").textContent = `${done}/${activeSteps.length}（成功 ${completed}，跳过 ${skipped}）`;"""
content = content.replace(s3, r3)

s4 = """    else if (done >= STEPS.length) $("metricJob").textContent = "已完成";"""
r4 = """    else if (done >= activeSteps.length) $("metricJob").textContent = "已完成";"""
content = content.replace(s4, r4)

s5 = """function stepNote(step, item = {}) {
  if (!item || item.status === "pending") return "";
  if (item.status === "skipped") return skippedReasonText(item.reason, step);
  return item.error || item.reason || "";
}"""

r5 = """function stepNote(step, item = {}) {
  if (!item) return "";

  if (step === "vision" && item.summary) {
    const s = item.summary || {};
    const total = Number(s.total_chunks || 0);
    const processed = Number(s.processed_chunks || 0);
    const failed = Number(s.failed_chunks || 0);
    if (total && item.status === "running") {
      return `画面 chunk ${processed}/${total}${failed ? `，失败 ${failed}` : ""}`;
    }
  }

  if (item.common_progress && item.status === "running") return "公共分析中";
  if (item.common_progress && item.status === "success") return "复用公共分析结果";
  if (item.common_progress && item.status === "partial_success") return "公共分析部分成功";

  if (!item.status || item.status === "pending") return "";
  if (item.status === "skipped") return skippedReasonText(item.reason, step);
  return item.error || item.reason || "";
}"""
content = content.replace(s5, r5)

s6 = """  if (job.deduplicated) {
    $("logViewer").textContent = job.message || "已切换到现有任务。";
  } else {
    updateProgress(0, STEPS.length, 0, 0, 0);
    if ($("metricCurrentStep")) $("metricCurrentStep").textContent = stepLabel(STEPS[0]);
    $("logViewer").textContent = "任务已提交，等待日志输出...";
  }"""

r6 = """  if (job.deduplicated) {
    $("logViewer").textContent = job.message || "已切换到现有任务。";
  } else {
    const activeSteps = $("productionMode")?.value === "highlight_reassembly"
      ? HIGHLIGHT_REASSEMBLY_STEPS
      : AI_VOICEOVER_STEPS;
    updateProgress(0, activeSteps.length, 0, 0, 0);
    if ($("metricCurrentStep")) $("metricCurrentStep").textContent = stepLabel(activeSteps[0]);
    $("logViewer").textContent = "任务已提交，等待日志输出...";
  }"""
content = content.replace(s6, r6)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("web_static/app.js patched successfully!")
