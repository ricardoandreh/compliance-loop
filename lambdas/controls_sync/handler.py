import boto3, json, os

dynamodb = boto3.client('dynamodb')
events = boto3.client('events')

TABLE = os.environ['CONTROLS_TABLE']
RULE_NAME = os.environ['EVENTBRIDGE_RULE_NAME']


def lambda_handler(event, context):
    items = dynamodb.scan(TableName=TABLE, ProjectionExpression='cloudtrail_events')['Items']

    all_events = set()
    for item in items:
        for evt in item.get('cloudtrail_events', {}).get('SS', []):
            all_events.add(evt)

    pattern = json.dumps({
        "detail-type": ["AWS API Call via CloudTrail"],
        "detail": {"eventName": sorted(all_events)}
    })

    events.put_rule(Name=RULE_NAME, EventPattern=pattern, State='ENABLED')
    print(f"Updated {RULE_NAME}: {sorted(all_events)}")
