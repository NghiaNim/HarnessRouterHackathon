"""Decision lifecycle (T-10): propose -> accept | reject, and supersede.

State machine:

    PROPOSED --accept(human)--> ACCEPTED --(a replacement is accepted)--> SUPERSEDED
    PROPOSED --reject(human)--> REJECTED

Trust boundary (docs/PRODUCT.md, "Humans decide, agents propose"): anyone, agents included, may *propose*,
but `accept`/`reject` refuse any actor that starts with `agent:`. A replacement is proposed with `supersedes`
set; only when a human accepts it does the old decision become SUPERSEDED.

Depends only on the ContextStore protocol, so it works on SqliteStore and HarnessMemoryStore alike.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime

from hive.models import Decision, DecisionStatus, utcnow
from hive.store.base import ContextStore

AGENT_PREFIX = "agent:"


class DecisionError(ValueError):
    """Invalid decision operation (bad input or illegal state transition)."""


class AgentCannotDecide(DecisionError, PermissionError):
    """An agent tried to accept or reject. Only humans decide."""


def is_agent(actor: str) -> bool:
    return actor.strip().lower().startswith(AGENT_PREFIX)


def _normalize_path(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


class DecisionService:
    def __init__(self, store: ContextStore, clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self._clock = clock

    # --- proposing ---------------------------------------------------------------------------------

    def propose(
        self,
        statement: str,
        *,
        proposed_by: str,
        title: str | None = None,
        rationale: str = "",
        scope: Iterable[str] | None = None,
        tags: Iterable[str] | None = None,
        source: str | None = None,
        supersedes: str | None = None,
    ) -> Decision:
        """Record a PROPOSED decision. Agents and humans may both propose."""
        statement = statement.strip()
        if not statement:
            raise DecisionError("a decision needs a statement")
        if not proposed_by.strip():
            raise DecisionError("proposed_by is required")
        if supersedes is not None:
            old = self.store.get_decision(supersedes)  # raises NotFound
            if old.status is not DecisionStatus.ACCEPTED:
                raise DecisionError(
                    f"can only supersede an accepted decision; {old.id} is {old.status.value}"
                )
        scope_list = [s.strip() for s in (scope or []) if s.strip()] or ["**"]
        decision = Decision(
            title=(title or statement).strip()[:300],
            statement=statement,
            rationale=rationale.strip(),
            scope=scope_list,
            proposed_by=proposed_by.strip(),
            created_at=self._clock(),
            supersedes=supersedes,
            tags=list(tags or []),
            source=source,
        )
        return self.store.put_decision(decision)

    def supersede(self, old_id: str, new_statement: str, *, proposed_by: str, **fields) -> Decision:
        """Propose a replacement for an accepted decision. It takes effect when a human accepts it.

        Scope and tags default to the old decision's, since a replacement usually covers the same area.
        """
        old = self.store.get_decision(old_id)
        fields.setdefault("scope", old.scope)
        fields.setdefault("tags", old.tags)
        return self.propose(new_statement, proposed_by=proposed_by, supersedes=old_id, **fields)

    # --- deciding (humans only) --------------------------------------------------------------------

    def accept(self, decision_id: str, by: str) -> Decision:
        decision = self._decidable(decision_id, by, "accept")
        old: Decision | None = None
        if decision.supersedes:
            old = self.store.get_decision(decision.supersedes)
            if old.status is not DecisionStatus.ACCEPTED:
                raise DecisionError(
                    f"{decision.id} supersedes {old.id}, which is no longer accepted "
                    f"({old.status.value}); re-propose against the current decision"
                )
        now = self._clock()
        accepted = decision.model_copy(
            update={"status": DecisionStatus.ACCEPTED, "decided_by": by.strip(), "decided_at": now}
        )
        self.store.put_decision(accepted)
        if old is not None:
            self.store.put_decision(
                old.model_copy(update={"status": DecisionStatus.SUPERSEDED, "decided_at": now})
            )
        return accepted

    def reject(self, decision_id: str, by: str, reason: str = "") -> Decision:
        decision = self._decidable(decision_id, by, "reject")
        rationale = decision.rationale
        if reason.strip():
            # The contract has no dedicated field; keep the reason visible next to the rationale.
            rationale = f"{rationale}\n\nRejected by {by.strip()}: {reason.strip()}".strip()
        rejected = decision.model_copy(
            update={
                "status": DecisionStatus.REJECTED,
                "decided_by": by.strip(),
                "decided_at": self._clock(),
                "rationale": rationale,
            }
        )
        return self.store.put_decision(rejected)

    def _decidable(self, decision_id: str, by: str, verb: str) -> Decision:
        if not by.strip():
            raise DecisionError(f"`by` is required to {verb} a decision")
        if is_agent(by):
            raise AgentCannotDecide(
                f"{by} cannot {verb} decisions: agents may only propose; a human must decide"
            )
        decision = self.store.get_decision(decision_id)  # raises NotFound
        if decision.status is not DecisionStatus.PROPOSED:
            raise DecisionError(
                f"can only {verb} a proposed decision; {decision.id} is {decision.status.value}"
            )
        return decision

    # --- reading -----------------------------------------------------------------------------------

    def in_scope(self, paths: Iterable[str] | None = None) -> list[Decision]:
        """Accepted decisions that apply to any of `paths`. No paths: every accepted decision."""
        accepted = self.store.list_decisions(status=DecisionStatus.ACCEPTED)
        normalized = [_normalize_path(p) for p in (paths or []) if p.strip()]
        if not normalized:
            return accepted
        return [d for d in accepted if any(d.applies_to(p) for p in normalized)]

    def pending(self) -> list[Decision]:
        """Proposals awaiting a human, newest first."""
        return self.store.list_decisions(status=DecisionStatus.PROPOSED)
