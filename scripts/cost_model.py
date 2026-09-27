#!/usr/bin/env python3
"""
What the loop costs, per incident and per month, from what one series consumed.

Every variable quantity is measured in the account over a benchmark series —
state transitions per execution, billed Lambda time per function, bytes of logs
ingested, audit rows written — and turned into money with list prices. Nothing
is estimated from the architecture diagram.

Prices are us-east-1 list prices (September 2026), kept in PRICES below so they
can be checked against the AWS calculator and updated in one place. Free tiers
are ignored on purpose: the question is what the design costs, not what one
small account pays.

"Incident" = one violation that is remediated and verified: a detection round
plus its verification round. The suite is not made only of incidents (it also
creates buckets, evaluates compliant resources, escalates), so the per-incident
figure divides the series' total by the number of detection rounds that
remediated — an upper bound on the cost of one remediated violation.

Usage:
    python3 scripts/cost_model.py --results results/final
"""

import argparse
import collections
import datetime
import json
import os
import re
import sys

import boto3

sys.path.insert(0, os.path.dirname(__file__))
from classify_cold_starts import REGION, list_executions, run_windows  # noqa: E402

PRICES = {
    "sfn_transition": 0.025 / 1000,            # Step Functions Standard, per transition
    "lambda_gb_s_arm": 0.0000133334,           # Lambda arm64, per GB-second
    "lambda_request": 0.20 / 1_000_000,        # per invocation
    "ddb_write": 0.625 / 1_000_000,            # DynamoDB on-demand, per write request unit
    "ddb_read": 0.125 / 1_000_000,             # per read request unit
    "logs_ingest_gb": 0.50,                    # CloudWatch Logs ingestion, per GB
    "sqs_fifo_request": 0.50 / 1_000_000,
    # Fixed, per month
    "alarm_month": 0.10,                       # standard-resolution alarm
    "dashboard_month": 3.00,
    "pitr_gb_month": 0.20,
}
# The functions whose billed time is priced. Memory is read from each REPORT
# line ("Memory Size"), not assumed: a fixed table priced the Execute at 2 GB
# after it moved to 3008 MB, understating the optimized series.
FUNCTIONS = ("analyze", "execute", "escalation", "controls-sync", "exception-scheduler-sync")
SM = "compliance-loop-compliance-loop-dev"


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


def billed(logs, function, since_ms, until_ms):
    """(invocations, billed GB-seconds) from the REPORT lines."""
    n, gb_seconds, token = 0, 0.0, None
    while True:
        kw = {"logGroupName": f"/aws/lambda/compliance-loop-{function}-dev", "filterPattern": "REPORT",
              "startTime": since_ms, "endTime": until_ms}
        if token:
            kw["nextToken"] = token
        page = logs.filter_log_events(**kw)
        for ev in page["events"]:
            m = re.search(r"Billed Duration: (\d+) ms", ev["message"])
            mem = re.search(r"Memory Size: (\d+) MB", ev["message"])
            if m and mem:
                n += 1
                gb_seconds += int(m.group(1)) / 1000 * int(mem.group(1)) / 1024
        token = page.get("nextToken")
        if not token:
            return n, gb_seconds


def metric_period(start):
    """The finest period CloudWatch still keeps for data this old.

    A daily period summed the whole day, so two series run on the same day were
    each charged the other's logs (the upgrade series read 174 MB instead of 57).
    """
    age = datetime.datetime.now(datetime.timezone.utc) - start
    if age < datetime.timedelta(days=15):
        return 60
    return 300 if age < datetime.timedelta(days=63) else 3600


def ingested_bytes(cw, group, start, end):
    r = cw.get_metric_statistics(Namespace="AWS/Logs", MetricName="IncomingBytes",
                                 Dimensions=[{"Name": "LogGroupName", "Value": group}],
                                 StartTime=start, EndTime=end, Period=metric_period(start),
                                 Statistics=["Sum"])
    return sum(p["Sum"] for p in r["Datapoints"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    args = ap.parse_args()

    s = boto3.Session(region_name=REGION)
    sfn, logs, cw = s.client("stepfunctions"), s.client("logs"), s.client("cloudwatch")
    account = s.client("sts").get_caller_identity()["Account"]
    sm_arn = f"arn:aws:states:{REGION}:{account}:stateMachine:{SM}"

    windows = run_windows(args.results)
    since, until = windows[0][1], windows[-1][2] + 300
    start = datetime.datetime.fromtimestamp(since, datetime.timezone.utc)
    end = datetime.datetime.fromtimestamp(until, datetime.timezone.utc)

    # State transitions, by kind of execution
    kinds = collections.defaultdict(lambda: {"executions": 0, "transitions": 0})
    remediating_detections = 0
    for e in list_executions(sfn, sm_arn, since, until):
        d = sfn.describe_execution(executionArn=e["executionArn"])
        mode = json.loads(d.get("input") or "{}").get("mode") or "detect"
        ev = history(sfn, e["executionArn"])
        entered = [x["stateEnteredEventDetails"]["name"] for x in ev if x["type"].endswith("StateEntered")]
        kinds[mode]["executions"] += 1
        kinds[mode]["transitions"] += len(entered)
        if mode == "detect" and "ScheduleVerification" in entered:
            remediating_detections += 1

    functions = {f: billed(logs, f, int(since * 1000), int(until * 1000)) for f in FUNCTIONS}
    groups = {"step-functions": f"/aws/vendedlogs/states/{SM}",
              **{f: f"/aws/lambda/compliance-loop-{f}-dev" for f in FUNCTIONS}}
    log_bytes = {k: ingested_bytes(cw, g, start, end) for k, g in groups.items()}

    transitions = sum(k["transitions"] for k in kinds.values())
    variable = {
        "step_functions": transitions * PRICES["sfn_transition"],
        "lambda_compute": sum(gbs for _, gbs in functions.values()) * PRICES["lambda_gb_s_arm"],
        "lambda_requests": sum(n for n, _ in functions.values()) * PRICES["lambda_request"],
        "logs_ingestion": sum(log_bytes.values()) / 1e9 * PRICES["logs_ingest_gb"],
    }
    total = sum(variable.values())
    per_incident = {k: v / remediating_detections for k, v in variable.items()} if remediating_detections else {}

    summary = {
        "results": args.results,
        "prices_usd": PRICES,
        "executions": {k: dict(v) for k, v in kinds.items()},
        "transitions_per_execution": {k: round(v["transitions"] / v["executions"], 1) for k, v in kinds.items()},
        "remediating_detections": remediating_detections,
        "lambda": {f: {"invocations": n, "gb_seconds": round(g, 1)} for f, (n, g) in functions.items()},
        "log_mb": {k: round(v / 1e6, 2) for k, v in log_bytes.items()},
        "series_cost_usd": {k: round(v, 4) for k, v in variable.items()} | {"total": round(total, 4)},
        "per_remediated_incident_usd": {k: round(v, 6) for k, v in per_incident.items()}
                                       | {"total": round(sum(per_incident.values()), 6)},
        "share_of_variable_cost": {k: round(v / total, 3) for k, v in variable.items()},
    }
    out = os.path.join(args.results, "cost_model.json")
    with open(out, "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
