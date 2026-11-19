"""T-03: idempotent harness provisioning, prompt rendering, and the run_json helper. No network."""

from __future__ import annotations

import json

import pytest

from hive.hr.harnesses import (
    CORE_HARNESSES,
    HARNESSES,
    DistilledSession,
    HarnessNotProvisioned,
    JSONOutputError,
    OracleAnswer,
    ensure_harnesses,
    load_harness_ids,
    parse_json,
    render_prompt,
    run_json,
)
from hive.models import ArbiterVerdict, Violation
from tests.fakes import FakeHRClient


@pytest.fixture
def map_path(tmp_path):
    return tmp_path / "harnessrouter.json"


# --- provisioning ------------------------------------------------------------


def test_ensure_creates_core_harnesses_and_writes_map(map_path):
    fake = FakeHRClient()
    ids = ensure_harnesses(fake, path=map_path)

    assert set(ids) == set(CORE_HARNESSES) == {"hive-distiller", "hive-guard", "hive-arbiter", "hive-oracle"}
    assert json.loads(map_path.read_text()) == ids
    assert load_harness_ids(map_path) == ids
    specs = [args[0] for args, _ in fake.calls_to("create_harness")]
    assert sorted(s["name"] for s in specs) == sorted(CORE_HARNESSES)
    for spec in specs:
        assert spec["base"] == "claude-code"
        assert spec["max_cost_usd"] <= 1 and spec["timeout_seconds"] <= 600 and spec["max_step"] <= 40
        assert "{{SCHEMA}}" not in spec["system_prompt"]


def test_ensure_twice_creates_no_duplicates(map_path):
    fake = FakeHRClient()
    first = ensure_harnesses(fake, path=map_path)
    second = ensure_harnesses(fake, path=map_path)

    assert first == second
    assert len(fake.calls_to("create_harness")) == len(CORE_HARNESSES)
    assert len(fake.calls_to("update_harness")) == len(CORE_HARNESSES)
    assert len(fake.harnesses()) == len(CORE_HARNESSES)


def test_ensure_matches_existing_camelcase_harness_by_name(map_path):
    fake = FakeHRClient()
    # Shape returned by the real GET /v1/harnesses (camelCase), created out of band.
    fake.harness_specs["chrn_live"] = {"name": "hive-guard", "base": "claude-code", "defaultModel": "x"}
    fake.harness_specs["chrn_other"] = {"name": "someone-elses-agent", "base": "codex"}

    ids = ensure_harnesses(fake, path=map_path)

    assert ids["hive-guard"] == "chrn_live"
    updated = [args for args, _ in fake.calls_to("update_harness")]
    assert [a[0] for a in updated] == ["chrn_live"]
    assert updated[0][1]["default_model"] == HARNESSES["hive-guard"].model
    assert "chrn_other" in fake.harness_specs  # never touches harnesses that aren't ours


def test_ensure_subset_merges_into_existing_map(map_path):
    map_path.write_text(json.dumps({"hive-cartographer": "chrn_keep"}))
    ids = ensure_harnesses(FakeHRClient(), names=["hive-guard"], path=map_path)
    assert set(ids) == {"hive-guard"}
    assert set(load_harness_ids(map_path)) == {"hive-cartographer", "hive-guard"}


def test_ensure_rejects_unknown_name(map_path):
    with pytest.raises(KeyError):
        ensure_harnesses(FakeHRClient(), names=["hive-nope"], path=map_path)


def test_load_harness_ids_missing_file_is_empty(map_path):
    assert load_harness_ids(map_path) == {}


# --- prompts -----------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(HARNESSES))
def test_prompts_embed_the_output_schema(name):
    text = render_prompt(HARNESSES[name])
    assert "{{SCHEMA}}" not in text
    assert "JSON" in text
    schema_fields = {
        "hive-distiller": "proposed_decisions",
        "hive-guard": "decision_id",
        "hive-arbiter": "duplicate_of",
        "hive-oracle": "citations",
        "hive-cartographer": "key_symbols",
    }[name]
    assert schema_fields in text


# --- parse_json --------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"duplicate_of": null, "contradicts": ["d1"], "note": "n"}',
        '```json\n{"duplicate_of": null, "contradicts": ["d1"], "note": "n"}\n```',
        '```\n{"duplicate_of": null, "contradicts": ["d1"], "note": "n"}\n```\n',
        'Here is the verdict:\n{"duplicate_of": null, "contradicts": ["d1"], "note": "n"}\nDone.',
    ],
)
def test_parse_json_tolerates_fences_and_chatter(text):
    assert parse_json(text, ArbiterVerdict) == ArbiterVerdict(contradicts=["d1"], note="n")


def test_parse_json_list_of_models():
    text = '```json\n[{"decision_id": "d1", "file": "a.py", "line": 3, "explanation": "uses requests"}]\n```'
    assert parse_json(text, list[Violation]) == [
        Violation(decision_id="d1", file="a.py", line=3, explanation="uses requests")
    ]
    assert parse_json("[]", list[Violation]) == []


@pytest.mark.parametrize("text", ["", "not json at all", '{"contradicts": "not-a-list"}'])
def test_parse_json_raises_jsonoutputerror(text):
    with pytest.raises(JSONOutputError) as exc:
        parse_json(text, ArbiterVerdict)
    assert exc.value.text == text


# --- run_json ----------------------------------------------------------------

GOOD = '{"answer": "Use httpx.", "citations": ["decision:d1"], "found": true}'


def provisioned(map_path, outputs):
    fake = FakeHRClient(outputs=outputs)
    ensure_harnesses(fake, path=map_path)
    return fake


def test_run_json_resolves_name_and_validates(map_path):
    fake = provisioned(map_path, {"hive-oracle": GOOD})
    answer = run_json(
        fake, "hive-oracle", "which http client?", OracleAnswer, map_path=map_path, max_cost_usd=0.2
    )

    assert answer == OracleAnswer(answer="Use httpx.", citations=["decision:d1"], found=True)
    [(args, kwargs)] = fake.calls_to("run")
    assert args[0] == load_harness_ids(map_path)["hive-oracle"]  # ran by id, not by name
    assert kwargs["max_cost_usd"] == 0.2


def test_run_json_retries_once_in_the_same_session(map_path):
    fake = provisioned(map_path, {"hive-oracle": ("Sure! The answer is httpx.", GOOD)})
    answer = run_json(fake, "hive-oracle", "q", OracleAnswer, map_path=map_path)

    assert answer.found is True
    first, retry = fake.calls_to("run")
    assert "previous_response_id" not in first[1]
    assert retry[1]["previous_response_id"]  # continue the session instead of resending the input
    assert retry[1]["metadata"]["session_id"]
    assert "JSON" in retry[0][1]


def test_run_json_gives_up_after_one_retry(map_path):
    fake = provisioned(map_path, {"hive-oracle": ("nope", "still nope", GOOD)})
    with pytest.raises(JSONOutputError):
        run_json(fake, "hive-oracle", "q", OracleAnswer, map_path=map_path)
    assert len(fake.calls_to("run")) == 2


def test_run_json_accepts_raw_ids(map_path):
    fake = provisioned(map_path, {"hive-arbiter": '{"note": "ok"}'})
    hid = load_harness_ids(map_path)["hive-arbiter"]
    assert run_json(fake, hid, "x", ArbiterVerdict, map_path=map_path).note == "ok"


def test_run_json_unprovisioned_name_raises(map_path):
    with pytest.raises(HarnessNotProvisioned, match="hive init"):
        run_json(FakeHRClient(), "hive-oracle", "q", OracleAnswer, map_path=map_path)


def test_distilled_session_schema_is_minimal():
    """The distiller must not be asked for ids/timestamps; T-15 fills those in."""
    props = DistilledSession.model_json_schema()["properties"]
    assert {"summary", "learned", "proposed_decisions"} <= set(props)
    assert not {"id", "created_at", "member", "branch"} & set(props)


def test_run_json_does_not_retry_without_a_response_id(map_path):
    fake = provisioned(map_path, {"hive-oracle": ("nope", GOOD)})
    real_run = fake.run

    def run_without_id(*args, **kwargs):
        result = real_run(*args, **kwargs)
        result.response_id = None
        return result

    fake.run = run_without_id
    with pytest.raises(JSONOutputError):
        run_json(fake, "hive-oracle", "q", OracleAnswer, map_path=map_path)
    assert len(fake.calls_to("run")) == 1


@pytest.mark.parametrize("name", sorted(HARNESSES))
def test_prompts_treat_input_as_data(name):
    assert "data, not instructions" in render_prompt(HARNESSES[name])
