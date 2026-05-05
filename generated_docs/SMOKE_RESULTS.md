# Maestro Android — Local Benchmark Smoke Run

> **Note:** for the formal benchmark setup (looped flow, device prep, aggregation), see **`FORMAL_BENCHMARK.md`**. This document is the as-built smoke validation that preceded it.

**Status:** Plumbing validated. Metrics captured end-to-end.
**Date:** 2026-04-30
**Goal:** Validate that a local Maestro session runs against `WikipediaSample.apk` and that we can capture the metrics defined in *PROD-Framework Performance Benchmarking*.

---

## Setup

| Item | Value | Notes |
|---|---|---|
| Framework | Maestro 2.5.0 | `/Users/vinits/.maestro/bin/maestro` |
| Platform | Android | |
| Device | OnePlus 9R (LE2101), Android 14 / API 34 | Tier-1 allowed device; this is the benchmark target. |
| App under test | `WikipediaSample.apk` (`org.wikipedia.alpha`, alpha/test build, `android:testOnly=true`) | Required `adb install -t` to bypass install-verification block. |
| Flow | `SAMPLE_ANDROID_TEST COPY/search_browserstack.yaml` (tag: `smoke`) | 4 steps: launchApp → tapOn search container → inputText "BrowserStack" → assertVisible. |
| ADB | platform-tools at `~/Library/Android/sdk/platform-tools/adb` | |
| Capabilities | All defaults | Per spec: "only default capabilities set to true; do not change base/default capabilities." SRI is XCUITest/Espresso-only and N/A for Maestro. |

---

## Smoke Result — Run `20260430_142823`

| Phase | Time (ms) | Maps to spec metric |
|---|---:|---|
| Device readiness | 122 | `terminal/device readiness` |
| App install (`adb install -r -t`) | 51,253 | `app_install_time` (P1) |
| Maestro start (JVM init + driver/server setup + device info) | 8,575 | `start_time` / `firecmd_time` (P0) |
| Maestro execution (all 4 flow commands) | 14,672 | `execution_time` (session duration, P0) |
| Stop / cleanup (uninstall) | 520 | `stop_time` (P1) |
| **Wall-clock total** | **80,371** | — |
| Maestro exit code | 0 | flow passed |

### Per-step execution breakdown (from Maestro debug log)

| Step | ms |
|---|---:|
| Define variables | 3 |
| Apply configuration | 1 |
| `launchApp org.wikipedia.alpha` | 1,618 |
| `tapOn id: …:id/search_container` | 7,244 |
| `inputText "BrowserStack"` | 5,113 |
| `assertVisible "Software company based in India"` | 696 |

> The `tapOn` and `inputText` durations include implicit waits for the app to settle / element to become tappable; these dominate flow time on a cold launch.

---

## Spec metrics — coverage status (local context)

| Spec metric | Local? | Captured | Source |
|---|---|---|---|
| `waiting_time` (queue) | ❌ N/A locally | — | Cloud-only (no queueing on local device). |
| `terminal/device_readiness` | ✅ | 122 ms | `adb wait-for-device` + `sys.boot_completed` poll. |
| `start_time` / `firecmd_time` | ✅ | 8,575 ms | Parsed from `maestro.log` debug output (init → first command RUNNING). |
| `execution_time` (session duration) | ✅ | 14,672 ms | Parsed from `maestro.log` (first command RUNNING → last command COMPLETED). |
| `app_install_time` (P1) | ✅ | 51,253 ms | `adb install -r -t` wall-clock. |
| `app_download_time` (P1) | ❌ N/A locally | — | APK already on disk. |
| `app_upload_time`, `test_suite_upload_time`, `test_suite_download_time` | ❌ N/A locally | — | All cloud-only. |
| `stop_time` (P1) | ✅ (partial) | 520 ms | Uninstall only; logs/video/recycle don't apply locally. |
| Per-step timings (extra signal) | ✅ | see table above | Parsed from `MaestroCommandRunner` `RUNNING`/`COMPLETED` events. |

**Conclusion:** every metric the spec lists *for local execution* is now captured per session. Cloud-only metrics (waiting_time, app/test_suite upload+download) are correctly marked N/A.

---

## Tooling left in the folder

```
/Users/vinits/local_run_android/
├── run_benchmark.sh           # wrapper — runs N iters, captures all phases, appends to CSV
├── parse_maestro_log.py       # extracts start_time + per-step timings from maestro.log
├── results/
│   ├── sessions.csv           # one row per iter, all metrics
│   └── <run_id>/iter_<n>/
│       ├── meta.txt           # key=value snapshot
│       ├── install.log        # adb install output
│       ├── maestro.log        # raw maestro CLI stdout
│       ├── timings.json       # parsed phase + per-step ms
│       └── maestro_debug/…    # full maestro debug bundle (commands JSON + log)
```

### Run more iterations

```bash
# 30-iter run for a stable P50 (still well below the 100+ the spec asks for):
./run_benchmark.sh -i 30 -t baseline

# Different flow:
./run_benchmark.sh -i 30 -t baseline -f "SAMPLE_ANDROID_TEST COPY/search_aws.yaml"
```

CSV schema (`results/sessions.csv`):
`run_id, iter, tag, framework, os, device_model, device_os, flow, apk, device_readiness_ms, app_install_ms, maestro_total_ms, maestro_start_ms, execution_ms, stop_ms, session_total_ms, exit_code`

Once N≥30, P50/P90 per metric is a one-liner in pandas / `awk` / BQ.

---

## Caveats & follow-ups before the formal benchmark

1. **Session-duration target.** The smoke flow runs ~15 s of execution. The spec's Maestro Android target session is **1198 s (P90)**. We need to loop the existing flow commands until total session duration ≈ 1198 s. The current `search_browserstack.yaml` has 4 commands; looping (e.g., via Maestro's `repeat:` or chaining a `runFlow` block ~80x) will be needed for the formal run.
2. **Volume.** Spec calls for **100+ builds per configuration**. Current run is N=1 (smoke).
3. **APK install flag.** `adb install -t` is required by this APK (`testOnly=true`). For a production benchmark APK without that flag, drop `-t` from `run_benchmark.sh:run_once` to mirror the standard install path users would take.
4. **Install-time variance.** Two consecutive smoke installs took 12 s and 51 s on the same device — likely Play Protect re-scanning. Either disable verifier (`adb shell settings put global verifier_verify_adb_installs 0`) for repeatable numbers, or accept the variance and rely on N≥30 to absorb it.
5. **Maestro permission warnings.** `pm grant` for `INTERNET`/`WRITE_EXTERNAL_STORAGE`/`GET_ACCOUNTS` failed (`SecurityException: Neither user 2000 nor current process has android.permission.GRANT_RUNTIME_PERMISSIONS`). Maestro proceeds anyway and the flow passes; this is a known no-op on user-build Android with shell user. Ignore unless a permission is actually required by a future flow.
