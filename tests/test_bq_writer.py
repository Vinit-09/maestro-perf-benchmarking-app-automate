"""Unit tests for pipeline.bq_writer DDL + (later) INSERT builders."""

from __future__ import annotations

import pytest

from pipeline.bq_writer import COLUMNS, build_create_table_ddl


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
