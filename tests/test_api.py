"""T-16: REST API on SqliteStore, via FastAPI TestClient. No network, no key.

`FakeDecisions` and `fake_compose_brief` stand in for hive.core.decisions (T-10) and hive.core.brief (T-11).
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from hive.api.app import create_app
from hive.models import Brief, Decision, DecisionStatus, Handoff, ModuleNote
from hive.store.base import ContextStore
from hive.store.sqlite import SqliteStore

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


class FakeDecisions:
    """Minimal stand-in for the T-10 lifecycle, working on any ContextStore."""

    def __init__(self, store: ContextStore) -> None:
        self.store = store

    def propose(self, **fields) -> Decision:
        return self.store.put_decision(Decision(**fields))

    def _decide(self, decision_id: str, by: str, status: DecisionStatus) -> Decision:
        if by.startswith("agent:"):
            raise PermissionError("agents cannot decide")
        d = self.store.get_decision(decision_id)
        if d.status is not DecisionStatus.PROPOSED:
            raise ValueError(f"decision is already {d.status.value}")
        return self.store.put_decision(d.model_copy(update={"status": status, "decided_by": by}))

    def accept(self, decision_id: str, by: str) -> Decision:
        return self._decide(decision_id, by, DecisionStatus.ACCEPTED)

    def reject(self, decision_id: str, by: str, reason: str) -> Decision:
        return self._decide(decision_id, by, DecisionStatus.REJECTED)


def fake_compose_brief(
    member, branch, changed_paths, ticket=None, budget_tokens=2000, *, store=None
) -> Brief:
    accepted = [
        d
        for d in store.list_decisions(status=DecisionStatus.ACCEPTED)
        if any(d.applies_to(p) for p in changed_paths)
    ]
    markdown = "\n".join(f"- {d.title}" for d in accepted)
    return Brief(
        member=member,
        branch=branch,
        markdown=markdown,
        token_estimate=min(len(markdown) // 4, budget_tokens),
        decision_ids=[d.id for d in accepted],
    )


@pytest.fixture
def store():
    s = SqliteStore(":memory:")
    s.put_decision(
        Decision(
            id="d-httpx",
            title="HTTP client is httpx",
            statement="Use httpx with a timeout; never requests.",
            scope=["hive/hr/**"],
            status=DecisionStatus.ACCEPTED,
            proposed_by="alice",
            decided_by="alice",
            created_at=T0,
        )
    )
    s.put_decision(
        Decision(
            id="d-browser",
            title="Browser never calls HarnessRouter",
            statement="Never call HarnessRouter from the browser.",
            scope=["hive/web/**", "hive/api/**"],
            proposed_by="agent:bob",
            created_at=T0 + timedelta(minutes=5),
        )
    )
    s.put_handoff(
        Handoff(id="h-old", member="bob", branch="bob/feat/api", summary="Sketched routes", created_at=T0)
    )
    s.put_handoff(
        Handoff(
            id="h-new",
            member="carol",
            branch="carol/feat/store",
            summary="Sqlite recall uses keyword scoring",
            created_at=T0 + timedelta(hours=1),
        )
    )
    s.put_module_note(
        ModuleNote(path="hive/hr/client.py", summary="HarnessRouter HTTP client", commit_sha="abc")
    )
    s.put_module_note(ModuleNote(path="hive/store/sqlite.py", summary="Local store", commit_sha="abc"))
    yield s
    s.close()


@pytest.fixture
def client(store):
    return TestClient(
        create_app(store=store, decisions=FakeDecisions(store), compose_brief=fake_compose_brief)
    )


# --- decisions: read -------------------------------------------------------
def test_list_decisions_newest_first(client):
    r = client.get("/api/decisions")
    assert r.status_code == 200
    assert [d["id"] for d in r.json()] == ["d-browser", "d-httpx"]


def test_list_decisions_filters_by_status_and_path(client):
    assert [d["id"] for d in client.get("/api/decisions", params={"status": "accepted"}).json()] == [
        "d-httpx"
    ]
    assert [d["id"] for d in client.get("/api/decisions", params={"path": "hive/api/app.py"}).json()] == [
        "d-browser"
    ]
    assert client.get("/api/decisions", params={"status": "accepted", "path": "hive/api/app.py"}).json() == []


def test_list_decisions_rejects_unknown_status(client):
    assert client.get("/api/decisions", params={"status": "approved"}).status_code == 422


# --- decisions: propose ----------------------------------------------------
def test_propose_creates_a_proposed_decision(client, store):
    r = client.post(
        "/api/decisions",
        json={
            "title": "Timeouts everywhere",
            "statement": "Always pass a timeout.",
            "proposed_by": "agent:carol",
            "scope": ["hive/**"],
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "proposed"
    assert body["decided_by"] is None
    assert body["proposed_by"] == "agent:carol"
    assert store.get_decision(body["id"]).scope == ["hive/**"]


@pytest.mark.parametrize("smuggled", [{"status": "accepted"}, {"decided_by": "alice"}, {"id": "d-httpx"}])
def test_propose_cannot_set_status_decider_or_id(client, store, smuggled):
    payload = {"title": "Sneaky", "statement": "Pre-accepted rule.", "proposed_by": "agent:bob", **smuggled}
    assert client.post("/api/decisions", json=payload).status_code == 422
    assert len(store.list_decisions()) == 2
    assert store.get_decision("d-httpx").title == "HTTP client is httpx"


def test_propose_requires_title_statement_and_proposer(client):
    assert client.post("/api/decisions", json={"title": "x", "statement": "y"}).status_code == 422
    assert (
        client.post("/api/decisions", json={"title": "", "statement": "y", "proposed_by": "a"}).status_code
        == 422
    )


# --- decisions: accept / reject (humans only) ------------------------------
def test_human_accepts_an_agent_proposal(client, store):
    r = client.post("/api/decisions/d-browser/accept", json={"by": "alice"})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    assert r.json()["decided_by"] == "alice"
    assert store.get_decision("d-browser").status is DecisionStatus.ACCEPTED


def test_human_rejects_with_a_reason(client, store):
    r = client.post("/api/decisions/d-browser/reject", json={"by": "alice", "reason": "too broad"})
    assert r.status_code == 200
    assert store.get_decision("d-browser").status is DecisionStatus.REJECTED


@pytest.mark.parametrize("action", ["accept", "reject"])
@pytest.mark.parametrize("by", ["agent:bob", "Agent:bob", "  agent:bob", "agent :bob", "agent：bob"])
def test_agents_can_never_accept_or_reject(client, store, action, by):
    r = client.post(f"/api/decisions/d-browser/{action}", json={"by": by})
    assert r.status_code == 403
    assert "only humans" in r.json()["detail"]
    assert store.get_decision("d-browser").status is DecisionStatus.PROPOSED


def test_agent_actor_is_refused_before_the_service_is_called(store):
    class Permissive(FakeDecisions):
        def _decide(self, decision_id, by, status):
            raise AssertionError("service must not be reached for an agent actor")

    client = TestClient(create_app(store=store, decisions=Permissive(store)))
    assert client.post("/api/decisions/d-browser/accept", json={"by": "agent:bob"}).status_code == 403


@pytest.mark.parametrize("body", [{}, {"by": ""}, {"by": "   "}, {"by": "alice", "status": "accepted"}])
def test_accept_requires_a_named_actor_and_nothing_else(client, store, body):
    assert client.post("/api/decisions/d-browser/accept", json=body).status_code == 422
    assert store.get_decision("d-browser").status is DecisionStatus.PROPOSED


def test_accept_unknown_decision_is_404(client):
    assert client.post("/api/decisions/nope/accept", json={"by": "alice"}).status_code == 404


def test_deciding_twice_is_409(client):
    assert client.post("/api/decisions/d-httpx/reject", json={"by": "alice"}).status_code == 409


def test_service_permission_error_maps_to_403(store):
    class Strict(FakeDecisions):
        def accept(self, decision_id, by):
            raise PermissionError("alice is not allowed")

    client = TestClient(create_app(store=store, decisions=Strict(store)))
    r = client.post("/api/decisions/d-browser/accept", json={"by": "alice"})
    assert r.status_code == 403
    assert r.json()["detail"] == "alice is not allowed"


# --- handoffs, modules, recall ---------------------------------------------
def test_list_handoffs_newest_first_with_filters(client):
    assert [h["id"] for h in client.get("/api/handoffs").json()] == ["h-new", "h-old"]
    assert [h["id"] for h in client.get("/api/handoffs", params={"member": "bob"}).json()] == ["h-old"]
    assert [h["id"] for h in client.get("/api/handoffs", params={"branch": "carol/feat/store"}).json()] == [
        "h-new"
    ]
    since = (T0 + timedelta(minutes=30)).isoformat()
    assert [h["id"] for h in client.get("/api/handoffs", params={"since": since}).json()] == ["h-new"]
    assert [h["id"] for h in client.get("/api/handoffs", params={"limit": 1}).json()] == ["h-new"]


def test_handoffs_limit_is_bounded(client):
    assert client.get("/api/handoffs", params={"limit": 0}).status_code == 422
    assert client.get("/api/handoffs", params={"limit": 100000}).status_code == 422


def test_list_modules_with_prefix(client):
    assert len(client.get("/api/modules").json()) == 2
    notes = client.get("/api/modules", params={"prefix": "hive/hr"}).json()
    assert [n["path"] for n in notes] == ["hive/hr/client.py"]


def test_recall_returns_typed_hits(client):
    hits = client.get("/api/recall", params={"q": "httpx timeout"}).json()
    assert hits
    assert hits[0]["kind"] == "decision"
    assert hits[0]["item"]["id"] == "d-httpx"


def test_recall_filters_by_kind(client):
    hits = client.get("/api/recall", params={"q": "sqlite store", "kinds": ["handoff"]}).json()
    assert hits
    assert {h["kind"] for h in hits} == {"handoff"}


def test_recall_requires_a_query_and_known_kinds(client):
    assert client.get("/api/recall").status_code == 422
    assert client.get("/api/recall", params={"q": ""}).status_code == 422
    assert client.get("/api/recall", params={"q": "x", "kinds": ["secrets"]}).status_code == 422


# --- brief -----------------------------------------------------------------
def test_brief_passes_member_branch_and_paths(client):
    r = client.get(
        "/api/brief",
        params={"member": "carol", "branch": "carol/feat/x", "paths": ["hive/hr/client.py", "README.md"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert (body["member"], body["branch"]) == ("carol", "carol/feat/x")
    assert body["decision_ids"] == ["d-httpx"]
    assert "HTTP client is httpx" in body["markdown"]


def test_brief_forwards_ticket_and_budget(store):
    seen = {}

    def composer(member, branch, changed_paths, ticket=None, budget_tokens=2000, *, store=None):
        seen.update(paths=changed_paths, ticket=ticket, budget=budget_tokens, store=store)
        return Brief(member=member, branch=branch, markdown="", token_estimate=0)

    client = TestClient(create_app(store=store, compose_brief=composer))
    r = client.get(
        "/api/brief", params={"member": "a", "branch": "b", "ticket": "T-16", "budget_tokens": 500}
    )
    assert r.status_code == 200
    assert seen == {"paths": [], "ticket": "T-16", "budget": 500, "store": store}


def test_brief_requires_member_and_branch(client):
    assert client.get("/api/brief", params={"member": "carol"}).status_code == 422


# --- wiring to the real core modules ---------------------------------------
def test_503_until_the_core_modules_land(store, monkeypatch):
    # None in sys.modules makes the import fail, whether or not the module exists on this branch.
    monkeypatch.setitem(sys.modules, "hive.core.decisions", None)
    monkeypatch.setitem(sys.modules, "hive.core.brief", None)
    client = TestClient(create_app(store=store))
    assert client.post("/api/decisions/d-browser/accept", json={"by": "alice"}).status_code == 503
    assert client.get("/api/brief", params={"member": "a", "branch": "b"}).status_code == 503
    assert client.get("/api/decisions").status_code == 200  # reads need only the store


def test_core_modules_are_picked_up_and_bound_to_the_store(store, monkeypatch):
    decisions = types.ModuleType("hive.core.decisions")
    fake = FakeDecisions(store)
    calls = []

    def propose(*, store, **fields):
        calls.append(store)
        return fake.propose(**fields)

    def accept(decision_id, by, *, store):
        calls.append(store)
        return fake.accept(decision_id, by)

    def reject(decision_id, by, reason, *, store):
        calls.append(store)
        return fake.reject(decision_id, by, reason)

    decisions.propose, decisions.accept, decisions.reject = propose, accept, reject
    brief = types.ModuleType("hive.core.brief")
    brief.compose_brief = fake_compose_brief
    monkeypatch.setitem(sys.modules, "hive.core.decisions", decisions)
    monkeypatch.setitem(sys.modules, "hive.core.brief", brief)

    client = TestClient(create_app(store=store))
    new = client.post("/api/decisions", json={"title": "t", "statement": "s", "proposed_by": "agent:bob"})
    assert new.status_code == 201
    assert client.post(f"/api/decisions/{new.json()['id']}/reject", json={"by": "alice"}).status_code == 200
    assert client.post("/api/decisions/d-browser/accept", json={"by": "alice"}).status_code == 200
    assert calls == [store, store, store]
    assert client.get("/api/brief", params={"member": "a", "branch": "b"}).status_code == 200


def test_store_is_built_lazily_from_the_factory(monkeypatch, tmp_path):
    db = tmp_path / "hive.db"
    monkeypatch.setenv("HIVE_STORE", "sqlite")
    monkeypatch.setenv("HIVE_DB_PATH", str(db))
    client = TestClient(create_app())
    assert not db.exists()
    assert client.get("/api/decisions").json() == []
    assert db.exists()


def test_unusable_store_is_503(monkeypatch):
    monkeypatch.setenv("HIVE_STORE", "redis")
    r = TestClient(create_app()).get("/api/decisions")
    assert r.status_code == 503
    assert "redis" in r.json()["detail"]


def test_module_level_app_exposes_the_routes():
    from hive.api.app import app

    paths = set(app.openapi()["paths"])
    assert paths == {
        "/api/decisions",
        "/api/decisions/{decision_id}/accept",
        "/api/decisions/{decision_id}/reject",
        "/api/handoffs",
        "/api/modules",
        "/api/recall",
        "/api/brief",
    }


def test_service_validation_error_is_422_without_pydantic_internals(store):
    class Sloppy(FakeDecisions):
        def propose(self, **fields):
            return Decision(**{**fields, "title": "x" * 301})

    client = TestClient(create_app(store=store, decisions=Sloppy(store)))
    r = client.post("/api/decisions", json={"title": "t", "statement": "s", "proposed_by": "bob"})
    assert r.status_code == 422
    assert r.json()["detail"] == "The decision is not valid."


def test_optional_module_only_swallows_the_module_itself(monkeypatch):
    from hive.api import app as app_module

    def missing(name):
        def fake_import(_):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)

        return fake_import

    monkeypatch.setattr(app_module.importlib, "import_module", missing("hive.core.decisions"))
    assert app_module._optional_module("hive.core.decisions") is None
    monkeypatch.setattr(app_module.importlib, "import_module", missing("hive.core"))
    assert app_module._optional_module("hive.core.decisions") is None
    monkeypatch.setattr(app_module.importlib, "import_module", missing("hive.core.dec"))
    with pytest.raises(ModuleNotFoundError):
        app_module._optional_module("hive.core.decisions")
    monkeypatch.setattr(app_module.importlib, "import_module", missing("some_dependency"))
    with pytest.raises(ModuleNotFoundError):
        app_module._optional_module("hive.core.decisions")


def test_store_is_built_once_when_first_requests_race(monkeypatch):
    import threading
    import time

    import hive.store

    built = []

    def slow_build():
        time.sleep(0.05)
        built.append(SqliteStore(":memory:"))
        return built[-1]

    monkeypatch.setattr(hive.store, "get_store", slow_build)
    client = TestClient(create_app())
    threads = [threading.Thread(target=client.get, args=("/api/decisions",)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1
