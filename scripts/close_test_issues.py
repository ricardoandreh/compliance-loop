#!/usr/bin/env python3
"""
Close the GitHub issues the test suite opens.

Scenario C4 escalates on purpose — a bucket that does not exist, so remediation
fails and the loop raises a GitHub issue. That is the behaviour under test, but
it means every benchmark run leaves one more issue behind for a bucket that
never existed. Over months of runs they pile up and bury any real escalation.

This closes issues whose resource looks like a test fixture, and refuses to
touch anything else: an issue for a resource that is not obviously a test
bucket is left open and reported, because a false positive here would hide a
genuine compliance violation.

Closing does not weaken the audit trail. violations_log keeps github_issue_url
regardless of issue state, and a closed issue stays readable — arguably it
represents "violation handled" better than one left open forever.

Usage:
    python3 scripts/close_test_issues.py --dry-run     # list what would close
    python3 scripts/close_test_issues.py               # close them
    python3 scripts/close_test_issues.py --title "..." # close one exact title
"""

import os
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

import boto3

# Resources the suite creates. Anything not matching is treated as potentially
# real and left alone.
TEST_RESOURCE_PATTERNS = [
    re.compile(r"^bucket-inexistente-c4-\d+$"),      # C4, by design
    re.compile(r"^bucket-inexistente-test$"),        # early manual probes
    re.compile(r"^compliance-inttest-c\d+-\d+$"),    # C1..C9
    # CTRL-06 creates a 1 GiB volume and deletes it; the escalation names the
    # resource as volume/<id>. This account runs no volumes of its own.
    re.compile(r"^volume/vol-[0-9a-f]+$"),
    re.compile(r"^compliance-ctrl\d+-\d+$"),         # CTRL-01..05
    re.compile(r"^compliance-test-.*$"),             # older harness
    re.compile(r"^test-compliance-bucket-\d+$"),     # manual
    re.compile(r"^teste-bucket-.*$"),                # manual
]

GITHUB = "https://api.github.com"


def github_params(region=None):
    region = region or os.environ.get("AWS_REGION", "us-east-1")
    ssm = boto3.client("ssm", region_name=region)
    resp = ssm.get_parameters(
        Names=[
            "/compliance/github/pat",
            "/compliance/github/owner",
            "/compliance/github/repo",
        ],
        WithDecryption=True,
    )
    return {p["Name"].split("/")[-1]: p["Value"] for p in resp["Parameters"]}


def api(params, path, method="GET", body=None):
    req = urllib.request.Request(
        f"{GITHUB}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {params['pat']}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def list_open_issues(params, per_page=100):
    """Use the issues API, not search: search is eventually consistent and a
    just-created issue may not be indexed yet."""
    out, page = [], 1
    while True:
        batch = api(params, f"/repos/{params['owner']}/{params['repo']}/issues"
                            f"?state=open&labels=compliance-violation"
                            f"&per_page={per_page}&page={page}")
        # The issues endpoint also returns pull requests; drop them.
        batch = [i for i in batch if "pull_request" not in i]
        out += batch
        if len(batch) < per_page:
            return out
        page += 1


def resource_of(title):
    """Issue titles are '[VIOLATION] <control_id> — <resource_id>'."""
    m = re.search(r"—\s*(.+?)\s*$", title)
    return m.group(1) if m else None


def is_test_resource(resource):
    return bool(resource) and any(p.match(resource) for p in TEST_RESOURCE_PATTERNS)


def close_issue(params, number):
    return api(params, f"/repos/{params['owner']}/{params['repo']}/issues/{number}",
               method="PATCH", body={"state": "closed", "state_reason": "not_planned"})


def main():
    ap = argparse.ArgumentParser(description="Close test-fixture compliance issues")
    ap.add_argument("--dry-run", action="store_true", help="list without closing")
    ap.add_argument("--title", help="close the single open issue with this exact title")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    args = ap.parse_args()

    params = github_params(args.region)
    issues = list_open_issues(params)

    if args.title:
        issues = [i for i in issues if i["title"] == args.title]
        if not issues:
            print(f"no open issue titled {args.title!r}")
            return 0

    closable, skipped = [], []
    for i in issues:
        (closable if is_test_resource(resource_of(i["title"])) else skipped).append(i)

    print(f"{len(issues)} open, {len(closable)} match a test fixture, "
          f"{len(skipped)} left alone\n")

    for i in closable:
        if args.dry_run:
            print(f"  would close #{i['number']:4}  {i['title']}")
        else:
            close_issue(params, i["number"])
            print(f"  closed #{i['number']:4}  {i['title']}")

    if skipped:
        print("\n  NOT closed — resource does not look like a test fixture:")
        for i in skipped:
            print(f"    #{i['number']:4}  {i['title']}")
        print("  Review these by hand; they may be real violations.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
