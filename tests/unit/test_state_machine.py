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


def test_verification_branches_never_reach_execute(sm):
    inner = map_states(sm)
    for start in ("RegisterVerified", "RegisterNonConvergence"):
        assert "InvokeExecute" not in reachable(inner, start), (
            f"{start} can reach InvokeExecute: a verification round could "
            f"remediate again, and the loop could oscillate")


def test_verification_choices_are_evaluated_before_the_violation_choice(sm):
    """Choice rules are evaluated in order. If the generic S_violation rule came
    first, a verification round would route a violation to InvokeExecute."""
    choices = map_states(sm)["ClassifyState"]["Choices"]
    targets = [c["Next"] for c in choices]
    first_generic_violation = targets.index("RegisterViolation")
    for verify_target in ("RegisterVerified", "RegisterNonConvergence"):
        idx = targets.index(verify_target)
        assert idx < first_generic_violation, (
            f"{verify_target} is evaluated after RegisterViolation")
        assert "mode = 'verify'" in choices[idx]["Condition"]


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
