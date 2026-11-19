from __future__ import annotations

import json

import pytest

from hive.agents.arbiter import ARBITER_TAG, arbitrate, verdict_from_tags
from hive.hr.client import HRError
from hive.hr.harnesses import resolve_harness
from hive.models import Decision, DecisionStatus
from tests.fakes import FakeHRClient

ARBITER_ID = resolve_harness("hive-arbiter")


@pytest.fixture
def httpx_rule(store):
    return store.put_decision(
        Decision(
            id="dhttpx0001",
            title="HTTP client is httpx",
            statement="Use httpx with a timeout; never requests.",
            scope=["hive/**"],
            status=DecisionStatus.ACCEPTED,
            proposed_by="alice",
            decided_by="bob",
        )
    )


def propose(store, title, statement, **kw):
    return store.put_decision(Decision(title=title, statement=statement, proposed_by="agent:carol", **kw))


def test_requests_proposal_is_flagged_as_contradicting_httpx(store, httpx_rule):
    p = propose(store, "Use requests for HTTP", "Use the requests library for HTTP calls.")
    fake = FakeHRClient(
        outputs={
            ARBITER_ID: {
                "duplicate_of": None,
                "contradicts": ["dhttpx0001"],
                "note": "Contradicts 'HTTP client is httpx'.",
            }
        }
    )
    verdict = arbitrate(p.id, client=fake, store=store)
    assert verdict.contradicts == ["dhttpx0001"] and verdict.duplicate_of is None

    saved = store.get_decision(p.id)
    assert f"{ARBITER_TAG}contradicts:dhttpx0001" in saved.tags
    assert verdict_from_tags(saved).contradicts == ["dhttpx0001"]
    assert saved.status is DecisionStatus.PROPOSED  # the arbiter only warns; humans decide

    [event] = store.list_events()
    assert (event.type, event.ref_id) == ("check_run", p.id) and "Contradicts" in event.summary

    [(args, kwargs)] = fake.calls_to("run")
    sent = json.loads(args[1].split("\n", 1)[1])
    assert sent["proposal"]["id"] == p.id
    assert [c["id"] for c in sent["existing"]] == ["dhttpx0001"]
    assert kwargs["max_cost_usd"] <= 0.5


def test_duplicate_detected_and_hallucinated_ids_dropped(store, httpx_rule):
    p = propose(store, "Prefer httpx", "Always use httpx, not requests, with a timeout.")
    fake = FakeHRClient(
        outputs={
            ARBITER_ID: {
                "duplicate_of": "dhttpx0001",
                "contradicts": ["dNOTREAL", p.id],
                "note": "Same rule.",
            }
        }
    )
    verdict = arbitrate(p.id, client=fake, store=store)
    assert verdict.duplicate_of == "dhttpx0001"
    assert verdict.contradicts == []  # unknown id and the proposal itself are not candidates


def test_no_similar_decisions_skips_the_paid_call(store, httpx_rule):
    p = propose(store, "Dashboard uses HTMX", "No Node build step for the dashboard.")
    fake = FakeHRClient()
    verdict = arbitrate(p.id, client=fake, store=store)
    assert verdict.duplicate_of is None and verdict.contradicts == []
    assert fake.calls_to("run") == []


def test_rejected_and_superseded_are_not_candidates(store):
    store.put_decision(
        Decision(
            title="Use requests",
            statement="requests for HTTP",
            status=DecisionStatus.REJECTED,
            proposed_by="x",
        )
    )
    p = propose(store, "Use requests for HTTP", "requests everywhere")
    fake = FakeHRClient()
    arbitrate(p.id, client=fake, store=store)
    assert fake.calls_to("run") == []


def test_rearbitration_replaces_old_tags(store, httpx_rule):
    p = propose(store, "Use requests for HTTP", "Use requests.", tags=["http"])
    out = iter([{"contradicts": ["dhttpx0001"], "note": "x"}, {"contradicts": [], "note": "fine now"}])
    fake = FakeHRClient(outputs={ARBITER_ID: lambda _input: json.dumps(next(out))})
    arbitrate(p.id, client=fake, store=store)
    arbitrate(p.id, client=fake, store=store)
    tags = store.get_decision(p.id).tags
    assert tags == ["http", f"{ARBITER_TAG}checked"]


def test_harness_failure_degrades_without_raising(store, httpx_rule):
    p = propose(store, "Use requests for HTTP", "Use requests.")
    fake = FakeHRClient(outputs={ARBITER_ID: HRError("no_credit", 402, "balance used up")})
    verdict = arbitrate(p.id, client=fake, store=store)
    assert verdict.contradicts == [] and "unavailable" in verdict.note
    assert f"{ARBITER_TAG}unavailable" in store.get_decision(p.id).tags


def test_secrets_in_proposals_are_redacted_before_sending(store, httpx_rule):
    token = "ghp_" + "S3cr3tT0k3n" * 3
    p = propose(store, "Use requests for HTTP", f"Use requests; auth with {token}")
    fake = FakeHRClient(outputs={ARBITER_ID: {"contradicts": []}})
    arbitrate(p.id, client=fake, store=store, env_values={})
    assert token not in fake.calls_to("run")[0][0][1]
