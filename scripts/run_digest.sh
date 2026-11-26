#!/usr/bin/env zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
  PYTHON_BIN="$PROJECT_DIR/.venv/bin/python3"
fi

export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
"$PYTHON_BIN" -m yt_podcast.cli run --config "$PROJECT_DIR/config.toml" "$@"
