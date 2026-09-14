#!/usr/bin/env python3
"""
Classify each benchmark remediation sample as cold or warm, from evidence.

A sample is cold when a Lambda that served it started a fresh container: the
REPORT line of that exact invocation carries `Init Duration`. The link is exact,
not inferred from timing — execution history -> Lambda RequestId -> REPORT line —
so no duration threshold is involved. Picking a cutoff (say, "anything above
9s is cold") would bake the conclusion into the classification.

Each sample is re-identified the way the harness picked it (scenario bucket,
triggering event, longest remediating execution), and its duration is checked
against the value recorded in run-NN.json. A mismatch is reported, never
silently absorbed, because it would mean the classification describes a
different execution than the metric.

Classified: the primary metric of C1, C3, C5 (t_remediation), C2, C6
(t_classification), C4 and C8 (t_escalation), CTRL-05 (t_remediation). t_exposure and t_verified of C1/C5
come from the same detection execution, so they inherit its class.
t_detection happens before any Lambda runs and is not affected. CTRL-* and C7
time the whole test rather than one execution and are left unclassified.

Usage:
    python3 scripts/classify_cold_starts.py --results results/two-round
    python3 scripts/classify_cold_starts.py --results results
"""

import argparse
import glob
import json
import os
import re
import statistics

import boto3

REGION = "us-east-1"
SCENARIOS = {
    # scenario: (bucket prefix, input fields the harness matched, metric, which
    # match the harness kept). The pick rule has to mirror the harness exactly:
    # before the echo was filtered, a C2 bucket also had the PutBucketPublicAccessBlock
    # echo of the remediation its own teardown triggered, and taking the longest
    # match classified that echo instead of the execution the metric timed.
    "C1": ("compliance-inttest-c1-", {"event_name": "PutBucketPublicAccessBlock"}, "t_remediation", "longest"),
    "C5": ("compliance-inttest-c5-", {"event_name": "PutBucketPublicAccessBlock"}, "t_remediation", "longest"),
    "C3": ("compliance-inttest-c3-", {"trigger": "exception_ttl_expiry"}, "t_remediation", "earliest"),
    # Scenarios without remediation still run the Analyze Lambda, so a cold
    # container shows up in them too.
    "C2": ("compliance-inttest-c2-", {"event_name": "PutBucketPublicAccessBlock"}, "t_classification", "earliest"),
    "C6": ("compliance-inttest-c6-", {"event_name": "PutBucketPublicAccessBlock"}, "t_classification", "earliest"),
    "C4": ("bucket-inexistente-c4-", {"event_name": "PutBucketPublicAccessBlock"}, "t_escalation", "earliest"),
    "C8": ("compliance-inttest-c8-", {"event_name": "PutBucketPublicAccessBlock"}, "t_escalation", "earliest"),
    # Account-level: no bucket to key on, so the event name itself is the prefix.
    "CTRL-05": ("DisableEbsEncryptionByDefault", {"event_name": "DisableEbsEncryptionByDefault"}, "t_remediation", "earliest"),
    # Not classified: CTRL-* and C7 time the whole test (bucket setup,
    # CloudTrail waits, a stream propagation), not one execution, so there is no
    # single invocation whose container state could explain the sample.
}


def run_windows(results_dir):
    """(start, end] for each run, from the run files' write times.

    When the benchmark log is present, each run starts its own duration before
    its file was written. Without it, a run is assumed to start where the
    previous one ended — which also swallows an attempt that crashed in
    between (an expired token, say) and the bucket it left behind, whose
    executions then match the scenario's prefix.
    """
    files = sorted(glob.glob(os.path.join(results_dir, "run-*.json")))
    ends = [os.path.getmtime(f) for f in files]
    gaps = [b - a for a, b in zip(ends, ends[1:])]
    typical = statistics.median(gaps) if gaps else 700
    starts = [ends[0] - typical] + ends[:-1]

    log = os.path.join(results_dir, "benchmark.log")
    if os.path.exists(log):
        took = {f"run-{int(m.group(1)):02d}.json": float(m.group(2))
                for m in re.finditer(r"run (\d+): .*\(ok, ([\d.]+)s\)", open(log).read())}
        # A little slack for the process start before the suite's own clock.
        starts = [e - took[os.path.basename(f)] - 5 if os.path.basename(f) in took else s
                  for f, s, e in zip(files, starts, ends)]
    return [(os.path.basename(f), s, e) for f, s, e in zip(files, starts, ends)]


def list_executions(sfn, arn, since, until):
    out = []
    for page in sfn.get_paginator("list_executions").paginate(
            stateMachineArn=arn, PaginationConfig={"MaxItems": 5000}):
        for e in page["executions"]:
            t = e["startDate"].timestamp()
            if t < since:
                return out
            if t <= until:
                out.append(e)
    return out


def lambda_request_ids(sfn, execution_arn):
    """{'analyze': [...], 'execute': [...]} in call order.

    InvokeAnalyze is top-level and InvokeExecute runs inside a Map with
    MaxConcurrency=1, so invocations are strictly sequential and each
    TaskSucceeded belongs to the most recently scheduled Lambda.
    """
    ids, current, token = {"analyze": [], "execute": []}, None, None
    while True:
        kw = {"executionArn": execution_arn, "maxResults": 1000}
        if token:
            kw["nextToken"] = token
        page = sfn.get_execution_history(**kw)
        for ev in page["events"]:
            sched = ev.get("taskScheduledEventDetails")
            if sched and sched.get("resourceType") == "lambda":
                params = sched.get("parameters", "")
                current = "execute" if "-execute-" in params else "analyze" if "-analyze-" in params else None
            done = ev.get("taskSucceededEventDetails")
            if done and done.get("resourceType") == "lambda" and current:
                out = json.loads(done.get("output") or "{}")
                rid = (out.get("SdkResponseMetadata") or {}).get("RequestId") or \
                      ((out.get("SdkHttpMetadata") or {}).get("HttpHeaders") or {}).get("x-amzn-RequestId")
                if rid:
                    ids[current].append(rid)
                current = None
        token = page.get("nextToken")
        if not token:
            return ids


def report_lines(logs, function, since, until):
    """RequestId -> Init Duration in seconds (None when the container was warm)."""
    inits, token = {}, None
    while True:
        kw = {"logGroupName": f"/aws/lambda/compliance-loop-{function}-dev",
              "filterPattern": "REPORT", "startTime": int(since * 1000),
              "endTime": int(until * 1000)}
        if token:
            kw["nextToken"] = token
        page = logs.filter_log_events(**kw)
        for ev in page["events"]:
            rid = re.search(r"RequestId: ([0-9a-f-]+)", ev["message"])
            init = re.search(r"Init Duration: ([\d.]+) ms", ev["message"])
            if rid:
                inits[rid.group(1)] = float(init.group(1)) / 1000 if init else None
        token = page.get("nextToken")
        if not token:
            return inits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--out")
    args = ap.parse_args()

    session = boto3.Session(region_name=REGION)
    sfn, logs = session.client("stepfunctions"), session.client("logs")
    account = session.client("sts").get_caller_identity()["Account"]
    arn = f"arn:aws:states:{REGION}:{account}:stateMachine:compliance-loop-compliance-loop-dev"

    windows = run_windows(args.results)
    since, until = windows[0][1], windows[-1][2]
    execs = list_executions(sfn, arn, since, until)
    described = {}
    for e in execs:
        d = sfn.describe_execution(executionArn=e["executionArn"])
        described[e["executionArn"]] = (e, json.loads(d.get("input") or "{}"), d)

    inits = {fn: report_lines(logs, fn, since, until + 300) for fn in ("analyze", "execute")}

    samples, problems = [], []
    for run_file, start, end in windows:
        recorded = json.load(open(os.path.join(args.results, run_file)))["metrics"]
        for scenario, (prefix, selector, metric, pick) in SCENARIOS.items():
            value = (recorded.get(scenario) or {}).get(metric)
            if value is None:
                continue
            candidates = []
            for arn_, (e, inp, d) in described.items():
                if not (start < e["startDate"].timestamp() <= end) or not d.get("stopDate"):
                    continue
                if prefix not in json.dumps(inp):
                    continue
                if any(inp.get(k) != v for k, v in selector.items()):
                    continue
                candidates.append((round((d["stopDate"] - d["startDate"]).total_seconds(), 2),
                                   e["startDate"].timestamp(), arn_))
            if not candidates:
                problems.append(f"{run_file} {scenario}: no matching execution")
                continue
            if pick == "longest":
                duration, _, chosen = max(candidates)
            else:
                duration, _, chosen = min(candidates, key=lambda c: c[1])
            if abs(duration - value) > 0.011:
                problems.append(f"{run_file} {scenario}: execution lasted {duration}s, "
                                f"run file records {value}s")
                continue
            ids = lambda_request_ids(sfn, chosen)
            detail = {}
            for fn in ("analyze", "execute"):
                known = [(r, inits[fn].get(r, "missing")) for r in ids[fn]]
                detail[fn] = [{"request_id": r, "init_s": i} for r, i in known]
                if any(i == "missing" for _, i in known):
                    problems.append(f"{run_file} {scenario}: no REPORT line for a {fn} invocation")
            cold = any(inv["init_s"] not in (None, "missing")
                       for fn in detail for inv in detail[fn])
            samples.append({"run": run_file, "scenario": scenario, "metric": metric, "value": value,
                            "execution_arn": chosen, "cold": cold, "lambdas": detail})

    cold = [s for s in samples if s["cold"]]
    summary = {
        "samples": len(samples), "cold": len(cold), "warm": len(samples) - len(cold),
        "cold_samples": [f"{s['run']} {s['scenario']} {s['metric']}={s['value']}s" for s in cold],
        "problems": problems,
    }
    out = args.out or os.path.join(args.results, "cold_starts.json")
    with open(out, "w") as f:
        json.dump({"summary": summary, "samples": samples}, f, indent=2)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nwrote {out}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
