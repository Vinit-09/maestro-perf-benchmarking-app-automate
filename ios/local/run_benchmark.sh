#!/usr/bin/env bash
# Maestro iOS local benchmark runner. Mirrors android/local/run_benchmark.sh
# but uses xcrun devicectl + xctrace instead of adb. Captures the same
# per-session column schema as the Android local CSV.
#
# Usage:
#   ./run_benchmark.sh [-i ITERATIONS] [-f FLOW_YAML] [-a APP_BUNDLE] [-b BUNDLE_ID] [-t TAG] [-k]
#
# Defaults: 1 iter (smoke) of ios-benchmark-loop.yaml against the HelloBench
# .app bundle in the local Debug-iphoneos build dir, tagged "smoke".
#
# Flags:
#   -i N        iterations (default 1)
#   -f FLOW     flow YAML path
#   -a APP      .app bundle (NOT .ipa) — what xcrun devicectl install consumes
#   -b BID      bundle id (used for uninstall)
#   -t TAG      CSV tag
#   -k          keep app installed between iters (skip uninstall+reinstall)

set -u
set -o pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
MAESTRO="${MAESTRO:-/Users/vinits/.maestro/bin/maestro}"

ITERATIONS=1
FLOW="$ROOT_DIR/flows/ios-benchmark-loop.yaml"
APP="$ROOT_DIR/HelloBench/build/dd/Build/Products/Debug-iphoneos/HelloBench.app"
BUNDLE_ID="com.vinitg.HelloBench"
TAG="smoke"
KEEP_INSTALLED=0
APPLE_TEAM_ID="${APPLE_TEAM_ID:-33MLQVU859}"

while getopts "i:f:a:b:t:T:k" opt; do
  case "$opt" in
    i) ITERATIONS="$OPTARG" ;;
    f) FLOW="$OPTARG" ;;
    a) APP="$OPTARG" ;;
    b) BUNDLE_ID="$OPTARG" ;;
    t) TAG="$OPTARG" ;;
    T) APPLE_TEAM_ID="$OPTARG" ;;
    k) KEEP_INSTALLED=1 ;;
    *) echo "Unknown opt"; exit 2 ;;
  esac
done

RESULTS_DIR="$ROOT_DIR/results"
mkdir -p "$RESULTS_DIR"
RUN_ID="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="$RESULTS_DIR/$RUN_ID"
mkdir -p "$RUN_DIR"


# --- Device probe ------------------------------------------------------------
# Pull devicectl output and grab the first connected iPhone's identifier (the
# core-device UUID, NOT the iOS UDID — install/uninstall use the core-device id)
DEVICE_LINE=$(xcrun devicectl list devices 2>/dev/null | awk '/iPhone/ && /connected/ { print; exit }')
if [[ -z "$DEVICE_LINE" ]]; then
  echo "No connected iPhone found (xcrun devicectl)." >&2
  exit 1
fi
DEVICE_ID=$(echo "$DEVICE_LINE" | awk '{print $3}')
DEVICE_MODEL=$(echo "$DEVICE_LINE" | awk -F '   *' '{print $5}' | sed 's/ *$//' | tr -d ',')

# Pull iOS version + true UDID from xctrace (devicectl identifier ≠ udid)
XCTRACE_LINE=$(xcrun xctrace list devices 2>&1 | awk '/^iPhone \(/{print; exit}')
DEVICE_UDID=$(echo "$XCTRACE_LINE" | sed -E 's/.*\(([0-9A-Fa-f-]{20,})\).*/\1/')
DEVICE_OS=$(echo "$XCTRACE_LINE"   | sed -E 's/^iPhone \(([0-9.]+)\).*/\1/')

# Prevent macOS from sleeping during the run.
CAFFEINATE_PID=""
if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -i -m -s &
  CAFFEINATE_PID=$!
fi

# ----- PR #2856 real-device runtime model -----
# Maestro 2.5.0's auto-spawn iOS-real-device path is broken (NotImplementedError
# on launchApp, runner crashes on tap). Replaced with the manual model from
# PR #2856:
#   1) start XCTest runner ourselves via `xcodebuild test-without-building`
#      (runner binds 0.0.0.0:22087 thanks to the PR's Swift fix)
#   2) forward Mac:22087 -> Device:22087 via iproxy --udid
#   3) tell the patched maestro CLI to skip its own bootstrap by passing
#      --port 22087 --device <UDID>; it connects to the running runner
RUNNER_XCTESTRUN="${RUNNER_XCTESTRUN:-/tmp/maestro-pr2856/maestro-ios-xctest-runner/Build/Build/Products/maestro-driver-ios_iphoneos26.4-arm64.xctestrun}"
RUNNER_PORT="${RUNNER_PORT:-22087}"

if [[ ! -f "$RUNNER_XCTESTRUN" ]]; then
  echo "ERROR: xctestrun missing at $RUNNER_XCTESTRUN" >&2
  echo "Build it once with: cd /tmp/maestro-pr2856/maestro-ios-xctest-runner && xcodebuild build-for-testing -project maestro-driver-ios.xcodeproj -scheme maestro-driver-ios -destination 'platform=iOS,id=$DEVICE_UDID' -derivedDataPath Build DEVELOPMENT_TEAM=$APPLE_TEAM_ID -allowProvisioningUpdates" >&2
  exit 1
fi

pkill -f "iproxy ${RUNNER_PORT}" 2>/dev/null || true
pkill -f "pymobiledevice3 usbmux forward ${RUNNER_PORT}" 2>/dev/null || true
pkill -f "test-without-building.*maestro-driver-ios" 2>/dev/null || true

echo "[xctest] starting test runner via xcodebuild test-without-building..."
XCB_LOG="$RUN_DIR/xcodebuild.log"
xcodebuild test-without-building \
  -xctestrun "$RUNNER_XCTESTRUN" \
  -destination "id=$DEVICE_UDID" \
  -derivedDataPath "$RUN_DIR/_xcodebuild_dd" >"$XCB_LOG" 2>&1 &
XCB_PID=$!
echo "[xctest] xcodebuild pid=$XCB_PID, log=$XCB_LOG"

echo "[bridge] starting pymobiledevice3 forward: localhost:${RUNNER_PORT} -> device:${RUNNER_PORT} (udid=$DEVICE_UDID)"
# pymobiledevice3 replaces iproxy here. iproxy was observed to drop the
# tunnel after ~7 min of continuous traffic; pymobiledevice3 holds for the
# full session (validated 2026-05-08, smoke run 20260508_173000+).
PYMD3="${PYMD3:-/Users/vinits/perf_bench_maestro/.venv/bin/pymobiledevice3}"
"$PYMD3" usbmux forward "$RUNNER_PORT" "$RUNNER_PORT" --udid "$DEVICE_UDID" >"$RUN_DIR/iproxy.log" 2>&1 &
IPROXY_PID=$!
echo "[bridge] pid=$IPROXY_PID"

trap 'kill $CAFFEINATE_PID $IPROXY_PID $XCB_PID 2>/dev/null || true' EXIT

echo -n "[xctest] waiting for runner to serve on :$RUNNER_PORT ..."
for i in $(seq 1 90); do
  if curl -s -o /dev/null -m 1 "http://127.0.0.1:$RUNNER_PORT/status" 2>/dev/null; then
    echo " ready (after ${i}s)"
    break
  fi
  if ! kill -0 "$XCB_PID" 2>/dev/null; then
    echo " xcodebuild died early, see $XCB_LOG" >&2
    exit 1
  fi
  sleep 1
  if [[ "$i" == 90 ]]; then
    echo " TIMEOUT after 90s — see $XCB_LOG" >&2
    exit 1
  fi
done

echo "=== Maestro iOS Local Benchmark ==="
echo "Run ID:          $RUN_ID"
echo "Device:          $DEVICE_MODEL (iOS $DEVICE_OS) [$DEVICE_ID / udid=$DEVICE_UDID]"
echo "Flow:            $FLOW"
echo "App bundle:      $APP"
echo "Bundle ID:       $BUNDLE_ID"
echo "Iterations:      $ITERATIONS"
echo "Tag:             $TAG"
echo "Keep installed:  $KEEP_INSTALLED"
echo "Results dir:     $RUN_DIR"
echo "caffeinate:      ${CAFFEINATE_PID:-not started}"
echo "iproxy 7001:    ${IPROXY_PID:-not started}"
echo

now_ms() { python3 -c 'import time;print(int(time.time()*1000))'; }

run_once() {
  local iter="$1"
  local iter_dir="$RUN_DIR/iter_$iter"
  mkdir -p "$iter_dir"
  local maestro_log="$iter_dir/maestro.log"
  local meta="$iter_dir/meta.txt"

  echo "----- Iteration $iter / $ITERATIONS -----"

  # Phase 1: device readiness (verify still reachable)
  local t0 t1
  t0=$(now_ms)
  xcrun devicectl device info details --device "$DEVICE_ID" >/dev/null 2>&1 || {
    echo "Device unreachable" >&2
    return 1
  }
  t1=$(now_ms)
  local device_readiness_ms=$((t1 - t0))

  # Phase 2: app install (with fresh uninstall unless -k)
  local app_install_ms=0
  if [[ "$KEEP_INSTALLED" == "1" && "$iter" -gt 1 ]]; then
    echo "  (keep-installed: skipping uninstall/install)"
    app_install_ms=-1
  else
    xcrun devicectl device uninstall app --device "$DEVICE_ID" "$BUNDLE_ID" >/dev/null 2>&1 || true
    t0=$(now_ms)
    if ! xcrun devicectl device install app --device "$DEVICE_ID" "$APP" >"$iter_dir/install.log" 2>&1; then
      echo "App install failed; see $iter_dir/install.log" >&2
      return 1
    fi
    t1=$(now_ms)
    app_install_ms=$((t1 - t0))
  fi

  # Phase 3: maestro test
  t0=$(now_ms)
  set +e
  $MAESTRO --port "$RUNNER_PORT" --device "$DEVICE_UDID" test "$FLOW" --debug-output "$iter_dir/maestro_debug" > "$maestro_log" 2>&1
  local exit_code=$?
  set -e
  t1=$(now_ms)
  local maestro_total_ms=$((t1 - t0))

  # Parse finer-grained timings out of maestro debug log if available.
  local maestro_start_ms="-1"
  local execution_ms="$maestro_total_ms"
  local debug_log
  debug_log="$(find "$iter_dir/maestro_debug" -name maestro.log 2>/dev/null | head -1)"
  if [[ -n "$debug_log" && -f "$debug_log" ]]; then
    local parsed_json="$iter_dir/timings.json"
    if python3 "$ROOT_DIR/../../parse_maestro_log.py" "$debug_log" > "$parsed_json" 2>/dev/null; then
      maestro_start_ms=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get('start_time_ms') or -1)" "$parsed_json")
      execution_ms=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get('execution_ms') or -1)" "$parsed_json")
    fi
  fi

  # Phase 4: stop / uninstall (skip when -k unless last iter)
  local stop_ms=0
  if [[ "$KEEP_INSTALLED" == "1" && "$iter" -lt "$ITERATIONS" ]]; then
    stop_ms=-1
  else
    t0=$(now_ms)
    xcrun devicectl device uninstall app --device "$DEVICE_ID" "$BUNDLE_ID" >/dev/null 2>&1 || true
    t1=$(now_ms)
    stop_ms=$((t1 - t0))
  fi

  local session_total_ms=$((device_readiness_ms + maestro_total_ms))
  [[ "$app_install_ms" -ge 0 ]] && session_total_ms=$((session_total_ms + app_install_ms))
  [[ "$stop_ms" -ge 0 ]]        && session_total_ms=$((session_total_ms + stop_ms))

  {
    echo "run_id=$RUN_ID"
    echo "iter=$iter"
    echo "tag=$TAG"
    echo "device_model=$DEVICE_MODEL"
    echo "device_os=$DEVICE_OS"
    echo "flow=$FLOW"
    echo "app=$APP"
    echo "device_readiness_ms=$device_readiness_ms"
    echo "app_install_ms=$app_install_ms"
    echo "maestro_total_ms=$maestro_total_ms"
    echo "maestro_start_ms=$maestro_start_ms"
    echo "execution_ms=$execution_ms"
    echo "stop_ms=$stop_ms"
    echo "session_total_ms=$session_total_ms"
    echo "exit_code=$exit_code"
  } > "$meta"

  echo
  echo "Iter $iter results:"
  echo "  device_readiness  : ${device_readiness_ms} ms"
  echo "  app_install       : ${app_install_ms} ms"
  echo "  maestro_total     : ${maestro_total_ms} ms  (start=${maestro_start_ms} ms + exec=${execution_ms} ms)"
  echo "  stop              : ${stop_ms} ms"
  echo "  session_total     : ${session_total_ms} ms"
  echo "  maestro exit code : ${exit_code}"
  echo

  return $exit_code
}

OVERALL_RC=0
for i in $(seq 1 "$ITERATIONS"); do
  if ! run_once "$i"; then
    OVERALL_RC=1
  fi
done

echo "=== Done. Run dir: $RUN_DIR ==="
echo

# Emit local_ios_final_report_<TS>.csv + local_ios_sessions_report_<TS>.csv
# into $RESULTS_DIR. Timestamped at the moment the benchmark completes.
if [[ -x "$ROOT_DIR/../../aggregate_unified_report.py" ]]; then
  python3 "$ROOT_DIR/../../aggregate_unified_report.py" \
    --run-dir "$RUN_DIR" || true
fi

exit $OVERALL_RC
