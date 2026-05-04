"""Cell loaders — read raw session rows into a canonical in-memory schema.

A "cell" is one quadrant of the benchmark matrix: local_android, cloud_android,
local_ios, cloud_ios. Different sources have different raw shapes (local CSVs
from run_benchmark.sh vs. cloud BQ row JSON), but all sessions normalize into
the same ``CellSession`` for downstream rollup math.

This module owns local CSV loading; cloud BQ loading lives alongside in U3.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CellSession:
    """One canonical session row, normalized across local + cloud sources.

    All durations are integers in milliseconds, except ``execution_s`` which
    is a float in seconds (matching the BQ table schema).
    """
    waiting_ms: int | None
    waiting_reason_no_parallel_ms: int | None
    waiting_reason_device_tier_ms: int | None
    waiting_reason_async_signing_ms: int | None
    waiting_reason_region_pool_ms: int | None
    start_ms: int | None
    execution_s: float | None
    app_dl_ms: int | None
    app_install_ms: int | None
    test_dl_ms: int | None
    test_install_ms: int | None
    stop_ms: int | None
    region: str | None
    source_id: str


@dataclass
class Cell:
    """A collection of sessions for one cell of the matrix."""
    name: str  # local_android | cloud_android | local_ios | cloud_ios
    framework: str  # maestro
    os: str  # android | ios
    capability_profile: str  # defaults | local_on | network_logs_on | ...
    sessions: list[CellSession]
    source_paths: list[str] = field(default_factory=list)


class EmptyCellError(Exception):
    """Raised when a cell points at a path that has no usable session rows."""


def _to_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        v = int(value)
    except ValueError:
        return None
    return None if v < 0 else v


def _read_meta_capability(results_path: Path) -> str | None:
    """Read capability_profile from results_path/meta.txt if present."""
    meta_path = results_path / "meta.txt"
    if not meta_path.exists():
        return None
    for line in meta_path.read_text().splitlines():
        if line.startswith("capability_profile="):
            value = line.split("=", 1)[1].strip()
            if value:
                return value
    return None


def _load_local_csv(
    results_dir: str | Path,
    *,
    expected_os: str,
    cell_name: str,
    capability_profile: str | None,
) -> Cell:
    """Shared loader for local_android and local_ios cells.

    Reads sessions.csv at the directory root (the schema written by
    run_benchmark.sh) and normalizes to ``CellSession``. Skips rows whose
    exit_code is non-zero — failures are not benchmark data.
    """
    results_path = Path(results_dir)
    csv_path = results_path / "sessions.csv"
    if not csv_path.exists():
        raise EmptyCellError(f"sessions.csv missing under {results_dir!r}")

    rows = list(csv.DictReader(csv_path.open()))
    if not rows:
        raise EmptyCellError(f"sessions.csv has no rows under {results_dir!r}")

    capability = capability_profile or _read_meta_capability(results_path) or "defaults"

    sessions: list[CellSession] = []
    for r in rows:
        if (r.get("os") or "").strip().lower() != expected_os:
            continue
        try:
            exit_code = int(r.get("exit_code", "1") or "1")
        except ValueError:
            exit_code = 1
        if exit_code != 0:
            continue

        execution_ms = _to_int(r.get("execution_ms"))
        execution_s = execution_ms / 1000.0 if execution_ms is not None else None

        sessions.append(
            CellSession(
                waiting_ms=None,  # local has no queue
                waiting_reason_no_parallel_ms=None,
                waiting_reason_device_tier_ms=None,
                waiting_reason_async_signing_ms=None,
                waiting_reason_region_pool_ms=None,
                start_ms=_to_int(r.get("maestro_start_ms")),
                execution_s=execution_s,
                app_dl_ms=0,  # already on disk locally
                app_install_ms=_to_int(r.get("app_install_ms")),
                test_dl_ms=0,
                test_install_ms=None,  # local doesn't track separately
                stop_ms=_to_int(r.get("stop_ms")),
                region=None,
                source_id=f"{r.get('run_id', '')}:{r.get('iter', '')}",
            )
        )

    if not sessions:
        raise EmptyCellError(
            f"no successful {expected_os!r} sessions found in {results_dir!r}"
        )

    return Cell(
        name=cell_name,
        framework="maestro",
        os=expected_os,
        capability_profile=capability,
        sessions=sessions,
        source_paths=[str(results_path.resolve())],
    )


def load_local_android(
    results_dir: str | Path, *, capability_profile: str | None = None
) -> Cell:
    """Load a local Android cell from a run_benchmark.sh results directory."""
    return _load_local_csv(
        results_dir,
        expected_os="android",
        cell_name="local_android",
        capability_profile=capability_profile,
    )


def load_local_ios(
    results_dir: str | Path, *, capability_profile: str | None = None
) -> Cell:
    """Load a local iOS cell. Expects the same CSV shape as load_local_android."""
    return _load_local_csv(
        results_dir,
        expected_os="ios",
        cell_name="local_ios",
        capability_profile=capability_profile,
    )
