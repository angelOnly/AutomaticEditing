import sys
import re

path = r'e:\ai\skills\AutomaticEditing\newsclip_agent\pipeline.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Replace _mark_running
s1 = """    def _mark_running(self, step: str) -> None:
        self.manifest["status"] = "running"
        self.manifest.setdefault("steps", {})[step] = {
            "step_name": step,
            "status": "running",
            "updated_at": now_iso(),
            "can_rerun": False,
        }
        self._save_manifest()"""

r1 = """    def _mark_running(self, step: str) -> None:
        self.manifest["status"] = "running"
        previous = self.manifest.setdefault("steps", {}).get(step, {})
        started_at = previous.get("started_at") or now_iso()
        self.manifest.setdefault("steps", {})[step] = {
            "step_name": step,
            "status": "running",
            "started_at": started_at,
            "updated_at": now_iso(),
            "can_rerun": False,
        }
        self._save_manifest()"""
content = content.replace(s1, r1)

# Replace _record_step
s2 = """    def _record_step(
        self,
        *,
        step: str,
        version: str,
        status: str,
        output: str | None,
        input_hash: str,
        output_files: list[Path] | None = None,
        extra: dict[str, Any] | None = None,
        mark_downstream_stale: bool = True,
    ) -> None:
        data = {
            "step_name": step,
            "version": version,
            "status": status,
            "updated_at": now_iso(),
            "input_hash": input_hash,
            "output_hash": output_hash(output_files or []) if output_files else "",
            "depends_on": DEPENDENCIES.get(step, []),
            "output": output,
            "can_rerun": True,
        }"""

r2 = """    def _record_step(
        self,
        *,
        step: str,
        version: str,
        status: str,
        output: str | None,
        input_hash: str,
        output_files: list[Path] | None = None,
        extra: dict[str, Any] | None = None,
        mark_downstream_stale: bool = True,
    ) -> None:
        previous = self.manifest.setdefault("steps", {}).get(step, {})
        finished_at = now_iso()
        data = {
            "step_name": step,
            "version": version,
            "status": status,
            "started_at": previous.get("started_at", ""),
            "finished_at": finished_at,
            "updated_at": finished_at,
            "input_hash": input_hash,
            "output_hash": output_hash(output_files or []) if output_files else "",
            "depends_on": DEPENDENCIES.get(step, []),
            "output": output,
            "can_rerun": True,
        }"""
content = content.replace(s2, r2)

# Replace step_vision chunk logic
s3 = """        max_workers = max(1, int(self.options.vision_max_workers or 1))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_one_chunk, chunk) for chunk in chunks_to_process]
            for future in as_completed(futures):
                record = future.result()
                chunk_records.append(record)
                print(f"vision {record['chunk_id']}: {record['status']}")"""

r3 = """        max_workers = max(1, int(self.options.vision_max_workers or 1))
        total_chunks = len(chunks_to_process) + len(chunk_records)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_one_chunk, chunk) for chunk in chunks_to_process]
            for future in as_completed(futures):
                record = future.result()
                chunk_records.append(record)

                success = sum(1 for c in chunk_records if c["status"] in {"success", "reused", "manual_edited"})
                failed = sum(1 for c in chunk_records if c["status"] == "failed")
                processed = len(chunk_records)

                self.manifest.setdefault("steps", {}).setdefault("vision", {})
                self.manifest["steps"]["vision"].update(
                    {
                        "step_name": "vision",
                        "status": "running",
                        "updated_at": now_iso(),
                        "can_rerun": False,
                        "current_chunk": record.get("chunk_id", ""),
                        "summary": {
                            "total_chunks": total_chunks,
                            "processed_chunks": processed,
                            "success_chunks": success,
                            "failed_chunks": failed,
                        },
                    }
                )
                self._save_manifest()

                print(f"vision {record['chunk_id']}: {record['status']}")"""
content = content.replace(s3, r3)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Patcher completed successfully!")
