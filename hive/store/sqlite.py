"""SqliteStore: local/offline ContextStore, also the default store for tests.

Each model is stored as its full pydantic JSON (so it round-trips exactly) plus a few indexed columns used for
filtering and ordering. Timestamps are normalized to fixed-width UTC strings so they sort lexicographically.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from hive.models import Decision, DecisionStatus, Handoff, ItemKind, ModuleNote, RecallHit
from hive.store.base import NotFound

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoffs (
    id TEXT PRIMARY KEY,
    member TEXT NOT NULL,
    branch TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS handoffs_created ON handoffs(created_at);
CREATE TABLE IF NOT EXISTS module_notes (
    path TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
"""

_WORD = re.compile(r"[a-z0-9_]{2,}")


def _ts(dt: datetime) -> str:
    """Fixed-width UTC timestamp; naive datetimes are treated as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _terms(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class SqliteStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One connection shared across threads (FastAPI sync handlers use a threadpool), guarded by a lock.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _write(self, sql: str, params: tuple) -> None:
        with self._lock, self._conn:
            self._conn.execute(sql, params)

    # --- decisions -----------------------------------------------------------

    def put_decision(self, decision: Decision) -> Decision:
        self._write(
            "INSERT OR REPLACE INTO decisions (id, status, created_at, data) VALUES (?, ?, ?, ?)",
            (decision.id, decision.status.value, _ts(decision.created_at), decision.model_dump_json()),
        )
        return decision

    def get_decision(self, decision_id: str) -> Decision:
        rows = self._rows("SELECT data FROM decisions WHERE id = ?", (decision_id,))
        if not rows:
            raise NotFound(decision_id)
        return Decision.model_validate_json(rows[0][0])

    def list_decisions(self, status: DecisionStatus | None = None, path: str | None = None) -> list[Decision]:
        sql, params = "SELECT data FROM decisions", ()
        if status is not None:
            sql, params = sql + " WHERE status = ?", (DecisionStatus(status).value,)
        decisions = [
            Decision.model_validate_json(r[0]) for r in self._rows(sql + " ORDER BY created_at DESC", params)
        ]
        if path is not None:
            decisions = [d for d in decisions if d.applies_to(path)]
        return decisions

    # --- handoffs ------------------------------------------------------------

    def put_handoff(self, handoff: Handoff) -> Handoff:
        self._write(
            "INSERT OR REPLACE INTO handoffs (id, member, branch, created_at, data) VALUES (?, ?, ?, ?, ?)",
            (handoff.id, handoff.member, handoff.branch, _ts(handoff.created_at), handoff.model_dump_json()),
        )
        return handoff

    def list_handoffs(
        self,
        member: str | None = None,
        branch: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[Handoff]:
        where, params = [], []
        if member is not None:
            where.append("member = ?")
            params.append(member)
        if branch is not None:
            where.append("branch = ?")
            params.append(branch)
        if since is not None:
            where.append("created_at > ?")
            params.append(_ts(since))
        sql = "SELECT data FROM handoffs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [Handoff.model_validate_json(r[0]) for r in self._rows(sql, tuple(params))]

    # --- codebase map --------------------------------------------------------

    def put_module_note(self, note: ModuleNote) -> ModuleNote:
        self._write(
            "INSERT OR REPLACE INTO module_notes (path, data) VALUES (?, ?)",
            (note.path, note.model_dump_json()),
        )
        return note

    def list_module_notes(self, prefix: str | None = None) -> list[ModuleNote]:
        notes = [
            ModuleNote.model_validate_json(r[0])
            for r in self._rows("SELECT data FROM module_notes ORDER BY path")
        ]
        if prefix is not None:
            notes = [n for n in notes if n.path.startswith(prefix)]
        return notes

    # --- search --------------------------------------------------------------

    def recall(self, query: str, kinds: list[ItemKind] | None = None, limit: int = 8) -> list[RecallHit]:
        """Keyword score: fraction of query terms matched, plus a small bonus for repeated occurrences."""
        q_terms = list(dict.fromkeys(_terms(query)))
        if not q_terms:
            return []
        wanted = set(kinds) if kinds else {"decision", "handoff", "module"}

        candidates: list[tuple[ItemKind, Decision | Handoff | ModuleNote, str]] = []
        if "decision" in wanted:
            for d in self.list_decisions():
                text = " ".join([d.title, d.statement, d.rationale, *d.scope, *d.tags])
                candidates.append(("decision", d, text))
        if "handoff" in wanted:
            for h in self.list_handoffs(limit=1000):
                text = " ".join(
                    [
                        h.summary,
                        h.branch,
                        *h.learned,
                        *h.gotchas,
                        *h.files_touched,
                        *h.open_questions,
                        *h.next_steps,
                    ]
                )
                candidates.append(("handoff", h, text))
        if "module" in wanted:
            for n in self.list_module_notes():
                text = " ".join([n.path, n.summary, *n.key_symbols, *n.depends_on])
                candidates.append(("module", n, text))

        hits: list[RecallHit] = []
        for kind, item, text in candidates:
            words = _terms(text)
            matched = [t for t in q_terms if any(t in w for w in words)]
            if not matched:
                continue
            occurrences = sum(1 for w in words for t in matched if t in w)
            score = len(matched) / len(q_terms) + min(occurrences, 10) * 0.01
            hits.append(
                RecallHit(kind=kind, item=item, score=round(score, 4), why="matched: " + ", ".join(matched))
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]
