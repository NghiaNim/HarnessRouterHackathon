"""`hive` CLI (T-12): how humans (and hooks) talk to the shared brain.

Humans record and decide rules here. Agents use the MCP server (T-13) and may only *propose*.

`accept`/`reject` guard the "humans decide" rule in two layers:
1. They require an interactive terminal (stdin and stdout are TTYs) and a typed confirmation of the id.
   Agent tool calls in any harness (Claude Code, Codex, Gemini CLI, ...) have no TTY.
2. Inside Claude Code (`CLAUDECODE` set) the actor is `agent:<member>`, which the lifecycle refuses.
This is a speed bump, NOT a security boundary: there is no auth (a non-goal), and a determined agent can
fake a pty. The dashboard is the primary place humans accept decisions.

`--hook` modes read Claude Code hook JSON on stdin and must never break a session: on any error they print
nothing and exit 0.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from hive.config import current_member, get_settings, git_context
from hive.core import decisions as decisions_core
from hive.core.decisions import AgentCannotDecide, DecisionError
from hive.models import Brief, Decision, DecisionStatus, Handoff
from hive.store.base import ContextStore, NotFound


class _Typer(typer.Typer):
    def __call__(self, *args, **kwargs):
        # Click glob-expands args on Windows by default, turning `--scope "hive/**"` into a file list.
        # Scope globs are the point of Hive, so pass them through verbatim.
        kwargs.setdefault("windows_expand_args", False)
        for stream in (sys.stdout, sys.stderr):  # briefs/hook JSON must be UTF-8 on Windows consoles too
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        return super().__call__(*args, **kwargs)


app = _Typer(no_args_is_help=True, add_completion=False, help="Hive: shared brain for coding agents.")

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


# --- plumbing -------------------------------------------------------------------------------------


def open_store(cwd: str | None = None, *, allow_fallback: bool = False) -> ContextStore:
    """Store from settings. A relative SQLite path is anchored at the repo root of `cwd`, not the cwd.

    If the shared memory store is configured but unavailable, commands fail loudly (so nobody silently
    works on a private local file). Hooks pass `allow_fallback=True` to degrade to local sqlite.
    TODO: delegate to `hive.store.get_store()` once it reads settings + anchors at the repo root.
    """
    from hive.store.sqlite import SqliteStore

    try:
        settings = get_settings()
        if settings.store == "memory":
            from hive.store import get_store

            try:
                return get_store("memory")
            except RuntimeError as e:  # HarnessMemoryStore not merged yet (T-04 part 2)
                if not allow_fallback:
                    fail(f"{e}. Set HIVE_STORE=sqlite in .env to use a local store.")
        path = Path(settings.sqlite_path)
        if not path.is_absolute():
            root = git_context(cwd=cwd).repo_root
            path = Path(root) / path if root else path
        return SqliteStore(path)
    except typer.Exit:
        raise
    except Exception as e:  # noqa: BLE001  config/store errors: a message, never a traceback
        fail(f"can't open the Hive store: {e}")
    raise AssertionError  # unreachable


def interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def confirm_human(verb: str, decision: Decision) -> None:
    """Positive human check: a real terminal, and the person re-types the id prefix."""
    if not interactive():
        fail(
            f"`hive {verb}` needs an interactive terminal (agents may only propose). "
            f"Run it in your own terminal, or use the dashboard.",
            code=3,
        )
    typer.echo(f"{verb} {decision.id[:8]}: {decision.title}\n  {decision.statement}")
    typed = typer.prompt(f"Type {decision.id[:8]} to confirm", default="", show_default=False)
    if typed.strip() != decision.id[:8]:
        fail("confirmation did not match; nothing changed", code=3)


def fail(message: str, code: int = 1) -> None:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def human_actor() -> str:
    member = current_member()
    if os.environ.get("CLAUDECODE"):
        return f"agent:{member}"  # running inside an agent's tool call
    return member


def resolve_id(store: ContextStore, ref: str) -> str:
    """Accept a full id or a unique prefix (briefs show the first 8 chars)."""
    try:
        return store.get_decision(ref).id
    except NotFound:
        pass
    matches = [d.id for d in store.list_decisions() if d.id.startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    fail(f"no decision matches {ref!r}" if not matches else f"{ref!r} is ambiguous ({len(matches)} matches)")
    raise AssertionError  # unreachable


def print_decision(d: Decision) -> None:
    by = f" by {d.decided_by}" if d.decided_by else ""
    typer.echo(f"{d.id[:8]}  [{d.status.value}{by}]  {d.title}")
    if d.statement != d.title:
        typer.echo(f"          {d.statement}")
    typer.echo(f"          scope: {', '.join(d.scope)} · proposed by {d.proposed_by}")


def dump(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


# --- decisions ------------------------------------------------------------------------------------


@app.command()
def decide(
    statement: str,
    title: Annotated[str | None, typer.Option(help="Short name (defaults to the statement).")] = None,
    scope: Annotated[list[str] | None, typer.Option(help="Path glob; repeatable. Default: **")] = None,
    rationale: Annotated[str, typer.Option(help="Why.")] = "",
    tag: Annotated[list[str] | None, typer.Option(help="Repeatable.")] = None,
    supersedes: Annotated[str | None, typer.Option(help="Id/prefix of the decision it replaces.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Propose a decision. A human still has to `hive accept` it before agents must follow it."""
    store = open_store()
    try:
        d = decisions_core.propose(
            statement=statement, title=title, scope=scope, rationale=rationale, tags=tag,
            supersedes=resolve_id(store, supersedes) if supersedes else None,
            proposed_by=human_actor(), store=store,
        )
    except DecisionError as e:
        fail(str(e))
    if as_json:
        return dump(d.model_dump(mode="json"))
    typer.echo(f"proposed {d.id[:8]}: {d.title}\n  accept with: hive accept {d.id[:8]}")


def _decide(verb: str, ref: str, reason: str = "") -> Decision:
    store = open_store()
    decision_id = resolve_id(store, ref)
    actor = human_actor()
    if actor.startswith("agent:"):
        fail(
            f"{actor} cannot {verb} decisions: agents may only propose. "
            "Ask your human to run it in their own terminal, or use the dashboard.",
            code=3,
        )
    confirm_human(verb, store.get_decision(decision_id))
    try:
        if verb == "accept":
            return decisions_core.accept(decision_id, actor, store=store)
        return decisions_core.reject(decision_id, actor, reason, store=store)
    except AgentCannotDecide as e:
        fail(f"{e}. Ask your human to run `hive {verb}` in their own terminal.", code=3)
    except DecisionError as e:
        fail(str(e))
    raise AssertionError  # unreachable


@app.command()
def accept(decision: Annotated[str, typer.Argument(help="Id or unique prefix.")]) -> None:
    """Accept a proposed decision (humans only). Every agent must follow it from now on."""
    d = _decide("accept", decision)
    typer.echo(f"accepted {d.id[:8]}: {d.title}")


@app.command()
def reject(
    decision: Annotated[str, typer.Argument(help="Id or unique prefix.")],
    reason: Annotated[str, typer.Option(help="Why it was rejected.")] = "",
) -> None:
    """Reject a proposed decision (humans only)."""
    d = _decide("reject", decision, reason)
    typer.echo(f"rejected {d.id[:8]}: {d.title}")


@app.command("decisions")
def list_decisions(
    status: Annotated[DecisionStatus | None, typer.Option(help="Filter by status.")] = None,
    path: Annotated[str | None, typer.Option(help="Only decisions whose scope covers this path.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """List decisions (newest first)."""
    found = open_store().list_decisions(status=status, path=path)
    if as_json:
        return dump([d.model_dump(mode="json") for d in found])
    if not found:
        typer.echo("no decisions")
    for d in found:
        print_decision(d)


# --- brief ----------------------------------------------------------------------------------------


def _fallback_brief(store: ContextStore, member: str, branch: str, paths: list[str]) -> Brief:
    """Used until the brief composer (T-11) is merged: binding decisions only."""
    accepted = decisions_core.in_scope(paths, store=store)
    lines = [f"# Hive brief: {member} @ {branch}", "", "## Team decisions (binding)"]
    lines += [f"- **{d.title}**: {d.statement} _(scope: {', '.join(d.scope)})_" for d in accepted]
    md = "\n".join(lines if accepted else lines[:1] + ["", "No team decisions yet."]) + "\n"
    return Brief(member=member, branch=branch, markdown=md, token_estimate=len(md) // 4,
                 decision_ids=[d.id for d in accepted])


def build_brief(
    cwd: str | None = None, ticket: str | None = None, budget: int = 2000, *, hook: bool = False
) -> Brief:
    store = open_store(cwd, allow_fallback=hook)
    ctx = git_context(cwd=cwd)
    member, branch = current_member(), ctx.branch or "unknown"
    try:
        from hive.core.brief import compose_brief
    except ImportError:
        return _fallback_brief(store, member, branch, ctx.changed_files)
    return compose_brief(member, branch, ctx.changed_files, ticket, budget, store=store)


@app.command()
def brief(
    hook: Annotated[bool, typer.Option("--hook", help="Claude Code SessionStart hook mode.")] = False,
    ticket: Annotated[str | None, typer.Option(help="e.g. T-12")] = None,
    budget: Annotated[int, typer.Option(help="Token budget.")] = 2000,
) -> None:
    """Print the brief: binding decisions, teammates' handoffs, codebase notes for what you touch."""
    if not hook:
        try:
            md = build_brief(ticket=ticket, budget=budget).markdown
        except typer.Exit:
            raise
        except Exception as e:  # noqa: BLE001
            fail(f"couldn't build the brief: {e}")
        typer.echo(md, nl=False)
        return
    try:  # fail open: never break or block a teammate's session
        payload = json.loads(sys.stdin.read() or "{}")
        b = build_brief(cwd=payload.get("cwd"), ticket=ticket, budget=budget, hook=True)
        out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": b.markdown}}
        typer.echo(json.dumps(out))
    except Exception:  # noqa: BLE001
        raise typer.Exit(0) from None


# --- handoff --------------------------------------------------------------------------------------


@app.command()
def handoff(
    summary: Annotated[str | None, typer.Option(help="Write a handoff by hand.")] = None,
    learned: Annotated[list[str] | None, typer.Option(help="Repeatable.")] = None,
    gotcha: Annotated[list[str] | None, typer.Option(help="Repeatable.")] = None,
    ticket: Annotated[str | None, typer.Option()] = None,
    hook: Annotated[bool, typer.Option("--hook", help="Claude Code SessionEnd hook mode.")] = False,
) -> None:
    """Share what this session learned so teammates' agents don't rediscover it."""
    if hook:
        try:  # distillation lands with T-15; until then the hook is a safe no-op
            payload = json.loads(sys.stdin.read() or "{}")
            from hive.agents.distiller import distill_in_background  # type: ignore[attr-defined]

            distill_in_background(payload)
        except Exception:  # noqa: BLE001
            pass
        raise typer.Exit(0)
    if not summary:
        fail("pass --summary (or --hook from a Claude Code SessionEnd hook)")
    ctx = git_context()
    h = open_store().put_handoff(
        Handoff(
            member=current_member(), branch=ctx.branch or "unknown", ticket=ticket, summary=summary,
            learned=learned or [], gotchas=gotcha or [], files_touched=ctx.changed_files,
            commit_sha=ctx.head_sha,
        )
    )
    typer.echo(f"handoff {h.id[:8]} saved ({len(h.files_touched)} files touched)")


# --- recall ---------------------------------------------------------------------------------------


@app.command()
def recall(
    query: str,
    kind: Annotated[list[str] | None, typer.Option(help="decision | handoff | module; repeatable.")] = None,
    limit: Annotated[int, typer.Option()] = 8,
    as_json: JsonOpt = False,
) -> None:
    """Search decisions, handoffs and codebase notes before re-reading the code."""
    bad = set(kind or []) - {"decision", "handoff", "module"}
    if bad:
        fail(f"unknown kind(s): {', '.join(sorted(bad))}")
    hits = open_store().recall(query, kinds=kind or None, limit=limit)
    if as_json:
        return dump([h.model_dump(mode="json") for h in hits])
    if not hits:
        typer.echo("no matches")
    for h in hits:
        item = h.item
        label = getattr(item, "title", None) or getattr(item, "summary", None) or getattr(item, "path", "")
        typer.echo(f"{h.score:.2f}  {h.kind:<8}  {label}")


# --- commands whose engines land in other tickets -------------------------------------------------


def _not_yet(command: str, ticket: str) -> None:
    fail(f"`hive {command}` needs {ticket}, which hasn't merged yet", code=2)


@app.command()
def init() -> None:
    """Create the HarnessRouter memory tree and harnesses (needs T-03)."""
    _not_yet("init", "T-03 (#14)")  # wire HRClient.from_env() + ensure_harnesses(client) once #14 lands


@app.command("map")
def map_codebase() -> None:
    """Refresh the codebase map incrementally (needs T-21)."""
    _not_yet("map", "T-21")


@app.command()
def check() -> None:
    """Check your diff against accepted decisions (needs T-20)."""
    _not_yet("check", "T-20")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
