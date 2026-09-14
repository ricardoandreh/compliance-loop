#!/usr/bin/env python3
"""
Collect the facts about a benchmark series that only exist in the AWS account.

The results page is rendered offline (render_report.py), but three of its
findings come from the account and expire with it — Step Functions keeps
execution history for 90 days, CloudWatch Logs as long as its retention:

- executions per suite round (Step Functions history);
- how many Execute invocations each Lambda container served, and how many of
  them ran in the fast regime (REPORT lines);
- which container served the remediation of each C1/C3/C5 sample.

Run this once per series while the history is still there; it writes
<results>/series_facts.json, and from then on the page can be rebuilt from the
repository alone.

"Fast" is a descriptive split of an Execute duration distribution that is
clearly bimodal (~4.2 s against ~5.3 s, nothing in between), not a
classification threshold: the container each invocation ran in is recorded next
to it, and the page reports the regime by container.

Usage:
    python3 scripts/report/collect_series_facts.py --results results/no-wait
"""

import argparse
import json
import os
import re
import statistics
import sys

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from classify_cold_starts import REGION, lambda_request_ids, list_executions, run_windows  # noqa: E402

FAST_EXECUTE_S = 4.8
FUNCTION = "compliance-loop-execute-dev"


def report_lines(logs, since, until):
    """RequestId -> (log stream, duration s) for every Execute invocation."""
    out, token = {}, None
    while True:
        kw = {"logGroupName": f"/aws/lambda/{FUNCTION}", "filterPattern": "REPORT",
              "startTime": int(since * 1000), "endTime": int(until * 1000)}
        if token:
            kw["nextToken"] = token
        page = logs.filter_log_events(**kw)
        for ev in page["events"]:
            rid = re.search(r"RequestId: (\S+)", ev["message"]).group(1)
            dur = float(re.search(r"Duration: ([\d.]+) ms", ev["message"]).group(1)) / 1000
            out[rid] = (ev["logStreamName"], dur)
        token = page.get("nextToken")
        if not token:
            return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    args = ap.parse_args()

    session = boto3.Session(region_name=REGION)
    sfn, logs = session.client("stepfunctions"), session.client("logs")
    account = session.client("sts").get_caller_identity()["Account"]
    arn = f"arn:aws:states:{REGION}:{account}:stateMachine:compliance-loop-compliance-loop-dev"

    windows = run_windows(args.results)
    per_round = [len(list_executions(sfn, arn, s, e)) for _, s, e in windows]

    since, until = windows[0][1], windows[-1][2] + 300
    invocations = report_lines(logs, since, until)
    containers = {}
    for stream, dur in invocations.values():
        c = containers.setdefault(stream[-6:], {"invocations": 0, "fast": 0})
        c["invocations"] += 1
        c["fast"] += dur < FAST_EXECUTE_S

    samples = {}
    cold_path = os.path.join(args.results, "cold_starts.json")
    if os.path.exists(cold_path):
        for s in json.load(open(cold_path))["samples"]:
            if s["scenario"] not in ("C1", "C3", "C5") or s["metric"] != "t_remediation":
                continue
            ids = [x["request_id"] for x in s["lambdas"]["execute"]]
            if not ids or ids[0] not in invocations:
                continue
            stream, dur = invocations[ids[0]]
            samples[f"{s['run']}/{s['scenario']}"] = {
                "container": stream[-6:], "execute_s": round(dur, 3),
                "fast": (not s["cold"]) and dur < FAST_EXECUTE_S}

    durations = []
    log_path = os.path.join(args.results, "benchmark.log")
    if os.path.exists(log_path):
        durations = [float(x) for x in re.findall(r"\(ok, ([\d.]+)s\)", open(log_path).read())]

    facts = {
        "executions_per_round": round(statistics.mean(per_round), 1),
        "executions_by_round": per_round,
        "round_duration_s": ({"median": round(statistics.median(durations), 1),
                              "min": min(durations), "max": max(durations)} if durations else None),
        "execute_invocations": {
            "total": sum(c["invocations"] for c in containers.values()),
            "fast": sum(c["fast"] for c in containers.values()),
            "fast_threshold_s": FAST_EXECUTE_S,
            "containers": containers,
        },
        "remediation_samples": samples,
    }
    out = os.path.join(args.results, "series_facts.json")
    with open(out, "w") as f:
        json.dump(facts, f, indent=2)
    print(json.dumps({k: v for k, v in facts.items() if k != "remediation_samples"}, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
