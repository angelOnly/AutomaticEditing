from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from newsclip_agent.manual_editor import (
    add_asset,
    add_mosaic_effect,
    export_project,
    initialize_project,
    insert_clip,
    load_project,
    move_clip,
    reorder_clips,
    ripple_delete,
    save_project,
    split_clip,
    trim_clip,
    validate_project,
)


def _source_overrides(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise argparse.ArgumentTypeError("--source must use SOURCE_ID=PATH")
        source_id, path = value.split("=", 1)
        if not source_id.strip() or not path.strip():
            raise argparse.ArgumentTypeError("--source must use SOURCE_ID=PATH")
        result[source_id.strip()] = path.strip()
    return result


def _write_edit(project: dict[str, Any], output: str) -> None:
    path = save_project(project, output)
    print(
        json.dumps(
            {
                "project": str(path.resolve()),
                "revision": project["revision"],
                "duration_seconds": project["timeline"]["duration_seconds"],
                "clip_count": len(project["timeline"]["clips"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Non-destructive single-track manual video editor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="initialize a project from full_concat_cut_plan.json")
    init_parser.add_argument("plan")
    init_parser.add_argument("output")
    init_parser.add_argument("--source-manifest")
    init_parser.add_argument("--source", action="append", default=[], metavar="SOURCE_ID=PATH")
    init_parser.add_argument("--reassembly-id")
    init_parser.add_argument("--task-root")

    validate_parser = subparsers.add_parser("validate", help="validate a project document")
    validate_parser.add_argument("project")
    validate_parser.add_argument("--check-files", action="store_true")

    delete_parser = subparsers.add_parser("delete", help="ripple-delete a timeline interval")
    delete_parser.add_argument("project")
    delete_parser.add_argument("output")
    delete_parser.add_argument("--start", required=True, type=float)
    delete_parser.add_argument("--end", required=True, type=float)

    split_parser = subparsers.add_parser("split", help="split at a clip-local offset")
    split_parser.add_argument("project")
    split_parser.add_argument("output")
    split_parser.add_argument("--clip-id", required=True)
    split_parser.add_argument("--at", required=True, type=float)

    trim_parser = subparsers.add_parser("trim", help="shorten a clip using absolute source times")
    trim_parser.add_argument("project")
    trim_parser.add_argument("output")
    trim_parser.add_argument("--clip-id", required=True)
    trim_parser.add_argument("--source-in", type=float)
    trim_parser.add_argument("--source-out", type=float)

    asset_parser = subparsers.add_parser("add-asset", help="add a media asset")
    asset_parser.add_argument("project")
    asset_parser.add_argument("output")
    asset_parser.add_argument("path")
    asset_parser.add_argument("--asset-id")
    asset_parser.add_argument("--source-id")
    asset_parser.add_argument("--display-name")

    insert_parser = subparsers.add_parser("insert", help="insert an asset source range")
    insert_parser.add_argument("project")
    insert_parser.add_argument("output")
    insert_parser.add_argument("--asset-id", required=True)
    insert_parser.add_argument("--source-in", required=True, type=float)
    insert_parser.add_argument("--source-out", required=True, type=float)
    position = insert_parser.add_mutually_exclusive_group()
    position.add_argument("--index", type=int)
    position.add_argument("--at", type=float, dest="timeline_seconds")

    move_parser = subparsers.add_parser("move", help="move one clip to a final list index")
    move_parser.add_argument("project")
    move_parser.add_argument("output")
    move_parser.add_argument("--clip-id", required=True)
    move_parser.add_argument("--index", required=True, type=int)

    reorder_parser = subparsers.add_parser("reorder", help="set the complete clip order")
    reorder_parser.add_argument("project")
    reorder_parser.add_argument("output")
    reorder_parser.add_argument("clip_ids", nargs="+")

    mosaic_parser = subparsers.add_parser("mosaic", help="add a fixed rectangle mosaic to one clip")
    mosaic_parser.add_argument("project")
    mosaic_parser.add_argument("output")
    mosaic_parser.add_argument("--clip-id", required=True)
    mosaic_parser.add_argument("--start", required=True, type=float)
    mosaic_parser.add_argument("--end", required=True, type=float)
    mosaic_parser.add_argument("--x", required=True, type=float)
    mosaic_parser.add_argument("--y", required=True, type=float)
    mosaic_parser.add_argument("--width", required=True, type=float)
    mosaic_parser.add_argument("--height", required=True, type=float)
    mosaic_parser.add_argument("--block-size", type=int, default=18)

    export_parser = subparsers.add_parser("export", help="render, concatenate and ffprobe-QC a project")
    export_parser.add_argument("project")
    export_parser.add_argument("output")
    export_parser.add_argument("--cache-dir")
    export_parser.add_argument("--work-dir")
    export_parser.add_argument("--preset", default="veryfast")
    export_parser.add_argument("--crf", type=int, default=18)
    export_parser.add_argument("--timeout", type=float)
    export_parser.add_argument("--no-strict-qc", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "init":
        project = initialize_project(
            args.plan,
            source_manifest=args.source_manifest,
            source_paths=_source_overrides(args.source),
            reassembly_id=args.reassembly_id,
            task_root=args.task_root,
        )
        _write_edit(project, args.output)
        return 0

    if args.command == "validate":
        project = load_project(args.project, check_files=args.check_files)
        validate_project(project, check_files=args.check_files)
        print(json.dumps({"status": "ok", "project": str(Path(args.project).resolve())}, indent=2))
        return 0

    project = load_project(args.project)
    if args.command == "delete":
        _write_edit(ripple_delete(project, args.start, args.end), args.output)
    elif args.command == "split":
        _write_edit(split_clip(project, args.clip_id, args.at), args.output)
    elif args.command == "trim":
        _write_edit(
            trim_clip(project, args.clip_id, source_in=args.source_in, source_out=args.source_out),
            args.output,
        )
    elif args.command == "add-asset":
        _write_edit(
            add_asset(
                project,
                args.path,
                asset_id=args.asset_id,
                source_id=args.source_id,
                display_name=args.display_name,
            ),
            args.output,
        )
    elif args.command == "insert":
        _write_edit(
            insert_clip(
                project,
                args.asset_id,
                args.source_in,
                args.source_out,
                index=args.index,
                timeline_seconds=args.timeline_seconds,
            ),
            args.output,
        )
    elif args.command == "move":
        _write_edit(move_clip(project, args.clip_id, args.index), args.output)
    elif args.command == "reorder":
        _write_edit(reorder_clips(project, args.clip_ids), args.output)
    elif args.command == "mosaic":
        _write_edit(
            add_mosaic_effect(
                project,
                args.clip_id,
                start_seconds=args.start,
                end_seconds=args.end,
                x=args.x,
                y=args.y,
                width=args.width,
                height=args.height,
                block_size=args.block_size,
            ),
            args.output,
        )
    elif args.command == "export":
        report = export_project(
            project,
            args.output,
            cache_dir=args.cache_dir,
            work_dir=args.work_dir,
            preset=args.preset,
            crf=args.crf,
            timeout=args.timeout,
            strict_qc=not args.no_strict_qc,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
