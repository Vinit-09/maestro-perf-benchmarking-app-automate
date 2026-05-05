"""Tests for pipeline.email_renderer subject + HTML body (U7)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pipeline.email_renderer import (
    STOP_FOOTNOTE_TEXT,
    build_html,
    build_subject,
    status_phrase,
)
from pipeline.gate import GateResult
from pipeline.rollup import RollupRow

AGG_AT = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)


def _row(
    *,
    cell: str = "cloud_ios",
    region: str | None = "ap-south-1",
    n: int = 50,
    low_sample: bool = False,
    execution_p50_s: float | None = 720.0,
    execution_p90_s: float | None = 810.0,
    stop_p50_ms: int | None = None,
    stop_p90_ms: int | None = None,
    capability: str = "defaults",
    source_build_ids: tuple[str, ...] = ("build-abc",),
    source_run_ids: tuple[str, ...] = (),
) -> RollupRow:
    return RollupRow(
        run_set_id="rs-2026-05-05",
        cell=cell,
        framework="maestro",
        os="ios" if "ios" in cell else "android",
        region=region,
        capabilities_profile=capability,
        source_build_ids=source_build_ids,
        source_run_ids=source_run_ids,
        n_sessions=n,
        low_sample=low_sample,
        waiting_p50_ms=0,
        waiting_p90_ms=0,
        waiting_reason_no_parallel_p50_ms=0,
        waiting_reason_no_parallel_p90_ms=0,
        waiting_reason_device_tier_p50_ms=0,
        waiting_reason_device_tier_p90_ms=0,
        waiting_reason_async_signing_p50_ms=0,
        waiting_reason_async_signing_p90_ms=0,
        waiting_reason_region_pool_p50_ms=0,
        waiting_reason_region_pool_p90_ms=0,
        start_p50_ms=12000 if not low_sample else None,
        start_p90_ms=23000 if not low_sample else None,
        execution_p50_s=execution_p50_s,
        execution_p90_s=execution_p90_s,
        app_download_p50_ms=65,
        app_download_p90_ms=860,
        app_install_p50_ms=1600,
        app_install_p90_ms=3700,
        stop_p50_ms=stop_p50_ms,
        stop_p90_ms=stop_p90_ms,
        aggregated_at=AGG_AT,
    )


def _gate(complete: bool = True, missing: tuple[str, ...] = (), partial: tuple[str, ...] = ()) -> GateResult:
    return GateResult(complete=complete, missing=missing, partial=partial)


# ---------------------------------------------------------------------------
# Subject
# ---------------------------------------------------------------------------


class TestBuildSubject:
    def test_complete_4_cells(self) -> None:
        subj = build_subject(
            run_set_id="2026-05-04-baseline",
            capability_profile="defaults",
            gate=_gate(),
        )
        assert subj == "Maestro Benchmark / 2026-05-04-baseline / defaults / 4-cell complete"

    def test_partial_cells_in_subject(self) -> None:
        subj = build_subject(
            run_set_id="rs",
            capability_profile="defaults",
            gate=_gate(partial=("local_ios",)),
        )
        assert "complete (partial: local_ios)" in subj

    def test_incomplete_lists_missing(self) -> None:
        subj = build_subject(
            run_set_id="rs",
            capability_profile="defaults",
            gate=_gate(complete=False, missing=("local_ios", "cloud_android")),
        )
        assert "incomplete (missing: local_ios, cloud_android)" in subj

    def test_capability_profile_appears(self) -> None:
        subj = build_subject(
            run_set_id="rs",
            capability_profile="local_on",
            gate=_gate(),
        )
        # Confirms the new-PDF requirement: capability info in the subject line.
        assert "local_on" in subj


class TestStatusPhrase:
    def test_complete_no_partial(self) -> None:
        assert status_phrase(_gate()) == "4-cell complete"

    def test_complete_with_partial(self) -> None:
        assert status_phrase(_gate(partial=("local_ios",))) == "complete (partial: local_ios)"

    def test_incomplete(self) -> None:
        assert status_phrase(_gate(complete=False, missing=("cloud_ios",))) == "incomplete (missing: cloud_ios)"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


class TestBuildHtml:
    def test_happy_path_4_cells(self) -> None:
        rows = [
            _row(cell="local_android", region=None, source_build_ids=(), source_run_ids=("20260101",)),
            _row(cell="cloud_android"),
            _row(cell="local_ios", region=None, source_build_ids=(), source_run_ids=("20260102",)),
            _row(cell="cloud_ios"),
        ]
        html_body = build_html(
            rollup_rows=rows,
            gate=_gate(),
            run_set_id="rs-2026-05-05",
            capability_profile="defaults",
        )
        # banner shows up only for missing/partial — should NOT appear here
        assert "Missing cells" not in html_body

        # all 4 cell names appear in the body (summary + per-cell sections)
        for cell_name in ("local_android", "cloud_android", "local_ios", "cloud_ios"):
            assert cell_name in html_body
        # capability profile + run_set_id surfaced
        assert "defaults" in html_body
        assert "rs-2026-05-05" in html_body
        # source ids shown in footer
        assert "build-abc" in html_body
        assert "20260101" in html_body

    def test_low_sample_annotates_n_in_per_cell_table(self) -> None:
        rows = [
            _row(cell="cloud_ios", region="us-east-1", n=3, low_sample=True,
                 execution_p50_s=None, execution_p90_s=None),
        ]
        body = build_html(
            rollup_rows=rows, gate=_gate(),
            run_set_id="rs", capability_profile="defaults",
        )
        assert "low sample" in body
        # the (low) annotation appears in the n cell of the per-cell table
        assert "3 (low)" in body

    def test_null_stop_renders_em_dash_with_footnote(self) -> None:
        rows = [_row(stop_p50_ms=None, stop_p90_ms=None)]
        body = build_html(
            rollup_rows=rows, gate=_gate(),
            run_set_id="rs", capability_profile="defaults",
        )
        # &mdash; with the footnote marker (default *) appears in the body
        assert "&mdash;<sup>*</sup>" in body
        # footer carries the footnote text
        assert STOP_FOOTNOTE_TEXT in body

    def test_gate_banner_renders_missing_cells(self) -> None:
        rows = [_row()]
        body = build_html(
            rollup_rows=rows,
            gate=_gate(complete=False, missing=("local_ios",)),
            run_set_id="rs",
            capability_profile="defaults",
        )
        assert "Missing cells" in body
        assert "local_ios" in body

    def test_gate_banner_renders_partial_cells(self) -> None:
        rows = [_row()]
        body = build_html(
            rollup_rows=rows,
            gate=_gate(partial=("local_ios",)),
            run_set_id="rs",
            capability_profile="defaults",
        )
        assert "Partial cells" in body
        assert "local_ios" in body

    def test_run_set_id_html_escaped(self) -> None:
        rows = [_row()]
        body = build_html(
            rollup_rows=rows, gate=_gate(),
            run_set_id="rs<script>",
            capability_profile="defaults",
        )
        # Title and footer use the escaped form, not the raw <script>.
        assert "<script>" not in body
        assert "rs&lt;script&gt;" in body

    def test_summary_shows_all_4_cell_rows_even_if_one_missing(self) -> None:
        rows = [_row(cell="cloud_ios")]
        body = build_html(
            rollup_rows=rows,
            gate=_gate(complete=False, missing=("local_android", "cloud_android", "local_ios")),
            run_set_id="rs",
            capability_profile="defaults",
        )
        # summary table includes a row for each of the four cells; missing ones get em-dash
        for cell_name in ("local_android", "cloud_android", "local_ios", "cloud_ios"):
            assert cell_name in body
