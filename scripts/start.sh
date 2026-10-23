#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"
mkdir -p var/log var/run
PIDFILE="$ROOT/var/run/server.pid"
if [[ -f "$PIDFILE" ]]; then
  PID=$(cat "$PIDFILE")
  if [[ "$PID" =~ ^[0-9]+$ ]] && kill -0 "$PID" 2>/dev/null; then
    COMMAND=$(ps -p "$PID" -o args= 2>/dev/null || true)
    if [[ "$COMMAND" == *"$ROOT/.venv/bin/python"* && "$COMMAND" == *"-m logpresent"* ]]; then
      echo "AstroLOG already running: PID=$PID"; exit 0
    fi
    echo 'Recorded PID belongs to another process; inspect var/run/server.pid before starting.' >&2
    exit 1
  fi
fi
CONFIG=${LOGPRESENT_CONFIG:-$ROOT/config/config.yaml}
[[ -x "$ROOT/.venv/bin/python" ]] || { echo 'Run bash scripts/install.sh first.' >&2; exit 1; }
[[ -f "$CONFIG" ]] || { echo "Configuration not found: $CONFIG" >&2; echo 'First copy config/demo.yaml or config/example.yaml to config/config.yaml.' >&2; exit 1; }
BIND=$("$ROOT/.venv/bin/python" - "$CONFIG" <<'PY'
import sys
from logpresent.config import load_config, resolve_server

try:
    server = resolve_server(load_config(sys.argv[1]).server)
except (OSError, ValueError) as exc:
    raise SystemExit(str(exc))
print(server.host, server.port)
PY
)
read -r HOST PORT <<< "$BIND"
PROBE_HOST=$HOST
[[ "$HOST" != 0.0.0.0 ]] || PROBE_HOST=127.0.0.1
[[ "$HOST" != :: ]] || PROBE_HOST=::1
[[ "$PROBE_HOST" != *:* ]] || PROBE_HOST="[$PROBE_HOST]"
nohup env PYTHONUNBUFFERED=1 "$ROOT/.venv/bin/python" -m logpresent --config "$CONFIG" --host "$HOST" --port "$PORT" > "$ROOT/var/log/server.log" 2>&1 < /dev/null &
PID=$!
printf '%s\n' "$PID" > "$PIDFILE"
for _ in {1..40}; do
  if ! kill -0 "$PID" 2>/dev/null; then rm -f "$PIDFILE"; tail -n 35 "$ROOT/var/log/server.log" >&2; exit 1; fi
  if "$ROOT/.venv/bin/python" - "$PROBE_HOST" "$PORT" <<'PY' > /dev/null 2>&1
import json
import sys
import urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(f"http://{sys.argv[1]}:{sys.argv[2]}/healthz", timeout=1) as response:
    if json.load(response).get("status") != "ok":
        raise SystemExit(1)
PY
  then
    if ! kill -0 "$PID" 2>/dev/null; then rm -f "$PIDFILE"; tail -n 35 "$ROOT/var/log/server.log" >&2; exit 1; fi
    echo "AstroLOG running: PID=$PID URL=http://$PROBE_HOST:$PORT/"
    echo "Service log: $ROOT/var/log/server.log"
    exit 0
  fi
  sleep 0.25
done
echo 'AstroLOG has not become ready; inspect var/log/server.log.' >&2
exit 1
