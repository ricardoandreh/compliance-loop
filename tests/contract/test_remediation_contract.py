"""
Contract: every remediation produces a state its own policy accepts.

The compliance loop only terminates because of this property. A remediation
changes the resource, the change emits a CloudTrail event, the resource is
re-evaluated — and the chain stops only if that re-evaluation says S_nominal.
If a policy were stricter than its remediation, the loop would oscillate
(remediate -> event -> violation -> remediate), each turn a fresh Step Functions
execution that no timeout bounds. See design.md, L11.

So this test derives the remediated state from each control's remediation.tf,
maps it to the shape the AWS API reports back (which is what fetch_config hands
to OPA), and evaluates the control's policy with the same OPA version the
Lambda layer ships. Nothing is transcribed by hand: change a remediation.tf and
this test sees the change.

Each control also carries a deliberately broken variant that MUST be denied,
so a vacuous pass (a policy that allows everything, a query that silently
evaluates nothing) cannot hide behind a green run.

Requires Docker and the pinned OPA image; skipped otherwise. OPA runs from the
official image rather than the arm64 layer binary: Rego semantics depend on
the OPA version, not the CPU architecture, and the version is read from
scripts/build_analyze.sh so the two cannot drift apart.
"""

import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONTROLS = os.path.join(ROOT, "controls")
BUCKET = "contract-test-bucket"
BUCKET_ARN = f"arn:aws:s3:::{BUCKET}"


def opa_image():
    script = open(os.path.join(ROOT, "scripts", "build_analyze.sh")).read()
    m = re.search(r'OPA_VERSION="\$\{OPA_VERSION:-v?([0-9.]+)\}"', script)
    assert m, "could not read OPA_VERSION from scripts/build_analyze.sh"
    return f"openpolicyagent/opa:{m.group(1)}"


def control_ids():
    return sorted(d for d in os.listdir(CONTROLS)
                  if os.path.isfile(os.path.join(CONTROLS, d, "remediation.tf")))


def read(control, name):
    with open(os.path.join(CONTROLS, control, name)) as f:
        return f.read()


def hcl_value(text, attr):
    """Literal value of `attr = ...` in HCL: true/false or a quoted string."""
    m = re.search(rf'\b{re.escape(attr)}\s*=\s*(true|false|"[^"]*")', text)
    assert m, f"attribute {attr!r} not found as a literal"
    raw = m.group(1)
    return raw == "true" if raw in ("true", "false") else raw.strip('"')


def hcl_block(text, header):
    """Body of `header = { ... }` or `header { ... }`, braces balanced."""
    m = re.search(rf'\b{re.escape(header)}\s*=?\s*\{{', text)
    assert m, f"block {header!r} not found"
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end():i - 1]


def interpolate(s):
    return s.replace("${var.bucket_name}", BUCKET)


# --- remediated state, in the shape fetch_config returns --------------------
#
# Each builder reads remediation.tf and returns (ok_input, broken_input). The
# HCL attribute -> API field mapping below is the real translation layer: it is
# what S3 / EC2 report back after the provider applies the resource.

def public_block(tf):
    fields = {
        "BlockPublicAcls": "block_public_acls",
        "IgnorePublicAcls": "ignore_public_acls",
        "BlockPublicPolicy": "block_public_policy",
        "RestrictPublicBuckets": "restrict_public_buckets",
    }
    ok = {api: hcl_value(tf, attr) for api, attr in fields.items()}
    return {"resource": ok}, {"resource": {**ok, "BlockPublicPolicy": False}}


def versioning(tf):
    status = hcl_value(hcl_block(tf, "versioning_configuration"), "status")
    return {"resource": {"Status": status}}, {"resource": {"Status": "Suspended"}}


def encryption(tf):
    algo = hcl_value(hcl_block(tf, "apply_server_side_encryption_by_default"), "sse_algorithm")
    ok = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": algo}}]}
    return {"resource": ok}, {"resource": {"Rules": []}}


def ebs_default(tf):
    enabled = hcl_value(tf, "enabled")
    return ({"resource": {"EbsEncryptionByDefault": enabled}},
            {"resource": {"EbsEncryptionByDefault": False}})


def ssl_only(tf):
    body = hcl_block(tf, "ssl_statement")
    statement = {
        "Sid": hcl_value(body, "Sid"),
        "Effect": hcl_value(body, "Effect"),
        "Principal": hcl_value(body, "Principal"),
        "Action": hcl_value(body, "Action"),
        "Resource": [interpolate(r) for r in re.findall(
            r'"([^"]+)"', re.search(r'Resource\s*=\s*\[([^\]]*)\]', body).group(1))],
        "Condition": {"Bool": {"aws:SecureTransport": re.search(
            r'"aws:SecureTransport"\s*=\s*"([^"]+)"', body).group(1)}},
    }
    # The remediation merges into whatever policy exists; exercise the merge,
    # since that is the case where a customer's statements sit alongside ours.
    existing = {"Sid": "CustomerRead", "Effect": "Allow",
                "Principal": {"AWS": "arn:aws:iam::111122223333:root"},
                "Action": "s3:GetObject", "Resource": f"{BUCKET_ARN}/*"}
    context = {"resource_id": BUCKET, "resource_arn": BUCKET_ARN}

    def as_input(stmts):
        return {"resource": {"Policy": json.dumps({"Version": "2012-10-17", "Statement": stmts})},
                "context": context}

    broken = {**statement, "Condition": {"Bool": {"aws:SecureTransport": "true"}}}
    return as_input([existing, statement]), as_input([existing, broken])


BUILDERS = {
    "ctrl-s3-public-block": public_block,
    "ctrl-s3-versioning": versioning,
    "ctrl-s3-encryption": encryption,
    "ctrl-ebs-encryption-default": ebs_default,
    "ctrl-s3-ssl-only": ssl_only,
}


# --- running OPA ---------------------------------------------------------------

def docker_ready(image):
    if not shutil.which("docker"):
        return "docker not on PATH"
    probe = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    if probe.returncode != 0:
        return f"image {image} not present (docker pull {image})"
    return None


@pytest.fixture(scope="module")
def verdicts():
    """Evaluate every control's ok and broken input in ONE container run."""
    image = opa_image()
    why = docker_ready(image)
    if why:
        pytest.skip(why)

    exprs, keys = [], {}
    for n, control in enumerate(control_ids()):
        if control not in BUILDERS:
            continue
        pkg = json.loads(read(control, "control.json"))["rego_policy_package"]["S"]
        ok, broken = BUILDERS[control](read(control, "remediation.tf"))
        for kind, value in (("ok", ok), ("broken", broken)):
            var = f"c{n}_{kind}"
            keys[var] = (control, kind)
            # JSON literals are valid Rego terms, so inputs travel in the query
            # itself and only the policies need a bind mount.
            exprs.append(f"{var} := data.{pkg}.allow with input as {json.dumps(value)}")

    proc = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{CONTROLS}:/controls:ro", image,
         "eval", "--format", "json", "--data", "/controls", "; ".join(exprs)],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    bindings = json.loads(proc.stdout)["result"][0]["bindings"]
    return {keys[var]: value for var, value in bindings.items()}


def test_remediating_controls_declare_their_actions_and_scope():
    """The Execute turns these into the session policy of the remediation it is
    about to run. A control without them cannot be scoped, and the remediation
    would fail at the assume-role instead of at deploy time."""
    allowed = {"s3:PutBucketPublicAccessBlock", "s3:GetBucketPublicAccessBlock",
               "s3:PutBucketPolicy", "s3:GetBucketPolicy",
               "s3:PutBucketVersioning", "s3:GetBucketVersioning",
               "s3:PutEncryptionConfiguration", "s3:GetEncryptionConfiguration",
               "ec2:EnableEbsEncryptionByDefault", "ec2:GetEbsEncryptionByDefault"}
    for control in control_ids():
        meta = json.loads(read(control, "control.json"))
        actions = [a["S"] for a in meta.get("remediation_actions", {}).get("L", [])]
        assert actions, f"{control} declares no remediation_actions"
        assert set(actions) <= allowed, f"{control}: {sorted(set(actions) - allowed)}"
        assert meta.get("remediation_scope", {}).get("S") in ("resource", "account"), control


def test_detective_controls_declare_manual_remediation():
    """A control without remediation.tf is only legitimate when it says so: the
    state machine routes it to escalation by that field, and a control that just
    forgot its remediation would otherwise be silently detective."""
    for control in sorted(os.listdir(CONTROLS)):
        if not os.path.isdir(os.path.join(CONTROLS, control)):
            continue
        if os.path.isfile(os.path.join(CONTROLS, control, "remediation.tf")):
            continue
        meta = json.loads(read(control, "control.json"))
        assert meta.get("remediation", {}).get("S") == "manual", (
            f"{control} has no remediation.tf and does not declare "
            f'"remediation": {{"S": "manual"}}')


def test_every_control_has_a_contract():
    """A new control must bring its contract; otherwise its convergence is
    unverified and the loop could oscillate on it unnoticed."""
    missing = set(control_ids()) - set(BUILDERS)
    assert not missing, f"controls without a remediation contract: {sorted(missing)}"


@pytest.mark.parametrize("control", sorted(BUILDERS))
def test_remediated_state_is_accepted(verdicts, control):
    assert verdicts[(control, "ok")] is True, (
        f"{control}: the state remediation.tf produces is DENIED by policy.rego — "
        f"the loop would re-remediate forever")


@pytest.mark.parametrize("control", sorted(BUILDERS))
def test_broken_state_is_denied(verdicts, control):
    """Guards against a vacuous pass: the harness must be able to fail."""
    assert verdicts[(control, "broken")] is False, (
        f"{control}: a deliberately broken state was ALLOWED — the contract "
        f"check is not discriminating")
