# perf_bench_maestro

Maestro performance benchmark framework for the BrowserStack *Framework
Performance Benchmarking* spec (`docs/spec/framework-performance-benchmarking-040526.pdf`).

Measures end-to-end session timings across four cells, compares cloud
(BrowserStack App Automate Maestro v2) against local execution, and
produces an HTML + email report.

## The four-cell matrix

| Cell | Tool | Driver | Default device |
|---|---|---|---|
| **local-Android** | `android/local/run_benchmark.sh` | local `adb` + Maestro CLI | physical device on USB |
| **cloud-Android** | `android/cloud/cloud_run_android.sh` | BS App Automate Maestro v2 | `Samsung Galaxy S24-14.0` |
| **local-iOS** | `ios/local/run_benchmark.sh` | local `xcrun devicectl` + Maestro CLI | physical device on USB |
| **cloud-iOS** | `ios/cloud/cloud_run_ios.sh` | BS App Automate Maestro v2 | `iPhone 17-26` |

Each cell writes a per-session CSV under `<cell>/results/`. The pipeline
in `pipeline/` aggregates the four CSVs into one BQ row + one email per
run-set (see `docs/runbooks/benchmark-report.md`).

## Prerequisites

| Need | Why | How |
|---|---|---|
| Python 3.12+ | aggregators, pipeline, tests | system or pyenv |
| Maestro CLI | local cells | `curl -fsSL "https://get.maestro.mobile.dev" \| bash` — installs to `~/.maestro/bin/maestro` |
| Xcode 15+ | local-iOS, building HelloBench app | Mac App Store |
| Android SDK + `adb` | local-Android | Android Studio or `brew install android-platform-tools` |
| BrowserStack credentials | cloud cells | export `BROWSERSTACK_USERNAME` and `BROWSERSTACK_ACCESS_KEY` |
| gcloud auth (BigQuery) | aggregating cloud results | done inside a Claude session via the BigQuery MCP — no local `gcloud` needed |
| Test deps | unit tests | `pip install -r requirements-dev.txt` |

`MAESTRO` env var overrides the Maestro binary path (default
`~/.maestro/bin/maestro`).

### iOS bundle ID

The default is `com.vinitg.HelloBench` (the team-prefix the original
HelloBench.ipa was signed with). If you re-sign the app:

1. In Xcode, change `PRODUCT_BUNDLE_IDENTIFIER` + `DEVELOPMENT_TEAM` on the
   HelloBench target and rebuild the `.ipa`.
2. Export `IOS_BUNDLE_ID=<your-bundle-id>` before invoking any iOS runner.

The local runners pass it to Maestro via `--env`; the cloud runner
substitutes it into the flow yaml at zip time (independent of any
BS-side env interpolation).

## Apps

`apps/` is gitignored (binaries). The next person needs to populate it:

| File | What | How to obtain |
|---|---|---|
| `apps/WikipediaSample.apk` | Wikipedia alpha — used by every Android cell | Maestro samples repo or BS sample-apps page |
| `apps/HelloBench.ipa` | Bare SwiftUI search app — used by every current iOS cell | Build from `ios/local/HelloBench/HelloBench.xcodeproj` (Release, generic iOS device archive → export as ad-hoc .ipa). If you re-sign with your own Apple Developer team, set `IOS_BUNDLE_ID` (see below) |
| `apps/BrowserStack-SampleApp.ipa` | Legacy iOS app — no longer used by the canonical benchmark | Keep for reference or delete |

## How to run a cell

Each runner script has its own `--help`-equivalent header at the top of
the file. The common defaults:

```bash
# Local-Android — physical device on USB, 100 reps of benchmark_loop.yaml
cd android/local && ./prepare_device.sh   # one-time, idempotent
./run_benchmark.sh -i 100 -t baseline

# Cloud-Android — 100 sessions on Samsung Galaxy S24-14.0
cd android/cloud && ./cloud_run_android.sh -n 100 -t baseline

# Local-iOS — physical device on USB, 1 rep smoke
cd ios/local && ./run_benchmark.sh -i 1 -t smoke

# Cloud-iOS — 100 sessions on iPhone 17-26
cd ios/cloud && ./cloud_run_ios.sh -n 100 -t baseline
```

For the formal Android local benchmark setup (device prep details, flow
sizing math, aggregation), see `docs/reference/formal-benchmark-android.md`.

## Reading results

- Per-session CSVs: `<cell>/results/<run-id>/sessions.csv`
- Cell-level final + sessions reports: `<cell>/results/local_*_final_report_*.csv`, `*_sessions_report_*.csv`
- Cross-cell HTML reports: `analysis/maestro_{android,ios}_benchmark_report.html`
- Cross-cell comparison CSV: `analysis/local_vs_cloud_*_comparison_*.csv`
- BQ rollup + email: produced by `pipeline/cli.py` (see runbook)

## Current state (as of 2026-05-22)

| Cell | Status |
|---|---|
| local-Android | 100-rep baseline complete (2026-05-10 run, see `android/local/results/local_android_*_20260510_231553.csv`) |
| cloud-Android | 100-session multitest baseline complete (build_id `8ed0b0edc7e137a39670472e4855b521e04a6889`, 2026-05-18). Cloud beats local by ~8% at session-total P90 |
| local-iOS | Baseline complete (2026-05-08 run `20260508_181354`, 10 sessions × 115-rep search loop, see `ios/local/results/local_ios_*_20260508_200720.csv`) |
| cloud-iOS | 100-session baseline complete (build `82920fe…`, 2026-05-04, see `docs/reference/benchmark-report-ios-cloud.md`) |

Outstanding: final unified four-cell BQ rollup + email via `pipeline/cli.py`
(the pipeline refuses to send when any cell is missing — exits 2).

## docs/ map

| Path | What |
|---|---|
| `docs/spec/` | The PROD spec PDFs the project targets — start here for *why* this exists |
| `docs/runbooks/cloud-maestro-gotchas.md` | 16 hard-won BS platform pitfalls (900 s `idleTimeout` cap, device-pool stalls, BQ ingestion lag, no-stop API, …) — **read before debugging any cloud failure** |
| `docs/runbooks/benchmark-report.md` | How to produce the four-cell BQ rollup + email from a completed run-set |
| `docs/reference/formal-benchmark-android.md` | Android formal-mode run instructions, phase math, flow sizing |
| `docs/reference/phase-definitions.md` | What each timing column in the CSV means + which are cloud-only |
| `docs/reference/session-lifecycle.md` | Conceptual walk-through: what happens from "press go" to "session ends" |
| `docs/reference/benchmark-report-ios-cloud.md` | Cloud-iOS write-up + recommendations (used as the template for the Android write-up) |
| `docs/reference/smoke-results-android.md` | Original local-Android smoke validation (historical) |
| `docs/plans/` | Point-in-time plan documents (chronological record of three work tracks) |

## Common gotchas (read these once)

The numbered list lives in `docs/runbooks/cloud-maestro-gotchas.md`. The
ones that bite hardest:

- **`idleTimeout` is a hard wall-time cap, not an idle timer.** BS App
  Automate Maestro v2 enforces 900 s max regardless of activity. Flows
  sized for the 1198 s Android / 733 s iOS PDF P90 targets will be
  killed mid-loop. Reduce reps to land under 900 s.
- **BS dashboard `PASSED` counts testcases, not sessions**, and the
  duration counter is cumulative pool-time, not remaining ETA. Trust
  the BS API JSON, not the dashboard.
- **BQ ingestion lag is ~60 minutes** after a cloud session ends. The
  pipeline retries automatically; expect to wait if the row count is
  short.
- **No public API to stop a running BS build** — you can only stop from
  the dashboard. Plan around long-running builds.
- **Cloud-Android runner has a known transient `curl: (56) Recv failure`**
  in the poll loop. The BS build continues fine — recovery procedure
  is in the runbook.

## Layout

```
perf_bench_maestro/
├── android/{local,cloud}/                 # Android cells: runners, flows, results
├── ios/{local,cloud}/                     # iOS cells: runners, flows, results
│   └── local/HelloBench/                  # SwiftUI app the iOS flow drives
├── pipeline/                              # Four-cell aggregator + BQ writer + email renderer
├── analysis/                              # Cross-cell HTML reports + comparison CSVs
├── apps/                                  # .apk / .ipa binaries (gitignored — populate manually)
├── docs/                                  # spec, runbooks, reference, plans
├── results/                               # cross-cell run artifacts (per-run dirs gitignored)
├── tests/                                 # unit tests for the aggregators
├── aggregate_results.py                   # legacy single-cell aggregator (local-Android)
├── aggregate_android_cloud_report.py      # cloud-Android-side aggregator + report renderer
├── aggregate_unified_report.py            # cross-cell aggregator producing the analysis/ CSVs
├── parse_maestro_log.py                   # extracts execution_ms + per-command timings from Maestro debug logs
└── requirements-dev.txt                   # pytest only
```

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests cover the aggregators and the pipeline. They don't talk to BQ or
BrowserStack — fixtures in `tests/fixtures/` stand in for live responses.
