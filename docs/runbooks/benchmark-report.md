# Runbook — Maestro benchmark health-report email

How to take a completed four-cell run-set (local-Android, cloud-Android, local-iOS, cloud-iOS) and produce the BigQuery rollup row + the consolidated email.

The pipeline is split across two surfaces:

- **`pipeline/`** (this repo) — pure Python, owns aggregation, gating, SQL & HTML rendering. No live BQ or email I/O.
- **The Claude session** — owns BQ writes (via the BigQuery MCP) and email send (via the Gmail MCP). The pipeline emits an `action.json` describing what to fire; you ask Claude to fire it.

This split keeps the Python module testable without auth and uses the connectors that are already authenticated in your session.

---

## Pre-conditions

1. **All four cells of the matrix have been benchmarked.** The pipeline refuses to send when any cell is missing — it prints which cells are missing and exits 2.
   - `local_android`: `run_benchmark.sh` produced a `results/<timestamp>/sessions.csv` with at least one passing session.
   - `cloud_android`: a single-build cloud run produced a BS build_id, and that build's per-session BQ rows have ingested.
   - `local_ios`: same shape as local-Android (a sessions.csv from a local-iOS runner).
   - `cloud_ios`: same as cloud-Android.
2. **The BQ destination table exists.** One-time CREATE step described in step 5 below.
3. **Both MCP connectors are authenticated** in your Claude session: BigQuery and Gmail. If not:
   - Run `/mcp` and complete OAuth for each connector.
4. **Your venv is set up:** `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.

---

## Step 1 — Prepare cloud-cell BQ exports

For each cloud cell, ask Claude to run a SELECT against `app_automate_test_sessions_partitioned` for that build, and dump the result to a local JSON file.

The query needs at minimum these columns (matching what `pipeline.cells.load_cloud_cell` expects):

```sql
SELECT
  hashed_id,
  build_id,
  product.performance.device_region AS device_region,
  duration AS execution_s,
  CAST(firecmd_time AS INT64) AS firecmd_ms,
  CAST(app_download_time AS INT64) AS app_dl_ms,
  CAST(app_install_time AS INT64) AS app_install_ms,
  CAST(test_download_time AS INT64) AS test_dl_ms,
  CAST(test_install_time AS INT64) AS test_install_ms,
  CAST(product.performance.total_stop_time AS INT64) AS stop_ms,
  -- queue reason buckets — fill from product.performance.queued_*_time fields,
  -- substitute 0 when has_queued_* is false
  IFNULL(IF(product.performance.has_queued_device_tier,
            product.performance.queued_device_tier_time, 0), 0) AS waiting_device_tier_ms,
  IFNULL(IF(product.performance.has_queued_async_signing,
            product.performance.queued_async_signing_time, 0), 0) AS waiting_async_signing_ms,
  IFNULL(IF(product.performance.has_queued_soft_nta,
            product.performance.queued_soft_nta_time, 0), 0) AS waiting_region_pool_ms,
  -- "no parallel available" not directly tracked; surface 0 for now
  0 AS waiting_no_parallel_ms
FROM `browserstack-production.app_automate.app_automate_test_sessions_partitioned`
WHERE DATE(created_day) BETWEEN '<start>' AND '<end>'
  AND framework = 'maestro'
  AND build_id = '<your_build_id>'
```

Save the response JSON to e.g. `results/cloud_<timestamp>/bq_response.json`. The exact filename doesn't matter — the run-set descriptor points at it.

---

## Step 2 — Construct the run-set descriptor

Create a JSON file (e.g. `run_sets/2026-05-05-baseline.json`):

```json
{
  "run_set_id": "2026-05-05-baseline",
  "capability_profile": "defaults",
  "bq_table_fqn": "browserstack-production.app_automate.maestro_benchmark_metrics_aggregated",
  "email_recipients": ["you@browserstack.com"],
  "cells": {
    "local_android": { "results_dir": "results/20260430_175317" },
    "cloud_android": { "bq_response_path": "results/cloud_android_xxx/bq_response.json" },
    "local_ios":     { "results_dir": "results/20260505_local_ios" },
    "cloud_ios":     { "bq_response_path": "results/cloud_20260504_163420/bq_response.json" }
  }
}
```

Cells with empty entries (`{}`) or missing keys are treated as missing — the gate will list them.

Paths in the descriptor can be absolute or relative to the descriptor file's directory.

---

## Step 3 — Run the pipeline

```bash
.venv/bin/python -m pipeline.cli \
  --run-set run_sets/2026-05-05-baseline.json \
  --out results/action_2026-05-05.json
```

Exit code:

- **0** — gate passed; `action.json` contains `bq_insert_sql` + `email`. Proceed to step 4.
- **2** — gate blocked; `action.json` contains `error.missing_cells` and an operator-friendly `message`. Run the missing cell(s) and re-run this step.

For a quick check without writing the file: add `--dry-run` to print to stdout.

---

## Step 4 — Fire the action via Claude

Once you have a valid `action.json` from step 3, ask Claude (in this session) something like:

> Read `results/action_2026-05-05.json`. Execute the `bq_insert_sql` against `browserstack-production` via the BigQuery MCP, then send the `email` payload to the listed recipients via the Gmail MCP.

Claude will:

1. Read `action.json`.
2. Call `mcp__claude_ai_Google_Cloud_BigQuery__execute_sql` with the `bq_insert_sql` string.
3. Call the Gmail MCP `send_message`-style tool with the `email.subject` + `email.body_html` + `email.recipients`.

Both calls are durable artefacts — even if the Gmail send fails, `action.json` keeps the rendered HTML so you can re-attempt without re-running aggregation.

---

## Step 5 — One-time: create the BQ table

Before step 4 ever works, the destination table needs to exist. Generate the DDL:

```bash
.venv/bin/python - <<'PY'
from pipeline.bq_writer import build_create_table_ddl
print(build_create_table_ddl(
    project="browserstack-production",
    dataset="app_automate",
    table="maestro_benchmark_metrics_aggregated",
))
PY
```

Coordinate with the BS data team on the final table name and dataset before executing — this lives in the production project, not a sandbox. Once confirmed, ask Claude to fire the DDL through the BigQuery MCP.

---

## What the email looks like

- **Subject:** `Maestro Benchmark / <run_set_id> / <capability_profile> / <status>`
- **Body:**
  - Header: title + status (4-cell complete / partial / incomplete)
  - Banner: appears only when cells are missing or partial; lists which
  - Summary table: one row per cell (n_total, exec P50, exec P90, start P50, start P90)
  - Per-cell tables: one row per region in that cell, with full metric set (waiting / start / exec / app_install / stop)
  - Footer: source build_ids (cloud), source run_ids (local), aggregated_at timestamp, footnote about NULL stop_time for Maestro

---

## Failure modes & resolutions

| Symptom | Cause | Resolution |
|---|---|---|
| Exit 2, "missing cells: local_ios" | `local_ios` results_dir absent or empty | Run a local iOS benchmark; populate the `results_dir` |
| Exit 2, "no successful 'ios' sessions" | All sessions in the dir failed (exit_code != 0) | Investigate the local-iOS runner; the rollup needs at least one passing session |
| `action.json` has `partial_cells: ["..."]` but still complete | A cell has data but n < 30 | Acceptable — email annotates the cell. Or re-run that cell to grow N |
| BQ INSERT in Claude returns "table not found" | Step 5 hasn't run | Run step 5 first |
| Stop columns in email all show `—*` | Expected — `total_stop_time` is NULL in BQ for every Maestro session as of 2026-05-04 | Out of pipeline scope; tracked as recommendation A4 in `BENCHMARK_REPORT_IOS_CLOUD.md` |
| BQ ingestion lag — cloud BQ response has fewer rows than the build had sessions | Some sessions haven't been written yet | Wait 60+ minutes after last session ended, then re-run step 1 |

---

## Files this runbook references

- `pipeline/cli.py` — the CLI invoked in step 3
- `pipeline/bq_writer.py` — the DDL + INSERT SQL builders
- `pipeline/email_renderer.py` — the subject + body
- `pipeline/cells.py` + `pipeline/rollup.py` + `pipeline/gate.py` — the aggregation core
- `docs/plans/2026-05-04-001-feat-maestro-benchmark-pipeline-plan.md` — the plan that produced this pipeline
- `generated_docs/BENCHMARK_REPORT_IOS_CLOUD.md` — recommendation A4 (NULL stop_time gap)
