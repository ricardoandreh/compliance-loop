#!/usr/bin/env python3
"""
Did any verification round read a stale bucket configuration?

A remediation that worked but is read back too soon looks, to the verification
round, like a remediation that did not work: the control is still violated and
the round escalates a non-convergence. So the question "is the read-after-write
consistent?" becomes countable: every verification round in a benchmark window,
per control, verified or not.

For each round it also reports the gap the read actually had: from the end of
the last remediation in the origin execution to the verification round's
InvokeAnalyze being scheduled. That is the margin the system gives consistency
without any explicit wait, and the number the result depends on — "no stale
read" only means something together with how soon the read happened.

A non-verified control is not automatically a stale read: it can also be a real
non-convergence. Those rounds are listed with their execution ARN so the
Analyze input (the configuration it fetched) can be inspected.

Usage:
    python3 scripts/verification_consistency.py --results results/two-round
    python3 scripts/verification_consistency.py --results results/no-wait
"""

import argparse
import json
import os
import statistics
import sys

import boto3

sys.path.insert(0, os.path.dirname(__file__))
from classify_cold_starts import REGION, list_executions, run_windows  # noqa: E402


def history(sfn, arn):
    events, token = [], None
    while True:
        kw = {"executionArn": arn, "maxResults": 1000}
        if token:
            kw["nextToken"] = token
        page = sfn.get_execution_history(**kw)
        events += page["events"]
        token = page.get("nextToken")
        if not token:
            return events


def last_remediation_end(events):
    """Timestamp of the last successful InvokeExecute in an execution."""
    current, last = None, None
    for ev in events:
        sched = ev.get("taskScheduledEventDetails")
        if sched and sched.get("resourceType") == "lambda":
            current = "-execute-" in sched.get("parameters", "")
        if ev.get("taskSucceededEventDetails") and current:
            last = ev["timestamp"]
            current = None
    return last


def first_analyze_scheduled(events):
    for ev in events:
        sched = ev.get("taskScheduledEventDetails")
        if sched and "-analyze-" in sched.get("parameters", ""):
            return ev["timestamp"]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    args = ap.parse_args()

    session = boto3.Session(region_name=REGION)
    sfn = session.client("stepfunctions")
    account = session.client("sts").get_caller_identity()["Account"]
    sm = f"arn:aws:states:{REGION}:{account}:stateMachine:compliance-loop-compliance-loop-dev"

    windows = run_windows(args.results)
    execs = list_executions(sfn, sm, windows[0][1], windows[-1][2] + 300)

    rounds, gaps, suspects = 0, [], []
    per_control = {}
    for e in execs:
        d = sfn.describe_execution(executionArn=e["executionArn"])
        inp = json.loads(d.get("input") or "{}")
        if inp.get("mode") != "verify":
            continue
        rounds += 1
        # Verification rounds name their controls (control_ids); before the
        # fix that stopped trusting definitions from the input, they carried
        # the definitions themselves (controls).
        remediated = inp.get("control_ids") or [c["control_id"]["S"] for c in inp.get("controls", [])]
        outcomes = {}
        if d.get("output"):
            outcomes = {o.get("control_id"): o["outcome"]
                        for o in json.loads(d["output"]).get("outcomes", []) if o.get("control_id")}
        for control in remediated:
            verdict = outcomes.get(control, f"not verified ({e['status']})")
            per_control.setdefault(control, {}).setdefault(verdict, 0)
            per_control[control][verdict] += 1
            if verdict != "verified":
                suspects.append({"execution": e["executionArn"], "control": control,
                                 "status": e["status"], "resource": inp.get("resource_id")})

        verify_events = history(sfn, e["executionArn"])
        origin = inp.get("origin_execution")
        if origin:
            written = last_remediation_end(history(sfn, origin))
            read = first_analyze_scheduled(verify_events)
            if written and read:
                gaps.append((read - written).total_seconds())

    summary = {
        "results": args.results,
        "verification_rounds": rounds,
        "controls_checked": {c: v for c, v in sorted(per_control.items())},
        "not_verified": suspects,
        "read_after_write_gap_s": None if not gaps else {
            "n": len(gaps), "mean": round(statistics.mean(gaps), 2),
            "median": round(statistics.median(gaps), 2),
            "min": round(min(gaps), 2), "max": round(max(gaps), 2)},
    }
    out = os.path.join(args.results, "verification_consistency.json")
    with open(out, "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {out}")
    return 1 if suspects else 0


if __name__ == "__main__":
    raise SystemExit(main())
