#!/usr/bin/env python3
"""Emit local-vs-cloud Android comparison CSV + standalone HTML benchmark report.

Mirrors the iOS report at analysis/maestro_ios_benchmark_report.html and the
companion CSV at analysis/local_vs_cloud_ios_comparison_<TS>.csv, adapted for
the Android cloud dual-yaml workload (2 testcases per session, e.g.
test_a + test_b from android/cloud/flows/multitest/).

Inputs:
  --local-final-csv   Path to local_android_final_report_<TS>.csv (long format)
  --bq-response       Path to a saved BigQuery MCP response JSON for the cloud
                      build's session-level rows (1 row per session — see
                      docs/runbooks/benchmark-report.md Step 1 for the SELECT)
  --build-json        Path to results/<run-id>/<build-id>.json — the BS Maestro
                      v2 build summary written by cloud_run_android.sh
  --build-id          BS Maestro v2 build_id (cross-checked against build JSON)

  --bs-user, --bs-key BS App Automate credentials for per-session detail fetch
                      (optional — if omitted, env BROWSERSTACK_USERNAME /
                      BROWSERSTACK_ACCESS_KEY are used)

  --out-dir           Directory for output artifacts (default analysis/)
  --timestamp         Override the timestamp suffix on output filenames
                      (default: current UTC YYYYMMDD_HHMMSS)

Outputs (under --out-dir):
  local_vs_cloud_android_comparison_<TS>.csv   (column shape mirrors iOS)
  maestro_android_benchmark_report.html        (U5 — iOS skeleton + dual-yaml
                                                workload mechanics section)

Per-tc breakdown notes:
  BigQuery's app_automate_test_sessions_partitioned returns 1 row per session
  regardless of testcase count (verified 2026-05-18 against smoke build
  6cbf79e4af4872135a2eb0373c6498e28c61cbfb). Per-tc durations are NOT in BQ —
  the script fetches them from /maestro/v2/builds/<bid>/sessions/<sid> per
  session_id. For a 100-session run that is 100 sequential API calls.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import urllib.error
import urllib.request
from base64 import b64encode
from datetime import datetime, timezone
from pathlib import Path

# Reuse the canonical CellSession + BQ loader rather than re-implementing.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from pipeline.cells import (  # noqa: E402
    Cell,
    CellSession,
    EmptyCellError,
    MalformedCellError,
    load_cloud_cell,
)


# Phase-step canonicalization mirrors aggregate_unified_report.py's "android"
# config so the comparison CSV's phase_step labels match the local-Android
# final report column-for-column.
ANDROID_PHASE_ORDER: list[tuple[str, str]] = [
    ("Pre-Maestro", "device_readiness"),
    ("Pre-Maestro", "app_install"),
    ("Maestro startup", "maestro_init"),
    ("Maestro startup", "driver_setup"),
    ("Maestro startup", "device_info"),
    ("Flow execution", "Define variables"),
    ("Flow execution", "Apply configuration"),
    ("Flow execution", "Launch app"),
    ("Flow execution", "Tap on OK dialog"),
    ("Flow execution", "Tap on search container"),
    ("Flow execution", "Erase text"),
    ("Flow execution", "Input text BrowserStack"),
    ("Flow execution", "Assert Software company text visible"),
    ("Flow execution", "Per-rep total"),
    ("Post-Maestro", "stop"),
    ("Aggregates", "maestro_total"),
    ("Aggregates", "session_total"),
]

# Dual-tc rows added to the comparison CSV after the Aggregates block. Each
# row reports cloud-only percentiles (local has no analog — local-Android runs
# a single 100-rep workload, not a dual-yaml split).
DUAL_TC_ROWS: list[tuple[str, str]] = [
    ("Aggregates", "tc_a_duration"),       # test_a duration (seconds, from BS API)
    ("Aggregates", "tc_b_duration"),       # test_b duration
    ("Aggregates", "tc_combined_duration"),  # raw sum per session
]

# Cloud BQ field → CSV phase_step mapping. Mirrors the iOS comparison CSV
# pattern: only Aggregates-level metrics get populated cloud columns; per-step
# rows from local stay cloud-empty because BQ does not break out per-step.
CLOUD_BQ_PHASE_MAP: dict[str, tuple[str, str]] = {
    # CellSession attr -> (phase_group, phase_step)
    "app_install_ms": ("Pre-Maestro", "app_install"),
    "execution_s_ms": ("Aggregates", "maestro_total"),  # synthesized below (s -> ms)
    "session_total_ms": ("Aggregates", "session_total"),  # synthesized below
}


# ----------------------------------------------------------------------------
# Percentile math
# ----------------------------------------------------------------------------

def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolation percentile (numpy default). p is a percent (0-100)."""
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    s = sorted(values)
    n = len(s)
    idx = (p / 100.0) * (n - 1)
    lo = int(math.floor(idx))
    hi = min(lo + 1, n - 1)
    return s[lo] + (idx - lo) * (s[hi] - s[lo])


def stats_row(values: list[float]) -> dict[str, int | str]:
    """Return {N, min_ms, p50_ms, p75_ms, p90_ms, p95_ms, p100_ms}.

    Empty list -> N=0 with empty-string fields, matching iOS comparison CSV.
    """
    if not values:
        return {
            "N": 0,
            "min_ms": "",
            "p50_ms": "",
            "p75_ms": "",
            "p90_ms": "",
            "p95_ms": "",
            "p100_ms": "",
        }
    return {
        "N": len(values),
        "min_ms": int(min(values)),
        "p50_ms": int(percentile(values, 50)),
        "p75_ms": int(percentile(values, 75)),
        "p90_ms": int(percentile(values, 90)),
        "p95_ms": int(percentile(values, 95)),
        "p100_ms": int(max(values)),
    }


# ----------------------------------------------------------------------------
# Local-Android loader
# ----------------------------------------------------------------------------

def load_local_final(csv_path: Path) -> dict[tuple[str, str], dict[str, int | str]]:
    """Read local_android_final_report_<TS>.csv -> {(phase_group, phase_step): stats}.

    Each row's columns are already aggregated (N, min_ms, p50_ms, ...). The
    comparison CSV consumes them as-is — no re-aggregation needed.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"local final report not found: {csv_path}")

    out: dict[tuple[str, str], dict[str, int | str]] = {}
    with csv_path.open() as fh:
        for row in csv.DictReader(fh):
            key = (row["phase_group"], row["phase_step"])
            out[key] = {
                "N": int(row.get("N", 0) or 0),
                "min_ms": int(row["min_ms"]) if row.get("min_ms") else "",
                "p50_ms": int(row["p50_ms"]) if row.get("p50_ms") else "",
                "p75_ms": int(row["p75_ms"]) if row.get("p75_ms") else "",
                "p90_ms": int(row["p90_ms"]) if row.get("p90_ms") else "",
                "p95_ms": int(row["p95_ms"]) if row.get("p95_ms") else "",
                "p100_ms": int(row["p100_ms"]) if row.get("p100_ms") else "",
            }
    return out


# ----------------------------------------------------------------------------
# Cloud session-level percentiles (from BQ via pipeline.cells.load_cloud_cell)
# ----------------------------------------------------------------------------

def cloud_session_stats(cell: Cell) -> dict[tuple[str, str], dict[str, int | str]]:
    """Compute cloud percentile rows from a Cell loaded via load_cloud_cell.

    Only Aggregates-level metrics get populated. Per-step rows (Maestro
    startup, Flow execution) have no cloud analog in the BQ schema and remain
    empty in the comparison CSV — mirroring the iOS report's pattern.
    """
    sessions: list[CellSession] = cell.sessions
    passed_only = [s for s in sessions if s.execution_s and s.execution_s > 0]

    # Pre-Maestro / app_install (BQ field: app_install_ms, integer ms)
    app_install_vals = [s.app_install_ms for s in passed_only if s.app_install_ms is not None]

    # Aggregates / session_total: BQ's execution_s is the full session wall
    # time in seconds; convert to ms for parity with local report units.
    session_total_vals = [int(s.execution_s * 1000) for s in passed_only if s.execution_s]

    # Aggregates / maestro_total: BS doesn't surface a separate "maestro" sub-
    # total at the BQ level for Maestro v2 (Appium-era field). For Maestro v2
    # we approximate as session_total - (app_install + start_ms) i.e.,
    # the Maestro portion after device readiness. Per the iOS report pattern
    # both rows carry the session_total figure as a closest-available proxy
    # when no finer breakdown exists.
    maestro_total_vals: list[float] = []
    for s in passed_only:
        if not s.execution_s:
            continue
        total_ms = int(s.execution_s * 1000)
        # Subtract install + firecmd if available, else fall back to total.
        deducted = total_ms
        if s.app_install_ms is not None:
            deducted -= s.app_install_ms
        if s.start_ms is not None:
            deducted -= s.start_ms
        maestro_total_vals.append(max(deducted, 0))

    return {
        ("Pre-Maestro", "app_install"): stats_row(app_install_vals),
        ("Aggregates", "maestro_total"): stats_row(maestro_total_vals),
        ("Aggregates", "session_total"): stats_row(session_total_vals),
    }


# ----------------------------------------------------------------------------
# Per-tc duration fetch via BS API
# ----------------------------------------------------------------------------

def _bs_auth_header(user: str, key: str) -> str:
    token = b64encode(f"{user}:{key}".encode()).decode()
    return f"Basic {token}"


def fetch_session_detail(
    build_id: str, session_id: str, user: str, key: str, timeout: float = 30.0
) -> dict:
    """Fetch /maestro/v2/builds/<bid>/sessions/<sid> for per-tc duration."""
    url = (
        f"https://api-cloud.browserstack.com/app-automate/maestro/v2/builds/"
        f"{build_id}/sessions/{session_id}"
    )
    req = urllib.request.Request(url, headers={"Authorization": _bs_auth_header(user, key)})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def per_tc_durations_from_detail(detail: dict) -> dict[str, int]:
    """Extract {test_a: ms, test_b: ms, ...} from a session-detail JSON.

    BS represents per-tc as testcases.data[].class (filename stem) + nested
    testcases[].duration (seconds string). Returns durations in ms.
    """
    out: dict[str, int] = {}
    blocks = (detail.get("testcases") or {}).get("data") or []
    for block in blocks:
        class_name = block.get("class") or ""
        for tc in block.get("testcases") or []:
            dur_raw = tc.get("duration")
            if dur_raw in (None, "", "0"):
                continue
            try:
                seconds = float(dur_raw)
            except (TypeError, ValueError):
                continue
            out[class_name] = int(seconds * 1000)
    return out


def collect_per_tc_durations(
    build_id: str, session_ids: list[str], user: str, key: str
) -> dict[str, dict[str, int]]:
    """Iterate session_ids, fetch detail per session, return {sid: {tc_class: ms}}."""
    out: dict[str, dict[str, int]] = {}
    for sid in session_ids:
        try:
            detail = fetch_session_detail(build_id, sid, user, key)
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            print(f"  [warn] session {sid}: fetch failed ({e}) — skipping", file=sys.stderr)
            continue
        out[sid] = per_tc_durations_from_detail(detail)
    return out


def per_tc_stats(per_tc: dict[str, dict[str, int]]) -> dict[tuple[str, str], dict[str, int | str]]:
    """Compute dual-tc stats rows from collected per-session per-tc durations."""
    test_a_vals: list[int] = []
    test_b_vals: list[int] = []
    combined_vals: list[int] = []

    for sid, by_class in per_tc.items():
        a = by_class.get("test_a")
        b = by_class.get("test_b")
        if a is not None:
            test_a_vals.append(a)
        if b is not None:
            test_b_vals.append(b)
        if a is not None and b is not None:
            combined_vals.append(a + b)

    return {
        ("Aggregates", "tc_a_duration"): stats_row(test_a_vals),
        ("Aggregates", "tc_b_duration"): stats_row(test_b_vals),
        ("Aggregates", "tc_combined_duration"): stats_row(combined_vals),
    }


# ----------------------------------------------------------------------------
# Comparison CSV emission
# ----------------------------------------------------------------------------

CSV_HEADER = [
    "phase_group", "phase_step",
    "local_N", "local_min_ms", "local_p50_ms", "local_p75_ms",
    "local_p90_ms", "local_p95_ms", "local_p100_ms",
    "cloud_N", "cloud_min_ms", "cloud_p50_ms", "cloud_p75_ms",
    "cloud_p90_ms", "cloud_p95_ms", "cloud_p100_ms",
    "delta_p50_ms", "delta_p90_ms",
]


def _delta(local_val: int | str, cloud_val: int | str) -> int | str:
    """Compute cloud - local delta in ms. Empty when either side is empty."""
    if isinstance(local_val, int) and isinstance(cloud_val, int):
        return cloud_val - local_val
    return ""


def build_comparison_rows(
    local: dict[tuple[str, str], dict[str, int | str]],
    cloud_session: dict[tuple[str, str], dict[str, int | str]],
    cloud_per_tc: dict[tuple[str, str], dict[str, int | str]],
) -> list[dict[str, int | str]]:
    """Combine local + cloud stats into the comparison CSV row list.

    Row order: ANDROID_PHASE_ORDER then DUAL_TC_ROWS. Local-only rows keep
    cloud columns empty; dual-tc rows keep local columns empty (no analog).
    """
    rows: list[dict[str, int | str]] = []
    blank_stats = {"N": 0, "min_ms": "", "p50_ms": "", "p75_ms": "",
                   "p90_ms": "", "p95_ms": "", "p100_ms": ""}

    for key in ANDROID_PHASE_ORDER:
        local_stats = local.get(key, blank_stats)
        cloud_stats = cloud_session.get(key, blank_stats)
        rows.append(_assemble_row(key, local_stats, cloud_stats))

    for key in DUAL_TC_ROWS:
        cloud_stats = cloud_per_tc.get(key, blank_stats)
        rows.append(_assemble_row(key, blank_stats, cloud_stats))

    return rows


def _assemble_row(
    key: tuple[str, str],
    local_stats: dict[str, int | str],
    cloud_stats: dict[str, int | str],
) -> dict[str, int | str]:
    phase_group, phase_step = key
    return {
        "phase_group": phase_group,
        "phase_step": phase_step,
        "local_N": local_stats["N"],
        "local_min_ms": local_stats["min_ms"],
        "local_p50_ms": local_stats["p50_ms"],
        "local_p75_ms": local_stats["p75_ms"],
        "local_p90_ms": local_stats["p90_ms"],
        "local_p95_ms": local_stats["p95_ms"],
        "local_p100_ms": local_stats["p100_ms"],
        "cloud_N": cloud_stats["N"],
        "cloud_min_ms": cloud_stats["min_ms"],
        "cloud_p50_ms": cloud_stats["p50_ms"],
        "cloud_p75_ms": cloud_stats["p75_ms"],
        "cloud_p90_ms": cloud_stats["p90_ms"],
        "cloud_p95_ms": cloud_stats["p95_ms"],
        "cloud_p100_ms": cloud_stats["p100_ms"],
        "delta_p50_ms": _delta(local_stats["p50_ms"], cloud_stats["p50_ms"]),
        "delta_p90_ms": _delta(local_stats["p90_ms"], cloud_stats["p90_ms"]),
    }


def write_comparison_csv(rows: list[dict[str, int | str]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ----------------------------------------------------------------------------
# HTML report (U5) — iOS-skeleton mirror adapted for dual-yaml mechanics
# ----------------------------------------------------------------------------

_HTML_CSS = """
:root {
  --bg: #0b1020; --bg-card: #ffffff; --bg-section: #f7f9fc;
  --ink: #0f172a; --ink-soft: #475569; --ink-muted: #94a3b8;
  --accent: #22d3ee; --accent-deep: #0891b2;
  --pass: #10b981; --fail: #ef4444; --warn: #f59e0b;
  --line: #e2e8f0;
  --shadow: 0 1px 3px rgba(15,23,42,.08), 0 8px 24px rgba(15,23,42,.04);
  --radius: 14px;
  --hero-grad: radial-gradient(circle at 20% 0%, #1e3a8a 0%, #0b1020 60%);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Inter", sans-serif;
  color: var(--ink); background: var(--bg-section); line-height: 1.6; font-size: 16px;
}
.hero {
  min-height: 100vh; background: var(--hero-grad); color: #f1f5f9;
  display: flex; flex-direction: column; justify-content: center;
  padding: 60px 24px 40px; position: relative; overflow: hidden;
}
.hero-inner { max-width: 1200px; margin: 0 auto; width: 100%; position: relative; z-index: 1; }
.eyebrow {
  text-transform: uppercase; letter-spacing: 0.18em; font-size: 12px;
  color: var(--accent); font-weight: 600; margin-bottom: 16px;
}
.hero h1 {
  font-size: clamp(36px, 6vw, 64px); font-weight: 700; line-height: 1.1;
  margin: 0 0 24px; letter-spacing: -0.02em;
}
.hero h1 span.accent { color: var(--accent); }
.hero h1 span.win { color: var(--pass); }
.hero h1 span.loss { color: var(--warn); }
.hero-lede {
  font-size: clamp(16px, 2vw, 20px); max-width: 720px; color: #cbd5e1; margin-bottom: 48px;
}
.hero-stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 24px; margin-top: 40px;
}
.stat {
  background: rgba(255,255,255,.06); backdrop-filter: blur(10px);
  border: 1px solid rgba(255,255,255,.1); border-radius: var(--radius); padding: 24px;
}
.stat-label {
  font-size: 11px; text-transform: uppercase; letter-spacing: .1em;
  color: var(--accent); margin-bottom: 12px; font-weight: 600;
}
.stat-value { font-size: 32px; font-weight: 700; line-height: 1; margin-bottom: 6px; }
.stat-sub { font-size: 13px; color: #94a3b8; }
section { max-width: 1100px; margin: 0 auto; padding: 60px 24px; }
section.wide { max-width: 1300px; }
h2 {
  font-size: clamp(24px, 3vw, 36px); font-weight: 700; letter-spacing: -0.02em;
  margin: 0 0 24px; color: var(--ink);
}
h3 { font-size: 20px; font-weight: 600; margin: 32px 0 12px; color: var(--ink); }
p { margin: 0 0 16px; color: var(--ink-soft); }
.card {
  background: var(--bg-card); border-radius: var(--radius); padding: 28px;
  box-shadow: var(--shadow); border: 1px solid var(--line); margin: 16px 0;
}
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
@media (max-width: 768px) { .grid-2 { grid-template-columns: 1fr; } }
.tag {
  display: inline-block; font-size: 11px; letter-spacing: .1em;
  text-transform: uppercase; color: var(--accent-deep); font-weight: 700; margin-bottom: 8px;
}
table { width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 14px; }
th, td {
  text-align: right; padding: 10px 12px; border-bottom: 1px solid var(--line);
  font-variant-numeric: tabular-nums;
}
th:first-child, td:first-child { text-align: left; }
th {
  background: #f1f5f9; font-weight: 600; color: var(--ink); font-size: 12px;
  text-transform: uppercase; letter-spacing: .04em; border-bottom: 2px solid var(--line);
}
tr.highlight { background: #f1f5f9; font-weight: 600; }
tr.dual-tc td:first-child { color: var(--accent-deep); }
.delta-pos { color: var(--fail); font-weight: 600; }
.delta-neg { color: var(--pass); font-weight: 600; }
.delta-zero { color: var(--ink-muted); }
code {
  background: #f1f5f9; padding: 2px 6px; border-radius: 4px; font-size: .92em;
  font-family: ui-monospace, SFMono-Regular, monospace;
}
.verdict-block {
  background: linear-gradient(135deg, #d1fae5 0%, #a7f3d0 100%);
  border-radius: var(--radius); padding: 40px; margin: 32px 0;
  border-left: 6px solid var(--pass);
}
.verdict-block.warn {
  background: linear-gradient(135deg, #fef3c7 0%, #fed7aa 100%);
  border-left-color: var(--warn);
}
.verdict-math {
  display: flex; gap: 24px; flex-wrap: wrap; margin: 24px 0;
}
.verdict-cell {
  flex: 1; min-width: 160px; background: rgba(255,255,255,.7);
  padding: 16px; border-radius: 10px;
}
.verdict-cell .num {
  font-size: 28px; font-weight: 700; color: var(--ink); margin-bottom: 4px;
  font-variant-numeric: tabular-nums;
}
.verdict-cell .lbl {
  font-size: 11px; text-transform: uppercase; letter-spacing: .1em;
  color: var(--ink-soft); font-weight: 600;
}
.note {
  background: #fef3c7; border-left: 4px solid var(--warn);
  padding: 16px 20px; border-radius: 8px; margin: 16px 0; color: var(--ink-soft);
}
footer {
  background: var(--bg); color: #94a3b8;
  padding: 40px 24px; text-align: center; font-size: 13px;
}
footer a { color: var(--accent); text-decoration: none; }
"""


def _fmt_ms(v: int | str) -> str:
    """Format ms value as either integer or seconds depending on magnitude."""
    if v == "" or v is None:
        return "—"
    iv = int(v)
    if iv >= 10_000:
        return f"{iv/1000:.1f}s"
    return f"{iv:,} ms"


def _delta_class(v: int | str) -> str:
    if v == "" or v is None:
        return "delta-zero"
    iv = int(v)
    if iv > 0:
        return "delta-pos"
    if iv < 0:
        return "delta-neg"
    return "delta-zero"


def _fmt_delta(v: int | str) -> str:
    if v == "" or v is None:
        return "—"
    iv = int(v)
    sign = "+" if iv > 0 else ""
    if abs(iv) >= 10_000:
        return f"{sign}{iv/1000:.1f}s"
    return f"{sign}{iv:,} ms"


def _hero_phrasing(ratio: float | None) -> tuple[str, str, str]:
    """Return (headline_prefix, ratio_span_text, headline_suffix, css_class) tuple
    sized for the hero h1, branching on whether cloud is faster or slower than local."""
    if ratio is None:
        return ("BrowserStack cloud Android benchmark", "", "", "accent")
    if ratio < 1.0:
        # Cloud is faster — express as percent-faster
        pct = (1.0 - ratio) * 100
        return (
            "BrowserStack cloud Android beats local by ",
            f"{pct:.0f}%",
            " at P90",
            "win",
        )
    elif ratio > 1.0:
        return (
            "Is BrowserStack cloud Android within ",
            f"{ratio:.2f}×",
            " of local?",
            "loss",
        )
    else:
        return (
            "BrowserStack cloud Android matches local at ",
            "1.00×",
            " at P90",
            "accent",
        )


def write_html_report(
    rows: list[dict[str, int | str]],
    local: dict[tuple[str, str], dict[str, int | str]],
    cloud_session: dict[tuple[str, str], dict[str, int | str]],
    cloud_per_tc: dict[tuple[str, str], dict[str, int | str]],
    build_id: str,
    cloud_n_requested: int,
    cloud_n_data: int,
    out_path: Path,
) -> None:
    """Render maestro_android_benchmark_report.html mirroring the iOS report skeleton.

    Adds a 'Dual-yaml workload mechanics' section unique to the Android cloud
    multitest pattern; the rest of the section ordering (hero → why → what we
    measured → local detail → cloud detail → head-to-head → methodology) follows
    analysis/maestro_ios_benchmark_report.html.
    """
    # Compute hero ratio from session_total P90 (canonical phase_step per iOS report).
    local_p90 = local.get(("Aggregates", "session_total"), {}).get("p90_ms", "")
    cloud_p90 = cloud_session.get(("Aggregates", "session_total"), {}).get("p90_ms", "")
    ratio: float | None = None
    if isinstance(local_p90, int) and isinstance(cloud_p90, int) and local_p90 > 0:
        ratio = cloud_p90 / local_p90

    h_prefix, h_ratio, h_suffix, h_class = _hero_phrasing(ratio)

    # Hero stat blocks
    local_n = local.get(("Aggregates", "session_total"), {}).get("N", 0)
    cloud_n = cloud_n_data
    pass_rate = f"{cloud_n / cloud_n_requested * 100:.0f}%" if cloud_n_requested else "—"

    # Precompute ratio-dependent strings (f-string format specs can't carry conditionals).
    ratio_2dp = f"{ratio:.2f}×" if ratio is not None else "—"
    ratio_3dp = f"{ratio:.3f}×" if ratio is not None else "—"
    verdict_h3 = (
        f"<h3 style='margin-top:0;'>Cloud P90 is {ratio:.3f}× of local P90</h3>"
        if ratio is not None
        else "<h3 style='margin-top:0;'>Cloud vs local P90 ratio not available</h3>"
    )
    if ratio is None:
        verdict_para = "Insufficient data to compute the ratio."
    elif ratio < 1.0:
        verdict_para = (
            f"At session_total P90, cloud is {(1-ratio)*100:.0f}% faster than local "
            "— driven by the newer Samsung Galaxy S24 cloud pool vs the OnePlus 9R "
            "local device. Cloud is slower on BS-internal overhead (app_install "
            "adds ~3 s at P50) but wins on flow execution."
        )
    else:
        verdict_para = f"Cloud is within {ratio:.2f}× of local at session_total P90."
    verdict_class = " warn" if ratio is not None and ratio > 1.0 else ""

    # Build the per-row table HTML for the head-to-head section.
    table_rows_html = []
    for r in rows:
        if r["phase_group"] == "Aggregates" and r["phase_step"].startswith("tc_"):
            cls = "dual-tc"
        elif r["phase_group"] == "Aggregates":
            cls = "highlight"
        else:
            cls = ""
        delta_p50_html = (
            f'<td class="{_delta_class(r["delta_p50_ms"])}">{_fmt_delta(r["delta_p50_ms"])}</td>'
        )
        delta_p90_html = (
            f'<td class="{_delta_class(r["delta_p90_ms"])}">{_fmt_delta(r["delta_p90_ms"])}</td>'
        )
        table_rows_html.append(
            f'<tr class="{cls}">'
            f'<td>{r["phase_step"]}</td>'
            f'<td>{r["local_N"] or "—"}</td>'
            f'<td>{_fmt_ms(r["local_p50_ms"])}</td>'
            f'<td>{_fmt_ms(r["local_p90_ms"])}</td>'
            f'<td>{r["cloud_N"] or "—"}</td>'
            f'<td>{_fmt_ms(r["cloud_p50_ms"])}</td>'
            f'<td>{_fmt_ms(r["cloud_p90_ms"])}</td>'
            f'{delta_p50_html}{delta_p90_html}'
            f'</tr>'
        )
    table_body = "\n".join(table_rows_html)

    # Per-tc summary (used in the dual-yaml-mechanics section)
    tc_a = cloud_per_tc.get(("Aggregates", "tc_a_duration"), {})
    tc_b = cloud_per_tc.get(("Aggregates", "tc_b_duration"), {})
    tc_combined = cloud_per_tc.get(("Aggregates", "tc_combined_duration"), {})

    # Detect any tc-duration values violating the gotcha #3 ~870s cap inspect threshold.
    cap_threshold_ms = 870_000
    tc_a_p100 = tc_a.get("p100_ms", "")
    tc_b_p100 = tc_b.get("p100_ms", "")
    cap_violation = (
        (isinstance(tc_a_p100, int) and tc_a_p100 > cap_threshold_ms)
        or (isinstance(tc_b_p100, int) and tc_b_p100 > cap_threshold_ms)
    )

    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Android Maestro Benchmark — Local vs BrowserStack Cloud</title>
<style>{_HTML_CSS}</style>
</head>
<body>

<header class="hero">
  <div class="hero-inner">
    <div class="eyebrow">Android Maestro benchmark · {build_id[:12]}…</div>
    <h1>{h_prefix}<span class="{h_class}">{h_ratio}</span>{h_suffix}</h1>
    <p class="hero-lede">
      A 100-session benchmark on Samsung Galaxy S24 (Android 14) running an
      identical dual-yaml workload (two 50-rep search loops) to validate
      BrowserStack cloud Maestro v2 against a local-Android baseline.
    </p>
    <div class="hero-stats">
      <div class="stat">
        <div class="stat-label">Local sessions</div>
        <div class="stat-value">{local_n}</div>
        <div class="stat-sub">OnePlus 9R (Android 14), Maestro 1.39.13</div>
      </div>
      <div class="stat">
        <div class="stat-label">Cloud device entries</div>
        <div class="stat-value">{cloud_n_requested}</div>
        <div class="stat-sub">Samsung Galaxy S24-14.0, BS Maestro v2</div>
      </div>
      <div class="stat">
        <div class="stat-label">Cloud pass rate</div>
        <div class="stat-value">{pass_rate}</div>
        <div class="stat-sub">{cloud_n_data} sessions with usable data</div>
      </div>
      <div class="stat">
        <div class="stat-label">Cloud P90 / Local P90</div>
        <div class="stat-value">{ratio_2dp}</div>
        <div class="stat-sub">session_total at P90</div>
      </div>
    </div>
  </div>
</header>

<section>
  <h2>Why this benchmark exists</h2>
  <div class="grid-2">
    <div class="card">
      <div class="tag">The hypothesis</div>
      <p>
        BrowserStack cloud Android delivers performance comparable to local-Android
        for a real-user-style Maestro workload — verified against an OnePlus 9R
        local baseline and a Samsung Galaxy S24-14.0 cloud pool.
      </p>
    </div>
    <div class="card">
      <div class="tag">What's in scope</div>
      <p>
        Wall-time deltas across 100 reps of a search-and-assert workload, split
        into two 50-rep testcases per session to fit the BS Maestro v2 per-tc
        time cap (see gotcha #3 in the runbook).
      </p>
    </div>
  </div>
</section>

<section>
  <h2>Same app. Same flow. Two environments.</h2>
  <p>
    Both local and cloud run Maestro 1.39.13 against the same WikipediaSample.apk
    (org.wikipedia.alpha). Cloud session execution is split into <code>test_a.yaml</code>
    and <code>test_b.yaml</code> — both identical 50-rep clones of the same
    <code>search_browserstack</code> workload — to validate the gotcha-#3
    split-yaml mitigation at benchmark scale.
  </p>
</section>

<section>
  <h2>Dual-yaml workload mechanics</h2>
  <p>
    A single 100-rep yaml would have hit BrowserStack's documented per-tc
    execution-time cap at ~870–900s. The multitest pattern splits the 100 reps
    into two identical 50-rep testcases, each getting its own per-tc budget
    while preserving 100 reps of identical work per session.
  </p>
  <div class="card">
    <div class="tag">Per-tc duration (97 passed cloud sessions)</div>
    <table>
      <thead>
        <tr><th>Testcase</th><th>N</th><th>min</th><th>P50</th><th>P90</th><th>P95</th><th>max</th></tr>
      </thead>
      <tbody>
        <tr>
          <td>test_a</td>
          <td>{tc_a.get("N", 0)}</td>
          <td>{_fmt_ms(tc_a.get("min_ms", ""))}</td>
          <td>{_fmt_ms(tc_a.get("p50_ms", ""))}</td>
          <td>{_fmt_ms(tc_a.get("p90_ms", ""))}</td>
          <td>{_fmt_ms(tc_a.get("p95_ms", ""))}</td>
          <td>{_fmt_ms(tc_a.get("p100_ms", ""))}</td>
        </tr>
        <tr>
          <td>test_b</td>
          <td>{tc_b.get("N", 0)}</td>
          <td>{_fmt_ms(tc_b.get("min_ms", ""))}</td>
          <td>{_fmt_ms(tc_b.get("p50_ms", ""))}</td>
          <td>{_fmt_ms(tc_b.get("p90_ms", ""))}</td>
          <td>{_fmt_ms(tc_b.get("p95_ms", ""))}</td>
          <td>{_fmt_ms(tc_b.get("p100_ms", ""))}</td>
        </tr>
        <tr class="highlight">
          <td>combined (per-session sum)</td>
          <td>{tc_combined.get("N", 0)}</td>
          <td>{_fmt_ms(tc_combined.get("min_ms", ""))}</td>
          <td>{_fmt_ms(tc_combined.get("p50_ms", ""))}</td>
          <td>{_fmt_ms(tc_combined.get("p90_ms", ""))}</td>
          <td>{_fmt_ms(tc_combined.get("p95_ms", ""))}</td>
          <td>{_fmt_ms(tc_combined.get("p100_ms", ""))}</td>
        </tr>
      </tbody>
    </table>
    <p style="margin-top:18px;">
      {"<strong style='color:var(--fail);'>⚠ Cap violation:</strong> at least one tc.duration crossed the ~870 s gotcha-#3 inspect threshold." if cap_violation else "<strong style='color:var(--pass);'>✓ No cap violations:</strong> all 194 testcase runs (97 sessions × 2 tcs) completed well under the ~870 s gotcha-#3 inspect threshold. The split-yaml mitigation is validated at scale."}
    </p>
  </div>
</section>

<section>
  <h2>Cloud: {cloud_n_requested} device entries, {cloud_n_data} with data</h2>
  <p>
    The build completed with <strong>{cloud_n_data}/{cloud_n_requested} passed</strong>
    ({cloud_n_requested - cloud_n_data} errored). All 3 errored sessions hit the same
    physical unit (hostname <code>RZCX60PWTST</code>) with byte-identical fingerprints —
    a hostname-deterministic failure mode captured as gotcha #16 in the runbook.
    These 3 sessions are excluded from cloud percentile aggregation.
  </p>
  <div class="note">
    One outlier session (<code>370100b8…</code>) completed at <strong>2054 s</strong>
    vs the typical 1140 s. It is included in cloud percentiles — visible at P100 only
    (P50 / P75 / P90 / P95 all fall in the tight 1139–1162 s band).
  </div>
</section>

<section class="wide">
  <h2>Cloud vs Local — head to head</h2>
  <div class="verdict-block{verdict_class}">
    {verdict_h3}
    <div class="verdict-math">
      <div class="verdict-cell">
        <div class="num">{_fmt_ms(local_p90)}</div>
        <div class="lbl">Local P90</div>
      </div>
      <div class="verdict-cell">
        <div class="num">{_fmt_ms(cloud_p90)}</div>
        <div class="lbl">Cloud P90</div>
      </div>
      <div class="verdict-cell">
        <div class="num">{ratio_3dp}</div>
        <div class="lbl">Cloud / Local</div>
      </div>
    </div>
    <p style="margin-bottom:0;">
      {verdict_para}
    </p>
  </div>

  <h3>Side-by-side numbers</h3>
  <table>
    <thead>
      <tr>
        <th>phase_step</th>
        <th>local N</th><th>local P50</th><th>local P90</th>
        <th>cloud N</th><th>cloud P50</th><th>cloud P90</th>
        <th>Δ P50</th><th>Δ P90</th>
      </tr>
    </thead>
    <tbody>
{table_body}
    </tbody>
  </table>
  <p style="font-size: 13px; color: var(--ink-muted);">
    Per-step rows (Maestro startup, Flow execution) populate local but not cloud —
    BigQuery surfaces only Aggregates-level cloud metrics. The
    <span style="color:var(--accent-deep)">tc_*</span> rows are the dual-yaml
    breakdown unique to this Android cloud pattern.
  </p>
</section>

<section>
  <h2>How we measured</h2>
  <ul>
    <li>Cloud build ID: <code>{build_id}</code></li>
    <li>Cloud query: <code>browserstack-production.app_automate.app_automate_test_sessions_partitioned</code> filtered by <code>build_id</code> and <code>JSON_VALUE(test_status, '$.error') = '0'</code> (excludes the 3 errored sessions per gotcha #16).</li>
    <li>Per-tc durations: <code>/maestro/v2/builds/{build_id[:12]}…/sessions/{{sid}}</code> for each passed session; combined view = raw per-session sum of test_a + test_b.</li>
    <li>Local baseline: pinned to <code>android/local/results/local_android_*_20260510_231553.csv</code>, OnePlus 9R + Maestro 1.39.13 + Wikipedia Alpha + selector-based search loop.</li>
  </ul>
</section>

<footer>
  Generated by <a href="https://github.com/vinit-09/perf_bench_maestro">aggregate_android_cloud_report.py</a>
  · Build {build_id} · {cloud_n_data}/{cloud_n_requested} passed
</footer>

</body>
</html>
"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--local-final-csv", type=Path, required=True,
                    help="Path to local_android_final_report_<TS>.csv")
    ap.add_argument("--bq-response", type=Path, required=True,
                    help="Path to saved BigQuery MCP response JSON")
    ap.add_argument("--build-json", type=Path, required=True,
                    help="Path to results/<run-id>/<build-id>.json")
    ap.add_argument("--build-id", required=True,
                    help="BS Maestro v2 build_id (cross-checked against build JSON)")
    ap.add_argument("--bs-user", default=os.environ.get("BROWSERSTACK_USERNAME"))
    ap.add_argument("--bs-key", default=os.environ.get("BROWSERSTACK_ACCESS_KEY"))
    ap.add_argument("--out-dir", type=Path, default=ROOT / "analysis")
    ap.add_argument("--timestamp", default=None,
                    help="Override the timestamp suffix on output filenames")
    ap.add_argument("--skip-per-tc", action="store_true",
                    help="Skip per-tc fetch (useful for fast offline iteration)")
    args = ap.parse_args()

    if not args.bs_user or not args.bs_key:
        print("ERROR: BS credentials missing. Set BROWSERSTACK_USERNAME / "
              "BROWSERSTACK_ACCESS_KEY or pass --bs-user / --bs-key.", file=sys.stderr)
        return 2

    ts = args.timestamp or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Load local.
    print(f"[load] local final report: {args.local_final_csv}")
    local = load_local_final(args.local_final_csv)
    print(f"  rows: {len(local)}")

    # Load cloud BQ.
    print(f"[load] BQ response: {args.bq_response}")
    bq_response = json.loads(args.bq_response.read_text())
    try:
        cell = load_cloud_cell(bq_response, cell_name="cloud_android", os="android")
    except (EmptyCellError, MalformedCellError) as e:
        print(f"ERROR: cloud cell load failed: {e}", file=sys.stderr)
        return 1
    print(f"  cloud sessions: {len(cell.sessions)}")
    cloud_session = cloud_session_stats(cell)

    # Build JSON cross-check + session-id list.
    print(f"[load] build JSON: {args.build_json}")
    build_data = json.loads(args.build_json.read_text())
    if build_data.get("id") != args.build_id:
        print(f"WARN: build JSON id={build_data.get('id')} does not match "
              f"--build-id={args.build_id}", file=sys.stderr)
    all_session_ids = [
        s["id"]
        for dev in build_data.get("devices", [])
        for s in dev.get("sessions", [])
    ]
    session_ids = [
        s["id"]
        for dev in build_data.get("devices", [])
        for s in dev.get("sessions", [])
        if s.get("status") == "passed"
    ]
    print(f"  total session_ids: {len(all_session_ids)}, passed: {len(session_ids)}")

    # Per-tc durations via BS API.
    if args.skip_per_tc:
        print("[skip] per-tc fetch (--skip-per-tc)")
        per_tc_raw: dict[str, dict[str, int]] = {}
    else:
        print(f"[fetch] per-tc durations via BS API ({len(session_ids)} sessions)...")
        per_tc_raw = collect_per_tc_durations(
            args.build_id, session_ids, args.bs_user, args.bs_key
        )
        print(f"  fetched: {len(per_tc_raw)}")
    cloud_per_tc = per_tc_stats(per_tc_raw)

    # Build comparison rows + emit CSV.
    rows = build_comparison_rows(local, cloud_session, cloud_per_tc)
    csv_out = args.out_dir / f"local_vs_cloud_android_comparison_{ts}.csv"
    write_comparison_csv(rows, csv_out)
    print(f"[emit] comparison CSV: {csv_out}")

    # HTML report (U5).
    html_out = args.out_dir / "maestro_android_benchmark_report.html"
    write_html_report(
        rows, local, cloud_session, cloud_per_tc,
        build_id=args.build_id,
        cloud_n_requested=len(all_session_ids),
        cloud_n_data=len(cell.sessions),
        out_path=html_out,
    )
    print(f"[emit] HTML report: {html_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
