#!/usr/bin/env bash
#
# rollback.sh — flip the box back to the previous release and restart, with a health check.
# Use when a deploy went out that you want to undo (deploy.sh already auto-rolls-back a release
# that fails its own health check; this is the manual "the last one was bad after all" button).
#
# Overridable via env: BHYVE_REMOTE  BHYVE_BASE  BHYVE_HEALTH_URL  BHYVE_SERVICE
set -euo pipefail

REMOTE="${BHYVE_REMOTE:-bhyve-linux}"
BASE="${BHYVE_BASE:-/home/evans/bhyve}"
HEALTH_URL="${BHYVE_HEALTH_URL:-http://192.168.2.169:8000}"
SERVICE="${BHYVE_SERVICE:-bhyve.service}"

bold(){ printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die(){ printf '\033[31m✗ ROLLBACK FAILED: %s\033[0m\n' "$*" >&2; exit 1; }

health_ok(){                                   # $1 = expected short sha
  local want="$1" out
  for _ in $(seq 1 15); do
    sleep 2
    out="$(curl -fsS --max-time 10 "$HEALTH_URL/api/version" 2>/dev/null || true)"
    if printf '%s' "$out" | grep -q "\"$want\""; then
      curl -fsS -o /dev/null --max-time 10 "$HEALTH_URL/" 2>/dev/null && return 0
    fi
  done
  return 1
}

cur="$(ssh "$REMOTE" "basename \"\$(readlink '$BASE/current')\"")"
prev="$(ssh "$REMOTE" "cd '$BASE/releases' && ls -1dt */ 2>/dev/null | sed 's#/##' | grep -vx '$cur' | head -1")"
[ -n "$prev" ] || die "no previous release to roll back to (current: $cur)"

bold "Rollback: $cur -> $prev"
ssh "$REMOTE" "ln -sfn 'releases/$prev' '$BASE/current'"
ssh "$REMOTE" "sudo systemctl restart '$SERVICE'"

if health_ok "${prev##*_}"; then
  curl -s "$HEALTH_URL/api/version"; echo
  printf '\033[32m✓ rolled back to %s\033[0m\n' "$prev"
  exit 0
fi
die "rolled the symlink to $prev but it did not come up healthy — inspect: ssh $REMOTE journalctl -u $SERVICE -n 40"
