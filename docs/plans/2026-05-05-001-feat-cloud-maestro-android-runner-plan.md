---
title: "feat: Cloud Maestro Android benchmark runner"
type: feat
status: active
date: 2026-05-05
---

# feat: Cloud Maestro Android benchmark runner

## Summary

A new `cloud_run_android.sh` orchestrator mirroring `cloud_run_ios.sh` — uploads the Wikipedia `.apk` + Android Maestro flow once, triggers one BS App Automate Maestro v2 Android build with N device entries, polls the build to terminal status, and writes a `sessions.txt` the existing `pipeline/cells.py` cloud-Android loader can consume. Targets a 100-session OnePlus 11R / Android 13 baseline (BS does not list OnePlus 9R; 11R is the closest OnePlus R-series available, and BS pairs each OnePlus model with exactly one Android version). Closes with a hand-aggregated `BENCHMARK_REPORT_ANDROID_CLOUD.md` mirroring the iOS report's structure (BQ `SELECT` per-session metrics → P50/P90 by region → reason-bucket breakdown → recommendations).

---

## Problem Frame

The repo already has a working iOS cloud orchestrator (`cloud_run_ios.sh`) that produced a 100-session baseline build (`82920fe249e2dad80a69ba92e5ffc144e0621761`) feeding the four-cell aggregation pipeline (per `docs/plans/2026-05-04-001-feat-maestro-benchmark-pipeline-plan.md`). The `cloud_android` cell of that matrix is the last leg outstanding — there is no Android cloud trigger yet. Local Android baselines exist via `run_benchmark.sh` and `SAMPLE_ANDROID_TEST COPY/benchmark_loop.yaml`, which is calibrated for OnePlus 9R / Android 14 to the spec's ~1198s execution-time P90 target. This plan ships the Android cloud trigger so the four-cell matrix can be completed.

---

## Requirements

- R1. Trigger N=100 Maestro Android sessions on BStack App Automate via a single Maestro v2 Android build with N device entries (mirroring the iOS pattern's verified single-build/multi-device shape).
- R2. Write a per-run results dir under `results/cloud_<timestamp>/` containing a `sessions.txt` (`session_id,build_id,status` per line) compatible with the existing `pipeline/cells.py:load_cloud_cell` consumer.
- R3. Default to OnePlus 11R / Android 13 (canonical BS specifier `OnePlus 11R-13.0`) with operator override via `-d` flag for alternative devices.
- R4. Default to `apps/WikipediaSample.apk` and a cloud-tuned Maestro flow targeting the spec's P90 execution-time of ~1198s.
- R5. Surface the BS plan parallel cap and current usage before triggering (mirroring iOS pre-flight) so the operator sees if the requested N exceeds available slots.
- R6. Idempotent app + test-suite uploads via a `-s` skip-upload flag and `_cloud_cache.env` (mirror iOS pattern).
- R7. After BQ ingestion lag clears, query per-session metrics for the run's `build_id` and produce `generated_docs/BENCHMARK_REPORT_ANDROID_CLOUD.md` mirroring `BENCHMARK_REPORT_IOS_CLOUD.md`'s sections (Executive Summary, Setup, Methodology, Results with P50/P90 + reason-bucket breakdown + region split, Recommendations).

---

## Scope Boundaries

- HyperExecute coverage (excluded by the prior plan's scope and reaffirmed here).
- Refactoring `cloud_run_ios.sh` and `cloud_run_android.sh` into a shared bash core — premature for two callers.
- Modifying `pipeline/cells.py` or any aggregation logic — `cloud_android` is already supported there per the prior plan's U2/U3.
- Email rendering / BQ `INSERT` to `maestro_benchmark_metrics_aggregated` — covered by the prior plan, gated on the four-cell matrix being complete; this plan does not run that.
- Recalibrating the spec's P90 execution-time target (~1198s) — accept as input.
- Espresso / UIAutomator framework variants — Maestro only.
- Local Android CSV format changes — local-cell plumbing untouched.

### Deferred to Follow-Up Work

- A shared `_cloud_run_common.sh` core — defer until a 3rd platform / orchestrator surfaces (e.g., HyperExecute).
- Per-device calibration sweep across multiple Android device tiers — this plan's calibration is single-device.
- Auto-fallback inside the script when the primary device is not allocated — this plan keeps fallback explicit (operator passes `-d`).

---

## Context & Research

### Relevant Code and Patterns

- `cloud_run_ios.sh` — direct line-for-line reference for shape, flag set, output layout, and pre-flight.
- `SAMPLE_ANDROID_TEST COPY/benchmark_loop.yaml` — local Android Maestro flow on Wikipedia, calibrated for OnePlus 9R / Android 14 (85 iters × ~14s ≈ 1198s).
- `SAMPLE_IOS_TEST/ios-benchmark-loop.yaml` — comment/header structure for calibration math.
- `apps/WikipediaSample.apk` — Android app for the cloud upload.
- `pipeline/cells.py:load_cloud_cell` — downstream consumer; accepts `os="android"` already, so no pipeline changes required.
- `run_benchmark.sh` — env-var error handling and exit-code conventions.
- `docs/runbooks/benchmark-report.md` — existing runbook this plan extends.

### Institutional Learnings

- BS does NOT dedupe duplicate device entries in the `devices` array of one Maestro v2 build payload (verified iOS build `b825797062cbb0be9bc5d5a7fe3c7dc3937868fb`, 2026-05-04). The Android script inherits this property and uses the single-build-multi-device shape.
- BQ ingestion lag for Maestro sessions on this account is ~50 minutes; aggregation of the cloud_android cell can only run after that lag.
- Cross-region device allocation occurs during high-parallelism bursts (28% of the iOS run scattered outside `ap-south-1`). The aggregator already groups by `device_region`; this plan's only impact is that operators should expect multi-region scatter.
- `total_stop_time` is NULL for every Maestro session in BQ — propagated by the pipeline; not this plan's concern.

### External References

- BS App Automate plan probe: `GET /app-automate/plan.json` (used for the parallel-cap pre-flight).
- BS Maestro v2 build endpoint: `POST /app-automate/maestro/v2/android/build` (Android sibling of the iOS endpoint at `/app-automate/maestro/v2/ios/build`) — verify payload shape via single-build smoke before N=100.
- BS App Automate device list: `GET /app-automate/devices.json` — used in U3 to confirm the canonical OnePlus 9R / Android 14 device specifier string.

---

## Key Technical Decisions

- **Sibling script, not a refactor.** A new `cloud_run_android.sh` mirrors `cloud_run_ios.sh` shape-for-shape, swapping iOS-specific defaults (`.ipa` → `.apk`, `iPhone 17-26` → `OnePlus 9R-14`, `ios/build` → `android/build`, iOS flow path → Android flow path). Rationale: refactoring into a shared `_cloud_run_common.sh` after only two callers is premature; revisit when a 3rd platform / orchestrator lands.
- **Single-build / N device entries shape** retained from iOS — BS does not dedupe duplicates (verified). One `build_id` per run-set aligns with the prior plan's run-set identity decision.
- **Cloud-tuned flow as separate files in the existing Android dir**, not edits to the local ones. New `SAMPLE_ANDROID_TEST COPY/cloud_benchmark_loop.yaml` (sibling of `benchmark_loop.yaml`) and `cloud_benchmark_calibration.yaml` (sibling of `benchmark_calibration.yaml`). Rationale: matches the existing convention where local and cloud flows share a per-platform directory (the iOS dir holds both `ios-sample.yaml` and the cloud-targeted `ios-benchmark-loop.yaml` together); keeps the local files unmodified so the local Android baseline runner is untouched. Same `appId: org.wikipedia.alpha` so the apk works.
- **Device target is OnePlus 11R-13.0**, not the originally-requested OnePlus 9R-14. Rationale: BS App Automate does not list OnePlus 9R in any OS version; OnePlus 11R is the closest OnePlus R-series, and BS pairs each OnePlus device with exactly one Android version (11R↔13.0, 12R↔14.0, 13R↔15.0). Choice between 11R-13 (same R-series tier, OS one major version behind) and 12R-14 (different R-series, OS-parity with local) was made by the user; OS delta of 13 vs 14 is immaterial for this Maestro Wikipedia-loop benchmark (<5% expected timing impact).
- **Wikipedia app-specific element selectors retained** in the cloud flow. Rationale: the iOS flow had to drop selectors due to unknown-device variance; here the device class is known and Wikipedia's `org.wikipedia.alpha:id/search_container` is app-internal so it does not depend on Android system UI. If selectors break, U3's smoke catches it before the 100-session firing.
- **Calibration smoke as a first-class unit** (n=1, 10-iter dedicated `cloud_benchmark_calibration.yaml`) before the 100-session trigger. Rationale: cloud OnePlus 11R is a different SoC class than the local OnePlus 9R (SD 8+ Gen 1 vs SD 870), so per-iter timing will differ from the local calibration; the smoke re-derives `repeat.times` for the baseline flow. The iOS run did the same calibration step (50-iter / 584s smoke documented in `generated_docs/BENCHMARK_REPORT_IOS_CLOUD.md`).
- **Device fallback is operator-passed `-d`, not auto-fallback in the script.** Rationale: keeping fallback explicit puts the choice in the operator's hands; auto-fallback inside a 100-session orchestrator could mis-target an undesired device class for the entire run.
- **`_cloud_cache.env` shared across iOS and Android.** Rationale: simpler than per-platform caches; the runbook documents the caveat that operators must run `-s` only after the matching platform's upload is current. Future hardening: per-platform cache files when a third caller lands.

---

## Open Questions

### Resolved During Planning

- **Cloud cell ingestion path?** The existing `pipeline/cells.py:load_cloud_cell` accepts `os="android"`; no new ingestion code required.
- **Run-set identity?** The Android `build_id` becomes the cloud_android cell's run-set descriptor; same convention as the iOS cell.
- **Where do calibration results live?** Same `results/cloud_<timestamp>/` shape; calibration runs are tagged via the existing `-t calibration` flag (mirrors iOS).
- **Does the pipeline need any changes to consume the cloud-Android `build_id`?** No — confirmed by reading the prior plan's U3 (`load_cloud_cell` already takes `os` as a parameter).
- **Canonical device specifier?** Verified via `GET /app-automate/devices.json` (2026-05-05): OnePlus 9R is not on BS; closest OnePlus R-series is `OnePlus 11R-13.0`. BS pairs each OnePlus model with exactly one Android version, so 11R + Android 14 is also not a valid combo.

### Deferred to Implementation

- **Cloud-flow iteration count.** Local is 85 iters × ~14s on OnePlus 9R + SD 870. Cloud OnePlus 11R has SD 8+ Gen 1 (stronger SoC), so per-iter timing is expected to be faster; calibrate with the 10-iter smoke and adjust to land on ~1198s P90.
- **Stability of the Wikipedia element ID `org.wikipedia.alpha:id/search_container` on BS-allocated OnePlus 11R / Android 13 instances.** Verified by U3 smoke; if unstable, fall back to a selector-free flow analogous to the iOS loop.
- **Whether the fallback device set (`OnePlus 12R-14.0`, `Google Pixel 8-14.0`) is in fact allocatable on this BS account.** Verified by U3 only if the primary device is not allocated.

---

## Implementation Units

- U1. **Cloud-tuned Android Maestro flow files**

**Goal:** Create cloud-suitable Android Maestro flows (a 10-iter calibration flow and a baseline loop flow) targeting the spec's P90 execution-time (~1198s) on OnePlus 11R / Android 13, packaged for upload as a Maestro v2 test suite.

**Requirements:** R4

**Dependencies:** None

**Files:**
- Create: `SAMPLE_ANDROID_TEST COPY/cloud_benchmark_loop.yaml`
- Create: `SAMPLE_ANDROID_TEST COPY/cloud_benchmark_calibration.yaml`

**Approach:**
- Mirror `SAMPLE_ANDROID_TEST COPY/benchmark_loop.yaml` shape (search_container loop on Wikipedia) for the loop file; mirror `benchmark_calibration.yaml` for the 10-iter calibration file.
- `cloud_benchmark_loop.yaml` initial `repeat.times` = local's 85; the final value is calibrated by U3 and committed before U4's runbook update.
- `appId: org.wikipedia.alpha`, tags `[benchmark, real-user-sim]` for the loop file; `[calibration]` for the calibration file.
- Header comments document calibration math (iters × per-iter seconds + pre-loop overhead, target P90).

**Patterns to follow:**
- `SAMPLE_IOS_TEST/ios-benchmark-loop.yaml` — header comment structure, calibration math format, tag set.
- `SAMPLE_ANDROID_TEST COPY/benchmark_loop.yaml` — Wikipedia element selectors and per-iter command body.

**Test scenarios:**
- Test expectation: none — content is a Maestro YAML; behavioural validation happens in U3's smoke.

**Verification:**
- File parses with `maestro test --dry` (or equivalent dry-validate) without errors.
- Header comment names target P90 (~1198s), iter count, per-iter seconds, and pre-loop overhead.

- U2. **`cloud_run_android.sh` orchestrator**

**Goal:** Create the Android sibling of `cloud_run_ios.sh` — upload `.apk` + test-suite zip once (with `_cloud_cache.env` skip), trigger one Maestro v2 Android build with N device entries, poll to terminal, and write `sessions.txt` compatible with the existing pipeline cell loader.

**Requirements:** R1, R2, R3, R5, R6

**Dependencies:** U1

**Files:**
- Create: `cloud_run_android.sh`

**Approach:**
- Mirror the structure of `cloud_run_ios.sh`; swap defaults — `DEVICE="OnePlus 11R-13.0"`, `FLOW="$ROOT_DIR/SAMPLE_ANDROID_TEST COPY/cloud_benchmark_loop.yaml"`, `APK="$ROOT_DIR/apps/WikipediaSample.apk"` (rename `IPA` → `APK` throughout).
- Trigger endpoint: `POST /app-automate/maestro/v2/android/build` (verified during U3 smoke).
- Plan-probe pre-flight retained (`/app-automate/plan.json` → parallel cap + current usage), printed before trigger.
- Same `-n / -t / -d / -f / -a / -s` flag set as the iOS script.
- Output dir: `results/cloud_<RUN_ID>/` with `builds.txt`, `sessions.txt`, `initial_status.json`, `<BID>.json`.
- Same env requirements: `BROWSERSTACK_USERNAME`, `BROWSERSTACK_ACCESS_KEY`.
- Shared `_cloud_cache.env` with iOS — runbook (U4) documents the caveat that `-s` is valid only when the cache matches the current platform's upload.

**Execution note:** Validate end-to-end via the U3 smoke before any N=100 firing; do not run the 100-session command directly off this unit's completion.

**Patterns to follow:**
- `cloud_run_ios.sh` — line-by-line structural template.
- `run_benchmark.sh` — env-var error handling, exit-code conventions, log-friendly stdout.

**Test scenarios:**
- Test expectation: none — bash orchestrator; behavioural correctness validated by U3 smoke and (optionally) the operator's N=100 baseline run.

**Verification:**
- `bash -n cloud_run_android.sh` reports no syntax errors.
- Invocation with no env vars set prints the env-error message and exits 1.
- Help / unknown-flag invocation prints usage and exits non-zero.

- U3. **Calibration smoke run (n=1) and iteration tuning**

**Goal:** Validate the script + flow + device specifier end-to-end on BS with N=1, confirm OnePlus 9R / Android 14 is allocatable, and capture per-iter timing to finalize U1's iter count for the 100-session run.

**Requirements:** R3, R4

**Dependencies:** U1, U2

**Files:**
- Modify (post-smoke): `SAMPLE_ANDROID_TEST COPY/cloud_benchmark_loop.yaml` — adjust `repeat.times` to land on ~1198s P90 based on observed per-iter seconds.
- Artifact (gitignored): `results/cloud_<smoke_run_id>/`.

**Approach:**
- Invoke with `-t calibration -n 1 -f "$PWD/SAMPLE_ANDROID_TEST COPY/cloud_benchmark_calibration.yaml"` (the dedicated 10-iter calibration flow) to keep the smoke cheap.
- Capture: BS-allocated device, session pass/fail, observed per-iter seconds, total execution-time.
- If `OnePlus 11R-13.0` is not allocated within ~5 minutes of trigger, retry once with a fallback (e.g., `OnePlus 12R-14.0`, `Google Pixel 8-14.0`); record which device the smoke landed on.
- Re-derive `repeat.times` for `cloud_benchmark_loop.yaml`: `(1198 − pre_loop_overhead) / per_iter_seconds`.

**Patterns to follow:**
- `generated_docs/BENCHMARK_REPORT_IOS_CLOUD.md` — calibration narrative shape (iOS used 50 iters / 584s smoke to derive 63-iter cloud target).
- `SAMPLE_ANDROID_TEST COPY/benchmark_loop.yaml` header comment — calibration math format.

**Test scenarios:**
- Happy path: smoke session passes, observed execution-time within ±10% of the projected target after iter-count adjustment.
- Edge case: OnePlus 9R-14 not allocated → fallback device used; runbook entry records the canonical specifier that worked.
- Error path: smoke session fails on a Wikipedia selector (`assertVisible` not satisfied, `tapOn` element missing) → switch flow to selector-free pattern (analogous to iOS's `swipe`-only loop) and re-smoke before continuing.

**Verification:**
- One terminal-state session in `results/cloud_<run_id>/sessions.txt` with `status=passed`.
- Updated `repeat.times` in U1's flow file projects an execution-time within ±5% of 1198s based on observed per-iter seconds.
- The device specifier that produced a passing smoke is committed as the script's default `-d` value (or documented as the recommended fallback if the primary did not allocate).

- U4. **Runbook entry + cloud_android cell handoff documentation**

**Goal:** Document how to invoke `cloud_run_android.sh` for the 100-session baseline run, how to find the resulting `build_id`, the BQ ingestion wait, and how to hand the `build_id` to the pipeline (when the four-cell matrix completes) or to the standalone Android cloud report (U5).

**Requirements:** R1, R2

**Dependencies:** U1, U2, U3

**Files:**
- Modify: `docs/runbooks/benchmark-report.md` — new "Cloud Android baseline" section.

**Approach:**
- New section walks through: env setup, plan-cap pre-check, smoke calibration command, 100-session command, finding the `build_id` in `results/cloud_<run_id>/builds.txt`, the ~50-min BQ ingestion wait, and the two downstream paths (standalone report per U5; or run-set descriptor JSON when the four-cell matrix is being aggregated).
- Document the device fallback list (canonical specifier strings verified during U3) and how to invoke with `-d "<fallback>"`.
- Cross-reference the prior plan (`docs/plans/2026-05-04-001-feat-maestro-benchmark-pipeline-plan.md`) for the four-cell aggregation flow.
- Document the `_cloud_cache.env` cross-platform caveat: `-s` is valid only when the cache matches the current platform's upload.

**Patterns to follow:**
- Existing runbook structure in `docs/runbooks/benchmark-report.md` (Pre-conditions / Step 1 / Step 2 / ...).
- `generated_docs/SESSION_LIFECYCLE_EXPLAINED.md` — runbook tone.

**Test scenarios:**
- Test expectation: none — documentation.

**Verification:**
- A reader following the runbook can complete the cloud_android baseline run end-to-end without consulting the source scripts.
- Example commands match the script's actual flag names (no drift between the runbook and `cloud_run_android.sh`).
- The fallback device list in the runbook matches the canonical specifiers verified during U3.

- U5. **Cloud Android benchmark report (BQ aggregation)**

**Goal:** Produce `generated_docs/BENCHMARK_REPORT_ANDROID_CLOUD.md` mirroring `BENCHMARK_REPORT_IOS_CLOUD.md`'s structure — pulling per-session metrics from BigQuery for the run's `build_id`, computing P50/P90 across all spec metrics + the BS-side `data` JSON sub-phases, splitting by `device_region`, decomposing the queue waiting-time reason buckets, and ending with recommendations.

**Requirements:** R7

**Dependencies:** U2, U3 (need the live build_id) and ~50-min BQ ingestion lag after the 100-session run reaches terminal.

**Files:**
- Create: `generated_docs/BENCHMARK_REPORT_ANDROID_CLOUD.md`
- Use (read-only): `mcp__claude_ai_Google_Cloud_BigQuery__execute_sql_readonly` MCP for the per-session SELECT against `browserstack-production.app_automate.app_automate_test_sessions_partitioned` (and `app_automate_queueing_data_partitioned` for the reason buckets).

**Approach:**
- Run the same per-session SELECT shape used for iOS (documented in `docs/runbooks/benchmark-report.md` Step 1) keyed by the cloud_android `build_id`.
- Compute aggregates in-memory using the percentile conventions from `aggregate_results.py` (nearest-rank).
- Mirror the iOS report's section layout: §1 Executive Summary, §2 Setup, §3 Methodology, §4 Results (4.1 aggregate metrics, 4.2 region split, 4.3 reason-bucket waiting decomposition, 4.4 outcomes & failure modes), §5 Comparison to local Android baseline, §6 Recommendations.
- Note explicitly where Android differs from iOS (e.g., `total_stop_time` NULL same as iOS; any data-JSON sub-phases that differ).
- Persist the SQL queries used in the report's appendix so the run is reproducible.

**Execution note:** This unit is documentation/analysis, not code. Run after the 100-session build reaches terminal AND the BQ ingestion lag has cleared (~50 min after last session ends).

**Patterns to follow:**
- `generated_docs/BENCHMARK_REPORT_IOS_CLOUD.md` — section structure, table layout, calibration narrative, recommendations format.
- `docs/runbooks/benchmark-report.md` Step 1 — BQ SELECT shape.
- `pipeline/cells.py:load_cloud_cell` — confirms the canonical metric column mapping.

**Test scenarios:**
- Test expectation: none — analysis report.

**Verification:**
- Report's §4.1 aggregate row count equals the count of rows BQ returned for the build_id (with explicit note if any sessions are still mid-ingestion).
- All P50/P90 values cross-check against a second `APPROX_QUANTILES(metric, 100)` run for spot-validation.
- The Recommendations section names at least one Android-specific finding (e.g., reason-bucket distribution, region scatter) not just iOS-borrowed boilerplate.
- A reader can identify the run's build_id, device, sample size, and headline P90 from §1 alone.

---

## System-Wide Impact

- **Interaction graph:** New `cloud_run_android.sh` calls BS App Automate REST API (uploads, build trigger, build status) and writes results files. No new callbacks, daemons, or persistent state.
- **Error propagation:** Env-missing → exit 1 with clear message. Build-trigger failure → exit 1 with the BS response body. Poll-timeout / non-terminal handling matches iOS (no timeout; relies on BS reaching terminal). An indefinite-running BS build is a known operational concern surfaced in the iOS report; this plan inherits the same posture and does not introduce a new mitigation.
- **State lifecycle risks:** `_cloud_cache.env` is shared with the iOS script. Operator running `cloud_run_ios.sh` (upload), then `cloud_run_android.sh -s` (skip upload) would reuse iOS app/test-suite URLs and trigger a malformed Android build. Mitigation: runbook caveat + the U3 smoke catches it before N=100.
- **API surface parity:** The iOS script remains the canonical reference; if BS Maestro v2 API changes (build payload shape, endpoint path), both scripts will need updating. Surface this in the runbook so future maintainers know to update both.
- **Integration coverage:** The end-to-end test is the U3 smoke (n=1, real BS, real device). The pipeline cell loader is already integration-covered by the prior plan's `tests/test_cloud_cells.py`; this plan adds no new test surface there.
- **Unchanged invariants:** `cloud_run_ios.sh`, `pipeline/cells.py`, `run_benchmark.sh`, `apps/WikipediaSample.apk`, the local Android flow, and the prior plan's pipeline module remain untouched. The new script reads the same env vars and shares `_cloud_cache.env`.

---

## Risks & Dependencies

| Risk | Mitigation |
|---|---|
| BS does not list OnePlus 9R; OnePlus 11R-13.0 picked as substitute. Cloud per-iter timing will differ from local (different SoC class) | U3 calibration smoke re-derives `repeat.times` based on observed per-iter seconds; the 100-session run uses the calibrated value. |
| BS rejects `OnePlus 11R-13.0` or fails to allocate it during the run | Documented fallback list (`OnePlus 12R-14.0`, `Google Pixel 8-14.0`) for operator override via `-d`. |
| Maestro v2 Android endpoint shape differs from iOS (different field names, different status enum) | U3 smoke catches before the 100-session run; runbook records the verified endpoint and payload shape. |
| Wikipedia element ID drift on cloud-allocated OnePlus 9R | U3 smoke detects (failure → selector-free fallback flow); flow's selectors are isolated to a single line for easy swap. |
| `_cloud_cache.env` shared with iOS script causes accidental cross-platform reuse | Runbook caveat; `-s` valid only after a fresh upload for the current platform. U3 smoke catches a mismatched cache before N=100. |
| BS plan parallel cap < N=100 starves the run | Pre-flight `plan.json` probe surfaces cap and current usage before trigger; operator decides whether to lower N. |
| BQ ingestion lag (~50 min) confuses operators expecting immediate cell ingestion | Runbook documents the wait; existing pipeline gate already handles this case for iOS. |
| Selectors-vs-selector-free trade-off if device class shifts mid-run | Single-build/single-device-spec design caps blast radius; if BS allocates a different device, the smoke catches before the 100-session firing. |
| OnePlus 9R fallback device runs at a materially different per-iter speed than the local OnePlus 9R | U3 calibration re-derives `repeat.times` based on observed per-iter seconds, so timing parity is enforced regardless of which device class allocates. |

---

## Documentation / Operational Notes

- Runbook entry in `docs/runbooks/benchmark-report.md` (U4) covers the new flow.
- The prior plan's runbook (same file) handles the aggregation/email leg; this plan adds only the Android trigger leg, ending once `build_id` and `sessions.txt` exist.
- Future hardening: a shared bash core (`_cloud_run_common.sh`) when a 3rd platform lands; per-platform cache files at the same time so the cross-platform `-s` caveat goes away.

---

## Sources & References

- iOS sibling orchestrator: `cloud_run_ios.sh`
- Local Android orchestrator: `run_benchmark.sh`
- Local Android flow (template + calibration source): `SAMPLE_ANDROID_TEST COPY/benchmark_loop.yaml`
- Local iOS flow (header / tag-set reference): `SAMPLE_IOS_TEST/ios-benchmark-loop.yaml`
- Downstream cell loader: `pipeline/cells.py`
- Prior plan: `docs/plans/2026-05-04-001-feat-maestro-benchmark-pipeline-plan.md`
- iOS report (calibration narrative + institutional findings): `generated_docs/BENCHMARK_REPORT_IOS_CLOUD.md`
- Existing runbook this plan extends: `docs/runbooks/benchmark-report.md`
- BS App Automate Maestro v2 build endpoint (Android): `https://api-cloud.browserstack.com/app-automate/maestro/v2/android/build`
- BS App Automate plan probe: `https://api-cloud.browserstack.com/app-automate/plan.json`
- BS App Automate device list (for canonical device specifier verification): `https://api-cloud.browserstack.com/app-automate/devices.json`
