"""T-17: decisions board, via TestClient on SqliteStore.

`FakeDecisions` stands in for hive.core.decisions (T-10).
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from hive.api.app import create_app
from hive.models import Decision, DecisionStatus
from hive.store.base import ContextStore
from hive.store.sqlite import SqliteStore


class FakeDecisions:
    def __init__(self, store: ContextStore) -> None:
        self.store = store
        self.reasons: list[str] = []

    def propose(self, **fields) -> Decision:
        return self.store.put_decision(Decision(**fields))

    def _decide(self, decision_id: str, by: str, status: DecisionStatus) -> Decision:
        if by.startswith("agent:"):
            raise PermissionError("agents cannot decide")
        d = self.store.get_decision(decision_id)
        if d.status is not DecisionStatus.PROPOSED:
            raise ValueError(f"Decision is already {d.status.value}.")
        return self.store.put_decision(d.model_copy(update={"status": status, "decided_by": by}))

    def accept(self, decision_id: str, by: str) -> Decision:
        return self._decide(decision_id, by, DecisionStatus.ACCEPTED)

    def reject(self, decision_id: str, by: str, reason: str) -> Decision:
        self.reasons.append(reason)
        return self._decide(decision_id, by, DecisionStatus.REJECTED)


@pytest.fixture
def store():
    s = SqliteStore(":memory:")
    s.put_decision(
        Decision(
            id="old",
            title="HTTP client is requests",
            statement="Use requests.",
            status=DecisionStatus.SUPERSEDED,
            proposed_by="alice",
            decided_by="alice",
        )
    )
    s.put_decision(
        Decision(
            id="httpx",
            title="HTTP client is httpx",
            statement="Use httpx with a timeout; never requests.",
            scope=["hive/hr/**", "tests/**"],
            status=DecisionStatus.ACCEPTED,
            proposed_by="alice",
            decided_by="alice",
            supersedes="old",
        )
    )
    s.put_decision(
        Decision(
            id="browser",
            title="Browser never calls HarnessRouter",
            statement="Never call HarnessRouter from the browser.",
            rationale="It leaks the key.",
            scope=["hive/web/**"],
            proposed_by="agent:bob",
        )
    )
    yield s
    s.close()


@pytest.fixture
def service(store):
    return FakeDecisions(store)


@pytest.fixture
def client(store, service):
    return TestClient(create_app(store=store, decisions=service))


def column(html: str, key: str) -> str:
    """The HTML of one board column."""
    start = html.index(f'class="column column-{key}"')
    end = html.find('class="column column-', start + 1)
    return html[start : end if end != -1 else len(html)]


def test_page_renders_three_columns_with_cards_in_the_right_one(client):
    r = client.get("/decisions")
    assert r.status_code == 200
    html = r.text
    assert "Browser never calls HarnessRouter" in column(html, "proposed")
    assert "HTTP client is httpx" in column(html, "accepted")
    assert "HTTP client is requests" in column(html, "superseded")
    assert re.findall(r'<a href="([^"]+)"\s+aria-current="page"', html) == ["/decisions"]
    assert "Coming soon" not in html


def test_card_shows_agent_badge_scope_chips_and_supersede_links(client):
    html = client.get("/decisions").text
    proposed = column(html, "proposed")
    assert 'class="badge badge-agent">proposed by agent:bob' in proposed
    assert '<code class="chip">hive/web/**</code>' in proposed
    accepted = column(html, "accepted")
    assert 'Supersedes <a href="#d-old">HTTP client is requests</a>' in accepted
    assert "badge-agent" not in accepted
    assert 'Superseded by <a href="#d-httpx">HTTP client is httpx</a>' in column(html, "superseded")


def test_only_proposed_cards_have_accept_and_reject_buttons(client):
    html = client.get("/decisions").text
    assert 'hx-post="/ui/decisions/browser/accept"' in column(html, "proposed")
    assert 'hx-post="/ui/decisions/browser/reject"' in column(html, "proposed")
    assert "hx-post" not in column(html, "accepted")
    assert "hx-post" not in column(html, "superseded")


def test_human_accepts_agent_proposal_in_the_ui(client, store):
    r = client.post("/ui/decisions/browser/accept", data={"by": "alice"})
    assert r.status_code == 200
    assert r.text.lstrip().startswith(
        '<section id="board" data-swap-errors'
    )  # the partial, not the whole page
    assert "Browser never calls HarnessRouter" in column(r.text, "accepted")
    assert "accepted by @alice" in column(r.text, "accepted")
    # What `hive decisions` will read:
    decision = store.get_decision("browser")
    assert decision.status is DecisionStatus.ACCEPTED
    assert decision.decided_by == "alice"


def test_human_rejects_with_reason_from_the_prompt(client, store, service):
    r = client.post("/ui/decisions/browser/reject", data={"by": "alice"}, headers={"HX-Prompt": "too broad"})
    assert r.status_code == 200
    assert service.reasons == ["too broad"]
    assert store.get_decision("browser").status is DecisionStatus.REJECTED
    assert "Browser never calls HarnessRouter" not in column(r.text, "proposed")
    assert "1 rejected" in r.text


@pytest.mark.parametrize("verb", ["accept", "reject"])
@pytest.mark.parametrize("by", ["agent:bob", "AGENT:bob", " agent:bob ", "agent :bob"])
def test_agent_cannot_decide_through_the_ui(client, store, verb, by):
    r = client.post(f"/ui/decisions/browser/{verb}", data={"by": by})
    assert r.status_code == 403
    assert 'role="alert"' in r.text
    assert "only humans" in r.text
    assert store.get_decision("browser").status is DecisionStatus.PROPOSED


@pytest.mark.parametrize("data", [{}, {"by": ""}, {"by": "   "}])
def test_deciding_needs_a_handle(client, store, data):
    r = client.post("/ui/decisions/browser/accept", data=data)
    assert r.status_code == 422
    assert "GitHub handle" in r.text
    assert store.get_decision("browser").status is DecisionStatus.PROPOSED


def test_errors_re_render_the_board_with_a_banner(client):
    missing = client.post("/ui/decisions/nope/accept", data={"by": "alice"})
    assert missing.status_code == 404
    assert "Decision not found." in missing.text
    assert "Browser never calls HarnessRouter" in column(missing.text, "proposed")
    again = client.post("/ui/decisions/httpx/accept", data={"by": "alice"})
    assert again.status_code == 409
    assert "already accepted" in again.text


def test_unknown_verb_is_404(client, store):
    assert client.post("/ui/decisions/browser/supersede", data={"by": "alice"}).status_code == 404
    assert store.get_decision("browser").status is DecisionStatus.PROPOSED


def test_human_adds_a_decision_from_the_form(client, store):
    r = client.post(
        "/ui/decisions",
        data={
            "by": "carol",
            "title": "Dashboard is HTMX",
            "statement": "No Node build step.",
            "rationale": "Keep the stack small.",
            "scope": "hive/web/**, hive/api/**",
        },
    )
    assert r.status_code == 200
    assert "Dashboard is HTMX" in column(r.text, "proposed")
    (new,) = [d for d in store.list_decisions() if d.title == "Dashboard is HTMX"]
    assert new.status is DecisionStatus.PROPOSED  # still needs an explicit accept
    assert new.decided_by is None
    assert new.proposed_by == "carol"
    assert new.scope == ["hive/web/**", "hive/api/**"]
    assert new.source == "dashboard"


def test_form_defaults_scope_and_ignores_smuggled_status(client, store):
    r = client.post(
        "/ui/decisions",
        data={"by": "carol", "title": "T", "statement": "S", "status": "accepted", "decided_by": "carol"},
    )
    assert r.status_code == 200
    (new,) = [d for d in store.list_decisions() if d.title == "T"]
    assert new.scope == ["**"]
    assert new.status is DecisionStatus.PROPOSED
    assert new.decided_by is None


@pytest.mark.parametrize(
    "data",
    [
        {"title": "T", "statement": "S"},
        {"by": "carol", "title": "", "statement": "S"},
        {"by": "carol", "title": "T", "statement": " "},
        {"by": "agent:carol", "title": "T", "statement": "S"},
    ],
)
def test_form_rejects_incomplete_or_agent_submissions(client, store, data):
    r = client.post("/ui/decisions", data=data)
    assert r.status_code in (403, 422)
    assert 'role="alert"' in r.text
    assert len(store.list_decisions()) == 3


def test_decision_text_is_html_escaped(client, store):
    store.put_decision(
        Decision(
            title="<script>alert(1)</script>",
            statement='<img src=x onerror="alert(2)">',
            scope=["<b>**</b>"],
            proposed_by="agent:<i>mallory</i>",
        )
    )
    html = client.get("/ui/decisions/board").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x" not in html
    assert "<b>**</b>" not in html
    assert "<i>mallory</i>" not in html


def test_board_partial_polls_itself(client):
    html = client.get("/ui/decisions/board").text
    assert 'hx-get="/ui/decisions/board"' in html
    assert 'hx-trigger="every 5s [' in html  # paused while the rejected list is open
    assert "<html" not in html


def test_ui_writes_are_503_until_the_lifecycle_lands(store, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "hive.core.decisions", None)
    client = TestClient(create_app(store=store))
    r = client.post("/ui/decisions/browser/accept", data={"by": "alice"})
    assert r.status_code == 503
    assert "not available yet" in r.text
    assert client.get("/decisions").status_code == 200  # the board itself still renders


def test_board_stylesheet_is_linked_and_served(client):
    assert 'href="/static/decisions.css"' in client.get("/decisions").text
    assert client.get("/static/decisions.css").status_code == 200
