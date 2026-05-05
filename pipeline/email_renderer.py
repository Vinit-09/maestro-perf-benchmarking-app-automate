"""Subject + HTML body renderer for the Maestro benchmark health-report email.

The renderer takes the same RollupRows that the BQ writer consumes plus the
GateResult that produced them, and emits:

- ``build_subject(...)`` — a short string embedding run-set id, capability
  profile, and gate status, per the new-PDF reporting requirement.
- ``build_html(...)`` — a self-contained HTML email body with a summary table
  across cells and per-cell detail tables broken down by region. NULL stop
  times (Maestro reality) render as an em dash with a footnote.

No external template engine — string-templated HTML keeps the dependency
surface zero, matching the rest of the pipeline.
"""

from __future__ import annotations

import html
from collections import defaultdict
from typing import Iterable

from pipeline.gate import GateResult
from pipeline.rollup import RollupRow


STOP_FOOTNOTE_MARKER = "*"
STOP_FOOTNOTE_TEXT = (
    "Stop time is NULL for Maestro sessions in BigQuery as of 2026-05-04 — "
    "see BENCHMARK_REPORT_IOS_CLOUD recommendation A4."
)


def status_phrase(gate: GateResult) -> str:
    """Compact phrase used in subject + header. Matches the new-PDF requirement
    of capability info appearing alongside outcome."""
    if not gate.complete:
        missing = ", ".join(gate.missing) or "unknown"
        return f"incomplete (missing: {missing})"
    if gate.partial:
        partial = ", ".join(gate.partial)
        return f"complete (partial: {partial})"
    return "4-cell complete"


def build_subject(
    *,
    run_set_id: str,
    capability_profile: str,
    gate: GateResult,
) -> str:
    """Email subject line.

    Format: ``Maestro Benchmark / <run_set_id> / <capability_profile> / <status>``
    """
    return f"Maestro Benchmark / {run_set_id} / {capability_profile} / {status_phrase(gate)}"


# --- HTML helpers ------------------------------------------------------------


def _esc(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def _fmt_int(v: int | None) -> str:
    if v is None:
        return f"&mdash;<sup>{STOP_FOOTNOTE_MARKER}</sup>"
    return f"{v:,}"


def _fmt_int_plain(v: int | None) -> str:
    """Like _fmt_int but plain dash (no footnote) for non-stop fields."""
    if v is None:
        return "&mdash;"
    return f"{v:,}"


def _fmt_float(v: float | None) -> str:
    if v is None:
        return "&mdash;"
    return f"{v:,.1f}"


def _table(rows: list[list[str]], header: list[str]) -> str:
    head = (
        "<thead><tr>"
        + "".join(
            f'<th style="text-align:left;padding:4px 10px;border-bottom:1px solid #ddd;">{h}</th>'
            for h in header
        )
        + "</tr></thead>"
    )
    body_rows = []
    for row in rows:
        body_rows.append(
            "<tr>"
            + "".join(
                f'<td style="padding:4px 10px;border-bottom:1px solid #f1f1f1;">{cell}</td>'
                for cell in row
            )
            + "</tr>"
        )
    return (
        '<table style="border-collapse:collapse;font-family:-apple-system,Segoe UI,sans-serif;'
        'font-size:13px;margin:6px 0 14px 0;">'
        + head
        + "<tbody>"
        + "".join(body_rows)
        + "</tbody></table>"
    )


# --- summary + per-cell renderers --------------------------------------------


def _summary_table(rollup_rows: Iterable[RollupRow]) -> str:
    """One row per cell — sums n_sessions across regions, picks the max-n region's
    P90 numbers as the cell's headline. The per-cell detail table below shows
    the full breakdown."""
    by_cell: dict[str, list[RollupRow]] = defaultdict(list)
    for r in rollup_rows:
        by_cell[r.cell].append(r)

    rows = []
    for cell_name in ("local_android", "cloud_android", "local_ios", "cloud_ios"):
        cell_rows = by_cell.get(cell_name, [])
        if not cell_rows:
            rows.append([cell_name, "&mdash;", "&mdash;", "&mdash;", "&mdash;", "&mdash;"])
            continue
        n_total = sum(r.n_sessions for r in cell_rows)
        # Use the region with the most sessions as the "headline" for summary;
        # the per-cell detail table below shows the full split.
        headline = max(cell_rows, key=lambda r: r.n_sessions)
        rows.append(
            [
                cell_name,
                str(n_total),
                _fmt_float(headline.execution_p50_s),
                _fmt_float(headline.execution_p90_s),
                _fmt_int_plain(headline.start_p50_ms),
                _fmt_int_plain(headline.start_p90_ms),
            ]
        )
    return _table(
        rows,
        header=["Cell", "n_total", "exec P50 (s)", "exec P90 (s)", "start P50 (ms)", "start P90 (ms)"],
    )


def _per_cell_detail(cell_name: str, cell_rows: list[RollupRow]) -> str:
    if not cell_rows:
        return f"<h4>{cell_name}</h4><p><em>No data.</em></p>"

    title_bits = [cell_name]
    if any(r.low_sample for r in cell_rows):
        title_bits.append("⚠ low sample")
    title = " — ".join(title_bits)

    header = [
        "Region",
        "n",
        "wait P50 (ms)",
        "wait P90 (ms)",
        "start P50 (ms)",
        "start P90 (ms)",
        "exec P50 (s)",
        "exec P90 (s)",
        "app_install P50 (ms)",
        "app_install P90 (ms)",
        f"stop P50 (ms){STOP_FOOTNOTE_MARKER}",
        f"stop P90 (ms){STOP_FOOTNOTE_MARKER}",
    ]
    rows: list[list[str]] = []
    for r in cell_rows:
        region_label = r.region or "&mdash;"
        n_label = f"{r.n_sessions}{' (low)' if r.low_sample else ''}"
        rows.append(
            [
                region_label,
                n_label,
                _fmt_int_plain(r.waiting_p50_ms),
                _fmt_int_plain(r.waiting_p90_ms),
                _fmt_int_plain(r.start_p50_ms),
                _fmt_int_plain(r.start_p90_ms),
                _fmt_float(r.execution_p50_s),
                _fmt_float(r.execution_p90_s),
                _fmt_int_plain(r.app_install_p50_ms),
                _fmt_int_plain(r.app_install_p90_ms),
                _fmt_int(r.stop_p50_ms),  # uses footnote marker on NULL
                _fmt_int(r.stop_p90_ms),
            ]
        )
    cell_meta = cell_rows[0]
    capability = _esc(cell_meta.capabilities_profile)
    return (
        f'<h3 style="margin:18px 0 4px 0;font-size:14px;">{title} '
        f'<span style="font-weight:normal;color:#666;font-size:12px;">'
        f"({capability})</span></h3>" + _table(rows, header)
    )


def _gate_banner(gate: GateResult) -> str:
    if gate.complete and not gate.partial:
        return ""  # nothing to flag
    bullets = []
    if gate.missing:
        items = ", ".join(_esc(m) for m in gate.missing)
        bullets.append(f"<li><strong>Missing cells:</strong> {items}</li>")
    if gate.partial:
        items = ", ".join(_esc(p) for p in gate.partial)
        bullets.append(f"<li><strong>Partial cells (n &lt; 30):</strong> {items}</li>")
    return (
        '<div style="background:#fff8e1;border-left:3px solid #f5a623;padding:8px 12px;'
        'margin:8px 0 14px 0;font-size:13px;font-family:-apple-system,Segoe UI,sans-serif;">'
        + "<ul style=\"margin:4px 0;padding-left:18px;\">"
        + "".join(bullets)
        + "</ul></div>"
    )


def _footer(rollup_rows: list[RollupRow], run_set_id: str) -> str:
    build_ids: set[str] = set()
    run_ids: set[str] = set()
    aggregated_at = None
    for r in rollup_rows:
        build_ids.update(r.source_build_ids)
        run_ids.update(r.source_run_ids)
        aggregated_at = r.aggregated_at  # all rows share the same aggregated_at
    builds_str = ", ".join(sorted(build_ids)) or "&mdash;"
    runs_str = ", ".join(sorted(run_ids)) or "&mdash;"
    when = aggregated_at.strftime("%Y-%m-%d %H:%M:%S UTC") if aggregated_at else "&mdash;"

    return (
        '<hr style="margin:18px 0;border:none;border-top:1px solid #eee;"/>'
        '<div style="font-size:12px;color:#777;font-family:-apple-system,Segoe UI,sans-serif;">'
        f"<p><strong>Run set:</strong> {_esc(run_set_id)}<br/>"
        f"<strong>Aggregated at:</strong> {when}<br/>"
        f"<strong>Source build_ids (cloud):</strong> {_esc(builds_str)}<br/>"
        f"<strong>Source run_ids (local):</strong> {_esc(runs_str)}</p>"
        f'<p>{STOP_FOOTNOTE_MARKER} {_esc(STOP_FOOTNOTE_TEXT)}</p>'
        "<p>Report generated by <code>pipeline.email_renderer</code> "
        "from <code>maestro_benchmark_metrics_aggregated</code>.</p>"
        "</div>"
    )


def build_html(
    *,
    rollup_rows: Iterable[RollupRow],
    gate: GateResult,
    run_set_id: str,
    capability_profile: str,
) -> str:
    """Render the full health-report HTML body."""
    rollup_list = list(rollup_rows)
    by_cell: dict[str, list[RollupRow]] = defaultdict(list)
    for r in rollup_list:
        by_cell[r.cell].append(r)

    parts: list[str] = []
    parts.append(
        '<div style="font-family:-apple-system,Segoe UI,sans-serif;color:#222;max-width:900px;">'
    )
    parts.append(
        f'<h2 style="font-size:18px;margin:0 0 4px 0;">Maestro Benchmark — '
        f"{_esc(run_set_id)}</h2>"
    )
    parts.append(
        f'<p style="font-size:13px;color:#555;margin:0 0 8px 0;">'
        f"Capability profile: <code>{_esc(capability_profile)}</code> · "
        f"Status: <strong>{_esc(status_phrase(gate))}</strong></p>"
    )
    parts.append(_gate_banner(gate))

    parts.append('<h3 style="font-size:14px;margin:14px 0 4px 0;">Summary</h3>')
    parts.append(_summary_table(rollup_list))

    for cell_name in ("local_android", "cloud_android", "local_ios", "cloud_ios"):
        parts.append(_per_cell_detail(cell_name, by_cell.get(cell_name, [])))

    parts.append(_footer(rollup_list, run_set_id))
    parts.append("</div>")
    return "".join(parts)
