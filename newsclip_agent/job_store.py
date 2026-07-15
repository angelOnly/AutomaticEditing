from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from newsclip_agent.utils import ensure_dir, write_json


class JobStore:
    def __init__(self, directory: str | Path):
        self.directory = ensure_dir(Path(directory))

    def save_job(self, job_id: str, job_data: dict[str, Any]) -> None:
        # 走 utils.write_json 的原子写，避免半写入损坏 job json。
        write_json(self.directory / f"{job_id}.json", job_data)
            
    def load_jobs(self) -> dict[str, dict[str, Any]]:
        jobs = {}
        for path in self.directory.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as f:
                    payload = json.load(f)
                job_id = str(payload.get("job_id") or path.stem)
                jobs[job_id] = payload
            except Exception:
                pass
        return jobs

    def delete_job(self, job_id: str) -> None:
        path = self.directory / f"{job_id}.json"
        try:
            if path.exists():
                path.unlink()
        except FileNotFoundError:
            pass
