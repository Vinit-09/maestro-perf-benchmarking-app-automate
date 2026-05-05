# Maestro Android Local Benchmark — Phase Definitions

Reference for `run_benchmark.sh` + `parse_maestro_log.py`. Each phase is a wall-clock interval between two well-defined events, timed in the wrapper with `time.time()*1000` taken just before/after the relevant command.

## Phases

| # | Phase | Start event | End event | What's actually being measured | Maps to spec |
|---|---|---|---|---|---|
| 1 | **`device_readiness_ms`** | wrapper invokes `adb wait-for-device` | `getprop sys.boot_completed` returns `1` (or 30 s timeout) | Time for adb to see the device + Android to finish booting. On an already-booted, already-trusted device this is essentially "adb round-trip" (~100 ms). On a cold boot it would be 30–60 s. | `terminal/device readiness` (P0) |
| 2 | **`app_install_ms`** | `adb install -r -t <APK>` invoked | adb prints `Success` | Streamed APK push over USB + PackageManager parse + dexopt + first-time setup. Includes any device-side verification (Play Protect scan) and any user-facing install dialog if one appears. | `app_install_time` (P1) |
| 3 | **`maestro_total_ms`** = `maestro_start_ms` + `execution_ms` | `maestro test` process launched | `maestro test` process exits | JVM startup + Maestro driver/server push to device + flow run + report write + JVM shutdown. Split below. | — |
| 3a | &nbsp;&nbsp;**`maestro_start_ms`** | first line in `maestro.log` (= debug reporter ready) | first user-flow command logs `RUNNING` | Maestro JVM init (~400 ms) + driver/server APK install on device + ADB port forward to 7001 + initial `getDeviceInfo`. This is the analog of `firecmd_time` — overhead from "I want to run" to "first action fires". | `start_time` / `firecmd_time` (P0) |
| 3b | &nbsp;&nbsp;**`execution_ms`** | first user-flow command `RUNNING` | last user-flow command `COMPLETED` | Pure flow runtime — sum of per-step times (`launchApp`, `tapOn`, `inputText`, `assertVisible`). Each step includes Maestro's implicit waits for the UI to settle and the element to become tappable/visible. | `execution_time` / session duration (P0) |
| 4 | **`stop_ms`** | `adb uninstall <appId>` invoked | adb returns | Locally just package removal. Cloud equivalent additionally covers log/video upload + device recycle, which don't apply here. | `stop_time` (P1) |
| — | `session_total_ms` | sum of phases 1–4 | — | Bookkeeping only; not a spec metric. | — |

The per-step ms inside `execution_ms` (e.g., `tapOn = 7,244 ms`) come from Maestro's own `MaestroCommandRunner` log events, parsed by `parse_maestro_log.py`. Anything outside the explicit spans above is unaccounted (typically <100 ms of process startup/shutdown overhead).

## Cloud-only metrics, intentionally not captured locally

`waiting_time` (queue), `app_upload_time`, `app_download_time`, `test_suite_upload_time`, `test_suite_download_time` — all require BrowserStack infrastructure (queues, blob storage, host VM). They're correctly N/A in the local context and recorded as `—` in `SMOKE_RESULTS.md`.

---

# Human Intervention

The wrapper's measurement window starts at `adb wait-for-device` and ends at `adb uninstall`. **Anything before that window is not in any metric.** Anything *inside* the window — including a human tapping a dialog — IS counted, but in whichever phase was active when it happened.

## One-time setup, NOT measured

These are prerequisites a human does once per device/machine, before any benchmark run. The wrapper assumes they're already done; the time is invisible to all metrics.

- Connect device via USB
- Enable Developer Options (tap Build Number ×7) and USB debugging
- Accept the "Allow USB debugging from this computer?" RSA fingerprint prompt on the device — **first connection only**
- Install Android platform-tools and Maestro CLI on the host
- Recommended: enable "Stay awake while charging" in Developer Options so the screen doesn't sleep during long runs
- Recommended: `adb shell settings put global verifier_verify_adb_installs 0` to suppress per-install verifier dialogs/scans for repeatable install timings

## Per-run prompts that CAN appear and ARE counted (in their host phase)

These are device-side dialogs that, if they occur, block the flow until a human taps. The wrapper has no visibility into them — they just stretch the wall-clock of whichever phase was running.

| Possible prompt | Phase it lands in | Mitigation |
|---|---|---|
| First-time install of the Maestro driver APKs ("Install blocked / Allow from this source?") on stricter OEM Androids | `maestro_start_ms` | Pre-install once manually; subsequent runs skip. |
| "Allow this app to be installed?" on the test APK if Play Protect is enforcing | `app_install_ms` | Disable `verifier_verify_adb_installs` once. |
| Mid-flow system dialogs Maestro can't auto-dismiss (e.g., "Wikipedia would like to send notifications", carrier popups, OS toasts overlapping a tap target) | `execution_ms` | Add explicit dismiss steps to the YAML, or pre-dismiss once with the app launched. |
| Device screen lock kicking in during a long benchmark run | whichever phase was active | "Stay awake while charging"; or `adb shell svc power stayon usb`. |

## What did NOT require intervention in the smoke run

- The `pm grant` `SecurityException` lines in the Maestro log (`INTERNET` / `WRITE_EXTERNAL_STORAGE` / `GET_ACCOUNTS`) — Maestro tries grants best-effort and continues on failure. No prompt, no human action, included as part of `launchApp` step time (~1.6 s).
- The first install attempt failure (`INSTALL_FAILED_VERIFICATION_FAILURE`) — fixed by adding `-t` to `adb install` since the sample APK has `android:testOnly=true`. This is a code-side fix, not a per-run intervention.

---

# Bottom Line for the Formal Benchmark

For the timings to be clean and directly comparable across N≥30 runs, do the one-time setup once, then keep the device unlocked + plugged in + on the home screen for the duration. Then the only thing the human does is `./run_benchmark.sh -i 30` and walks away — every measured metric is then strictly machine-driven wall-clock.
