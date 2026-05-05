# Maestro Android — Formal Benchmark

This is the per-spec setup for **PROD-Framework Performance Benchmarking** §"How to Run Benchmarks" → Maestro / Android.

For phase definitions and human-intervention semantics see `PHASE_DEFINITIONS.md`.

---

## What changed vs the smoke run

| Concern | Smoke | Formal |
|---|---|---|
| Flow | `search_browserstack.yaml` (4 commands, ~15 s execution) | `benchmark_loop.yaml` (loops the search cycle 125× to hit the spec's **1198 s** P90 target) |
| Iterations | 1 | 30 (default), spec asks for **100+** |
| Default tag | `smoke` | `baseline` |
| Device prep | none | `prepare_device.sh` runs once: disables install verifier, keeps screen on, pre-installs Maestro driver APKs |
| Per-iter app cycle | install + run + uninstall | same (mirrors a fresh BrowserStack session); `-k` flag available to skip for specialised runs |
| Aggregation | manual | `aggregate_results.py` auto-runs at end of every benchmark; `min / P50 / P90 / P95 / max / mean` per metric |

---

## How to run

```bash
cd /Users/vinits/local_run_android

# 1. Prep the device — once per device. Idempotent. See prepare_device.sh
#    header for the exhaustive list of changes.
./prepare_device.sh

# 2. Run the formal benchmark. Default: 30 iters, baseline tag, ~10 h wall-clock.
#    For the spec ask of 100+ runs, use:
./run_benchmark.sh -i 100 -t baseline

# 3. Inspect results at any time (also auto-printed at end of run):
./aggregate_results.py --tag baseline

# 4. JSON for downstream BQ ingest:
./aggregate_results.py --tag baseline --json > results/baseline_summary.json
```

CSV at `results/sessions.csv` is the source of truth. Each row = one session. Schema:
`run_id, iter, tag, framework, os, device_model, device_os, flow, apk, device_readiness_ms, app_install_ms, maestro_total_ms, maestro_start_ms, execution_ms, stop_ms, session_total_ms, exit_code`.

---

## Files added / changed for the formal benchmark

```
/Users/vinits/local_run_android/
├── prepare_device.sh                                    # NEW — one-time device prep
├── run_benchmark.sh                                     # UPDATED — defaults to formal mode
├── parse_maestro_log.py                                 # (unchanged)
├── aggregate_results.py                                 # NEW — P50/P90 stats from CSV
├── SAMPLE_ANDROID_TEST COPY/
│   ├── benchmark_loop.yaml                              # NEW — 125× loop, ~1198 s exec
│   ├── benchmark_calibration.yaml                       # NEW — 5× loop, used for sizing
│   └── search_browserstack.yaml … (smoke flows)         # (unchanged)
├── results/sessions.csv                                 # appended per run
└── PHASE_DEFINITIONS.md, SMOKE_RESULTS.md, FORMAL_BENCHMARK.md
```

---

## Why N = 125 in `benchmark_loop.yaml`

From the calibration run (`results/20260430_162200/iter_1/timings.json`):

- Pre-loop overhead (`launchApp` + first `tapOn`): **~9.2 s**
- Per-loop body (`eraseText` + `inputText` + `assertVisible`): **~9.5 s** (mean over 5 iters; one outlier of 13 s)
- Spec target P90 `execution_ms`: **1198 s**
- Solving: `9.2 + 9.5·N ≈ 1198` ⇒ **N ≈ 125**

The 125 is hardcoded in `SAMPLE_ANDROID_TEST COPY/benchmark_loop.yaml`, sized for the **OnePlus 9R (LE2101) / Android 14** in use (a Tier-1 allowed device). Re-tune if the device changes — per-loop times shift with hardware. To re-tune:

```bash
./run_benchmark.sh -i 1 -t calibration -f "SAMPLE_ANDROID_TEST COPY/benchmark_calibration.yaml"
# inspect results/<run_id>/iter_1/timings.json, recompute N, edit benchmark_loop.yaml.
```

---

## Bottom line for the formal run

For the timings to be clean and directly comparable across N≥30 runs:

1. Run `./prepare_device.sh` once. Confirm the printout shows `verifier_verify_adb_installs=0`.
2. Ensure the device is plugged in via USB, unlocked, on the home screen, and screen-lock has no PIN.
3. Don't touch the device or laptop during the run — every dialog tap, screen lock, or USB disconnect lands inside one of the measured phases.
4. Kick off `./run_benchmark.sh -i 100 -t baseline`. Each iteration is ~20 minutes (~1198 s execution + ~30 s overhead), so 100 iters ≈ 33 hours; 30 iters ≈ 10 hours. Plan accordingly.
5. Aggregator output at the end of the run is the deliverable. Pipe to `--json` to feed BQ.

### Possible per-run prompts and which phase they land in

| Possible prompt | Phase it lands in | Mitigation |
|---|---|---|
| First-time install of the Maestro driver APKs ("Install blocked / Allow from this source?") on stricter OEM Androids | `maestro_start_ms` | `prepare_device.sh` step 4 pre-installs the driver. |
| "Allow this app to be installed?" on the test APK if Play Protect is enforcing | `app_install_ms` | `prepare_device.sh` step 1 disables `verifier_verify_adb_installs`. |
| Mid-flow system dialogs Maestro can't auto-dismiss (notifications, carrier popups, OS toasts overlapping a tap target) | `execution_ms` | Add explicit dismiss steps to the YAML, or pre-dismiss once with the app launched. |
| Device screen lock kicking in during a long benchmark run | whichever phase was active | `prepare_device.sh` step 2 sets stay-awake-while-charging. |

### Cloud-only metrics intentionally NOT captured locally

`waiting_time` (queue), `app_upload_time`, `app_download_time`, `test_suite_upload_time`, `test_suite_download_time`. These all require BrowserStack infrastructure and have no local analog.
