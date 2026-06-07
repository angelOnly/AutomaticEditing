import re

CODE_TO_INSERT = """
    def step_content_analysis_preselect(self) -> None:
        source_doc = self._load_step_json("asr_micro_segment")
        segments = [x for x in source_doc.get("segments", []) if isinstance(x, dict)]

        cfg = self.config.raw.get("content_analysis_preselect", {})
        enabled = bool(cfg.get("enabled", True))

        strategy = self._resolve_content_analysis_preselect_strategy(len(segments), cfg)
        input_hash = stable_hash({
            "asr_micro_segment": self._step_content_hash("asr_micro_segment"),
            "strategy": strategy,
            "enabled": enabled,
            "prompt": prompts.CONTENT_ANALYSIS_PRESELECT_PROMPT,
        })

        if self._can_reuse("content_analysis_preselect", input_hash):
            print("复用缓存: content_analysis_preselect")
            return

        version, vdir = self._version_dir("content_analysis_preselect", "agents/content_analysis_preselect")
        ensure_dir(vdir)

        if not enabled or len(segments) <= strategy["direct_threshold"]:
            output = self._build_content_analysis_preselect_passthrough(segments, strategy, enabled)
            self._write_content_analysis_preselect_output(output, input_hash, version, vdir)
            return

        batches = self._split_micro_segments_for_preselect(segments, strategy["batch_size"])
        results = self._run_content_analysis_preselect_batches(batches, strategy, vdir)

        output = self._merge_content_analysis_preselect_results(
            segments=segments,
            batch_results=results,
            strategy=strategy,
        )
        self._write_content_analysis_preselect_output(output, input_hash, version, vdir)

    def _resolve_content_analysis_preselect_strategy(self, total_segments: int, cfg: dict) -> dict:
        direct_threshold = int(cfg.get("direct_threshold", 10) or 10)
        small_total_threshold = int(cfg.get("small_total_threshold", 30) or 30)

        if total_segments <= small_total_threshold:
            batch_size = int(cfg.get("small_batch_size", 10) or 10)
            max_keep = int(cfg.get("small_max_keep_per_batch", 5) or 5)
            max_final = int(cfg.get("small_max_final_candidates", 20) or 20)
        else:
            batch_size = int(cfg.get("large_batch_size", 10) or 10)
            max_keep = int(cfg.get("large_max_keep_per_batch", 3) or 3)
            max_final = int(cfg.get("large_max_final_candidates", 30) or 30)

        return {
            "direct_threshold": max(0, direct_threshold),
            "small_total_threshold": max(1, small_total_threshold),
            "batch_size": max(1, batch_size),
            "max_keep_per_batch": max(1, max_keep),
            "max_final_candidates": max(1, max_final),
            "max_workers": max(1, int(cfg.get("max_workers", 4) or 4)),
            "max_asr_chars_per_segment": max(40, int(cfg.get("max_asr_chars_per_segment", 220) or 220)),
            "max_visual_chars_per_segment": max(20, int(cfg.get("max_visual_chars_per_segment", 100) or 100)),
            "max_summary_chars_per_segment": max(20, int(cfg.get("max_summary_chars_per_segment", 120) or 120)),
            "max_input_chars_per_batch": max(1000, int(cfg.get("max_input_chars_per_batch", 8000) or 8000)),
        }

    def _build_content_analysis_preselect_passthrough(self, segments: list[dict], strategy: dict, enabled: bool) -> dict:
        kept = []
        for seg in segments:
            msid = str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
            if not msid: continue
            kept.append({
                "micro_segment_id": msid,
                "batch_id": "passthrough",
                "score": 10,
                "summary": compact_text(str(seg.get("summary") or ""), max_chars=strategy["max_summary_chars_per_segment"]),
                "reason": "pass-through",
            })
        return {
            "version": "content_analysis_preselect_v1",
            "enabled": enabled,
            "source": "asr_micro_segment",
            "strategy": strategy,
            "batch_count": 0,
            "input_segment_count": len(segments),
            "kept_segment_count": len(kept),
            "kept_segments": kept,
            "batches": [],
            "warnings": [],
        }

    def _split_micro_segments_for_preselect(self, segments: list[dict], batch_size: int) -> list[dict]:
        batches = []
        for start in range(0, len(segments), batch_size):
            end = min(start + batch_size, len(segments))
            batches.append({
                "batch_id": f"batch_{len(batches) + 1:03d}",
                "start_index": start,
                "end_index": end,
                "segments": segments[start:end],
            })
        return batches

    def _shrink_preselect_batch_text(self, batch: dict, strategy: dict) -> str:
        s2 = dict(strategy)
        s2["max_asr_chars_per_segment"] = max(20, strategy["max_asr_chars_per_segment"] // 2)
        s2["max_visual_chars_per_segment"] = max(10, strategy["max_visual_chars_per_segment"] // 2)
        s2["max_summary_chars_per_segment"] = max(10, strategy["max_summary_chars_per_segment"] // 2)
        text = self._build_content_analysis_preselect_batch_text(batch, s2, shrink_fallback=False)
        if len(text) > strategy["max_input_chars_per_batch"]:
            hint = "请降低 batch_size 或减少每个 segment 的 ASR/画面字段长度。"
            raise RuntimeError(f"LLM input size [content_analysis_preselect] exceeds limit={strategy['max_input_chars_per_batch']}; {hint}")
        return text

    def _build_content_analysis_preselect_batch_text(self, batch: dict, strategy: dict, shrink_fallback: bool = True) -> str:
        lines = [
            "任务：从当前 batch 的 micro_segments 中预选 AI 配音候选片段。",
            f"batch_id: {batch['batch_id']}",
            f"max_keep_per_batch: {strategy['max_keep_per_batch']}",
            "",
            "规则：",
            "- 只能选择下面出现过的 micro_segment_id。",
            f"- 最多选择 {strategy['max_keep_per_batch']} 个。",
            "- 可以一个都不选。",
            "- 不要输出时间码。",
            "- 不要改写 micro_segment_id。",
            "",
            "micro_segments:",
            "",
        ]

        for offset, seg in enumerate(batch["segments"], start=1):
            msid = str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
            if not msid:
                continue
            source_id = str(seg.get("source_id") or "").strip()
            duration = seg.get("duration_seconds", "")
            summary = compact_text(str(seg.get("summary") or ""), max_chars=strategy["max_summary_chars_per_segment"])
            speech = sanitize_llm_text(str(seg.get("asr_text") or seg.get("speech") or ""), max_chars=strategy["max_asr_chars_per_segment"])
            visual = compact_text(str(seg.get("visual_summary") or seg.get("visual") or ""), max_chars=strategy["max_visual_chars_per_segment"])

            lines.append(f"[{msid}]")
            lines.append(f"source={source_id}")
            lines.append(f"index={batch['start_index'] + offset}")
            lines.append(f"duration={duration}")
            if summary: lines.append(f"摘要：{summary}")
            if speech: lines.append(f"声音：{speech}")
            if visual: lines.append(f"画面：{visual}")
            lines.append("")

        text = "\\n".join(lines).strip()
        if shrink_fallback and len(text) > strategy["max_input_chars_per_batch"]:
            text = self._shrink_preselect_batch_text(batch, strategy)
        return text

    def _run_content_analysis_preselect_batches(self, batches: list[dict], strategy: dict, vdir: Path) -> list[dict]:
        max_workers = min(strategy["max_workers"], len(batches)) or 1
        results = []

        def run_one(batch: dict) -> dict:
            text_input = self._build_content_analysis_preselect_batch_text(batch, strategy)
            parsed = self._run_text_agent_for_preselect_batch(batch, text_input, vdir)
            return self._normalize_content_analysis_preselect_batch_result(batch, parsed, strategy)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(run_one, batch): batch for batch in batches}
            for future in as_completed(future_map):
                batch = future_map[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({
                        "batch_id": batch["batch_id"],
                        "status": "failed",
                        "error": str(exc),
                        "kept_segments": [],
                        "input_count": len(batch.get("segments") or []),
                    })

        return sorted(results, key=lambda x: x.get("batch_id", ""))

    def _run_text_agent_for_preselect_batch(self, batch: dict, input_data: str, vdir: Path) -> Any:
        if self.llm_text is None:
            raise RuntimeError("缺少 text LLM 配置")

        llm_cfg = self.config.llm
        provider = llm_cfg.get("text_llm_provider", "openai")
        model = self.options.model or llm_cfg.get("text_llm_model_name") or llm_cfg.get(f"text_{provider}_model_name")
        fallback = llm_cfg.get(f"text_{provider}_fallback_models", [])
        max_tokens = int(llm_cfg.get("text_llm_max_tokens", 16000) or 16000)

        batch_dir = vdir / "batches" / batch["batch_id"]
        ensure_dir(batch_dir)

        self._log_llm_input_size("content_analysis_preselect", input_data)
        write_text(batch_dir / "input.txt", input_data)
        write_json(batch_dir / "input_meta.json", {"type": "text", "chars": len(input_data)})
        write_text(batch_dir / "prompt.txt", prompts.CONTENT_ANALYSIS_PRESELECT_PROMPT)

        result = self.llm_text.call_json(
            model=model,
            fallback_models=fallback,
            prompt=prompts.CONTENT_ANALYSIS_PRESELECT_PROMPT,
            input_data=input_data,
            temperature=0.2,
            debug_dir=batch_dir / "_llm_debug",
            max_tokens=max_tokens,
        )

        write_json(batch_dir / "raw_response.json", {
            "model": result.model,
            "created_at": now_iso(),
            "raw_text": result.raw_text,
            "usage": result.usage,
            "finish_reason": result.finish_reason,
            "latency_ms": result.latency_ms,
        })
        write_json(batch_dir / "model_output.json", result.parsed)
        return result.parsed

    def _normalize_content_analysis_preselect_batch_result(self, batch: dict, parsed: Any, strategy: dict) -> dict:
        allowed_ids = {
            str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
            for seg in batch.get("segments", [])
            if str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
        }

        raw_items = []
        if isinstance(parsed, dict) and isinstance(parsed.get("kept_segments"), list):
            raw_items = parsed.get("kept_segments")
        elif isinstance(parsed, list):
            raw_items = parsed

        kept = []
        seen = set()
        for item in raw_items:
            if not isinstance(item, dict): continue
            msid = str(item.get("micro_segment_id") or item.get("id") or "").strip()
            if not msid or msid not in allowed_ids or msid in seen:
                continue
            seen.add(msid)
            score = item.get("score", 0)
            try:
                score = int(score)
            except Exception:
                score = 0
            kept.append({
                "micro_segment_id": msid,
                "batch_id": batch["batch_id"],
                "score": max(0, min(10, score)),
                "summary": compact_text(str(item.get("summary") or ""), max_chars=160),
                "reason": compact_text(str(item.get("reason") or ""), max_chars=160),
            })
            if len(kept) >= strategy["max_keep_per_batch"]:
                break

        return {
            "batch_id": batch["batch_id"],
            "start_index": batch["start_index"],
            "end_index": batch["end_index"],
            "input_count": len(batch.get("segments") or []),
            "kept_count": len(kept),
            "status": "success",
            "kept_segments": kept,
        }

    def _merge_content_analysis_preselect_results(self, segments: list[dict], batch_results: list[dict], strategy: dict) -> dict:
        by_id = {
            str(seg.get("micro_segment_id") or seg.get("id") or "").strip(): seg
            for seg in segments
            if str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
        }

        items = []
        warnings = []
        for result in batch_results:
            if result.get("status") != "success":
                warnings.append({
                    "batch_id": result.get("batch_id"),
                    "error": result.get("error"),
                })
                continue
            for item in result.get("kept_segments") or []:
                msid = item.get("micro_segment_id")
                if msid in by_id:
                    source = by_id[msid]
                    enriched = dict(item)
                    enriched["source_id"] = source.get("source_id", "")
                    enriched["duration_seconds"] = source.get("duration_seconds", "")
                    items.append(enriched)

        # 去重，保留最高分
        dedup = {}
        for item in items:
            msid = item["micro_segment_id"]
            old = dedup.get(msid)
            if old is None or int(item.get("score") or 0) > int(old.get("score") or 0):
                dedup[msid] = item

        kept = list(dedup.values())
        kept.sort(key=lambda x: int(x.get("score") or 0), reverse=True)
        kept = kept[: strategy["max_final_candidates"]]

        if not kept and segments:
            # fallback: fallback to top N items
            fallback_count = min(10, len(segments))
            warnings.append({"type": "empty_preselect_fallback"})
            for i in range(fallback_count):
                seg = segments[i]
                msid = str(seg.get("micro_segment_id") or seg.get("id") or "").strip()
                if not msid: continue
                kept.append({
                    "micro_segment_id": msid,
                    "batch_id": "fallback",
                    "score": 1,
                    "summary": compact_text(str(seg.get("summary") or ""), max_chars=strategy["max_summary_chars_per_segment"]),
                    "reason": "fallback due to empty preselect",
                    "source_id": seg.get("source_id", ""),
                    "duration_seconds": seg.get("duration_seconds", ""),
                })

        return {
            "version": "content_analysis_preselect_v1",
            "enabled": True,
            "source": "asr_micro_segment",
            "strategy": strategy,
            "batch_count": len(batch_results),
            "input_segment_count": len(segments),
            "kept_segment_count": len(kept),
            "kept_segments": kept,
            "batches": [
                {k: v for k, v in result.items() if k != "kept_segments"}
                for result in batch_results
            ],
            "warnings": warnings,
        }

    def _write_content_analysis_preselect_output(self, output: dict, input_hash: str, version: str, vdir: Path) -> None:
        out = write_json(vdir / "content_analysis_preselect.json", output)
        status = self._base_status("content_analysis_preselect", version, input_hash, [out])
        self._write_status(vdir, status)
        self._record_step(
            step="content_analysis_preselect",
            version=version,
            status="success",
            output=relpath(out, self.task_dir),
            input_hash=input_hash,
            output_files=[out],
            extra={
                "summary": {
                    "input_segments": output.get("input_segment_count", 0),
                    "kept_segments": output.get("kept_segment_count", 0),
                    "batch_count": output.get("batch_count", 0),
                }
            },
        )
        print("完成: content_analysis_preselect")

"""

def main():
    file_path = "e:/ai/skills/AutomaticEditing/newsclip_agent/pipeline.py"
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    target = "    def step_content_analysis(self) -> None:"
    if target in content:
        print("Found target, inserting...")
        content = content.replace(target, CODE_TO_INSERT + "\\n" + target)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        print("Inserted code.")
    else:
        print("Target not found.")

if __name__ == "__main__":
    main()
