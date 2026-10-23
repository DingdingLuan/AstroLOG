#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
PIDFILE="$ROOT/var/run/demo.pid"
[[ -f "$PIDFILE" ]] || { echo 'No demo producer PID recorded.'; exit 0; }
PID=$(cat "$PIDFILE")
[[ "$PID" =~ ^[0-9]+$ ]] || { echo 'Invalid demo PID.' >&2; exit 1; }
if ! kill -0 "$PID" 2>/dev/null; then rm -f "$PIDFILE"; echo 'Demo producer already stopped.'; exit 0; fi
COMMAND=$(ps -p "$PID" -o args= 2>/dev/null || true)
if [[ "$COMMAND" != *"$ROOT/scripts/demo_logs.py"* ]]; then
  echo 'PID does not belong to this project demo producer; refusing to stop it.' >&2; exit 1
fi
kill -TERM "$PID"
for _ in {1..50}; do
  STATE=$(ps -p "$PID" -o stat= 2>/dev/null || true)
  if ! kill -0 "$PID" 2>/dev/null || [[ "$STATE" == Z* ]]; then rm -f "$PIDFILE"; echo 'Demo producer stopped.'; exit 0; fi
  sleep 0.1
done
echo 'Demo producer has not stopped yet.' >&2
exit 1
