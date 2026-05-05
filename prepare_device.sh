#!/usr/bin/env bash
# One-time device preparation for the formal Maestro Android benchmark.
# Idempotent — safe to run multiple times.
#
# This script tries every device-side hardening it can. If your device has a
# managed Work Profile (corporate MDM), several `settings put global` calls
# will be blocked by WRITE_SECURE_SETTINGS policy. The script detects that
# case, reports it clearly, and tells you what to toggle manually.
#
# Reverting any settings that *did* take effect:
#   adb shell settings put global verifier_verify_adb_installs 1
#   adb shell settings put global stay_on_while_plugged_in 0

set -u
set -o pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
ADB="${ADB:-/Users/vinits/Library/Android/sdk/platform-tools/adb}"
MAESTRO="${MAESTRO:-/Users/vinits/.maestro/bin/maestro}"

echo "=== Device preparation for formal Maestro benchmark ==="

DEVICE_SERIAL="$($ADB devices | awk 'NR>1 && $2=="device"{print $1; exit}')"
if [[ -z "${DEVICE_SERIAL:-}" ]]; then
  echo "ERROR: no connected device found." >&2
  exit 1
fi
DEVICE_MODEL="$($ADB -s "$DEVICE_SERIAL" shell getprop ro.product.model | tr -d '\r')"
DEVICE_OS="$($ADB -s "$DEVICE_SERIAL" shell getprop ro.build.version.release | tr -d '\r')"
echo "Device: $DEVICE_MODEL (Android $DEVICE_OS) [$DEVICE_SERIAL]"
echo

# Detect MDM / managed Work Profile up-front.
mdm_owner="$($ADB -s "$DEVICE_SERIAL" shell dpm list-owners 2>/dev/null | grep -i 'admin=' | head -1 || true)"
MDM_PRESENT=0
if [[ -n "$mdm_owner" ]]; then
  MDM_PRESENT=1
  echo "Managed profile detected:"
  echo "      $mdm_owner"
  echo "      Some 'settings put global' calls will be blocked by MDM policy;"
  echo "      this script will continue, mark them as blocked, and tell you what"
  echo "      to toggle manually."
  echo
fi

# Helper: try to set a global setting and verify by reading back.
try_put_global() {
  local key="$1" want="$2"
  local err
  err="$($ADB -s "$DEVICE_SERIAL" shell settings put global "$key" "$want" 2>&1 1>/dev/null)"
  local got
  got="$($ADB -s "$DEVICE_SERIAL" shell settings get global "$key" 2>/dev/null | tr -d '\r')"
  if [[ "$got" == "$want" ]]; then
    echo "      OK   $key=$got"
    return 0
  else
    if echo "$err" | grep -q WRITE_SECURE_SETTINGS; then
      echo "      MDM-BLOCKED  $key=$got (wanted $want; SecurityException)"
    else
      echo "      FAIL $key=$got (wanted $want)"
    fi
    return 1
  fi
}

# 1. Install verifier
echo "[1/4] Disabling package install verifier..."
try_put_global verifier_verify_adb_installs 0 || true
try_put_global package_verifier_user_consent -1 || true

# 2. Stay-awake hardening
echo "[2/4] Stay-awake hardening..."
try_put_global stay_on_while_plugged_in 7 || true
try_put_global low_power 0 || true
$ADB -s "$DEVICE_SERIAL" shell svc power stayon true >/dev/null 2>&1 || true
$ADB -s "$DEVICE_SERIAL" shell dumpsys deviceidle disable >/dev/null 2>&1 || true

# Read effective state
sosp="$($ADB -s "$DEVICE_SERIAL" shell settings get global stay_on_while_plugged_in | tr -d '\r')"
echo "      effective stay_on_while_plugged_in=$sosp  (need 7 = AC|USB|Wireless)"

# 3. Wake + dismiss keyguard
echo "[3/4] Waking device + dismissing keyguard..."
$ADB -s "$DEVICE_SERIAL" shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1 || true
$ADB -s "$DEVICE_SERIAL" shell wm dismiss-keyguard >/dev/null 2>&1 || true

# 4. Pre-install Maestro driver APKs
echo "[4/4] Pre-installing Maestro driver APKs..."
NOOP_FLOW="$ROOT_DIR/.maestro_noop.yaml"
cat > "$NOOP_FLOW" <<'EOF'
appId: com.android.settings
---
- launchApp
EOF
$MAESTRO test "$NOOP_FLOW" >/dev/null 2>&1 || true
rm -f "$NOOP_FLOW"

echo
echo "=== Summary ==="
verifier="$($ADB -s "$DEVICE_SERIAL" shell settings get global verifier_verify_adb_installs | tr -d '\r')"
sosp="$($ADB -s "$DEVICE_SERIAL" shell settings get global stay_on_while_plugged_in | tr -d '\r')"
echo "verifier_verify_adb_installs : $verifier  (want 0)"
echo "stay_on_while_plugged_in     : $sosp  (want 7)"

if [[ "$MDM_PRESENT" == "1" && "$sosp" != "7" ]]; then
  cat <<'BANNER'

MDM blocks `settings put global stay_on_while_plugged_in`. Please toggle
this manually on the device:

    Settings  →  System  →  Developer options  →  "Stay awake"  →  ON

That toggle is the UI equivalent and usually works under managed profiles.
Re-run this script after toggling to confirm `stay_on_while_plugged_in=7`.

If verifier_verify_adb_installs is also non-zero, the corresponding manual
toggle is:

    Play Store app  →  Profile  →  Play Protect  →  Settings  →  Scan apps
    with Play Protect  →  OFF
    (May be MDM-blocked too. If so, accept ~10-50 s install variance.)

BANNER
fi

echo "Run:  ./run_benchmark.sh -i 1 -t loop_validation -f \"SAMPLE_ANDROID_TEST COPY/benchmark_loop.yaml\""
