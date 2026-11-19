from datetime import UTC, datetime, timedelta

import pytest

from hive.core.decisions import AgentCannotDecide, DecisionError, DecisionService, is_agent
from hive.models import DecisionStatus
from hive.store.base import NotFound
from hive.store.sqlite import SqliteStore

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def svc():
    ticks = iter(range(10_000))
    store = SqliteStore(":memory:")
    yield DecisionService(store, clock=lambda: T0 + timedelta(minutes=next(ticks)))
    store.close()


def httpx_rule(svc, by="alice", **kw):
    return svc.propose(
        "Use httpx with a timeout; never requests.", title="HTTP client is httpx", proposed_by=by, **kw
    )


# --- propose -------------------------------------------------------------------------------------


def test_propose_defaults(svc):
    d = svc.propose("  Never commit .env  ", proposed_by="agent:bob")
    assert d.status is DecisionStatus.PROPOSED
    assert d.title == d.statement == "Never commit .env"
    assert d.scope == ["**"]
    assert d.decided_by is None
    assert svc.store.get_decision(d.id) == d
    assert svc.pending() == [d]


def test_propose_validates_input(svc):
    with pytest.raises(DecisionError, match="statement"):
        svc.propose("   ", proposed_by="alice")
    with pytest.raises(DecisionError, match="proposed_by"):
        svc.propose("x", proposed_by=" ")
    with pytest.raises(NotFound):
        svc.propose("x", proposed_by="alice", supersedes="missing")


def test_propose_scope_drops_blanks(svc):
    assert svc.propose("x", proposed_by="a", scope=["hive/**", " "]).scope == ["hive/**"]
    assert svc.propose("x", proposed_by="a", scope=[" "]).scope == ["**"]


# --- accept / reject + trust rule ----------------------------------------------------------------


def test_human_accepts(svc):
    d = httpx_rule(svc, by="agent:bob")  # agents may propose
    accepted = svc.accept(d.id, by="alice")
    assert accepted.status is DecisionStatus.ACCEPTED
    assert accepted.decided_by == "alice" and accepted.decided_at is not None
    assert svc.store.get_decision(d.id) == accepted
    assert svc.pending() == []


@pytest.mark.parametrize("actor", ["agent:alice", "Agent:bob", "  agent:x"])
def test_agents_cannot_accept_or_reject(svc, actor):
    d = httpx_rule(svc)
    with pytest.raises(AgentCannotDecide):
        svc.accept(d.id, by=actor)
    with pytest.raises(AgentCannotDecide):
        svc.reject(d.id, by=actor, reason="nope")
    with pytest.raises(PermissionError):  # also catchable as a plain PermissionError
        svc.accept(d.id, by=actor)
    assert svc.store.get_decision(d.id).status is DecisionStatus.PROPOSED


def test_is_agent():
    assert is_agent("agent:bob") and not is_agent("bob") and not is_agent("agentsmith")


def test_reject_records_reason(svc):
    d = httpx_rule(svc, rationale="sync + async in one lib")
    rejected = svc.reject(d.id, by="carol", reason="we need requests for a vendored SDK")
    assert rejected.status is DecisionStatus.REJECTED
    assert rejected.decided_by == "carol"
    assert rejected.rationale.startswith("sync + async in one lib")
    assert "Rejected by carol: we need requests" in rejected.rationale
    assert svc.in_scope() == []


@pytest.mark.parametrize("verb", ["accept", "reject"])
def test_only_proposed_decisions_can_be_decided(svc, verb):
    d = httpx_rule(svc)
    svc.accept(d.id, by="alice")
    with pytest.raises(DecisionError, match="only .* a proposed decision"):
        getattr(svc, verb)(d.id, by="bob")


def test_decide_requires_actor_and_existing_id(svc):
    d = httpx_rule(svc)
    with pytest.raises(DecisionError, match="`by` is required"):
        svc.accept(d.id, by="")
    with pytest.raises(NotFound):
        svc.accept("missing", by="alice")


# --- supersede -----------------------------------------------------------------------------------


def test_supersede_takes_effect_only_when_accepted(svc):
    old = svc.accept(httpx_rule(svc, scope=["hive/**"], tags=["http"]).id, by="alice")
    new = svc.supersede(old.id, "Use httpx; requests allowed in scripts/.", proposed_by="agent:bob")

    assert new.supersedes == old.id and new.status is DecisionStatus.PROPOSED
    assert new.scope == ["hive/**"] and new.tags == ["http"]  # inherited
    assert svc.store.get_decision(old.id).status is DecisionStatus.ACCEPTED  # still binding

    svc.accept(new.id, by="carol")
    assert svc.store.get_decision(old.id).status is DecisionStatus.SUPERSEDED
    assert [d.id for d in svc.in_scope()] == [new.id]


def test_supersede_rejected_replacement_keeps_old(svc):
    old = svc.accept(httpx_rule(svc).id, by="alice")
    new = svc.supersede(old.id, "Use requests", proposed_by="bob", scope=["scripts/**"])
    assert new.scope == ["scripts/**"]  # explicit override wins
    svc.reject(new.id, by="alice", reason="no")
    assert svc.store.get_decision(old.id).status is DecisionStatus.ACCEPTED


def test_cannot_supersede_non_accepted(svc):
    proposed = httpx_rule(svc)
    with pytest.raises(DecisionError, match="only supersede an accepted"):
        svc.supersede(proposed.id, "x", proposed_by="bob")


def test_competing_replacements_second_accept_fails(svc):
    old = svc.accept(httpx_rule(svc).id, by="alice")
    a = svc.supersede(old.id, "Option A", proposed_by="bob")
    b = svc.supersede(old.id, "Option B", proposed_by="carol")
    svc.accept(a.id, by="alice")
    with pytest.raises(DecisionError, match="no longer accepted"):
        svc.accept(b.id, by="alice")
    assert svc.store.get_decision(b.id).status is DecisionStatus.PROPOSED


# --- in_scope ------------------------------------------------------------------------------------


def test_in_scope_filters_accepted_by_paths(svc):
    repo_wide = svc.accept(svc.propose("Never commit .env", proposed_by="a").id, by="h")
    hr = svc.accept(svc.propose("Retry 429s", proposed_by="a", scope=["hive/hr/**"]).id, by="h")
    web = svc.accept(svc.propose("No Node build", proposed_by="a", scope=["hive/web/**"]).id, by="h")
    svc.propose("Pending only", proposed_by="a")  # never in scope

    ids = lambda ds: {d.id for d in ds}  # noqa: E731
    assert ids(svc.in_scope()) == {repo_wide.id, hr.id, web.id}
    assert ids(svc.in_scope(["hive/hr/client.py"])) == {repo_wide.id, hr.id}
    assert ids(svc.in_scope([".\\hive\\web\\x.html"])) == {repo_wide.id, web.id}  # Windows-style path
    assert ids(svc.in_scope(["hive/hr/a.py", "hive/web/b.html"])) == {repo_wide.id, hr.id, web.id}
    assert ids(svc.in_scope(["README.md"])) == {repo_wide.id}


# --- module-level API (matches the adapter in hive/api/app.py, PR #15) ---------------------------


def test_module_functions_with_store_kwarg():
    from hive.core import decisions

    store = SqliteStore(":memory:")
    fields = dict(  # exactly what the API's DecisionIn.model_dump() sends
        title="HTTP client is httpx", statement="Use httpx.", proposed_by="agent:bob", rationale="",
        scope=["hive/**"], supersedes=None, tags=[], source=None,
    )
    d = decisions.propose(**fields, store=store)
    assert decisions.accept(d.id, "alice", store=store).status is DecisionStatus.ACCEPTED
    assert [x.id for x in decisions.in_scope(["hive/x.py"], store=store)] == [d.id]
    p = decisions.propose(statement="x", proposed_by="a", store=store)
    assert decisions.reject(p.id, "alice", "no", store=store).status is DecisionStatus.REJECTED
    with pytest.raises(AgentCannotDecide):
        decisions.accept(p.id, "agent:bob", store=store)


def test_bare_string_scope_and_tags_are_one_item_not_characters(svc):
    # Regression: iterating "hive/**" would store a "*" glob, making the rule apply to every path.
    d = svc.propose("Use ruff", proposed_by="alice", scope="hive/**", tags="lint")
    assert d.scope == ["hive/**"] and d.tags == ["lint"]
    svc.accept(d.id, by="h")
    assert svc.in_scope(["README.md"]) == []
    with pytest.raises(DecisionError, match="expected strings"):
        svc.propose("x", proposed_by="a", scope=[3])


def test_evidence_is_stored_or_rejected_loudly(svc):
    from hive.models import Decision

    if "evidence" in Decision.model_fields:  # T-08 (#12) merged
        d = svc.propose("x", proposed_by="agent:a", evidence="no, use httpx")
        assert svc.store.get_decision(d.id).evidence == "no, use httpx"
    else:  # never silently dropped
        with pytest.raises(DecisionError, match="evidence"):
            svc.propose("x", proposed_by="agent:a", evidence="no, use httpx")
    assert svc.propose("y", proposed_by="a", evidence=None).statement == "y"


def test_module_supersede_and_keyword_only_propose():
    from hive.core import decisions

    store = SqliteStore(":memory:")
    old = decisions.propose(statement="Use httpx", proposed_by="a", scope="hive/**", store=store)
    decisions.accept(old.id, "alice", store=store)
    new = decisions.supersede(old.id, "Use httpx, timeout 10s", proposed_by="agent:b", store=store)
    assert new.supersedes == old.id and new.scope == ["hive/**"]
    decisions.accept(new.id, "alice", store=store)
    assert store.get_decision(old.id).status is DecisionStatus.SUPERSEDED
    with pytest.raises(TypeError):
        decisions.propose("positional", proposed_by="a", store=store)  # keyword-only, per ticket
