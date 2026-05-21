#!/usr/bin/env python3
"""Emit local_<platform>_final_report and local_<platform>_sessions_report after a benchmark run.

Reads:
  <run_dir>/iter_<n>/meta.txt      (key=value session-level phase timings)
  <run_dir>/iter_<n>/timings.json  (per-step timings parsed from maestro debug log)

Writes (timestamped at run-completion moment) into the parent of <run_dir>:
  local_<platform>_final_report_<TS>.csv     long, aggregated across sessions
  local_<platform>_sessions_report_<TS>.csv  wide, one row per session

Step-name canonicalization is tied to the per-platform benchmark flow shape:
  ios     -> HelloBench search loop (tap searchField, input "Browser",
             assertVisible id item_BrowserStack)
  android -> Wikipedia search loop (optional "OK" dismiss, tap search_container,
             input "BrowserStack", assertVisible text "Software company...")
Flow-shape changes need the per-platform config below updated.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path


# Per-platform configuration. Each platform wires:
#   filename_prefix      : "local_ios" / "local_android"
#   canon_rules          : ordered (prefix, canonical_label) pairs used by
#                          canonical_step_name to collapse verbose maestro
#                          debug-log step names to stable labels
#   phase_order          : rows in the long aggregated report (display order)
#   pre_loop_step_cols   : (column_name, canonical_step_label) emitted as
#                          single-value columns in the wide sessions report
#   rep_step_cols        : (column_prefix, canonical_step_label) emitted as
#                          p50/p90 column pairs in the wide sessions report
#   per_rep_components   : the three loop-body step labels summed into the
#                          synthesized "Per-rep total" series
PLATFORM_CONFIG: dict[str, dict] = {
    "ios": {
        "filename_prefix": "local_ios",
        "canon_rules": [
            ("Launch app", "Launch app"),
            ("Tap on id:", "Tap on searchField"),
            ("Input text", "Input text Browser"),
            ("Assert that id:", "Assert item_BrowserStack visible"),
        ],
        "phase_order": [
            ("Pre-Maestro", "device_readiness"),
            ("Pre-Maestro", "app_install"),
            ("Maestro startup", "maestro_init"),
            ("Maestro startup", "driver_setup"),
            ("Maestro startup", "device_info"),
            ("Flow execution", "Define variables"),
            ("Flow execution", "Apply configuration"),
            ("Flow execution", "Launch app"),
            ("Flow execution", "Tap on searchField"),
            ("Flow execution", "Erase text"),
            ("Flow execution", "Input text Browser"),
            ("Flow execution", "Assert item_BrowserStack visible"),
            ("Flow execution", "Per-rep total"),
            ("Post-Maestro", "stop"),
            ("Aggregates", "maestro_total"),
            ("Aggregates", "session_total"),
        ],
        "pre_loop_step_cols": [
            ("launch_app_ms", "Launch app"),
            ("tap_searchField_ms", "Tap on searchField"),
        ],
        "rep_step_cols": [
            ("erase_text", "Erase text"),
            ("input_text", "Input text Browser"),
            ("assert_visible", "Assert item_BrowserStack visible"),
            ("per_rep_total", "Per-rep total"),
        ],
        "per_rep_components": ("Erase text", "Input text Browser",
                               "Assert item_BrowserStack visible"),
    },
    "android": {
        "filename_prefix": "local_android",
        "canon_rules": [
            ("Launch app", "Launch app"),
            # Order matters: "(Optional)" prefix is checked before bare "Tap on id:"
            # so the optional-dismiss step gets its own bucket.
            ("Tap on (Optional)", "Tap on OK dialog"),
            ("Tap on id:", "Tap on search container"),
            ("Input text", "Input text BrowserStack"),
            ("Assert that ", "Assert Software company text visible"),
        ],
        "phase_order": [
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
        ],
        "pre_loop_step_cols": [
            ("launch_app_ms", "Launch app"),
            ("tap_ok_dialog_ms", "Tap on OK dialog"),
            ("tap_search_container_ms", "Tap on search container"),
        ],
        "rep_step_cols": [
            ("erase_text", "Erase text"),
            ("input_text", "Input text BrowserStack"),
            ("assert_visible", "Assert Software company text visible"),
            ("per_rep_total", "Per-rep total"),
        ],
        "per_rep_components": ("Erase text", "Input text BrowserStack",
                               "Assert Software company text visible"),
    },
}


def canonical_step_name(name: str, rules: list[tuple[str, str]]) -> str:
    """Map verbose maestro debug-log step names to stable short labels via prefix rules."""
    for prefix, label in rules:
        if name.startswith(prefix):
            return label
    return name


def pct(values: list[int], p: float) -> float | None:
    """Linear-interpolation percentile (numpy default)."""
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    s = sorted(values)
    n = len(s)
    idx = (p / 100) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return s[lo] + (idx - lo) * (s[hi] - s[lo])


def stats_row(values: list[int]) -> dict:
    if not values:
        return {"N": 0, "min_ms": "", "p50_ms": "", "p75_ms": "",
                "p90_ms": "", "p95_ms": "", "p100_ms": ""}
    return {
        "N": len(values),
        "min_ms": int(min(values)),
        "p50_ms": int(pct(values, 50)),
        "p75_ms": int(pct(values, 75)),
        "p90_ms": int(pct(values, 90)),
        "p95_ms": int(pct(values, 95)),
        "p100_ms": int(max(values)),
    }


def collect_step_obs(steps: list[dict], rules: list[tuple[str, str]]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for s in steps:
        cn = canonical_step_name(s["name"], rules)
        out.setdefault(cn, []).append(int(s["ms"]))
    return out


def load_run(run_dir: Path, cfg: dict, exclude_failed: bool = False) -> list[dict]:
    """Enumerate iter_*/meta.txt under run_dir; pair each with timings.json.

    When exclude_failed=True, iters whose meta.txt records a non-zero
    exit_code are skipped — useful when a single bad iter (e.g. a flaky
    assertVisible miss) would otherwise drag down P50/P90 stats.
    """
    iter_dirs = sorted(
        [d for d in run_dir.iterdir() if d.is_dir() and d.name.startswith("iter_")],
        key=lambda d: int(d.name.split("_")[1]),
    )
    rep_a, rep_b, rep_c = cfg["per_rep_components"]
    loaded = []
    for iter_dir in iter_dirs:
        meta_path = iter_dir / "meta.txt"
        if not meta_path.exists():
            continue
        meta: dict[str, str] = {}
        for line in meta_path.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                meta[k] = v
        if exclude_failed and meta.get("exit_code", "0") != "0":
            print(f"  excluding {iter_dir.name} (exit_code={meta.get('exit_code')})",
                  file=sys.stderr)
            continue
        session_row = {
            "run_id": meta.get("run_id", ""),
            "iter": meta.get("iter", ""),
            "device_readiness_ms": meta.get("device_readiness_ms", ""),
            "app_install_ms": meta.get("app_install_ms", ""),
            "maestro_total_ms": meta.get("maestro_total_ms", ""),
            "maestro_start_ms": meta.get("maestro_start_ms", ""),
            "execution_ms": meta.get("execution_ms", ""),
            "stop_ms": meta.get("stop_ms", ""),
            "session_total_ms": meta.get("session_total_ms", ""),
            "exit_code": meta.get("exit_code", ""),
        }
        timings_path = iter_dir / "timings.json"
        timings = json.load(open(timings_path)) if timings_path.exists() else {}
        step_obs = collect_step_obs(timings.get("steps", []), cfg["canon_rules"])
        a = step_obs.get(rep_a, [])
        b = step_obs.get(rep_b, [])
        c = step_obs.get(rep_c, [])
        n = min(len(a), len(b), len(c))
        step_obs["Per-rep total"] = [a[k] + b[k] + c[k] for k in range(n)]
        loaded.append({
            "iter": meta.get("iter", ""),
            "session_row": session_row,
            "timings": timings,
            "step_obs": step_obs,
        })
    return loaded


def write_sessions_report(loaded: list[dict], output_path: Path, run_id: str, cfg: dict) -> None:
    columns = [
        "session_id",
        "device_readiness_ms", "app_install_ms",
        "maestro_init_ms", "driver_setup_ms", "device_info_ms",
        "define_variables_ms", "apply_configuration_ms",
    ]
    columns += [col for col, _label in cfg["pre_loop_step_cols"]]
    for prefix, _label in cfg["rep_step_cols"]:
        for stat in ("p50", "p90"):
            columns.append(f"{prefix}_{stat}_ms")
    columns += ["execution_ms", "maestro_total_ms", "stop_ms", "session_total_ms", "exit_code"]

    with open(output_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for sess in loaded:
            sr = sess["session_row"]
            t = sess["timings"]
            so = sess["step_obs"]
            row = {
                "session_id": sess.get("_cloud_session_id") or f"{run_id}_iter{sess['iter']}",
                "device_readiness_ms": sr.get("device_readiness_ms", ""),
                "app_install_ms": sr.get("app_install_ms", ""),
                "maestro_init_ms": t.get("maestro_init_ms", ""),
                "driver_setup_ms": t.get("driver_setup_ms", ""),
                "device_info_ms": t.get("device_info_ms", ""),
                "define_variables_ms": (so.get("Define variables") or [""])[0],
                "apply_configuration_ms": (so.get("Apply configuration") or [""])[0],
                "execution_ms": sr.get("execution_ms", ""),
                "maestro_total_ms": sr.get("maestro_total_ms", ""),
                "stop_ms": sr.get("stop_ms", ""),
                "session_total_ms": sr.get("session_total_ms", ""),
                "exit_code": sr.get("exit_code", ""),
            }
            for col, label in cfg["pre_loop_step_cols"]:
                row[col] = (so.get(label) or [""])[0]
            for prefix, label in cfg["rep_step_cols"]:
                s = stats_row(so.get(label, []))
                for stat in ("p50", "p90"):
                    row[f"{prefix}_{stat}_ms"] = s[f"{stat}_ms"]
            w.writerow(row)


def write_final_report(loaded: list[dict], output_path: Path, cfg: dict) -> None:
    phase_order = cfg["phase_order"]
    pools: dict[str, list[int]] = {label: [] for _, label in phase_order}
    flow_step_labels = {label for group, label in phase_order if group == "Flow execution"}

    for sess in loaded:
        sr = sess["session_row"]
        t = sess["timings"]
        so = sess["step_obs"]

        for col, label in (("device_readiness_ms", "device_readiness"),
                           ("app_install_ms", "app_install"),
                           ("stop_ms", "stop"),
                           ("maestro_total_ms", "maestro_total"),
                           ("session_total_ms", "session_total")):
            v = sr.get(col)
            if v and v not in ("", "-1"):
                pools[label].append(int(v))

        for key, label in (("maestro_init_ms", "maestro_init"),
                           ("driver_setup_ms", "driver_setup"),
                           ("device_info_ms", "device_info")):
            v = t.get(key)
            if v is not None:
                pools[label].append(int(v))

        for label in flow_step_labels:
            pools[label].extend(so.get(label, []))

    with open(output_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["phase_group", "phase_step", "N",
                                          "min_ms", "p50_ms", "p75_ms",
                                          "p90_ms", "p95_ms", "p100_ms"])
        w.writeheader()
        for group, label in phase_order:
            w.writerow({"phase_group": group, "phase_step": label,
                        **stats_row(pools[label])})


def load_cloud_run(cloud_data_path: Path, cfg: dict) -> list[dict]:
    """Load 100 cloud sessions from a JSON file produced by the BQ extractor.

    Expected JSON shape:
      [
        {
          "session_id": "...",
          "build_id":   "...",
          "status":     "passed" | "failed" | ...,
          "session_duration_s": 705,           # from BS REST API session.duration
          # Optional BigQuery phase fields (whatever the user has access to):
          "app_install_ms":     <int>,
          "firecmd_ms":         <int>,
          "mrunner_install_ms": <int>,
          "execution_s":        <number>
        },
        ...
      ]

    Cloud-side phases that have no local-report row are silently dropped.
    Local-report rows that have no cloud equivalent (per-step inner-loop
    timings, device_info, stop, etc.) come out blank.
    """
    rows = json.loads(cloud_data_path.read_text())
    loaded: list[dict] = []
    for i, r in enumerate(rows, start=1):
        # Cloud → local phase mapping. Each row goes into the same dict
        # shape that load_run() produces so the writers don't care which
        # source we came from.
        execution_ms = (
            int(round(r["execution_s"] * 1000)) if r.get("execution_s") is not None else None
        )
        # session_total: prefer summed BS phases if all present, else the BS
        # REST session.duration (whole-session wall clock).
        session_total_ms = (
            int(r["session_duration_s"]) * 1000
            if r.get("session_duration_s") is not None else None
        )
        maestro_total_ms = None
        firecmd = r.get("firecmd_ms")
        if firecmd is not None and execution_ms is not None:
            maestro_total_ms = int(firecmd) + execution_ms

        session_row = {
            "run_id": r.get("build_id", ""),
            "iter": str(i),
            # local-equivalent phase fields:
            "device_readiness_ms": "",
            "app_install_ms": r.get("app_install_ms", "") or "",
            "maestro_total_ms": maestro_total_ms if maestro_total_ms is not None else "",
            "maestro_start_ms": r.get("firecmd_ms", "") or "",
            "execution_ms": execution_ms if execution_ms is not None else "",
            "stop_ms": "",  # NULL in BQ for Maestro sessions
            "session_total_ms": session_total_ms if session_total_ms is not None else "",
            "exit_code": "0" if r.get("status") == "passed" else "1",
        }
        timings = {
            "maestro_init_ms": None,        # not exposed by cloud
            "driver_setup_ms": r.get("mrunner_install_ms"),
            "device_info_ms": None,
            "steps": [],                    # cloud has no per-step data
        }
        loaded.append({
            "iter": str(i),
            "session_row": session_row,
            "timings": timings,
            "step_obs": {},                 # empty → all step rows come out blank
            "_cloud_session_id": r.get("session_id", ""),
        })
    return loaded


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir",
                    help="Local mode: path to the per-run dir "
                         "(e.g. results/20260507_184648/) containing "
                         "iter_*/meta.txt + iter_*/timings.json")
    ap.add_argument("--cloud-data",
                    help="Cloud mode: path to a JSON file with BQ + BS REST "
                         "session rows (see load_cloud_run for schema). "
                         "Reports land alongside this file.")
    ap.add_argument("--platform", choices=sorted(PLATFORM_CONFIG.keys()), default="ios",
                    help="Which platform's flow shape this run came from. "
                         "Drives step canonicalization and output filename prefix.")
    ap.add_argument("--exclude-failed", action="store_true",
                    help="Local mode: skip iters whose meta.txt has a non-zero "
                         "exit_code. Use when a flaky iter would distort stats.")
    args = ap.parse_args()

    if (args.run_dir is None) == (args.cloud_data is None):
        print("ERROR: pass exactly one of --run-dir or --cloud-data", file=sys.stderr)
        return 2

    cfg = PLATFORM_CONFIG[args.platform]

    if args.cloud_data:
        cloud_path = Path(args.cloud_data)
        if not cloud_path.is_file():
            print(f"ERROR: {cloud_path} is not a file", file=sys.stderr)
            return 1
        loaded = load_cloud_run(cloud_path, cfg)
        results_dir = cloud_path.parent
        run_id = cloud_path.stem
        prefix = f"cloud_{cfg['filename_prefix'][len('local_'):]}" if cfg["filename_prefix"].startswith("local_") else f"cloud_{cfg['filename_prefix']}"
    else:
        run_dir = Path(args.run_dir)
        if not run_dir.is_dir():
            print(f"ERROR: {run_dir} is not a directory", file=sys.stderr)
            return 1
        loaded = load_run(run_dir, cfg, exclude_failed=args.exclude_failed)
        results_dir = run_dir.parent
        run_id = run_dir.name
        prefix = cfg["filename_prefix"]
        if args.exclude_failed:
            prefix = f"{prefix}_clean"

    if not loaded:
        print("WARN: no sessions loaded", file=sys.stderr)
        return 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_path = results_dir / f"{prefix}_final_report_{ts}.csv"
    sessions_path = results_dir / f"{prefix}_sessions_report_{ts}.csv"

    write_final_report(loaded, final_path, cfg)
    write_sessions_report(loaded, sessions_path, run_id, cfg)

    print(f"Wrote: {final_path}")
    print(f"Wrote: {sessions_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
