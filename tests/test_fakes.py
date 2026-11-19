import json

import pytest

from hive.hr.client import HRClient, HRError
from hive.models import DecisionStatus
from tests.fakes import FakeHRClient


def test_fake_mirrors_real_client_public_api():
    public = {n for n in dir(HRClient) if not n.startswith("_") and callable(getattr(HRClient, n))}
    missing = public - {n for n in dir(FakeHRClient) if callable(getattr(FakeHRClient, n))}
    assert not missing, f"FakeHRClient is missing {sorted(missing)}"


def test_run_canned_by_name_or_id_and_sequences():
    fake = FakeHRClient(outputs={"hive-guard": ({"bad": 1}, "[]"), "chrn_x": lambda inp: inp.upper()})
    hid = fake.create_harness({"name": "hive-guard", "base": "claude-code"})["id"]
    assert json.loads(fake.run(hid, "diff").text) == {"bad": 1}
    assert fake.run("hive-guard", "diff").text == "[]"
    assert fake.run("hive-guard", "diff").text == "[]"  # last one repeats
    assert fake.run("chrn_x", "hi").text == "HI"
    assert len(fake.calls_to("run")) == 4


def test_run_raises_canned_errors_and_unknown_harness():
    fake = FakeHRClient(outputs={"h": HRError("rate_limited", 429)})
    with pytest.raises(HRError):
        fake.run("h", "x")
    with pytest.raises(AssertionError, match="no canned output"):
        fake.run("other", "x")


def test_memory_tree_recall_reaches_children():
    fake = FakeHRClient()
    root = fake.create_memory("hive/demo")["id"]
    dec = fake.create_memory("decisions", parent_id=root)["id"]
    rec = fake.write_record(dec, {"title": "HTTP client is httpx", "attributes": {"kind": "decision"}})
    assert [r["record"]["id"] for r in fake.recall(root, "httpx")] == [rec["id"]]
    assert fake.recall(root, "httpx", filters={"attributes.kind": "handoff"}) == []
    fake.patch_record(dec, rec["id"], {"attributes": {"kind": "decision", "status": "accepted"}})
    assert fake.get_record(dec, rec["id"])["attributes"]["status"] == "accepted"
    fake.delete_record(dec, rec["id"])
    assert fake.list_records(dec) == []


def test_seeded_store(seeded_store):
    accepted = seeded_store.list_decisions(status=DecisionStatus.ACCEPTED)
    assert len(accepted) == 3
    assert [d.title for d in seeded_store.list_decisions(path="hive/web/static/app.js")][:1] == [
        "Dashboard uses HTMX"
    ]
    assert len(seeded_store.list_handoffs()) == 2
    assert seeded_store.recall("httpx timeout")[0].kind == "decision"


def test_transcript_fixture_shape(transcript_path):
    lines = [json.loads(line) for line in transcript_path.read_text().splitlines()]
    human_text = [
        x["message"]["content"]
        for x in lines
        if x["type"] == "user" and isinstance(x["message"]["content"], str)
    ]
    assert sum("never" in t for t in human_text) == 2


def test_sample_diff(sample_diff):
    assert "hive/web/static/app.js" in sample_diff and "Bearer" in sample_diff
