"""Tests for pipeline.cells local CSV loaders (U2). Cloud BQ loader covered in U3."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.cells import (
    Cell,
    CellSession,
    EmptyCellError,
    load_local_android,
    load_local_ios,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _write_csv(tmp_path: Path, header: str, rows: list[str]) -> Path:
    csv_path = tmp_path / "sessions.csv"
    csv_path.write_text(header + "\n" + "\n".join(rows) + ("\n" if rows else ""))
    return tmp_path


HEADER = (
    "run_id,iter,tag,framework,os,device_model,device_os,flow,apk,"
    "device_readiness_ms,app_install_ms,maestro_total_ms,maestro_start_ms,"
    "execution_ms,stop_ms,session_total_ms,exit_code"
)


class TestLoadLocalAndroid:
    def test_happy_path_loads_passing_sessions_only(self, tmp_path: Path) -> None:
        # Use the project fixture: 10 successful + 1 failed row
        results_dir = FIXTURES.parent / "fixtures"  # uses local_android_sample.csv
        # Copy to tmp so we control directory layout
        sample_text = (FIXTURES / "local_android_sample.csv").read_text()
        (tmp_path / "sessions.csv").write_text(sample_text)

        cell = load_local_android(tmp_path)
        assert cell.name == "local_android"
        assert cell.framework == "maestro"
        assert cell.os == "android"
        assert cell.capability_profile == "defaults"  # no meta.txt
        assert len(cell.sessions) == 10  # the failed row is filtered out
        assert all(isinstance(s, CellSession) for s in cell.sessions)
        assert all(s.region is None for s in cell.sessions)
        assert all(s.app_dl_ms == 0 for s in cell.sessions)  # local
        assert all(s.waiting_ms is None for s in cell.sessions)
        # spot-check first row's normalization
        first = cell.sessions[0]
        assert first.app_install_ms == 13772
        assert first.start_ms == 6179
        assert first.execution_s == 1157.465
        assert first.stop_ms == 779

    def test_capability_profile_from_meta_txt(self, tmp_path: Path) -> None:
        sample = (FIXTURES / "local_android_sample.csv").read_text()
        (tmp_path / "sessions.csv").write_text(sample)
        (tmp_path / "meta.txt").write_text("capability_profile=local_on\n")

        cell = load_local_android(tmp_path)
        assert cell.capability_profile == "local_on"

    def test_capability_profile_parameter_overrides_meta(self, tmp_path: Path) -> None:
        sample = (FIXTURES / "local_android_sample.csv").read_text()
        (tmp_path / "sessions.csv").write_text(sample)
        (tmp_path / "meta.txt").write_text("capability_profile=local_on\n")

        cell = load_local_android(tmp_path, capability_profile="network_logs_on")
        assert cell.capability_profile == "network_logs_on"

    def test_default_capability_when_no_meta(self, tmp_path: Path) -> None:
        sample = (FIXTURES / "local_android_sample.csv").read_text()
        (tmp_path / "sessions.csv").write_text(sample)

        cell = load_local_android(tmp_path)
        assert cell.capability_profile == "defaults"

    def test_missing_csv_raises(self, tmp_path: Path) -> None:
        with pytest.raises(EmptyCellError, match="sessions.csv missing"):
            load_local_android(tmp_path)

    def test_empty_csv_raises(self, tmp_path: Path) -> None:
        _write_csv(tmp_path, HEADER, rows=[])
        with pytest.raises(EmptyCellError, match="no rows"):
            load_local_android(tmp_path)

    def test_all_failed_sessions_raises(self, tmp_path: Path) -> None:
        rows = [
            "rid,1,t,maestro,android,m,14,f.yaml,a.apk,100,1000,2000,500,1500,200,3000,1",
            "rid,2,t,maestro,android,m,14,f.yaml,a.apk,100,1000,2000,500,1500,200,3000,1",
        ]
        _write_csv(tmp_path, HEADER, rows)
        with pytest.raises(EmptyCellError, match="no successful 'android'"):
            load_local_android(tmp_path)

    def test_skips_rows_with_wrong_os(self, tmp_path: Path) -> None:
        rows = [
            "rid,1,t,maestro,ios,m,17,f.yaml,a.apk,100,1000,2000,500,1500,200,3000,0",
            "rid,2,t,maestro,android,m,14,f.yaml,a.apk,100,1500,2000,500,1500,200,3000,0",
        ]
        _write_csv(tmp_path, HEADER, rows)
        cell = load_local_android(tmp_path)
        assert len(cell.sessions) == 1
        assert cell.sessions[0].app_install_ms == 1500


class TestLoadLocalIos:
    def test_loads_ios_rows(self, tmp_path: Path) -> None:
        rows = [
            "r,1,t,maestro,ios,iPhone17,26,f.yaml,a.ipa,100,1500,2000,500,1500,200,3000,0",
            "r,2,t,maestro,ios,iPhone17,26,f.yaml,a.ipa,100,1700,2000,500,1500,200,3000,0",
        ]
        _write_csv(tmp_path, HEADER, rows)
        cell = load_local_ios(tmp_path)
        assert cell.name == "local_ios"
        assert cell.os == "ios"
        assert len(cell.sessions) == 2

    def test_filters_out_android_rows(self, tmp_path: Path) -> None:
        rows = [
            "r,1,t,maestro,android,m,14,f.yaml,a.apk,100,1000,2000,500,1500,200,3000,0",
            "r,2,t,maestro,ios,iPhone17,26,f.yaml,a.ipa,100,1500,2000,500,1500,200,3000,0",
        ]
        _write_csv(tmp_path, HEADER, rows)
        cell = load_local_ios(tmp_path)
        assert len(cell.sessions) == 1
        assert cell.sessions[0].app_install_ms == 1500
