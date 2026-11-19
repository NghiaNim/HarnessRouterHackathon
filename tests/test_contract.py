from hive.models import Decision, DecisionStatus, Handoff
from hive.store.base import ContextStore


def test_decision_defaults_and_scope():
    d = Decision(title="HTTP client", statement="Use httpx", proposed_by="agent:alice", scope=["hive/**"])
    assert d.status is DecisionStatus.PROPOSED
    assert d.applies_to("hive/hr/client.py")
    assert not d.applies_to("tests/test_x.py")


def test_models_roundtrip_json():
    h = Handoff(member="bob", branch="bob/feat/x", summary="did x", learned=["a"])
    assert Handoff.model_validate_json(h.model_dump_json()) == h


def test_store_protocol_is_importable():
    assert hasattr(ContextStore, "recall")


# --- T-08 additions ----------------------------------------------------------

from datetime import UTC, datetime, timedelta  # noqa: E402

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from hive.models import BriefLog, HiveEvent, Intent  # noqa: E402


def test_new_optional_fields_default_to_none():
    d = Decision(title="t", statement="s", proposed_by="alice")
    h = Handoff(member="bob", branch="b", summary="s")
    assert d.evidence is None
    assert h.transcript_file_id is None
    d2 = d.model_copy(update={"evidence": "no, use httpx"})
    assert Decision.model_validate_json(d2.model_dump_json()).evidence == "no, use httpx"


def test_hive_event_types_are_closed():
    e = HiveEvent(type="decision_proposed", actor="agent:alice", ref_id="abc", summary="Use httpx")
    assert e.id and e.at.tzinfo is not None
    assert HiveEvent.model_validate_json(e.model_dump_json()) == e
    with pytest.raises(ValidationError):
        HiveEvent(type="something_else", actor="alice")


def test_brief_log_roundtrip():
    b = BriefLog(member="alice", branch="alice/feat/x", tokens=900, baseline_tokens=12000)
    assert BriefLog.model_validate_json(b.model_dump_json()) == b


def test_intent_is_active():
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    i = Intent(member="alice", branch="alice/feat/x", heartbeat_at=now - timedelta(minutes=10))
    assert i.paths == [] and i.ended_at is None and i.ticket is None
    assert i.is_active(15, now=now)
    assert not i.is_active(5, now=now)
    assert not i.model_copy(update={"ended_at": now}).is_active(15, now=now)
    naive = i.model_copy(update={"heartbeat_at": (now - timedelta(minutes=1)).replace(tzinfo=None)})
    assert naive.is_active(15, now=now)  # naive datetimes are treated as UTC


# --- T-48: gitignore semantics for scope globs ------------------------------


@pytest.mark.parametrize(
    ("scope", "path", "expected"),
    [
        ("hive/*.py", "hive/a.py", True),
        ("hive/*.py", "hive/sub/deep.py", False),  # fnmatch said True
        ("**/test_*.py", "test_x.py", True),  # fnmatch said False
        ("**/test_*.py", "tests/a/test_y.py", True),
        ("hive/api", "hive/api/app.py", True),  # fnmatch matched nothing
        ("hive/api/", "hive/api/app.py", True),
        ("hive/api", "hive/apis/x.py", False),
        ("hive/**", "hive/hr/client.py", True),
        ("hive/**/*.py", "hive/x.py", True),
        ("tests/**", "hive/tests/x.py", False),
        ("*.md", "docs/deep/README.md", True),
        ("**", "anything/at/all.txt", True),
        ("hive/api/**", "./hive/api/app.py", True),  # normalized
        ("hive/api/**", "hive\\api\\app.py", True),  # Windows separators
        ("hive\\web\\**", "hive/web/x.js", True),  # Windows-typed scope
    ],
)
def test_scope_gitignore_semantics(scope, path, expected):
    d = Decision(title="t", statement="s", proposed_by="alice", scope=[scope])
    assert d.applies_to(path) is expected


def test_scope_negation_and_multiple_patterns():
    d = Decision(title="t", statement="s", proposed_by="alice", scope=["hive/**", "!hive/web/**"])
    assert d.applies_to("hive/hr/client.py") and not d.applies_to("hive/web/app.js")
