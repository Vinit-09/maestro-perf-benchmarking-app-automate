#!/usr/bin/env python3
"""Parse a Maestro 2.5 debug log into per-phase timings.

Emits JSON:
  {
    "maestro_init_ms":   ...,   # JVM start to "Selected device"  (debug reporter ready)
    "driver_setup_ms":   ...,   # "Selected device" -> "Running flow"  (port + driver/server)
    "device_info_ms":    ...,   # initial cachedDeviceInfo
    "start_time_ms":     ...,   # init + driver_setup + device_info  (analog of "firecmd_time")
    "execution_ms":      ...,   # first command RUNNING -> last command COMPLETED
    "steps": [{"name": ..., "ms": ...}, ...]
  }
"""
from __future__ import annotations
import json
import re
import sys
from datetime import datetime
from pathlib import Path

TS = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3}) ")


def t(line: str) -> int | None:
    m = TS.match(line)
    if not m:
        return None
    h, mi, s, ms = map(int, m.groups())
    return ((h * 3600) + (mi * 60) + s) * 1000 + ms


def parse(path: Path) -> dict:
    lines = path.read_text(errors="replace").splitlines()
    t_first = None
    t_selected = None
    t_running_flow = None
    t_get_device_info = None
    t_got_device_info = None

    steps: list[dict] = []
    open_step: tuple[str, int] | None = None  # (name, start_ms)
    t_first_cmd_start = None
    t_last_cmd_end = None

    for ln in lines:
        ts = t(ln)
        if ts is None:
            continue
        if t_first is None:
            t_first = ts
        if "Debug output path:" in ln and t_first is None:
            t_first = ts
        if "Selected device" in ln and t_selected is None:
            t_selected = ts
        if "Getting device info" in ln and t_get_device_info is None:
            t_get_device_info = ts
        if "Got device info:" in ln and t_got_device_info is None:
            t_got_device_info = ts
        if "Running flow" in ln and t_running_flow is None:
            t_running_flow = ts

        m = re.search(r"runCommands\$lambda\$\d+: (.+?) (RUNNING|COMPLETED|FAILED)\s*$", ln)
        if m:
            name, status = m.group(1), m.group(2)
            if status == "RUNNING":
                open_step = (name, ts)
                if t_first_cmd_start is None and name not in ("Define variables", "Apply configuration"):
                    t_first_cmd_start = ts
            elif status in ("COMPLETED", "FAILED") and open_step and open_step[0] == name:
                start_ms = open_step[1]
                steps.append({"name": name, "status": status, "ms": ts - start_ms})
                if name not in ("Define variables", "Apply configuration"):
                    t_last_cmd_end = ts
                open_step = None

    out = {
        "maestro_init_ms": (t_selected - t_first) if (t_first and t_selected) else None,
        "driver_setup_ms": (t_running_flow - t_selected) if (t_selected and t_running_flow) else None,
        "device_info_ms": (t_got_device_info - t_get_device_info) if (t_get_device_info and t_got_device_info) else None,
        "start_time_ms": (t_first_cmd_start - t_first) if (t_first and t_first_cmd_start) else None,
        "execution_ms": (t_last_cmd_end - t_first_cmd_start) if (t_first_cmd_start and t_last_cmd_end) else None,
        "log_total_ms": (t_last_cmd_end - t_first) if (t_first and t_last_cmd_end) else None,
        "steps": steps,
    }
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: parse_maestro_log.py <maestro.log>", file=sys.stderr)
        return 2
    print(json.dumps(parse(Path(sys.argv[1])), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
