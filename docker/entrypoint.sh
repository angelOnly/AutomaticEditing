#!/usr/bin/env bash
set -euo pipefail

cd /workspace

if [ ! -f "/workspace/web_app.py" ]; then
  echo "ERROR: /workspace does not contain web_app.py."
  echo "Mount your project code with: -v /path/to/AutomaticEditing:/workspace"
  exit 1
fi

if [ ! -e "/workspace/models" ]; then
  ln -s /opt/automatic-editing/models /workspace/models
fi

if [ ! -e "/workspace/tts_ref" ]; then
  ln -s /opt/automatic-editing/tts_ref /workspace/tts_ref
fi

mkdir -p /workspace/videos /workspace/outputs

exec "$@"
