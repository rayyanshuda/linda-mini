#!/usr/bin/env bash
# Repeatable demo: for each scenario, start an ISOLATED portal (temp DB, fault mode, own
# port), run the agent, and copy results into sample_results/<scenario>/.
# Does not touch ./portal.sqlite3 or a portal currently already running on :8000.
#
#   scripts/demo.sh            # all scenarios
#   scripts/demo.sh normal     # one scenario
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
OUT="${OUT:-sample_results}"
PORT="${PORT:-8765}"
SCENARIOS=("$@")
[ $# -eq 0 ] && SCENARIOS=(normal download_503_once session_expire_on_route_once route_commit_then_503_once crash_resume)

run_scenario() {
  local name="$1" fault="none" work
  work="$(mktemp -d)"
  case "$name" in
    download_503_once|session_expire_on_route_once|route_commit_then_503_once) fault="$name" ;;
  esac
  echo "=== scenario: $name (LINDA_FAULT_MODE=$fault) ==="
  LINDA_DB_PATH="$work/portal.sqlite3" LINDA_FAULT_MODE="$fault" \
    "$PY" -m uvicorn app:app --host 127.0.0.1 --port "$PORT" >"$work/portal.log" 2>&1 &
  local pid=$!
  trap "kill $pid 2>/dev/null || true" RETURN
  for _ in $(seq 50); do curl -sf "http://127.0.0.1:$PORT/login" >/dev/null && break; sleep 0.2; done

  local args=(-m agent run --base-url "http://127.0.0.1:$PORT" --state-dir "$work/state")
  if [ "$name" = crash_resume ]; then
    echo "--- run 1: hard crash injected right after clicking OK on FX-203 ---"
    AGENT_CRASH_AT="after_submit_click:FX-203" "$PY" "${args[@]}" 2>"$work/agent-run1.log" || echo "(exit $? as injected)"
    "$PY" -c "import json;s=json.load(open('$work/state/state.json'))['faxes']['FX-203'];print('FX-203 checkpoint state:',s['state'])"
    echo "--- run 2: resume ---"
  fi
  "$PY" "${args[@]}" 2>"$work/agent.log" || true
  grep -h ": FINAL\|submit attempt\|resuming\|error in" "$work"/agent-run1.log "$work"/agent.log 2>/dev/null | sed 's/^/  /' || true
  echo "--- run again: idempotency check (expect no portal actions) ---"
  "$PY" "${args[@]}" 2>"$work/agent-rerun.log" || true
  echo "  $(grep -c 'already final' "$work/agent-rerun.log") faxes already final; $(grep -c 'submit attempt\|downloaded' "$work/agent-rerun.log" || true) actions taken"

  mkdir -p "$OUT/$name"
  cp "$work/state/results.json" "$work/state/results.csv" "$work/state/events.jsonl" "$OUT/$name/"
  cp "$work"/agent*.log "$OUT/$name/" 2>/dev/null || true
  "$PY" -c "import json;d=json.load(open('$OUT/$name/results.json'));print('summary:',d['summary'])"
  kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true
  trap - RETURN
}

for s in "${SCENARIOS[@]}"; do run_scenario "$s"; done
echo "results in $OUT/"
