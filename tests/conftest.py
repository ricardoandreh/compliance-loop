"""
Shared test setup: the remediation allowlist, read from where it is defined.

The Execute refuses any control asking for an action outside ALLOWED_ACTIONS,
which Terraform sets from local.remediation_actions in infra/modules/execute —
the same list the remediation role grants. The tests read it from there too,
so there is still one list; a test that hard-coded its own would pass against a
list the deployed function no longer has.
"""

import json
import os
import re

MODULE = os.path.join(os.path.dirname(__file__), "..", "infra", "modules", "execute", "main.tf")


def remediation_actions():
    tf = open(MODULE).read()
    block = re.search(r"remediation_actions = \{(.*?)\n  \}", tf, re.S).group(1)
    return sorted(re.findall(r'"((?:s3|ec2):[A-Za-z]+)"', block))


os.environ.setdefault("ALLOWED_ACTIONS", json.dumps(remediation_actions()))
