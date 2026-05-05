"""Tests for pipeline.gate four-cell completeness check (U4)."""

from __future__ import annotations

from pipeline.cells import Cell, CellSession
from pipeline.gate import EXPECTED_CELLS, check, format_missing


def _make_session() -> CellSession:
    return CellSession(
        waiting_ms=0,
        waiting_reason_no_parallel_ms=0,
        waiting_reason_device_tier_ms=0,
        waiting_reason_async_signing_ms=0,
        waiting_reason_region_pool_ms=0,
        start_ms=10000,
        execution_s=720.0,
        app_dl_ms=0,
        app_install_ms=1500,
        test_dl_ms=0,
        test_install_ms=2000,
        stop_ms=None,
        region=None,
        source_id="x",
    )


def _make_cell(name: str, n_sessions: int) -> Cell:
    return Cell(
        name=name,
        framework="maestro",
        os="android" if "android" in name else "ios",
        capability_profile="defaults",
        sessions=[_make_session() for _ in range(n_sessions)],
    )


def _full_cells(n: int = 30) -> dict[str, Cell]:
    return {name: _make_cell(name, n) for name in EXPECTED_CELLS}


class TestCheck:
    def test_all_cells_complete_passes(self) -> None:
        result = check(_full_cells(50))
        assert result.complete is True
        assert result.missing == ()
        assert result.partial == ()
        assert bool(result) is True

    def test_partial_cell_does_not_block(self) -> None:
        cells = _full_cells(50)
        cells["local_ios"] = _make_cell("local_ios", 5)  # below threshold
        result = check(cells)
        assert result.complete is True
        assert result.missing == ()
        assert result.partial == ("local_ios",)

    def test_missing_cell_blocks(self) -> None:
        cells = _full_cells(50)
        cells["local_ios"] = None  # explicitly missing
        result = check(cells)
        assert result.complete is False
        assert result.missing == ("local_ios",)
        assert bool(result) is False

    def test_absent_key_treated_as_missing(self) -> None:
        cells = _full_cells(50)
        del cells["cloud_ios"]
        result = check(cells)
        assert result.complete is False
        assert result.missing == ("cloud_ios",)

    def test_empty_sessions_treated_as_missing(self) -> None:
        cells = _full_cells(50)
        cells["cloud_android"] = _make_cell("cloud_android", 0)
        result = check(cells)
        assert result.complete is False
        assert result.missing == ("cloud_android",)

    def test_multiple_missing_in_expected_order(self) -> None:
        cells = {name: _make_cell(name, 50) for name in EXPECTED_CELLS}
        cells["cloud_android"] = None
        cells["local_ios"] = None
        result = check(cells)
        # Order follows EXPECTED_CELLS, not insertion order
        assert result.missing == ("cloud_android", "local_ios")

    def test_missing_and_partial_can_coexist(self) -> None:
        cells = _full_cells(50)
        cells["local_ios"] = _make_cell("local_ios", 5)
        cells["cloud_ios"] = None
        result = check(cells)
        assert result.complete is False
        assert result.missing == ("cloud_ios",)
        assert result.partial == ("local_ios",)

    def test_custom_partial_threshold(self) -> None:
        cells = _full_cells(50)
        cells["local_android"] = _make_cell("local_android", 25)
        # Default threshold = 30 → partial. With threshold 20 → not partial.
        assert check(cells).partial == ("local_android",)
        assert check(cells, partial_threshold=20).partial == ()


class TestFormatMissing:
    def test_complete_returns_complete_marker(self) -> None:
        result = check(_full_cells(50))
        assert format_missing(result) == "complete"

    def test_missing_listed(self) -> None:
        cells = _full_cells(50)
        cells["local_ios"] = None
        msg = format_missing(check(cells))
        assert "Refusing to send" in msg
        assert "local_ios" in msg

    def test_partial_listed_when_present(self) -> None:
        cells = _full_cells(50)
        cells["local_ios"] = None
        cells["cloud_android"] = _make_cell("cloud_android", 5)
        msg = format_missing(check(cells))
        assert "Missing cells:" in msg
        assert "Partial cells" in msg
        assert "  - local_ios" in msg
        assert "  - cloud_android" in msg
