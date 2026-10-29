#!/usr/bin/env bash
# Starts the optional SSH collector; the viewer is managed separately.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"
mkdir -p var/log var/run
PIDFILE="$ROOT/var/run/collector.pid"
if [[ -f "$PIDFILE" ]]; then
  PID=$(cat "$PIDFILE")
  if [[ "$PID" =~ ^[0-9]+$ ]] && kill -0 "$PID" 2>/dev/null; then
    COMMAND=$(ps -p "$PID" -o args= 2>/dev/null || true)
    if [[ "$COMMAND" == *"$ROOT/.venv/bin/python"* && "$COMMAND" == *"-m logpresent.collector"* ]]; then
      echo "AstroLOG collector already running: PID=$PID"; exit 0
    fi
    echo 'Recorded PID belongs to another process; inspect var/run/collector.pid.' >&2
    exit 1
  fi
fi
CONFIG=${ASTROLOG_COLLECTOR_CONFIG:-$ROOT/config/collector.yaml}
[[ -x "$ROOT/.venv/bin/python" ]] || { echo 'Run bash scripts/install.sh first.' >&2; exit 1; }
[[ -f "$CONFIG" ]] || { echo "Collector configuration not found: $CONFIG" >&2; exit 1; }
PID=$("$ROOT/.venv/bin/python" - "$ROOT" "$CONFIG" <<'PY'
import os
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1])
with (root / "var/log/collector.log").open("wb") as output:
    child = subprocess.Popen(
        [str(root / ".venv/bin/python"), "-m", "logpresent.collector", "--config", sys.argv[2]],
        cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"}, start_new_session=True,
    )
print(child.pid)
PY
)
printf '%s\n' "$PID" > "$PIDFILE"
sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$PIDFILE"
  tail -n 30 "$ROOT/var/log/collector.log" >&2
  exit 1
fi
echo "AstroLOG collector started: PID=$PID"
echo 'Check collector-status.json in its configured local_root for SSH connection state.'
echo "Service log: $ROOT/var/log/collector.log"
