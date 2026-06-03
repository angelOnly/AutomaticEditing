import sys
path = r'e:\ai\skills\AutomaticEditing\newsclip_agent\pipeline.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

bad_chunk_1 = '''                    "type": "confirm_long_video",
                    "source_step": source_step,
        plan = self._load_step_json("short_video_planning")'''

good_chunk_1 = '''                    "type": "confirm_long_video",
                    "source_step": source_step,
                    "short_video_id": item.get("short_video_id", ""),
                    "estimated_duration_seconds": seconds,
                    "reason": item.get("over_60_seconds_reason") or item.get("duration_reason") or item.get("reason") or "规划时长超过 60 秒，需要确认长版后继续。",
                    "continue_command": "--allow-long-video --rerun-from editing_script",
                }
                self._save_manifest()
                raise RuntimeError(f"需要确认长版后继续: {seconds:.1f}s")

    def _enforce_long_video_confirmation_after_voiceover(self, voiceover: dict[str, Any]) -> None:
        if self.options.allow_long_video:
            return
        editing = self._load_step_json("editing_script")
        plan = self._load_step_json("short_video_planning")'''

bad_chunk_2 = '''                self._save_manifest()

                print(f"vision {record['chunk_id']}: {record['status']}")

        success = sum(1 for c in chunk_records if c["status"] in {"success", "reused", "manual_edited"})'''

good_chunk_2 = '''                self._save_manifest()

                print(f"vision {record['chunk_id']}: {record['status']}")

        chunk_records.sort(key=lambda x: x.get("chunk_id", ""))
        success = sum(1 for c in chunk_records if c["status"] in {"success", "reused", "manual_edited"})'''

changed = False
if bad_chunk_1 in content:
    content = content.replace(bad_chunk_1, good_chunk_1)
    changed = True
    print('Fixed _enforce_long_video_confirmation')
else:
    print('bad_chunk_1 not found')

if bad_chunk_2 in content:
    content = content.replace(bad_chunk_2, good_chunk_2)
    changed = True
    print('Fixed chunk_records.sort')
else:
    print('bad_chunk_2 not found')

if changed:
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
