import json
import boto3
import os
from datetime import datetime, timezone, timedelta

dynamodb  = boto3.resource('dynamodb')
scheduler = boto3.client('scheduler')
sfn       = boto3.client('stepfunctions')

TABLE             = os.environ['EXCEPTIONS_TABLE']
STATE_MACHINE_ARN = os.environ['STATE_MACHINE_ARN']
SCHEDULER_ROLE    = os.environ['SCHEDULER_ROLE_ARN']
SCHEDULE_GROUP    = os.environ.get('SCHEDULE_GROUP', 'compliance')
ACCOUNT_ID        = os.environ.get('ACCOUNT_ID', '')
REGION            = os.environ.get('AWS_REGION', 'us-east-1')

table = dynamodb.Table(TABLE)


def get_current_item(exception_id):
    resp = table.get_item(Key={'exception_id': exception_id})
    return resp.get('Item')


def build_sfn_input(item, trigger):
    resource_arn = item.get('resource_arn', '')
    resource_id = resource_arn.split(':::')[-1] if ':::' in resource_arn else resource_arn
    return json.dumps({
        'resource_id':  resource_id,
        'resource_arn': resource_arn,
        # control_id makes the state machine resolve THIS control via GetItem,
        # instead of guessing an event_name — a synthetic event would re-run
        # unrelated controls (and miss non-S3 ones entirely).
        'control_id':   item['control_id'],
        'account_id':   item.get('account_id', ACCOUNT_ID),
        'region':       item.get('region', REGION),
        'event_time':   datetime.now(timezone.utc).isoformat(),
        'trigger':      trigger,
    })


def upsert_schedule(item):
    exception_id = item['exception_id']
    valid_until = int(item['valid_until'])
    fire_at = datetime.fromtimestamp(valid_until, tz=timezone.utc) + timedelta(seconds=30)

    schedule_kwargs = dict(
        Name=f'exc-{exception_id}',
        GroupName=SCHEDULE_GROUP,
        ScheduleExpression=f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})",
        ScheduleExpressionTimezone='UTC',
        FlexibleTimeWindow={'Mode': 'OFF'},
        Target={
            'Arn':     STATE_MACHINE_ARN,
            'RoleArn': SCHEDULER_ROLE,
            'Input':   build_sfn_input(item, 'exception_ttl_expiry'),
        },
        ActionAfterCompletion='DELETE',
    )

    try:
        scheduler.create_schedule(**schedule_kwargs, ClientToken=f'exc-{exception_id}')
    except scheduler.exceptions.ConflictException:
        scheduler.update_schedule(**schedule_kwargs)


def delete_schedule(exception_id):
    try:
        scheduler.delete_schedule(Name=f'exc-{exception_id}', GroupName=SCHEDULE_GROUP)
    except scheduler.exceptions.ResourceNotFoundException:
        pass


def trigger_immediate(item):
    sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        input=build_sfn_input(item, 'exception_revoked'),
    )


def handler(event, context):
    failures = []
    now = int(datetime.now(timezone.utc).timestamp())

    for record in event['Records']:
        item_id = record['dynamodb']['SequenceNumber']
        if record['eventName'] not in ('INSERT', 'MODIFY'):
            continue

        try:
            exception_id = record['dynamodb']['NewImage']['exception_id']['S']
            item = get_current_item(exception_id)
            if not item:
                continue

            status     = item.get('status', 'active')
            valid_until = int(item.get('valid_until', 0))

            if status == 'revoked':
                delete_schedule(exception_id)
                trigger_immediate(item)
            elif status == 'active' and valid_until > now:
                upsert_schedule(item)

        except Exception as e:
            print(f"Failed to process {item_id}: {e}")
            failures.append({'itemIdentifier': item_id})

    return {'batchItemFailures': failures}
