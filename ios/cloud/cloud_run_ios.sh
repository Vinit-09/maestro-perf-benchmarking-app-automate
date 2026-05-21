#!/usr/bin/env bash
# Cloud Maestro iOS benchmark runner — uploads the app + test suite once,
# triggers N builds in parallel (one device per build), polls them all to
# completion, then queries BigQuery for the canonical per-session metrics.
#
# Usage:
#   ./cloud_run_ios.sh -n 100 -t baseline
#   ./cloud_run_ios.sh -n 1   -t calibration
#
# Flags:
#   -n N        number of builds to trigger (default 1)
#   -t TAG      buildTag set on every build (default baseline)
#   -d DEVICE   device specifier (default "iPhone 17-26")
#   -f FLOW     path to the Maestro flow YAML (default benchmark loop)
#   -a APK      path to the .ipa (default HelloBench.ipa)
#   -s          skip the upload step (reuse cached app/test_suite urls)
#
# Requires env: BROWSERSTACK_USERNAME, BROWSERSTACK_ACCESS_KEY

set -u
set -o pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="$ROOT_DIR/results"
mkdir -p "$RESULTS_DIR"

N=1
TAG="baseline"
DEVICE="iPhone 17-26"
FLOW="$ROOT_DIR/flows/ios-benchmark-loop.yaml"
IPA="$ROOT_DIR/../../apps/HelloBench.ipa"
SKIP_UPLOAD=0

while getopts "n:t:d:f:a:s" opt; do
  case "$opt" in
    n) N="$OPTARG" ;;
    t) TAG="$OPTARG" ;;
    d) DEVICE="$OPTARG" ;;
    f) FLOW="$OPTARG" ;;
    a) IPA="$OPTARG" ;;
    s) SKIP_UPLOAD=1 ;;
    *) echo "Unknown opt"; exit 2 ;;
  esac
done

if [[ -z "${BROWSERSTACK_USERNAME:-}" || -z "${BROWSERSTACK_ACCESS_KEY:-}" ]]; then
  echo "ERROR: BROWSERSTACK_USERNAME / BROWSERSTACK_ACCESS_KEY not in env." >&2
  exit 1
fi

API_AUTH=(-u "$BROWSERSTACK_USERNAME:$BROWSERSTACK_ACCESS_KEY")
RUN_ID="cloud_$(date +%Y%m%d_%H%M%S)"
RUN_DIR="$RESULTS_DIR/$RUN_ID"
mkdir -p "$RUN_DIR"
CACHE="$RESULTS_DIR/_cloud_cache.env"
BUILDS_FILE="$RUN_DIR/builds.txt"
SESSIONS_FILE="$RUN_DIR/sessions.txt"

echo "=== Cloud Maestro iOS Benchmark ==="
echo "Run ID:     $RUN_ID"
echo "N builds:   $N"
echo "Device:     $DEVICE"
echo "Flow:       $FLOW"
echo "IPA:        $IPA"
echo "Tag:        $TAG"
echo "Skip upload: $SKIP_UPLOAD"
echo

# Plan probe
PLAN=$(curl -sS "${API_AUTH[@]}" "https://api-cloud.browserstack.com/app-automate/plan.json")
PARALLEL_MAX=$(echo "$PLAN" | python3 -c "import json,sys;print(json.load(sys.stdin)['parallel_sessions_max_allowed'])")
PARALLEL_RUNNING=$(echo "$PLAN" | python3 -c "import json,sys;print(json.load(sys.stdin)['parallel_sessions_running'])")
echo "Plan parallel cap: $PARALLEL_MAX  (currently in use: $PARALLEL_RUNNING)"
echo

# Upload (or reuse cached urls).
if [[ "$SKIP_UPLOAD" == "0" ]]; then
  echo "[upload] zipping flow file..."
  ZIP="$RUN_DIR/test_suite.zip"
  ( cd "$(dirname "$FLOW")" && zip -j "$ZIP" "$(basename "$FLOW")" >/dev/null )

  echo "[upload] iOS app (.ipa)..."
  APP_RESP=$(curl -sS "${API_AUTH[@]}" -X POST \
    "https://api-cloud.browserstack.com/app-automate/maestro/v2/app" \
    -F "file=@$IPA")
  APP_URL=$(echo "$APP_RESP" | python3 -c "import json,sys;print(json.load(sys.stdin)['app_url'])")
  echo "    app_url=$APP_URL"

  echo "[upload] Maestro test suite..."
  TS_RESP=$(curl -sS "${API_AUTH[@]}" -X POST \
    "https://api-cloud.browserstack.com/app-automate/maestro/v2/test-suite" \
    -F "file=@$ZIP")
  TS_URL=$(echo "$TS_RESP" | python3 -c "import json,sys;print(json.load(sys.stdin)['test_suite_url'])")
  echo "    test_suite_url=$TS_URL"

  cat > "$CACHE" <<EOF
APP_URL=$APP_URL
TS_URL=$TS_URL
EOF
else
  echo "[upload] skipping; loading cached urls from $CACHE"
  source "$CACHE"
  echo "    app_url=$APP_URL"
  echo "    test_suite_url=$TS_URL"
fi

# Trigger ONE build with N device entries (BS does not dedupe duplicates;
# verified 2026-05-04 with build b825797062cbb0be9bc5d5a7fe3c7dc3937868fb).
echo
echo "[trigger] firing 1 build with $N device entries..."
DEVICES_JSON=$(python3 -c "import json,sys;print(json.dumps([sys.argv[1]]*int(sys.argv[2])))" "$DEVICE" "$N")
PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
  'devices': json.loads(sys.argv[1]),
  'app': sys.argv[2],
  'testSuite': sys.argv[3],
  'project': 'Maestro Benchmark',
  'buildName': sys.argv[4],
  'buildTag': sys.argv[5],
  'deviceLogs': True,
  'video': True,
}))
" "$DEVICES_JSON" "$APP_URL" "$TS_URL" "$RUN_ID" "$TAG")

RESP=$(curl -sS "${API_AUTH[@]}" -X POST \
  "https://api-cloud.browserstack.com/app-automate/maestro/v2/ios/build" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")
BID=$(echo "$RESP" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('build_id',''))" 2>/dev/null || echo "")
if [[ -z "$BID" ]]; then
  echo "ERROR: trigger failed: $RESP" >&2
  exit 1
fi
echo "$BID" > "$BUILDS_FILE"
echo "    build_id=$BID"

# Confirm BS spawned $N sessions before we wait $N * ~12 min.
INITIAL=$(curl -sS "${API_AUTH[@]}" "https://api-cloud.browserstack.com/app-automate/maestro/v2/builds/$BID")
SESS_COUNT=$(echo "$INITIAL" | python3 -c "
import json, sys
d = json.load(sys.stdin)
n = sum(len(dev.get('sessions', [])) for dev in d.get('devices', []))
print(n)
")
echo "    BS spawned $SESS_COUNT sessions (requested $N)"
echo "$INITIAL" > "$RUN_DIR/initial_status.json"

# Poll the single build until terminal, write each session row to SESSIONS_FILE.
echo
echo "[poll] waiting for build $BID to finish..."
> "$SESSIONS_FILE"
while :; do
  BJ=$(curl -sS "${API_AUTH[@]}" "https://api-cloud.browserstack.com/app-automate/maestro/v2/builds/$BID")
  BSTATUS=$(echo "$BJ" | python3 -c "import json,sys;print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "?")
  COUNTS=$(echo "$BJ" | python3 -c "
import json, sys
d = json.load(sys.stdin)
counts = {'running': 0, 'queued': 0, 'passed': 0, 'failed': 0, 'other': 0}
for dev in d.get('devices', []):
    for s in dev.get('sessions', []):
        st = s.get('status', 'other')
        counts[st] = counts.get(st, 0) + 1
print(' '.join(f'{k}={v}' for k, v in counts.items() if v > 0))
")
  echo "    build=$BSTATUS sessions: $COUNTS"
  if [[ "$BSTATUS" != "running" && "$BSTATUS" != "queued" ]]; then
    echo "$BJ" > "$RUN_DIR/$BID.json"
    echo "$BJ" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for dev in d.get('devices', []):
    for s in dev.get('sessions', []):
        print(f\"{s['id']},{d['id']},{s['status']}\")
" > "$SESSIONS_FILE"
    break
  fi
  sleep 30
done
echo "    all builds terminal"
echo

# Summary
PASSED=$(awk -F, '$3=="passed"' "$SESSIONS_FILE" | wc -l | tr -d ' ')
FAILED=$(awk -F, '$3!="passed"' "$SESSIONS_FILE" | wc -l | tr -d ' ')
echo "    passed=$PASSED failed=$FAILED total=$(wc -l < "$SESSIONS_FILE" | tr -d ' ')"
echo
echo "Sessions written to: $SESSIONS_FILE"
echo "Next: ./cloud_query_bq.sh $RUN_ID  (queries BQ for these session ids)"
