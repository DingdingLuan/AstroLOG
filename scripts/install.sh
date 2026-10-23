#!/usr/bin/env bash
# All Python packages and caches stay below this checkout; no root required.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"
mkdir -p var/cache/pip var/log var/run
export PIP_CACHE_DIR="$ROOT/var/cache/pip"
PYTHON=${ASTROLOG_PYTHON:-python3}
"$PYTHON" -c 'import sys; sys.exit("AstroLOG requires Python 3.10 or newer.") if sys.version_info < (3, 10) else None'
if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON" -m venv .venv || {
    echo 'Python venv support is required (for example, the python3-venv package on Debian/Ubuntu).' >&2
    exit 1
  }
fi
if ! .venv/bin/python -m pip --version > /dev/null 2>&1; then
  .venv/bin/python -m ensurepip --upgrade || {
    echo 'This Python installation needs pip/ensurepip support before AstroLOG can be installed.' >&2
    exit 1
  }
fi
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
.venv/bin/python -m logpresent --help
echo 'Installed. Next: cp config/demo.yaml config/config.yaml'
