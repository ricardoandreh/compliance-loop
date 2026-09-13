import boto3, json, os

dynamodb = boto3.client('dynamodb')
events = boto3.client('events')

TABLE = os.environ['CONTROLS_TABLE']
RULE_NAME = os.environ['EVENTBRIDGE_RULE_NAME']
# The Execute Lambda's IAM role. Every write a remediation makes reaches
# CloudTrail under this identity; required, so a deploy that forgets it fails
# loudly instead of silently re-enabling the echo.
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
    """
    issuer = lambda cond: {"userIdentity": {"sessionContext": {"sessionIssuer": {"arn": [cond]}}}}
    return {
        "detail-type": ["AWS API Call via CloudTrail"],
        "detail": {
            "eventName": sorted(set(event_names)),
            "$or": [
                issuer({"exists": False}),
                issuer({"anything-but": remediation_role_arn}),
            ],
        },
    }


def lambda_handler(event, context):
    items = dynamodb.scan(TableName=TABLE, ProjectionExpression='cloudtrail_events')['Items']

    all_events = set()
    for item in items:
        for evt in item.get('cloudtrail_events', {}).get('SS', []):
            all_events.add(evt)

    pattern = build_pattern(all_events, REMEDIATION_ROLE_ARN)
    events.put_rule(Name=RULE_NAME, EventPattern=json.dumps(pattern), State='ENABLED')
    print(f"Updated {RULE_NAME}: {sorted(all_events)}, excluding {REMEDIATION_ROLE_ARN}")
