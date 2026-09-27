#!/usr/bin/env python3
"""
Measure candidate optimizations of the remediation path, one variant at a time.

Each variant is a configuration of the deployed functions — Execute memory, the
switches in lambdas/execute/handler.py (how the scoped S3 client is built,
whether the provider cache is fetched at init, gzip or zstd cache), the
provider block the controls' remediation.tf publish, and the Analyze memory.
For each one the script configures the account, runs the C1 scenario several
times (forcing a fresh execution environment before some runs, so cold starts
are sampled on purpose), and reads back, from the functions' own logs, the
phase timings the handlers print and the REPORT line of every invocation.

A C1 run invokes the Execute three times: the bucket's creation fans out to the
S3 controls and remediates versioning and ssl-only (the one that reads the
bucket policy through the scoped client), and the violation itself remediates
public-block.

Everything it changes it puts back at the end: function configuration and the
published remediation.tf. The configuration it changes is also declared in
Terraform, so a plan after the experiment must say "No changes".

Usage:
    python3 scripts/perf_experiment.py --out results/perf
    python3 scripts/perf_experiment.py --out results/perf --only baseline,lean
"""

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import uuid

import boto3

REGION = "us-east-1"
EXECUTE = "compliance-loop-execute-dev"
ANALYZE = "compliance-loop-analyze-dev"
BUCKET = "compliance-mvp-terraform-state"
CONTROLS = ["ctrl-s3-public-block", "ctrl-s3-ssl-only", "ctrl-s3-versioning", "ctrl-s3-encryption",
            "ctrl-ebs-encryption-default"]
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
GZ, ZST = "provider-cache/tf-dot-terraform.tar.gz", "provider-cache/tf-dot-terraform.tar.zst"

# Oldest code path first; each variant changes one thing over the one it names.
VARIANTS = [
    ("baseline", dict(memory=2048, scoped="eager", init="0", cache=GZ, skip=False, analyze=512)),
    ("lean", dict(memory=2048, scoped="lazy", init="0", cache=GZ, skip=True, analyze=512)),
    ("lean+zstd-init", dict(memory=2048, scoped="lazy", init="1", cache=ZST, skip=True, analyze=512)),
    ("mem-1769", dict(memory=1769, scoped="lazy", init="1", cache=ZST, skip=True, analyze=512)),
    ("mem-3008", dict(memory=3008, scoped="lazy", init="1", cache=ZST, skip=True, analyze=512)),
    ("mem-4096", dict(memory=4096, scoped="lazy", init="1", cache=ZST, skip=True, analyze=512)),
    ("analyze-256", dict(memory=2048, scoped="lazy", init="1", cache=ZST, skip=True, analyze=256)),
]
SKIP = ('  skip_credentials_validation = true\n  skip_requesting_account_id  = true\n'
        '  skip_metadata_api_check     = true\n')


def provider_block(tf, skip):
    """The remediation.tf with or without the provider's start-up checks.

    The block is delimited by counting braces, so nested blocks inside it
    (default_tags, assume_role) are kept whole; only top-level skip_ lines change.
    """
    start = tf.index('provider "aws" {')
    depth, i = 0, tf.index("{", start)
    for j in range(i, len(tf)):
        depth += {"{": 1, "}": -1}.get(tf[j], 0)
        if depth == 0:
            break
    body, level = "", 0
    for ln in tf[i + 1:j].splitlines():
        text = ln.strip()
        if not text or (level == 0 and text.startswith("skip_")):
            continue
        body += ("  " + text if level == 0 else ln.rstrip()) + "\n"
        level += text.count("{") - text.count("}")
    return tf[:start] + 'provider "aws" {\n' + body + (SKIP if skip else "") + "}" + tf[j + 1:]


def publish_controls(s3, skip):
    for c in CONTROLS:
        tf = open(os.path.join(ROOT, "controls", c, "remediation.tf")).read()
        s3.put_object(Bucket=BUCKET, Key=f"controls/{c}/remediation.tf", Body=provider_block(tf, skip).encode())


def wait_updated(lam, fn):
    while lam.get_function_configuration(FunctionName=fn)["LastUpdateStatus"] == "InProgress":
        time.sleep(2)


def configure(lam, v, bust):
    env = lam.get_function_configuration(FunctionName=EXECUTE)["Environment"]["Variables"]
    env.update(SCOPED_CLIENT_MODE=v["scoped"], CACHE_AT_INIT=v["init"], PROVIDER_CACHE_KEY=v["cache"])
    if bust:
        env["COLD_BUST"] = uuid.uuid4().hex[:8]  # a new configuration means new environments
    lam.update_function_configuration(FunctionName=EXECUTE, MemorySize=v["memory"],
                                      Environment={"Variables": env})
    wait_updated(lam, EXECUTE)
    lam.update_function_configuration(FunctionName=ANALYZE, MemorySize=v["analyze"])
    wait_updated(lam, ANALYZE)


def restore(lam, s3, original):
    for fn, cfg in original.items():
        lam.update_function_configuration(FunctionName=fn, MemorySize=cfg["MemorySize"],
                                          Environment=cfg["Environment"])
        wait_updated(lam, fn)
    for c in CONTROLS:
        s3.upload_file(os.path.join(ROOT, "controls", c, "remediation.tf"), BUCKET, f"controls/{c}/remediation.tf")


def invocations(logs, fn, since_ms, until_ms):
    """One record per invocation: the handler's timing line joined to its REPORT."""
    events, token = [], None
    while True:
        kw = dict(logGroupName=f"/aws/lambda/{fn}", startTime=since_ms, endTime=until_ms,
                  filterPattern='?"\\"timings\\"" ?REPORT')
        if token:
            kw["nextToken"] = token
        page = logs.filter_log_events(**kw)
        events += page["events"]
        token = page.get("nextToken")
        if not token:
            break
    out, pending = [], {}
    for e in sorted(events, key=lambda e: (e["logStreamName"], e["timestamp"])):
        msg = e["message"].strip()
        if msg.startswith("{") and '"timings"' in msg:
            pending[e["logStreamName"]] = json.loads(msg)
        elif msg.startswith("REPORT"):
            rec = pending.pop(e["logStreamName"], None)
            if rec is None:
                continue
            rec["billed_ms"] = int(re.search(r"Billed Duration: (\d+) ms", msg).group(1))
            rec["max_mem_mb"] = int(re.search(r"Max Memory Used: (\d+) MB", msg).group(1))
            init = re.search(r"Init Duration: ([\d.]+) ms", msg)
            rec["init_s"] = float(init.group(1)) / 1000 if init else None
            out.append(rec)
    return out


def summary(values):
    if not values:
        return None
    return {"n": len(values), "mean": round(statistics.mean(values), 3),
            "median": round(statistics.median(values), 3),
            "sd": round(statistics.stdev(values), 3) if len(values) > 1 else None}


def run_variant(name, v, lam, s3, logs, runs, cold_runs, out_dir):
    print(f"== {name}: {v}", flush=True)
    publish_controls(s3, v["skip"])
    configure(lam, v, bust=True)
    subprocess.run([sys.executable, os.path.join(ROOT, "scripts/integration_test.py"), "--test", "C1"],
                   capture_output=True)  # warm-up: the first run after a change is cold anyway
    since = int(time.time() * 1000)
    c1 = []
    for i in range(runs):
        if i in cold_runs:
            configure(lam, v, bust=True)
        path = os.path.join(out_dir, f".{name}-{i}.json")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts/integration_test.py"),
                            "--test", "C1", "--json-out", path], capture_output=True, text=True)
        m = json.load(open(path))["metrics"].get("C1", {}) if os.path.exists(path) else {}
        os.path.exists(path) and os.remove(path)
        c1.append(m.get("t_remediation"))
        print(f"   run {i + 1}: C1 {'PASS' if r.returncode == 0 else 'FAIL'} t_remediation={m.get('t_remediation')}", flush=True)
    until = int(time.time() * 1000) + 60_000
    time.sleep(20)  # let the last log lines land
    ex = invocations(logs, EXECUTE, since, until)
    an = invocations(logs, ANALYZE, since, until)
    warm = [x for x in ex if not x["cold"]]
    cold = [x for x in ex if x["cold"]]
    phases = sorted({k for x in ex for k in x["timings"]})
    result = {
        "variant": name, "config": v, "c1_t_remediation": [x for x in c1 if x is not None],
        "execute": {
            "warm": {p: summary([x["timings"][p] for x in warm if p in x["timings"]]) for p in phases},
            "cold": {p: summary([x["timings"][p] for x in cold if p in x["timings"]]) for p in phases},
            "cold_init_s": summary([x["init_s"] for x in cold if x["init_s"]]),
            "warm_billed_gb_s": summary([x["billed_ms"] / 1000 * v["memory"] / 1024 for x in warm]),
            "max_mem_mb": max((x["max_mem_mb"] for x in ex), default=None),
        },
        "analyze": {
            "warm": {p: summary([x["timings"][p] for x in an if not x["cold"]]) for p in ("fetch", "opa", "exception", "total")},
            "max_mem_mb": max((x["max_mem_mb"] for x in an), default=None),
        },
    }
    with open(os.path.join(out_dir, f"{name}.json"), "w") as f:
        json.dump(result, f, indent=2)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", type=int, default=8)
    ap.add_argument("--only", help="comma-separated variant names")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    s = boto3.Session(region_name=REGION)
    lam, s3, logs = s.client("lambda"), s.client("s3"), s.client("logs")
    original = {fn: {k: lam.get_function_configuration(FunctionName=fn)[k] for k in ("MemorySize", "Environment")}
                for fn in (EXECUTE, ANALYZE)}
    wanted = set(args.only.split(",")) if args.only else None
    try:
        for name, v in VARIANTS:
            if wanted and name not in wanted:
                continue
            run_variant(name, v, lam, s3, logs, args.runs, cold_runs={0, 3, 6}, out_dir=args.out)
    finally:
        restore(lam, s3, original)
        print("restored function configuration and remediation.tf", flush=True)


if __name__ == "__main__":
    main()
