#!/usr/bin/env bash
set -euo pipefail

cd /workspace

if [ ! -f "/workspace/web_app.py" ]; then
  echo "ERROR: /workspace does not contain web_app.py."
  echo "Mount your project code with: -v /path/to/AutomaticEditing:/workspace"
  exit 1
fi

# The Seedream prompt skill is versioned application code. Resolve its configured
# path from config.toml; never fall back to a host user's global Agent skills.
python - <<'PY'
from pathlib import Path
import sys
import tomllib

root = Path("/workspace")
try:
    config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    configured = str((config.get("cover_generation") or {}).get("skill_path") or "").strip()
    skill_path = Path(configured)
    if not skill_path.is_absolute():
        skill_path = root / skill_path
    required = (
        skill_path,
        skill_path.parent / "README.md",
        skill_path.parent / "scripts" / "generate_image.py",
    )
except Exception as exc:
    print(f"ERROR: unable to resolve cover_generation.skill_path: {exc}", file=sys.stderr)
    raise SystemExit(1)

missing = [str(path) for path in required if not path.is_file()]
if missing:
    print("ERROR: /workspace is missing the checked-in Seedream Skill:", file=sys.stderr)
    print("\n".join(missing), file=sys.stderr)
    raise SystemExit(1)
PY

if [ ! -e "/workspace/models" ]; then
  ln -s /opt/automatic-editing/models /workspace/models
fi

if [ ! -e "/workspace/tts_ref" ]; then
  ln -s /opt/automatic-editing/tts_ref /workspace/tts_ref
fi

mkdir -p /workspace/videos /workspace/outputs

exec "$@"
