#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
PIDFILE="$ROOT/var/run/server.pid"
[[ -f "$PIDFILE" ]] || { echo 'No detached-server PID file.'; exit 0; }
PID=$(cat "$PIDFILE")
[[ "$PID" =~ ^[0-9]+$ ]] || { echo 'Invalid PID file.' >&2; exit 1; }
if ! kill -0 "$PID" 2>/dev/null; then rm -f "$PIDFILE"; echo 'Server already stopped.'; exit 0; fi
CMD=$(ps -p "$PID" -o args= 2>/dev/null || true)
[[ "$CMD" == *"$ROOT/.venv/bin/python"* && "$CMD" == *"-m logpresent"* ]] || { echo 'PID does not belong to this AstroLOG server; refusing to stop it.' >&2; exit 1; }
kill -TERM "$PID"
for _ in {1..40}; do
  STATE=$(ps -p "$PID" -o stat= 2>/dev/null || true)
  if ! kill -0 "$PID" 2>/dev/null || [[ "$STATE" == Z* ]]; then rm -f "$PIDFILE"; echo 'AstroLOG stopped.'; exit 0; fi
  sleep 0.25
done
echo 'Shutdown still in progress; inspect the process and service log.' >&2
exit 1
