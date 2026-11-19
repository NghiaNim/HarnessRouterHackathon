"""Shared fixtures: no network, no key. See tests/fakes.py and tests/fixtures/README.md."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hive.models import Decision, Handoff, ModuleNote
from hive.store.base import ContextStore
from hive.store.sqlite import SqliteStore
from tests.fakes import FakeHRClient

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str, model):
    return [model.model_validate(x) for x in json.loads((FIXTURES / name).read_text())]


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def store() -> ContextStore:
    """Empty in-memory store. Test modules may override this fixture (e.g. the store contract suite)."""
    s = SqliteStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def seed_decisions() -> list[Decision]:
    """3 accepted (httpx / browser-never-calls-HR / HTMX), 1 proposed by an agent, 1 rejected."""
    return _load("seed_decisions.json", Decision)


@pytest.fixture
def seed_handoffs() -> list[Handoff]:
    return _load("seed_handoffs.json", Handoff)


@pytest.fixture
def seed_module_notes() -> list[ModuleNote]:
    return _load("seed_module_notes.json", ModuleNote)


@pytest.fixture
def seeded_store(store, seed_decisions, seed_handoffs, seed_module_notes) -> ContextStore:
    for d in seed_decisions:
        store.put_decision(d)
    for h in seed_handoffs:
        store.put_handoff(h)
    for n in seed_module_notes:
        store.put_module_note(n)
    return store


@pytest.fixture
def fake_hr() -> FakeHRClient:
    return FakeHRClient()


@pytest.fixture
def transcript_path() -> Path:
    """Claude Code JSONL transcript: 2 human corrections (httpx; timeout + no store_write_failed retry),
    1 agent opinion (cache /v1/bases) that must NOT become a proposal, edits to hive/hr/client.py + tests."""
    return FIXTURES / "transcript.jsonl"


@pytest.fixture
def sample_diff() -> str:
    """Unified diff: complies with the httpx decision, violates 'browser never sees the key' in app.js."""
    return (FIXTURES / "sample.diff").read_text()
