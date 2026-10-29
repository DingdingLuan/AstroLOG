#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
PIDFILE="$ROOT/var/run/collector.pid"
[[ -f "$PIDFILE" ]] || { echo 'No collector PID file.'; exit 0; }
PID=$(cat "$PIDFILE")
[[ "$PID" =~ ^[0-9]+$ ]] || { echo 'Invalid collector PID file.' >&2; exit 1; }
if ! kill -0 "$PID" 2>/dev/null; then rm -f "$PIDFILE"; echo 'Collector already stopped.'; exit 0; fi
COMMAND=$(ps -p "$PID" -o args= 2>/dev/null || true)
[[ "$COMMAND" == *"$ROOT/.venv/bin/python"* && "$COMMAND" == *"-m logpresent.collector"* ]] || { echo 'PID does not belong to this collector; refusing to stop it.' >&2; exit 1; }
kill -TERM "$PID"
for _ in {1..40}; do
  STATE=$(ps -p "$PID" -o stat= 2>/dev/null || true)
  if ! kill -0 "$PID" 2>/dev/null || [[ "$STATE" == Z* ]]; then
    rm -f "$PIDFILE"; echo 'AstroLOG collector stopped.'; exit 0
  fi
  sleep 0.25
done
echo 'Collector shutdown is still in progress; inspect var/log/collector.log.' >&2
exit 1
