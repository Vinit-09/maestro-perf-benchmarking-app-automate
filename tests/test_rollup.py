"""Tests for pipeline.rollup percentile + per-cut aggregation (U5)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.cells import Cell, CellSession, load_cloud_cell
from pipeline.rollup import (
    DEFAULT_MIN_SAMPLE,
    RollupRow,
    percentile_nearest_rank,
    rollup,
)

FIXTURES = Path(__file__).parent / "fixtures"
AGGREGATED_AT = datetime(2026, 5, 5, 0, 0, 0, tzinfo=timezone.utc)


def _session(
    *,
    waiting_ms: int | None = 0,
    start_ms: int | None = 10000,
    execution_s: float | None = 720.0,
    app_dl_ms: int | None = 100,
    app_install_ms: int | None = 1500,
    stop_ms: int | None = None,
    region: str | None = None,
    source_id: str = "x",
) -> CellSession:
    return CellSession(
        waiting_ms=waiting_ms,
        waiting_reason_no_parallel_ms=0,
        waiting_reason_device_tier_ms=0,
        waiting_reason_async_signing_ms=0,
        waiting_reason_region_pool_ms=0,
        start_ms=start_ms,
        execution_s=execution_s,
        app_dl_ms=app_dl_ms,
        app_install_ms=app_install_ms,
        test_dl_ms=0,
        test_install_ms=2000,
        stop_ms=stop_ms,
        region=region,
        source_id=source_id,
    )


def _cell(
    name: str,
    *,
    os_name: str | None = None,
    sessions: list[CellSession] | None = None,
    capability: str = "defaults",
    source_paths: list[str] | None = None,
) -> Cell:
    return Cell(
        name=name,
        framework="maestro",
        os=os_name or ("android" if "android" in name else "ios"),
        capability_profile=capability,
        sessions=sessions or [],
        source_paths=source_paths or [],
    )


# --- percentile_nearest_rank ----------------------------------------------------


class TestPercentileNearestRank:
    def test_p50_of_10_values_is_5th(self) -> None:
        # nearest-rank: P50 of [100..1000] is value at index ceil(0.5*10)-1 = 4 → 500
        assert percentile_nearest_rank([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000], 0.50) == 500

    def test_p90_of_10_values_is_9th(self) -> None:
        assert percentile_nearest_rank([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000], 0.90) == 900

    def test_unsorted_input_is_sorted(self) -> None:
        assert percentile_nearest_rank([1000, 100, 500, 700, 300], 0.50) == 500

    def test_single_value(self) -> None:
        assert percentile_nearest_rank([42.0], 0.50) == 42.0

    def test_empty_returns_none(self) -> None:
        assert percentile_nearest_rank([], 0.50) is None

    def test_all_none_returns_none(self) -> None:
        assert percentile_nearest_rank([None, None], 0.50) is None

    def test_filters_none_then_computes(self) -> None:
        assert percentile_nearest_rank([None, 100, None, 200, 300], 0.50) == 200

    def test_p0_returns_min(self) -> None:
        assert percentile_nearest_rank([5, 1, 4, 2, 3], 0) == 1

    def test_p1_returns_max(self) -> None:
        assert percentile_nearest_rank([5, 1, 4, 2, 3], 1) == 5


# --- rollup ---------------------------------------------------------------------


class TestRollup:
    def test_single_cell_single_region_happy_path(self) -> None:
        sessions = [_session(execution_s=v / 1.0) for v in [700, 705, 710, 715, 720, 725, 730, 735, 740, 800]]
        cell = _cell("cloud_ios", sessions=sessions, source_paths=["build_abc"])

        rows = rollup([cell], run_set_id="rs1", aggregated_at=AGGREGATED_AT)
        assert len(rows) == 1
        r = rows[0]
        assert r.run_set_id == "rs1"
        assert r.cell == "cloud_ios"
        assert r.framework == "maestro"
        assert r.os == "ios"
        assert r.region is None
        assert r.capabilities_profile == "defaults"
        assert r.source_build_ids == ("build_abc",)
        assert r.source_run_ids == ()
        assert r.n_sessions == 10
        assert r.low_sample is False
        # nearest-rank: P50 of [700..800] (10 values) = idx 4 → 720
        assert r.execution_p50_s == 720
        # P90 = idx 8 → 740
        assert r.execution_p90_s == 740
        assert r.aggregated_at == AGGREGATED_AT

    def test_low_sample_emits_row_with_null_stats(self) -> None:
        sessions = [_session(execution_s=v) for v in [700, 800, 900]]  # n=3 < 5
        cell = _cell("local_android", sessions=sessions)

        rows = rollup([cell], run_set_id="rs1", aggregated_at=AGGREGATED_AT)
        assert len(rows) == 1
        r = rows[0]
        assert r.n_sessions == 3
        assert r.low_sample is True
        assert r.execution_p50_s is None
        assert r.execution_p90_s is None
        assert r.start_p50_ms is None

    def test_multi_region_emits_one_row_per_region(self) -> None:
        sessions = (
            [_session(region="ap-south-1", source_id=f"a{i}") for i in range(5)]
            + [_session(region="us-east-1", source_id=f"u{i}") for i in range(5)]
        )
        cell = _cell("cloud_ios", sessions=sessions, source_paths=["build_xyz"])

        rows = rollup([cell], run_set_id="rs1", aggregated_at=AGGREGATED_AT)
        assert len(rows) == 2
        regions = {r.region for r in rows}
        assert regions == {"ap-south-1", "us-east-1"}
        assert all(r.cell == "cloud_ios" for r in rows)
        assert all(r.n_sessions == 5 for r in rows)

    def test_empty_cell_not_emitted(self) -> None:
        cell = _cell("local_ios", sessions=[])
        rows = rollup([cell], run_set_id="rs1", aggregated_at=AGGREGATED_AT)
        assert rows == []

    def test_maestro_stop_propagates_null(self) -> None:
        # Every session has stop_ms=None (Maestro reality)
        sessions = [_session(stop_ms=None) for _ in range(10)]
        cell = _cell("cloud_ios", sessions=sessions)

        rows = rollup([cell], run_set_id="rs1", aggregated_at=AGGREGATED_AT)
        assert rows[0].stop_p50_ms is None
        assert rows[0].stop_p90_ms is None

    def test_local_cell_source_run_ids_uses_dirname(self) -> None:
        sessions = [_session() for _ in range(5)]
        cell = _cell(
            "local_android",
            sessions=sessions,
            source_paths=["/Users/vinits/perf_bench_maestro/results/20260430_175317"],
        )
        rows = rollup([cell], run_set_id="rs1", aggregated_at=AGGREGATED_AT)
        assert rows[0].source_run_ids == ("20260430_175317",)
        assert rows[0].source_build_ids == ()

    def test_multiple_cells_multiple_rows(self) -> None:
        local = _cell(
            "local_android",
            sessions=[_session(region=None) for _ in range(8)],
            source_paths=["/x/results/20260101"],
        )
        cloud = _cell(
            "cloud_ios",
            sessions=(
                [_session(region="ap-south-1") for _ in range(6)]
                + [_session(region="eu-west-2") for _ in range(7)]
            ),
            source_paths=["build_a", "build_b"],
        )
        rows = rollup([local, cloud], run_set_id="rs1", aggregated_at=AGGREGATED_AT)
        # 1 row for local (None region) + 2 rows for cloud (2 regions) = 3 rows total
        assert len(rows) == 3
        cells_seen = {(r.cell, r.region) for r in rows}
        assert cells_seen == {
            ("local_android", None),
            ("cloud_ios", "ap-south-1"),
            ("cloud_ios", "eu-west-2"),
        }

    def test_custom_min_sample(self) -> None:
        sessions = [_session() for _ in range(3)]
        cell = _cell("cloud_ios", sessions=sessions)
        # default min_sample=5 → low_sample
        assert rollup([cell], run_set_id="rs", aggregated_at=AGGREGATED_AT)[0].low_sample is True
        # min_sample=2 → not low_sample
        assert (
            rollup([cell], run_set_id="rs", aggregated_at=AGGREGATED_AT, min_sample=2)[
                0
            ].low_sample
            is False
        )

    def test_default_min_sample_constant(self) -> None:
        assert DEFAULT_MIN_SAMPLE == 5


class TestRollupOnCloudFixture:
    """Integration: rollup math on the cloud_ios_sample.json fixture."""

    def test_rollup_groups_fixture_by_region(self) -> None:
        bq_response = json.loads((FIXTURES / "cloud_ios_sample.json").read_text())
        cell = load_cloud_cell(bq_response, cell_name="cloud_ios", os="ios")

        rows = rollup([cell], run_set_id="rs-fixture", aggregated_at=AGGREGATED_AT)

        # The fixture has 7 ap-south-1, 2 us-east-1, 1 eu-west-2 sessions.
        regions = {r.region: r.n_sessions for r in rows}
        assert regions == {"ap-south-1": 7, "us-east-1": 2, "eu-west-2": 1}

        # ap-south-1 cell has 7 sessions (≥ min_sample=5) → real P50/P90
        ap = next(r for r in rows if r.region == "ap-south-1")
        assert ap.low_sample is False
        assert ap.execution_p50_s is not None
        assert ap.execution_p90_s is not None
        # exec values in ap-south-1: [720, 717, 705, 812, 750, 716, 50]
        # sorted: [50, 705, 716, 717, 720, 750, 812]
        # P50: ceil(0.5*7)=4 → s[3] = 717
        # P90: ceil(0.9*7)=7 → s[6] = 812
        assert ap.execution_p50_s == 717
        assert ap.execution_p90_s == 812

        # us-east-1 has 2 sessions (< min_sample=5) → low_sample, NULL stats
        use = next(r for r in rows if r.region == "us-east-1")
        assert use.low_sample is True
        assert use.execution_p50_s is None

    def test_stop_p50_is_null_for_all_fixture_rows(self) -> None:
        bq_response = json.loads((FIXTURES / "cloud_ios_sample.json").read_text())
        cell = load_cloud_cell(bq_response, cell_name="cloud_ios", os="ios")
        rows = rollup([cell], run_set_id="rs-fixture", aggregated_at=AGGREGATED_AT)
        # Every fixture row has stop_ms=None (Maestro reality), so every rollup row's stop_p50 is None
        non_low_sample_rows = [r for r in rows if not r.low_sample]
        assert non_low_sample_rows  # at least one
        assert all(r.stop_p50_ms is None for r in non_low_sample_rows)
        assert all(r.stop_p90_ms is None for r in non_low_sample_rows)
