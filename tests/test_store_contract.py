"""Shared contract suite for every ContextStore implementation.

Each store fixture param runs the same tests. HarnessMemoryStore joins the params (live only, HIVE_LIVE=1)
once it lands; until then this suite pins down SqliteStore's behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hive.models import Decision, DecisionStatus, Handoff, ModuleNote, RecallHit
from hive.store.base import ContextStore, NotFound
from hive.store.sqlite import SqliteStore

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


@pytest.fixture(params=["sqlite"])
def store(request, tmp_path) -> ContextStore:
    if request.param == "sqlite":
        return SqliteStore(tmp_path / "hive.db")
    raise AssertionError(request.param)


def make_decision(**kw) -> Decision:
    base = dict(title="HTTP client is httpx", statement="Use httpx; never requests.", proposed_by="alice")
    return Decision(**{**base, **kw})


def make_handoff(**kw) -> Handoff:
    base = dict(member="alice", branch="alice/feat/x", summary="did a thing")
    return Handoff(**{**base, **kw})


def test_implements_protocol(store):
    assert isinstance(store, ContextStore)


# --- decisions -------------------------------------------------------------


def test_decision_roundtrip_is_exact(store):
    d = make_decision(
        rationale="sync + async",
        scope=["hive/hr/**"],
        tags=["http"],
        source="PR #3",
        supersedes="abc",
        created_at=at(0),
    )
    store.put_decision(d)
    assert store.get_decision(d.id) == d


def test_get_missing_decision_raises_notfound(store):
    with pytest.raises(NotFound):
        store.get_decision("nope")


def test_put_decision_replaces_by_id(store):
    d = store.put_decision(make_decision())
    accepted = d.model_copy(
        update={"status": DecisionStatus.ACCEPTED, "decided_by": "bob", "decided_at": at(5)}
    )
    store.put_decision(accepted)
    assert store.get_decision(d.id).status is DecisionStatus.ACCEPTED
    assert len(store.list_decisions()) == 1


def test_list_decisions_newest_first_and_filters(store):
    old = store.put_decision(make_decision(title="old", created_at=at(0), scope=["hive/hr/**"]))
    new = store.put_decision(
        make_decision(title="new", created_at=at(10), status=DecisionStatus.ACCEPTED, scope=["hive/web/**"])
    )
    assert [d.id for d in store.list_decisions()] == [new.id, old.id]
    assert [d.id for d in store.list_decisions(status=DecisionStatus.ACCEPTED)] == [new.id]
    assert [d.id for d in store.list_decisions(path="hive/hr/client.py")] == [old.id]
    assert store.list_decisions(status=DecisionStatus.ACCEPTED, path="hive/hr/client.py") == []


# --- handoffs --------------------------------------------------------------


def test_handoff_roundtrip_and_filters(store):
    h1 = store.put_handoff(make_handoff(created_at=at(0), learned=["x"], files_touched=["a.py"]))
    h2 = store.put_handoff(make_handoff(member="bob", branch="bob/feat/y", created_at=at(10)))
    h3 = store.put_handoff(make_handoff(created_at=at(20), ticket="T-04"))

    assert store.list_handoffs() == [h3, h2, h1]
    assert store.list_handoffs(member="alice") == [h3, h1]
    assert store.list_handoffs(branch="bob/feat/y") == [h2]
    assert store.list_handoffs(since=at(10)) == [h3]  # strictly after `since`
    assert store.list_handoffs(limit=2) == [h3, h2]


def test_handoff_since_accepts_naive_datetime_as_utc(store):
    h = store.put_handoff(make_handoff(created_at=at(30)))
    assert store.list_handoffs(since=at(0).replace(tzinfo=None)) == [h]


# --- module notes ----------------------------------------------------------


def test_module_note_upserts_by_path(store):
    store.put_module_note(ModuleNote(path="hive/hr/client.py", summary="v1", commit_sha="a"))
    n2 = store.put_module_note(ModuleNote(path="hive/hr/client.py", summary="v2", commit_sha="b"))
    notes = store.list_module_notes()
    assert notes == [n2]


def test_list_module_notes_prefix(store):
    a = store.put_module_note(ModuleNote(path="hive/hr/client.py", summary="client", commit_sha="a"))
    b = store.put_module_note(ModuleNote(path="hive/store/sqlite.py", summary="store", commit_sha="a"))
    assert store.list_module_notes() == [a, b]  # sorted by path
    assert store.list_module_notes(prefix="hive/hr") == [a]


# --- recall ----------------------------------------------------------------


def test_recall_ranks_and_filters_by_kind(store):
    d = store.put_decision(make_decision(title="HTTP client is httpx", statement="Use httpx with a timeout."))
    store.put_decision(make_decision(title="Dashboard uses HTMX", statement="No Node build step."))
    h = store.put_handoff(make_handoff(summary="Wired httpx retries", learned=["429 needs backoff"]))
    n = store.put_module_note(ModuleNote(path="hive/hr/client.py", summary="httpx wrapper", commit_sha="a"))

    hits = store.recall("httpx timeout")
    assert all(isinstance(hit, RecallHit) for hit in hits)
    assert hits[0].item == d  # matches both terms
    assert {hit.item.id for hit in hits} == {d.id, h.id, n.id}
    assert hits == sorted(hits, key=lambda x: x.score, reverse=True)

    only_modules = store.recall("httpx", kinds=["module"])
    assert [(hit.kind, hit.item) for hit in only_modules] == [("module", n)]

    assert len(store.recall("httpx", limit=1)) == 1
    assert store.recall("kubernetes") == []
    assert store.recall("   ") == []
