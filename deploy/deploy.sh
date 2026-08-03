#!/usr/bin/env bash
#
# deploy.sh — push the current git HEAD to the always-on Linux box as an immutable,
# symlinked release, then atomically switch the systemd service onto it.
#
#   Mac (dev)  --rsync-->  bhyve-linux:~/bhyve/releases/<ts>_<sha>/
#                          flip ~/bhyve/current -> that release, restart bhyve.service
#
# Test-gated: a red suite aborts before the box is touched. config.json / secrets/ live in
# ~/bhyve/shared and are symlinked into each release — they are never copied or overwritten.
#
# P2: land + atomic switch + restart + version-confirm. (Health-check on /api/status +
# automatic rollback arrive in P3.)
#
# One-time prerequisites on the box (see deploy/README.md, plan phases P1 + P2a):
#   - the ~/bhyve/{releases,shared,current} layout, with bhyve.service pointing at ~/bhyve/current
#   - /etc/sudoers.d/bhyve-deploy granting NOPASSWD `systemctl restart bhyve.service`
#
# Overridable via env: BHYVE_REMOTE  BHYVE_BASE  BHYVE_HEALTH_URL  BHYVE_SERVICE
set -euo pipefail

REMOTE="${BHYVE_REMOTE:-bhyve-linux}"
BASE="${BHYVE_BASE:-/home/evans/bhyve}"
HEALTH_URL="${BHYVE_HEALTH_URL:-http://192.168.2.169:8000}"
SERVICE="${BHYVE_SERVICE:-bhyve.service}"

cd "$(dirname "$0")/.."   # repo root (this script lives in deploy/)

bold(){ printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die(){ printf '\033[31m✗ DEPLOY FAILED: %s\033[0m\n' "$*" >&2; exit 1; }

# 1) test gate — never touch the box on a red suite
bold "Test gate: pytest test_e2e.py"
PY=python3; [ -x venv/bin/python ] && PY=venv/bin/python
"$PY" -m pytest test_e2e.py -q || die "tests are red — aborting before touching $REMOTE"

# 2) release id from git HEAD (+ '-dirty' if the working tree has uncommitted changes)
SHA="$(git rev-parse --short HEAD)"
git diff --quiet && git diff --cached --quiet || SHA="${SHA}-dirty"
ID="$(date -u +%Y%m%dT%H%M%SZ)_${SHA}"
REL="$BASE/releases/$ID"
bold "Release: $ID"

# 3) land an immutable copy of the tree (box-local state + build junk excluded)
bold "Rsync -> $REMOTE:$REL"
rsync -az \
  --exclude 'venv/' --exclude '.git/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude 'config.json' --exclude 'secrets/' \
  ./ "$REMOTE:$REL/"

# 4) finalize on the box: symlink shared state in, build a fresh isolated venv, stamp VERSION
bold "Finalize on box (link shared, build venv, stamp VERSION)"
ssh "$REMOTE" REL="$REL" SHA="$SHA" bash -s <<'FINALIZE'
set -euo pipefail
cd "$REL"
ln -sfn ../../shared/config.json config.json
ln -sfn ../../shared/secrets     secrets
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt
printf '%s\n%s\n' "$SHA" "$(date -u +%FT%TZ)" > VERSION
./venv/bin/python -c 'import server'   # smoke: the release imports on its own venv
FINALIZE

# 5) atomic switch: flip the 'current' symlink (all-or-nothing; relative target under $BASE)
bold "Switch: current -> releases/$ID"
ssh "$REMOTE" "ln -sfn 'releases/$ID' '$BASE/current'"

# 6) restart the service onto the new release (needs NOPASSWD sudoers from P2a)
bold "Restart $SERVICE"
ssh "$REMOTE" "sudo systemctl restart '$SERVICE'"

# 7) confirm the box is now serving the new version (pure signal — no BLE device needed)
bold "Confirm: $HEALTH_URL/api/version"
for _ in 1 2 3 4 5; do
  sleep 2
  if OUT="$(curl -fsS --max-time 20 "$HEALTH_URL/api/version" 2>/dev/null)"; then
    echo "$OUT"
    if printf '%s' "$OUT" | grep -q "\"$SHA\""; then
      printf '\033[32m✓ %s is live at %s\033[0m\n' "$ID" "$HEALTH_URL"
      exit 0
    fi
  fi
done
die "service did not report $SHA — inspect: ssh $REMOTE journalctl -u $SERVICE -n 40"
