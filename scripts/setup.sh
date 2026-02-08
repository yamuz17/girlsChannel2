#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3.11}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "[ERROR] ${PYTHON_BIN} not found. Install Python 3.11 or set PYTHON_BIN." >&2
  exit 1
fi

"${PYTHON_BIN}" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install -U pip
python -m pip install -r requirements.lock.txt

if python - <<'PY'
import sys
from pathlib import Path
req = Path('requirements.lock.txt').read_text(encoding='utf-8')
print('playwright' in req)
PY
then
  # macOS 13 では webkit が非対応のため、chromium のみに限定
  python -m playwright install chromium
fi

echo "[OK] setup complete"
