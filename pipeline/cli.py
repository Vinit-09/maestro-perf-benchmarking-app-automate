"""CLI entrypoint that ties cells → gate → rollup → BQ INSERT → email payload.

Reads a run-set descriptor JSON, loads each cell (or marks it missing), runs
the four-cell gate, and emits an ``action.json`` for the orchestrator (Claude
session) to consume:

- On gate failure:  ``status="incomplete"`` + ``error`` payload, exit code 2.
  No SQL, no email payload.
- On gate success:  ``status="complete"`` (or ``"partial"`` when partial cells
  exist) + ``bq_insert_sql`` + ``email`` payload (subject, body_html,
  recipients), exit code 0.

The orchestrator reads ``action.json``, fires the BQ INSERT via the BigQuery
MCP and the email via the Gmail MCP. Pure-logic boundary stays here; auth
and I/O live in the orchestrator.

Run-set descriptor shape::

    {
      "run_set_id": "2026-05-04-baseline",
      "capability_profile": "defaults",
      "bq_table_fqn": "project.dataset.maestro_benchmark_metrics_aggregated",
      "email_recipients": ["someone@example.com"],
      "cells": {
        "local_android": { "results_dir": "results/20260430_175317" },
        "cloud_android": { "bq_response_path": "results/cloud_X/bq.json", "build_id": "..." },
        "local_ios":     { "results_dir": "results/20260505_local_ios" },
        "cloud_ios":     { "bq_response_path": "results/cloud_Y/bq.json", "build_id": "..." }
      }
    }

Cells with absent or empty entries are treated as missing — the gate fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pipeline import bq_writer, email_renderer, gate, rollup
from pipeline.cells import (
    Cell,
    EmptyCellError,
    MalformedCellError,
    load_cloud_cell,
    load_local_android,
    load_local_ios,
)
from pipeline.gate import EXPECTED_CELLS


class CellLoadError(Exception):
    """Wraps a load failure for one cell so the CLI can mark it missing."""


def _resolve_path(base: Path, raw: str) -> Path:
    p = Path(raw)
    if p.is_absolute():
        return p
    return (base / p).resolve()


def _load_one_cell(
    cell_name: str,
    spec: dict[str, Any] | None,
    *,
    capability_profile: str,
    base: Path,
) -> Cell | None:
    """Return the loaded Cell, or None when the spec is empty or load fails.

    Failures are swallowed and turned into None so the gate can list all
    missing cells in one pass; the orchestrator surfaces them in the error
    payload.
    """
    if not spec:
        return None

    try:
        if cell_name == "local_android":
            results = spec.get("results_dir")
            if not results:
                return None
            return load_local_android(_resolve_path(base, results), capability_profile=capability_profile)

        if cell_name == "local_ios":
            results = spec.get("results_dir")
            if not results:
                return None
            return load_local_ios(_resolve_path(base, results), capability_profile=capability_profile)

        if cell_name in ("cloud_android", "cloud_ios"):
            path = spec.get("bq_response_path")
            if not path:
                return None
            data = json.loads(_resolve_path(base, path).read_text())
            os_name = "android" if "android" in cell_name else "ios"
            return load_cloud_cell(
                data,
                cell_name=cell_name,
                os=os_name,
                capability_profile=capability_profile,
            )
    except (FileNotFoundError, json.JSONDecodeError, EmptyCellError, MalformedCellError):
        return None

    return None


def _load_cells(run_set: dict[str, Any], base: Path) -> dict[str, Cell | None]:
    capability = run_set.get("capability_profile", "defaults")
    cell_specs = run_set.get("cells", {}) or {}
    out: dict[str, Cell | None] = {}
    for name in EXPECTED_CELLS:
        out[name] = _load_one_cell(name, cell_specs.get(name), capability_profile=capability, base=base)
    return out


def build_action(run_set: dict[str, Any], base: Path) -> tuple[dict[str, Any], int]:
    """Pure function: take a parsed run-set + base dir, return (action_dict, exit_code).

    Exposed for testing. The CLI thin-wraps this with file I/O.
    """
    required = ("run_set_id", "capability_profile", "bq_table_fqn")
    missing = [k for k in required if not run_set.get(k)]
    if missing:
        return (
            {
                "status": "invalid_input",
                "error": {"missing_keys": missing, "message": f"run-set missing keys: {missing}"},
            },
            2,
        )

    cells = _load_cells(run_set, base)
    gate_result = gate.check(cells)

    base_action: dict[str, Any] = {
        "run_set_id": run_set["run_set_id"],
        "capability_profile": run_set["capability_profile"],
        "cells_loaded": {
            name: (None if c is None else len(c.sessions)) for name, c in cells.items()
        },
    }

    if not gate_result.complete:
        base_action["status"] = "incomplete"
        base_action["error"] = {
            "missing_cells": list(gate_result.missing),
            "partial_cells": list(gate_result.partial),
            "message": gate.format_missing(gate_result),
        }
        return base_action, 2

    loaded_cells = [c for c in cells.values() if c is not None]
    rollup_rows = rollup.rollup(
        loaded_cells,
        run_set_id=run_set["run_set_id"],
    )

    sql = bq_writer.build_insert_sql(
        table_fqn=run_set["bq_table_fqn"],
        rollup_rows=rollup_rows,
    )
    subject = email_renderer.build_subject(
        run_set_id=run_set["run_set_id"],
        capability_profile=run_set["capability_profile"],
        gate=gate_result,
    )
    body_html = email_renderer.build_html(
        rollup_rows=rollup_rows,
        gate=gate_result,
        run_set_id=run_set["run_set_id"],
        capability_profile=run_set["capability_profile"],
    )

    base_action["status"] = "partial" if gate_result.partial else "complete"
    base_action["partial_cells"] = list(gate_result.partial)
    base_action["bq_insert_sql"] = sql
    base_action["email"] = {
        "subject": subject,
        "body_html": body_html,
        "recipients": list(run_set.get("email_recipients") or []),
    }
    base_action["rollup_summary"] = {
        "n_rows": len(rollup_rows),
        "rows_per_cell": {
            cell_name: sum(1 for r in rollup_rows if r.cell == cell_name)
            for cell_name in EXPECTED_CELLS
        },
    }
    return base_action, 0


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pipeline.cli",
        description="Aggregate Maestro benchmark cells, gate completeness, "
        "emit BQ INSERT + email payload as action.json.",
    )
    p.add_argument(
        "--run-set",
        required=True,
        type=Path,
        help="Path to run-set descriptor JSON.",
    )
    p.add_argument(
        "--out",
        type=Path,
        help="Where to write action.json. If omitted with --dry-run, prints to stdout.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print action.json to stdout instead of writing to --out.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    run_set_path: Path = args.run_set
    if not run_set_path.exists():
        print(f"run-set file not found: {run_set_path}", file=sys.stderr)
        return 2

    try:
        run_set = json.loads(run_set_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"run-set JSON parse error: {exc}", file=sys.stderr)
        return 2

    base = run_set_path.parent.resolve()
    action, exit_code = build_action(run_set, base)
    serialized = json.dumps(action, indent=2)

    if args.dry_run or not args.out:
        print(serialized)
    else:
        args.out.write_text(serialized + "\n")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
