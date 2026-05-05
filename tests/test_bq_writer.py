"""Unit tests for pipeline.bq_writer DDL + INSERT builders."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pipeline.bq_writer import (
    COLUMNS,
    MissingRequiredColumnError,
    build_create_table_ddl,
    build_insert_sql,
)
from pipeline.rollup import RollupRow


class TestBuildCreateTableDdl:
    def test_happy_path_includes_all_columns_and_partition(self) -> None:
        ddl = build_create_table_ddl(
            project="browserstack-production",
            dataset="app_automate",
            table="maestro_benchmark_metrics_aggregated",
        )

        assert "CREATE TABLE IF NOT EXISTS" in ddl
        assert (
            "`browserstack-production.app_automate.maestro_benchmark_metrics_aggregated`"
            in ddl
        )
        assert "PARTITION BY DATE(aggregated_at)" in ddl

        # every declared column appears in the DDL by name
        for name, _, _ in COLUMNS:
            assert name in ddl, f"column {name!r} missing from DDL"

        # required columns carry NOT NULL
        assert "run_set_id STRING NOT NULL" in ddl
        assert "n_sessions INT64 NOT NULL" in ddl

        # nullable columns don't carry NOT NULL
        assert "region STRING NOT NULL" not in ddl
        assert "stop_p50_ms INT64 NOT NULL" not in ddl

        # ARRAY columns rendered with their element type
        assert "source_build_ids ARRAY<STRING>" in ddl
        assert "source_run_ids ARRAY<STRING>" in ddl

    def test_rejects_table_with_special_chars(self) -> None:
        with pytest.raises(ValueError, match="invalid 'table'"):
            build_create_table_ddl(
                project="p",
                dataset="d",
                table="bad-table-name",  # dashes not allowed in BQ table identifiers
            )

    def test_rejects_dataset_with_special_chars(self) -> None:
        with pytest.raises(ValueError, match="invalid 'dataset'"):
            build_create_table_ddl(
                project="p",
                dataset="bad dataset",  # spaces not allowed
                table="t",
            )

    def test_rejects_invalid_project_id(self) -> None:
        with pytest.raises(ValueError, match="invalid project id"):
            build_create_table_ddl(
                project="123starts-with-digit",
                dataset="d",
                table="t",
            )

    def test_accepts_dashed_project_id(self) -> None:
        # GCP project IDs commonly contain dashes; dataset/table identifiers do not
        ddl = build_create_table_ddl(
            project="browserstack-production",
            dataset="app_automate",
            table="t",
        )
        assert "browserstack-production" in ddl

    def test_columns_appear_in_declared_order(self) -> None:
        # Ordering matters for INSERT VALUES tuples in U6.
        ddl = build_create_table_ddl(project="p", dataset="d", table="t")
        positions = {name: ddl.index(name) for name, _, _ in COLUMNS}
        # walk the COLUMNS list and confirm strictly increasing positions
        prev = -1
        for name, _, _ in COLUMNS:
            assert positions[name] > prev, f"{name} out of order"
            prev = positions[name]


# ---------------------------------------------------------------------------
# INSERT builder tests (U6)
# ---------------------------------------------------------------------------


def _row(
    *,
    cell: str = "cloud_ios",
    region: str | None = "ap-south-1",
    n_sessions: int = 50,
    low_sample: bool = False,
    execution_p50_s: float | None = 720.0,
    execution_p90_s: float | None = 810.0,
    stop_p50_ms: int | None = None,
    stop_p90_ms: int | None = None,
    source_build_ids: tuple[str, ...] = ("build-abc",),
    source_run_ids: tuple[str, ...] = (),
) -> RollupRow:
    return RollupRow(
        run_set_id="rs-2026-05-05",
        cell=cell,
        framework="maestro",
        os="ios" if "ios" in cell else "android",
        region=region,
        capabilities_profile="defaults",
        source_build_ids=source_build_ids,
        source_run_ids=source_run_ids,
        n_sessions=n_sessions,
        low_sample=low_sample,
        waiting_p50_ms=0,
        waiting_p90_ms=0,
        waiting_reason_no_parallel_p50_ms=0,
        waiting_reason_no_parallel_p90_ms=0,
        waiting_reason_device_tier_p50_ms=0,
        waiting_reason_device_tier_p90_ms=0,
        waiting_reason_async_signing_p50_ms=0,
        waiting_reason_async_signing_p90_ms=0,
        waiting_reason_region_pool_p50_ms=0,
        waiting_reason_region_pool_p90_ms=0,
        start_p50_ms=12000,
        start_p90_ms=23000,
        execution_p50_s=execution_p50_s,
        execution_p90_s=execution_p90_s,
        app_download_p50_ms=65,
        app_download_p90_ms=860,
        app_install_p50_ms=1600,
        app_install_p90_ms=3700,
        stop_p50_ms=stop_p50_ms,
        stop_p90_ms=stop_p90_ms,
        aggregated_at=datetime(2026, 5, 5, 12, 30, 0, tzinfo=timezone.utc),
    )


class TestBuildInsertSql:
    def test_single_row_happy_path(self) -> None:
        sql = build_insert_sql(
            table_fqn="browserstack-production.app_automate.maestro_benchmark_metrics_aggregated",
            rollup_rows=[_row()],
        )
        assert sql.startswith(
            "INSERT INTO `browserstack-production.app_automate.maestro_benchmark_metrics_aggregated`"
        )
        assert "VALUES" in sql
        assert sql.rstrip().endswith(";")
        # column list mirrors COLUMNS order
        for name, _, _ in COLUMNS:
            assert name in sql

    def test_multiple_rows_join_with_comma_newlines(self) -> None:
        sql = build_insert_sql(
            table_fqn="p.d.t",
            rollup_rows=[_row(region="ap-south-1"), _row(region="us-east-1")],
        )
        # Each VALUES tuple lives on its own indented line.
        values_block = sql.split("VALUES", 1)[1]
        tuple_lines = [ln for ln in values_block.splitlines() if ln.lstrip().startswith("(")]
        assert len(tuple_lines) == 2
        # ap-south-1 row appears first, us-east-1 second
        assert "'ap-south-1'" in tuple_lines[0]
        assert "'us-east-1'" in tuple_lines[1]

    def test_null_stop_renders_as_null_literal(self) -> None:
        sql = build_insert_sql(table_fqn="p.d.t", rollup_rows=[_row(stop_p50_ms=None, stop_p90_ms=None)])
        # NULL appears as a bare keyword, not as a 'None' string
        assert ", NULL," in sql or sql.endswith("NULL);") or "NULL," in sql
        assert "'None'" not in sql

    def test_empty_array_renders_as_empty_brackets(self) -> None:
        sql = build_insert_sql(
            table_fqn="p.d.t",
            rollup_rows=[_row(source_build_ids=(), source_run_ids=("rs1",))],
        )
        # source_build_ids is empty → []
        assert "[]" in sql
        # source_run_ids has one item
        assert "['rs1']" in sql

    def test_string_with_apostrophe_is_escaped(self) -> None:
        row = _row(region="O'Hare")  # contrived but exercises the escape path
        sql = build_insert_sql(table_fqn="p.d.t", rollup_rows=[row])
        assert "'O\\'Hare'" in sql

    def test_timestamp_uses_bq_timestamp_function(self) -> None:
        sql = build_insert_sql(table_fqn="p.d.t", rollup_rows=[_row()])
        assert "TIMESTAMP('2026-05-05 12:30:00 UTC')" in sql

    def test_low_sample_with_null_stats_emits_nulls(self) -> None:
        row = _row(
            n_sessions=3,
            low_sample=True,
            execution_p50_s=None,
            execution_p90_s=None,
        )
        sql = build_insert_sql(table_fqn="p.d.t", rollup_rows=[row])
        assert ", TRUE," in sql  # low_sample
        # the execution_p50_s + p90_s positions both render as NULL
        assert sql.count("NULL") >= 2

    def test_required_field_none_raises(self) -> None:
        # n_sessions is REQUIRED; force-None it and expect a clean error.
        row = _row()
        # dataclass is frozen — use object.__setattr__ for the test
        object.__setattr__(row, "n_sessions", None)
        with pytest.raises(MissingRequiredColumnError, match="n_sessions"):
            build_insert_sql(table_fqn="p.d.t", rollup_rows=[row])

    def test_invalid_table_fqn_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid table"):
            build_insert_sql(table_fqn="bad name", rollup_rows=[_row()])

    def test_empty_rows_raises(self) -> None:
        with pytest.raises(ValueError, match="no rollup rows"):
            build_insert_sql(table_fqn="p.d.t", rollup_rows=[])

