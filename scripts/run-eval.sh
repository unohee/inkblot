#!/usr/bin/env bash
# Cron entry point for the harness eval.
#
# Layering is deliberate: the free static pass runs every fire, the paid
# behavioural pass only when asked. A cron that burns tokens on every tick gets
# turned off, and then nothing is measured at all.
#
#   scripts/run-eval.sh               static only  (lint + drift + unit tests)
#   scripts/run-eval.sh --behavioural static, then selftest, then the real cases
#
# Exit codes: 0 clean, 1 findings or failures, 2 another run holds the lock.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="${TMPDIR:-/tmp}/harness-eval.lock"
LOG_DIR="$REPO/results/cron"
STAMP="$(date +%Y%m%dT%H%M%S)"

# mkdir is atomic on every filesystem that matters here, unlike flock on macOS.
if ! mkdir "$LOCK" 2>/dev/null; then
    echo "another harness-eval run holds $LOCK — skipping this fire"
    exit 2
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$STAMP.log"
exec > >(tee -a "$LOG") 2>&1

echo "=== harness-eval $STAMP ($REPO) ==="
cd "$REPO" || exit 1

# shellcheck disable=SC1090
# Optionally activate a virtualenv named by the caller (cron has no shell rc).
[ -n "${VENV_ACTIVATE:-}" ] && [ -f "$VENV_ACTIVATE" ] && source "$VENV_ACTIVATE"

status=0

echo; echo "--- unit tests ---"
python3 -m pytest || status=1

echo; echo "--- static no-op lint ---"
PYTHONPATH=src python3 -m harness_eval.cli lint || status=1

echo; echo "--- drift vs the live harness ---"
PYTHONPATH=src python3 -m harness_eval.cli drift || status=1

if [ "${1:-}" = "--behavioural" ]; then
    echo; echo "--- selftest (rig validation) ---"
    if PYTHONPATH=src python3 -m harness_eval.cli selftest; then
        echo; echo "--- eval ---"
        PYTHONPATH=src python3 -m harness_eval.cli run || status=1
    else
        echo "selftest failed — refusing to run cases, every verdict would be noise"
        status=1
    fi
fi

echo; echo "=== done (status $status) — $LOG ==="
exit "$status"
