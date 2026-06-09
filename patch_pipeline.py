import os

path = r"e:\ai\skills\AutomaticEditing\newsclip_agent\pipeline.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Update AGENT_INFO
content = content.replace(
    '"short_video_edit_plan": ("agents/short_video_edit_plan", "short_video_edit_plan.json", prompts.SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT, "short_video_edit_plan_text_v1"),',
    '"short_video_edit_plan": ("agents/short_video_edit_plan", "short_video_edit_plan.json", prompts.SHORT_VIDEO_EDIT_PLAN_TEXT_PROMPT, "short_video_edit_plan_text_v2"),'
)

# 2. Add _run_text_agent_with_prompt and update _run_text_agent
run_text_agent_old = """    def _run_text_agent(self, step: str, input_data: Any, *, allow_reuse: bool = True) -> Any:
        if self.llm_text is None:
            raise RuntimeError("缺少 text LLM 配置")
        base, out_name, prompt, default_prompt_version = AGENT_INFO[step]"""

run_text_agent_new = """    def _run_text_agent(self, step: str, input_data: Any, *, allow_reuse: bool = True) -> Any:
        _, _, prompt, _ = AGENT_INFO[step]
        return self._run_text_agent_with_prompt(step, input_data, prompt, allow_reuse=allow_reuse)

    def _run_short_video_edit_plan_retry_agent(self, retry_input: str) -> Any:
        return self._run_text_agent_with_prompt(
            "short_video_edit_plan",
            retry_input,
            prompts.SHORT_VIDEO_EDIT_PLAN_RETRY_TEXT_PROMPT,
        )

    def _run_text_agent_with_prompt(self, step: str, input_data: Any, prompt_text: str, *, allow_reuse: bool = True) -> Any:
        if self.llm_text is None:
            raise RuntimeError("缺少 text LLM 配置")
        base, out_name, _, default_prompt_version = AGENT_INFO[step]"""

content = content.replace(run_text_agent_old, run_text_agent_new)

# Note: we also need to change `prompt=` to `prompt_text=` and `prompt` to `prompt_text` in the body of _run_text_agent_with_prompt.
# To do this safely, we will replace inside the _run_text_agent_with_prompt function block.
# We'll do it by replacing specific lines.

content = content.replace(
    'input_hash = stable_hash({"input": input_data, "model": model, "fallback": fallback, "prompt": prompt, "prompt_version": prompt_version})',
    'input_hash = stable_hash({"input": input_data, "model": model, "fallback": fallback, "prompt": prompt_text, "prompt_version": prompt_version})'
)
content = content.replace(
    'write_text(vdir / "prompt.txt", prompt)',
    'write_text(vdir / "prompt.txt", prompt_text)'
)
content = content.replace(
    'prompt=prompt,',
    'prompt=prompt_text,'
)


# 3. Add _short_video_edit_plan_duration_cfg, _short_video_edit_plan_budgets, _short_video_plan_duration_diagnostics, _build_short_video_edit_plan_duration_retry_text, _clip_duration_by_id, _trim_raw_short_video_plan_to_budget
# We can insert them before `_build_short_video_edit_plan_text`

new_methods = """
    def _short_video_edit_plan_duration_cfg(self) -> dict[str, Any]:
        cfg = self.config.raw.get("short_video_edit_plan", {}) if hasattr(self, "config") else {}
        return {
            "duration_retry_enabled": bool(cfg.get("duration_retry_enabled", True)),
            "duration_retry_max_rounds": int(cfg.get("duration_retry_max_rounds", 1) or 1),
            "duration_soft_budget_ratio": float(cfg.get("duration_soft_budget_ratio", 2.0) or 2.0),
            "duration_soft_budget_min_seconds": float(cfg.get("duration_soft_budget_min_seconds", 45) or 45),
            "duration_soft_budget_max_seconds": float(cfg.get("duration_soft_budget_max_seconds", 90) or 90),
            "fallback_trim_enabled": bool(cfg.get("fallback_trim_enabled", True)),
            "fallback_trim_min_clip_count": int(cfg.get("fallback_trim_min_clip_count", 1) or 1),
            "fallback_trim_prefer_soft_budget": bool(cfg.get("fallback_trim_prefer_soft_budget", True)),
        }

    def _short_video_edit_plan_budgets(self) -> dict[str, float]:
        target = float(getattr(self.options, "target_duration_seconds", 30) or 30)
        hard = float(self._effective_ai_voiceover_max_seconds())
        cfg = self._short_video_edit_plan_duration_cfg()

        soft = target * cfg["duration_soft_budget_ratio"]
        soft = max(soft, cfg["duration_soft_budget_min_seconds"])
        soft = min(soft, cfg["duration_soft_budget_max_seconds"])
        soft = min(soft, hard)

        return {
            "target_seconds": round(target, 3),
            "soft_budget_seconds": round(soft, 3),
            "hard_budget_seconds": round(hard, 3),
        }

    def _short_video_plan_duration_diagnostics(self, edit_plan: dict[str, Any]) -> dict[str, Any]:
        budgets = self._short_video_edit_plan_budgets()
        scripts = edit_plan.get("scripts") or []
        items: list[dict[str, Any]] = []
        hard_failed = False
        soft_exceeded = False

        for script in scripts:
            if not isinstance(script, dict):
                continue
            sid = str(script.get("short_video_id") or "unknown")
            clips = script.get("source_clip_ids") or []
            visual_seconds = editing_structure_duration(script.get("editing_structure") or [])

            item = {
                "short_video_id": sid,
                "source_clip_ids": clips,
                "clip_count": len(clips) if isinstance(clips, list) else 0,
                "visual_total_seconds": round(visual_seconds, 3),
                "target_seconds": budgets["target_seconds"],
                "soft_budget_seconds": budgets["soft_budget_seconds"],
                "hard_budget_seconds": budgets["hard_budget_seconds"],
                "soft_exceeded": visual_seconds > budgets["soft_budget_seconds"] + 0.01,
                "hard_exceeded": visual_seconds > budgets["hard_budget_seconds"] + 0.01,
            }
            if item["soft_exceeded"]:
                soft_exceeded = True
            if item["hard_exceeded"]:
                hard_failed = True
            items.append(item)

        return {
            "ok": not hard_failed,
            "hard_failed": hard_failed,
            "soft_exceeded": soft_exceeded,
            "items": items,
            "budgets": budgets,
        }

    def _build_short_video_edit_plan_duration_retry_text(
        self,
        *,
        materialized: dict[str, Any],
        diagnostics: dict[str, Any],
    ) -> str:
        clips_by_id = self._candidate_clips_by_id()
        budgets = diagnostics.get("budgets") or self._short_video_edit_plan_budgets()

        selected_ids: list[str] = []
        for script in materialized.get("scripts") or []:
            if not isinstance(script, dict):
                continue
            for cid in script.get("source_clip_ids") or []:
                cid = str(cid).strip()
                if cid and cid not in selected_ids:
                    selected_ids.append(cid)

        lines = [
            "运行参数：",
            f"- target_duration_seconds：{budgets.get('target_seconds')}",
            f"- soft_budget_seconds：{budgets.get('soft_budget_seconds')}",
            f"- hard_budget_seconds：{budgets.get('hard_budget_seconds')}",
            f"- allow_long_video：{self.options.allow_long_video}",
            f"- output_mode：{self.options.output_mode}",
            f"- max_output_videos：{self.options.max_output_videos}",
            "",
            "上一次选择失败：",
        ]

        for item in diagnostics.get("items") or []:
            lines.append(
                f"- {item.get('short_video_id')}: "
                f"clip_count={item.get('clip_count')}, "
                f"visual_total_seconds={item.get('visual_total_seconds')}, "
                f"soft_exceeded={item.get('soft_exceeded')}, "
                f"hard_exceeded={item.get('hard_exceeded')}"
            )

        lines.extend([
            "",
            "上一次选择的 clips：",
        ])

        for cid in selected_ids:
            clip = clips_by_id.get(cid)
            if not clip:
                lines.append(f"- {cid}: 未找到候选详情")
                continue
            duration = float(clip.get("duration_seconds") or 0)
            summary = compact_text(str(clip.get("summary") or clip.get("event_summary") or ""), max_chars=180)
            speech = sanitize_llm_text(str(clip.get("speech") or clip.get("asr_text") or ""), max_chars=220)
            visual = compact_text(str(clip.get("visual") or clip.get("visual_summary") or ""), max_chars=160)
            lines.append(f"- {cid} duration={duration:.3f}s")
            if summary:
                lines.append(f"  摘要：{summary}")
            if speech:
                lines.append(f"  声音：{speech}")
            if visual:
                lines.append(f"  画面：{visual}")

        lines.extend([
            "",
            "完整候选 clips：",
        ])

        for index, clip in enumerate(self._load_candidate_clips_for_current_mode()):
            cid = self._clip_id(clip, index)
            duration = float(clip.get("duration_seconds") or 0)
            summary = compact_text(str(clip.get("summary") or clip.get("event_summary") or ""), max_chars=180)
            speech = sanitize_llm_text(str(clip.get("speech") or clip.get("asr_text") or ""), max_chars=220)
            visual = compact_text(str(clip.get("visual") or clip.get("visual_summary") or ""), max_chars=160)
            lines.append(f"- {cid} duration={duration:.3f}s role={clip.get('role', 'fact')}")
            if summary:
                lines.append(f"  摘要：{summary}")
            if speech:
                lines.append(f"  声音：{speech}")
            if visual:
                lines.append(f"  画面：{visual}")

        return "\\n".join(lines)

    def _clip_duration_by_id(self) -> dict[str, float]:
        durations: dict[str, float] = {}
        for index, clip in enumerate(self._load_candidate_clips_for_current_mode()):
            cid = self._clip_id(clip, index)
            if not cid:
                continue
            try:
                durations[cid] = max(0.0, float(clip.get("duration_seconds") or 0))
            except (TypeError, ValueError):
                durations[cid] = 0.0
        return durations

    def _trim_raw_short_video_plan_to_budget(
        self,
        raw_result: Any,
        *,
        budget_seconds: float,
        min_clip_count: int = 1,
    ) -> tuple[Any, dict[str, Any]]:
        durations = self._clip_duration_by_id()

        if isinstance(raw_result, list):
            raw_videos = raw_result
            root_is_list = True
        elif isinstance(raw_result, dict):
            raw_videos = raw_result.get("videos")
            if not isinstance(raw_videos, list):
                raw_videos = raw_result.get("scripts") if isinstance(raw_result.get("scripts"), list) else []
            root_is_list = False
        else:
            return raw_result, {"trimmed": False, "reason": "raw_result_not_supported"}

        trimmed_videos = []
        trim_reports = []

        for video in raw_videos:
            if not isinstance(video, dict):
                continue

            selected_clips_raw = video.get("selected_clips")
            if isinstance(selected_clips_raw, list) and selected_clips_raw:
                clip_ids = [
                    str(item.get("clip_id") or item.get("source_clip_id") or "").strip()
                    for item in selected_clips_raw
                    if isinstance(item, dict)
                ]
            else:
                clip_ids = self._coerce_id_list(video.get("clip_ids") or video.get("source_clip_ids"))

            kept: list[str] = []
            dropped: list[str] = []
            total = 0.0

            for cid in clip_ids:
                duration = durations.get(cid, 0.0)
                if duration <= 0:
                    dropped.append(cid)
                    continue
                if kept and total + duration > budget_seconds + 0.01:
                    dropped.append(cid)
                    continue
                if not kept and duration > budget_seconds + 0.01:
                    # 单个 clip 已经超过预算时，仍保留一个，后续让原有 hard 校验报错；
                    # 不在这里切原片时间，避免破坏 clip 语义。
                    kept.append(cid)
                    total += duration
                    continue
                kept.append(cid)
                total += duration

            if len(kept) < min_clip_count and clip_ids:
                # 兜底至少保留第一个有效 clip
                first = clip_ids[0]
                if first not in kept:
                    kept = [first]
                    total = durations.get(first, 0.0)
                    dropped = [cid for cid in clip_ids if cid != first]

            new_video = dict(video)
            new_video.pop("selected_clips", None)
            new_video["clip_ids"] = kept
            trimmed_videos.append(new_video)

            trim_reports.append({
                "original_clip_ids": clip_ids,
                "kept_clip_ids": kept,
                "dropped_clip_ids": dropped,
                "kept_duration_seconds": round(total, 3),
                "budget_seconds": round(budget_seconds, 3),
            })

        if root_is_list:
            trimmed_result = trimmed_videos
        else:
            trimmed_result = dict(raw_result)
            if isinstance(raw_result.get("videos"), list):
                trimmed_result["videos"] = trimmed_videos
            elif isinstance(raw_result.get("scripts"), list):
                trimmed_result["scripts"] = trimmed_videos
            else:
                trimmed_result["videos"] = trimmed_videos

        return trimmed_result, {
            "trimmed": True,
            "budget_seconds": round(budget_seconds, 3),
            "reports": trim_reports,
        }

    def _build_short_video_edit_plan_text(self) -> str:"""

content = content.replace("    def _build_short_video_edit_plan_text(self) -> str:", new_methods)

# 4. Replace the body of _build_short_video_edit_plan_text
# Wait, I need to find the old body and replace it. Let's do it using regex or just string replacement if I can read the old body.
build_short_video_old = '''    def _build_short_video_edit_plan_text(self) -> str:
        llm_cfg = self.config.raw.get("llm_input", {})
        max_clips = int(llm_cfg.get("max_llm_candidate_clips", llm_cfg.get("max_candidate_clips_for_edit_plan", 14)) or 14)
        content = self._load_optional_step_json("content_analysis", {})
        clips = self._load_candidate_clips_for_current_mode()[:max_clips]
        
        target = float(getattr(self.options, "target_duration_seconds", 30) or 30)
        soft = target * 2.0
        hard = self._effective_ai_voiceover_max_seconds()
        soft = min(max(soft, 45), 90)
        soft = min(soft, hard)

        lines = [
            "运行参数：",
            f"- output_mode：{self.options.output_mode}",
            f"- max_output_videos：{self.options.max_output_videos}",
            f"- min_output_video_seconds：{self.options.min_output_video_seconds}",
            f"- target_duration_seconds：{self.options.target_duration_seconds}",
            f"- soft_budget_seconds：{soft}",
            f"- hard_budget_seconds：{hard}",
            f"- raw_max_output_video_seconds：{self.options.max_output_video_seconds}",
            f"- allow_long_video：{self.options.allow_long_video}",
            "",
            "新闻概览：",
            f"主题：{content.get('main_topic') or content.get('topic') or ''}",
            f"摘要：{content.get('summary') or ''}",
            "关键事实：",
        ]
        for fact in (content.get("key_facts") or [])[: int(llm_cfg.get("max_llm_key_facts", 8) or 8)]:
            lines.append(f"- {fact if isinstance(fact, str) else json.dumps(fact, ensure_ascii=False)}")

        lines.append("")
        lines.append("候选 clips：")
        for index, clip in enumerate(clips):
            cid = self._clip_id(clip, index)
            lines.append(f"[{cid}] duration={clip.get('duration_seconds', '')} role={clip.get('role', 'fact')}")
            lines.append(f"摘要：{compact_text(str(clip.get('summary') or ''), max_chars=240)}")
            lines.append(f"声音：{sanitize_llm_text(str(clip.get('speech') or clip.get('asr_text') or ''), max_chars=360)}")
            lines.append(f"画面：{compact_text(str(clip.get('visual') or clip.get('visual_context') or ''), max_chars=220)}")
            lines.append("")
        return "\\n".join(lines)'''

build_short_video_new = '''    def _build_short_video_edit_plan_text(self) -> str:
        llm_cfg = self.config.raw.get("llm_input", {})
        max_clips = int(llm_cfg.get("max_llm_candidate_clips", llm_cfg.get("max_candidate_clips_for_edit_plan", 14)) or 14)
        content = self._load_optional_step_json("content_analysis", {})
        clips = self._load_candidate_clips_for_current_mode()[:max_clips]
        budgets = self._short_video_edit_plan_budgets()

        lines = [
            "运行参数：",
            f"- output_mode：{self.options.output_mode}",
            f"- max_output_videos：{self.options.max_output_videos}",
            f"- min_output_video_seconds：{self.options.min_output_video_seconds}",
            f"- target_duration_seconds：{budgets['target_seconds']}",
            f"- soft_budget_seconds：{budgets['soft_budget_seconds']}",
            f"- hard_budget_seconds：{budgets['hard_budget_seconds']}",
            f"- raw_max_output_video_seconds：{self.options.max_output_video_seconds}",
            f"- allow_long_video：{self.options.allow_long_video}",
            "",
            "新闻概览：",
            f"主题：{content.get('main_topic') or content.get('topic') or ''}",
            f"摘要：{content.get('summary') or ''}",
            "关键事实：",
        ]
        for fact in (content.get("key_facts") or [])[: int(llm_cfg.get("max_llm_key_facts", 8) or 8)]:
            lines.append(f"- {fact if isinstance(fact, str) else json.dumps(fact, ensure_ascii=False)}")

        lines.append("")
        lines.append("候选 clips：")
        for index, clip in enumerate(clips):
            cid = self._clip_id(clip, index)
            lines.append(f"[{cid}] duration={clip.get('duration_seconds', '')} role={clip.get('role', 'fact')}")
            lines.append(f"摘要：{compact_text(str(clip.get('summary') or ''), max_chars=240)}")
            lines.append(f"声音：{sanitize_llm_text(str(clip.get('speech') or clip.get('asr_text') or ''), max_chars=360)}")
            lines.append(f"画面：{compact_text(str(clip.get('visual') or clip.get('visual_context') or ''), max_chars=220)}")
            lines.append("")
        return "\\n".join(lines)'''

# But since the previous step just added new_methods, `def _build_short_video_edit_plan_text(self) -> str:` is already there. Let's make sure we replace the whole block correctly.

# 5. step_short_video_edit_plan
step_old = """    def step_short_video_edit_plan(self) -> None:
        if self.options.production_mode == "ai_voiceover" and not self._load_candidate_clips_for_current_mode():
            raise UserFacingPipelineError(
                "short_video_edit_plan_candidate_clips_empty",
                user_message="选片规划失败：AI 配音候选片段为空。",
                suggestions=["回查 ai_voiceover_candidates/v*/candidate_clips.json。", "从 content_analysis 重新运行。"],
            )

        text_input = self._build_short_video_edit_plan_text()
        raw_result = self._run_text_agent("short_video_edit_plan", text_input)

        first_diagnostics = self._short_video_edit_plan_semantic_diagnostics(raw_result)
        retry_diagnostics: dict[str, Any] | None = None

        if self.options.production_mode == "ai_voiceover" and not first_diagnostics.get("ok"):
            first_out = self.task_dir / self._step_output("short_video_edit_plan")
            if first_out.exists():
                write_json(first_out.parent / "semantic_retry_first_output.json", raw_result)
                write_json(first_out.parent / "semantic_retry_first_diagnostics.json", first_diagnostics)
            retry_input = (
                text_input
                + "\\n\\n---\\n"
                + "上一次输出未通过业务校验，请只使用输入中存在的 clip_id 重新输出 JSON。"
                + f"\\n失败原因：{first_diagnostics.get('reason')}"
                + f"\\n无效 clip_id：{first_diagnostics.get('invalid_clip_ids', [])}"
                + f"\\n可用 clip_id：{first_diagnostics.get('candidate_clip_ids', [])}"
            )
            raw_result = self._run_text_agent("short_video_edit_plan", retry_input)
            retry_diagnostics = self._short_video_edit_plan_semantic_diagnostics(raw_result)
            retry_out = self.task_dir / self._step_output("short_video_edit_plan")
            if retry_out.exists():
                write_json(retry_out.parent / "semantic_retry_response.json", raw_result)
                write_json(retry_out.parent / "semantic_retry_diagnostics.json", {
                    "first": first_diagnostics,
                    "retry": retry_diagnostics,
                })
            if not retry_diagnostics.get("ok"):
                raise UserFacingPipelineError(
                    "short_video_edit_plan_semantic_retry_failed",
                    user_message="AI 配音选片规划失败：模型重试后仍未返回可用 clip_id。",
                    suggestions=["回查 agents/short_video_edit_plan/v*/semantic_retry_diagnostics.json。", "检查 candidate_clips 是否为空或 clip_id 是否过长难以复制。"],
                    technical_detail={"first": first_diagnostics, "retry": retry_diagnostics},
                )

        result = self._materialize_short_video_edit_plan(raw_result)

        if retry_diagnostics is not None:
            result["semantic_retry"] = {"first": first_diagnostics, "retry": retry_diagnostics}
        else:
            result["semantic_retry"] = {"first": first_diagnostics}

        self._validate_ai_voiceover_editing_structure_or_raise(result)
        self._validate_ai_voiceover_plan_duration_or_raise(result)
        self._overwrite_step_json("short_video_edit_plan", result, materialized=True)
        self._write_compat_short_video_plan_and_editing_script(result)"""

step_new = """    def step_short_video_edit_plan(self) -> None:
        if self.options.production_mode == "ai_voiceover" and not self._load_candidate_clips_for_current_mode():
            raise UserFacingPipelineError(
                "short_video_edit_plan_candidate_clips_empty",
                user_message="选片规划失败：AI 配音候选片段为空。",
                suggestions=["回查 ai_voiceover_candidates/v*/candidate_clips.json。", "从 content_analysis 重新运行。"],
            )

        text_input = self._build_short_video_edit_plan_text()
        raw_result = self._run_text_agent("short_video_edit_plan", text_input)

        first_diagnostics = self._short_video_edit_plan_semantic_diagnostics(raw_result)
        retry_diagnostics: dict[str, Any] | None = None

        if self.options.production_mode == "ai_voiceover" and not first_diagnostics.get("ok"):
            first_out = self.task_dir / self._step_output("short_video_edit_plan")
            if first_out.exists():
                write_json(first_out.parent / "semantic_retry_first_output.json", raw_result)
                write_json(first_out.parent / "semantic_retry_first_diagnostics.json", first_diagnostics)
            retry_input = (
                text_input
                + "\\n\\n---\\n"
                + "上一次输出未通过业务校验，请只使用输入中存在的 clip_id 重新输出 JSON。"
                + f"\\n失败原因：{first_diagnostics.get('reason')}"
                + f"\\n无效 clip_id：{first_diagnostics.get('invalid_clip_ids', [])}"
                + f"\\n可用 clip_id：{first_diagnostics.get('candidate_clip_ids', [])}"
            )
            raw_result = self._run_text_agent("short_video_edit_plan", retry_input)
            retry_diagnostics = self._short_video_edit_plan_semantic_diagnostics(raw_result)
            retry_out = self.task_dir / self._step_output("short_video_edit_plan")
            if retry_out.exists():
                write_json(retry_out.parent / "semantic_retry_response.json", raw_result)
                write_json(retry_out.parent / "semantic_retry_diagnostics.json", {
                    "first": first_diagnostics,
                    "retry": retry_diagnostics,
                })
            if not retry_diagnostics.get("ok"):
                raise UserFacingPipelineError(
                    "short_video_edit_plan_semantic_retry_failed",
                    user_message="AI 配音选片规划失败：模型重试后仍未返回可用 clip_id。",
                    suggestions=["回查 agents/short_video_edit_plan/v*/semantic_retry_diagnostics.json。", "检查 candidate_clips 是否为空或 clip_id 是否过长难以复制。"],
                    technical_detail={"first": first_diagnostics, "retry": retry_diagnostics},
                )

        result = self._materialize_short_video_edit_plan(raw_result)

        duration_retry_info = None
        trim_info = None

        if self.options.production_mode == "ai_voiceover":
            cfg = self._short_video_edit_plan_duration_cfg()
            duration_diag = self._short_video_plan_duration_diagnostics(result)
            out = self.task_dir / self._step_output("short_video_edit_plan")
            if out.exists():
                write_json(out.parent / "duration_first_diagnostics.json", duration_diag)

            need_duration_retry = (
                cfg["duration_retry_enabled"]
                and (
                    duration_diag.get("hard_failed")
                    or duration_diag.get("soft_exceeded")
                )
            )

            # 如果只是略微超过 soft_budget，但没有超过 hard_budget，可以选择不重试。
            # 为了避免 target=30 时输出 100+ 秒，建议 soft_exceeded 也重试一次。
            if need_duration_retry:
                retry_input = self._build_short_video_edit_plan_duration_retry_text(
                    materialized=result,
                    diagnostics=duration_diag,
                )
                retry_raw_result = self._run_short_video_edit_plan_retry_agent(retry_input)
                retry_semantic = self._short_video_edit_plan_semantic_diagnostics(retry_raw_result)

                if out.exists():
                    write_json(out.parent / "duration_retry_response.json", retry_raw_result)
                    write_json(out.parent / "duration_retry_semantic_diagnostics.json", retry_semantic)

                if retry_semantic.get("ok"):
                    retry_result = self._materialize_short_video_edit_plan(retry_raw_result)
                    retry_duration_diag = self._short_video_plan_duration_diagnostics(retry_result)
                    if out.exists():
                        write_json(out.parent / "duration_retry_diagnostics.json", retry_duration_diag)

                    raw_result = retry_raw_result
                    result = retry_result
                    duration_retry_info = {
                        "triggered": True,
                        "first": duration_diag,
                        "retry": retry_duration_diag,
                        "retry_semantic": retry_semantic,
                    }
                else:
                    duration_retry_info = {
                        "triggered": True,
                        "first": duration_diag,
                        "retry_semantic": retry_semantic,
                        "retry_ignored": True,
                    }

            # 重试后仍超过 hard_budget，则代码兜底裁剪。
            final_diag = self._short_video_plan_duration_diagnostics(result)
            if final_diag.get("hard_failed") and cfg["fallback_trim_enabled"]:
                budgets = final_diag.get("budgets") or self._short_video_edit_plan_budgets()
                budget = budgets["soft_budget_seconds"] if cfg["fallback_trim_prefer_soft_budget"] else budgets["hard_budget_seconds"]

                trimmed_raw, trim_info = self._trim_raw_short_video_plan_to_budget(
                    raw_result,
                    budget_seconds=float(budget),
                    min_clip_count=cfg["fallback_trim_min_clip_count"],
                )
                trimmed_result = self._materialize_short_video_edit_plan(trimmed_raw)
                trimmed_diag = self._short_video_plan_duration_diagnostics(trimmed_result)

                # 如果按 soft_budget 裁剪后仍失败，再按 hard_budget 裁一次。
                if trimmed_diag.get("hard_failed") and budget < budgets["hard_budget_seconds"]:
                    trimmed_raw, trim_info_hard = self._trim_raw_short_video_plan_to_budget(
                        raw_result,
                        budget_seconds=float(budgets["hard_budget_seconds"]),
                        min_clip_count=cfg["fallback_trim_min_clip_count"],
                    )
                    trimmed_result = self._materialize_short_video_edit_plan(trimmed_raw)
                    trimmed_diag = self._short_video_plan_duration_diagnostics(trimmed_result)
                    trim_info = {
                        "soft_trim": trim_info,
                        "hard_trim": trim_info_hard,
                    }

                raw_result = trimmed_raw
                result = trimmed_result
                if out.exists():
                    write_json(out.parent / "duration_trim_info.json", trim_info)
                    write_json(out.parent / "duration_trim_diagnostics.json", trimmed_diag)

        if retry_diagnostics is not None:
            result["semantic_retry"] = {"first": first_diagnostics, "retry": retry_diagnostics}
        else:
            result["semantic_retry"] = {"first": first_diagnostics}

        if duration_retry_info:
            result["duration_retry"] = duration_retry_info
        if trim_info:
            result["duration_trim"] = trim_info

        self._validate_ai_voiceover_editing_structure_or_raise(result)
        self._validate_ai_voiceover_plan_duration_or_raise(result)
        self._overwrite_step_json("short_video_edit_plan", result, materialized=True)
        self._write_compat_short_video_plan_and_editing_script(result)"""

import re

# To safely replace the body of _build_short_video_edit_plan_text
def replace_func(func_name, new_impl, content):
    pattern = r"    def " + func_name + r"\(self.*?\n(        .*?\n|\n)*?(?=    def |\Z)"
    match = re.search(pattern, content)
    if not match:
        print(f"Function {func_name} not found")
        return content
    return content[:match.start()] + new_impl + "\n" + content[match.end():]

content = replace_func("_build_short_video_edit_plan_text", build_short_video_new, content)
content = replace_func("step_short_video_edit_plan", step_new, content)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("Patch applied successfully.")
