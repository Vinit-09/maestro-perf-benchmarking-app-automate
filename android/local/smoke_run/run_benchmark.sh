#!/usr/bin/env bash
# Maestro Android local SMOKE runner. Same phase logic as
# android/local/run_benchmark.sh, but points at the 2-rep smoke flow under
# this folder so a session finishes in seconds. Verifies device wiring,
# app install, and selector validity before a long run.
#
# Emits the same two reports as the iOS smoke runner:
#   results/local_android_final_report_<TS>.csv     long, aggregated
#   results/local_android_sessions_report_<TS>.csv  wide, one row per session
#
# Usage:
#   ./run_benchmark.sh [-i ITERATIONS] [-f FLOW_YAML] [-a APK] [-t TAG] [-k]
#
# Defaults: 1 iter of smoke_run/flows/benchmark_loop.yaml (2 reps inside)
# against apps/WikipediaSample.apk, tagged "smoke".
#
# Flags:
#   -i N        iterations (default 1)
#   -f FLOW     flow YAML path
#   -a APK      APK to install
#   -t TAG      report tag (e.g. smoke, baseline)
#   -k          keep app installed between iters (skip uninstall+reinstall)
#
# Run ../prepare_device.sh once before the first formal run.

set -u
set -o pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
ADB="${ADB:-/Users/vinits/Library/Android/sdk/platform-tools/adb}"
MAESTRO="${MAESTRO:-/Users/vinits/.maestro/bin/maestro}"

ITERATIONS=1
FLOW="$ROOT_DIR/flows/benchmark_loop.yaml"
APK="$ROOT_DIR/../../../apps/WikipediaSample.apk"
TAG="smoke"
APP_ID="org.wikipedia.alpha"
KEEP_INSTALLED=0

while getopts "i:f:a:t:k" opt; do
  case "$opt" in
    i) ITERATIONS="$OPTARG" ;;
    f) FLOW="$OPTARG" ;;
    a) APK="$OPTARG" ;;
    t) TAG="$OPTARG" ;;
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
DEVICE_SERIAL="$($ADB devices | awk 'NR>1 && $2=="device"{print $1; exit}')"
if [[ -z "${DEVICE_SERIAL:-}" ]]; then
  echo "No connected device found (adb devices empty)." >&2
  exit 1
fi
DEVICE_MODEL="$($ADB -s "$DEVICE_SERIAL" shell getprop ro.product.model | tr -d '\r')"
DEVICE_OS="$($ADB -s "$DEVICE_SERIAL" shell getprop ro.build.version.release | tr -d '\r')"
DEVICE_API="$($ADB -s "$DEVICE_SERIAL" shell getprop ro.build.version.sdk | tr -d '\r')"

VERIFIER_OFF="$($ADB -s "$DEVICE_SERIAL" shell settings get global verifier_verify_adb_installs | tr -d '\r')"
STAY_ON="$($ADB -s "$DEVICE_SERIAL" shell settings get global stay_on_while_plugged_in | tr -d '\r')"

# Prevent macOS from sleeping (which detaches USB) for the duration of the run.
CAFFEINATE_PID=""
if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -i -m -s &
  CAFFEINATE_PID=$!
  trap 'kill $CAFFEINATE_PID 2>/dev/null || true' EXIT
fi

echo "=== Maestro Android Local SMOKE Benchmark ==="
echo "Run ID:        $RUN_ID"
echo "Device:        $DEVICE_MODEL (Android $DEVICE_OS / API $DEVICE_API) [$DEVICE_SERIAL]"
echo "Flow:          $FLOW"
echo "APK:           $APK"
echo "Iterations:    $ITERATIONS"
echo "Tag:           $TAG"
echo "Keep installed: $KEEP_INSTALLED"
echo "Results dir:   $RUN_DIR"
echo "verifier_verify_adb_installs: $VERIFIER_OFF (0 = disabled, recommended)"
echo "stay_on_while_plugged_in:     $STAY_ON (7 = always-on, recommended)"
echo "caffeinate (host stay-awake): ${CAFFEINATE_PID:-not started}"
echo
if [[ "$VERIFIER_OFF" != "0" || "$STAY_ON" != "7" ]]; then
  echo "  WARNING: device prep is incomplete. Run ../prepare_device.sh first to"
  echo "  avoid app-install variance and mid-run USB drops."
  echo
fi

now_ms() { python3 -c 'import time;print(int(time.time()*1000))'; }

run_once() {
  local iter="$1"
  local iter_dir="$RUN_DIR/iter_$iter"
  mkdir -p "$iter_dir"
  local maestro_log="$iter_dir/maestro.log"
  local meta="$iter_dir/meta.txt"

  echo "----- Iteration $iter / $ITERATIONS -----"

  # Phase 1: device readiness
  local t0 t1
  t0=$(now_ms)
  $ADB -s "$DEVICE_SERIAL" wait-for-device >/dev/null
  for _ in $(seq 1 60); do
    boot=$($ADB -s "$DEVICE_SERIAL" shell getprop sys.boot_completed | tr -d '\r')
    [[ "$boot" == "1" ]] && break
    sleep 0.5
  done
  t1=$(now_ms)
  local device_readiness_ms=$((t1 - t0))

  # Phase 2: app install (with fresh uninstall unless -k)
  local app_install_ms=0
  if [[ "$KEEP_INSTALLED" == "1" && "$iter" -gt 1 ]]; then
    echo "  (keep-installed: skipping uninstall/install)"
    app_install_ms=-1
  else
    $ADB -s "$DEVICE_SERIAL" uninstall "$APP_ID" >/dev/null 2>&1 || true
    t0=$(now_ms)
    if ! $ADB -s "$DEVICE_SERIAL" install -r -t "$APK" >"$iter_dir/install.log" 2>&1; then
      echo "App install failed; see $iter_dir/install.log" >&2
      return 1
    fi
    t1=$(now_ms)
    app_install_ms=$((t1 - t0))
  fi

  # Phase 3: maestro test (with device-drop watchdog).
  t0=$(now_ms)
  set +e
  $MAESTRO test "$FLOW" --debug-output "$iter_dir/maestro_debug" > "$maestro_log" 2>&1 &
  local maestro_pid=$!

  (
    while kill -0 "$maestro_pid" 2>/dev/null; do
      sleep 5
      if ! "$ADB" -s "$DEVICE_SERIAL" shell true >/dev/null 2>&1; then
        echo "[WATCHDOG] device $DEVICE_SERIAL not reachable — terminating maestro pid $maestro_pid" | tee -a "$maestro_log" >&2
        kill -TERM "$maestro_pid" 2>/dev/null
        sleep 3
        kill -KILL "$maestro_pid" 2>/dev/null
        break
      fi
    done
  ) &
  local watchdog_pid=$!

  ( tail -f "$maestro_log" --pid="$maestro_pid" 2>/dev/null ) &
  local tail_pid=$!

  wait "$maestro_pid"
  local exit_code=$?
  kill "$watchdog_pid" 2>/dev/null || true
  kill "$tail_pid" 2>/dev/null || true
  wait "$watchdog_pid" 2>/dev/null || true
  wait "$tail_pid" 2>/dev/null || true
  set -e
  t1=$(now_ms)
  local maestro_total_ms=$((t1 - t0))

  # Extract finer-grained timings from maestro debug log.
  local maestro_start_ms="-1"
  local execution_ms="$maestro_total_ms"
  local debug_log
  debug_log="$(find "$iter_dir/maestro_debug" -name maestro.log 2>/dev/null | head -1)"
  if [[ -n "$debug_log" && -f "$debug_log" ]]; then
    local parsed_json="$iter_dir/timings.json"
    if python3 "$ROOT_DIR/../../../parse_maestro_log.py" "$debug_log" > "$parsed_json" 2>/dev/null; then
      maestro_start_ms=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get('start_time_ms') or -1)" "$parsed_json")
      execution_ms=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get('execution_ms') or -1)" "$parsed_json")
    fi
  fi

  # Phase 4: stop / cleanup (skip uninstall when -k unless last iter)
  local stop_ms=0
  if [[ "$KEEP_INSTALLED" == "1" && "$iter" -lt "$ITERATIONS" ]]; then
    stop_ms=-1
  else
    t0=$(now_ms)
    $ADB -s "$DEVICE_SERIAL" uninstall "$APP_ID" >/dev/null 2>&1 || true
    t1=$(now_ms)
    stop_ms=$((t1 - t0))
  fi

  local session_total_ms=$((device_readiness_ms + maestro_total_ms))
  [[ "$app_install_ms" -ge 0 ]] && session_total_ms=$((session_total_ms + app_install_ms))
  [[ "$stop_ms" -ge 0 ]] && session_total_ms=$((session_total_ms + stop_ms))

  {
    echo "run_id=$RUN_ID"
    echo "iter=$iter"
    echo "tag=$TAG"
    echo "device_model=$DEVICE_MODEL"
    echo "device_os=$DEVICE_OS"
    echo "device_api=$DEVICE_API"
    echo "flow=$FLOW"
    echo "apk=$APK"
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

# Emit local_android_final_report_<TS>.csv + local_android_sessions_report_<TS>.csv
# into $RESULTS_DIR. Timestamped at the moment the benchmark completes.
if [[ -x "$ROOT_DIR/../../../aggregate_unified_report.py" ]]; then
  python3 "$ROOT_DIR/../../../aggregate_unified_report.py" \
    --run-dir "$RUN_DIR" \
    --platform android || true
fi

exit $OVERALL_RC
