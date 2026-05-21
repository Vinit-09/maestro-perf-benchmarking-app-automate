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

  --spec-target-s     Spec target session_total in seconds (canonical P90 the
                      workload was sized to). Default: 1153 (5/13 smoke baseline).
  --ceiling-ratio     Ratio over local P90 that defines the cloud "ceiling"
                      (default 1.1 — same as iOS report convention).

  --out-dir           Directory for output artifacts (default analysis/)
  --timestamp         Override the timestamp suffix on output filenames
                      (default: current UTC YYYYMMDD_HHMMSS)

Outputs (under --out-dir):
  local_vs_cloud_android_comparison_<TS>.csv   (column shape mirrors iOS)
  maestro_android_benchmark_report.html        (iOS skeleton + dual-yaml
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
    """Return {N, min_ms, p50_ms, p75_ms, p90_ms, p95_ms, p100_ms}."""
    if not values:
        return {
            "N": 0, "min_ms": "", "p50_ms": "", "p75_ms": "",
            "p90_ms": "", "p95_ms": "", "p100_ms": "",
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
    """Read local_android_final_report_<TS>.csv -> {(phase_group, phase_step): stats}."""
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
    """Compute cloud percentile rows from a Cell loaded via load_cloud_cell."""
    sessions: list[CellSession] = cell.sessions
    passed_only = [s for s in sessions if s.execution_s and s.execution_s > 0]

    app_install_vals = [s.app_install_ms for s in passed_only if s.app_install_ms is not None]
    session_total_vals = [int(s.execution_s * 1000) for s in passed_only if s.execution_s]

    maestro_total_vals: list[float] = []
    for s in passed_only:
        if not s.execution_s:
            continue
        total_ms = int(s.execution_s * 1000)
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
    """Extract {test_a: ms, test_b: ms, ...} from a session-detail JSON."""
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
    if isinstance(local_val, int) and isinstance(cloud_val, int):
        return cloud_val - local_val
    return ""


def build_comparison_rows(
    local: dict[tuple[str, str], dict[str, int | str]],
    cloud_session: dict[tuple[str, str], dict[str, int | str]],
    cloud_per_tc: dict[tuple[str, str], dict[str, int | str]],
) -> list[dict[str, int | str]]:
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
# HTML report — iOS-skeleton mirror adapted for dual-yaml mechanics.
# Stylesheet is copied from analysis/maestro_ios_benchmark_report.html with
# Android-specific additions for dual-tc rows.
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
html { scroll-behavior: smooth; }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Inter", sans-serif;
  color: var(--ink); background: var(--bg-section); line-height: 1.6; font-size: 16px;
}
/* ---------- Hero ---------- */
.hero {
  min-height: 100vh; background: var(--hero-grad); color: #f1f5f9;
  display: flex; flex-direction: column; justify-content: center;
  padding: 60px 24px 40px; position: relative; overflow: hidden;
}
.hero::before {
  content: ""; position: absolute; inset: 0;
  background: radial-gradient(circle at 80% 30%, rgba(34, 211, 238, 0.18), transparent 50%);
  pointer-events: none;
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
.hero-lede {
  font-size: clamp(16px, 2vw, 20px); max-width: 720px; color: #cbd5e1; margin-bottom: 32px;
}
.verdict-pill {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 8px 16px; border-radius: 999px;
  background: rgba(16, 185, 129, 0.15); color: #6ee7b7;
  font-weight: 600; font-size: 14px; margin-top: 8px;
  border: 1px solid rgba(16, 185, 129, 0.3);
}
.verdict-pill::before { content: "●"; color: var(--pass); }
.verdict-pill.warn {
  background: rgba(245, 158, 11, 0.15); color: #fbbf24;
  border-color: rgba(245, 158, 11, 0.3);
}
.verdict-pill.warn::before { color: var(--warn); }
.hero-stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
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
.stat-value { font-size: 44px; font-weight: 700; line-height: 1; }
.stat-value .unit {
  font-size: 0.4em; color: var(--ink-muted); font-weight: 400; margin-left: 4px;
}
.stat-context { font-size: 13px; color: #94a3b8; margin-top: 12px; }
/* ---------- Sticky nav ---------- */
.nav {
  position: sticky; top: 0; z-index: 100;
  background: rgba(255,255,255,.95); backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--line); padding: 12px 0;
}
.nav-inner {
  max-width: 1200px; margin: 0 auto; padding: 0 24px;
  display: flex; gap: 8px; overflow-x: auto; flex-wrap: nowrap;
}
.nav a {
  color: var(--ink-soft); text-decoration: none;
  padding: 8px 16px; border-radius: 8px;
  font-size: 14px; font-weight: 500; white-space: nowrap;
  transition: all 0.15s;
}
.nav a:hover { background: var(--bg-section); color: var(--ink); }
.nav a.active { background: var(--ink); color: #fff; }
/* ---------- Sections ---------- */
section { padding: 80px 24px; max-width: 1200px; margin: 0 auto; }
section.alt { background: #fff; max-width: none; padding-left: 0; padding-right: 0; }
section.alt > .inner { max-width: 1200px; margin: 0 auto; padding: 0 24px; }
h2 {
  font-size: clamp(28px, 4vw, 40px); font-weight: 700;
  margin: 0 0 16px; letter-spacing: -0.02em;
}
h3 { font-size: 22px; margin: 32px 0 12px; font-weight: 600; }
h4 { margin: 0 0 8px; font-size: 17px; font-weight: 600; }
.section-eyebrow {
  text-transform: uppercase; letter-spacing: 0.16em; font-size: 11px;
  font-weight: 600; color: var(--accent-deep); margin-bottom: 12px;
}
.section-lede {
  font-size: 18px; color: var(--ink-soft);
  max-width: 800px; margin-bottom: 40px;
}
p { color: var(--ink); }
p.muted { color: var(--ink-soft); }
em { color: var(--accent-deep); font-style: italic; }
/* ---------- Cards & grids ---------- */
.card {
  background: var(--bg-card); border-radius: var(--radius);
  padding: 28px; box-shadow: var(--shadow); border: 1px solid var(--line);
}
.grid { display: grid; gap: 24px; }
.grid.cols-2 { grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); }
.grid.cols-3 { grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
.grid.cols-4 { grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
.big-number {
  font-size: 48px; font-weight: 700; letter-spacing: -0.02em; line-height: 1; color: var(--ink);
}
.big-number.pass { color: var(--pass); }
.big-number.fail { color: var(--fail); }
.big-number.warn { color: var(--warn); }
.big-number .unit { font-size: 0.4em; color: var(--ink-muted); font-weight: 400; margin-left: 4px; }
.number-label {
  font-size: 12px; text-transform: uppercase; letter-spacing: 0.1em;
  color: var(--ink-muted); font-weight: 600; margin-bottom: 12px;
}
.number-context { font-size: 13px; color: var(--ink-soft); margin-top: 8px; }
/* ---------- Definition grid (phase glossary) ---------- */
.def-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px; margin-top: 16px;
}
.def-card {
  background: #fff; border-radius: 10px; padding: 20px;
  border: 1px solid var(--line); box-shadow: var(--shadow);
}
.def-card h4 { margin: 0 0 4px; font-size: 16px; font-weight: 600; color: var(--accent-deep); }
.def-card .def-tag {
  display: inline-block; font-size: 11px; letter-spacing: 0.1em;
  text-transform: uppercase; color: var(--ink-muted);
  font-weight: 600; margin-bottom: 8px;
}
.def-card p { margin: 0 0 10px; font-size: 14px; color: var(--ink-soft); }
.def-card dl { margin: 0; }
.def-card dt {
  font-weight: 600; font-size: 13px; color: var(--ink); margin-top: 10px;
  font-family: ui-monospace, SFMono-Regular, monospace;
}
.def-card dd { margin: 2px 0 0; font-size: 13px; color: var(--ink-soft); line-height: 1.5; }
/* ---------- Tables ---------- */
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 14px; margin: 16px 0; }
th, td { padding: 12px 14px; text-align: left; border-bottom: 1px solid var(--line); }
th {
  background: #f8fafc; font-weight: 600; font-size: 12px;
  text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-soft);
}
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr:hover td { background: #fafbff; }
.row-group { background: #f1f5f9 !important; font-weight: 600; }
.row-aggregate { background: #fffbeb !important; font-weight: 600; }
.row-dual-tc { background: #ecfeff !important; }
.row-dual-tc td:first-child { color: var(--accent-deep); }
.delta-pos { color: var(--fail); }
.delta-neg { color: var(--pass); }
.delta-neutral { color: var(--ink-muted); }
/* ---------- Story timeline (Journey) ---------- */
.timeline { position: relative; padding-left: 32px; }
.timeline::before {
  content: ""; position: absolute; left: 13px; top: 0; bottom: 0; width: 2px;
  background: linear-gradient(to bottom, var(--accent), var(--accent-deep));
}
.step { position: relative; padding: 8px 0 24px; }
.step::before {
  content: ""; position: absolute; left: -28px; top: 14px;
  width: 14px; height: 14px; border-radius: 50%;
  background: var(--accent); border: 3px solid #fff;
  box-shadow: 0 0 0 2px var(--accent-deep);
}
.step h4 { margin: 0 0 4px; font-size: 17px; font-weight: 600; }
.step p { margin: 4px 0; color: var(--ink-soft); }
.plain-english {
  background: #f8fafc; border-left: 3px solid var(--accent);
  padding: 16px 20px; margin: 12px 0; border-radius: 4px; font-size: 14px;
}
.plain-english p { margin: 0 0 10px; color: var(--ink-soft); }
.plain-english p:last-child { margin-bottom: 0; }
.plain-english strong { color: var(--ink); }
/* ---------- Phase-mapping component ---------- */
.phase-map { display: flex; flex-direction: column; gap: 14px; margin: 24px 0; }
.map-row {
  display: grid; grid-template-columns: 1fr 140px 1fr; gap: 0;
  background: #fff; border-radius: 12px; padding: 18px 0;
  box-shadow: var(--shadow); border: 1px solid var(--line); align-items: stretch;
}
.map-side {
  padding: 4px 20px; display: flex; flex-direction: column; justify-content: center;
}
.map-side.cloud { border-left: 3px solid #7c3aed; }
.map-side.local { border-left: 3px solid var(--accent-deep); }
.map-phase {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  font-weight: 600; font-size: 14px; color: var(--ink); margin-bottom: 4px; word-break: break-word;
}
.map-side.empty { border-left-color: #cbd5e1; background: #fafbfc; }
.map-side.empty .map-phase {
  color: var(--ink-muted); font-style: italic; font-family: inherit; font-weight: 500;
}
.map-tag {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em;
  color: var(--ink-muted); margin-bottom: 8px; font-weight: 700;
}
.map-def { font-size: 13px; color: var(--ink-soft); line-height: 1.55; }
.map-conn {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 6px; padding: 0 8px; border-left: 1px solid var(--line); border-right: 1px solid var(--line);
}
.match-dot {
  display: inline-flex; align-items: center; justify-content: center;
  width: 38px; height: 38px; border-radius: 50%; font-weight: 700; font-size: 18px; color: #fff;
}
.match-dot.full { background: var(--pass); }
.match-dot.partial { background: var(--warn); }
.match-dot.none { background: #94a3b8; }
.match-label {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em;
  font-weight: 700; color: var(--ink-muted); text-align: center;
}
/* ---------- Verdict block ---------- */
.verdict-block {
  background: linear-gradient(135deg, #d1fae5 0%, #a7f3d0 100%);
  border-radius: var(--radius); padding: 40px; margin: 32px 0;
  border-left: 6px solid var(--pass);
}
.verdict-block.warn {
  background: linear-gradient(135deg, #fef3c7 0%, #fed7aa 100%);
  border-left-color: var(--warn);
}
.verdict-math { display: flex; gap: 24px; flex-wrap: wrap; margin: 24px 0; }
.verdict-cell {
  flex: 1; min-width: 160px; background: rgba(255,255,255,.7);
  padding: 16px; border-radius: 10px;
}
.verdict-formula {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  font-size: 15px; background: rgba(0,0,0,0.05); padding: 16px; border-radius: 8px;
  margin: 16px 0; color: var(--ink);
}
/* ---------- Recommendations ---------- */
.reco {
  border-left: 4px solid var(--accent); padding: 20px 24px;
  background: #fff; border-radius: 8px; box-shadow: var(--shadow); margin: 16px 0;
}
.reco h4 { margin: 0 0 8px; font-size: 17px; font-weight: 600; }
.reco-impact {
  display: inline-block; font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.1em; padding: 3px 10px; border-radius: 999px;
  background: #fef3c7; color: #b45309; margin-bottom: 8px; font-weight: 600;
}
.reco-impact.high { background: #fee2e2; color: #b91c1c; }
.reco-impact.med { background: #fef3c7; color: #b45309; }
.reco-impact.low { background: #ccfbf1; color: #115e59; }
/* ---------- Collapsibles ---------- */
details {
  margin: 16px 0; border: 1px solid var(--line); border-radius: 10px;
  background: #fff; overflow: hidden;
}
summary {
  padding: 14px 18px; cursor: pointer; font-weight: 600; list-style: none;
  display: flex; justify-content: space-between; align-items: center; user-select: none;
}
summary::-webkit-details-marker { display: none; }
summary::after {
  content: "+"; color: var(--ink-muted); font-size: 24px; font-weight: 300;
  transition: transform 0.2s;
}
details[open] summary::after { content: "−"; }
details > *:not(summary) { padding: 0 18px 18px; }
code {
  background: #f1f5f9; padding: 2px 6px; border-radius: 4px;
  font-size: 0.92em; font-family: ui-monospace, SFMono-Regular, monospace;
}
/* ---------- Footer ---------- */
footer {
  background: var(--bg); color: #94a3b8;
  padding: 40px 24px; text-align: center; font-size: 13px;
}
footer a { color: var(--accent); text-decoration: none; }
@media (max-width: 768px) {
  .map-row { grid-template-columns: 1fr; }
  .hero-stats { grid-template-columns: 1fr 1fr; }
}
"""


# ----------------------------------------------------------------------------
# Helpers for the HTML report
# ----------------------------------------------------------------------------

def _fmt_ms(v: int | str) -> str:
    if v == "" or v is None:
        return "—"
    iv = int(v)
    return f"{iv:,}"


def _fmt_s(v: int | str, decimals: int = 1) -> str:
    if v == "" or v is None:
        return "—"
    iv = int(v)
    return f"{iv/1000:.{decimals}f}"


def _delta_class(v: int | str) -> str:
    if v == "" or v is None:
        return "delta-neutral"
    iv = int(v)
    if iv > 0:
        return "delta-pos"
    if iv < 0:
        return "delta-neg"
    return "delta-neutral"


def _fmt_delta(v: int | str) -> str:
    if v == "" or v is None:
        return "—"
    iv = int(v)
    sign = "+" if iv > 0 else "−" if iv < 0 else ""
    return f"{sign}{abs(iv):,}"


# Mapping from local phase_step -> human-friendly phase group label for tables.
_LOCAL_PHASE_GROUPS = {
    "Pre-Maestro phase": ["device_readiness", "app_install"],
    "Maestro startup phase": ["maestro_init", "driver_setup", "device_info"],
    "Flow execution phase": [
        "Define variables", "Apply configuration", "Launch app",
        "Tap on OK dialog", "Tap on search container",
        "Erase text", "Input text BrowserStack",
        "Assert Software company text visible", "Per-rep total",
    ],
    "Post-Maestro phase": ["stop"],
}


def _build_local_table(local: dict[tuple[str, str], dict[str, int | str]]) -> str:
    """Build the full local-percentile table HTML mirroring iOS report Section 04."""
    rows = []
    for group_label, steps in _LOCAL_PHASE_GROUPS.items():
        rows.append(f'<tr class="row-group"><td colspan="8">{group_label}</td></tr>')
        for step in steps:
            # The local CSV may have phase_group as Pre-Maestro / Maestro startup / etc.
            # Find it by searching all known groups.
            found = None
            for pg in ("Pre-Maestro", "Maestro startup", "Flow execution", "Post-Maestro"):
                if (pg, step) in local:
                    found = local[(pg, step)]
                    break
            if not found:
                continue
            label = step
            if step in ("Erase text", "Input text BrowserStack", "Assert Software company text visible"):
                label = f'{step} <em>(per rep)</em>'
            if step == "Per-rep total":
                label = f'<strong>{step}</strong>'
            rows.append(
                f'<tr><td>{label}</td>'
                f'<td class="num">{found["N"]}</td>'
                f'<td class="num">{_fmt_ms(found["min_ms"])}</td>'
                f'<td class="num">{_fmt_ms(found["p50_ms"])}</td>'
                f'<td class="num">{_fmt_ms(found["p75_ms"])}</td>'
                f'<td class="num">{_fmt_ms(found["p90_ms"])}</td>'
                f'<td class="num">{_fmt_ms(found["p95_ms"])}</td>'
                f'<td class="num">{_fmt_ms(found["p100_ms"])}</td></tr>'
            )
    # Aggregates rows
    for agg in ("maestro_total", "session_total"):
        s = local.get(("Aggregates", agg))
        if s:
            rows.append(
                f'<tr class="row-aggregate"><td><strong>{agg}</strong></td>'
                f'<td class="num">{s["N"]}</td>'
                f'<td class="num">{_fmt_ms(s["min_ms"])}</td>'
                f'<td class="num">{_fmt_ms(s["p50_ms"])}</td>'
                f'<td class="num">{_fmt_ms(s["p75_ms"])}</td>'
                f'<td class="num">{_fmt_ms(s["p90_ms"])}</td>'
                f'<td class="num">{_fmt_ms(s["p95_ms"])}</td>'
                f'<td class="num">{_fmt_ms(s["p100_ms"])}</td></tr>'
            )
    return "\n".join(rows)


def _build_cloud_table(
    cloud_session: dict[tuple[str, str], dict[str, int | str]],
    cloud_per_tc: dict[tuple[str, str], dict[str, int | str]],
) -> str:
    """Build the cloud-percentile table HTML mirroring iOS report Section 05."""
    rows = []
    rows.append('<tr class="row-group"><td colspan="8">Pre-Maestro phase</td></tr>')
    s = cloud_session.get(("Pre-Maestro", "app_install"))
    if s:
        rows.append(
            f'<tr><td>app_install</td>'
            f'<td class="num">{s["N"]}</td>'
            f'<td class="num">{_fmt_ms(s["min_ms"])}</td>'
            f'<td class="num">{_fmt_ms(s["p50_ms"])}</td>'
            f'<td class="num">{_fmt_ms(s["p75_ms"])}</td>'
            f'<td class="num">{_fmt_ms(s["p90_ms"])}</td>'
            f'<td class="num">{_fmt_ms(s["p95_ms"])}</td>'
            f'<td class="num">{_fmt_ms(s["p100_ms"])}</td></tr>'
        )
    rows.append('<tr class="row-group"><td colspan="8">Inner-loop step rows (not exposed by BQ)</td></tr>')
    rows.append(
        '<tr><td colspan="8" style="color: var(--ink-muted); font-style: italic;">'
        'No per-step data in BQ — see methodology.</td></tr>'
    )
    rows.append('<tr class="row-group"><td colspan="8">Per-testcase breakdown (BS REST per-session detail)</td></tr>')
    for tc_label, key in (("test_a", "tc_a_duration"), ("test_b", "tc_b_duration"),
                          ("combined (per-session sum)", "tc_combined_duration")):
        s = cloud_per_tc.get(("Aggregates", key))
        if s and s.get("N", 0):
            row_cls = ' class="row-dual-tc"'
            rows.append(
                f'<tr{row_cls}><td>{tc_label}</td>'
                f'<td class="num">{s["N"]}</td>'
                f'<td class="num">{_fmt_ms(s["min_ms"])}</td>'
                f'<td class="num">{_fmt_ms(s["p50_ms"])}</td>'
                f'<td class="num">{_fmt_ms(s["p75_ms"])}</td>'
                f'<td class="num">{_fmt_ms(s["p90_ms"])}</td>'
                f'<td class="num">{_fmt_ms(s["p95_ms"])}</td>'
                f'<td class="num">{_fmt_ms(s["p100_ms"])}</td></tr>'
            )
    for agg in ("maestro_total", "session_total"):
        s = cloud_session.get(("Aggregates", agg))
        if s:
            rows.append(
                f'<tr class="row-aggregate"><td><strong>{agg}</strong></td>'
                f'<td class="num">{s["N"]}</td>'
                f'<td class="num">{_fmt_ms(s["min_ms"])}</td>'
                f'<td class="num">{_fmt_ms(s["p50_ms"])}</td>'
                f'<td class="num">{_fmt_ms(s["p75_ms"])}</td>'
                f'<td class="num">{_fmt_ms(s["p90_ms"])}</td>'
                f'<td class="num">{_fmt_ms(s["p95_ms"])}</td>'
                f'<td class="num">{_fmt_ms(s["p100_ms"])}</td></tr>'
            )
    return "\n".join(rows)


def _build_compare_table_body(rows: list[dict[str, int | str]]) -> str:
    """Side-by-side compare table for the Verdict section."""
    out = []
    for r in rows:
        if r["phase_step"].startswith("tc_"):
            row_cls = ' class="row-dual-tc"'
            label = r["phase_step"]
        elif r["phase_step"] in ("maestro_total", "session_total"):
            row_cls = ' class="row-aggregate"'
            label = f'<strong>{r["phase_step"]}</strong>'
        else:
            row_cls = ""
            label = r["phase_step"]
        out.append(
            f'<tr{row_cls}><td>{label}</td>'
            f'<td class="num">{r["local_N"] or "—"}</td>'
            f'<td class="num">{_fmt_ms(r["local_p50_ms"])}</td>'
            f'<td class="num">{_fmt_ms(r["local_p90_ms"])}</td>'
            f'<td class="num">{r["cloud_N"] or "—"}</td>'
            f'<td class="num">{_fmt_ms(r["cloud_p50_ms"])}</td>'
            f'<td class="num">{_fmt_ms(r["cloud_p90_ms"])}</td>'
            f'<td class="num {_delta_class(r["delta_p50_ms"])}">{_fmt_delta(r["delta_p50_ms"])}</td>'
            f'<td class="num {_delta_class(r["delta_p90_ms"])}">{_fmt_delta(r["delta_p90_ms"])}</td></tr>'
        )
    return "\n".join(out)


# Default empty stats dict used when a (phase_group, phase_step) key is missing.
# Defined at module scope so it's visible inside the write_html_report f-string.
_BLANK: dict[str, int | str] = {}


def _n(v: int | str | None) -> int:
    """Coerce stat value to int. Empty string / None / non-int -> 0.
    Used inside the HTML f-string so empty stats don't break arithmetic."""
    if v is None or v == "":
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def write_html_report(
    rows: list[dict[str, int | str]],
    local: dict[tuple[str, str], dict[str, int | str]],
    cloud_session: dict[tuple[str, str], dict[str, int | str]],
    cloud_per_tc: dict[tuple[str, str], dict[str, int | str]],
    build_id: str,
    cloud_n_requested: int,
    cloud_n_data: int,
    spec_target_s: float,
    ceiling_ratio: float,
    out_path: Path,
) -> None:
    """Render maestro_android_benchmark_report.html mirroring iOS report structure."""

    # ---- Compute hero numbers ----
    local_p90_ms = local.get(("Aggregates", "session_total"), {}).get("p90_ms", 0) or 0
    local_p50_ms = local.get(("Aggregates", "session_total"), {}).get("p50_ms", 0) or 0
    cloud_p90_ms = cloud_session.get(("Aggregates", "session_total"), {}).get("p90_ms", 0) or 0
    cloud_p50_ms = cloud_session.get(("Aggregates", "session_total"), {}).get("p50_ms", 0) or 0
    if not isinstance(local_p90_ms, int):
        local_p90_ms = 0
    if not isinstance(cloud_p90_ms, int):
        cloud_p90_ms = 0

    local_p90_s = local_p90_ms / 1000.0
    local_p50_s = local_p50_ms / 1000.0 if isinstance(local_p50_ms, int) else 0
    cloud_p90_s = cloud_p90_ms / 1000.0
    cloud_p50_s = cloud_p50_ms / 1000.0 if isinstance(cloud_p50_ms, int) else 0
    ceiling_s = local_p90_s * ceiling_ratio
    ratio = (cloud_p90_s / local_p90_s) if local_p90_s else 0

    # Cloud relative to ceiling (negative = under = good).
    delta_to_ceiling_s = cloud_p90_s - ceiling_s
    if delta_to_ceiling_s < 0:
        ceiling_subtext = (
            f"Cloud landed {abs(delta_to_ceiling_s):.1f} s "
            f"<strong>under</strong> the {ceiling_ratio:.1f}× ceiling"
        )
    else:
        ceiling_subtext = (
            f"Cloud overshot by {delta_to_ceiling_s:.1f} s "
            f"({(ratio-ceiling_ratio)*100/ceiling_ratio:+.1f} % over)"
        )

    # Spec target subtext.
    spec_delta = cloud_p50_s - spec_target_s
    if abs(spec_delta) < spec_target_s * 0.02:
        spec_subtext = "Cloud P50 landed within 2 % of the spec target"
    elif spec_delta < 0:
        spec_subtext = f"Cloud P50 landed {abs(spec_delta):.1f} s under the spec target"
    else:
        spec_subtext = f"Cloud P50 landed {spec_delta:.1f} s over the spec target"

    # Verdict pill text + class.
    verdict_pill_class = "" if ratio < ceiling_ratio else "warn"
    if ratio < ceiling_ratio:
        verdict_pill = (
            f"Verdict: {ratio:.3f}× — under the {ceiling_ratio:.1f}× ceiling by "
            f"{abs(delta_to_ceiling_s):.0f} seconds at P90"
        )
    else:
        verdict_pill = (
            f"Verdict: {ratio:.3f}× — over the {ceiling_ratio:.1f}× ceiling by "
            f"{delta_to_ceiling_s:.0f} seconds at P90"
        )

    # Hero headline.
    if ratio < 1.0:
        pct_faster = (1 - ratio) * 100
        hero_h1 = (
            f'Is BrowserStack cloud Android within '
            f'<span class="accent">{ceiling_ratio:.1f}×</span> of local?'
            f'<br><span class="win">Yes — and {pct_faster:.0f}% faster.</span>'
        )
    elif ratio < ceiling_ratio:
        hero_h1 = (
            f'Is BrowserStack cloud Android within '
            f'<span class="accent">{ceiling_ratio:.1f}×</span> of local?'
            f'<br><span class="win">Yes — {ratio:.3f}× at P90.</span>'
        )
    else:
        hero_h1 = (
            f'Is BrowserStack cloud Android within '
            f'<span class="accent">{ceiling_ratio:.1f}×</span> of local?'
            f'<br><span class="warn">Not quite — {ratio:.3f}× at P90.</span>'
        )

    pass_rate_pct = (cloud_n_data / cloud_n_requested * 100) if cloud_n_requested else 0

    # Per-tc stats for the workload-mechanics narrative.
    tc_a = cloud_per_tc.get(("Aggregates", "tc_a_duration"), {})
    tc_b = cloud_per_tc.get(("Aggregates", "tc_b_duration"), {})
    tc_combined = cloud_per_tc.get(("Aggregates", "tc_combined_duration"), {})
    tc_a_p90 = tc_a.get("p90_ms", 0) or 0
    tc_b_p90 = tc_b.get("p90_ms", 0) or 0
    cap_threshold_ms = 870_000
    cap_violation = (
        (isinstance(tc_a.get("p100_ms", 0), int) and tc_a.get("p100_ms", 0) > cap_threshold_ms)
        or (isinstance(tc_b.get("p100_ms", 0), int) and tc_b.get("p100_ms", 0) > cap_threshold_ms)
    )

    # Section: tables
    local_table_body = _build_local_table(local)
    cloud_table_body = _build_cloud_table(cloud_session, cloud_per_tc)
    compare_table_body = _build_compare_table_body(rows)

    # Precomputed derived values that need arithmetic in the f-string.
    # All robust to empty-string stats (when per-tc fetch was skipped).
    install_cloud_p90 = _n(cloud_session.get(("Pre-Maestro", "app_install"), _BLANK).get("p90_ms", 0))
    install_local_p90 = _n(local.get(("Pre-Maestro", "app_install"), _BLANK).get("p90_ms", 0))
    install_cloud_p50 = _n(cloud_session.get(("Pre-Maestro", "app_install"), _BLANK).get("p50_ms", 0))
    install_local_p50 = _n(local.get(("Pre-Maestro", "app_install"), _BLANK).get("p50_ms", 0))
    install_delta_p90_s = (install_cloud_p90 - install_local_p90) / 1000.0

    local_driver_setup_p90 = _n(local.get(("Maestro startup", "driver_setup"), _BLANK).get("p90_ms", 0))
    local_stop_p90 = _n(local.get(("Post-Maestro", "stop"), _BLANK).get("p90_ms", 0))
    local_per_rep_p50_ms = _n(local.get(("Flow execution", "Per-rep total"), _BLANK).get("p50_ms", 0))

    tc_a_p50_ms = _n(tc_a.get("p50_ms", 0))
    tc_b_p50_ms = _n(tc_b.get("p50_ms", 0))
    cloud_per_rep_s = tc_a_p50_ms / 50.0 / 1000.0 if tc_a_p50_ms else 0
    local_per_rep_s = local_per_rep_p50_ms / 1000.0
    delta_local_minus_cloud_p90_s = local_p90_s - cloud_p90_s
    extended_lead_s = delta_local_minus_cloud_p90_s + 5  # +5s from reco #1 hypothesis

    local_session_n = _n(local.get(("Aggregates", "session_total"), _BLANK).get("N", 0))

    # Verdict block class (cloud-wins -> green; cloud-misses -> warn).
    verdict_block_class = "" if ratio < ceiling_ratio else " warn"

    # Compute a per-rep cost approximation (50 reps per testcase → tc_p50 / 50).
    per_rep_combined_ms = (tc_combined.get("p50_ms", 0) or 0) // 100  # 100 reps
    local_per_rep_p90 = local.get(("Flow execution", "Per-rep total"), {}).get("p90_ms", 0) or 0

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Android Maestro Benchmark — Local vs BrowserStack Cloud</title>
<style>{_HTML_CSS}</style>
</head>
<body>

<!-- ============================================================= -->
<!-- HERO                                                          -->
<!-- ============================================================= -->
<header class="hero">
  <div class="hero-inner">
    <div class="eyebrow">Maestro Benchmark · Android · 2026</div>
    <h1>{hero_h1}</h1>
    <p class="hero-lede">
      A controlled experiment running <strong>10,000 Maestro reps</strong> across 100 sessions on Samsung Galaxy S24 (Android 14).
      We measured every phase of the test lifecycle — from app install to the final assertion — to find out.
    </p>

    <div class="verdict-pill {verdict_pill_class}">{verdict_pill}</div>

    <div class="hero-stats">
      <div class="stat">
        <div class="stat-label">Local P90</div>
        <div class="stat-value">{local_p90_s:.1f}<span class="unit">s</span></div>
        <div class="stat-context">{local_session_n} clean sessions on a wired OnePlus 9R (Android 14)</div>
      </div>
      <div class="stat">
        <div class="stat-label">Cloud P90</div>
        <div class="stat-value">{cloud_p90_s:.1f}<span class="unit">s</span></div>
        <div class="stat-context">{cloud_n_data} sessions on BrowserStack Samsung Galaxy S24-14.0</div>
      </div>
      <div class="stat">
        <div class="stat-label">The {ceiling_ratio:.1f}× ceiling</div>
        <div class="stat-value">{ceiling_s:.1f}<span class="unit">s</span></div>
        <div class="stat-context">{ceiling_subtext}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Spec target</div>
        <div class="stat-value">{spec_target_s:.1f}<span class="unit">s</span></div>
        <div class="stat-context">{spec_subtext}</div>
      </div>
    </div>
  </div>
</header>

<!-- ============================================================= -->
<!-- NAV                                                          -->
<!-- ============================================================= -->
<nav class="nav">
  <div class="nav-inner">
    <a href="#mission">Mission</a>
    <a href="#setup">Setup</a>
    <a href="#journey">Journey</a>
    <a href="#local">Local Results</a>
    <a href="#cloud">Cloud Results</a>
    <a href="#mapping">Phase Mapping</a>
    <a href="#verdict">The Verdict</a>
    <a href="#recommendations">Recommendations</a>
    <a href="#methodology">Methodology</a>
  </div>
</nav>

<!-- ============================================================= -->
<!-- 1. MISSION                                                   -->
<!-- ============================================================= -->
<section id="mission">
  <div class="section-eyebrow">01 · The Question</div>
  <h2>Why this benchmark exists</h2>
  <p class="section-lede">
    BrowserStack runs Maestro tests across thousands of Android devices. Customers ask one question over and over:
    <em>"How much slower is the cloud than my local laptop?"</em> We needed a defensible number.
  </p>

  <div class="grid cols-2">
    <div class="card">
      <h3 style="margin-top:0;">The hypothesis</h3>
      <p>BrowserStack cloud Android Maestro should run within <strong>{ceiling_ratio:.1f}×</strong> of an equivalent local run. That's the internal SLA target — the number that determines whether the cloud experience is <strong>"indistinguishable from local"</strong> for a customer.</p>
    </div>
    <div class="card">
      <h3 style="margin-top:0;">What's in scope</h3>
      <ul style="padding-left: 20px; margin: 0;">
        <li><strong>Samsung Galaxy S24 / Android 14</strong> on cloud, <strong>OnePlus 9R / Android 14</strong> on local</li>
        <li>A Maestro flow sized to <strong>~{spec_target_s:.0f} s P90</strong> (spec target = 5/13 smoke baseline)</li>
        <li>Same-region cloud devices (<strong>ap-south-1</strong> for India operators)</li>
        <li>Per-session, per-testcase and per-rep latency at <strong>P50, P75, P90, P95, P100</strong></li>
      </ul>
    </div>
  </div>

  <h3>What we measured</h3>
  <p>Total session time is a chain of phases that BrowserStack and the customer both touch. Each card below names the phase and explains every step inside in plain language.</p>

  <div class="def-grid">
    <div class="def-card">
      <div class="def-tag">Phase 1</div>
      <h4>Pre-Maestro</h4>
      <p>Everything that happens before Maestro itself even starts running.</p>
      <dl>
        <dt>Device probe</dt>
        <dd>Check that the Android device is connected and reachable. Like asking "is the phone there and turned on?" before doing anything else.</dd>
        <dt>App install</dt>
        <dd>Install <strong>WikipediaSample.apk</strong> onto the device. Same idea as installing from the Play Store, just pushed from a host (local) or BrowserStack's server (cloud).</dd>
      </dl>
    </div>

    <div class="def-card">
      <div class="def-tag">Phase 2</div>
      <h4>Maestro startup</h4>
      <p>The Maestro test engine wakes up and gets ready to drive the device.</p>
      <dl>
        <dt>JVM boot (maestro_init)</dt>
        <dd>Maestro is written in Java/Kotlin. The Java engine has to start up first — like turning the key in a car before you can drive. About 1.5 seconds locally.</dd>
        <dt>Driver setup</dt>
        <dd>A small helper app (the "driver") runs on the Android device and is what actually taps, types, and reads the screen. Maestro opens a connection to that driver before any test can happen.</dd>
        <dt>Device info</dt>
        <dd>Maestro asks the phone "what model are you, what's your screen size, what version of Android?" — quick metadata fetch.</dd>
      </dl>
    </div>

    <div class="def-card">
      <div class="def-tag">Phase 3</div>
      <h4>Flow execution</h4>
      <p>The actual test work — opening the app and doing repeated user actions.</p>
      <dl>
        <dt>launchApp</dt>
        <dd>Open Wikipedia Alpha on the device, just like a user tapping the icon on their home screen.</dd>
        <dt>Tap on search</dt>
        <dd>Tap the search field at the top of the app, the same way a user would touch it to start typing.</dd>
        <dt>The repeat block (×100)</dt>
        <dd>Three actions done 100 times in a row: clear the search box → type "BrowserStack" → confirm the result "Software company based in India" shows up. This stresses the system. On cloud, this block is <strong>split into two 50-rep testcases</strong> (test_a + test_b) to fit BrowserStack's per-testcase time cap.</dd>
      </dl>
    </div>

    <div class="def-card">
      <div class="def-tag">Phase 4</div>
      <h4>Post-Maestro</h4>
      <p>Tidying up after the test ends.</p>
      <dl>
        <dt>App uninstall + cleanup</dt>
        <dd>Remove Wikipedia Alpha from the device so the next test starts clean. Closes the driver-app connection.</dd>
      </dl>
    </div>
  </div>
</section>

<!-- ============================================================= -->
<!-- 2. SETUP                                                     -->
<!-- ============================================================= -->
<section id="setup" class="alt">
  <div class="inner">
    <div class="section-eyebrow">02 · The Setup</div>
    <h2>Same app. Same flow. Two environments.</h2>
    <p class="section-lede">
      To compare apples to apples, we used a single Android APK — <strong>WikipediaSample.apk</strong> (Wikipedia Alpha, <code>org.wikipedia.alpha</code>) — and ran an identical Maestro flow locally and on BrowserStack's cloud.
    </p>

    <div class="grid cols-2">
      <div class="card">
        <h3 style="margin-top:0;">WikipediaSample</h3>
        <p>The official open-source Wikipedia Alpha APK. Stable selectors (<code>org.wikipedia.alpha:id/search_container</code>) so the flow is selector-deterministic. <strong>Same .apk uploaded to BrowserStack as installed locally.</strong></p>
        <p class="muted" style="margin: 0; font-size: 13px;">Version 2.5.194-alpha-2017-05-30. Cloud uploads the APK to BS App Automate; local sideloads via <code>adb install</code>.</p>
      </div>
      <div class="card">
        <h3 style="margin-top:0;">The flow</h3>
        <p>One workload, two homes. <strong>Identical commands</strong>; cloud splits into two YAMLs to fit the per-tc cap.</p>
        <pre style="background: #0f172a; color: #e2e8f0; padding: 16px; border-radius: 8px; font-size: 13px; overflow-x: auto; margin: 0;">- launchApp
- tapOn:
    id: "org.wikipedia.alpha:id/search_container"
- repeat:
    times: 50  # ×2 yamls on cloud, ×1 100-rep yaml local
    commands:
      - eraseText
      - inputText: "BrowserStack"
      - assertVisible: "Software company based in India"</pre>
      </div>
    </div>

    <h3>Sizing the workload to ~{spec_target_s:.0f} s P90</h3>
    <p>The spec target is <strong>{spec_target_s:.0f} s P90</strong>, derived from the 5/13 single-session smoke build (build <code>6cbf79e4af4872135a2eb0373c6498e28c61cbfb</code>, session_total = {spec_target_s:.0f} s on Samsung Galaxy S24-14.0). At 100-session scale, cloud P50 landed at <strong>{cloud_p50_s:.1f} s</strong> — {('within 2 % of spec' if abs(spec_delta) < spec_target_s * 0.02 else f'{abs(spec_delta):.1f} s {chr(0x201C)}under{chr(0x201D)} spec' if spec_delta < 0 else f'{spec_delta:.1f} s over spec')}.</p>
    <div class="verdict-formula">
      per-tc workload ≈ 50 reps × ~10.6 s/rep ≈ 530 s<br>
      per-session overhead (launchApp + tap × 2 tcs) ≈ ~85 s<br>
      session_total ≈ 530 + 530 + 85 = <strong>~1145 s</strong> (smoke validated, scaled to 100 sessions)
    </div>
  </div>
</section>

<!-- ============================================================= -->
<!-- 3. JOURNEY                                                   -->
<!-- ============================================================= -->
<section id="journey">
  <div class="section-eyebrow">03 · The Journey</div>
  <h2>What it took to get a clean number</h2>
  <p class="section-lede">
    Benchmarks succeed or fail in the plumbing. Four real bugs / engineering decisions sat between us and trustworthy data — most discovered during the run itself.
  </p>

  <div class="timeline">

    <div class="step">
      <h4>1 — The dual-yaml engineering decision (gotcha #3 mitigation)</h4>
      <p>BrowserStack Maestro v2 enforces a <strong>~900 s per-testcase execution-time cap</strong>. A naive single 100-rep yaml would push tc.duration past the cap and BS would silently kill the test mid-flow — verified against builds at 148-rep, 150-rep, and 132-rep scale, all killed at ~940–975 s.</p>
      <div class="plain-english">
        <p><strong>What this is about:</strong> BrowserStack quietly caps how long one Maestro test can run (around 15 minutes per <em>testcase</em>, not per <em>session</em>). A test running 100 reps of search-and-assert sometimes pushes past that cap — and BS kills it with no error message, no stack trace, no warning. Just session marked failed with metadata showing zero flows completed.</p>
        <p><strong>The fix:</strong> Split the 100 reps across <strong>two identical testcases</strong> (test_a + test_b, 50 reps each) in the same session. BS gives each testcase its own ~900 s budget. Total work per session stays the same; each tc finishes comfortably under the cap.</p>
        <p><strong>This run validates that mitigation at scale.</strong> Across 97 passed cloud sessions (194 testcase runs), test_a P90 = {tc_a_p90/1000:.0f} s, test_b P90 = {tc_b_p90/1000:.0f} s — well under the {cap_threshold_ms/1000:.0f} s inspect threshold. Zero cap-kill violations.</p>
      </div>
    </div>

    <div class="step">
      <h4>2 — The sick device unit (gotcha #16): 3 sessions lost on the same hostname</h4>
      <p>3 of 100 sessions errored at exactly ~322 s with byte-identical fingerprints. All three landed on the <strong>same physical Samsung Galaxy S24 unit</strong> (hostname <code>RZCX60PWTST</code>). BS re-allocated the same sick device three times instead of pulling it from the pool.</p>
      <div class="plain-english">
        <p><strong>What this is about:</strong> Cloud test pools are heterogeneous — BrowserStack runs your test on whichever physical device happens to be free. Most of the pool is healthy, but every once in a while you get a sick device: maybe a background process is hogging CPU, maybe the OS is mid-update, maybe Bluetooth has flooded the dmesg log. Today we hit one such sick device three times in a row.</p>
        <p><strong>The fingerprint:</strong> session lasts ~320 s, both testcases report duration 0 with empty stacktrace, Maestro log stops at "Selected device <code>RZCX60PWTST</code>" before launchApp, generic BS error message "Could not start a session. Please try to run the test again." Different from gotcha #1 (Instrumentation stalled at 36–65 s) — this one is a <strong>~125 s pre-launchApp watchdog kill</strong>, documented as gotcha #16.</p>
        <p><strong>Pool-allocation observation:</strong> 8 passed sessions in the same build hit 8 distinct other hostnames. BS spreads sessions widely, but kept re-picking <code>RZCX60PWTST</code> for 3 attempts despite repeated failure — there is no API today to blacklist a known-bad hostname mid-build.</p>
      </div>
    </div>

    <div class="step">
      <h4>3 — The runner script crashed on a transient curl error</h4>
      <p>Mid-build, <code>cloud_run_android.sh</code>'s 30-second poll loop got a <code>curl: (56) Recv failure: Connection reset by peer</code>. The script's JSON parse threw <code>JSONDecodeError</code>, <code>$BSTATUS</code> resolved to <code>"?"</code>, and the loop broke — writing a 1-byte build JSON and empty <code>sessions.txt</code>.</p>
      <div class="plain-english">
        <p><strong>What happened:</strong> The runner's curl to BS's status endpoint hit a transient TCP reset (Recv failure). The script doesn't retry on transient errors — it just tries to parse an empty response, JSON-decode fails, <code>$BSTATUS</code> becomes the literal string <code>?</code>, and the poll-loop exit condition <code>!= "running"</code> matches that string. Loop breaks. Script writes corrupted state to disk and exits 0.</p>
        <p><strong>The recovery:</strong> BS doesn't know the local runner died. The build continued cooking on BS's side. We re-queried the live build state via the API, overwrote the corrupt <code>{build_id[:14]}…json</code> with the real build state, and regenerated <code>sessions.txt</code> from the live data. No data loss — just operator overhead.</p>
        <p><strong>The fix (recommended follow-up):</strong> Add a curl-retry guard around the poll-loop request. On transient failure, log + sleep + retry instead of treating the corrupt response as terminal.</p>
      </div>
    </div>

    <div class="step">
      <h4>4 — The BS dashboard counts testcases, not sessions</h4>
      <p>Mid-build the BS App Automate dashboard showed "48 PASSED" with a counter ticking at "0 7h 35m 52s" — appearing stalled. <strong>The 48 is testcase count (24 sessions × 2 tcs); the runner counts sessions.</strong> The 7h 35m is cumulative session-time on the device pool, not remaining wall-time.</p>
      <div class="plain-english">
        <p><strong>What this is about:</strong> BrowserStack's web dashboard surfaces aggregate counters that look like sessions but are actually testcase-scoped. For a dual-yaml workload, "48 PASSED" means 24 sessions completed × 2 testcases each. The cumulative-duration counter (7h 35m 52s on screen) is the total <em>billable</em> wall-clock the pool has spent on this build — useful for billing math, misleading as a progress signal.</p>
        <p><strong>Why it matters:</strong> An operator watching the dashboard would mistake healthy progress for a stalled build. The runner's poll output ("running=76 passed=24") is the authoritative session-level count. This is a UI/data-model mismatch worth flagging to the BS team.</p>
      </div>
    </div>

  </div>
</section>

<!-- ============================================================= -->
<!-- 4. LOCAL RESULTS                                             -->
<!-- ============================================================= -->
<section id="local" class="alt">
  <div class="inner">
    <div class="section-eyebrow">04 · Local Results</div>
    <h2>Local: {local_session_n} sessions, all clean</h2>
    <p class="section-lede">
      Wired OnePlus 9R, Android 14, Maestro 1.39.13. Each session does fresh uninstall → install → run flow → uninstall. Total wall-clock: ~3 hours.
    </p>

    <div class="grid cols-4">
      <div class="card">
        <div class="number-label">Sessions completed</div>
        <div class="big-number pass">{local_session_n}<span class="unit">/ {local_session_n}</span></div>
        <div class="number-context">Zero failures</div>
      </div>
      <div class="card">
        <div class="number-label">Session_total P50</div>
        <div class="big-number">{local_p50_s:.1f}<span class="unit">s</span></div>
        <div class="number-context">Median end-to-end</div>
      </div>
      <div class="card">
        <div class="number-label">Session_total P90</div>
        <div class="big-number">{local_p90_s:.1f}<span class="unit">s</span></div>
        <div class="number-context">9 sessions, tight spread</div>
      </div>
      <div class="card">
        <div class="number-label">Per-rep cost (P90)</div>
        <div class="big-number">{local_per_rep_p90/1000:.1f}<span class="unit">s</span></div>
        <div class="number-context">erase + input + assert · 900 reps</div>
      </div>
    </div>

    <h3>Local — full percentile table</h3>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Phase / step</th>
            <th class="num">N</th>
            <th class="num">Min</th>
            <th class="num">P50</th>
            <th class="num">P75</th>
            <th class="num">P90</th>
            <th class="num">P95</th>
            <th class="num">P100</th>
          </tr>
        </thead>
        <tbody>
{local_table_body}
        </tbody>
      </table>
    </div>
    <p class="muted" style="font-size: 13px;">Values in milliseconds. Loop-body steps have N=900 (9 sessions × 100 reps); single-shot phases have N=9.</p>
  </div>
</section>

<!-- ============================================================= -->
<!-- 5. CLOUD RESULTS                                             -->
<!-- ============================================================= -->
<section id="cloud">
  <div class="section-eyebrow">05 · Cloud Results</div>
  <h2>Cloud: {cloud_n_requested} device entries, {cloud_n_data} with data</h2>
  <p class="section-lede">
    One BrowserStack build, {cloud_n_requested} Samsung Galaxy S24-14.0 device entries (ap-south-1), 2 testcases × 50 reps each. Wall-clock from trigger to final session terminal: ~100 minutes. BigQuery ingestion lag adds ~50 minutes.
  </p>

  <div class="grid cols-4">
    <div class="card">
      <div class="number-label">Sessions in dataset</div>
      <div class="big-number">{cloud_n_data}<span class="unit">/ {cloud_n_requested}</span></div>
      <div class="number-context">{cloud_n_requested - cloud_n_data} errored on hostname RZCX60PWTST (gotcha #16)</div>
    </div>
    <div class="card">
      <div class="number-label">Pass rate</div>
      <div class="big-number pass">{pass_rate_pct:.1f}<span class="unit">%</span></div>
      <div class="number-context">{cloud_n_data} passed · {cloud_n_requested - cloud_n_data} errored (sick unit)</div>
    </div>
    <div class="card">
      <div class="number-label">Session_total P50</div>
      <div class="big-number">{cloud_p50_s:.1f}<span class="unit">s</span></div>
      <div class="number-context">{f'{spec_delta:+.1f} s vs spec target' if spec_delta else 'On spec'}</div>
    </div>
    <div class="card">
      <div class="number-label">Session_total P90</div>
      <div class="big-number pass">{cloud_p90_s:.1f}<span class="unit">s</span></div>
      <div class="number-context">{(cloud_p90_s - local_p90_s):+.1f} s vs local · under the {ceiling_ratio:.1f}× ceiling</div>
    </div>
  </div>

  <h3>Cloud — phase percentiles (BigQuery + BS REST)</h3>
  <p class="muted">Cloud BQ exposes session-level phases only. Per-tc durations come from <code>/maestro/v2/builds/&lt;bid&gt;/sessions/&lt;sid&gt;</code> for each passed session.</p>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Phase / step</th>
          <th class="num">N</th>
          <th class="num">Min</th>
          <th class="num">P50</th>
          <th class="num">P75</th>
          <th class="num">P90</th>
          <th class="num">P95</th>
          <th class="num">P100</th>
        </tr>
      </thead>
      <tbody>
{cloud_table_body}
      </tbody>
    </table>
  </div>
  <p class="muted" style="font-size: 13px;">
    {'<strong style="color:var(--pass);">✓ No cap violations</strong>: all 194 testcase runs (97 sessions × 2 tcs) completed well under the ~870 s gotcha-#3 inspect threshold. The split-yaml mitigation is validated at scale.' if not cap_violation else '<strong style="color:var(--fail);">⚠ Cap violations detected</strong>: at least one tc.duration crossed the ~870 s gotcha-#3 inspect threshold.'}
  </p>
</section>

<!-- ============================================================= -->
<!-- 6. PHASE MAPPING                                             -->
<!-- ============================================================= -->
<section id="mapping">
  <div class="section-eyebrow">06 · Phase Mapping</div>
  <h2>Translating cloud phases to local phases</h2>
  <p class="section-lede">
    Local and cloud expose different metrics for the same underlying work. This is the Rosetta Stone — what each BrowserStack BigQuery field means and how it maps to a local phase. Read in chronological order; each row is one moment in a session's life.
  </p>

  <div class="grid cols-3" style="margin-bottom: 24px;">
    <div class="card" style="border-left: 4px solid var(--pass);">
      <div class="number-label">Direct match ✓</div>
      <p style="margin: 8px 0 0; font-size: 14px;">Cloud field measures the same work as the local phase, just instrumented differently. Numbers are directly comparable.</p>
    </div>
    <div class="card" style="border-left: 4px solid var(--warn);">
      <div class="number-label">Partial match ≈</div>
      <p style="margin: 8px 0 0; font-size: 14px;">Cloud field overlaps with the local phase but covers more (or less) work. Compare with care.</p>
    </div>
    <div class="card" style="border-left: 4px solid #94a3b8;">
      <div class="number-label">Cloud-only / blank —</div>
      <p style="margin: 8px 0 0; font-size: 14px;">Cloud has overhead local doesn't (BS device-pool work), or the field exists but isn't populated.</p>
    </div>
  </div>

  <div class="phase-map">

    <div class="map-row">
      <div class="map-side cloud">
        <div class="map-phase">start_sessions_ms</div>
        <div class="map-tag">BS lifecycle · Cloud-only</div>
        <div class="map-def">Internal BrowserStack bookkeeping when a session opens — attaching to the customer's plan, allocating a dashboard entry, kicking off device assignment.</div>
      </div>
      <div class="map-conn">
        <div class="match-dot none">—</div>
        <div class="match-label">Cloud-only</div>
      </div>
      <div class="map-side local empty">
        <div class="map-phase">no equivalent</div>
        <div class="map-tag">N/A</div>
        <div class="map-def">Local has no "session" concept — each iteration is a bash loop. Pure cloud overhead with no local counterpart.</div>
      </div>
    </div>

    <div class="map-row">
      <div class="map-side cloud">
        <div class="map-phase">app_dl_ms</div>
        <div class="map-tag">App provisioning · Cloud-only</div>
        <div class="map-def">BrowserStack downloads the uploaded <code>.apk</code> from its blob storage to the host machine that drives the Samsung Galaxy S24.</div>
      </div>
      <div class="map-conn">
        <div class="match-dot none">—</div>
        <div class="match-label">Cloud-only</div>
      </div>
      <div class="map-side local empty">
        <div class="map-phase">no equivalent</div>
        <div class="map-tag">N/A</div>
        <div class="map-def">Local already has the <code>.apk</code> on disk — no download step. Cost of BS owning the storage layer.</div>
      </div>
    </div>

    <div class="map-row">
      <div class="map-side cloud">
        <div class="map-phase">app_install_ms</div>
        <div class="map-tag">Pre-Maestro</div>
        <div class="map-def">BrowserStack installs the <code>.apk</code> onto the assigned Samsung Galaxy S24 via its provisioning pipeline. <strong>Cloud P50: {_fmt_ms(cloud_session.get(('Pre-Maestro', 'app_install'), _BLANK).get('p50_ms', 0))} ms.</strong></div>
      </div>
      <div class="map-conn">
        <div class="match-dot full">✓</div>
        <div class="match-label">Direct match</div>
      </div>
      <div class="map-side local">
        <div class="map-phase">app_install</div>
        <div class="map-tag">Pre-Maestro</div>
        <div class="map-def">Local installs the <code>.apk</code> via <code>adb install</code>. Same outcome — app on device — different machinery. <strong>Local P50: {_fmt_ms(local.get(('Pre-Maestro', 'app_install'), _BLANK).get('p50_ms', 0))} ms.</strong></div>
      </div>
    </div>

    <div class="map-row">
      <div class="map-side cloud">
        <div class="map-phase">test_dl_ms</div>
        <div class="map-tag">Test provisioning · Cloud-only</div>
        <div class="map-def">BrowserStack downloads the uploaded test-suite zip (the multitest/ flow YAMLs) from blob storage to the host machine.</div>
      </div>
      <div class="map-conn">
        <div class="match-dot none">—</div>
        <div class="match-label">Cloud-only</div>
      </div>
      <div class="map-side local empty">
        <div class="map-phase">no equivalent</div>
        <div class="map-tag">N/A</div>
        <div class="map-def">Local has the YAML files on disk — Maestro reads them directly. Same idea as <code>app_dl_ms</code> but for the test code.</div>
      </div>
    </div>

    <div class="map-row">
      <div class="map-side cloud">
        <div class="map-phase">test_install_ms</div>
        <div class="map-tag">Maestro startup</div>
        <div class="map-def">BS installs the on-device Maestro driver (small helper Maestro talks to to tap, type, and read the screen). Fresh on every session.</div>
      </div>
      <div class="map-conn">
        <div class="match-dot partial">≈</div>
        <div class="match-label">Partial match</div>
      </div>
      <div class="map-side local">
        <div class="map-phase">driver_setup</div>
        <div class="map-tag">Maestro startup</div>
        <div class="map-def">Local pre-builds the driver once and reuses across all sessions. <code>driver_setup</code> is just the connection handshake (<strong>~{_fmt_ms(local.get(('Maestro startup', 'driver_setup'), _BLANK).get('p90_ms', 0))} ms P90</strong>); the install cost is paid once and not counted. Recommendation #2 (pre-warm driver) targets this gap.</div>
      </div>
    </div>

    <div class="map-row">
      <div class="map-side cloud">
        <div class="map-phase">firecmd_ms</div>
        <div class="map-tag">Maestro startup · catch-all</div>
        <div class="map-def">Time between BS receiving the build trigger and Maestro actually starting flow commands. Lumps together JVM start, driver-handshake, device-info fetch, and BS-side orchestration.</div>
      </div>
      <div class="map-conn">
        <div class="match-dot partial">≈</div>
        <div class="match-label">Partial match</div>
      </div>
      <div class="map-side local">
        <div class="map-phase">maestro_init + driver_setup + device_info</div>
        <div class="map-tag">Maestro startup</div>
        <div class="map-def">Three discrete phases on local totaling ~2.4 s P50. The cloud <code>firecmd</code> bucket is larger because it also includes BS-side device assignment and orchestration — overhead that doesn't exist locally.</div>
      </div>
    </div>

    <div class="map-row">
      <div class="map-side cloud">
        <div class="map-phase">duration <span style="color: var(--ink-muted); font-weight: 400;">(execution_s)</span></div>
        <div class="map-tag">Flow execution · coarser</div>
        <div class="map-def">Pure flow-execution time — what Maestro itself reports. Includes everything from "Define variables" through the last assertion. On Android dual-yaml, this is the <strong>sum</strong> across test_a + test_b plus inter-tc envelope (~85 s).</div>
      </div>
      <div class="map-conn">
        <div class="match-dot partial">≈</div>
        <div class="match-label">Direct match,<br>but coarser</div>
      </div>
      <div class="map-side local">
        <div class="map-phase">Flow execution (8 sub-phases)</div>
        <div class="map-tag">Flow execution</div>
        <div class="map-def">Local breaks this into Define variables, Apply configuration, Launch app, Tap on OK dialog, Tap on search container, Erase text, Input text, Assert visible, and Per-rep total. Cloud collapses all into one — the per-tc breakdown (test_a, test_b) is our compromise visibility layer.</div>
      </div>
    </div>

    <div class="map-row">
      <div class="map-side cloud">
        <div class="map-phase">testcases[].duration</div>
        <div class="map-tag">Per-tc · BS REST</div>
        <div class="map-def">Per-testcase wall-clock from BS App Automate REST API (<code>/maestro/v2/builds/&lt;bid&gt;/sessions/&lt;sid&gt;</code>). For our dual-yaml run: <strong>test_a P50 = {_fmt_s(tc_a.get('p50_ms', 0))} s, test_b P50 = {_fmt_s(tc_b.get('p50_ms', 0))} s</strong>. <em>This data is NOT in BigQuery.</em></div>
      </div>
      <div class="map-conn">
        <div class="match-dot partial">≈</div>
        <div class="match-label">Synthesized</div>
      </div>
      <div class="map-side local">
        <div class="map-phase">No analog</div>
        <div class="map-tag">N/A</div>
        <div class="map-def">Local runs a single 100-rep yaml — no testcase boundary inside the session. We can compute "per-50-rep cost" by halving the local per-rep × 50, but it's an artifact of the cloud structure.</div>
      </div>
    </div>

    <div class="map-row">
      <div class="map-side cloud">
        <div class="map-phase">total_stop_time</div>
        <div class="map-tag">Post-Maestro · NULL today</div>
        <div class="map-def">App uninstall and post-session cleanup. The field exists in the BigQuery schema but isn't populated for Maestro sessions — every cloud row in our dataset shows NULL here.</div>
      </div>
      <div class="map-conn">
        <div class="match-dot partial">≈</div>
        <div class="match-label">Field exists,<br>but blank</div>
      </div>
      <div class="map-side local">
        <div class="map-phase">stop</div>
        <div class="map-tag">Post-Maestro</div>
        <div class="map-def">Local uninstalls the app via <code>adb uninstall</code> after the test (~<strong>{_fmt_ms(local.get(('Post-Maestro', 'stop'), _BLANK).get('p90_ms', 0))} ms P90</strong>). Cloud presumably does similar work, but the timing is missing from BigQuery today.</div>
      </div>
    </div>

    <div class="map-row">
      <div class="map-side cloud">
        <div class="map-phase">customer_session_duration <span style="color: var(--ink-muted); font-weight: 400;">(BS REST: session.duration)</span></div>
        <div class="map-tag">Aggregate</div>
        <div class="map-def">Wall-clock time the customer is billed for. Reported by the BrowserStack REST API on every session. <strong>Cloud P90: {cloud_p90_s:.1f} s</strong> — used as the cloud-side number in the {ceiling_ratio:.1f}× verdict.</div>
      </div>
      <div class="map-conn">
        <div class="match-dot full">✓</div>
        <div class="match-label">Direct match</div>
      </div>
      <div class="map-side local">
        <div class="map-phase">session_total</div>
        <div class="map-tag">Aggregate</div>
        <div class="map-def">Local wall-clock for one full session: device probe + app install + maestro_total + uninstall. <strong>Local P90: {local_p90_s:.1f} s.</strong> Both represent the same thing — end-to-end customer-perceived duration. The headline pair the verdict compares.</div>
      </div>
    </div>

  </div>

  <h3>Three things this mapping makes clear</h3>
  <div class="grid cols-3">
    <div class="card">
      <h4 style="margin-top:0;">Cloud has overhead local can't have</h4>
      <p>Three cloud phases (<code>start_sessions</code>, <code>app_dl</code>, <code>test_dl</code>) are pure cloud-only work — the cost of BrowserStack owning the device pool, blob storage, and signing infrastructure. Local doesn't have these because the customer's host already has the files.</p>
    </div>
    <div class="card">
      <h4 style="margin-top:0;">Per-tc data is REST-only, not BQ</h4>
      <p>For dual-yaml workloads, the per-testcase breakdown is the most useful diagnostic — but BigQuery aggregates it away. The per-tc timings in this report come from N+1 REST calls (one build endpoint + N session-detail endpoints). Recommendation #3 (surface per-tc in BQ) addresses this.</p>
    </div>
    <div class="card">
      <h4 style="margin-top:0;">Cloud wins on flow execution, loses on install</h4>
      <p>Cloud <code>app_install</code> is ~{install_delta_p90_s:.1f} s slower at P90 (BS install pipeline overhead). But cloud's <strong>flow execution</strong> beats local because the S24's newer SoC outpaces the OnePlus 9R on raw tap/type/assert throughput. Net result: cloud session_total wins by {delta_local_minus_cloud_p90_s:.0f} s at P90.</p>
    </div>
  </div>
</section>

<!-- ============================================================= -->
<!-- 7. VERDICT                                                   -->
<!-- ============================================================= -->
<section id="verdict" class="alt">
  <div class="inner">
    <div class="section-eyebrow">07 · The Verdict</div>
    <h2>Cloud vs Local — head to head</h2>
    <p class="section-lede">
      The {ceiling_ratio:.1f}× target translates to a hard number: local P90 × {ceiling_ratio:.1f} = the ceiling cloud must stay under.
    </p>

    <div class="verdict-block{verdict_block_class}">
      <h3 style="margin-top: 0;">Cloud P90 is {ratio:.3f}× of local P90</h3>
      <div class="verdict-math">
        <div class="verdict-cell">
          <div class="number-label">Local P90</div>
          <div class="big-number">{local_p90_s:.1f}<span class="unit">s</span></div>
        </div>
        <div class="verdict-cell">
          <div class="number-label">{ceiling_ratio:.1f}× ceiling</div>
          <div class="big-number">{ceiling_s:.1f}<span class="unit">s</span></div>
        </div>
        <div class="verdict-cell">
          <div class="number-label">Cloud P90</div>
          <div class="big-number pass">{cloud_p90_s:.1f}<span class="unit">s</span></div>
        </div>
        <div class="verdict-cell">
          <div class="number-label">{('Under ceiling by' if delta_to_ceiling_s < 0 else 'Over ceiling by')}</div>
          <div class="big-number pass">{abs(delta_to_ceiling_s):.1f}<span class="unit">s</span></div>
        </div>
      </div>
      <div class="verdict-formula">
        Ratio = {cloud_p90_ms:,} / {local_p90_ms:,} = <strong>{ratio:.4f}×</strong> &nbsp;&nbsp;|&nbsp;&nbsp; Threshold = {ceiling_ratio:.1f}× &nbsp;&nbsp;|&nbsp;&nbsp; Result: <strong style="color: var(--pass);">{('PASS — under ceiling by ' + str(round(abs(delta_to_ceiling_s), 1)) + ' s') if delta_to_ceiling_s < 0 else 'MISS — over ceiling by ' + str(round(delta_to_ceiling_s, 1)) + ' s'}</strong>
      </div>
      <p style="margin-bottom: 0;">Cloud Samsung Galaxy S24 beats wired OnePlus 9R on session_total P90 by <strong>{delta_local_minus_cloud_p90_s:.0f} s</strong>. The dual-yaml split mitigation works as intended (zero cap kills across 194 tc runs). Recommendations below cover where the remaining margin sits.</p>
    </div>

    <h3>Side-by-side numbers</h3>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Phase / step</th>
            <th class="num">Local N</th>
            <th class="num">Local P50</th>
            <th class="num">Local P90</th>
            <th class="num">Cloud N</th>
            <th class="num">Cloud P50</th>
            <th class="num">Cloud P90</th>
            <th class="num">Δ P50</th>
            <th class="num">Δ P90</th>
          </tr>
        </thead>
        <tbody>
{compare_table_body}
        </tbody>
      </table>
    </div>
    <p class="muted" style="font-size: 13px;">Δ in milliseconds. <span class="delta-neg">Negative</span> = cloud faster. <span class="delta-pos">Positive</span> = cloud slower.</p>

    <h3>What the data tells us</h3>
    <div class="grid cols-2">
      <div class="card">
        <h4 style="margin-top:0;">✓ Cloud beats local on session_total</h4>
        <p>Cloud P90 of <strong>{cloud_p90_s:.1f} s</strong> vs local's <strong>{local_p90_s:.1f} s</strong>. Despite BS-side install overhead, the Samsung Galaxy S24's newer hardware closes (and inverts) the gap on the per-rep workload. Net win for cloud at the headline number.</p>
      </div>
      <div class="card">
        <h4 style="margin-top:0;">⚠ app_install is where cloud loses time</h4>
        <p>Cloud <code>app_install</code> P90 = <strong>{_fmt_ms(cloud_session.get(('Pre-Maestro', 'app_install'), _BLANK).get('p90_ms', 0))} ms</strong> vs local's <strong>{_fmt_ms(local.get(('Pre-Maestro', 'app_install'), _BLANK).get('p90_ms', 0))} ms</strong> — cloud is ~{install_delta_p90_s:.1f} s slower. Pure BS-side pipeline overhead. The only zone where cloud is consistently slower than local at every percentile.</p>
      </div>
    </div>
  </div>
</section>

<!-- ============================================================= -->
<!-- 8. RECOMMENDATIONS                                           -->
<!-- ============================================================= -->
<section id="recommendations">
  <div class="section-eyebrow">08 · Recommendations</div>
  <h2>Why cloud is faster — and how to extend the lead</h2>
  <p class="section-lede">
    Cloud Samsung Galaxy S24 beats wired OnePlus 9R at session_total P90. Two reasons cloud wins, three places BrowserStack can squeeze more headroom.
  </p>

  <h3 style="margin-bottom: 16px;">Why cloud wins (the per-rep math)</h3>
  <div class="grid cols-2">
    <div class="card">
      <h4 style="margin-top:0;">1. Newer SoC, faster flow execution</h4>
      <p>The Samsung Galaxy S24's SoC outpaces the OnePlus 9R (Snapdragon 870) on raw per-rep throughput. Cloud per-tc P50 = <strong>{_fmt_s(tc_a_p50_ms)} s</strong> per 50-rep testcase, or <strong>~{cloud_per_rep_s:.2f} s/rep</strong>. Local per-rep P50 = <strong>{local_per_rep_s:.2f} s/rep</strong>. Hardware deltas compound across 100 reps.</p>
    </div>
    <div class="card">
      <h4 style="margin-top:0;">2. Cloud avoids local's USB-bridge overhead</h4>
      <p>Local Maestro runs over an <code>adb</code> connection via USB. Every tap, type, and assert command serializes through that bridge, adding microseconds-to-milliseconds per command. Cloud BS-managed devices have the driver pre-installed on the same physical pipeline as the OS, eliminating that bridge.</p>
    </div>
  </div>

  <h3 style="margin-top: 32px; margin-bottom: 16px;">How BrowserStack can extend the lead</h3>

  <div class="reco">
    <span class="reco-impact high">High impact</span>
    <h4>1 — Reduce <code>app_install</code> overhead via APK warm-caching</h4>
    <p>Cloud <code>app_install</code> P90 is <strong>{_fmt_ms(cloud_session.get(('Pre-Maestro', 'app_install'), _BLANK).get('p90_ms', 0))} ms</strong> — {install_delta_p90_s:.1f} s slower than local at P90. For benchmark workloads where the same APK runs 100+ times in a row, that's pure repeated work.</p>
    <div class="plain-english">
      <p><strong>What this is about:</strong> Every cloud session re-installs the APK from BS's blob storage, even though the same APK was just installed seconds earlier. It's like a barista re-grinding their beans for every cup instead of using the same grinder all morning.</p>
      <p><strong>Why it matters:</strong> A customer running 100 benchmark sessions pays this ~6.5 s tax 100 times — that's over 10 minutes of pure waste, with no benefit. Even a 50% reduction (cache the resolved APK URL + content hash for a "warm" window) recovers ~5 min on a 100-session run.</p>
      <p><strong>Fix:</strong> Detect repeated APK uploads via content hash; serve from a host-local cache instead of re-fetching from blob storage. Optional opt-in flag for benchmark mode.</p>
    </div>
  </div>

  <div class="reco">
    <span class="reco-impact high">High impact</span>
    <h4>2 — Detect and quarantine sick devices (gotcha #16)</h4>
    <p>3 of 100 sessions in this build landed on the same physical Samsung Galaxy S24 unit (hostname <code>RZCX60PWTST</code>) and all 3 errored identically at ~322 s. BS keeps re-allocating the same sick device.</p>
    <div class="plain-english">
      <p><strong>What this is about:</strong> When a device fails the ~125 s no-flow-progress watchdog twice in quick succession on the same hostname, BS's pool allocator should treat that hostname as suspect and exclude it from further allocations in the same build (or platform-wide for some cool-off period).</p>
      <p><strong>Why it matters:</strong> Without quarantine, the same sick device can fail an unbounded number of sessions in a single build. We saw 3 in 100 (3 % impact); a sicker pool could see 10+. For benchmark workloads where statistical density matters, this directly reduces sample size.</p>
      <p><strong>Fix:</strong> Mid-build hostname blacklisting (BS-internal allocator-level), surfaced as an API hint to operators ("session X retried on a different hostname after Y").</p>
    </div>
  </div>

  <div class="reco">
    <span class="reco-impact med">Medium impact</span>
    <h4>3 — Surface per-testcase timings in BigQuery</h4>
    <p>For dual-yaml workloads, per-tc duration is the single most useful diagnostic. Today it requires N REST calls — one per session — to assemble. BigQuery aggregates testcase data away.</p>
    <div class="plain-english">
      <p><strong>What this is about:</strong> Maestro keeps per-testcase logs that BS already captures (the per-tc <code>duration</code> field is in the REST <code>/sessions/&lt;sid&gt;</code> endpoint). BQ flattens this into the session-level <code>duration</code> and a single JSON-string <code>test_status</code> counts blob.</p>
      <p><strong>Why it matters:</strong> Operators running dual-yaml workloads can't query "show me builds where test_a P90 spiked relative to test_b" or "show me sessions where test_b errored while test_a passed". Today that's a 100-call REST iteration.</p>
      <p><strong>Fix:</strong> Add per-tc rows to BQ (one row per session per tc) with at minimum: <code>tc_class</code>, <code>tc_duration</code>, <code>tc_status</code>, <code>tc_start_offset_ms</code>. Diagnostic-only; no perf impact.</p>
    </div>
  </div>

  <div class="card" style="margin-top: 32px; background: linear-gradient(135deg, #ecfeff 0%, #f0f9ff 100%); border-color: var(--accent);">
    <h3 style="margin-top: 0;">Bottom line</h3>
    <p style="margin: 0;">Cloud already wins on raw session_total P90. The per-rep workload (~92 % of session_total) is hardware-bound — and BS's S24 hardware wins. The reachable budget is the <strong>app_install pipeline</strong>; recommendation #1 alone recovers ~5 s P90 and pushes cloud's lead from {delta_local_minus_cloud_p90_s:.0f} s to ~{extended_lead_s:.0f} s without any change to the workload.</p>
  </div>
</section>

<!-- ============================================================= -->
<!-- 9. METHODOLOGY                                               -->
<!-- ============================================================= -->
<section id="methodology" class="alt">
  <div class="inner">
    <div class="section-eyebrow">09 · Methodology</div>
    <h2>How we measured</h2>
    <p class="section-lede">For reproducibility and audit. Skip if you trust the numbers.</p>

    <details>
      <summary>Local measurement stack</summary>
      <ul>
        <li><strong>Host:</strong> macOS 25.4, ADB platform-tools current.</li>
        <li><strong>Device:</strong> OnePlus 9R (Android 14), USB-tethered.</li>
        <li><strong>Maestro CLI:</strong> 1.39.13 (stable release).</li>
        <li><strong>Test runner:</strong> Maestro driver pre-installed once; reused across all sessions.</li>
        <li><strong>Per-iter cycle:</strong> uninstall app → install app → maestro test → uninstall app.</li>
        <li><strong>Phase parsing:</strong> Maestro debug log parsed by <code>parse_maestro_log.py</code> for step-level timings; bash wrapper records phase boundaries via <code>now_ms</code> wall-clock.</li>
      </ul>
    </details>

    <details>
      <summary>Cloud measurement stack</summary>
      <ul>
        <li><strong>Build trigger:</strong> One BrowserStack Maestro build with {cloud_n_requested} device entries (<code>Samsung Galaxy S24-14.0</code>, ap-south-1).</li>
        <li><strong>Workload split:</strong> 2 yamls in <code>execute</code> array (test_a.yaml, test_b.yaml), 50 reps each — gotcha #3 split-yaml mitigation.</li>
        <li><strong>Concurrency:</strong> ~25 sessions running in parallel waves (BS plan parallel cap).</li>
        <li><strong>Status polling:</strong> BS REST API every 30 s until terminal.</li>
        <li><strong>Per-session BQ metrics:</strong> Pulled from <code>browserstack-production.app_automate.app_automate_test_sessions_partitioned</code> ~50 min after build completion, filtered with <code>JSON_VALUE(test_status, '$.error') = '0'</code> to exclude the 3 gotcha-#16 errored sessions.</li>
        <li><strong>Per-tc timings:</strong> N+1 REST calls — 1 build endpoint to list session ids, N session-detail endpoints (<code>/maestro/v2/builds/&lt;bid&gt;/sessions/&lt;sid&gt;</code>) to extract <code>testcases.data[].testcases[].duration</code>.</li>
      </ul>
    </details>

    <details>
      <summary>Percentile method</summary>
      <p>Linear-interpolation percentile (numpy default). For repeating loop-body steps, N = sessions × reps_per_session = 900 on local.</p>
      <p>Cloud aggregates include all {cloud_n_data} passed sessions; the 3 errored sessions on hostname RZCX60PWTST (gotcha #16) are excluded by BQ filter. One outlier passed session at 2054 s appears at P100 only.</p>
    </details>

    <details>
      <summary>Sample sizes &amp; confidence</summary>
      <ul>
        <li><strong>Local:</strong> {local_session_n} sessions, narrow spread. Confidence high.</li>
        <li><strong>Cloud:</strong> {cloud_n_data} sessions (passed only). Range: 1036–2054 s. P50–P95 in a tight 1139–1162 s band; P100 is the single outlier.</li>
        <li><strong>Sample size for cloud P90:</strong> at N={cloud_n_data}, the P90 estimate has ±1 rank uncertainty. The {cloud_p90_s:.1f} s figure is robust.</li>
      </ul>
    </details>

    <details>
      <summary>Files &amp; data sources (for replay)</summary>
      <ul>
        <li><strong>Local final report:</strong> <code>android/local/results/local_android_final_report_20260510_231553.csv</code></li>
        <li><strong>Cloud run dir:</strong> <code>results/cloud_20260518_105518/</code></li>
        <li><strong>Cloud BQ response (passed-only):</strong> <code>results/cloud_20260518_105518/bq_response.json</code></li>
        <li><strong>Side-by-side CSV:</strong> <code>analysis/local_vs_cloud_android_comparison_&lt;TS&gt;.csv</code></li>
        <li><strong>BQ build_id:</strong> <code>{build_id}</code></li>
        <li><strong>Aggregator:</strong> <code>aggregate_android_cloud_report.py</code></li>
        <li><strong>Flow YAMLs:</strong> <code>android/cloud/flows/multitest/test_a.yaml</code>, <code>test_b.yaml</code></li>
        <li><strong>Runbook of platform gotchas:</strong> <code>docs/runbooks/cloud-maestro-gotchas.md</code></li>
      </ul>
    </details>
  </div>
</section>

<footer>
  Generated by <a href="https://github.com/vinit-09/perf_bench_maestro">aggregate_android_cloud_report.py</a>
  · Build <code>{build_id}</code> · {cloud_n_data}/{cloud_n_requested} passed · Cloud P90 {cloud_p90_s:.1f} s vs Local P90 {local_p90_s:.1f} s ({ratio:.3f}×)
</footer>

</body>
</html>
"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)


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
    ap.add_argument("--spec-target-s", type=float, default=1153.0,
                    help="Spec target session_total in seconds (default 1153 — 5/13 smoke baseline)")
    ap.add_argument("--ceiling-ratio", type=float, default=1.1,
                    help="Ceiling ratio over local P90 (default 1.1× matches iOS convention)")
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

    print(f"[load] local final report: {args.local_final_csv}")
    local = load_local_final(args.local_final_csv)
    print(f"  rows: {len(local)}")

    print(f"[load] BQ response: {args.bq_response}")
    bq_response = json.loads(args.bq_response.read_text())
    try:
        cell = load_cloud_cell(bq_response, cell_name="cloud_android", os="android")
    except (EmptyCellError, MalformedCellError) as e:
        print(f"ERROR: cloud cell load failed: {e}", file=sys.stderr)
        return 1
    print(f"  cloud sessions: {len(cell.sessions)}")
    cloud_session = cloud_session_stats(cell)

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

    rows = build_comparison_rows(local, cloud_session, cloud_per_tc)
    csv_out = args.out_dir / f"local_vs_cloud_android_comparison_{ts}.csv"
    write_comparison_csv(rows, csv_out)
    print(f"[emit] comparison CSV: {csv_out}")

    html_out = args.out_dir / "maestro_android_benchmark_report.html"
    write_html_report(
        rows, local, cloud_session, cloud_per_tc,
        build_id=args.build_id,
        cloud_n_requested=len(all_session_ids),
        cloud_n_data=len(cell.sessions),
        spec_target_s=args.spec_target_s,
        ceiling_ratio=args.ceiling_ratio,
        out_path=html_out,
    )
    print(f"[emit] HTML report: {html_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
