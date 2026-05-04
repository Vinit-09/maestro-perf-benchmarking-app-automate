"""BigQuery DDL + INSERT SQL builders for the Maestro benchmark rollup table.

This module emits SQL strings only — execution happens elsewhere (the Claude
session's BigQuery MCP). Keeping execution out of the module preserves
testability without a live BQ connection.

Schema reference: docs/plans/2026-05-04-001-feat-maestro-benchmark-pipeline-plan.md
(High-Level Technical Design, BQ table shape).
"""

from __future__ import annotations

import re

# Stable column order shared between DDL and INSERT builders.
# Changing this list is a schema change — coordinate with downstream consumers.
COLUMNS: list[tuple[str, str, str]] = [
    # (name, bq_type, mode) — mode is "REQUIRED" or "NULLABLE"
    ("run_set_id", "STRING", "REQUIRED"),
    ("cell", "STRING", "REQUIRED"),
    ("framework", "STRING", "REQUIRED"),
    ("os", "STRING", "REQUIRED"),
    ("region", "STRING", "NULLABLE"),
    ("capabilities_profile", "STRING", "REQUIRED"),
    ("source_build_ids", "ARRAY<STRING>", "REPEATED"),
    ("source_run_ids", "ARRAY<STRING>", "REPEATED"),
    ("n_sessions", "INT64", "REQUIRED"),
    ("low_sample", "BOOL", "NULLABLE"),
    # waiting time — total + per reason bucket
    ("waiting_p50_ms", "INT64", "NULLABLE"),
    ("waiting_p90_ms", "INT64", "NULLABLE"),
    ("waiting_reason_no_parallel_p50_ms", "INT64", "NULLABLE"),
    ("waiting_reason_no_parallel_p90_ms", "INT64", "NULLABLE"),
    ("waiting_reason_device_tier_p50_ms", "INT64", "NULLABLE"),
    ("waiting_reason_device_tier_p90_ms", "INT64", "NULLABLE"),
    ("waiting_reason_async_signing_p50_ms", "INT64", "NULLABLE"),
    ("waiting_reason_async_signing_p90_ms", "INT64", "NULLABLE"),
    ("waiting_reason_region_pool_p50_ms", "INT64", "NULLABLE"),
    ("waiting_reason_region_pool_p90_ms", "INT64", "NULLABLE"),
    # start time (firecmd analog) and total execution
    ("start_p50_ms", "INT64", "NULLABLE"),
    ("start_p90_ms", "INT64", "NULLABLE"),
    ("execution_p50_s", "FLOAT64", "NULLABLE"),
    ("execution_p90_s", "FLOAT64", "NULLABLE"),
    # supporting P1
    ("app_download_p50_ms", "INT64", "NULLABLE"),
    ("app_download_p90_ms", "INT64", "NULLABLE"),
    ("app_install_p50_ms", "INT64", "NULLABLE"),
    ("app_install_p90_ms", "INT64", "NULLABLE"),
    ("stop_p50_ms", "INT64", "NULLABLE"),
    ("stop_p90_ms", "INT64", "NULLABLE"),
    # bookkeeping
    ("aggregated_at", "TIMESTAMP", "REQUIRED"),
]


# BigQuery identifier rule: leading letter or underscore, then letters / digits / underscores.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_ident(value: str, role: str) -> None:
    if not isinstance(value, str) or not _IDENT_RE.match(value):
        raise ValueError(f"invalid {role!r} identifier: {value!r}")


def build_create_table_ddl(*, project: str, dataset: str, table: str) -> str:
    """Return the CREATE TABLE SQL for the rollup table.

    The table is DAY-partitioned on ``aggregated_at`` per the plan.
    """
    # GCP project IDs: leading letter, then letters / digits / dashes.
    if not isinstance(project, str) or not re.match(r"^[A-Za-z][A-Za-z0-9-]*$", project):
        raise ValueError(f"invalid project id: {project!r}")
    _validate_ident(dataset, "dataset")
    _validate_ident(table, "table")

    column_lines = []
    for name, bq_type, mode in COLUMNS:
        if bq_type.startswith("ARRAY<"):
            # ARRAY<T> columns are inherently REPEATED; render without REQUIRED/NULLABLE.
            column_lines.append(f"  {name} {bq_type}")
        elif mode == "REQUIRED":
            column_lines.append(f"  {name} {bq_type} NOT NULL")
        else:
            column_lines.append(f"  {name} {bq_type}")

    columns_sql = ",\n".join(column_lines)

    return (
        f"CREATE TABLE IF NOT EXISTS `{project}.{dataset}.{table}` (\n"
        f"{columns_sql}\n"
        f")\n"
        f"PARTITION BY DATE(aggregated_at)\n"
        f"OPTIONS(\n"
        f"  description=\"Maestro benchmark P50/P90 rollups, one row per "
        f"(run_set, cell, region, capabilities_profile). "
        f"See docs/plans/2026-05-04-001-feat-maestro-benchmark-pipeline-plan.md.\"\n"
        f");"
    )
