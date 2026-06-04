from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from newsclip_agent.utils import ensure_dir


class JobStore:
    def __init__(self, directory: str | Path):
        self.directory = ensure_dir(Path(directory))
    
    def save_job(self, task_id: str, job_data: dict[str, Any]) -> None:
        path = self.directory / f"{task_id}.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump(job_data, f, ensure_ascii=False, indent=2)
            
    def load_jobs(self) -> dict[str, dict[str, Any]]:
        jobs = {}
        for path in self.directory.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as f:
                    jobs[path.stem] = json.load(f)
            except Exception:
                pass
        return jobs

    def delete_job(self, task_id: str) -> None:
        path = self.directory / f"{task_id}.json"
        try:
            if path.exists():
                path.unlink()
        except FileNotFoundError:
            pass
