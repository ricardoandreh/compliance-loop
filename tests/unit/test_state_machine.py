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
    """Every way out of RegisterViolation goes through PlanAction, whose first
    rule sends a verification round to Escalate, a terminal state. So a
    verification round cannot remediate again, and the loop cannot oscillate."""
    inner = map_states(sm)
    out_of_violation = {d for s, d in edges(inner) if s == "RegisterViolation"}
    assert out_of_violation == {"PlanAction"}
    into_execute = {s for s, d in edges(inner) if d == "InvokeExecute"}
    assert into_execute == {"PlanAction"}
    plan = inner["PlanAction"]
    first = plan["Choices"][0]
    assert "mode = 'verify'" in first["Condition"] and first["Next"] == "Escalate"
    assert inner["Escalate"].get("End") is True
    assert "InvokeExecute" not in reachable(inner, "Escalate")


def test_a_failed_violation_record_never_remediates(sm):
    inner = map_states(sm)
    catch = inner["RegisterViolation"]["Catch"]
    assert [c["Next"] for c in catch] == ["PlanAction"]
    assert "audit_write_failed" in catch[0]["Output"]
    targets = {c["Next"] for c in inner["PlanAction"]["Choices"]
               if "audit_write_failed" in c["Condition"]}
    assert targets == {"ViolationNotRecorded"}
    assert inner["ViolationNotRecorded"]["Type"] == "Fail"


def test_escalation_names_its_reason(sm):
    body = map_states(sm)["Escalate"]["Arguments"]["MessageBody"]
    for reason in ("remediation_failed", "non_convergence", "system_failure"):
        assert reason in body


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
    assert [c["Next"] for c in catch] == ["VerificationNotScheduledEscalation"]
    assert sm["States"]["VerificationNotScheduledEscalation"]["Next"] == "ExecutionFailed"
