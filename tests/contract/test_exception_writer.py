"""
Contract: only the exception-writer role writes the exceptions table.

The loop obeys every row of the exceptions table, so a row is a decision to
switch remediation off for a resource. Who takes that decision is the
platform's approval channel (L20); the loop's part is to make that channel the
only way in. Two things keep it so, both in infra/modules/knowledge:

- the table's resource policy denies the write actions to every principal but
  the writer role, and the writer role is allowed exactly those actions — both
  read the same local, local.exception_write_actions, which must cover every
  DynamoDB action that changes an item;
- no role of the loop itself is granted a write on the table: the Analyze and
  the scheduler sync only read it.

Like the other contracts, this reads the Terraform source; that the deny holds
against real credentials is scenario C11 of the integration suite.
"""

import glob
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODULES = os.path.join(ROOT, "infra", "modules")
KNOWLEDGE = os.path.join(MODULES, "knowledge", "main.tf")

# Every DynamoDB data-plane action that creates, changes or removes an item.
ITEM_WRITES = {
    "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem",
    "dynamodb:PartiQLInsert", "dynamodb:PartiQLUpdate", "dynamodb:PartiQLDelete",
}


def write_actions_local():
    tf = open(KNOWLEDGE).read()
    block = re.search(r"exception_write_actions = \[(.*?)\]", tf, re.S)
    assert block, "local.exception_write_actions is missing or was renamed"
    return tf, set(re.findall(r'"(dynamodb:[A-Za-z]+)"', block.group(1)))


def test_the_write_actions_cover_every_item_write():
    _, actions = write_actions_local()
    assert ITEM_WRITES - actions == set(), "an item write is neither denied nor granted to the writer"


def test_the_table_policy_denies_those_actions_to_everyone_but_the_writer():
    tf, _ = write_actions_local()
    policy = re.search(r'resource "aws_dynamodb_resource_policy" "exceptions" \{(.*?)\n\}', tf, re.S)
    assert policy, "the exceptions table has no resource policy"
    body = policy.group(1)
    assert 'Effect    = "Deny"' in body or 'Effect = "Deny"' in body
    assert 'Principal = "*"' in body
    assert "Action    = local.exception_write_actions" in body
    assert 'ArnNotEquals = { "aws:PrincipalArn" = aws_iam_role.exception_writer.arn }' in body


def test_the_writer_is_granted_those_actions_on_the_table_only():
    tf, _ = write_actions_local()
    grant = re.search(r'resource "aws_iam_role_policy" "exception_writer" \{(.*?)\n\}', tf, re.S)
    assert grant, "the writer role has no policy"
    assert "concat(local.exception_write_actions" in grant.group(1)
    assert "Resource = aws_dynamodb_table.exceptions.arn" in grant.group(1)


def test_no_role_of_the_loop_writes_the_exceptions_table():
    for path in glob.glob(os.path.join(MODULES, "*", "main.tf")):
        if path == KNOWLEDGE:
            continue
        for line in open(path):
            if "exceptions_table_arn" in line and "Action" in line:
                granted = set(re.findall(r'"(dynamodb:[A-Za-z]+)"', line))
                assert not granted & ITEM_WRITES, f"{os.path.relpath(path, ROOT)} grants {granted & ITEM_WRITES}"
