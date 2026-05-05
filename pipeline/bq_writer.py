"""BigQuery DDL + INSERT SQL builders for the Maestro benchmark rollup table.

This module emits SQL strings only — execution happens elsewhere (the Claude
session's BigQuery MCP). Keeping execution out of the module preserves
testability without a live BQ connection.

Schema reference: docs/plans/2026-05-04-001-feat-maestro-benchmark-pipeline-plan.md
(High-Level Technical Design, BQ table shape).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable

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


# --- INSERT SQL builder (U6) -------------------------------------------------


class MissingRequiredColumnError(Exception):
    """Raised when a RollupRow is missing a value for a REQUIRED BQ column."""


_TABLE_FQN_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$")


def _validate_table_fqn(fqn: str) -> None:
    if not isinstance(fqn, str) or not _TABLE_FQN_RE.match(fqn):
        raise ValueError(f"invalid table fully-qualified name: {fqn!r}")


def _escape_string(value: str) -> str:
    """BigQuery string literal escaping. Doubles backslash and single-quotes."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _format_array_string(value: tuple[str, ...] | list[str]) -> str:
    """ARRAY<STRING> literal — empty list literal for empty arrays."""
    if not value:
        return "[]"
    items = ", ".join(f"'{_escape_string(str(v))}'" for v in value)
    return f"[{items}]"


def _format_literal(value: object, bq_type: str) -> str:
    """Render a Python value as a BigQuery literal of the given type. NULL on None."""
    if value is None:
        return "NULL"
    if bq_type == "STRING":
        return f"'{_escape_string(str(value))}'"
    if bq_type == "INT64":
        # accept floats from rounding paths but emit integer literal
        return str(int(value))
    if bq_type == "FLOAT64":
        return repr(float(value))  # repr preserves precision better than str
    if bq_type == "BOOL":
        return "TRUE" if value else "FALSE"
    if bq_type == "TIMESTAMP":
        if not isinstance(value, datetime):
            raise TypeError(f"TIMESTAMP value must be datetime, got {type(value).__name__}")
        # Force UTC representation; assume naive datetimes are already UTC.
        if value.tzinfo is None:
            iso = value.strftime("%Y-%m-%d %H:%M:%S")
        else:
            iso = value.astimezone(tz=value.tzinfo).strftime("%Y-%m-%d %H:%M:%S")
        return f"TIMESTAMP('{iso} UTC')"
    if bq_type.startswith("ARRAY<"):
        return _format_array_string(value if isinstance(value, (list, tuple)) else list(value))
    raise ValueError(f"unsupported BQ type for literal: {bq_type!r}")


# Mapping from BQ column name → RollupRow attribute name.
# Most match by name; the few exceptions live here.
_ROW_ATTR: dict[str, str] = {
    "capabilities_profile": "capabilities_profile",
}


def _row_value(row: object, column_name: str) -> object:
    attr = _ROW_ATTR.get(column_name, column_name)
    return getattr(row, attr, None)


def build_insert_sql(*, table_fqn: str, rollup_rows: Iterable[object]) -> str:
    """Return a single INSERT statement with one VALUES tuple per rollup row.

    ``table_fqn`` is the fully-qualified ``project.dataset.table`` string.
    ``rollup_rows`` accepts any iterable of objects exposing the column names
    declared in :data:`COLUMNS` as attributes (intended consumer is
    :class:`pipeline.rollup.RollupRow`).

    Raises ``MissingRequiredColumnError`` when a REQUIRED column is None,
    ``ValueError`` on bad FQN or empty input.
    """
    _validate_table_fqn(table_fqn)
    rows = list(rollup_rows)
    if not rows:
        raise ValueError("no rollup rows to insert")

    column_names = [name for name, _, _ in COLUMNS]

    value_tuples: list[str] = []
    for idx, row in enumerate(rows):
        rendered = []
        for name, bq_type, mode in COLUMNS:
            v = _row_value(row, name)
            if v is None and mode == "REQUIRED":
                raise MissingRequiredColumnError(
                    f"row {idx}: required column {name!r} is None"
                )
            rendered.append(_format_literal(v, bq_type))
        value_tuples.append("(" + ", ".join(rendered) + ")")

    columns_csv = ", ".join(column_names)
    values_block = ",\n  ".join(value_tuples)
    return f"INSERT INTO `{table_fqn}` ({columns_csv})\nVALUES\n  {values_block};"
