"""
Structural guarantees of the state machine that the loop's correctness rests on.

These are properties of the graph, not of any one run, so they are checked
statically: a future edit that re-orders a Choice or wires a new transition can
break them without any integration scenario noticing.

The central one: an incident costs at most two executions. A detection round
may remediate and schedules exactly one verification round; a verification
round never remediates and never schedules another. That is what makes
oscillation impossible (design.md, L11) now that remediation echoes are filtered
out of the EventBridge rule.
"""

import json
import os
from collections import deque

import pytest

DEFINITION = os.path.join(os.path.dirname(__file__), "..", "..", "infra",
                          "state_machine_definition.json")


@pytest.fixture(scope="module")
def sm():
    with open(DEFINITION) as f:
        return json.load(f)


def edges(states):
    for name, st in states.items():
        for key in ("Next", "Default"):
            if key in st:
                yield name, st[key]
        for branch in st.get("Choices", []) + st.get("Catch", []):
            yield name, branch["Next"]


def reachable(states, start):
    graph = {}
    for src, dst in edges(states):
        graph.setdefault(src, set()).add(dst)
    seen, queue = {start}, deque([start])
    while queue:
        for nxt in graph.get(queue.popleft(), ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def map_states(sm):
    return sm["States"]["ProcessResults"]["ItemProcessor"]["States"]


def test_every_transition_targets_an_existing_state(sm):
    for scope in (sm["States"], map_states(sm)):
        broken = [(s, d) for s, d in edges(scope) if d not in scope]
        assert not broken, f"transitions to missing states: {broken}"


FORMAL_STATES = {"S_nominal": "RegisterNominal", "S_exception": "RegisterException",
                 "S_violation": "RegisterViolation"}


def test_classification_has_exactly_the_three_formal_states(sm):
    """The graph mirrors the formal model: one branch per state, no branch per
    round. Anything that is not one of the three states is a system failure and
    goes straight to escalation, without a register step that would record it
    as if it were a state."""
    classify = map_states(sm)["ClassifyState"]
    routes = {}
    for choice in classify["Choices"]:
        assert "mode" not in choice["Condition"], (
            "the round must not split the classification: mode belongs to PlanAction")
        state = choice["Condition"].split("state = '")[1].split("'")[0]
        routes[state] = choice["Next"]
    assert routes == FORMAL_STATES
    assert classify["Default"] == "Escalate"


def test_verification_round_never_reaches_execute(sm):
    """Every way out of RegisterViolation reaches PlanAction (through the
    breaker's count), and PlanAction's first rule sends a verification round to
    Escalate, a terminal state. So a verification round cannot remediate again,
    and the loop cannot oscillate."""
    inner = map_states(sm)
    out_of_violation = {d for s, d in edges(inner) if s == "RegisterViolation"}
    assert out_of_violation == {"CountRecentRemediations"}
    assert {d for s, d in edges(inner) if s == "CountRecentRemediations"} == {"PlanAction"}
    into_execute = {s for s, d in edges(inner) if d == "InvokeExecute"}
    assert into_execute == {"PlanAction"}
    plan = inner["PlanAction"]
    first = plan["Choices"][0]
    assert "mode = 'verify'" in first["Condition"] and first["Next"] == "Escalate"
    assert inner["Escalate"].get("End") is True
    assert "InvokeExecute" not in reachable(inner, "Escalate")


def test_every_choice_has_a_default(sm):
    """A Choice without Default fails the execution with States.NoChoiceMatched
    when no rule matches — an unhandled path that reads like a crash."""
    for scope in (sm["States"], map_states(sm)):
        missing = [n for n, st in scope.items() if st["Type"] == "Choice" and "Default" not in st]
        assert not missing, f"Choice states without Default: {missing}"


def test_every_state_is_reachable(sm):
    for scope, start in ((sm["States"], sm["StartAt"]),
                         (map_states(sm), sm["States"]["ProcessResults"]["ItemProcessor"]["StartAt"])):
        unreachable = set(scope) - reachable(scope, start)
        assert not unreachable, f"unreachable states: {sorted(unreachable)}"


def test_retries_use_the_error_prefix_of_their_integration(sm):
    """aws-sdk integrations name errors after the SDK client (DynamoDb.*), the
    optimized integrations after the service (DynamoDB.*). A Retry with the
    other spelling never matches — which is how the control lookups went without
    any retry. Observed with test-state against both integrations."""
    for scope in (sm["States"], map_states(sm)):
        for name, st in scope.items():
            resource = st.get("Resource", "")
            for retry in st.get("Retry", []):
                for err in retry["ErrorEquals"]:
                    if "aws-sdk:dynamodb" in resource:
                        assert err.startswith("DynamoDb."), f"{name}: {err}"
                    elif ":::dynamodb:" in resource:
                        assert err.startswith("DynamoDB."), f"{name}: {err}"


def test_a_verification_round_without_controls_escalates(sm):
    """Otherwise it would re-evaluate nothing and succeed."""
    route = sm["States"]["RouteControlLookup"]["Choices"]
    assert "$count($states.input.controls) > 0" in route[0]["Condition"]
    assert route[0]["Next"] == "InvokeAnalyze"
    assert "mode = 'verify'" in route[1]["Condition"]
    assert route[1]["Next"] == "UnverifiableRemediationEscalation"


def test_a_partial_verification_is_not_scheduled(sm):
    choices = sm["States"]["ScheduleVerificationIfNeeded"]["Choices"]
    assert choices[0]["Next"] == "ScheduleVerification"
    assert "$count($states.input.controls[control_id.S in $ids])" in choices[0]["Condition"]
    assert choices[1]["Next"] == "UnverifiableRemediationEscalation"


def test_escalation_names_its_reason(sm):
    body = map_states(sm)["Escalate"]["Arguments"]["MessageBody"]
    for reason in ("remediation_failed", "non_convergence", "system_failure"):
        assert reason in body


def test_every_write_declares_a_retry(sm):
    """DynamoDB and SQS integrations have no implicit retry: a throttled write
    fails the state on the first try unless the Retry is declared (design.md, L1).
    Escalations retry on anything — MessageDeduplicationId makes that idempotent —
    while the audit writes retry only on transient errors, so a
    ConditionalCheckFailed is not retried into a false alarm."""
    for scope in (sm["States"], map_states(sm)):
        for name, st in scope.items():
            resource = st.get("Resource", "")
            if "dynamodb" not in resource and "sqs" not in resource:
                continue
            errors = [e for r in st.get("Retry", []) for e in r["ErrorEquals"]]
            assert errors, f"{name} writes without Retry"
            if "sqs" in resource:
                assert errors == ["States.ALL"], f"{name}: {errors}"
            else:
                assert all("Throughput" in e or "RequestLimit" in e or "InternalServer" in e
                           for e in errors), f"{name}: {errors}"


def test_a_recurrent_violation_escalates_instead_of_remediating(sm):
    """The two rounds bound one incident; the breaker bounds a sequence of them.
    Counting comes before the decision, and the decision comes before Execute."""
    inner = map_states(sm)
    assert inner["RegisterViolation"]["Next"] == "CountRecentRemediations"
    count = inner["CountRecentRemediations"]
    assert count["Resource"].endswith("aws-sdk:dynamodb:query")
    assert count["Arguments"]["Select"] == "COUNT"
    assert count["Next"] == "PlanAction"
    # A failed count must not block the remediation that used to happen.
    assert count["Catch"][0]["Next"] == "PlanAction"
    assert "'recent_remediations': 0" in count["Catch"][0]["Output"]
    rules = inner["PlanAction"]["Choices"]
    assert "recent_remediations" in rules[1]["Condition"] and rules[1]["Next"] == "Escalate"
    assert "InvokeExecute" not in reachable(inner, "Escalate")


def test_a_detective_control_never_reaches_execute(sm):
    rules = map_states(sm)["PlanAction"]["Choices"]
    manual = [c for c in rules if "remediation = 'manual'" in c["Condition"]]
    assert [c["Next"] for c in manual] == ["Escalate"]


def test_only_a_detection_round_schedules_verification(sm):
    top = sm["States"]
    into_schedule = [s for s, d in edges(top) if d == "ScheduleVerification"]
    assert into_schedule == ["ScheduleVerificationIfNeeded"], (
        f"ScheduleVerification reachable from {into_schedule}")
    cond = top["ScheduleVerificationIfNeeded"]["Choices"][0]["Condition"]
    # $not(... = 'verify') rather than != 'verify': in JSONata a comparison with
    # an undefined value is false, so != would silently stop scheduling the
    # moment `mode` went missing from the state.
    assert "$not($states.input.mode = 'verify')" in cond
    assert "outcome = 'remediated'" in cond


def test_verification_round_is_marked_as_such(sm):
    started = sm["States"]["ScheduleVerification"]["Arguments"]["Input"]
    assert started["mode"] == "verify"
    assert "$states.context.Execution.Id" in started["origin_execution"]


def test_verification_round_reads_straight_away(sm):
    # No fixed consistency wait: whether a read right after the remediation can
    # be stale is measured, not assumed (a stale read shows up as a
    # non-convergence escalation, never as a false S_nominal).
    top = sm["States"]
    route = top["RouteControlLookup"]["Choices"]
    assert route[0]["Next"] == "InvokeAnalyze", "verify mode must be routed first"
    assert "mode = 'verify'" in route[0]["Condition"]
    assert not any(s["Type"] == "Wait" for s in top.values())


def test_unverifiable_remediation_escalates(sm):
    catch = sm["States"]["ScheduleVerification"]["Catch"]
    assert [c["Next"] for c in catch] == ["UnverifiableRemediationEscalation"]
    assert sm["States"]["UnverifiableRemediationEscalation"]["Next"] == "ExecutionFailed"
