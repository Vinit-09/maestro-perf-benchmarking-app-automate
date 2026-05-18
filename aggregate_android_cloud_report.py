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
# HTML report (U5) — placeholder; full skeleton implemented in U5.
# ----------------------------------------------------------------------------

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
    """Render maestro_android_benchmark_report.html — U5 placeholder.

    Full iOS-mirror skeleton + dual-yaml-mechanics section + computed
    quantitative hero claim is implemented in U5. This stub guarantees the
    output file exists so U4's wire-up tests pass; U5 fills it in.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "<!-- U5 placeholder. Run after U5 lands to render the full report. -->\n"
        f"<!-- build_id: {build_id}, cloud_n_data: {cloud_n_data} -->\n"
    )


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
    session_ids = [
        s["id"]
        for dev in build_data.get("devices", [])
        for s in dev.get("sessions", [])
        if s.get("status") == "passed"
    ]
    print(f"  passed session_ids: {len(session_ids)}")

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

    # HTML report placeholder (U5 fills the skeleton).
    html_out = args.out_dir / "maestro_android_benchmark_report.html"
    write_html_report(
        rows, local, cloud_session, cloud_per_tc,
        build_id=args.build_id,
        cloud_n_requested=len(session_ids),
        cloud_n_data=len(cell.sessions),
        out_path=html_out,
    )
    print(f"[emit] HTML report (U5 placeholder): {html_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
