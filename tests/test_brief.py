from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hive.core.brief import compose_brief, estimate_tokens
from hive.models import Decision, DecisionStatus, Handoff, ModuleNote
from hive.store.sqlite import SqliteStore

T0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


def at(hours: int) -> datetime:
    return T0 + timedelta(hours=hours)


@pytest.fixture
def store():
    s = SqliteStore(":memory:")
    A = DecisionStatus.ACCEPTED
    s.put_decision(
        Decision(
            id="dhttpx0001",
            title="HTTP client is httpx",
            statement="Use httpx; never requests.",
            scope=["hive/**/*.py"],
            status=A,
            proposed_by="alice",
            created_at=at(0),
        )
    )
    s.put_decision(
        Decision(
            id="dkey000002",
            title="Browser never sees the key",
            statement="Never call HarnessRouter from browser code.",
            scope=["hive/web/**"],
            status=A,
            proposed_by="carol",
            created_at=at(1),
        )
    )
    s.put_decision(
        Decision(
            id="dcache0003",
            title="Cache /v1/bases",
            statement="Cache bases for an hour.",
            scope=["hive/hr/**"],
            proposed_by="agent:bob",
            created_at=at(2),
        )
    )
    s.put_decision(
        Decision(
            id="dreq000004",
            title="Use requests",
            statement="Use requests.",
            status=DecisionStatus.REJECTED,
            proposed_by="agent:bob",
            created_at=at(3),
        )
    )
    # alice's own last session at t=10; bob before it (stale) and after it (fresh); carol after it.
    s.put_handoff(
        Handoff(
            id="halice0001", member="alice", branch="alice/feat/hr", summary="my own work", created_at=at(10)
        )
    )
    s.put_handoff(
        Handoff(
            id="hbobold001", member="bob", branch="bob/feat/old", summary="stale bob work", created_at=at(5)
        )
    )
    s.put_handoff(
        Handoff(
            id="hbob000002",
            member="bob",
            branch="bob/feat/cli",
            ticket="T-12",
            summary="CLI skeleton with decide/accept.",
            learned=["typer needs mix_stderr=False"],
            gotchas=["hive brief must stay < 1s"],
            created_at=at(12),
        )
    )
    s.put_handoff(
        Handoff(
            id="hcarol0003",
            member="carol",
            branch="carol/feat/api",
            summary="REST routes.",
            created_at=at(14),
        )
    )
    s.put_module_note(
        ModuleNote(
            path="hive/hr/client.py",
            summary="httpx HarnessRouter client.",
            key_symbols=["HRClient", "RunResult"],
            commit_sha="abc",
        )
    )
    s.put_module_note(ModuleNote(path="hive/web/app.js", summary="Dashboard JS.", commit_sha="abc"))
    return s


GOLDEN = """\
# Hive brief: alice @ alice/feat/hr (T-02)

## Team decisions (binding)
- **HTTP client is httpx**: Use httpx; never requests. _(scope: hive/**/*.py · id dhttpx00)_

## Since your last session
- **carol** on `carol/feat/api`: REST routes.
- **bob** on `bob/feat/cli` (T-12): CLI skeleton with decide/accept.
  - learned: typer needs mix_stderr=False
  - gotcha: hive brief must stay < 1s

## Codebase notes
- `hive/hr/client.py`: httpx HarnessRouter client. (HRClient, RunResult)

---
_Pending, not binding until a human accepts:_ Cache /v1/bases (agent:bob)
"""


def test_golden_brief(store):
    brief = compose_brief("alice", "alice/feat/hr", ["hive/hr/client.py"], ticket="T-02", store=store)
    assert brief.markdown == GOLDEN
    assert brief.decision_ids == ["dhttpx0001", "dcache0003"]
    assert brief.handoff_ids == ["hcarol0003", "hbob000002"]
    assert brief.module_paths == ["hive/hr/client.py"]
    assert brief.token_estimate == estimate_tokens(GOLDEN)
    assert (brief.member, brief.branch) == ("alice", "alice/feat/hr")


def test_no_changed_paths_means_all_accepted_decisions(store):
    brief = compose_brief("dave", "main", [], store=store)
    assert set(brief.decision_ids) >= {"dhttpx0001", "dkey000002"}
    assert "dreq000004" not in brief.decision_ids
    # dave has no previous session: all teammates' handoffs, newest first
    assert brief.handoff_ids == ["hcarol0003", "hbob000002", "halice0001", "hbobold001"]
    assert brief.module_paths == []


def test_scope_selects_web_decision(store):
    brief = compose_brief("bob", "bob/feat/ui", ["hive/web/app.js"], store=store)
    assert "dkey000002" in brief.decision_ids and "dhttpx0001" not in brief.decision_ids
    assert brief.module_paths == ["hive/web/app.js"]
    assert "dcache0003" not in brief.decision_ids  # pending, but out of scope


def test_budget_drops_lowest_priority_first_but_never_accepted_decisions(store):
    full = compose_brief("alice", "alice/feat/hr", ["hive/hr/client.py"], store=store)
    tight = compose_brief(
        "alice", "alice/feat/hr", ["hive/hr/client.py"], store=store, budget_tokens=full.token_estimate - 5
    )
    assert tight.token_estimate <= full.token_estimate - 5
    assert "dcache0003" not in tight.decision_ids  # pending goes first
    assert tight.handoff_ids == full.handoff_ids

    tiny = compose_brief("alice", "alice/feat/hr", ["hive/hr/client.py"], store=store, budget_tokens=10)
    assert tiny.decision_ids == ["dhttpx0001"]  # accepted in scope survives even over budget
    assert tiny.handoff_ids == [] and tiny.module_paths == []
    assert "omitted" in tiny.markdown and "hive recall" in tiny.markdown


def test_drops_oldest_handoff_before_newer(store):
    full = compose_brief("dave", "main", [], store=store)
    for budget in range(full.token_estimate - 1, 0, -1):
        b = compose_brief("dave", "main", [], store=store, budget_tokens=budget)
        if len(b.handoff_ids) < len(full.handoff_ids):
            assert b.handoff_ids == full.handoff_ids[: len(b.handoff_ids)]
            break


def test_empty_store():
    brief = compose_brief("alice", "main", ["x.py"], store=SqliteStore(":memory:"))
    assert "No team context yet" in brief.markdown
    assert brief.decision_ids == brief.handoff_ids == brief.module_paths == []


def test_directory_note_does_not_match_parent_siblings(store):
    store.put_module_note(ModuleNote(path="hive/hr", summary="HR package.", commit_sha="abc"))
    assert "hive/hr" not in compose_brief("x", "b", ["hive/models.py"], store=store).module_paths
    assert "hive/hr" in compose_brief("x", "b", ["hive/hr/new.py"], store=store).module_paths


def test_paths_are_normalized(store):
    brief = compose_brief("bob", "b", ["./hive\\web\\app.js"], store=store)
    assert "dkey000002" in brief.decision_ids
    assert brief.module_paths == ["hive/web/app.js"]
