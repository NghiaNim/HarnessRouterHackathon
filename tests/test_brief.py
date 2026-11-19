from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hive.core.brief import compose_brief, estimate_tokens, ticket_files
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


def test_no_changed_paths_and_no_focus_means_repo_wide_rules_only(store, tmp_path):
    store.put_decision(
        Decision(
            id="drepo00005",
            title="Conventional commits",
            statement="Use feat:/fix: prefixes.",
            scope=["**"],
            status=DecisionStatus.ACCEPTED,
            proposed_by="alice",
            created_at=at(4),
        )
    )
    brief = compose_brief("dave", "main", [], store=store, tickets_path=tmp_path / "missing.md")
    assert brief.decision_ids == ["drepo00005"]  # path-scoped rules don't apply when we know no paths
    assert "repo-wide rules only" in brief.markdown
    # dave has no previous session: all teammates' handoffs, newest first
    assert brief.handoff_ids == ["hcarol0003", "hbob000002", "halice0001", "hbobold001"]
    assert brief.module_paths == []


TICKETS_MD = """\
### T-02 · HarnessRouter HTTP client
- **Status:** CLAIMED @alice 2026-11-18
- **Files:** `hive/hr/client.py`, `tests/test_hr_client.py`

### T-27 · Dashboard shell
- **Status:** CLAIMED @carol 2026-11-18
- **Files:** `hive/web/templates/{base,page}.html`, routes in `hive/api/app.py` (`get_store()`, `HIVE_STORE`)
"""


def test_ticket_files_parses_backticked_paths_and_braces():
    assert ticket_files(TICKETS_MD, ticket="T-27") == [
        "hive/web/templates/base.html",
        "hive/web/templates/page.html",
        "hive/api/app.py",
    ]
    assert ticket_files(TICKETS_MD, member="alice") == ["hive/hr/client.py", "tests/test_hr_client.py"]
    assert ticket_files(TICKETS_MD, member="al") == []  # handle must match exactly
    assert ticket_files(TICKETS_MD) == []


def test_empty_paths_infer_focus_from_claimed_ticket(store, tmp_path):
    tickets = tmp_path / "TICKETS.md"
    tickets.write_text(TICKETS_MD)
    brief = compose_brief("alice", "alice/feat/hr", [], store=store, tickets_path=tickets)
    assert brief.decision_ids == ["dhttpx0001", "dcache0003"]  # scoped to hive/hr/client.py
    assert brief.module_paths == ["hive/hr/client.py"]
    assert "`hive/hr/client.py`" in brief.markdown and "No changes yet" in brief.markdown

    by_ticket = compose_brief("zed", "zed/x", [], ticket="T-27", store=store, tickets_path=tickets)
    assert "dkey000002" in by_ticket.decision_ids  # hive/web/** via the T-27 Files line


def test_empty_paths_infer_focus_from_this_branchs_handoffs(store, tmp_path):
    store.put_handoff(
        Handoff(
            member="erin",
            branch="erin/feat/web",
            summary="started UI",
            files_touched=["hive/web/app.js"],
            created_at=at(1),
        )
    )
    brief = compose_brief("erin", "erin/feat/web", [], store=store, tickets_path=tmp_path / "none.md")
    assert brief.decision_ids == ["dkey000002"]
    assert brief.module_paths == ["hive/web/app.js"]


def test_statement_equal_to_title_is_not_repeated(store):
    store.put_decision(
        Decision(
            id="dsame00006",
            title="Use ruff",
            statement="Use ruff",
            scope=["hive/**"],
            status=DecisionStatus.ACCEPTED,
            proposed_by="bob",
            created_at=at(6),
        )
    )
    md = compose_brief("x", "b", ["hive/a.py"], store=store).markdown
    assert "- **Use ruff** _(scope: hive/** · id dsame000)_" in md


def test_scope_selects_web_decision(store):
    brief = compose_brief("bob", "bob/feat/ui", ["hive/web/app.js"], store=store)
    assert "dkey000002" in brief.decision_ids and "dhttpx0001" not in brief.decision_ids
    assert brief.module_paths == ["hive/web/app.js"]
    assert "dcache0003" not in brief.decision_ids  # pending, but out of scope


def test_budget_drops_lowest_priority_first(store):
    full = compose_brief("alice", "alice/feat/hr", ["hive/hr/client.py"], store=store)
    tight = compose_brief(
        "alice", "alice/feat/hr", ["hive/hr/client.py"], store=store, budget_tokens=full.token_estimate - 5
    )
    assert tight.token_estimate <= full.token_estimate - 5
    assert "dcache0003" not in tight.decision_ids  # pending goes first
    assert tight.handoff_ids == full.handoff_ids
    assert "omitted" in tight.markdown and "hive recall" in tight.markdown


@pytest.mark.parametrize("paths", [["hive/hr/client.py"], []])
def test_budget_is_a_hard_cap_at_every_size(store, paths):
    full = compose_brief("alice", "alice/feat/hr", paths, store=store)
    for budget in range(1, full.token_estimate + 1):
        assert (
            compose_brief("alice", "alice/feat/hr", paths, store=store, budget_tokens=budget).token_estimate
            <= budget
        )


def test_dropped_rules_are_never_silent(store):
    for i in range(30):
        store.put_decision(
            Decision(
                title=f"Rule {i}",
                statement=f"Statement number {i} " * 5,
                scope=["hive/hr/**"],
                status=DecisionStatus.ACCEPTED,
                proposed_by="alice",
                created_at=at(i),
            )
        )
    brief = compose_brief("alice", "alice/feat/hr", ["hive/hr/client.py"], store=store, budget_tokens=400)
    assert brief.token_estimate <= 400
    n_rules = len([d for d in brief.decision_ids if d != "dcache0003"])
    assert 0 < n_rules < 31
    assert (
        f"+{31 - n_rules} more rule(s) apply" in brief.markdown and "hive_decisions(path=" in brief.markdown
    )
    assert brief.handoff_ids  # rules no longer starve teammates' handoffs


def test_more_specific_rules_come_first(store):
    store.put_decision(
        Decision(
            id="dexact0007",
            title="Client file rule",
            statement="Exact file.",
            scope=["hive/hr/client.py"],
            status=DecisionStatus.ACCEPTED,
            proposed_by="alice",
            created_at=at(0),
        )
    )
    brief = compose_brief("alice", "alice/feat/hr", ["hive/hr/client.py"], store=store)
    assert brief.decision_ids[:2] == ["dexact0007", "dhttpx0001"]


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


# --- T-59 perf fixture: 300 accepted rules, 1,000 handoffs ---------------------


@pytest.fixture
def big_store():
    s = SqliteStore(":memory:")
    scopes = [["**"], ["hive/api/**"], ["hive/**/*.py"], ["hive/web/**"], ["tests/**"]]
    for i in range(300):
        s.put_decision(
            Decision(
                title=f"Rule {i}: keep things consistent",
                statement=f"Do the consistent thing #{i}, always.",
                scope=scopes[i % len(scopes)],
                status=DecisionStatus.ACCEPTED,
                proposed_by="alice",
                created_at=at(i),
            )
        )
    for i in range(1000):
        s.put_handoff(
            Handoff(
                member=["bob", "carol"][i % 2],
                branch=f"feat/{i}",
                summary=f"Did piece {i} of the work.",
                learned=["a fact"],
                created_at=at(i),
            )
        )
    return s


@pytest.mark.parametrize("paths", [[], ["hive/api/app.py"]])
def test_big_store_respects_budget_and_keeps_handoffs(big_store, tmp_path, paths):
    brief = compose_brief("alice", "alice/feat/x", paths, store=big_store, tickets_path=tmp_path / "none.md")
    assert brief.token_estimate <= 2000
    assert len(brief.handoff_ids) >= 1
    applicable = [
        d
        for d in big_store.list_decisions(status=DecisionStatus.ACCEPTED)
        if (any(d.applies_to(p) for p in paths) if paths else "**" in d.scope)
    ]
    assert brief.decision_ids
    # every applicable rule is either shown or counted in the "+N more" pointer
    assert len(brief.decision_ids) == len(applicable) or (
        f"+{len(applicable) - len(brief.decision_ids)} more rule(s) apply" in brief.markdown
    )
