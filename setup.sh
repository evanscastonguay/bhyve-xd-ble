#!/usr/bin/env bash
# One-time setup for bhyve-xd-ble. Creates a local venv and installs deps.
# Usage:  ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> bhyve-xd-ble setup"

# 1. Find a Python 3 interpreter.
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "ERROR: Python 3.9+ not found."
  echo "  Install it (macOS): brew install python   — or from https://www.python.org/downloads/"
  exit 1
fi
echo "==> using $($PY --version) ($PY)"

# 2. Create the venv (idempotent).
if [ ! -d venv ]; then
  echo "==> creating venv/"
  "$PY" -m venv venv
fi

# 3. Install dependencies.
echo "==> installing dependencies"
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt

# 4. Sanity: run the offline tests (no device needed).
echo "==> running offline self-tests"
ok=1
for t in selftest_offline test_onboarding test_cli; do
  if [ -f "$t.py" ]; then
    ./venv/bin/python "$t.py" >/dev/null 2>&1 && echo "    $t: OK" || { echo "    $t: FAILED"; ok=0; }
  fi
done
[ "$ok" = 1 ] || { echo "ERROR: offline tests failed — setup incomplete"; exit 1; }

echo
echo "==> Setup complete. Use the ./bhyve wrapper:"
echo "      ./bhyve login          # one-time: email+password -> config.json"
echo "      ./bhyve status         # read state"
echo "      ./bhyve start 1 300    # start zone 1 for 300s"
echo "      ./bhyve stop           # stop all"
echo
echo "   (Tip: press a button on the timer to wake it before a command.)"
