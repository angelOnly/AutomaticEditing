from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


LOCK_DIR = Path("outputs/.locks")


@contextmanager
def file_slot_lock(name: str, slots: int = 1, poll_seconds: float = 1.0, stale_after_seconds: float = 12 * 60 * 60) -> Iterator[None]:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    acquired: Path | None = None
    try:
        while acquired is None:
            for index in range(max(1, int(slots))):
                path = LOCK_DIR / f"{name}_{index}.lock"
                try:
                    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(f"{os.getpid()}\n{time.time()}\n")
                    acquired = path
                    break
                except FileExistsError:
                    _remove_stale_lock(path, stale_after_seconds)
                    continue
            if acquired is None:
                time.sleep(poll_seconds)
        yield
    finally:
        if acquired:
            try:
                acquired.unlink()
            except FileNotFoundError:
                pass


def _remove_stale_lock(path: Path, stale_after_seconds: float) -> None:
    is_dead = False
    try:
        content = path.read_text(encoding="utf-8").splitlines()
        if content:
            pid = int(content[0].strip())
            try:
                os.kill(pid, 0)
            except OSError:
                is_dead = True
    except Exception:
        pass

    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return
        
    if not is_dead and age < stale_after_seconds:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
