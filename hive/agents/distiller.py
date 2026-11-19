"""Distiller: Claude Code session transcript -> Handoff + proposed decisions (T-15).

Pipeline (everything outbound is redacted first, T-28):
  1. redact the raw JSONL, then trim it: keep user/assistant text and a one-line summary of each tool call,
     drop tool outputs longer than `max_tool_output`, and collect the edited files;
  2. run `hive-distiller` on the trimmed transcript plus `git diff --stat` and validate the reply as
     `DistilledSession`;
  3. **correction mining guard**: keep a proposed decision only if its `evidence` is verbatim (modulo case and
     whitespace) part of a *human* turn. Agent opinions, hallucinated or injected quotes are dropped.
     A proposal without its own scope gets one inferred from the files edited around that quote;
  4. save the Handoff and propose each surviving decision as `agent:<member>` (humans still accept);
  5. upload the trimmed transcript and link it from the Handoff (for `hive ask`, T-53).

`distill_in_background(hook_payload)` is what the SessionEnd hook calls: it detaches a subprocess and returns
at once, so ending a session never waits on HarnessRouter.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hive.core.redact import Redactor
from hive.hr.client import HRError
from hive.hr.harnesses import HARNESS_MAP_PATH, DistilledSession, ProposedDecision, run_json
from hive.models import Decision, Handoff
from hive.store.base import ContextStore

log = logging.getLogger("hive.distiller")

HARNESS = "hive-distiller"
MAX_COST_USD = 1.0
DEFAULT_MAX_TOOL_OUTPUT = 500
MIN_EVIDENCE_CHARS = 8
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
LOG_PATH = Path.home() / ".cache" / "hive" / "distiller.log"


# --- trimming ------------------------------------------------------------------------------------


@dataclass
class TrimmedSession:
    text: str
    user_turns: list[str]
    edited_files: list[str]
    session_id: str | None = None
    branch: str | None = None
    cwd: str | None = None
    # ("human", text) / ("edit", path) in order, for inferring a quote's scope
    timeline: list[tuple[str, str]] = field(default_factory=list)


def _relative(path: str, cwd: str | None) -> str:
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1 :]
    return path


def _tool_summary(block: dict, cwd: str | None) -> str:
    inp = block.get("input") or {}
    target = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
    if target:
        detail = _relative(str(target), cwd)
    elif inp.get("command"):
        detail = str(inp["command"]).splitlines()[0][:120]
    else:
        detail = str(inp.get("pattern") or inp.get("query") or inp.get("url") or "")[:120]
    return f"TOOL: {block.get('name', '?')} {detail}".rstrip()


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


def trim_transcript(jsonl: str, max_tool_output: int = DEFAULT_MAX_TOOL_OUTPUT) -> TrimmedSession:
    lines: list[str] = []
    turns: list[str] = []
    edited: list[str] = []
    timeline: list[tuple[str, str]] = []
    session_id = branch = cwd = None
    for raw in jsonl.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if (
            not isinstance(event, dict)
            or event.get("type") not in ("user", "assistant")
            or event.get("isMeta")
        ):
            continue
        session_id = session_id or event.get("sessionId")
        branch = branch or event.get("gitBranch")
        cwd = cwd or event.get("cwd")
        content = (event.get("message") or {}).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and str(block.get("text", "")).strip():
                text = str(block["text"]).strip()
                if event["type"] == "user":
                    if text.startswith("<"):  # system reminders / command wrappers, not human words
                        continue
                    turns.append(text)
                    timeline.append(("human", text))
                    lines.append(f"USER: {text}")
                else:
                    lines.append(f"ASSISTANT: {text}")
            elif kind == "tool_use":
                lines.append(_tool_summary(block, cwd))
                path = (block.get("input") or {}).get("file_path") or (block.get("input") or {}).get(
                    "notebook_path"
                )
                if block.get("name") in EDIT_TOOLS and path:
                    rel = _relative(str(path), cwd)
                    timeline.append(("edit", rel))
                    if rel not in edited:
                        edited.append(rel)
            elif kind == "tool_result":
                out = _result_text(block.get("content"))
                if len(out) > max_tool_output:
                    lines.append(f"TOOL_RESULT: [{len(out)} chars omitted]")
                elif out.strip():
                    lines.append(f"TOOL_RESULT: {out.strip()}")
    return TrimmedSession("\n".join(lines) + "\n", turns, edited, session_id, branch, cwd, timeline)


# --- correction-mining guard -------------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip(" \"'“”‘’.,;:")


def evidence_in_turns(evidence: str | None, turns: list[str]) -> bool:
    """True if `evidence` is (case/whitespace-insensitively) a substring of one human turn."""
    if not evidence:
        return False
    needle = _norm(evidence)
    return len(needle) >= MIN_EVIDENCE_CHARS and any(needle in _norm(t) for t in turns)


def _scope_from_files(files: list[str]) -> list[str]:
    scopes: list[str] = []
    for f in files:
        parent = os.path.dirname(f)
        glob = f"{parent}/**" if parent else f
        if glob not in scopes:
            scopes.append(glob)
    return scopes or ["**"]


def infer_scope(evidence: str, session: TrimmedSession) -> list[str]:
    """Files edited between the human turn before the quote and the one after it."""
    needle = _norm(evidence)
    humans = [i for i, (kind, _) in enumerate(session.timeline) if kind == "human"]
    for n, i in enumerate(humans):
        if needle in _norm(session.timeline[i][1]):
            start = humans[n - 1] if n > 0 else -1
            end = humans[n + 1] if n + 1 < len(humans) else len(session.timeline)
            files = [p for kind, p in session.timeline[start + 1 : end] if kind == "edit"]
            return _scope_from_files(list(dict.fromkeys(files)) or session.edited_files)
    return _scope_from_files(session.edited_files)


# --- pipeline ------------------------------------------------------------------------------------------


@dataclass
class DistillResult:
    handoff: Handoff
    proposed: list[Decision]
    dropped: list[ProposedDecision]
    transcript_file_id: str | None


def build_input(session: TrimmedSession, diff_stat: str, member: str, branch: str) -> str:
    return (
        f"Member: {member}\nBranch: {branch}\n\n"
        f"## git diff --stat\n{diff_stat.strip() or '(no changes)'}\n\n"
        f"## Transcript (trimmed)\n{session.text}"
    )


def _propose(
    p: ProposedDecision, scope: list[str], member: str, source: str | None, store: ContextStore
) -> Decision:
    fields = dict(
        title=p.title,
        statement=p.statement,
        rationale=p.rationale,
        scope=scope,
        proposed_by=f"agent:{member}",
    )
    try:
        from hive.core import decisions as lifecycle  # T-10
    except ImportError:
        lifecycle = None
    if lifecycle is not None and hasattr(lifecycle, "propose"):
        decision = lifecycle.propose(**fields, store=store)
    else:
        decision = Decision(**fields)
    extra: dict[str, Any] = {"source": source}
    if "evidence" in Decision.model_fields:  # T-08 contract field
        extra["evidence"] = p.evidence
    else:
        extra["rationale"] = "\n".join(x for x in (decision.rationale, f'Evidence: "{p.evidence}"') if x)
    return store.put_decision(decision.model_copy(update=extra))


def distill(
    transcript_path: str | Path,
    *,
    member: str,
    branch: str,
    client: Any,
    store: ContextStore,
    cwd: str | None = None,
    diff_stat: str | None = None,
    commit_sha: str | None = None,
    ticket: str | None = None,
    upload: bool = True,
    env_values: dict[str, str] | None = None,
    max_tool_output: int = DEFAULT_MAX_TOOL_OUTPUT,
    map_path: Path = HARNESS_MAP_PATH,
) -> DistillResult:
    redactor = Redactor(env_values)
    raw = Path(transcript_path).read_text(encoding="utf-8", errors="replace")
    session = trim_transcript(redactor.redact_transcript(raw), max_tool_output)
    if diff_stat is None:
        diff_stat = git_diff_stat(cwd or session.cwd)
    harness_input = redactor.redact(build_input(session, diff_stat, member, branch))

    ds: DistilledSession = run_json(
        client,
        HARNESS,
        harness_input,
        DistilledSession,
        map_path=map_path,
        max_cost_usd=MAX_COST_USD,
        metadata={"hive": "distiller", "member": member, "branch": branch},
    )

    file_id = None
    if upload:
        try:
            name = f"hive-transcript-{session.session_id or 'session'}.txt"
            file_id = client.upload_file(redactor.redact(session.text).encode(), filename=name)["id"]
        except (HRError, KeyError, TypeError) as e:  # the handoff matters more than the attachment
            log.warning("transcript upload failed: %s", e)

    handoff_fields: dict[str, Any] = dict(
        member=member,
        branch=branch,
        ticket=ds.ticket or ticket,
        summary=ds.summary,
        learned=ds.learned,
        gotchas=ds.gotchas,
        files_touched=ds.files_touched or session.edited_files,
        open_questions=ds.open_questions,
        next_steps=ds.next_steps,
        commit_sha=commit_sha,
    )
    if "transcript_file_id" in Handoff.model_fields:  # T-08 contract field
        handoff_fields["transcript_file_id"] = file_id
    handoff = store.put_handoff(Handoff(**handoff_fields))

    source = f"session:{session.session_id}" if session.session_id else None
    proposed, dropped = [], []
    for p in ds.proposed_decisions:
        if not evidence_in_turns(p.evidence, session.user_turns):
            dropped.append(p)
            continue
        scope = p.scope if p.scope and p.scope != ["**"] else infer_scope(p.evidence or "", session)
        proposed.append(_propose(p, scope, member, source, store))
    if dropped:
        log.info("dropped %d proposal(s) without verbatim human evidence", len(dropped))
    return DistillResult(handoff, proposed, dropped, file_id)


# --- git + hook plumbing ---------------------------------------------------------------------------------


def _git(*args: str, cwd: str | None) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def git_diff_stat(cwd: str | None, base: str = "origin/main") -> str:
    merge_base = _git("merge-base", "HEAD", base, cwd=cwd)
    committed = _git("diff", "--stat", merge_base, cwd=cwd) if merge_base else ""
    return committed or _git("diff", "--stat", "HEAD", cwd=cwd)


def _member() -> str:
    try:
        from hive.config import current_member  # T-05

        return current_member()
    except ImportError:
        return os.environ.get("HIVE_MEMBER") or _git("config", "user.name", cwd=None) or "unknown"


def distill_in_background(payload: dict) -> None:
    """SessionEnd hook entry: detach a worker process and return immediately."""
    transcript = payload.get("transcript_path")
    if not transcript or not Path(transcript).exists():
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "hive.agents.distiller", transcript]
    if payload.get("cwd"):
        cmd += ["--cwd", str(payload["cwd"])]
    with LOG_PATH.open("a") as logf:
        subprocess.Popen(
            cmd,
            cwd=payload.get("cwd") or None,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
        )


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - exercised live
    import argparse

    from hive.hr.client import HRClient
    from hive.store import get_store

    ap = argparse.ArgumentParser(prog="python -m hive.agents.distiller")
    ap.add_argument("transcript")
    ap.add_argument("--cwd")
    ap.add_argument("--ticket")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s distiller %(levelname)s %(message)s")
    cwd = a.cwd or os.getcwd()
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd) or "unknown"
    with HRClient.from_env() as client:
        result = distill(
            a.transcript,
            member=_member(),
            branch=branch,
            client=client,
            store=get_store(),
            cwd=cwd,
            commit_sha=_git("rev-parse", "HEAD", cwd=cwd) or None,
            ticket=a.ticket,
        )
    log.info(
        "handoff %s, %d proposed, %d dropped",
        result.handoff.id[:8],
        len(result.proposed),
        len(result.dropped),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
