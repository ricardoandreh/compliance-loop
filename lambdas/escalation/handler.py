import json
import boto3
import os
from urllib.request import urlopen, Request

ssm = boto3.client('ssm')
dynamodb = boto3.resource('dynamodb')
violations_table = dynamodb.Table(os.environ.get('VIOLATIONS_TABLE', 'violations_log'))


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


def create_github_issue(pat, owner, repo, title, body):
    payload = json.dumps({
        'title': title,
        'body': body,
        'labels': ['compliance-violation'],
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

            violations_table.update_item(
                Key={'violation_id': body['violation_id']},
                UpdateExpression='SET github_issue_id = :id, github_issue_url = :url',
                ExpressionAttributeValues={
                    ':id': str(issue['number']),
                    ':url': issue['html_url'],
                },
            )
        except Exception as e:
            print(f"Failed to process {item_id}: {e}")
            failures.append({'itemIdentifier': item_id})

    return {'batchItemFailures': failures}
