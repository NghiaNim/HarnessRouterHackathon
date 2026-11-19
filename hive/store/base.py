"""Storage contract.

CONTRACT FILE: implemented by SqliteStore (store/sqlite.py) and HarnessMemoryStore (store/memory.py).
Everything above the store (core/, cli, mcp, api) depends ONLY on this protocol, never on a concrete store.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from hive.models import (
    Decision,
    DecisionStatus,
    Handoff,
    HiveEvent,
    Intent,
    ItemKind,
    ModuleNote,
    RecallHit,
)


class NotFound(KeyError):
    pass


@runtime_checkable
class ContextStore(Protocol):
    # --- decisions ---------------------------------------------------------
    def put_decision(self, decision: Decision) -> Decision:
        """Insert or replace by decision.id."""
        ...

    def get_decision(self, decision_id: str) -> Decision:
        """Raise NotFound if missing."""
        ...

    def list_decisions(
        self, status: DecisionStatus | None = None, path: str | None = None
    ) -> list[Decision]:
        """Filter by status and/or by `Decision.applies_to(path)`. Newest first."""
        ...

    # --- handoffs ----------------------------------------------------------
    def put_handoff(self, handoff: Handoff) -> Handoff: ...

    def list_handoffs(
        self,
        member: str | None = None,
        branch: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[Handoff]:
        """Newest first."""
        ...

    # --- codebase map ------------------------------------------------------
    def put_module_note(self, note: ModuleNote) -> ModuleNote:
        """Upsert by note.path."""
        ...

    def list_module_notes(self, prefix: str | None = None) -> list[ModuleNote]: ...

    # --- search ------------------------------------------------------------
    def recall(
        self, query: str, kinds: list[ItemKind] | None = None, limit: int = 8
    ) -> list[RecallHit]:
        """Relevance-ranked search across kinds (all kinds if None)."""
        ...

    # --- activity feed (T-08) ------------------------------------------------
    def log_event(self, event: HiveEvent) -> HiveEvent:
        """Append-only."""
        ...

    def list_events(self, since: datetime | None = None, limit: int = 50) -> list[HiveEvent]:
        """Events strictly after `since` (naive = UTC). Newest first."""
        ...

    # --- intents (T-08) ------------------------------------------------------
    def put_intent(self, intent: Intent) -> Intent:
        """Upsert by (member, branch): the new intent fully replaces the old one."""
        ...

    def list_intents(self, active_within_minutes: float | None = 15) -> list[Intent]:
        """Intents where `is_active(active_within_minutes)`, or all if None. Newest heartbeat first."""
        ...
