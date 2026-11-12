"""
Names of the deployed resources, derived the way the infrastructure derives them.

Every resource is named <project_name>-<component>-<environment>, from the three
OpenTofu variables of the same names (infra/variables.tf). The scripts take the
same three values from the environment, so a deployment with another prefix,
environment or region needs no edit here:

    AWS_REGION      (default us-east-1)
    PROJECT_NAME    (default compliance-loop)
    ENVIRONMENT     (default dev)

ARTIFACT_BUCKET overrides the artifact bucket's default name,
<project_name>-artifacts-<account id>, the one scripts/bootstrap.sh creates.
"""

import os

REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
PROJECT = os.environ.get("PROJECT_NAME", "compliance-loop")
ENV = os.environ.get("ENVIRONMENT", "dev")


def name(component):
    """<project>-<component>-<env>: functions, tables, queues, roles, topics."""
    return f"{PROJECT}-{component}-{ENV}"


# The state machine's component is the project name itself: <project>-<project>-<env>.
STATE_MACHINE = name(PROJECT)

# CloudWatch alarms and the schedule group are prefixed <project>-<env>-...
PREFIX_ENV = f"{PROJECT}-{ENV}"


def log_group(function):
    return f"/aws/lambda/{name(function)}"


def artifact_bucket(account):
    return (os.environ.get("ARTIFACT_BUCKET") or os.environ.get("CONTROLS_BUCKET")
            or f"{PROJECT}-artifacts-{account}")
