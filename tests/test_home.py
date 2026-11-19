"""T-69: the dashboard explains itself (home page, briefing preview, example data)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hive.api.app import create_app
from hive.models import Decision, DecisionStatus, Handoff
from hive.store.sqlite import SqliteStore


@pytest.fixture
def store():
    s = SqliteStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def client(store):
    return TestClient(create_app(store=store))


def test_home_explains_hive_in_plain_language(client):
    html = client.get("/").text
    assert "Hive keeps everyone's coding agents on the same page." in html
    steps = (
        "You correct your agent",
        "A human approves it",
        "Every agent starts informed",
        "Mistakes get caught",
    )
    for step in steps:
        assert step in html
    assert "See what an agent is told" in html
    assert 'hx-get="/ui/briefing"' in html


def test_empty_store_offers_example_data_and_seeding_fills_every_section(client, store):
    assert "Load example data" in client.get("/").text
    r = client.post("/ui/example-data")
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/"
    statuses = sorted(d.status.value for d in store.list_decisions())
    assert statuses == ["accepted", "accepted", "proposed"]
    assert {h.member for h in store.list_handoffs()} == {"bob", "carol"}
    assert all("example" in d.tags for d in store.list_decisions())
    html = client.get("/").text
    assert "Load example data" not in html
    assert "HTTP client is httpx" in html and "Built the REST API for team rules." in html


def test_example_data_never_touches_a_store_that_has_real_data(client, store):
    store.put_decision(Decision(title="Real rule", statement="Real.", proposed_by="alice"))
    client.post("/ui/example-data")
    assert [d.title for d in store.list_decisions()] == ["Real rule"]


def test_home_counts_follow_the_loop(client, store):
    store.put_decision(Decision(title="Waiting", statement="w", proposed_by="agent:bob"))
    store.put_decision(
        Decision(title="Active", statement="a", proposed_by="agent:bob", decided_by="alice",
                 status=DecisionStatus.ACCEPTED, scope=["hive/api/**"])
    )
    store.put_handoff(Handoff(member="carol", branch="carol/x", summary="did x"))
    html = client.get("/").text
    assert "<b>1</b> waiting for a human" in html
    assert "<b>1</b> active rule →" in html
    assert "<b>1</b> session note from 1 teammate" in html  # only note authors count


def test_briefing_preview_shows_exactly_the_rules_for_the_file(client, store):
    store.put_decision(
        Decision(title="HTTP client is httpx", statement="Use httpx.", proposed_by="agent:bob",
                 decided_by="alice", status=DecisionStatus.ACCEPTED, scope=["hive/**"])
    )
    store.put_decision(
        Decision(title="Tests are offline", statement="No network in tests.", proposed_by="agent:bob",
                 decided_by="alice", status=DecisionStatus.ACCEPTED, scope=["tests/**"])
    )
    in_scope = client.get("/ui/briefing", params={"member": "dana", "path": "hive/api/app.py"}).text
    assert "HTTP client is httpx" in in_scope and "Tests are offline" not in in_scope
    assert "tokens" in in_scope and "dana" in in_scope and "1 binding rule" in in_scope
    other = client.get("/ui/briefing", params={"member": "dana", "path": "tests/test_x.py"}).text
    assert "Tests are offline" in other and "HTTP client is httpx" not in other


def test_briefing_preview_reports_a_missing_composer_instead_of_crashing(store):
    from fastapi import HTTPException

    def broken(*a, **k):
        raise HTTPException(503, "Brief composer (hive.core.brief) is not available yet.")

    client = TestClient(create_app(store=store, compose_brief=broken))
    r = client.get("/ui/briefing", params={"member": "dana", "path": "x.py"})
    assert r.status_code == 200 and "not available yet" in r.text


def test_rules_page_uses_plain_language(client):
    html = client.get("/decisions").text
    assert "<h1>Team rules</h1>" in html
    for column in ("Waiting for a human", "Active rules", "Replaced"):
        assert column in html
    assert "Your name" in html
