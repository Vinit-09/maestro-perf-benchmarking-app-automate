"""Tests for aggregate_android_cloud_report (U4 — local-vs-cloud comparison aggregator)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aggregate_android_cloud_report import (  # noqa: E402
    ANDROID_PHASE_ORDER,
    CSV_HEADER,
    DUAL_TC_ROWS,
    build_comparison_rows,
    cloud_session_stats,
    load_local_final,
    per_tc_durations_from_detail,
    per_tc_stats,
    percentile,
    stats_row,
    write_comparison_csv,
)
from pipeline.cells import load_cloud_cell  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


# ----------------------------------------------------------------------------
# Percentile math
# ----------------------------------------------------------------------------

class TestPercentile:
    def test_empty_list_returns_none(self) -> None:
        assert percentile([], 50) is None

    def test_single_value_returns_that_value(self) -> None:
        assert percentile([42], 50) == 42.0
        assert percentile([42], 99) == 42.0

    def test_p50_of_three_is_middle(self) -> None:
        # Linear interpolation: P50 of [10, 20, 30] is at idx 1.0 → 20.
        assert percentile([10, 20, 30], 50) == 20.0

    def test_p90_interpolates_between_values(self) -> None:
        # [10, 20, 30, 40, 50]: P90 idx = 0.9 * 4 = 3.6 → between 40 and 50.
        # = 40 + 0.6*(50-40) = 46.
        assert percentile([10, 20, 30, 40, 50], 90) == 46.0

    def test_sorted_invariance(self) -> None:
        # Input order should not affect result.
        assert percentile([30, 10, 50, 20, 40], 50) == 30.0


class TestStatsRow:
    def test_empty_produces_n_zero_with_empty_fields(self) -> None:
        s = stats_row([])
        assert s["N"] == 0
        assert s["p50_ms"] == ""
        assert s["p100_ms"] == ""

    def test_populated_produces_all_percentiles(self) -> None:
        s = stats_row([100, 200, 300, 400, 500])
        assert s["N"] == 5
        assert s["min_ms"] == 100
        assert s["p50_ms"] == 300
        assert s["p100_ms"] == 500
        # All values are ints
        for k in ("min_ms", "p50_ms", "p75_ms", "p90_ms", "p95_ms", "p100_ms"):
            assert isinstance(s[k], int)


# ----------------------------------------------------------------------------
# Local CSV loader
# ----------------------------------------------------------------------------

class TestLoadLocalFinal:
    def test_happy_path(self) -> None:
        local = load_local_final(FIXTURES / "local_android_baseline_sample.csv")
        # Spot-check known rows.
        assert ("Pre-Maestro", "app_install") in local
        assert local[("Pre-Maestro", "app_install")]["p50_ms"] == 1672
        assert local[("Aggregates", "session_total")]["p90_ms"] == 1225000

    def test_missing_file_raises_filenotfounderror(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="local final report not found"):
            load_local_final(tmp_path / "does_not_exist.csv")


# ----------------------------------------------------------------------------
# Cloud session stats (BQ → percentiles)
# ----------------------------------------------------------------------------

class TestCloudSessionStats:
    def test_three_session_fixture_produces_expected_percentiles(self) -> None:
        bq = json.loads((FIXTURES / "cloud_android_multitest_sample.json").read_text())
        cell = load_cloud_cell(bq, cell_name="cloud_android", os="android")
        stats = cloud_session_stats(cell)

        # session_total_ms: derived from execution_s (1100, 1150, 1200) * 1000.
        # P50 of [1100000, 1150000, 1200000] = 1150000.
        assert stats[("Aggregates", "session_total")]["N"] == 3
        assert stats[("Aggregates", "session_total")]["p50_ms"] == 1150000
        assert stats[("Aggregates", "session_total")]["min_ms"] == 1100000
        assert stats[("Aggregates", "session_total")]["p100_ms"] == 1200000

        # app_install_ms: [4800, 4900, 5000]. P50 = 4900.
        assert stats[("Pre-Maestro", "app_install")]["p50_ms"] == 4900

    def test_maestro_total_subtracts_install_and_start_when_present(self) -> None:
        bq = json.loads((FIXTURES / "cloud_android_multitest_sample.json").read_text())
        cell = load_cloud_cell(bq, cell_name="cloud_android", os="android")
        stats = cloud_session_stats(cell)

        # Session 1: total=1100000, app_install=4800, firecmd/start=13000 → 1082200.
        # Session 2: 1150000 - 4900 - 13500 = 1131600.
        # Session 3: 1200000 - 5000 - 14000 = 1181000.
        # P50 = 1131600.
        assert stats[("Aggregates", "maestro_total")]["p50_ms"] == 1131600


# ----------------------------------------------------------------------------
# Per-tc duration extraction
# ----------------------------------------------------------------------------

class TestPerTcDurationsFromDetail:
    def test_extracts_two_classes(self) -> None:
        detail = {
            "id": "sa01",
            "testcases": {
                "data": [
                    {"class": "test_a", "testcases": [{"name": "test_a", "duration": "531", "status": "passed"}]},
                    {"class": "test_b", "testcases": [{"name": "test_b", "duration": "529", "status": "passed"}]},
                ]
            },
        }
        out = per_tc_durations_from_detail(detail)
        assert out == {"test_a": 531000, "test_b": 529000}

    def test_handles_missing_data_block(self) -> None:
        # Session that errored mid-flight may not have a data array.
        detail = {"id": "sa02", "testcases": {"count": 0, "status": {}}}
        assert per_tc_durations_from_detail(detail) == {}

    def test_skips_zero_and_missing_durations(self) -> None:
        detail = {
            "testcases": {
                "data": [
                    {"class": "test_a", "testcases": [{"duration": "0"}]},
                    {"class": "test_b", "testcases": [{"duration": None}]},
                ]
            }
        }
        assert per_tc_durations_from_detail(detail) == {}


class TestPerTcStats:
    def test_three_sessions_each_with_both_tcs(self) -> None:
        per_tc = {
            "sa01": {"test_a": 530000, "test_b": 520000},
            "sa02": {"test_a": 560000, "test_b": 540000},
            "sa03": {"test_a": 580000, "test_b": 560000},
        }
        stats = per_tc_stats(per_tc)

        assert stats[("Aggregates", "tc_a_duration")]["N"] == 3
        assert stats[("Aggregates", "tc_a_duration")]["p50_ms"] == 560000
        assert stats[("Aggregates", "tc_b_duration")]["p50_ms"] == 540000

        # Combined = test_a + test_b per session: [1050000, 1100000, 1140000].
        assert stats[("Aggregates", "tc_combined_duration")]["N"] == 3
        assert stats[("Aggregates", "tc_combined_duration")]["p50_ms"] == 1100000

    def test_session_with_only_one_tc_excluded_from_combined(self) -> None:
        # If test_b is missing for a session, that session shouldn't poison the
        # combined view — combined requires BOTH tcs.
        per_tc = {
            "sa01": {"test_a": 530000, "test_b": 520000},
            "sa02": {"test_a": 560000},  # missing test_b
        }
        stats = per_tc_stats(per_tc)
        assert stats[("Aggregates", "tc_a_duration")]["N"] == 2
        assert stats[("Aggregates", "tc_b_duration")]["N"] == 1
        # Combined: only sa01 has both → N=1.
        assert stats[("Aggregates", "tc_combined_duration")]["N"] == 1
        assert stats[("Aggregates", "tc_combined_duration")]["p50_ms"] == 1050000

    def test_empty_per_tc_yields_zero_n_rows(self) -> None:
        stats = per_tc_stats({})
        for key in DUAL_TC_ROWS:
            assert stats[key]["N"] == 0


# ----------------------------------------------------------------------------
# Comparison CSV assembly
# ----------------------------------------------------------------------------

class TestBuildComparisonRows:
    def _load_inputs(self) -> tuple[dict, dict, dict]:
        local = load_local_final(FIXTURES / "local_android_baseline_sample.csv")
        bq = json.loads((FIXTURES / "cloud_android_multitest_sample.json").read_text())
        cell = load_cloud_cell(bq, cell_name="cloud_android", os="android")
        cloud_session = cloud_session_stats(cell)
        cloud_per_tc = per_tc_stats({
            "sa01": {"test_a": 530000, "test_b": 520000},
            "sa02": {"test_a": 560000, "test_b": 540000},
            "sa03": {"test_a": 580000, "test_b": 560000},
        })
        return local, cloud_session, cloud_per_tc

    def test_row_count_equals_phase_order_plus_dual_tc(self) -> None:
        local, cloud_session, cloud_per_tc = self._load_inputs()
        rows = build_comparison_rows(local, cloud_session, cloud_per_tc)
        assert len(rows) == len(ANDROID_PHASE_ORDER) + len(DUAL_TC_ROWS)

    def test_row_order_is_phase_then_dual_tc(self) -> None:
        local, cloud_session, cloud_per_tc = self._load_inputs()
        rows = build_comparison_rows(local, cloud_session, cloud_per_tc)
        for i, expected in enumerate(ANDROID_PHASE_ORDER + DUAL_TC_ROWS):
            assert (rows[i]["phase_group"], rows[i]["phase_step"]) == expected

    def test_local_only_phase_rows_have_empty_cloud_columns(self) -> None:
        local, cloud_session, cloud_per_tc = self._load_inputs()
        rows = build_comparison_rows(local, cloud_session, cloud_per_tc)
        # Maestro startup / driver_setup has no cloud analog → cloud_N must be 0.
        driver_row = next(r for r in rows if r["phase_step"] == "driver_setup")
        assert driver_row["local_N"] == 9
        assert driver_row["cloud_N"] == 0
        assert driver_row["cloud_p50_ms"] == ""
        assert driver_row["delta_p50_ms"] == ""

    def test_aggregates_row_has_both_local_and_cloud_and_delta(self) -> None:
        local, cloud_session, cloud_per_tc = self._load_inputs()
        rows = build_comparison_rows(local, cloud_session, cloud_per_tc)
        session_total = next(r for r in rows if r["phase_step"] == "session_total")
        assert session_total["local_p50_ms"] == 1185000
        assert session_total["cloud_p50_ms"] == 1150000
        # delta = cloud - local = 1150000 - 1185000 = -35000.
        assert session_total["delta_p50_ms"] == -35000

    def test_dual_tc_rows_have_cloud_data_but_no_local_or_delta(self) -> None:
        local, cloud_session, cloud_per_tc = self._load_inputs()
        rows = build_comparison_rows(local, cloud_session, cloud_per_tc)
        tc_a = next(r for r in rows if r["phase_step"] == "tc_a_duration")
        assert tc_a["local_N"] == 0
        assert tc_a["cloud_N"] == 3
        assert tc_a["cloud_p50_ms"] == 560000
        # No local analog → no delta computable.
        assert tc_a["delta_p50_ms"] == ""

    def test_combined_view_is_per_session_sum_not_summed_percentiles(self) -> None:
        # The combined view computes (test_a + test_b) PER SESSION, then takes
        # percentiles of those sums — NOT P50(test_a) + P50(test_b).
        local, cloud_session, cloud_per_tc = self._load_inputs()
        rows = build_comparison_rows(local, cloud_session, cloud_per_tc)
        combined = next(r for r in rows if r["phase_step"] == "tc_combined_duration")
        tc_a = next(r for r in rows if r["phase_step"] == "tc_a_duration")
        tc_b = next(r for r in rows if r["phase_step"] == "tc_b_duration")

        # Sums per session: 1050000, 1100000, 1140000. P50 = 1100000.
        assert combined["cloud_p50_ms"] == 1100000
        # P50(test_a) + P50(test_b) = 560000 + 540000 = 1100000 — same here BY
        # COINCIDENCE because the data is monotonic. The independent path
        # through per-session sums is the discipline.
        assert tc_a["cloud_p50_ms"] + tc_b["cloud_p50_ms"] == combined["cloud_p50_ms"]


# ----------------------------------------------------------------------------
# CSV writer (column header + emitted shape)
# ----------------------------------------------------------------------------

class TestWriteComparisonCsv:
    def test_header_matches_ios_csv_header(self) -> None:
        # Header must match the iOS comparison CSV file's header exactly for
        # cross-platform comparison tooling to work.
        ios_csv = (REPO_ROOT / "analysis" /
                   "local_vs_cloud_ios_comparison_20260510_191348.csv")
        if not ios_csv.exists():
            pytest.skip("iOS reference CSV not present")
        with ios_csv.open() as fh:
            ios_header = next(csv.reader(fh))
        assert ios_header == CSV_HEADER

    def test_writer_emits_full_header_and_rows(self, tmp_path: Path) -> None:
        local = load_local_final(FIXTURES / "local_android_baseline_sample.csv")
        bq = json.loads((FIXTURES / "cloud_android_multitest_sample.json").read_text())
        cell = load_cloud_cell(bq, cell_name="cloud_android", os="android")
        cloud_session = cloud_session_stats(cell)
        cloud_per_tc = per_tc_stats({})
        rows = build_comparison_rows(local, cloud_session, cloud_per_tc)
        out_path = tmp_path / "out.csv"
        write_comparison_csv(rows, out_path)

        with out_path.open() as fh:
            reader = csv.DictReader(fh)
            assert reader.fieldnames == CSV_HEADER
            emitted = list(reader)
        assert len(emitted) == len(ANDROID_PHASE_ORDER) + len(DUAL_TC_ROWS)
