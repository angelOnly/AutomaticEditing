from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .utils import read_json, write_json


STEP_OUTPUTS = {
    "highlight_detection": "agents/highlight_detection/{version}/candidate_clips.json",
    "short_video_planning": "agents/short_video_planning/{version}/short_video_plan.json",
    "editing_script": "agents/editing_script/{version}/editing_script.json",
    "voiceover_script": "agents/voiceover_script/{version}/voiceover_script.json",
    "risk_review": "agents/risk_review/{version}/review_report.json",
    "vision": "vision/{version}/visual_analysis.json",
}


def compare_highlights(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    old_items = {x.get("clip_id"): x for x in old.get("candidate_clips", []) if x.get("clip_id")}
    new_items = {x.get("clip_id"): x for x in new.get("candidate_clips", []) if x.get("clip_id")}
    added = sorted(set(new_items) - set(old_items))
    removed = sorted(set(old_items) - set(new_items))
    changed = []
    for cid in sorted(set(old_items) & set(new_items)):
        o, n = old_items[cid], new_items[cid]
        fields = {}
        for key in ["start", "end", "summary", "news_value_score", "risk_level", "recommendation"]:
            if o.get(key) != n.get(key):
                fields[key] = {"old": o.get(key), "new": n.get(key)}
        if fields:
            changed.append({"clip_id": cid, "changes": fields})
    return {"added_clips": added, "removed_clips": removed, "changed_clips": changed}


def compare_generic(old: Any, new: Any) -> dict[str, Any]:
    return {
        "old_summary": summarize(old),
        "new_summary": summarize(new),
        "changed": old != new,
    }


def summarize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: summarize(v) for k, v in list(value.items())[:8]}
    if isinstance(value, list):
        return {"count": len(value), "first": summarize(value[0]) if value else None}
    text = str(value)
    return text[:160] + ("..." if len(text) > 160 else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对比某一步两个版本的结果")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--outputs-dir", default="outputs")
    parser.add_argument("--step", required=True, choices=sorted(STEP_OUTPUTS))
    parser.add_argument("--v1", required=True)
    parser.add_argument("--v2", required=True)
    parser.add_argument("--save", help="保存对比 JSON 的路径")
    args = parser.parse_args(argv)

    task_dir = Path(args.outputs_dir) / args.task_id
    pattern = STEP_OUTPUTS[args.step]
    old = read_json(task_dir / pattern.format(version=args.v1), {})
    new = read_json(task_dir / pattern.format(version=args.v2), {})
    if args.step == "highlight_detection":
        result = compare_highlights(old, new)
    else:
        result = compare_generic(old, new)
    result = {"task_id": args.task_id, "step": args.step, "compare": f"{args.v1} vs {args.v2}", **result}
    if args.save:
        write_json(args.save, result)
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
