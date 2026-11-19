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
