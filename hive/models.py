"""Shared data contract for Hive.

CONTRACT FILE: every track builds against these models. Additive changes (new optional fields) are OK;
renaming/removing fields needs a contract PR reviewed by both other members. See docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from fnmatch import fnmatch
from typing import Literal, Union

from pydantic import BaseModel, Field


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DecisionStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class Decision(BaseModel):
    """A binding team rule. Agents may only PROPOSE; only humans accept/reject."""

    id: str = Field(default_factory=new_id)
    title: str = Field(max_length=300, description="Short name, e.g. 'HTTP client is httpx'")
    statement: str = Field(description="The rule, imperative: 'Use httpx with a timeout; never requests.'")
    rationale: str = ""
    scope: list[str] = Field(default_factory=lambda: ["**"], description="Path globs the rule applies to")
    status: DecisionStatus = DecisionStatus.PROPOSED
    proposed_by: str = Field(description="Member handle, or 'agent:<handle>' when an agent proposed it")
    decided_by: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None
    supersedes: str | None = Field(default=None, description="Decision.id this replaces")
    tags: list[str] = Field(default_factory=list)
    source: str | None = Field(default=None, description="Where it came from: PR url, Slack link, session id")

    def applies_to(self, path: str) -> bool:
        return any(fnmatch(path, pattern) for pattern in self.scope)


class Handoff(BaseModel):
    """What one agent session learned, for teammates' agents to pick up."""

    id: str = Field(default_factory=new_id)
    member: str
    branch: str
    ticket: str | None = Field(default=None, description="e.g. 'T-07'")
    summary: str
    learned: list[str] = Field(default_factory=list, description="Facts discovered about the codebase")
    gotchas: list[str] = Field(default_factory=list)
    files_touched: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    commit_sha: str | None = None
    hr_session_id: str | None = Field(default=None, description="HarnessRouter session that produced it")
    created_at: datetime = Field(default_factory=utcnow)


class ModuleNote(BaseModel):
    """Cached understanding of one path in the codebase, so agents don't re-read it."""

    id: str = Field(default_factory=new_id)
    path: str = Field(description="File or directory path relative to repo root (upsert key)")
    summary: str
    key_symbols: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    commit_sha: str
    updated_at: datetime = Field(default_factory=utcnow)


ContextItem = Union[Decision, Handoff, ModuleNote]
ItemKind = Literal["decision", "handoff", "module"]


class RecallHit(BaseModel):
    kind: ItemKind
    item: ContextItem
    score: float = 0.0
    why: str | None = None


class Brief(BaseModel):
    """Token-budgeted context injected at agent session start."""

    member: str
    branch: str
    markdown: str
    token_estimate: int
    decision_ids: list[str] = Field(default_factory=list)
    handoff_ids: list[str] = Field(default_factory=list)
    module_paths: list[str] = Field(default_factory=list)


class Violation(BaseModel):
    decision_id: str
    file: str
    line: int | None = None
    explanation: str
    severity: Literal["block", "warn"] = "warn"


class ArbiterVerdict(BaseModel):
    duplicate_of: str | None = None
    contradicts: list[str] = Field(default_factory=list)
    note: str = ""
