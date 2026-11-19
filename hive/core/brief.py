"""Brief composer: the token-budgeted context an agent gets at session start (T-11).

Priority, highest first:
  1. accepted decisions in scope of the changed paths (binding, so NEVER dropped, even over budget)
  2. teammates' handoffs since my last session (newest first, my own skipped)
  3. module notes for the touched files/directories
  4. proposed decisions in scope, as a "pending" footnote
Over budget, items are dropped lowest priority first (pending, then module notes, then the oldest handoffs).
Tokens are estimated as len/4.

"My last session" = the newest handoff by `member`. With no changed paths (fresh branch) every accepted
decision is in scope, since the agent could touch anything.
"""

from __future__ import annotations

import os

from hive.models import Brief, Decision, DecisionStatus, Handoff, ModuleNote
from hive.store.base import ContextStore

MAX_LIST_ITEMS = 3  # learned/gotchas per handoff
MAX_SYMBOLS = 5


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _in_scope(decision: Decision, paths: list[str]) -> bool:
    return not paths or any(decision.applies_to(p) for p in paths)


def _note_touched(note: ModuleNote, paths: list[str]) -> bool:
    note_path = note.path.rstrip("/")
    for p in paths:
        if (
            p == note_path
            or p.startswith(note_path + "/")
            or os.path.dirname(p) == os.path.dirname(note_path)
        ):
            return True
    return False


def _render_decision(d: Decision) -> str:
    return f"- **{d.title}**: {d.statement} _(scope: {', '.join(d.scope)} · id {d.id[:8]})_"


def _render_handoff(h: Handoff) -> str:
    ticket = f" ({h.ticket})" if h.ticket else ""
    lines = [f"- **{h.member}** on `{h.branch}`{ticket}: {h.summary}"]
    lines += [f"  - learned: {x}" for x in h.learned[:MAX_LIST_ITEMS]]
    lines += [f"  - gotcha: {x}" for x in h.gotchas[:MAX_LIST_ITEMS]]
    return "\n".join(lines)


def _render_note(n: ModuleNote) -> str:
    symbols = f" ({', '.join(n.key_symbols[:MAX_SYMBOLS])})" if n.key_symbols else ""
    return f"- `{n.path}`: {n.summary}{symbols}"


def _render(
    header: str,
    accepted: list[Decision],
    handoffs: list[Handoff],
    notes: list[ModuleNote],
    pending: list[Decision],
    omitted: int,
) -> str:
    blocks = [header + "\n"]
    if accepted:
        blocks.append("## Team decisions (binding)\n" + "".join(_render_decision(d) + "\n" for d in accepted))
    if handoffs:
        blocks.append("## Since your last session\n" + "".join(_render_handoff(h) + "\n" for h in handoffs))
    if notes:
        blocks.append("## Codebase notes\n" + "".join(_render_note(n) + "\n" for n in notes))
    if pending:
        items = "; ".join(f"{d.title} ({d.proposed_by})" for d in pending)
        blocks.append(f"---\n_Pending, not binding until a human accepts:_ {items}\n")
    if omitted:
        blocks.append(f'_{omitted} more item(s) omitted for budget; use `hive recall "<topic>"`._\n')
    if len(blocks) == 1:
        blocks.append("No team context yet. Decisions and teammates' handoffs will show up here.\n")
    return "\n".join(blocks)


def compose_brief(
    member: str,
    branch: str,
    changed_paths: list[str],
    ticket: str | None = None,
    budget_tokens: int = 2000,
    *,
    store: ContextStore | None = None,
) -> Brief:
    if store is None:
        from hive.store import get_store

        store = get_store()
    paths = list(changed_paths)

    accepted = [d for d in store.list_decisions(status=DecisionStatus.ACCEPTED) if _in_scope(d, paths)]
    pending = [d for d in store.list_decisions(status=DecisionStatus.PROPOSED) if _in_scope(d, paths)]

    mine = store.list_handoffs(member=member, limit=1)
    since = mine[0].created_at if mine else None
    handoffs = [h for h in store.list_handoffs(since=since, limit=50) if h.member != member]

    notes = [n for n in store.list_module_notes() if paths and _note_touched(n, paths)]

    header = f"# Hive brief: {member} @ {branch}" + (f" ({ticket})" if ticket else "")

    # Drop order: pending (last first), then notes (last first), then handoffs (oldest first).
    omitted = 0
    markdown = _render(header, accepted, handoffs, notes, pending, omitted)
    while estimate_tokens(markdown) > budget_tokens:
        for bucket in (pending, notes, handoffs):
            if bucket:
                bucket.pop()
                break
        else:
            break  # only accepted decisions left: those are never dropped
        omitted += 1
        markdown = _render(header, accepted, handoffs, notes, pending, omitted)

    return Brief(
        member=member,
        branch=branch,
        markdown=markdown,
        token_estimate=estimate_tokens(markdown),
        decision_ids=[d.id for d in accepted] + [d.id for d in pending],
        handoff_ids=[h.id for h in handoffs],
        module_paths=[n.path for n in notes],
    )
