#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"
mkdir -p var/log var/run
PIDFILE="$ROOT/var/run/demo.pid"
if [[ -f "$PIDFILE" ]]; then
  PID=$(cat "$PIDFILE")
  if [[ "$PID" =~ ^[0-9]+$ ]] && kill -0 "$PID" 2>/dev/null; then
    COMMAND=$(ps -p "$PID" -o args= 2>/dev/null || true)
    if [[ "$COMMAND" == *"$ROOT/scripts/demo_logs.py"* ]]; then
      echo "Demo producer already running: PID=$PID"; exit 0
    fi
    echo 'Recorded PID belongs to another process; inspect var/run/demo.pid before starting.' >&2
    exit 1
  fi
fi
[[ -x "$ROOT/.venv/bin/python" ]] || { echo 'Run bash scripts/install.sh first.' >&2; exit 1; }
nohup "$ROOT/.venv/bin/python" -u "$ROOT/scripts/demo_logs.py" > "$ROOT/var/log/demo.log" 2>&1 < /dev/null &
PID=$!
printf '%s\n' "$PID" > "$PIDFILE"
for _ in {1..30}; do
  if ! kill -0 "$PID" 2>/dev/null; then rm -f "$PIDFILE"; tail -n 20 var/log/demo.log >&2; exit 1; fi
  if [[ -s var/demo/simulation/run.log && -s var/demo/diagnostics/cpu.log && -s var/demo/diagnostics/events.log ]]; then
    echo "Three synthetic demo logs updating every 3 seconds: PID=$PID"
    exit 0
  fi
  sleep 0.2
done
echo 'Demo producer is not ready; inspect var/log/demo.log.' >&2
exit 1
