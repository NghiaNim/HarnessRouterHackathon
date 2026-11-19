"""Conflict detector for accepting decisions, with no LLM (T-63, detector half).

Before a human accepts a proposal, `candidates(proposal, store)` returns up to 3 **accepted** decisions that
could clash with it. A candidate needs both:

1. an **overlapping scope**: each glob's literal directory prefix is compared, conservatively. A false
   positive only costs the human one extra click, but rules for `hive/web/**` and `tests/**` never collide;
2. a **lexical signal**: a key term one rule forbids ("never/no/not/don't/avoid/without/instead of X") and
   the other prescribes (score >= 1 per opposed term), or a high key-term overlap (possible duplicate or
   restatement; score < 1).

`conflicting_pairs(decisions)` finds opposed pairs among already-accepted rules, so the brief can show them
together as "conflicting rules: ask your human" instead of as two independent orders.

The accept endpoints (API `/api/decisions/{id}/accept`, the `/ui` board, `hive accept`) use this to return
409 `possible_conflict` unless the human picks `supersede=<id>` or `keep_both=true`. That wiring lives in
tracks B/C. The LLM arbiter (T-22) can refine the ranking when it's available.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from hive.models import Decision, DecisionStatus, normalize_path
from hive.store.base import ContextStore

MAX_CANDIDATES = 3
OVERLAP_THRESHOLD = 0.5
OVERLAP_WEIGHT = 0.8

_CLAUSE = re.compile(r"[;,.:!?\n]+|\bbut\b|\bwhile\b")
_WORD = re.compile(r"[a-z0-9_][a-z0-9_'+.-]*[a-z0-9_]|[a-z0-9_]")
_NEGATORS = {
    "never",
    "no",
    "not",
    "don't",
    "dont",
    "avoid",
    "without",
    "stop",
    "ban",
    "forbid",
    "forbidden",
    "disallow",
    "nothing",
    "none",
    "neither",
    "nor",
    "can't",
    "cannot",
    "mustn't",
    "shouldn't",
}
_STOP = {
    "use",
    "uses",
    "using",
    "used",
    "always",
    "all",
    "every",
    "any",
    "for",
    "with",
    "the",
    "and",
    "from",
    "into",
    "onto",
    "our",
    "your",
    "its",
    "this",
    "that",
    "these",
    "those",
    "call",
    "calls",
    "calling",
    "code",
    "file",
    "files",
    "must",
    "should",
    "only",
    "prefer",
    "preferred",
    "set",
    "make",
    "keep",
    "via",
    "per",
    "each",
    "one",
    "are",
    "was",
    "has",
    "have",
    "when",
    "then",
    "than",
    "new",
    "also",
    "there",
    "here",
    "more",
    "less",
    "instead",
    "do",
    "does",
    "doing",
    "is",
    "be",
    "it",
    "to",
    "of",
    "in",
    "on",
    "at",
    "by",
    "as",
    "an",
    "a",
    "or",
    "we",
    "you",
    "they",
    "them",
    "will",
    "can",
    "may",
    "need",
    "needs",
    "rule",
    "rules",
    "same",
}


# --- scopes ---------------------------------------------------------------------------------------------


def _prefix(pattern: str) -> str | None:
    """Literal directory prefix a gitignore pattern is confined to ('' = anywhere, None = selects nothing)."""
    p = normalize_path(pattern.strip())
    if not p or p.startswith("!"):
        return None
    body = p.rstrip("/")
    if "/" not in body:
        return ""  # gitignore: a slash-less pattern matches at any depth
    glob_at = min((i for i, ch in enumerate(body) if ch in "*?["), default=None)
    if glob_at is None:
        return body + "/"  # a literal file or directory: itself and everything under it
    return body[:glob_at].rsplit("/", 1)[0] + "/" if "/" in body[:glob_at] else ""


def scopes_overlap(a: Iterable[str], b: Iterable[str]) -> bool:
    pa = [x for x in map(_prefix, a) if x is not None]
    pb = [x for x in map(_prefix, b) if x is not None]
    return any(x.startswith(y) or y.startswith(x) for x in pa for y in pb)


# --- lexical signal --------------------------------------------------------------------------------------


def polarities(text: str) -> dict[str, str]:
    return dict(_polarities(text))


@lru_cache(maxsize=4096)  # pure; conflicting_pairs is O(n^2) on the SessionStart hot path
def _polarities(text: str) -> tuple[tuple[str, str], ...]:
    """Key terms of a rule -> '+' (prescribed) or '-' (forbidden). '-' wins if a term appears both ways."""
    out: dict[str, str] = {}
    for clause in _CLAUSE.split(text.lower()):
        words = _WORD.findall(clause.replace("do not", "don't").replace("instead of", " insteadof "))
        sign = "+"
        for w in words:
            if w in _NEGATORS or w == "insteadof":
                sign = "-"
                continue
            if w in _STOP or len(w) < 3:
                continue
            out[w] = "-" if out.get(w) == "-" else sign
    return tuple(out.items())


def _terms(d: Decision) -> dict[str, str]:
    return dict(_polarities(f"{d.title}. {d.statement}"))


@dataclass(frozen=True)
class ConflictCandidate:
    decision: Decision
    score: float
    reason: str


def conflict(a: Decision, b: Decision) -> tuple[float, str] | None:
    """(score, reason) if `a` and `b` may clash; None otherwise. Score >= 1 means opposed terms."""
    if a.id == b.id or not scopes_overlap(a.scope, b.scope):
        return None
    ta, tb = _terms(a), _terms(b)
    shared = ta.keys() & tb.keys()
    union = ta.keys() | tb.keys()
    jaccard = len(shared) / len(union) if union else 0.0
    opposed = sorted(t for t in shared if ta[t] != tb[t])
    if opposed:
        parts = [
            f"'{t}' is {'forbidden' if ta[t] == '-' else 'prescribed'} by \"{a.title}\" but "
            f'{"forbidden" if tb[t] == "-" else "prescribed"} by "{b.title}"'
            for t in opposed
        ]
        return len(opposed) + OVERLAP_WEIGHT * jaccard, "; ".join(parts)
    if jaccard >= OVERLAP_THRESHOLD:
        return (
            OVERLAP_WEIGHT * jaccard,
            f"high term overlap ({jaccard:.0%}) on overlapping scopes: duplicate or restatement?",
        )
    return None


def candidates(
    decision: Decision, store: ContextStore, limit: int = MAX_CANDIDATES
) -> list[ConflictCandidate]:
    """Accepted decisions that may conflict with `decision`, best first."""
    found: list[ConflictCandidate] = []
    for other in store.list_decisions(status=DecisionStatus.ACCEPTED):
        hit = conflict(other, decision)
        if hit:
            found.append(ConflictCandidate(other, round(hit[0], 3), hit[1]))
    found.sort(key=lambda c: c.score, reverse=True)
    return found[:limit]


def conflicting_pairs(decisions: list[Decision]) -> list[tuple[Decision, Decision, str]]:
    """Pairs with opposed terms (score >= 1) among `decisions`, in input order. For the defensive brief."""
    pairs = []
    for i, a in enumerate(decisions):
        for b in decisions[i + 1 :]:
            hit = conflict(a, b)
            if hit and hit[0] >= 1.0:
                pairs.append((a, b, hit[1]))
    return pairs
