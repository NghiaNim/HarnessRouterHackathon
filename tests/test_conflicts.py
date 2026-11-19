from __future__ import annotations

import pytest

from hive.core.conflicts import candidates, conflicting_pairs, polarities, scopes_overlap
from hive.models import Decision, DecisionStatus

A = DecisionStatus.ACCEPTED


def rule(title, statement, scope=("hive/**",), status=A, id=None):
    kw = {"id": id} if id else {}
    return Decision(
        title=title, statement=statement, scope=list(scope), status=status, proposed_by="alice", **kw
    )


@pytest.mark.parametrize(
    ("a", "b", "overlap"),
    [
        (["hive/**"], ["hive/**"], True),
        (["hive/**"], ["hive/api/app.py"], True),
        (["hive/api"], ["hive/api/**/*.py"], True),
        (["hive/web/**"], ["tests/**"], False),
        (["hive/web/**"], ["hive/api/**"], False),
        (["hive/api"], ["hive/apis/**"], False),
        (["**"], ["tests/**"], True),
        (["*.py"], ["hive/web/**"], True),  # no-slash pattern matches anywhere: conservative
        (["hive/*.py"], ["hive/sub/**"], True),  # prefix-level overlap (conservative)
        (["!hive/**"], ["hive/**"], False),  # negations alone select nothing
    ],
)
def test_scopes_overlap(a, b, overlap):
    assert scopes_overlap(a, b) is overlap
    assert scopes_overlap(b, a) is overlap


def test_polarities():
    assert polarities("Use httpx with a timeout; never requests.") == {
        "httpx": "+",
        "timeout": "+",
        "requests": "-",
    }
    assert polarities("Use httpx instead of requests")["requests"] == "-"
    assert polarities("Don't call HarnessRouter from the browser")["harnessrouter"] == "-"
    assert polarities("Avoid global state, prefer dependency injection")["global"] == "-"
    assert polarities("Avoid global state, prefer dependency injection")["injection"] == "+"


def test_httpx_requests_scenario(store):
    """The exact T-63 repro: accepting 'Use requests' next to 'never requests' must be flagged."""
    httpx_rule = store.put_decision(
        rule("HTTP client is httpx", "Use httpx with a timeout; never requests.", id="dhttpx0001")
    )
    store.put_decision(rule("Dashboard uses HTMX", "No Node build step.", scope=["hive/web/**"]))
    proposal = store.put_decision(
        rule("Use requests", "Use requests for all HTTP calls.", status=DecisionStatus.PROPOSED)
    )

    found = candidates(proposal, store)
    assert [c.decision.id for c in found] == [httpx_rule.id]
    assert "requests" in found[0].reason and found[0].score >= 1.0


def test_unrelated_scopes_never_trigger(store):
    store.put_decision(rule("No requests in web", "Never use requests.", scope=["hive/web/**"]))
    proposal = rule(
        "Use requests in tests",
        "Use requests for test HTTP calls.",
        scope=["tests/**"],
        status=DecisionStatus.PROPOSED,
    )
    assert candidates(proposal, store) == []


def test_only_accepted_decisions_are_candidates(store):
    store.put_decision(rule("Never requests", "Never use requests.", status=DecisionStatus.PROPOSED))
    store.put_decision(rule("Never requests (old)", "Never use requests.", status=DecisionStatus.SUPERSEDED))
    store.put_decision(rule("Never requests (no)", "Never use requests.", status=DecisionStatus.REJECTED))
    assert candidates(rule("Use requests", "Use requests.", status=DecisionStatus.PROPOSED), store) == []


def test_near_duplicate_is_a_weaker_signal_and_self_is_excluded(store):
    dup = store.put_decision(
        rule("Timeouts on HTTP calls", "Always set a timeout on every HTTP call to HarnessRouter.")
    )
    proposal = rule(
        "HTTP timeouts", "Set a timeout on every HarnessRouter HTTP call.", status=DecisionStatus.PROPOSED
    )
    [c] = candidates(proposal, store)
    assert c.decision.id == dup.id and 0 < c.score < 1.0 and "overlap" in c.reason
    assert candidates(dup, store) == []  # a decision never conflicts with itself


def test_top_three_ranked(store):
    for i in range(5):
        store.put_decision(rule(f"No requests {i}", f"Never use requests in module {i}."))
    found = candidates(
        rule("Use requests", "Use requests everywhere.", status=DecisionStatus.PROPOSED), store
    )
    assert len(found) == 3
    assert found == sorted(found, key=lambda c: c.score, reverse=True)


def test_compatible_rules_are_not_flagged(store):
    store.put_decision(rule("HTTP client is httpx", "Use httpx with a timeout; never requests."))
    assert (
        candidates(
            rule("Retries", "Retry 429 with exponential backoff.", status=DecisionStatus.PROPOSED), store
        )
        == []
    )


def test_conflicting_pairs_for_the_defensive_brief():
    a = rule("HTTP client is httpx", "Use httpx; never requests.")
    b = rule("Use requests", "Use requests for all HTTP calls.")
    c = rule("Dashboard uses HTMX", "No Node build step.", scope=["hive/web/**"])
    pairs = conflicting_pairs([a, b, c])
    assert [(x.id, y.id) for x, y, _ in pairs] == [(a.id, b.id)]
