#!/usr/bin/env python3
"""Summarise one SAGE-Vision telemetry log captured with `tee`.

Parses the fixed-format telemetry lines emitted by `rpi_edge/pi_edge_node.py`
(adaptive) or `test/test_baseline_edge.py` (baseline) and prints the per-run
metrics for every benchmarking objective the software can measure: mean CPU,
peak temperature, mean active-frame latency, wake latency, and time-in-state.

Whole-Pi power/energy is NOT in the telemetry — it is read by hand off the
inline USB-C power meter's display during each run (see docs/TESTING.md), so it
is compared manually, not by this script.

Usage:
    python3 test/analyze_log.py ~/run_baseline.log
    python3 test/analyze_log.py ~/run_adaptive.log

Notes / caveats (see docs/TESTING.md):
  - Timestamps are 1-second resolution, so duration is approximate.
  - `---` (no inference latency in SLEEP/STANDBY) is treated as missing and
    excluded from the latency average.
  - The baseline log has no `dist` field; both formats parse fine.
"""

import re
import sys
from collections import defaultdict


def t_to_s(t):                       # "HH:MM:SS" -> seconds since midnight
    h, m, s = map(int, t.split(":"))
    return h * 3600 + m * 60 + s


def parse(path):
    rows = []
    with open(path) as f:
        for line in f:
            mt = re.match(r"\[(\d\d:\d\d:\d\d)\]", line)
            if not mt or "|" not in line:
                continue
            num = lambda pat: (float(m.group(1)) if (m := re.search(pat, line)) else None)
            rows.append({
                "t":     t_to_s(mt.group(1)),
                "state": line.split("]")[1].split("|")[0].strip(),
                "lat":   num(r"lat\s+([\d.]+)ms"),
                "cpu":   num(r"cpu\s+([\d.]+)%"),
                "temp":  num(r"temp\s+([\d.]+)C"),
            })
    return rows


def wake_latencies(path):
    """Wake-signal -> first-inference latencies (ms) from the [WAKE] log lines."""
    out = []
    with open(path) as f:
        for line in f:
            m = re.search(r"\[WAKE\].*?([\d.]+)\s*ms", line)
            if m:
                out.append(float(m.group(1)))
    return out


def main(path):
    rows = parse(path)
    if not rows:
        print(f"No telemetry lines found in {path}")
        return

    n = len(rows)
    dur = rows[-1]["t"] - rows[0]["t"] if n > 1 else 0
    mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    cpu = [r["cpu"] for r in rows if r["cpu"] is not None]
    temp = [r["temp"] for r in rows if r["temp"] is not None]
    lat = [r["lat"] for r in rows if r["lat"] is not None]   # active frames only

    # Time-in-state, using the timestamp gaps.
    secs = defaultdict(float)
    for a, b in zip(rows, rows[1:]):
        dt = b["t"] - a["t"]
        if not (0 <= dt < 30):       # skip gaps / midnight wrap
            continue
        secs[a["state"]] += dt

    print(f"file              : {path}")
    print(f"samples / duration: {n} lines / {dur} s")
    print(f"mean CPU %        : {mean(cpu):.1f}")
    print(f"mean temp C       : {mean(temp):.1f}" if temp else "mean temp C       : n/a")
    print(f"max temp C        : {max(temp):.1f}" if temp else "max temp C        : n/a")
    print(f"mean latency ms   : {mean(lat):.1f}  (active frames only)" if lat else "mean latency ms   : n/a")
    wl = wake_latencies(path)
    if wl:
        print(f"wake latency ms   : mean {mean(wl):.0f}, min {min(wl):.0f}, max {max(wl):.0f}  (n={len(wl)})")
    print("power W           : read manually off the inline USB-C meter (not in the log)")
    print("time in state:")
    for s in sorted(secs, key=secs.get, reverse=True):
        pct = 100 * secs[s] / dur if dur else 0
        print(f"  {s:<10}: {secs[s]:5.0f} s ({pct:.0f}%)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python3 test/analyze_log.py <telemetry.log>")
        sys.exit(1)
    main(sys.argv[1])
