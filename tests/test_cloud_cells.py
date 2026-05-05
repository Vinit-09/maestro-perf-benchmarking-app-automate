"""Tests for pipeline.cells cloud BQ row loader (U3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.cells import (
    Cell,
    CellSession,
    EmptyCellError,
    MalformedCellError,
    load_cloud_cell,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture() -> dict:
    return json.loads((FIXTURES / "cloud_ios_sample.json").read_text())


class TestLoadCloudCell:
    def test_happy_path(self) -> None:
        cell = load_cloud_cell(
            _load_fixture(),
            cell_name="cloud_ios",
            os="ios",
            capability_profile="defaults",
        )

        assert cell.name == "cloud_ios"
        assert cell.os == "ios"
        assert cell.framework == "maestro"
        assert cell.capability_profile == "defaults"
        assert len(cell.sessions) == 10
        assert cell.source_paths == ["b001"]  # one build_id, deduped

        # spot-check session normalization
        first = cell.sessions[0]
        assert first.source_id == "s001"
        assert first.region == "ap-south-1"
        assert first.execution_s == 720.0
        assert first.start_ms == 12000
        assert first.app_install_ms == 1600
        assert first.stop_ms is None  # NULL for Maestro — propagate, don't impute

        # waiting_ms = sum of reason buckets (all 0 in fixture → 0)
        assert all(s.waiting_ms == 0 for s in cell.sessions)

    def test_regions_are_carried_per_session(self) -> None:
        cell = load_cloud_cell(_load_fixture(), cell_name="cloud_ios", os="ios")
        regions = {s.region for s in cell.sessions}
        assert regions == {"ap-south-1", "us-east-1", "eu-west-2"}

    def test_stop_ms_is_none_for_all_maestro_rows(self) -> None:
        cell = load_cloud_cell(_load_fixture(), cell_name="cloud_ios", os="ios")
        assert all(s.stop_ms is None for s in cell.sessions)

    def test_outlier_row_preserved(self) -> None:
        # Row s007 has execution_s=2568 (43-min outlier from the real iOS run).
        cell = load_cloud_cell(_load_fixture(), cell_name="cloud_ios", os="ios")
        outliers = [s for s in cell.sessions if s.source_id == "s007"]
        assert len(outliers) == 1
        assert outliers[0].execution_s == 2568.0

    def test_non_dict_input_raises(self) -> None:
        with pytest.raises(MalformedCellError, match="not a dict"):
            load_cloud_cell([], cell_name="cloud_ios", os="ios")

    def test_missing_schema_raises(self) -> None:
        with pytest.raises(MalformedCellError, match="schema.fields is missing"):
            load_cloud_cell({"rows": []}, cell_name="cloud_ios", os="ios")

    def test_row_with_wrong_cell_count_raises(self) -> None:
        bad = {
            "schema": {"fields": [{"name": "a", "type": "STRING"}, {"name": "b", "type": "STRING"}]},
            "rows": [{"f": [{"v": "1"}]}],  # only 1 cell, schema expects 2
        }
        with pytest.raises(MalformedCellError, match="expected 2 cells"):
            load_cloud_cell(bad, cell_name="cloud_ios", os="ios")

    def test_empty_rows_raises_empty_cell(self) -> None:
        empty = {"schema": _load_fixture()["schema"], "rows": []}
        with pytest.raises(EmptyCellError):
            load_cloud_cell(empty, cell_name="cloud_ios", os="ios")

    def test_missing_optional_fields_become_none(self) -> None:
        # Schema with only a subset of canonical fields — others should stay None.
        minimal = {
            "schema": {
                "fields": [
                    {"name": "hashed_id", "type": "STRING"},
                    {"name": "build_id", "type": "STRING"},
                    {"name": "device_region", "type": "STRING"},
                    {"name": "execution_s", "type": "FLOAT"},
                ],
            },
            "rows": [{"f": [{"v": "x"}, {"v": "b1"}, {"v": "us-east-1"}, {"v": "100.0"}]}],
        }
        cell = load_cloud_cell(minimal, cell_name="cloud_ios", os="ios")
        assert len(cell.sessions) == 1
        s = cell.sessions[0]
        assert s.execution_s == 100.0
        assert s.start_ms is None
        assert s.app_install_ms is None
        assert s.waiting_ms is None  # no reason buckets at all → None
