"""Hive REST API (T-16). Run with `uv run uvicorn hive.api.app:app --reload`.

The server is the only place that talks to the store (and, through it, HarnessRouter): the HarnessRouter
key stays in the server process and no endpoint returns it.

Everything here depends on the `ContextStore` protocol plus two collaborators injected into `create_app`:

- a `DecisionService` (the decision lifecycle, `hive.core.decisions`, T-10)
- a brief composer (`hive.core.brief.compose_brief`, T-11)

When they are not injected they are resolved lazily from those modules, and an endpoint that needs a
module which has not landed yet answers 503 instead of failing at import time.

Trust rule: agents may propose decisions, only humans accept or reject. The lifecycle service enforces
it, and this layer refuses an `agent:` actor on accept/reject before the service is even called.
"""

from __future__ import annotations

import importlib
import re
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any, Protocol

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hive.models import Brief, Decision, DecisionStatus, Handoff, ItemKind, ModuleNote, RecallHit
from hive.store.base import ContextStore, NotFound

# An agent actor: "agent:bob", tolerant of case, padding and a full-width colon.
_AGENT_ACTOR = re.compile(r"\s*agent\s*[:\uff1a]", re.IGNORECASE)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=WEB_DIR / "templates")

# Dashboard nav, in order: (key, label, path). base.html renders it; each page marks its own key active.
NAV = [
    ("radar", "Radar", "/radar"),
    ("decisions", "Decisions", "/decisions"),
    ("timeline", "Timeline", "/timeline"),
    ("ask", "Ask", "/ask"),
    ("impact", "Impact", "/impact"),
]
templates.env.globals["nav"] = NAV

# Sections that are still a "coming soon" page: key -> what will be there. The ticket that builds a section
# removes its entry here and adds its own route and template (which extends base.html).
PLACEHOLDERS = {
    "radar": "Who is working where, live, with collisions in red.",
    "decisions": "Rules agents proposed, waiting for a human to accept or reject.",
    "timeline": "What each teammate's agent learned, session by session.",
    "ask": "Questions answered from a teammate's agent context, with citations.",
    "impact": "Tokens saved, rules mined, violations caught, collisions warned.",
}


class DecisionService(Protocol):
    """The slice of the decision lifecycle (T-10) the API needs."""

    def propose(self, **fields: Any) -> Decision: ...

    def accept(self, decision_id: str, by: str) -> Decision: ...

    def reject(self, decision_id: str, by: str, reason: str) -> Decision: ...


BriefComposer = Callable[..., Brief]


class _CoreDecisions:
    """Binds the `hive.core.decisions` module functions to one store."""

    def __init__(self, module: ModuleType, store: ContextStore) -> None:
        self._module = module
        self._store = store

    def propose(self, **fields: Any) -> Decision:
        return self._module.propose(**fields, store=self._store)

    def accept(self, decision_id: str, by: str) -> Decision:
        return self._module.accept(decision_id, by, store=self._store)

    def reject(self, decision_id: str, by: str, reason: str) -> Decision:
        return self._module.reject(decision_id, by, reason, store=self._store)


def _optional_module(name: str) -> ModuleType | None:
    """Import `name`, or return None when that module itself does not exist yet."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name and (name == e.name or name.startswith(e.name + ".")):
            return None
        raise


class DecisionIn(BaseModel):
    """A new decision. It is always created as PROPOSED: status and decider cannot be set here."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    statement: str = Field(min_length=1)
    proposed_by: str = Field(min_length=1)
    rationale: str = ""
    scope: list[str] = Field(default_factory=lambda: ["**"])
    supersedes: str | None = None
    tags: list[str] = Field(default_factory=list)
    source: str | None = None


class _Actor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: str = Field(min_length=1, description="GitHub handle of the human making the call")

    @field_validator("by")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class AcceptIn(_Actor):
    pass


class RejectIn(_Actor):
    reason: str = ""


def _require_human(by: str) -> None:
    if _AGENT_ACTOR.match(by):
        raise HTTPException(403, "Agents may propose decisions, but only humans accept or reject them.")


def create_app(
    store: ContextStore | None = None,
    decisions: DecisionService | None = None,
    compose_brief: BriefComposer | None = None,
) -> FastAPI:
    """Build the API. Anything not passed in is resolved lazily on first use."""
    app = FastAPI(title="Hive API", version="0.1.0")
    resolved: dict[str, Any] = {"store": store, "decisions": decisions, "compose_brief": compose_brief}
    # Handlers run in a threadpool: build each collaborator once, even if the first requests race.
    lock = threading.RLock()

    def get_store() -> ContextStore:
        with lock:
            if resolved["store"] is None:
                from hive.store import get_store as build_store

                try:
                    resolved["store"] = build_store()
                except (RuntimeError, ValueError) as e:
                    raise HTTPException(503, f"Store unavailable: {e}") from e
            return resolved["store"]

    def get_decisions() -> DecisionService:
        with lock:
            if resolved["decisions"] is None:
                module = _optional_module("hive.core.decisions")
                if module is None:
                    raise HTTPException(503, "Decision lifecycle (hive.core.decisions) is not available yet.")
                resolved["decisions"] = _CoreDecisions(module, get_store())
            return resolved["decisions"]

    def get_brief_composer() -> BriefComposer:
        with lock:
            if resolved["compose_brief"] is None:
                module = _optional_module("hive.core.brief")
                if module is None:
                    raise HTTPException(503, "Brief composer (hive.core.brief) is not available yet.")
                resolved["compose_brief"] = module.compose_brief
            return resolved["compose_brief"]

    def lifecycle(call: Callable[[], Decision]) -> Decision:
        """Run a lifecycle call, turning its failures into HTTP errors."""
        try:
            return call()
        except NotFound as e:
            raise HTTPException(404, "Decision not found.") from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except ValidationError as e:
            # A ValueError subclass: keep it out of the 409 arm, and keep pydantic internals out of the reply.
            raise HTTPException(422, "The decision is not valid.") from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    # --- decisions ---------------------------------------------------------
    @app.get("/api/decisions")
    def list_decisions(status: DecisionStatus | None = None, path: str | None = None) -> list[Decision]:
        return get_store().list_decisions(status=status, path=path)

    @app.post("/api/decisions", status_code=201)
    def propose_decision(body: DecisionIn) -> Decision:
        service = get_decisions()
        return lifecycle(lambda: service.propose(**body.model_dump()))

    @app.post("/api/decisions/{decision_id}/accept")
    def accept_decision(decision_id: str, body: AcceptIn) -> Decision:
        _require_human(body.by)
        service = get_decisions()
        return lifecycle(lambda: service.accept(decision_id, body.by))

    @app.post("/api/decisions/{decision_id}/reject")
    def reject_decision(decision_id: str, body: RejectIn) -> Decision:
        _require_human(body.by)
        service = get_decisions()
        return lifecycle(lambda: service.reject(decision_id, body.by, body.reason))

    # --- handoffs, modules, recall, brief ----------------------------------
    @app.get("/api/handoffs")
    def list_handoffs(
        member: str | None = None,
        branch: str | None = None,
        since: datetime | None = None,
        limit: int = Query(20, ge=1, le=200),
    ) -> list[Handoff]:
        return get_store().list_handoffs(member=member, branch=branch, since=since, limit=limit)

    @app.get("/api/modules")
    def list_modules(prefix: str | None = None) -> list[ModuleNote]:
        return get_store().list_module_notes(prefix=prefix)

    @app.get("/api/recall")
    def recall(
        q: str = Query(min_length=1),
        kinds: Annotated[list[ItemKind] | None, Query()] = None,
        limit: int = Query(8, ge=1, le=100),
    ) -> list[RecallHit]:
        return get_store().recall(q, kinds=kinds, limit=limit)

    @app.get("/api/brief")
    def brief(
        member: str = Query(min_length=1),
        branch: str = Query(min_length=1),
        paths: Annotated[list[str] | None, Query(description="Changed paths; repeat the parameter")] = None,
        ticket: str | None = None,
        budget_tokens: int = Query(2000, ge=100, le=20000),
    ) -> Brief:
        composer = get_brief_composer()
        return composer(
            member, branch, paths or [], ticket=ticket, budget_tokens=budget_tokens, store=get_store()
        )

    # --- dashboard shell (T-27) ---------------------------------------------
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    def placeholder_page(key: str) -> Callable[[Request], HTMLResponse]:
        label = next(label for nav_key, label, _ in NAV if nav_key == key)

        def page(request: Request) -> HTMLResponse:
            context = {"active": key, "label": label, "blurb": PLACEHOLDERS[key]}
            return templates.TemplateResponse(request, "placeholder.html", context)

        return page

    for key, _, path in NAV:
        if key in PLACEHOLDERS:
            app.add_api_route(
                path, placeholder_page(key), response_class=HTMLResponse, include_in_schema=False
            )

    # The dashboard opens on the first nav item.
    home_key = NAV[0][0]
    if home_key in PLACEHOLDERS:
        app.add_api_route(
            "/", placeholder_page(home_key), response_class=HTMLResponse, include_in_schema=False
        )

    return app


app = create_app()
