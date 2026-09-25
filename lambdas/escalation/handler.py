import json
import boto3
import os
from urllib.request import urlopen, Request

from boto3.dynamodb.conditions import Attr, Key

ssm = boto3.client('ssm')
dynamodb = boto3.resource('dynamodb')
violations_table = dynamodb.Table(os.environ.get('VIOLATIONS_TABLE', 'violations_log'))
# How many distinct issues to check before giving up and opening a new one.
# Each check is a GitHub API call; the rows themselves are read without a limit
# (see open_issue_from_log).
MAX_ISSUES_CHECKED = 5


def link_issue(violation_id, issue_number, issue_url):
    """Attach the GitHub issue to an EXISTING violations_log entry.

    UpdateItem on a missing key would CREATE a phantom item holding only the
    issue fields — no state, no resource, invisible to the audit GSI. The
    condition prevents that; synthetic ids (analyze-failed-*, control-not-
    found-*) legitimately have no log row, so a failed condition is only
    logged, never retried.
    """
    try:
        violations_table.update_item(
            Key={'violation_id': violation_id},
            UpdateExpression='SET github_issue_id = :id, github_issue_url = :url',
            ConditionExpression='attribute_exists(violation_id)',
            ExpressionAttributeValues={':id': str(issue_number), ':url': issue_url},
        )
    except Exception as e:
        code = getattr(e, 'response', {}).get('Error', {}).get('Code', '')
        if code == 'ConditionalCheckFailedException':
            print(f"No violations_log entry for {violation_id}; issue not linked")
        else:
            raise


def open_issue_from_log(resource_arn, control_id, pat, owner, repo):
    """The issue already tracking this resource+control, from the audit trail.

    The Search API used before has an indexing delay of seconds to minutes, so a
    just-created issue was often invisible and a duplicate was opened for the
    same violation (design.md, L5). The violations_log has no such delay: its
    GSI answers with the recent rows of this resource, and any github_issue_url
    among them points at an issue this system opened. Whether it is still open
    is then read directly from the issue (not from the search index).

    No Limit on the query: DynamoDB applies Limit to the items it READS, before
    the FilterExpression. With a limit of 20, the 20 newest rows of a bucket —
    several controls, two rounds per incident — could all belong to other
    controls, the filter would empty the page, and an issue already open for
    this control would be missed: the duplicate this lookup exists to prevent.
    query_exception in the Analyze avoids the same trap for the same reason.
    The rows of one resource are few, so the query pages through all of them,
    newest first, and stops at the first issue that is still open.
    """
    kwargs = dict(
        IndexName='resource_arn-timestamp-index',
        KeyConditionExpression=Key('resource_arn').eq(resource_arn),
        FilterExpression=Attr('control_id').eq(control_id) & Attr('github_issue_id').exists(),
        ScanIndexForward=False,
    )
    checked = set()
    while True:
        resp = violations_table.query(**kwargs)
        for row in resp.get('Items', []):
            number = row['github_issue_id']
            if number in checked:
                continue
            checked.add(number)
            if issue_is_open(pat, owner, repo, number):
                return int(number)
            if len(checked) >= MAX_ISSUES_CHECKED:
                return None
        if 'LastEvaluatedKey' not in resp:
            return None
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']


def issue_is_open(pat, owner, repo, number):
    req = Request(
        f'https://api.github.com/repos/{owner}/{repo}/issues/{number}',
        headers={'Authorization': f'Bearer {pat}', 'Accept': 'application/vnd.github+json'},
    )
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())['state'] == 'open'
    except Exception as e:
        print(f"Could not read issue #{number}: {e}")
        return False


def get_github_params():
    resp = ssm.get_parameters(
        Names=[
            '/compliance/github/pat',
            '/compliance/github/owner',
            '/compliance/github/repo',
        ],
        WithDecryption=True,
    )
    return {p['Name'].split('/')[-1]: p['Value'] for p in resp['Parameters']}


def find_open_issue(pat, owner, repo, title):
    """Return number of an existing open issue with the same title, or None.

    Fallback for escalations that have no violations_log row to look through —
    a system failure carries a synthetic violation_id. Subject to the Search
    API's indexing delay, which is why it is no longer the first check.
    """
    import urllib.parse
    query = urllib.parse.quote(f'repo:{owner}/{repo} is:issue is:open in:title "{title}"')
    req = Request(
        f'https://api.github.com/search/issues?q={query}',
        headers={
            'Authorization': f'Bearer {pat}',
            'Accept': 'application/vnd.github+json',
        },
        method='GET',
    )
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        for item in data.get('items', []):
            if item.get('title') == title:
                return item['number']
    except Exception as e:
        print(f"Issue search failed (will create new): {e}")
    return None


def create_github_issue(pat, owner, repo, title, body):
    payload = json.dumps({
        'title': title,
        'body': body,
        # Both labels from Requirement 8.11: what the issue is about, and that
        # no person opened it.
        'labels': ['compliance-violation', 'auto-escalation'],
    }).encode()
    req = Request(
        f'https://api.github.com/repos/{owner}/{repo}/issues',
        data=payload,
        headers={
            'Authorization': f'Bearer {pat}',
            'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def lambda_handler(event, context):
    params = get_github_params()
    failures = []

    for record in event['Records']:
        item_id = record['messageId']
        try:
            body = json.loads(record['body'])
            resource_id = body['resource_arn'].split(':')[-1]

            title = f"[VIOLATION] {body['control_id']} — {resource_id}"

            # The audit trail is exact and has no indexing delay; the Search API
            # remains as the fallback for escalations with no log row of their
            # own (system failures, whose violation_id is synthetic).
            existing = open_issue_from_log(body['resource_arn'], body['control_id'],
                                           params['pat'], params['owner'], params['repo'])
            if not existing:
                existing = find_open_issue(params['pat'], params['owner'], params['repo'], title)
            if existing:
                # Still link this violation to the existing issue — the audit
                # trail (H2) must show which issue tracks each violation.
                print(f"Open issue #{existing} already exists for {title}, linking")
                link_issue(body['violation_id'], existing,
                           f"https://github.com/{params['owner']}/{params['repo']}/issues/{existing}")
                continue

            issue_body = (
                f"- **Recurso**: {body['resource_arn']}\n"
                f"- **Controle**: {body['control_id']}\n"
                f"- **Violação ID**: {body['violation_id']}\n"
                f"- **Erro**: {body['error']}\n"
                f"- **Execução**: {body['step_functions_execution_arn']}\n"
                f"- **Timestamp**: {body['timestamp']}\n"
                f"- **Conta / Região**: {body['account_id']} / {body['region']}"
            )

            issue = create_github_issue(
                params['pat'], params['owner'], params['repo'],
                title, issue_body,
            )
            print(f"Created issue #{issue['number']}: {issue['html_url']}")

            link_issue(body['violation_id'], issue['number'], issue['html_url'])
        except Exception as e:
            print(f"Failed to process {item_id}: {e}")
            failures.append({'itemIdentifier': item_id})

    return {'batchItemFailures': failures}
