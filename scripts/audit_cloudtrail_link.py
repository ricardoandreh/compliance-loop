#!/usr/bin/env python3
"""
Every remediation of a series, seen from the provider's side.

The Execute assumes the remediation role with the violation_id as the session
name, so each write it makes reaches CloudTrail as
assumed-role/<remediation role>/<violation_id>. This script checks, over a whole
series window, that the two records agree: every row of the violations_log with
remediation_status = success has at least one successful write under its own
session, and every session of the remediation role is explained by a row (or by
a scenario that invokes the Execute directly, like C10).

CloudTrail event history keeps 90 days of management events, so a series can
only be audited this way within 90 days of being run. Read-only.

Usage: python3 scripts/audit_cloudtrail_link.py results/optimized
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Attr

sys.path.insert(0, os.path.dirname(__file__))
import deployment  # noqa: E402

REGION = deployment.REGION
TABLE = deployment.name('violations-log')
ROLE = deployment.name('remediation')


def ts(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def log_rows(start, end):
    table = boto3.resource('dynamodb', region_name=REGION).Table(TABLE)
    rows, kw = [], {'FilterExpression': Attr('timestamp').between(start, end)}
    while True:
        page = table.scan(**kw)
        rows += page['Items']
        if 'LastEvaluatedKey' not in page:
            return rows
        kw['ExclusiveStartKey'] = page['LastEvaluatedKey']


def remediation_writes(start, end):
    """session name -> [(event, error, bucket)] for the remediation role's writes."""
    ct = boto3.client('cloudtrail', region_name=REGION)
    sessions, scanned = {}, 0
    pages = ct.get_paginator('lookup_events').paginate(
        LookupAttributes=[{'AttributeKey': 'ReadOnly', 'AttributeValue': 'false'}],
        # CloudTrail delivers a few minutes late; the last writes of the window
        # carry event times inside it, so a short tail is enough.
        StartTime=ts(start), EndTime=ts(end) + timedelta(minutes=10))
    for page in pages:
        for e in page['Events']:
            scanned += 1
            d = json.loads(e['CloudTrailEvent'])
            arn = d.get('userIdentity', {}).get('arn', '')
            if f'assumed-role/{ROLE}/' in arn:
                bucket = (d.get('requestParameters') or {}).get('bucketName', 'account')
                sessions.setdefault(arn.rsplit('/', 1)[1], []).append(
                    (d['eventName'], d.get('errorCode'), bucket))
    return sessions, scanned


def main():
    results = sys.argv[1]
    window = json.load(open(os.path.join(results, 'log_audit.json')))['window']
    rows = log_rows(*window)
    success = {r['violation_id'] for r in rows if r.get('remediation_status') == 'success'}
    failed = {r['violation_id'] for r in rows if r.get('remediation_status') == 'failed'}
    sessions, scanned = remediation_writes(*window)
    written = {s for s, w in sessions.items() if any(err is None for _, err, _ in w)}
    unexplained = set(sessions) - success - failed

    def scenario(bucket):
        parts = bucket.split('-')
        return parts[2] if bucket.startswith('compliance-inttest-') and len(parts) > 2 else bucket

    report = {
        'results': results,
        'window': window,
        'cloudtrail_write_events_scanned': scanned,
        'log_rows': len(rows),
        'success_rows': len(success),
        'success_rows_with_successful_write_in_own_session': len(success & written),
        'success_rows_without_write': sorted(success - written),
        'failed_rows': len(failed),
        'failed_rows_with_only_denied_writes': len({s for s in failed if s in sessions
                                                    and all(err for _, err, _ in sessions[s])}),
        'remediation_sessions': len(sessions),
        'sessions_without_log_row': len(unexplained),
        'sessions_without_log_row_by_scenario': {
            f'{k[0]} {k[1]} {k[2]}': v for k, v in Counter(
                (scenario(b), ev, err or 'ok') for s in unexplained for ev, err, b in sessions[s]).items()},
    }
    out = os.path.join(results, 'cloudtrail_link.json')
    json.dump(report, open(out, 'w'), indent=2)
    print(json.dumps(report, indent=2))
    print(f'written to {out}')


if __name__ == '__main__':
    main()
