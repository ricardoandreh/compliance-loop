import json
import boto3
import os
from datetime import datetime, timezone, timedelta
from boto3.dynamodb.types import TypeDeserializer

dynamodb  = boto3.resource('dynamodb')
scheduler = boto3.client('scheduler')
sfn       = boto3.client('stepfunctions')

TABLE             = os.environ['EXCEPTIONS_TABLE']
STATE_MACHINE_ARN = os.environ['STATE_MACHINE_ARN']
SCHEDULER_ROLE    = os.environ['SCHEDULER_ROLE_ARN']
SCHEDULE_GROUP    = os.environ.get('SCHEDULE_GROUP', 'compliance')
ACCOUNT_ID        = os.environ.get('ACCOUNT_ID', '')
REGION            = os.environ.get('AWS_REGION', 'us-east-1')

# TTL deletions are performed by DynamoDB itself and surface on the stream under
# this service principal; user/API deletions do not carry it.
DDB_TTL_PRINCIPAL = 'dynamodb.amazonaws.com'

table = dynamodb.Table(TABLE)
_deserializer = TypeDeserializer()


def deserialize_image(image):
    """Convert a DynamoDB Streams image (low-level typed) into a plain dict."""
    return {k: _deserializer.deserialize(v) for k, v in image.items()}


def is_ttl_deletion(record):
    return (record.get('userIdentity') or {}).get('principalId') == DDB_TTL_PRINCIPAL


def get_current_item(exception_id):
    resp = table.get_item(Key={'exception_id': exception_id})
    return resp.get('Item')


def build_sfn_input(item, trigger):
    resource_arn = item.get('resource_arn', '')
    resource_id = resource_arn.split(':::')[-1] if ':::' in resource_arn else resource_arn
    # Re-evaluation on TTL expiry / revocation / deletion targets the ONE
    # control this exception covered — not an event-name fan-out. control_id
    # makes the state machine GetItem that exact control (RouteControlLookup →
    # GetControlById), instead of scanning by a hardcoded event_name, which
    # failed for non-S3 controls like ctrl-ebs-encryption-default and ran
    # unrelated S3 controls against the resource, producing spurious S_error.
    #
    # Indexed strictly, not .get(): an exception row without control_id is
    # malformed, and omitting the key would route the execution to QueryControls
    # with an empty event_name — a scan that matches nothing and ends in a
    # silent ExecutionSucceeded. Raising here surfaces it via batchItemFailures.
    return json.dumps({
        'resource_id':  resource_id,
        'resource_arn': resource_arn,
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


def trigger_immediate(item, trigger='exception_revoked'):
    sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        input=build_sfn_input(item, trigger),
    )


def handler(event, context):
    failures = []
    now = int(datetime.now(timezone.utc).timestamp())

    for record in event['Records']:
        item_id = record['dynamodb']['SequenceNumber']
        event_name = record['eventName']

        try:
            if event_name == 'REMOVE':
                # An exception's normal TTL expiry is already covered by the
                # EventBridge schedule created on INSERT/MODIFY, so DynamoDB's own
                # TTL deletion must NOT trigger a second (and late) re-evaluation.
                # Only a manual/API deletion (early revoke-by-delete) is actioned
                # here: cancel the now-obsolete schedule and re-evaluate the
                # resource immediately.
                if is_ttl_deletion(record):
                    continue
                old = record['dynamodb'].get('OldImage')
                if not old:
                    continue
                item = deserialize_image(old)
                delete_schedule(item['exception_id'])
                # Re-evaluate only if the deleted exception was still a live
                # grant. Deleting an already-dead row (revoked earlier, or
                # valid_until past — TTL deletion can lag for hours, leaving
                # expired rows visible to manual cleanup) is housekeeping: the
                # revoke/expiry path already re-evaluated, and re-running here
                # against a possibly long-gone resource only produces spurious
                # S_error escalations.
                if item.get('status', 'active') == 'active' and int(item.get('valid_until', 0)) > now:
                    trigger_immediate(item, 'exception_deleted')
                continue

            if event_name not in ('INSERT', 'MODIFY'):
                continue

            exception_id = record['dynamodb']['NewImage']['exception_id']['S']
            item = get_current_item(exception_id)
            if not item:
                continue

            status      = item.get('status', 'active')
            valid_until = int(item.get('valid_until', 0))

            if status == 'revoked':
                delete_schedule(exception_id)
                trigger_immediate(item, 'exception_revoked')
            elif status == 'active' and valid_until > now:
                upsert_schedule(item)
            elif status == 'active':
                # Already expired by the time the stream is processed: there is no
                # future instant to schedule and the grant is no longer valid, so
                # drop any stale schedule and re-evaluate immediately.
                delete_schedule(exception_id)
                trigger_immediate(item, 'exception_expired')

        except Exception as e:
            print(f"Failed to process {item_id}: {e}")
            failures.append({'itemIdentifier': item_id})

    return {'batchItemFailures': failures}
