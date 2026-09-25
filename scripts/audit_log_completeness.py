#!/usr/bin/env python3
"""
Audit every violations_log row written during a benchmark series (H2).

The integration scenarios assert the fields they care about, one scenario at a
time. H2 claims more than that: every evaluation leaves a complete audit row.
This reads ALL rows whose timestamp falls inside the series window and checks
each against the data model, so the claim is measured over the whole series
instead of inferred from ten scenarios.

Checks, per row (Properties 11, 12, 13, 14, 23 and Requirement 19.5):

- the base fields exist and are well formed: violation_id, resource_arn,
  control_id, account_id, region, policy_version (never "unknown"), state in the
  three formal states, and timestamp in the canonical format the audit GSI's
  ordering depends on;
- S_exception rows name the exception and who approved it;
- remediation_status, when present, is one of success / failed /
  not_converged, and a failure carries its error;
- verification rows point back at the detection round and the violation they
  verify;
- a GitHub issue appears only on rows that were escalated — never on a
  compliant, excepted or successfully remediated one (P23);
- violation_id is unique (it is the table key; a duplicate would have
  overwritten an earlier evaluation).

Usage:
    python3 scripts/audit_log_completeness.py --results results/final
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
from classify_cold_starts import REGION, run_windows  # noqa: E402

TABLE = "compliance-loop-violations-log-dev"
STATES = {"S_nominal", "S_exception", "S_violation"}
CANONICAL_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
BASE = ("violation_id", "resource_arn", "control_id", "account_id", "region", "policy_version", "state", "timestamp")


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def rows_in(table, since, until):
    kwargs = {"FilterExpression": "#ts BETWEEN :a AND :b",
              "ExpressionAttributeNames": {"#ts": "timestamp"},
              "ExpressionAttributeValues": {":a": since, ":b": until}}
    while True:
        page = table.scan(**kwargs)
        yield from page["Items"]
        if "LastEvaluatedKey" not in page:
            return
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def problems_of(row):
    found = []
    for field in BASE:
        if not row.get(field):
            found.append(f"missing {field}")
    if row.get("state") and row["state"] not in STATES:
        found.append(f"state outside the model: {row['state']}")
    if row.get("timestamp") and not CANONICAL_TS.match(row["timestamp"]):
        found.append(f"non-canonical timestamp: {row['timestamp']}")
    if row.get("policy_version") == "unknown":
        found.append("policy_version unknown")

    if row.get("state") == "S_exception":
        for field in ("exception_id", "approved_by"):
            if not row.get(field):
                found.append(f"S_exception without {field}")

    status = row.get("remediation_status")
    if status is not None:
        if status not in ("success", "failed", "not_converged"):
            found.append(f"unexpected remediation_status: {status}")
        if status == "failed" and not row.get("remediation_error"):
            found.append("failed remediation without remediation_error")
        if status == "success" and not row.get("remediation_output"):
            found.append("successful remediation without remediation_output")

    if row.get("round") == "verification":
        if not row.get("origin_execution"):
            found.append("verification row without origin_execution")
        if row.get("verifies_violation_id") in (None, "", "unknown"):
            found.append("verification row without the violation it verifies")

    escalated = status in ("failed", "not_converged") or (
        row.get("state") == "S_violation" and status is None)
    if row.get("github_issue_id") and not escalated:
        found.append("issue on a row that was not escalated (P23)")
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    args = ap.parse_args()

    windows = run_windows(args.results)
    since, until = iso(windows[0][1]), iso(windows[-1][2] + 300)
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    rows = list(rows_in(table, since, until))

    by_problem = collections.Counter()
    examples = {}
    for row in rows:
        for p in problems_of(row):
            key = p.split(":")[0]
            by_problem[key] += 1
            examples.setdefault(key, row["violation_id"])

    ids = [r["violation_id"] for r in rows]
    states = collections.Counter(r.get("state") for r in rows)
    statuses = collections.Counter(r.get("remediation_status") for r in rows if r.get("remediation_status"))
    summary = {
        "results": args.results,
        "window": [since, until],
        "rows": len(rows),
        "unique_violation_ids": len(set(ids)) == len(ids),
        "by_state": dict(states),
        "by_remediation_status": dict(statuses),
        "verification_rows": sum(1 for r in rows if r.get("round") == "verification"),
        "rows_with_issue": sum(1 for r in rows if r.get("github_issue_id")),
        "complete_rows": sum(1 for r in rows if not problems_of(r)),
        "problems": dict(by_problem),
        "example_violation_ids": examples,
    }
    out = os.path.join(args.results, "log_audit.json")
    with open(out, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"wrote {out}")
    return 1 if by_problem or not summary["unique_violation_ids"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
