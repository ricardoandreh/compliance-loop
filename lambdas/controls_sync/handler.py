import boto3, json, os

dynamodb = boto3.client('dynamodb')
events = boto3.client('events')

TABLE = os.environ['CONTROLS_TABLE']
RULE_NAME = os.environ['EVENTBRIDGE_RULE_NAME']
# The role every remediation runs as. The Execute Lambda holds no write
# permission of its own: it assumes this role with a session scoped to the
# violation, so the writes reach CloudTrail under this identity, not the
# Lambda's. Required, so a deploy that forgets it fails loudly instead of
# silently re-enabling the echo.
REMEDIATION_ROLE_ARN = os.environ['REMEDIATION_ROLE_ARN']


def build_pattern(event_names, remediation_role_arn):
    """EventBridge pattern: the controls' events, from anyone but the loop itself.

    Without the identity clause, every remediation's own API call comes back as
    a CloudTrail event and starts a new execution — one echo per remediated
    control. The loop closes explicitly instead: a detection round that
    remediated schedules exactly one verification round (see the state
    machine), so the echo is pure noise and is filtered here.

    The $or is load-bearing. Human changes arrive as IAMUser events, which have
    no sessionContext.sessionIssuer at all, and an anything-but on a missing
    field does NOT match — a bare anything-but would silently drop every
    violation made by an IAM user, including console changes. Checked with
    `aws events test-event-pattern`:

        pattern               IAMUser   Execute role   other role
        anything-but only     False     False          True
        $or + exists:false    True      False          True

    The source prefix pins the origin. detail-type is free text: anyone allowed
    events:PutEvents on the default bus can publish "AWS API Call via
    CloudTrail" with any eventName and request parameters, and without the
    prefix the rule matched it (checked with test-event-pattern, source
    "fault.injection": True). PutEvents refuses every source starting with
    "aws." (NotAuthorizedForSourceException), so only events emitted by AWS
    itself — the CloudTrail deliveries — can start the loop.
    """
    issuer = lambda cond: {"userIdentity": {"sessionContext": {"sessionIssuer": {"arn": [cond]}}}}
    return {
        "source": [{"prefix": "aws."}],
        "detail-type": ["AWS API Call via CloudTrail"],
        "detail": {
            "eventName": sorted(set(event_names)),
            "$or": [
                issuer({"exists": False}),
                issuer({"anything-but": remediation_role_arn}),
            ],
        },
    }


def all_event_names():
    """Every CloudTrail event any control listens to, across every page.

    A single scan returns at most 1 MB; past that, the controls on later pages
    would silently stop being detected (design.md, L2).
    """
    names = set()
    pages = dynamodb.get_paginator('scan').paginate(
        TableName=TABLE, ProjectionExpression='cloudtrail_events')
    for page in pages:
        for item in page['Items']:
            names.update(item.get('cloudtrail_events', {}).get('SS', []))
    return names


def lambda_handler(event, context):
    all_events = all_event_names()

    if not all_events:
        # EventBridge rejects an empty list in a pattern ("Empty arrays are not
        # allowed"), so building one would fail on every record of the stream
        # — and the stream would retry it. With no controls there is nothing to
        # watch: disable the rule. The next control put_rule()s it back ENABLED.
        events.disable_rule(Name=RULE_NAME)
        print(f"No controls left; {RULE_NAME} disabled")
        return

    pattern = build_pattern(all_events, REMEDIATION_ROLE_ARN)
    events.put_rule(Name=RULE_NAME, EventPattern=json.dumps(pattern), State='ENABLED')
    print(f"Updated {RULE_NAME}: {sorted(all_events)}, excluding {REMEDIATION_ROLE_ARN}")
