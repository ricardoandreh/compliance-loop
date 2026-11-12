#!/usr/bin/env python3
"""
How well each Lambda is sized, from what it actually used during a series.

For every function of the loop: configured memory against the maximum memory
any invocation used, and the duration distribution (p50, p99, max) against the
timeout — read from the REPORT lines Lambda writes for every invocation inside
the series window. Init Duration is averaged over the cold invocations.

Memory on Lambda also buys CPU (about one vCPU at 1769 MB), so a low memory
share does not by itself mean a function is oversized: a CPU-bound function can
get faster with more. The table says where to look, not what to set.

Usage:
    python3 scripts/lambda_sizing.py --results results/upgrade
"""

import argparse
import json
import os
import re
import statistics
import sys

import boto3

sys.path.insert(0, os.path.dirname(__file__))
from classify_cold_starts import REGION, run_windows  # noqa: E402
import deployment  # noqa: E402

FUNCTIONS = ("analyze", "execute", "escalation", "controls-sync", "exception-scheduler-sync")


def reports(logs, name, since_ms, until_ms):
    token = None
    while True:
        kw = dict(logGroupName=f"/aws/lambda/{name}", filterPattern="REPORT",
                  startTime=since_ms, endTime=until_ms)
        if token:
            kw["nextToken"] = token
        page = logs.filter_log_events(**kw)
        yield from (e["message"] for e in page["events"])
        token = page.get("nextToken")
        if not token:
            return


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(p * len(values)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    args = ap.parse_args()

    session = boto3.Session(region_name=REGION)
    logs, lam = session.client("logs"), session.client("lambda")
    windows = run_windows(args.results)
    since, until = int(windows[0][1] * 1000), int(windows[-1][2] * 1000)

    table = {}
    for f in FUNCTIONS:
        name = deployment.name(f)
        cfg = lam.get_function_configuration(FunctionName=name)
        durations, memory, inits = [], [], []
        for m in reports(logs, name, since, until):
            durations.append(float(re.search(r"\tDuration: ([\d.]+) ms", m).group(1)) / 1000)
            memory.append(int(re.search(r"Max Memory Used: (\d+) MB", m).group(1)))
            init = re.search(r"Init Duration: ([\d.]+) ms", m)
            if init:
                inits.append(float(init.group(1)) / 1000)
        if not durations:
            continue
        table[f] = {
            "memory_mb": cfg["MemorySize"], "max_used_mb": max(memory),
            "memory_share": round(max(memory) / cfg["MemorySize"], 2),
            "invocations": len(durations),
            "duration_s": {"p50": round(statistics.median(durations), 2),
                           "p99": round(pct(durations, 0.99), 2), "max": round(max(durations), 2)},
            "timeout_s": cfg["Timeout"],
            "init_s": round(statistics.mean(inits), 2) if inits else None, "cold": len(inits),
        }

    print(f"{'função':26} {'mem':>6} {'usada':>6} {'uso':>5} {'n':>5} {'p50':>6} {'p99':>6} {'máx':>6} {'timeout':>7}")
    for f, r in table.items():
        d = r["duration_s"]
        print(f"{f:26} {r['memory_mb']:>6} {r['max_used_mb']:>6} {r['memory_share']:>5.0%} {r['invocations']:>5} "
              f"{d['p50']:>6.2f} {d['p99']:>6.2f} {d['max']:>6.2f} {r['timeout_s']:>7}")
    out = os.path.join(args.results, "lambda_sizing.json")
    with open(out, "w") as fh:
        json.dump(table, fh, indent=2)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
