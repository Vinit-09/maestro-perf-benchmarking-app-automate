---
title: "feat: 100-session multitest cloud Android benchmark + local-vs-cloud report"
type: feat
status: active
date: 2026-05-18
---

# feat: 100-session multitest cloud Android benchmark + local-vs-cloud report

## Summary

Scale the previously-validated 1-session `multitest` smoke build to a 100-session benchmark run on Samsung Galaxy S24-14.0 using `cloud_run_android.sh` unchanged, then produce a local-vs-cloud Android benchmark report (comparison CSV + standalone HTML) mirroring the iOS cloud report's shape, with per-tc handling so the dual-yaml workload is reported alongside a synthesized per-session combined view.

---

## Problem Frame

A 1-session smoke build (build `6cbf79e4af4872135a2eb0373c6498e28c61cbfb`, 2026-05-13 18:32, tag `multitest-execute-v2`) demonstrated that two 50-rep search_browserstack yamls in a single BS Maestro v2 `execute` array run cleanly on Samsung Galaxy S24-14.0 — session passed at 1153 s session.duration with both tcs completing. That smoke proved the gotcha #3 split-yaml mitigation viable in shape but said nothing about steady-state behavior at benchmark scale. The 100-session run produces statistical density for local-vs-cloud comparison and validates the dual-yaml workload as a recurring benchmark pattern.

The iOS cloud-run produced an analogous standalone report (`analysis/maestro_ios_benchmark_report.html` + `analysis/local_vs_cloud_ios_comparison_<TS>.csv`). The Android side has no comparable artifact today.

---

## Requirements

- R1. Trigger one BS Maestro v2 Android build with 100 device entries on Samsung Galaxy S24-14.0, running test_a.yaml + test_b.yaml per session via the `execute` array
- R2. The flow yaml pair (currently only present in past `test_suite.zip` artifacts) must be durably committed to the repo so the run is reproducible
- R3. The run must apply a stall-rate gate after the first 25-session batch (gotcha #1 mitigation) to avoid burning ~80 min of wall-time in a bad device-pool window
- R4. The run must produce diagnostic visibility on per-tc duration (gotcha #3 cap inspection) — surfaced in the report's methodology section, not used as an abort trigger
- R5. A local-vs-cloud Android comparison CSV in the iOS file format must be produced, pinned to the 2026-05-10 local-Android baseline
- R6. A standalone HTML report at `analysis/maestro_android_benchmark_report.html` must be produced, mirroring the iOS report's skeleton and adapted for the dual-tc shape
- R7. Per-tc percentile breakdown (test_a, test_b) AND a synthesized per-session combined view (raw sum of test_a + test_b execution) must both appear in the report
- R8. The HTML hero must carry a quantitative claim computed from the actual data (cloud P90 ÷ local P90 ratio), matching the iOS report's "1.1×" framing
- R9. The report generator must be a committed, reproducible Python script (not a manual one-off)

---

## Scope Boundaries

- Modifying smoke yaml content (the smoke pair is the spec — recreated verbatim)
- Edits to `cloud_run_android.sh`
- iOS or local-Android benchmark equivalents (this plan is cloud Android only)
- The four-cell rollup email pipeline (separate flow — see `docs/runbooks/benchmark-report.md`)
- Generalizing the report generator into reusable `pipeline/` engine code (one-off script per iOS precedent; promote-to-engine only if a third platform asks)
- Running a fresh local-Android baseline (pinned to 2026-05-10 committed CSVs)
- Comparing this dual-yaml run against the single-yaml `cloud_benchmark_loop` 100-rep baseline as a primary lens (the headline is local-vs-cloud; dual-vs-single comparison is not in scope this round)

### Deferred to Follow-Up Work

- Potential `pipeline.cells.load_cloud_cell` extension for dual-tc BQ row shape: depends on U2 finding. If U2 reveals BQ returns multiple rows per session, deciding whether to extend the engine vs. handle in the report script is a follow-up decision, not pre-committed scope.

---

## Context & Research

### Relevant Code and Patterns

- `cloud_run_android.sh` — runner; already supports `-f <dir>` with multi-yaml `execute` (script lines around the `EXECUTE_JSON` build), `-d` device override (default already Samsung Galaxy S24-14.0), `-t` build tag, and re-upload by default (no `-s`)
- `SAMPLE_ANDROID_TEST COPY/cloud_benchmark_loop.yaml` — original 100-rep selector-based search workload (test_a + test_b are 50-rep clones of this)
- `aggregate_results.py` — percentile math (P50/P75/P90/P95/min/max/mean) — pattern to follow for the new generator
- `aggregate_unified_report.py` — CSV emission shape (per-platform config, phase-group canonicalization) — pattern to follow
- `analysis/maestro_ios_benchmark_report.html` — HTML skeleton, hero pattern (`Is BrowserStack cloud iOS<br>within <span class="accent">1.1×</span> of local?`), section ordering, color tokens, Chart.js usage
- `analysis/local_vs_cloud_ios_comparison_20260510_191348.csv` — comparison CSV column shape: `phase_group, phase_step, local_N, local_pX_ms (min/p50/p75/p90/p95/p100), cloud_N, cloud_pX_ms, delta_p50_ms, delta_p90_ms`
- `pipeline/cells.py` — `CellSession` schema and `load_cloud_cell` (BQ row consumer) — relevant for U2's verify-first BQ check
- `android/local/results/local_android_final_report_20260510_231553.csv` + `local_android_sessions_report_20260510_231553.csv` — pinned local baseline

### Institutional Learnings

- `docs/runbooks/cloud-maestro-gotchas.md` is authoritative for platform pitfalls. Gotchas materially relevant to this plan:
  - #1 device-pool stall variance (informs R3 stall-rate gate)
  - #2 no-stop-via-API (abort path is BS dashboard — manual)
  - #3 ~900 s per-tc cap (informs R4 cap-kill inspect signal; this run's dual-yaml structure is the gotcha #3 mitigation under steady-state evaluation)
  - #4 silent param drop (verify `input_capabilities` echoes `execute` after trigger)
  - #5 `BS spawned N sessions` snapshot is lazy (initial count unreliable)
  - #9 shared `_cloud_cache.env` across platforms (informs re-upload decision)
  - #13 parallel cap = 25 (informs wall-time forecast: ceil(100/25) × ~19 min ≈ 77 min best-case)
- The smoke build session ran 1153 s with the dual-yaml pair on S24-14.0 — informs per-tc budget (~575 s each, well under 870 s cap headroom)

### External References

None — this plan is fully grounded in the in-repo runner, the smoke build artifact, the runbook, and the iOS report precedent.

---

## Key Technical Decisions

- **Recreate the smoke yamls verbatim in `android/cloud/flows/multitest/`**: the smoke pair was never committed (only in past `test_suite.zip` artifacts). Co-locates with existing `android/cloud/flows/` siblings (cloud_benchmark_loop, cloud_benchmark_calibration).
- **Build tag `multitest-100x-baseline`**: extends the smoke's `multitest-execute-v2` lineage, names this as the first baseline at 100x.
- **Re-upload (default; no `-s`)**: shared cache (gotcha #9) may have drifted to iOS or other content since 5/13; cost is a few seconds.
- **Stall-rate gate at first 25-session batch**: matches the parallel-cap boundary (gotcha #13). If stall fingerprints (status=error, duration <80 s, empty stacktrace per gotcha #1) exceed 30 % of the first batch, abort via BS dashboard (gotcha #2). Cap-kill (gotcha #3) is an inspect signal only, not abort criteria.
- **Headline frame: local-Android vs cloud-Android**: mirrors the iOS report's spine. Local side pinned to the 2026-05-10 committed CSVs for reproducibility.
- **Per-tc + synthesized per-session combined view (raw sum)**: report shows test_a percentiles, test_b percentiles, and `execution_combined = execution(test_a) + execution(test_b)` per session. Combined view includes the inter-tc gap (second `launchApp`, BS overhead) — honest about the dual-yaml envelope cost.
- **BQ dual-tc verify-first**: U2 queries the smoke build (1 session, 2 tcs) and inspects row shape before structuring downstream consumption. Three branches captured under Open Questions.
- **One-off committed Python script `aggregate_android_cloud_report.py` at repo root**: matches existing `aggregate_results.py` / `aggregate_unified_report.py` naming and location. Reproducible across re-runs.
- **HTML skeleton mirrors iOS exactly with a dual-yaml-mechanics section added**: same hero, same section ordering, same Chart.js, same color tokens. New "Dual-yaml workload mechanics" section explains the test_a/test_b split and surfaces per-tc duration distributions (R4 cap inspection).
- **Quantitative hero claim filled at generation time**: hero text computed from cloud-P90 ÷ local-P90 ratio (e.g., "Is BrowserStack cloud Android<br>within X× of local?").

---

## Open Questions

### Resolved During Planning

- Test pair identity: 2 yamls, identical 50-rep clones of search_browserstack (resolved from smoke artifact, not search_browserstack + search_aws as initially synthesized)
- Split shape: one build, both tests per session via `execute` array (smoke pattern preserved)
- Local baseline: pinned to `android/local/results/*_20260510_231553.csv`

### Deferred to Implementation

- **BQ row shape for dual-tc sessions**: does `app_automate_test_sessions_partitioned` return 1 row per session (with per-tc fields aggregated or flattened) or 2 rows per session (one per tc)? Resolved by U2's diagnostic query against the smoke build. U4's data-access path depends on this finding; see U4 Approach for the three branches.
- **Stall-rate gate wall-clock interpretation**: the 30 % threshold and "first 25-session batch" are stated in plan; the exact polling cadence and which `cloud_run_android.sh` poll-line fields to grep are an execution-time concern.
- **Combined-view percentile order**: P50 of (test_a + test_b) is not the same as P50(test_a) + P50(test_b). The plan specifies raw per-session sum, then percentiles computed across the 100 combined values — confirm at implementation time the math lines up with the iOS combined column.

---

## Implementation Units

- U1. **Commit the multitest flow pair**

**Goal:** Durable in-repo source for the test_a / test_b yaml pair (currently only in past `test_suite.zip` artifacts).

**Requirements:** R2

**Dependencies:** none

**Files:**
- Create: `android/cloud/flows/multitest/test_a.yaml`
- Create: `android/cloud/flows/multitest/test_b.yaml`

**Approach:**
- Recreate the yamls verbatim from `results/cloud_20260513_183231/test_suite.zip` (extract path: `multitest/test_a.yaml`, `multitest/test_b.yaml`).
- Both files are identical in shape (50-rep search_browserstack clones); their tag values differ (`multitest-a` vs `multitest-b`) and a one-line comment differs ("file A" vs "file B").
- Don't modify content; this is a faithful recreation, not an adaptation.

**Patterns to follow:**
- `SAMPLE_ANDROID_TEST COPY/cloud_benchmark_loop.yaml` for shape (frontmatter, `launchApp`, optional OK tap, search_container tap, repeat block)

**Test scenarios:**
- Test expectation: none — verbatim recreation. Verify via byte-equality against extracted zip contents (`diff` against the extracted files).

**Verification:**
- Both files exist at the target paths and parse as valid Maestro yaml
- Both files match the smoke artifact byte-for-byte except for whitespace tolerance

---

- U2. **BQ dual-tc row shape verification**

**Goal:** Determine how `app_automate_test_sessions_partitioned` represents sessions with multiple testcases — the unknown that branches U4's approach.

**Requirements:** R5, R7

**Dependencies:** U1 is not required (this queries existing smoke build data)

**Files:**
- No code changes. Diagnostic finding captured inline in the plan's Open Questions resolution log or as a one-paragraph note in U4's approach selection.

**Approach:**
- Query BQ for build_id `6cbf79e4af4872135a2eb0373c6498e28c61cbfb` (the 5/13 smoke build) and inspect:
  - Row count for the build (1 session: 1 row total? 2 rows total? Other?)
  - Whether `tc_id` / testcase-level identifying fields are present per row
  - Whether `duration` / `execution_s` is per-session aggregate or per-tc
  - Which queue/install/teardown fields are populated per row (those should appear once per session, not per tc)
- Capture the finding as: "1-row-per-session" / "N-rows-per-session" / "ambiguous, ask BQ owner".

**Patterns to follow:**
- `docs/runbooks/benchmark-report.md` Step 1 SELECT (the cloud-cell BQ export query) — use the same columns

**Test scenarios:**
- N/A — this is a diagnostic query, not code under test. Finding is documented; outcome routes U4.

**Verification:**
- A documented row-shape finding (which branch of U4's approach applies)
- BQ response saved to `results/cloud_20260513_183231/bq_smoke_response.json` for future reference

---

- U3. **Trigger the 100-session run with stall-rate gate**

**Goal:** Produce a passing 100-session build on Samsung Galaxy S24-14.0 with the multitest flow pair.

**Requirements:** R1, R3, R4

**Dependencies:** U1

**Files:**
- No code changes (runner script and cache infra unchanged).
- Output (runner-generated): `results/cloud_<YYYYMMDD_HHMMSS>/builds.txt`, `sessions.txt`, `initial_status.json`, `<build_id>.json`, `test_suite.zip`.

**Approach:**
- Invocation (single command):
  `./cloud_run_android.sh -n 100 -d "Samsung Galaxy S24-14.0" -t multitest-100x-baseline -f android/cloud/flows/multitest/`
- Re-upload (no `-s`) — fresh test_suite + APK upload to defend against gotcha #9.
- Confirm pre-flight: `BROWSERSTACK_USERNAME` / `BROWSERSTACK_ACCESS_KEY` set; plan parallel cap check (runner already does this).
- Post-trigger verification: fetch the build's `input_capabilities` and confirm `execute: ["test_a.yaml", "test_b.yaml"]` is echoed (gotcha #4 silent-drop guard).
- Stall-rate gate (R3): once the first 25 sessions reach terminal state (roughly the first parallel batch per gotcha #13), grep finished sessions for the gotcha #1 stall fingerprint (`status=error`, `duration` < 80 s, empty stacktrace). If > 30 % of the first batch stalls, abort via the BS App Automate web dashboard (gotcha #2 — no API stop path).
- Cap-kill inspect signal (R4): the run is NOT aborted on cap-kills; instead, any tc with `duration` > 870 s is logged for the report's methodology section. Expected count: zero (per-tc workload sized to ~575 s based on smoke).

**Patterns to follow:**
- The smoke run invocation from 2026-05-13 (build_id `6cbf79e4af4872135a2eb0373c6498e28c61cbfb`) — same flags, just `-n 100` instead of `-n 1`.

**Test scenarios:**
- N/A — operational unit, not code under test. Verification is on outputs, not behavior of new code.

**Verification:**
- `results/<run-id>/builds.txt` contains a single build_id
- `results/<run-id>/sessions.txt` contains 100 rows (or fewer if the stall-rate gate triggered an abort — in which case the row count documents how far the run went)
- Pass rate ≥ 70 % (conservative threshold given gotcha #1 device-pool variance)
- `input_capabilities.execute == ["test_a.yaml", "test_b.yaml"]` in `initial_status.json`
- No tc.duration > 870 s in the build JSON (cap-kill inspect: expected pass; flag if violated)

---

- U4. **Local-vs-cloud comparison CSV + percentile aggregation**

**Goal:** Produce `analysis/local_vs_cloud_android_comparison_<TS>.csv` carrying per-phase percentile comparison between the pinned local-Android baseline and the new cloud-Android multitest run, with per-tc + per-session-combined views.

**Requirements:** R5, R7, R9

**Dependencies:** U2 (BQ row shape known), U3 (run complete, ingestion lag ~50 min cleared)

**Files:**
- Create: `aggregate_android_cloud_report.py` (at repo root, mirroring `aggregate_results.py` / `aggregate_unified_report.py` location)
- Create: `analysis/local_vs_cloud_android_comparison_<TS>.csv` (output, where `<TS>` is the run's timestamp)
- Test: `tests/test_aggregate_android_cloud_report.py` (new — repo's existing test discipline; create the `tests/` dir if absent)

**Approach:**
- Reads:
  - Local: `android/local/results/local_android_final_report_20260510_231553.csv` + `..._sessions_report_20260510_231553.csv` (pinned)
  - Cloud: BQ rows for the build_id from U3 (consumed per U2's branch — single-row-per-session, multi-row-per-session, or raw-query-with-tc-filtering)
- Computes per phase_step:
  - Local: N, min, P50, P75, P90, P95, P100 (already in committed CSV; pass through)
  - Cloud per-tc: separate percentile tables for test_a and test_b across the 100 sessions
  - Cloud per-session combined: `execution_combined_ms = execution_ms(test_a) + execution_ms(test_b)` per session, then percentile-aggregate across 100 combined values
- Emits CSV with the same column shape as `analysis/local_vs_cloud_ios_comparison_20260510_191348.csv`, extended for dual-tc:
  - `phase_group, phase_step, local_N, local_min_ms, local_p50_ms, ..., cloud_N, cloud_min_ms, cloud_p50_ms, ..., delta_p50_ms, delta_p90_ms`
  - Additional rows for per-tc views: phase_step values like `Per-rep total (test_a)`, `Per-rep total (test_b)`, `Per-rep total (combined)`

**Patterns to follow:**
- `aggregate_results.py` — percentile math (`percentile()` function, linear interpolation)
- `aggregate_unified_report.py` — per-platform phase-step canonicalization, CSV column ordering
- `analysis/local_vs_cloud_ios_comparison_20260510_191348.csv` — column shape (delta columns at end)

**Test scenarios:**
- Happy path: synthetic input with 10 local sessions + 100 cloud sessions × 2 tcs → CSV with all phase rows populated, delta columns computed correctly, per-tc rows present
- Edge case: cloud session with status != passed (e.g., one errored session in the 100) → excluded from cloud percentile computation; cloud_N reflects effective count, not requested count
- Edge case: per-tc median ≠ half of per-session-combined median (since per-tc P50s are independent draws) — assert the combined view is computed from per-session sums, not from summed per-tc percentiles
- Edge case: empty cloud cell (build had 0 passing sessions) → script exits with a clear error, not a divide-by-zero
- Edge case: missing local CSV → script errors with the missing path, doesn't silently produce a one-sided CSV
- Integration: percentile values match `aggregate_results.py` output for the same numeric input (no formula drift)
- Integration: column order matches iOS CSV when diffed header-by-header (for the columns that exist in both)

**Verification:**
- The CSV is byte-identical when regenerated from the same inputs (deterministic)
- For each phase_step row present in the iOS reference CSV, the Android CSV carries the same column set (plus per-tc rows added on top)
- Computed `delta_p90_ms = cloud_p90_ms - local_p90_ms` for at least one phase_step matches a hand-computed value

---

- U5. **Standalone HTML report generation**

**Goal:** Produce `analysis/maestro_android_benchmark_report.html` mirroring the iOS report's structure with a quantitative hero claim and a new dual-yaml-mechanics section.

**Requirements:** R6, R7, R8, R9

**Dependencies:** U4 (comparison CSV must exist as data backing)

**Files:**
- Modify: `aggregate_android_cloud_report.py` (add HTML emission alongside the CSV emission from U4)
- Create: `analysis/maestro_android_benchmark_report.html` (output)
- Test: extend `tests/test_aggregate_android_cloud_report.py` with HTML emission cases

**Approach:**
- Copy the iOS report's HTML skeleton (`analysis/maestro_ios_benchmark_report.html`) section structure, color tokens, and Chart.js usage. Substitute Android content for iOS content.
- Compute the hero ratio: `hero_ratio = round(cloud_p90 / local_p90, 2)` where both percentiles come from a stable canonical phase_step (e.g., `Per-rep total (combined)`). Render as `Is BrowserStack cloud Android<br>within <span class="accent">{hero_ratio}×</span> of local?`.
- Add a new section "Dual-yaml workload mechanics" between methodology and head-to-head:
  - Explain test_a + test_b are identical 50-rep clones (the gotcha #3 split-yaml mitigation)
  - Show per-tc duration distribution (min, P50, P90, max for both test_a and test_b)
  - Note any tc.duration violations of the 870 s cap inspect threshold from U3 (expected none)
- Adapt the "Cloud — phase percentiles (BigQuery)" section to surface both per-tc and per-session-combined rows
- Preserve the iOS report's narrative tone in section headers but rewrite copy for Android content

**Patterns to follow:**
- `analysis/maestro_ios_benchmark_report.html` — full structure: hero (lines around the `<h1>` claim), "Why this benchmark exists", "What we measured", "Local: N sessions, all clean", "Cloud: N device entries, M with data", "Cloud vs Local — head to head", percentile tables, "How BrowserStack can close the gap"

**Test scenarios:**
- Happy path: comparison CSV with known cloud/local P90 values → HTML rendered with correct hero ratio (asserted via DOM parse / regex on hero `<h1>` content)
- Edge case: hero_ratio < 1.0 (cloud faster than local) → wording flips gracefully ("within X× of local" remains structurally correct; agent should not assume hero phrasing for this case — present as deferred sub-question if encountered in execution)
- Edge case: dual-yaml-mechanics section renders the per-tc duration table from the U3 build JSON correctly
- Integration: rendered HTML is well-formed and parseable
- Integration: Chart.js data series are populated from CSV values (not hardcoded), verified by re-rendering with synthetic input and asserting the data structure

**Verification:**
- The HTML file exists at the target path and renders correctly in a browser (visual inspection of hero, percentile tables, dual-yaml section, head-to-head section)
- The hero ratio value matches `cloud_p90 ÷ local_p90` from the CSV for the canonical phase_step
- All sections present in the iOS report's section ordering have an Android-content equivalent (modulo the dual-yaml-mechanics addition)

---

## System-Wide Impact

- **Interaction graph:** `cloud_run_android.sh` is invoked unchanged. The runner outputs (run dir under `results/`) feed U4; the BQ table downstream of BS ingestion feeds U4 after the ~50 min ingestion lag (per `docs/runbooks/benchmark-report.md`). The new generator script reads from local CSVs (pinned) + BQ + run dir, writes to `analysis/`.
- **Error propagation:** `aggregate_android_cloud_report.py` must fail loudly on missing inputs (missing local CSV, empty cloud cell, BQ query error). No silent half-renders.
- **State lifecycle risks:** the shared `_cloud_cache.env` (gotcha #9) is overwritten by U3's run — subsequent iOS runs would need fresh upload. Document this as an operational note, not a code change.
- **API surface parity:** none — this plan does not add or change any public interface.
- **Integration coverage:** U4's BQ branch (selected via U2's finding) must be verified end-to-end; the per-tc + combined math is not provable from unit tests alone.
- **Unchanged invariants:** `cloud_run_android.sh` behavior (including its directory-of-yamls `execute` handling, parent-folder zip preservation, parallel cap probe, poll loop), the existing `pipeline/*` engine code, and the iOS report artifacts are all explicitly unchanged.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Device-pool stall variance on S24-14.0 (gotcha #1 fingerprint) burns ~80 min wall-time | Stall-rate gate after first 25-session batch (R3 / U3); abort via BS dashboard if > 30 % stalls |
| Per-tc cap-kill at 870 s tc.duration (gotcha #3) | Expected workload is ~575 s per tc; cap-kill is an inspect signal, not abort criteria (R4 / U3 / report methodology section) |
| Silent `execute` key drop (gotcha #4) | Verify `input_capabilities.execute` echoes after trigger (U3 verification) |
| BQ ingestion lag (~50 min after build terminal) | U4 dependency is "ingestion cleared" — operator waits, not a code-level concern |
| BQ row shape unknown for dual-tc sessions | U2 verify-first; U4 has three documented branches keyed off the finding |
| Shared `_cloud_cache.env` corruption across platforms (gotcha #9) | Re-upload (no `-s`) — explicit decision documented in Key Technical Decisions |
| Local baseline staleness (2026-05-10) | Pinned by explicit choice; trade-off accepted (reproducibility > recency) |
| Hero ratio wording when cloud is faster than local | Flagged as edge case in U5 test scenarios; resolution deferred to execution time if encountered |

---

## Documentation / Operational Notes

- After U3 completes, the operator must wait ~50 min before U4 (BQ ingestion lag — see `docs/runbooks/benchmark-report.md`).
- The cap-kill inspect signal from U3 feeds U5's "Dual-yaml workload mechanics" section. If any tc.duration > 870 s occurs, the report should explicitly note it as a deviation from the smoke baseline.
- `_cloud_cache.env` will be overwritten by U3's upload. Subsequent iOS runs need fresh upload (gotcha #9 — no API to pin-by-platform).
- The plan deliberately avoids comparing this dual-yaml run to the existing single-yaml `cloud_benchmark_loop` 100-rep baseline as a primary lens. If the dual-vs-single comparison is desired later, treat it as a follow-up analysis using the same CSV columns plus a second cloud column.

---

## Sources & References

- Smoke baseline: build `6cbf79e4af4872135a2eb0373c6498e28c61cbfb` (2026-05-13 18:32, tag `multitest-execute-v2`), artifacts under `results/cloud_20260513_183231/`
- Runner: `cloud_run_android.sh`
- Runbook (platform gotchas): `docs/runbooks/cloud-maestro-gotchas.md`
- Runbook (pipeline): `docs/runbooks/benchmark-report.md`
- iOS report precedent: `analysis/maestro_ios_benchmark_report.html`, `analysis/local_vs_cloud_ios_comparison_20260510_191348.csv`
- Local-Android baseline (pinned): `android/local/results/local_android_final_report_20260510_231553.csv` + `local_android_sessions_report_20260510_231553.csv`
- Percentile math reference: `aggregate_results.py`
- CSV emission reference: `aggregate_unified_report.py`
- Original 100-rep cloud workload (the source of the 50-rep split): `SAMPLE_ANDROID_TEST COPY/cloud_benchmark_loop.yaml`
