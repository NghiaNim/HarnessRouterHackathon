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
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
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
    ("home", "Home", "/"),
    ("decisions", "Team rules", "/decisions"),
    ("timeline", "Session notes", "/timeline"),
    ("radar", "Live radar", "/radar"),
    ("ask", "Ask", "/ask"),
    ("impact", "Impact", "/impact"),
]
templates.env.globals["nav"] = NAV

# Sections that are still a "coming soon" page: key -> what will be there. The ticket that builds a section
# removes its entry here and adds its own route and template (which extends base.html).
PLACEHOLDERS = {
    "radar": "See which teammate's agent is editing which files right now, and get warned before two agents "
    "change the same file. (T-55)",
    "timeline": "Everything your teammates' agents learned, session by session: what they built, the gotchas "
    "they hit, and what's left to do. (T-18)",
    "ask": "Ask a teammate's agent a question (\"why did you change the retry logic?\") and get an "
    "answer from "
    "its actual session, without interrupting your teammate. (T-53)",
    "impact": "What Hive saved the team: tokens not spent re-reading code, rules learned, mistakes caught. "
    "(T-24)",
}
# Unbuilt sections are shown dimmed with a "soon" pill in the nav.
templates.env.globals["soon"] = set(PLACEHOLDERS)


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


async def _form(request: Request) -> dict[str, str]:
    """Read a urlencoded form body (what HTMX posts) without needing python-multipart."""
    raw = (await request.body()).decode("utf-8", "replace")
    return {k: v[-1].strip() for k, v in parse_qs(raw, keep_blank_values=True).items()}


def _board(store: ContextStore) -> dict[str, Any]:
    """Template context for the decisions board (T-17)."""
    decisions = store.list_decisions()
    by_status = {status: [d for d in decisions if d.status is status] for status in DecisionStatus}
    return {
        "columns": [
            ("Waiting for a human", "proposed", by_status[DecisionStatus.PROPOSED]),
            ("Active rules", "accepted", by_status[DecisionStatus.ACCEPTED]),
            ("Replaced", "superseded", by_status[DecisionStatus.SUPERSEDED]),
        ],
        "rejected": by_status[DecisionStatus.REJECTED],
        "titles": {d.id: d.title for d in decisions},
        "replaced_by": {d.supersedes: d for d in by_status[DecisionStatus.ACCEPTED] if d.supersedes},
    }


def _home(store: ContextStore) -> dict[str, Any]:
    """Template context for the home page (T-69): live counts for each step of the loop."""
    decisions = store.list_decisions()
    notes = store.list_handoffs(limit=50)
    waiting = [d for d in decisions if d.status is DecisionStatus.PROPOSED]
    active = [d for d in decisions if d.status is DecisionStatus.ACCEPTED]
    people = sorted({h.member for h in notes})  # who wrote the notes
    # A sensible default for the briefing preview: a file an active rule actually covers.
    sample_path = "hive/api/app.py"
    for d in active:
        glob = next((g for g in d.scope if g not in ("**", "*")), "")
        if glob:
            sample_path = glob.replace("**", "").replace("*", "x").rstrip("/") or sample_path
            if "." not in sample_path.rsplit("/", 1)[-1]:
                sample_path = f"{sample_path}/app.py"
            break
    return {
        "waiting": waiting,
        "active_rules": active,
        "notes": notes[:5],
        "note_count": len(notes),
        "people": people,
        "empty": not decisions and not notes,
        "sample_path": sample_path,
    }


def _seed_example(store: ContextStore) -> None:
    """A tiny, realistic story for an empty dashboard. Everything is tagged as example data."""
    from hive.models import Handoff as H

    tag = ["example"]
    store.put_decision(
        Decision(
            title="HTTP client is httpx",
            statement="All HTTP calls use httpx with a timeout. Never add `requests`.",
            rationale="One client means consistent timeouts and retries everywhere.",
            evidence="no, don't add requests, we use httpx everywhere",
            scope=["hive/**"],
            status=DecisionStatus.ACCEPTED,
            proposed_by="agent:bob",
            decided_by="alice",
            tags=tag,
            source="example data",
        )
    )
    store.put_decision(
        Decision(
            title="The HarnessRouter key never reaches the browser",
            statement="Only the server calls HarnessRouter. "
            "Pages talk to our own API, never to HarnessRouter directly.",
            evidence="never call HarnessRouter from the browser, it leaks the key",
            scope=["hive/web/**", "hive/api/**"],
            status=DecisionStatus.ACCEPTED,
            proposed_by="agent:carol",
            decided_by="bob",
            tags=tag,
            source="example data",
        )
    )
    store.put_decision(
        Decision(
            title="Tests never hit the network",
            statement="Tests use the fakes in tests/fakes.py; nothing in tests/ calls a real API.",
            evidence="tests must not hit the real API, use the fakes",
            scope=["tests/**"],
            status=DecisionStatus.PROPOSED,
            proposed_by="agent:carol",
            tags=tag,
            source="example data",
        )
    )
    store.put_handoff(
        H(
            member="bob",
            branch="bob/feat/api-server",
            ticket="example",
            summary="Built the REST API for team rules.",
            learned=["Rules are stored through the ContextStore protocol, never a concrete store."],
            gotchas=["Accepting a rule must go through DecisionService, or the endpoint returns 500."],
            files_touched=["hive/api/app.py"],
        )
    )
    store.put_handoff(
        H(
            member="carol",
            branch="carol/feat/redact",
            ticket="example",
            summary="Added secret redaction before transcripts leave the machine.",
            learned=["Claude Code transcripts keep a second copy of every file read, under toolUseResult."],
            files_touched=["hive/core/redact.py"],
        )
    )


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

    # --- dashboard: home / "how Hive works" (T-69) ----------------------------
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home_page(request: Request) -> HTMLResponse:
        context = {**_home(get_store()), "active": "home"}
        return templates.TemplateResponse(request, "home.html", context)

    @app.get("/ui/briefing", response_class=HTMLResponse, include_in_schema=False)
    def ui_briefing(request: Request, member: str = "", path: str = "") -> HTMLResponse:
        """The exact text Hive adds to an agent's context at session start, for `member` touching `path`."""
        member = member.strip() or "new-teammate"
        paths = [p.strip() for p in path.split(",") if p.strip()]
        context: dict[str, Any] = {"member": member, "paths": paths, "brief": None, "error": None}
        context["binding"] = 0
        try:
            composer = get_brief_composer()
            store = get_store()
            context["brief"] = brief = composer(member, f"{member}/work", paths, store=store)
            accepted = {d.id for d in store.list_decisions(status=DecisionStatus.ACCEPTED)}
            context["binding"] = sum(1 for i in brief.decision_ids if i in accepted)  # not pending ones
        except HTTPException as e:
            context["error"] = str(e.detail)
        return templates.TemplateResponse(request, "_briefing.html", context)

    @app.post("/ui/example-data", response_class=HTMLResponse, include_in_schema=False)
    def ui_example_data(request: Request) -> HTMLResponse:
        """Seed a small example story, only into an empty store, so a fresh dashboard explains itself."""
        store = get_store()
        if not (store.list_decisions() or store.list_handoffs(limit=1)):
            _seed_example(store)
        return HTMLResponse("", headers={"HX-Redirect": "/"})

    # --- dashboard: decisions board (T-17) ----------------------------------
    def render_board(request: Request, error: str | None = None, status_code: int = 200) -> HTMLResponse:
        context = {**_board(get_store()), "error": error}
        return templates.TemplateResponse(request, "_board.html", context, status_code=status_code)

    async def board_action(request: Request, action: Callable[[], Any]) -> HTMLResponse:
        """Run a board action off the event loop, then re-render the board (with the error, if it failed)."""
        try:
            await run_in_threadpool(action)
        except HTTPException as e:
            return await run_in_threadpool(render_board, request, str(e.detail), e.status_code)
        return await run_in_threadpool(render_board, request)

    @app.get("/decisions", response_class=HTMLResponse, include_in_schema=False)
    def decisions_page(request: Request) -> HTMLResponse:
        context = {**_board(get_store()), "error": None, "active": "decisions"}
        return templates.TemplateResponse(request, "decisions.html", context)

    @app.get("/ui/decisions/board", response_class=HTMLResponse, include_in_schema=False)
    def decisions_board(request: Request) -> HTMLResponse:
        return render_board(request)

    @app.post("/ui/decisions", response_class=HTMLResponse, include_in_schema=False)
    async def ui_propose(request: Request) -> HTMLResponse:
        form = await _form(request)

        def action() -> None:
            by = form.get("by", "")
            if not by:
                raise HTTPException(422, "Type your GitHub handle in “Your name” (top right) first.")
            if not form.get("title") or not form.get("statement"):
                raise HTTPException(422, "A rule needs a title and the rule itself.")
            _require_human(by)  # this form is the human path; agents propose through the API or MCP
            scope = [s.strip() for s in form.get("scope", "").split(",") if s.strip()] or ["**"]
            service = get_decisions()
            lifecycle(
                lambda: service.propose(
                    title=form["title"][:300],
                    statement=form["statement"],
                    rationale=form.get("rationale", ""),
                    scope=scope,
                    proposed_by=by,
                    source="dashboard",
                )
            )

        return await board_action(request, action)

    @app.post("/ui/decisions/{decision_id}/{verb}", response_class=HTMLResponse, include_in_schema=False)
    async def ui_decide(request: Request, decision_id: str, verb: str) -> HTMLResponse:
        if verb not in ("accept", "reject"):
            raise HTTPException(404)
        form = await _form(request)

        def action() -> None:
            by = form.get("by", "")
            if not by:
                raise HTTPException(
                    422, "Type your GitHub handle in “Your name” (top right) before approving or rejecting."
                )
            _require_human(by)
            service = get_decisions()
            if verb == "accept":
                lifecycle(lambda: service.accept(decision_id, by))
            else:
                reason = request.headers.get("HX-Prompt", "") or form.get("reason", "")
                lifecycle(lambda: service.reject(decision_id, by, reason))

        return await board_action(request, action)

    return app


app = create_app()
