"""Brief composer: the token-budgeted context an agent gets at session start (T-11, T-59).

Sections, highest priority first:
  1. accepted decisions in scope (binding), most specific scope first
  2. teammates' handoffs related to my files (same file or directory, last HANDOFF_WINDOW), however old
  3. teammates' other handoffs since my last session
  4. module notes for the touched files/directories
  5. proposed decisions in scope, as a "pending" footnote
Handoffs are ranked by path overlap with my focus paths (same file > same directory > same top-level
package) plus a recency bonus that never outweighs one overlap tier (T-67).

The budget is a HARD cap (`token_estimate <= budget_tokens`, tokens = len/4). Rules first get at most
RULES_SHARE of it, so teammates' handoffs always get the rest they need; any budget left over at the end
flows back to rules. Only the top FULL_RULES rules show their statement; the rest are one line each, and
rules that still don't fit become "+N more rules apply" with a pointer to look them up. A binding rule is
never dropped silently.

"My last session" = my latest `brief_served` event or my latest handoff, whichever is newer.

With no changed paths (the normal SessionStart case), focus paths are inferred from the ticket's `Files:`
line in TICKETS.md (the given ticket, else the member's CLAIMED tickets) and this branch's previous
handoffs. With no focus at all, only repo-wide rules (`**`) apply.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hive.models import Brief, Decision, DecisionStatus, Handoff, ModuleNote, utcnow
from hive.store.base import ContextStore

MAX_LIST_ITEMS = 3  # learned/gotchas per handoff
MAX_SYMBOLS = 5
RULES_SHARE = 0.55  # first-pass cap for accepted rules; leftovers flow back to rules at the end
FULL_RULES = 8  # rules rendered with their full statement; the rest are compact
MAX_FOCUS_PATHS = 50
REPO_WIDE = "**"
HANDOFF_WINDOW = timedelta(days=7)
RELATED_SCORE = 2  # same directory or closer: shown even if older than my last session


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _in_scope(decision: Decision, paths: list[str]) -> bool:
    return not paths or any(decision.applies_to(p) for p in paths)


def _normalize(path: str) -> str:
    """Hooks may hand us Windows separators or `./`-prefixed paths; scopes are POSIX globs."""
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _note_touched(note: ModuleNote, paths: list[str]) -> bool:
    note_path = note.path.rstrip("/")
    for p in paths:
        if (
            p == note_path
            or p.startswith(note_path + "/")
            # sibling files share context, but only for FILE notes: a directory note `hive/hr` must not
            # match everything in `hive/`
            or (os.path.splitext(note_path)[1] and os.path.dirname(p) == os.path.dirname(note_path))
        ):
            return True
    return False


def _render_decision(d: Decision) -> str:
    statement = "" if d.statement.strip() == d.title.strip() else f": {d.statement}"
    return f"- **{d.title}**{statement} _(scope: {', '.join(d.scope)} · id {d.id[:8]})_"


def _render_decision_compact(d: Decision) -> str:
    return f"- **{d.title}** _(id {d.id[:8]})_"


def _more_rules(n: int) -> str:
    return f"_+{n} more rule(s) apply: call `hive_decisions(path=…)` or `hive decisions --path <file>`._"


def _specificity(d: Decision) -> float:
    """How narrow the rule's scope is: literal path segments count 1, partly-literal globs (`*.py`) 0.5."""

    def one(pattern: str) -> float:
        score = 0.0
        for seg in pattern.split("/"):
            if seg and not any(c in seg for c in "*?["):
                score += 1
            elif seg.strip("*"):
                score += 0.5
        return score

    return max((one(p) for p in d.scope), default=0.0)


def _render_handoff(h: Handoff, touched: str | None = None) -> str:
    ticket = f" ({h.ticket})" if h.ticket else ""
    where = f" _(touched `{touched}`)_" if touched else ""
    lines = [f"- **{h.member}** on `{h.branch}`{ticket}{where}: {h.summary}"]
    lines += [f"  - learned: {x}" for x in h.learned[:MAX_LIST_ITEMS]]
    lines += [f"  - gotcha: {x}" for x in h.gotchas[:MAX_LIST_ITEMS]]
    return "\n".join(lines)


def _render_note(n: ModuleNote) -> str:
    symbols = f" ({', '.join(n.key_symbols[:MAX_SYMBOLS])})" if n.key_symbols else ""
    return f"- `{n.path}`: {n.summary}{symbols}"


EMPTY = "No team context yet. Decisions and teammates' handoffs will show up here.\n"


def _omitted_line(n: int) -> str:
    return f'_{n} more item(s) omitted for budget; use `hive recall "<topic>"`._\n'


def _cost(block: str) -> int:
    """Tokens a block adds to the brief, including the blank line that joins it to the next one.

    Summing ceil'd per-block costs over-estimates the joined text, so packing by `_cost` keeps the cap hard.
    """
    return estimate_tokens(block + "\n") if block else 0


# --- packing: each returns (block, items taken). Greedy in priority order; stops at the first misfit. ---


def _pack_rules(rules: list[Decision], quota: int) -> tuple[str, list[Decision]]:
    if not rules:
        return "", []
    lines, taken = ["## Team decisions (binding)"], []
    for i, d in enumerate(rules):
        left_after = len(rules) - len(taken) - 1
        tail = [_more_rules(left_after)] if left_after else []
        renders = [_render_decision(d), _render_decision_compact(d)] if i < FULL_RULES else []
        renders = renders or [_render_decision_compact(d)]
        fit = next((r for r in renders if _cost("\n".join([*lines, r, *tail]) + "\n") <= quota), None)
        if fit is None:
            break
        lines.append(fit)
        taken.append(d)
    if len(taken) < len(rules):
        lines.append(_more_rules(len(rules) - len(taken)))
    block = "\n".join(lines) + "\n"
    return (block, taken) if _cost(block) <= quota else ("", [])


def _pack_lines(heading: str, rendered: list[tuple[str, object]], quota: int) -> tuple[str, list]:
    lines, taken = [heading], []
    for text, item in rendered:
        if _cost("\n".join([*lines, text]) + "\n") > quota:
            break
        lines.append(text)
        taken.append(item)
    return ("\n".join(lines) + "\n", taken) if taken else ("", [])


def _pack_handoffs(
    handoffs: list[Handoff], quota: int, heading: str = "## Since your last session", why: dict | None = None
) -> tuple[str, list[Handoff]]:
    why = why or {}
    rendered = [(_render_handoff(h, why.get(h.id)), h) for h in handoffs]
    block, taken = _pack_lines(heading, rendered, quota)
    if handoffs and not taken:
        # The top handoff alone is too big: cut its full entry (gotchas included) to fill the whole quota,
        # rather than dropping it or shrinking it to a headline that leaves room for lower-priority items.
        text = rendered[0][0]
        room = quota * 4 - len(heading) - 4  # chars: quota minus heading, 2 newlines, the joining "\n", "…"
        if room >= 40:
            block, taken = _pack_lines(heading, [(text[:room].rstrip() + "…", handoffs[0])], quota)
    return block, taken


def _pack_notes(notes: list[ModuleNote], quota: int) -> tuple[str, list[ModuleNote]]:
    return _pack_lines("## Codebase notes", [(_render_note(n), n) for n in notes], quota)


def _pack_pending(pending: list[Decision], quota: int) -> tuple[str, list[Decision]]:
    head, taken = "---\n_Pending, not binding until a human accepts:_ ", []
    for d in pending:
        items = "; ".join(f"{x.title} ({x.proposed_by})" for x in [*taken, d])
        if _cost(head + items + "\n") > quota:
            break
        taken.append(d)
    if not taken:
        return "", []
    return head + "; ".join(f"{x.title} ({x.proposed_by})" for x in taken) + "\n", taken


# --- handoff relevance (T-67) ---------------------------------------------------------------------------


def _overlap(touched: list[str], focus: list[str]) -> tuple[int, str | None]:
    """Best path overlap: 3 same file, 2 same directory (or inside a focus dir), 1 same top-level package."""
    best: tuple[int, str | None] = (0, None)
    for f in (_normalize(t) for t in touched if t.strip()):
        for p in focus:
            if f == p:
                score = 3
            elif f.startswith(p.rstrip("/") + "/") or (
                os.path.dirname(f) and os.path.dirname(f) == os.path.dirname(p)
            ):
                score = 2
            elif "/" in f and "/" in p and f.split("/", 1)[0] == p.split("/", 1)[0]:
                score = 1
            else:
                continue
            if score > best[0]:
                best = (score, f)
    return best


def _recency_bonus(h: Handoff, now: datetime) -> float:
    created = h.created_at if h.created_at.tzinfo else h.created_at.replace(tzinfo=UTC)
    age = max((now - created).total_seconds(), 0.0)
    return 0.5 * max(0.0, 1 - age / HANDOFF_WINDOW.total_seconds())  # < 1, so it never beats an overlap tier


def _last_seen(member: str, store: ContextStore) -> datetime | None:
    """When `member` last got context: their latest brief_served event or their latest handoff."""
    seen = [h.created_at for h in store.list_handoffs(member=member, limit=1)]
    list_events = getattr(store, "list_events", None)  # T-08; tolerate stores that predate it
    if list_events is not None:
        seen += [e.at for e in list_events(limit=500) if e.type == "brief_served" and e.actor == member][:1]
    seen = [t if t.tzinfo else t.replace(tzinfo=UTC) for t in seen]
    return max(seen, default=None)


def select_handoffs(
    member: str, focus: list[str], store: ContextStore, now: datetime
) -> tuple[list[Handoff], list[Handoff], dict[str, str]]:
    """(related to my files, new since my last session, {handoff id: overlapping path}), each best first."""
    last = _last_seen(member, store)
    candidates = [h for h in store.list_handoffs(since=now - HANDOFF_WINDOW, limit=500) if h.member != member]
    scored = {h.id: _overlap(h.files_touched, focus) for h in candidates}

    def rank(h: Handoff) -> float:
        return scored[h.id][0] + _recency_bonus(h, now)

    related = sorted((h for h in candidates if scored[h.id][0] >= RELATED_SCORE), key=rank, reverse=True)
    related_ids = {h.id for h in related}

    def is_new(h: Handoff) -> bool:
        created = h.created_at if h.created_at.tzinfo else h.created_at.replace(tzinfo=UTC)
        return last is None or created > last

    recent = sorted((h for h in candidates if h.id not in related_ids and is_new(h)), key=rank, reverse=True)
    why = {h.id: path for h in related if (path := scored[h.id][1])}
    return related, recent, why


# --- focus inference for an empty change set (T-59) ------------------------------------------------------

_SECTION = re.compile(r"^### (T-\d+) .*?(?=^### |\Z)", re.M | re.S)
_FILES = re.compile(r"\*\*Files:\*\*(.*)")
_PATHLIKE = re.compile(r"^[\w.{},*/-]+$")


def _expand_braces(token: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", token)
    if not m:
        return [token]
    return [x for opt in m[1].split(",") for x in _expand_braces(token[: m.start()] + opt + token[m.end() :])]


def ticket_files(tickets_md: str, ticket: str | None = None, member: str | None = None) -> list[str]:
    """Paths on the `Files:` line of `ticket`, or of every ticket CLAIMED by `member` if no ticket given."""
    out: list[str] = []
    for m in _SECTION.finditer(tickets_md):
        section = m[0]
        if ticket is not None:
            if m[1] != ticket:
                continue
        elif member is None or not re.search(rf"\*\*Status:\*\* CLAIMED @{re.escape(member)}\b", section):
            continue
        files = _FILES.search(section)
        for token in re.findall(r"`([^`]+)`", files[1] if files else ""):
            if _PATHLIKE.match(token) and ("/" in token or re.search(r"\.\w+$", token)):
                out += _expand_braces(token)
    return out


def _focus_paths(
    member: str, branch: str, ticket: str | None, store: ContextStore, tickets_path: Path | None
) -> list[str]:
    paths: list[str] = []
    tickets_file = tickets_path if tickets_path is not None else Path("TICKETS.md")
    try:
        paths += ticket_files(tickets_file.read_text(), ticket=ticket, member=None if ticket else member)
    except OSError:
        pass
    for h in store.list_handoffs(branch=branch, limit=5):
        paths += h.files_touched
    return list(dict.fromkeys(_normalize(p) for p in paths if p.strip()))[:MAX_FOCUS_PATHS]


def compose_brief(
    member: str,
    branch: str,
    changed_paths: list[str],
    ticket: str | None = None,
    budget_tokens: int = 2000,
    *,
    store: ContextStore | None = None,
    tickets_path: Path | None = None,
    now: datetime | None = None,
) -> Brief:
    if store is None:
        from hive.store import get_store

        store = get_store()
    paths = [_normalize(p) for p in changed_paths if p.strip()]
    focus_note = ""
    if not paths:
        paths = _focus_paths(member, branch, ticket, store, tickets_path)
        shown = ", ".join(f"`{p}`" for p in paths[:3]) + (
            f" +{len(paths) - 3} more" if len(paths) > 3 else ""
        )
        focus_note = (
            f"_No changes yet; rules for the expected files: {shown}._"
            if paths
            else "_No changes yet; showing repo-wide rules only._"
        )

    def in_focus(d: Decision) -> bool:
        return any(d.applies_to(p) for p in paths) if paths else REPO_WIDE in d.scope

    accepted = [d for d in store.list_decisions(status=DecisionStatus.ACCEPTED) if in_focus(d)]
    accepted.sort(key=lambda d: (_specificity(d), d.decided_at or d.created_at), reverse=True)
    pending = [d for d in store.list_decisions(status=DecisionStatus.PROPOSED) if in_focus(d)]

    related, recent, why = select_handoffs(member, paths, store, now or utcnow())

    notes = [n for n in store.list_module_notes() if paths and _note_touched(n, paths)]

    header = f"# Hive brief: {member} @ {branch}" + (f" ({ticket})" if ticket else "") + "\n"
    if focus_note:
        header += focus_note + "\n"

    def build(
        reserve: int,
    ) -> tuple[str, list[Decision], list[Handoff], list[Handoff], list[ModuleNote], list[Decision], int]:
        avail = budget_tokens - _cost(header) - reserve
        # Pass 1: rules capped at RULES_SHARE, then each lower section gets what's left, in priority order.

        def pack_related(items: list[Handoff], quota: int) -> tuple[str, list[Handoff]]:
            return _pack_handoffs(items, quota, "## Related to your files", why)

        packers = [
            (_pack_rules, accepted),
            (pack_related, related),
            (_pack_handoffs, recent),
            (_pack_notes, notes),
            (_pack_pending, pending),
        ]
        quotas = [int(avail * RULES_SHARE)] + [0] * (len(packers) - 1)
        blocks, taken = [""] * len(packers), [[] for _ in packers]
        for i, (pack, items) in enumerate(packers):
            if i:
                quotas[i] = avail - sum(_cost(b) for b in blocks)
            blocks[i], taken[i] = pack(items, quotas[i])
        # Pass 2: whatever is left over flows back up, to the highest-priority section that dropped items.
        for i, (pack, items) in enumerate(packers):
            leftover = avail - sum(_cost(b) for b in blocks)
            if leftover > 0 and len(taken[i]) < len(items):
                blocks[i], taken[i] = pack(items, _cost(blocks[i]) + leftover)
        omitted = sum(len(items) - len(t) for (_, items), t in zip(packers[1:], taken[1:], strict=True))
        body = [b for b in blocks if b]
        if omitted:
            body.append(_omitted_line(omitted))
        if not any(blocks) and not omitted and not accepted:
            body.append(EMPTY)
        return "\n".join([header, *body]), *taken, omitted

    markdown, rules, kept_related, kept_recent, kept_notes, kept_pending, omitted = build(0)
    if omitted or estimate_tokens(markdown) > budget_tokens:
        most_omitted = len(related) + len(recent) + len(notes) + len(pending)
        reserve = _cost(_omitted_line(most_omitted)) if most_omitted else _cost(EMPTY)
        markdown, rules, kept_related, kept_recent, kept_notes, kept_pending, omitted = build(reserve)
    if estimate_tokens(markdown) > budget_tokens:  # a budget smaller than the header itself
        markdown = markdown[: max(budget_tokens, 0) * 4]

    return Brief(
        member=member,
        branch=branch,
        markdown=markdown,
        token_estimate=estimate_tokens(markdown),
        decision_ids=[d.id for d in rules] + [d.id for d in kept_pending],
        handoff_ids=[h.id for h in kept_related + kept_recent],
        module_paths=[n.path for n in kept_notes],
    )
