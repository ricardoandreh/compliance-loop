#!/usr/bin/env python3
"""Measure the blind window after the trail's event selectors are reconfigured.

On 2026-10-18 the first suite run after a `tofu apply` that changed the trail's
event selectors (PutEventSelectors at 14:12:21) failed every event-triggered
scenario for about ten minutes: CloudTrail recorded the events in its history,
but none reached the loop (monografia, Pendência 20). This probe reproduces the
reconfiguration on purpose and measures the window instead of inferring it.

Method
  1. Wait until no execution is running.
  2. Reconfigure the trail twice, as the apply did: remove the DynamoDB data
     selector, then put the original selectors back. The trail ends exactly
     as it started; t0 is the second PutEventSelectors.
  3. Every --interval seconds, create a bucket. CreateBucket is observed by
     the S3 controls, so a delivered event starts an execution whose input
     carries the bucket name. Each probe records when (and whether) that
     execution started.
  4. Stop creating buckets once three consecutive probes were delivered, keep
     watching for late deliveries, then look every probe up in the CloudTrail
     event history to separate "recorded but not delivered" from "not recorded".
  5. Delete the probe buckets.

Usage:
  lambdas/analyze/.venv/bin/python scripts/sensor_window_probe.py --out results/sensor_reconfig_window.json
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import integration_test as it  # noqa: E402


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="milliseconds")


def aws_now():
    return time.time() - it.CLOCK_OFFSET


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--prefix", default="compliance-loop")
    ap.add_argument("--env", default="dev")
    ap.add_argument("--interval", type=int, default=30, help="seconds between probe buckets")
    ap.add_argument("--max-minutes", type=int, default=25, help="stop probing after this long")
    ap.add_argument("--late-minutes", type=int, default=5, help="keep watching for late deliveries")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    it.init(args.region, args.prefix, args.env)
    trail = f"{args.prefix}-s3-events-{args.env}"
    ct = boto3.client("cloudtrail", region_name=args.region)
    s3 = it.s3

    original = ct.get_event_selectors(TrailName=trail)["EventSelectors"]
    reduced = [s for s in original if not s.get("DataResources")]
    assert len(reduced) == 1 and len(original) == 2, "unexpected selector layout"

    print("waiting for the loop to be idle...", flush=True)
    it.wait_quiesce(timeout=180)

    result = {"trail": trail, "interval_s": args.interval, "clock_offset_s": round(it.CLOCK_OFFSET, 3),
              "selectors_before": original, "probes": []}

    t_put1 = aws_now()
    ct.put_event_selectors(TrailName=trail, EventSelectors=reduced)
    time.sleep(5)
    t_put2 = aws_now()
    ct.put_event_selectors(TrailName=trail, EventSelectors=original)
    result["t_put_reduced"] = iso(t_put1)
    result["t_put_restored"] = iso(t_put2)
    after = ct.get_event_selectors(TrailName=trail)["EventSelectors"]
    result["selectors_restored_identical"] = (after == original)
    print(f"reconfigured: reduced at {iso(t_put1)}, restored at {iso(t_put2)} "
          f"(identical={after == original})", flush=True)

    probes = result["probes"]
    deadline = t_put2 + args.max_minutes * 60
    stamp = int(t_put2)
    consecutive = 0
    i = 0

    def check(open_only=True):
        nonlocal consecutive
        for p in probes:
            if p["exec_start"] is not None:
                continue
            found = it.executions_for(p["name"], p["t_create"] - 5)
            if found:
                first = min(found, key=lambda e: e["start"])
                p["exec_start"] = iso(first["start"])
                p["delay_s"] = round(first["start"] - p["t_create"], 2)
                p["executions"] = len(found)
                print(f"  {p['name']}: execution after {p['delay_s']}s", flush=True)

    while aws_now() < deadline:
        i += 1
        name = f"compliance-probe-{stamp}-{i:02d}"
        t_create = aws_now()
        s3.create_bucket(Bucket=name)
        probes.append({"name": name, "t_create": t_create, "t_create_iso": iso(t_create),
                       "since_restore_s": round(t_create - t_put2, 1),
                       "exec_start": None, "delay_s": None, "executions": 0})
        print(f"probe {i:02d} {name} at +{t_create - t_put2:.0f}s", flush=True)
        # watch for `interval` seconds, checking every 5 s
        end = time.time() + args.interval
        while time.time() < end:
            time.sleep(5)
            check()
        tail = probes[-3:]
        if len(tail) == 3 and all(p["exec_start"] is not None for p in tail):
            print("three consecutive probes delivered; window closed", flush=True)
            break

    # late deliveries
    late_end = time.time() + args.late_minutes * 60
    while time.time() < late_end and any(p["exec_start"] is None for p in probes):
        time.sleep(15)
        check()

    delivered = [p for p in probes if p["exec_start"] is not None]
    undelivered = [p for p in probes if p["exec_start"] is None]
    result["probes_total"] = len(probes)
    result["probes_delivered"] = len(delivered)
    result["probes_undelivered"] = len(undelivered)
    if delivered:
        first_ok = min(delivered, key=lambda p: p["t_create"])
        result["window_s_upper_bound"] = round(first_ok["t_create"] - t_put2, 1)
    if undelivered:
        last_lost = max(undelivered, key=lambda p: p["t_create"])
        result["window_s_lower_bound"] = round(last_lost["t_create"] - t_put2, 1)
    else:
        result["window_s_lower_bound"] = 0.0

    # CloudTrail history: recorded but not delivered vs not recorded (history lags ~15 min)
    print("looking the probes up in the CloudTrail event history (may take up to 15 min)...", flush=True)
    hist_deadline = time.time() + 16 * 60
    pending = {p["name"] for p in probes}
    while pending and time.time() < hist_deadline:
        for p in probes:
            if p["name"] not in pending:
                continue
            ev = ct.lookup_events(LookupAttributes=[{"AttributeKey": "ResourceName", "AttributeValue": p["name"]}],
                                  StartTime=dt.datetime.fromtimestamp(p["t_create"] - 60, dt.timezone.utc),
                                  MaxResults=10)["Events"]
            cb = [e for e in ev if e.get("EventName") == "CreateBucket"]
            if cb:
                p["cloudtrail_event_time"] = cb[0]["EventTime"].isoformat()
                pending.discard(p["name"])
        if pending:
            time.sleep(60)
    for p in probes:
        p.setdefault("cloudtrail_event_time", None)
        p["classification"] = ("delivered" if p["exec_start"] else
                               "recorded, not delivered" if p["cloudtrail_event_time"] else
                               "not found in history")

    # cleanup
    print("cleaning up...", flush=True)
    it.wait_quiesce(timeout=180)
    for p in probes:
        try:
            s3.delete_bucket(Bucket=p["name"])
        except Exception as e:  # noqa: BLE001
            p["cleanup_error"] = str(e)
    result["finished"] = iso(aws_now())

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(json.dumps({k: v for k, v in result.items() if k != "probes" and not k.startswith("selectors")}, indent=2))
    for p in probes:
        print(f"  +{p['since_restore_s']:>6}s {p['name']}: {p['classification']}"
              + (f" ({p['delay_s']}s)" if p["delay_s"] is not None else ""))


if __name__ == "__main__":
    main()
