#!/usr/bin/env python3
"""
Collect the facts about a benchmark series that only exist in the AWS account.

Most of a series is analysed offline from its run-NN.json files, but three
facts come from the account and expire with it — Step Functions keeps
execution history for 90 days, CloudWatch Logs as long as its retention:

- executions per suite round (Step Functions history);
- how many Execute invocations each Lambda container served, and how many of
  them ran in the fast regime (REPORT lines);
- which environment served each remediating sample, so a claim about one
  scenario's cost can be checked in the regime it actually ran in.

Run this once per series while the history is still there; it writes
<results>/series_facts.json, and from then on the series can be analysed from
the repository alone (compare_series.py, build_report_data.py).

"Fast" is a descriptive split of an Execute duration distribution that is
clearly bimodal (~4.2 s against ~5.3 s, nothing in between), not a
classification threshold: the container each invocation ran in is recorded next
to it, so the regime can be reported by container.

Usage:
    python3 scripts/collect_series_facts.py --results results/no-wait
"""

import argparse
import json
import os
import re
import statistics
import sys

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classify_cold_starts import REGION, lambda_request_ids, list_executions, run_windows  # noqa: E402
import deployment  # noqa: E402

# Below this gap between the two tiers, a series is taken to have one tier.
MIN_TIER_GAP_S = 0.5


def fast_threshold(durations):
    """Where the fast execution environment ends, from this series' own data.

    The Execute runs in one of two tiers depending on the host (L12), about a
    second apart. A fixed cut-off (4.8 s, fitted to the first series) held
    until the dependency upgrade moved both tiers up: 5.1-5.4 s became the
    fast tier and the cut-off called every sample slow, mixing the tiers in
    the comparison. The cut-off is now the middle of the widest gap between
    consecutive warm durations, when that gap is wider than MIN_TIER_GAP_S;
    otherwise the series has a single tier and nothing is fast. Cold
    invocations (over 1.5x the median) are left out: they are a third regime.
    """
    warm = sorted(d for d in durations if d < 1.5 * statistics.median(durations))
    gaps = [(b - a, (a + b) / 2) for a, b in zip(warm, warm[1:])]
    if not gaps:
        return 0.0
    width, middle = max(gaps)
    return round(middle, 3) if width >= MIN_TIER_GAP_S else 0.0
FUNCTION = deployment.name("execute")


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
    arn = f"arn:aws:states:{REGION}:{account}:stateMachine:{deployment.STATE_MACHINE}"

    windows = run_windows(args.results)
    per_round = [len(list_executions(sfn, arn, s, e)) for _, s, e in windows]

    since, until = windows[0][1], windows[-1][2] + 300
    invocations = report_lines(logs, since, until)

    samples = {}
    cold_path = os.path.join(args.results, "cold_starts.json")
    if os.path.exists(cold_path):
        for s in json.load(open(cold_path))["samples"]:
            # Every scenario whose metric times an Execute invocation: the S3
            # paths, the EBS one (CTRL-05) and the denied remediation (C8).
            # Leaving CTRL-05 out once made a published comparison between
            # services unverifiable — its mean mixed both regimes.
            if s["metric"] not in ("t_remediation", "t_escalation"):
                continue
            if s["scenario"] in ("C4", "C9", "CTRL-06"):
                continue
            ids = [x["request_id"] for x in s["lambdas"]["execute"]]
            if not ids or ids[0] not in invocations:
                continue
            stream, dur = invocations[ids[0]]
            samples[f"{s['run']}/{s['scenario']}"] = {
                "container": stream[-6:], "execute_s": round(dur, 3), "cold": s["cold"]}

    # The cut-off comes from the samples being compared - one kind of
    # remediation each - not from every Execute invocation, whose mix of
    # remediations blurs the two tiers into one spread.
    FAST_EXECUTE_S = fast_threshold([x["execute_s"] for x in samples.values() if not x["cold"]])
    for x in samples.values():
        x["fast"] = (not x.pop("cold")) and x["execute_s"] < FAST_EXECUTE_S
    containers = {}
    for stream, dur in invocations.values():
        c = containers.setdefault(stream[-6:], {"invocations": 0, "fast": 0})
        c["invocations"] += 1
        c["fast"] += dur < FAST_EXECUTE_S

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
