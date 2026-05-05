#!/usr/bin/env python3
"""Aggregate per-iteration metrics from results/sessions.csv into P50/P90/min/max/mean.

Usage:
  ./aggregate_results.py                              # all rows
  ./aggregate_results.py --tag baseline               # filter by tag
  ./aggregate_results.py --tag baseline --json        # JSON output
"""
from __future__ import annotations
import argparse
import csv
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "results" / "sessions.csv"

METRICS = [
    "device_readiness_ms",
    "app_install_ms",
    "maestro_total_ms",
    "maestro_start_ms",
    "execution_ms",
    "stop_ms",
    "session_total_ms",
]


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def stats(xs: list[float]) -> dict:
    xs = [x for x in xs if x is not None and x >= 0]
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "min": min(xs),
        "p50": percentile(xs, 0.50),
        "p90": percentile(xs, 0.90),
        "p95": percentile(xs, 0.95),
        "max": max(xs),
        "mean": sum(xs) / len(xs),
    }


def load(csv_path: Path, tag: str | None) -> list[dict]:
    rows = []
    with csv_path.open() as fh:
        for row in csv.DictReader(fh):
            if int(row.get("exit_code", "1")) != 0:
                continue
            if tag and row.get("tag") != tag:
                continue
            rows.append(row)
    return rows


def fmt_ms(v: float) -> str:
    if math.isnan(v):
        return "—"
    return f"{v:>10,.0f} ms ({v/1000:>7.2f} s)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", help="filter by tag (e.g. baseline, smoke)")
    ap.add_argument("--csv", default=str(CSV_PATH))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"missing {csv_path}", file=sys.stderr)
        return 2

    rows = load(csv_path, args.tag)
    if not rows:
        print(f"no rows {'for tag '+args.tag if args.tag else ''}", file=sys.stderr)
        return 1

    out = {"n_runs": len(rows), "tag": args.tag, "metrics": {}}
    for m in METRICS:
        vals = []
        for r in rows:
            v = r.get(m)
            if v in (None, "", "-1"):
                continue
            try:
                vals.append(float(v))
            except ValueError:
                pass
        out["metrics"][m] = stats(vals)

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"\n=== Aggregated metrics ===  n={out['n_runs']}  tag={out['tag'] or 'ALL'}\n")
    print(f"{'metric':<22} {'min':>16} {'P50':>16} {'P90':>16} {'P95':>16} {'max':>16} {'mean':>16}")
    print("-" * 130)
    for m in METRICS:
        s = out["metrics"][m]
        if s.get("n", 0) == 0:
            print(f"{m:<22}  (no data)")
            continue
        print(
            f"{m:<22}"
            f" {fmt_ms(s['min']):>16}"
            f" {fmt_ms(s['p50']):>16}"
            f" {fmt_ms(s['p90']):>16}"
            f" {fmt_ms(s['p95']):>16}"
            f" {fmt_ms(s['max']):>16}"
            f" {fmt_ms(s['mean']):>16}"
        )
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
