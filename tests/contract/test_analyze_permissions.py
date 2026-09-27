"""
Contract: the Analyze role grants exactly the reads the controls make.

A control is data: its fetch_config names the boto3 call the Analyze makes to
read the resource. The role's read actions live in one Terraform local
(local.resource_read_actions in infra/modules/analyze). Both directions matter:
a control whose read is missing fails every evaluation with AccessDenied, which
the loop reports as a system failure; an action no control uses is a
permission granted ahead of need — the least-privilege gap this test closes.

boto3 method names and IAM action names do not always match
(get_bucket_encryption is s3:GetEncryptionConfiguration), so the translation
is written out below. A control using a call not listed here fails the test
until someone looks up its IAM action.
"""

import glob
import json
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODULE = os.path.join(ROOT, "infra", "modules", "analyze", "main.tf")

IAM_ACTION = {
    ("s3", "get_public_access_block"): "s3:GetBucketPublicAccessBlock",
    ("s3", "get_bucket_policy"): "s3:GetBucketPolicy",
    ("s3", "get_bucket_encryption"): "s3:GetEncryptionConfiguration",
    ("s3", "get_bucket_versioning"): "s3:GetBucketVersioning",
    ("ec2", "get_ebs_encryption_by_default"): "ec2:GetEbsEncryptionByDefault",
    ("ec2", "describe_volumes"): "ec2:DescribeVolumes",
}


def control_reads():
    reads = set()
    for path in glob.glob(os.path.join(ROOT, "controls", "*", "control.json")):
        fetch = json.load(open(path))["fetch_config"]["M"]
        reads.add((fetch["service"]["S"], fetch["method"]["S"]))
    return reads


def granted_reads():
    tf = open(MODULE).read()
    block = re.search(r"resource_read_actions = \{(.*?)\n  \}", tf, re.S)
    assert block, "local.resource_read_actions is missing or was renamed"
    return tf, block, set(re.findall(r'"((?:s3|ec2|rds|iam|cloudtrail):[A-Za-z]+)"', block.group(1)))


def test_every_control_read_has_a_known_iam_action():
    unknown = control_reads() - IAM_ACTION.keys()
    assert not unknown, f"add the IAM action for {sorted(unknown)} to IAM_ACTION"


def test_the_role_grants_exactly_the_reads_the_controls_make():
    needed = {IAM_ACTION[r] for r in control_reads() if r in IAM_ACTION}
    _, _, granted = granted_reads()
    assert needed - granted == set(), "a control reads something the role does not grant"
    assert granted - needed == set(), "the role grants a read no control makes"


def test_the_read_actions_have_one_source():
    """The policy statements reference the local; an action spelled out
    elsewhere in the module is a second copy that can drift."""
    tf, block, granted = granted_reads()
    outside = tf[:block.start()] + tf[block.end():]
    assert not [a for a in granted if f'"{a}"' in outside]
    assert "local.resource_read_actions.s3" in outside and "local.resource_read_actions.ec2" in outside
