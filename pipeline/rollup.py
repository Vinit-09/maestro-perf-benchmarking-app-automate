"""P50/P90 rollup math for the Maestro benchmark.

Aggregates ``Cell``s into ``RollupRow``s — one row per
``(cell, region, capabilities_profile)`` cut. Region cuts are derived from each
session's ``region`` field; for local cells (region=None) one row per cell is
emitted. Capability profile is per-cell.

Convention: nearest-rank percentile (P_p = value at index ceil(p*n) − 1, 0-indexed).
Cuts with ``n < min_sample`` are emitted with NULL P50/P90 and ``low_sample=True``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from pipeline.cells import Cell, CellSession


DEFAULT_MIN_SAMPLE = 5


@dataclass(frozen=True)
class RollupRow:
    """One aggregated row, schema-aligned with the BQ rollup table."""

    # identity
    run_set_id: str
    cell: str
    framework: str
    os: str
    region: str | None
    capabilities_profile: str
    source_build_ids: tuple[str, ...]
    source_run_ids: tuple[str, ...]

    # counts
    n_sessions: int
    low_sample: bool

    # waiting (total + per reason bucket)
    waiting_p50_ms: int | None
    waiting_p90_ms: int | None
    waiting_reason_no_parallel_p50_ms: int | None
    waiting_reason_no_parallel_p90_ms: int | None
    waiting_reason_device_tier_p50_ms: int | None
    waiting_reason_device_tier_p90_ms: int | None
    waiting_reason_async_signing_p50_ms: int | None
    waiting_reason_async_signing_p90_ms: int | None
    waiting_reason_region_pool_p50_ms: int | None
    waiting_reason_region_pool_p90_ms: int | None

    # start time (firecmd analog) and total execution
    start_p50_ms: int | None
    start_p90_ms: int | None
    execution_p50_s: float | None
    execution_p90_s: float | None

    # supporting P1
    app_download_p50_ms: int | None
    app_download_p90_ms: int | None
    app_install_p50_ms: int | None
    app_install_p90_ms: int | None
    stop_p50_ms: int | None
    stop_p90_ms: int | None

    # bookkeeping
    aggregated_at: datetime


def percentile_nearest_rank(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile.

    For sorted values v_1..v_n, returns v_{⌈p·n⌉}. Returns None on an empty
    or all-None input. Boundary behavior: p≤0 returns the min, p≥1 the max.
    """
    cleaned = [float(v) for v in values if v is not None]
    if not cleaned:
        return None
    s = sorted(cleaned)
    if p <= 0:
        return s[0]
    if p >= 1:
        return s[-1]
    rank = math.ceil(p * len(s))
    return s[rank - 1]


def _maybe_int(v: float | None) -> int | None:
    return None if v is None else int(round(v))


def _split_source_ids(cell: Cell) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Pick build_ids vs run_ids based on cell type.

    Local cells store full paths in source_paths; we keep just the directory
    basename (the run-id timestamp) for traceability. Cloud cells store
    build_ids directly.
    """
    if cell.name.startswith("cloud_"):
        return tuple(cell.source_paths), ()
    return (), tuple(Path(p).name for p in cell.source_paths)


def _build_row(
    cell: Cell,
    region: str | None,
    sessions: list[CellSession],
    *,
    run_set_id: str,
    aggregated_at: datetime,
    min_sample: int,
) -> RollupRow:
    n = len(sessions)
    low_sample = n < min_sample

    def stat(attr: str, p: float) -> float | None:
        if low_sample:
            return None
        return percentile_nearest_rank([getattr(s, attr) for s in sessions], p)

    source_build_ids, source_run_ids = _split_source_ids(cell)

    return RollupRow(
        run_set_id=run_set_id,
        cell=cell.name,
        framework=cell.framework,
        os=cell.os,
        region=region,
        capabilities_profile=cell.capability_profile,
        source_build_ids=source_build_ids,
        source_run_ids=source_run_ids,
        n_sessions=n,
        low_sample=low_sample,
        waiting_p50_ms=_maybe_int(stat("waiting_ms", 0.50)),
        waiting_p90_ms=_maybe_int(stat("waiting_ms", 0.90)),
        waiting_reason_no_parallel_p50_ms=_maybe_int(stat("waiting_reason_no_parallel_ms", 0.50)),
        waiting_reason_no_parallel_p90_ms=_maybe_int(stat("waiting_reason_no_parallel_ms", 0.90)),
        waiting_reason_device_tier_p50_ms=_maybe_int(stat("waiting_reason_device_tier_ms", 0.50)),
        waiting_reason_device_tier_p90_ms=_maybe_int(stat("waiting_reason_device_tier_ms", 0.90)),
        waiting_reason_async_signing_p50_ms=_maybe_int(stat("waiting_reason_async_signing_ms", 0.50)),
        waiting_reason_async_signing_p90_ms=_maybe_int(stat("waiting_reason_async_signing_ms", 0.90)),
        waiting_reason_region_pool_p50_ms=_maybe_int(stat("waiting_reason_region_pool_ms", 0.50)),
        waiting_reason_region_pool_p90_ms=_maybe_int(stat("waiting_reason_region_pool_ms", 0.90)),
        start_p50_ms=_maybe_int(stat("start_ms", 0.50)),
        start_p90_ms=_maybe_int(stat("start_ms", 0.90)),
        execution_p50_s=stat("execution_s", 0.50),
        execution_p90_s=stat("execution_s", 0.90),
        app_download_p50_ms=_maybe_int(stat("app_dl_ms", 0.50)),
        app_download_p90_ms=_maybe_int(stat("app_dl_ms", 0.90)),
        app_install_p50_ms=_maybe_int(stat("app_install_ms", 0.50)),
        app_install_p90_ms=_maybe_int(stat("app_install_ms", 0.90)),
        stop_p50_ms=_maybe_int(stat("stop_ms", 0.50)),
        stop_p90_ms=_maybe_int(stat("stop_ms", 0.90)),
        aggregated_at=aggregated_at,
    )


def rollup(
    cells: Iterable[Cell],
    *,
    run_set_id: str,
    aggregated_at: datetime | None = None,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> list[RollupRow]:
    """Compute per-(cell × region × capability) rollup rows.

    ``capabilities_profile`` is a property of the cell, so it's not a separate
    grouping axis here — every row from one cell shares its capability label.
    Cuts with no sessions are not emitted; cuts with ``n < min_sample`` are
    emitted with NULL P50/P90 and ``low_sample=True``.
    """
    if aggregated_at is None:
        aggregated_at = datetime.now(timezone.utc)

    rows: list[RollupRow] = []
    for cell in cells:
        if not cell.sessions:
            continue
        # Group sessions by region (preserving local's None-region path).
        by_region: dict[str | None, list[CellSession]] = defaultdict(list)
        for s in cell.sessions:
            by_region[s.region].append(s)

        for region in sorted(by_region.keys(), key=lambda r: (r is None, r or "")):
            sessions = by_region[region]
            if not sessions:
                continue
            rows.append(
                _build_row(
                    cell,
                    region,
                    sessions,
                    run_set_id=run_set_id,
                    aggregated_at=aggregated_at,
                    min_sample=min_sample,
                )
            )
    return rows
