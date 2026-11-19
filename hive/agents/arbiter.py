"""Arbiter: is a proposed decision a duplicate of, or in contradiction with, an existing one? (T-22)

    verdict = arbitrate(decision_id, client=client, store=store)

Call it right after `propose` (distiller, API, CLI). It recalls the top-k similar *live* decisions
(accepted or proposed), runs `hive-arbiter` on them, and records the verdict on the proposal as tags so the
dashboard can warn a human before they accept:

    arbiter:checked                  always, once arbitrated
    arbiter:duplicate:<id>           same rule as <id>
    arbiter:contradicts:<id>         can't both be followed
    arbiter:unavailable              the harness failed; nothing was checked

The arbiter never changes a decision's status: humans decide. Ids the model returns that weren't among the
candidates are dropped. With no similar decisions, no paid call is made. Input is redacted (T-28).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from hive.core.redact import Redactor
from hive.hr.client import HRError
from hive.hr.harnesses import HARNESS_MAP_PATH, JSONOutputError, run_json
from hive.models import ArbiterVerdict, Decision, DecisionStatus, HiveEvent
from hive.store.base import ContextStore

log = logging.getLogger("hive.arbiter")

HARNESS = "hive-arbiter"
ARBITER_TAG = "arbiter:"
MAX_COST_USD = 0.25
TOP_K = 5
_LIVE = {DecisionStatus.ACCEPTED, DecisionStatus.PROPOSED}


def similar_decisions(proposal: Decision, store: ContextStore, k: int = TOP_K) -> list[Decision]:
    hits = store.recall(f"{proposal.title} {proposal.statement}", kinds=["decision"], limit=k + 5)
    out: list[Decision] = []
    for hit in hits:
        d = hit.item
        if isinstance(d, Decision) and d.id != proposal.id and d.status in _LIVE:
            out.append(d)
    return out[:k]


def _brief(d: Decision) -> dict[str, Any]:
    return {
        "id": d.id,
        "title": d.title,
        "statement": d.statement,
        "scope": d.scope,
        "status": d.status.value,
    }


def _tags_for(verdict: ArbiterVerdict) -> list[str]:
    tags = [f"{ARBITER_TAG}checked"]
    if verdict.duplicate_of:
        tags.append(f"{ARBITER_TAG}duplicate:{verdict.duplicate_of}")
    tags += [f"{ARBITER_TAG}contradicts:{i}" for i in verdict.contradicts]
    return tags


def verdict_from_tags(decision: Decision) -> ArbiterVerdict:
    """Read a stored verdict back (for the dashboard / brief)."""
    dup, contra = None, []
    for t in decision.tags:
        if t.startswith(f"{ARBITER_TAG}duplicate:"):
            dup = t.split(":", 2)[2]
        elif t.startswith(f"{ARBITER_TAG}contradicts:"):
            contra.append(t.split(":", 2)[2])
    return ArbiterVerdict(duplicate_of=dup, contradicts=contra)


def _save(store: ContextStore, proposal: Decision, tags: list[str], note: str) -> None:
    kept = [t for t in proposal.tags if not t.startswith(ARBITER_TAG)]
    store.put_decision(proposal.model_copy(update={"tags": kept + tags}))
    log_event = getattr(store, "log_event", None)  # optional store method (T-08)
    if log_event is not None:
        log_event(
            HiveEvent(
                type="check_run",
                actor="agent:hive-arbiter",
                ref_id=proposal.id,
                summary=f"arbiter: {note}"[:300],
            )
        )


def arbitrate(
    decision_id: str,
    *,
    client: Any,
    store: ContextStore,
    k: int = TOP_K,
    env_values: dict[str, str] | None = None,
    map_path=HARNESS_MAP_PATH,
) -> ArbiterVerdict:
    proposal = store.get_decision(decision_id)
    candidates = similar_decisions(proposal, store, k)
    if not candidates:
        verdict = ArbiterVerdict(note="No similar decisions.")
        _save(store, proposal, _tags_for(verdict), verdict.note)
        return verdict

    payload = {"proposal": _brief(proposal), "existing": [_brief(d) for d in candidates]}
    text = Redactor(env_values).redact(
        "Proposed decision and similar existing decisions (JSON):\n" + json.dumps(payload, indent=2)
    )
    try:
        raw: ArbiterVerdict = run_json(
            client,
            HARNESS,
            text,
            ArbiterVerdict,
            map_path=map_path,
            max_cost_usd=MAX_COST_USD,
            metadata={"hive": "arbiter"},
        )
    except (HRError, JSONOutputError) as e:  # degrade: a human still reviews every proposal
        log.warning("arbiter unavailable: %s", e)
        verdict = ArbiterVerdict(
            note=f"Arbiter unavailable ({getattr(e, 'code', 'bad output')}); not checked."
        )
        _save(store, proposal, [f"{ARBITER_TAG}unavailable"], verdict.note)
        return verdict

    known = {d.id for d in candidates}
    verdict = ArbiterVerdict(
        duplicate_of=raw.duplicate_of if raw.duplicate_of in known else None,
        contradicts=[i for i in dict.fromkeys(raw.contradicts) if i in known],
        note=raw.note,
    )
    _save(store, proposal, _tags_for(verdict), verdict.note or "checked")
    return verdict
