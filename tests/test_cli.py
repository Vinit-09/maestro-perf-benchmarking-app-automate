"""End-to-end + unit tests for pipeline.cli (U8)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pipeline.cli import build_action, main

FIXTURES = Path(__file__).parent / "fixtures"
HEADER = (
    "run_id,iter,tag,framework,os,device_model,device_os,flow,apk,"
    "device_readiness_ms,app_install_ms,maestro_total_ms,maestro_start_ms,"
    "execution_ms,stop_ms,session_total_ms,exit_code"
)


def _make_local_results(tmp_path: Path, *, name: str, os_name: str, n: int = 30) -> Path:
    """Materialize a local results dir with N successful sessions."""
    d = tmp_path / name
    d.mkdir()
    rows = [HEADER]
    for i in range(1, n + 1):
        rows.append(
            f"r{name},{i},baseline,maestro,{os_name},m,14,f.yaml,a.{ 'apk' if os_name=='android' else 'ipa'},"
            f"100,1500,2000,500,1500,200,3000,0"
        )
    (d / "sessions.csv").write_text("\n".join(rows) + "\n")
    return d


def _make_cloud_bq_response(tmp_path: Path, name: str, n: int = 30) -> Path:
    """Materialize a BQ response JSON file with N rows in a single region."""
    rows = []
    for i in range(n):
        rows.append({"f": [
            {"v": f"sess{i}"}, {"v": "build1"}, {"v": "ap-south-1"},
            {"v": "720.0"}, {"v": "12000"}, {"v": "65"}, {"v": "1600"},
            {"v": "0"}, {"v": "2400"}, {"v": None}, {"v": "0"}, {"v": "0"},
            {"v": "0"}, {"v": "0"},
        ]})
    payload = {
        "schema": {
            "fields": [
                {"name": "hashed_id", "type": "STRING"},
                {"name": "build_id", "type": "STRING"},
                {"name": "device_region", "type": "STRING"},
                {"name": "execution_s", "type": "FLOAT"},
                {"name": "firecmd_ms", "type": "INT64"},
                {"name": "app_dl_ms", "type": "INT64"},
                {"name": "app_install_ms", "type": "INT64"},
                {"name": "test_dl_ms", "type": "INT64"},
                {"name": "test_install_ms", "type": "INT64"},
                {"name": "stop_ms", "type": "INT64"},
                {"name": "waiting_no_parallel_ms", "type": "INT64"},
                {"name": "waiting_device_tier_ms", "type": "INT64"},
                {"name": "waiting_async_signing_ms", "type": "INT64"},
                {"name": "waiting_region_pool_ms", "type": "INT64"},
            ]
        },
        "rows": rows,
    }
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(payload))
    return p


def _full_run_set(tmp_path: Path) -> dict:
    la = _make_local_results(tmp_path, name="la", os_name="android")
    li = _make_local_results(tmp_path, name="li", os_name="ios")
    ca = _make_cloud_bq_response(tmp_path, name="ca")
    ci = _make_cloud_bq_response(tmp_path, name="ci")
    return {
        "run_set_id": "rs-test",
        "capability_profile": "defaults",
        "bq_table_fqn": "p.d.maestro_benchmark_metrics_aggregated",
        "email_recipients": ["test@example.com"],
        "cells": {
            "local_android": {"results_dir": str(la)},
            "cloud_android": {"bq_response_path": str(ca)},
            "local_ios": {"results_dir": str(li)},
            "cloud_ios": {"bq_response_path": str(ci)},
        },
    }


# ---------------------------------------------------------------------------
# build_action — pure-function tests
# ---------------------------------------------------------------------------


class TestBuildAction:
    def test_full_4_cells_returns_action_with_sql_and_email(self, tmp_path: Path) -> None:
        run_set = _full_run_set(tmp_path)
        action, code = build_action(run_set, base=tmp_path)
        assert code == 0
        assert action["status"] == "complete"
        assert "bq_insert_sql" in action
        assert "INSERT INTO" in action["bq_insert_sql"]
        assert "email" in action
        assert action["email"]["subject"].startswith("Maestro Benchmark / rs-test / defaults")
        assert action["email"]["recipients"] == ["test@example.com"]
        assert "<table" in action["email"]["body_html"]
        assert action["rollup_summary"]["n_rows"] >= 4  # one row per cell at minimum

    def test_missing_cell_blocks_with_error_payload(self, tmp_path: Path) -> None:
        run_set = _full_run_set(tmp_path)
        run_set["cells"]["local_ios"] = {}  # empty spec → missing
        action, code = build_action(run_set, base=tmp_path)
        assert code == 2
        assert action["status"] == "incomplete"
        assert action["error"]["missing_cells"] == ["local_ios"]
        assert "bq_insert_sql" not in action
        assert "email" not in action

    def test_partial_cell_does_not_block(self, tmp_path: Path) -> None:
        run_set = _full_run_set(tmp_path)
        # Make local_ios have only n=5 sessions (below partial threshold 30 but above 0)
        small = _make_local_results(tmp_path, name="li_small", os_name="ios", n=5)
        run_set["cells"]["local_ios"] = {"results_dir": str(small)}
        action, code = build_action(run_set, base=tmp_path)
        assert code == 0
        assert action["status"] == "partial"
        assert action["partial_cells"] == ["local_ios"]
        assert "bq_insert_sql" in action

    def test_invalid_input_missing_required_keys(self, tmp_path: Path) -> None:
        action, code = build_action({"run_set_id": "x"}, base=tmp_path)
        assert code == 2
        assert action["status"] == "invalid_input"
        assert "capability_profile" in action["error"]["missing_keys"]
        assert "bq_table_fqn" in action["error"]["missing_keys"]

    def test_load_failure_treated_as_missing_not_crash(self, tmp_path: Path) -> None:
        run_set = _full_run_set(tmp_path)
        # Point cloud_ios at a non-existent path — should mark missing, not raise
        run_set["cells"]["cloud_ios"] = {"bq_response_path": "/no/such/file.json"}
        action, code = build_action(run_set, base=tmp_path)
        assert code == 2
        assert "cloud_ios" in action["error"]["missing_cells"]

    def test_relative_paths_resolve_against_base(self, tmp_path: Path) -> None:
        run_set = _full_run_set(tmp_path)
        # Use the directory basename only — should resolve relative to base (tmp_path)
        run_set["cells"]["local_android"]["results_dir"] = "la"
        action, code = build_action(run_set, base=tmp_path)
        assert code == 0  # still works because relative path resolves to tmp_path/la

    def test_cells_loaded_summary_lists_session_counts(self, tmp_path: Path) -> None:
        run_set = _full_run_set(tmp_path)
        action, _ = build_action(run_set, base=tmp_path)
        loaded = action["cells_loaded"]
        assert loaded["local_android"] == 30
        assert loaded["cloud_android"] == 30
        assert loaded["local_ios"] == 30
        assert loaded["cloud_ios"] == 30


# ---------------------------------------------------------------------------
# main() — end-to-end via the argparse harness
# ---------------------------------------------------------------------------


class TestMain:
    def test_full_run_writes_action_json_and_exits_zero(self, tmp_path: Path) -> None:
        run_set = _full_run_set(tmp_path)
        run_set_path = tmp_path / "run_set.json"
        run_set_path.write_text(json.dumps(run_set))
        out_path = tmp_path / "action.json"

        code = main(["--run-set", str(run_set_path), "--out", str(out_path)])
        assert code == 0
        action = json.loads(out_path.read_text())
        assert action["status"] == "complete"
        assert "INSERT INTO" in action["bq_insert_sql"]

    def test_gate_failure_exits_2_and_writes_error_payload(self, tmp_path: Path) -> None:
        run_set = _full_run_set(tmp_path)
        run_set["cells"]["local_ios"] = {}  # missing
        run_set_path = tmp_path / "run_set.json"
        run_set_path.write_text(json.dumps(run_set))
        out_path = tmp_path / "action.json"

        code = main(["--run-set", str(run_set_path), "--out", str(out_path)])
        assert code == 2
        action = json.loads(out_path.read_text())
        assert action["status"] == "incomplete"
        assert "local_ios" in action["error"]["missing_cells"]

    def test_dry_run_prints_to_stdout(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        run_set = _full_run_set(tmp_path)
        run_set_path = tmp_path / "run_set.json"
        run_set_path.write_text(json.dumps(run_set))

        code = main(["--run-set", str(run_set_path), "--dry-run"])
        assert code == 0
        captured = capsys.readouterr()
        action = json.loads(captured.out)
        assert action["status"] == "complete"
        assert "bq_insert_sql" in action

    def test_missing_run_set_file_exits_2(self, tmp_path: Path) -> None:
        code = main(["--run-set", str(tmp_path / "missing.json")])
        assert code == 2

    def test_malformed_run_set_json_exits_2(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        code = main(["--run-set", str(bad)])
        assert code == 2
