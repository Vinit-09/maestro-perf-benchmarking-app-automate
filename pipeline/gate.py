"""Four-cell completeness gate for the Maestro benchmark matrix.

The pipeline only emits an email after data exists for all four cells of
``(local|cloud) x (android|ios)``. Missing cells are a hard block. Cells
that exist but have fewer than ``partial_threshold`` sessions are surfaced
as a soft warning — the email still sends but annotates the cell.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from pipeline.cells import Cell


EXPECTED_CELLS: tuple[str, ...] = (
    "local_android",
    "cloud_android",
    "local_ios",
    "cloud_ios",
)

DEFAULT_PARTIAL_THRESHOLD = 30  # n_sessions below which a cell is "partial" (warning)


@dataclass(frozen=True)
class GateResult:
    """Outcome of the four-cell check.

    ``complete`` is True iff every expected cell is present with at least one
    successful session. ``missing`` lists cells absent or empty. ``partial``
    lists cells present but with n_sessions below ``partial_threshold`` —
    these are warnings the email surfaces but do NOT block sending.
    """
    complete: bool
    missing: tuple[str, ...]
    partial: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.complete


def check(
    cells: Mapping[str, Cell | None],
    *,
    partial_threshold: int = DEFAULT_PARTIAL_THRESHOLD,
) -> GateResult:
    """Evaluate a cells map against the expected four-cell matrix.

    The mapping should use the cell names from :data:`EXPECTED_CELLS` as keys.
    A value of ``None`` (or an absent key) marks a missing cell. A ``Cell``
    with empty ``sessions`` is also treated as missing — empty cells carry no
    data to report on.
    """
    missing: list[str] = []
    partial: list[str] = []

    for name in EXPECTED_CELLS:
        cell = cells.get(name)
        if cell is None or not cell.sessions:
            missing.append(name)
            continue
        if len(cell.sessions) < partial_threshold:
            partial.append(name)

    return GateResult(
        complete=not missing,
        missing=tuple(missing),
        partial=tuple(partial),
    )


def format_missing(result: GateResult) -> str:
    """Operator-friendly explanation when the gate blocks."""
    if result.complete:
        return "complete"
    lines = ["Refusing to send: matrix incomplete.", "", "Missing cells:"]
    for name in result.missing:
        lines.append(f"  - {name}")
    if result.partial:
        lines.append("")
        lines.append("Partial cells (will be flagged in email when run):")
        for name in result.partial:
            lines.append(f"  - {name}")
    return "\n".join(lines)
