import json
from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from hive.hr.budget import BudgetExceeded, SpendLedger, extract_cost, format_spend
from hive.hr.client import HRClient, HRError

BASE = "https://hr.test"


@pytest.fixture
def ledger(tmp_path):
    return SpendLedger(tmp_path / "spend.jsonl", daily_budget_usd=1.0)


def test_extract_cost():
    assert extract_cost({"cost_usd": 0.12}) == 0.12
    assert extract_cost({"usage": {"total_cost_usd": 0.3}}) == 0.3
    assert extract_cost({"usage": {"input_tokens": 5}}) is None
    assert extract_cost({"cost": True}) is None
    assert extract_cost(None) is None


def test_record_and_totals_per_day(ledger):
    ledger.record("chrn_a", 0.25, estimated=False)
    ledger.record("chrn_a", 0.05, estimated=False)
    ledger.record("chrn_b", 0.1, estimated=True)
    ledger.record("chrn_a", 9.0, estimated=False, at=datetime(2020, 1, 1, tzinfo=UTC))  # other day
    assert ledger.by_harness() == pytest.approx({"chrn_a": 0.30, "chrn_b": 0.1})
    assert ledger.total() == pytest.approx(0.4)
    assert ledger.total(date(2020, 1, 1)) == 9.0


def test_corrupt_lines_are_ignored(ledger):
    ledger.record("h", 0.2, estimated=False)
    with ledger.path.open("a") as f:
        f.write('{"at": "garbage"\n{not json}\n')
    assert ledger.total() == pytest.approx(0.2)


def test_check_blocks_when_over_or_projected_over(ledger):
    ledger.check(0.5)
    ledger.record("h", 0.8, estimated=False)
    ledger.check(0.2)  # exactly at budget is allowed
    with pytest.raises(BudgetExceeded) as ei:
        ledger.check(0.5)
    assert ei.value.code == "budget_exceeded" and "HIVE_DAILY_BUDGET_USD" in ei.value.message
    ledger.record("h", 0.2, estimated=False)
    with pytest.raises(BudgetExceeded):
        ledger.check()  # spent == budget: no more runs


def test_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVE_DAILY_BUDGET_USD", "2.5")
    monkeypatch.setenv("HIVE_SPEND_PATH", str(tmp_path / "x.jsonl"))
    led = SpendLedger.from_env()
    assert led.daily_budget_usd == 2.5 and led.path == tmp_path / "x.jsonl"
    monkeypatch.setenv("HIVE_DAILY_BUDGET_USD", "lots")
    with pytest.raises(ValueError, match="HIVE_DAILY_BUDGET_USD"):
        SpendLedger.from_env()


def test_format_spend(ledger):
    ledger.record("chrn_a", 0.25, estimated=False)
    ledger.record("chrn_b", 1.0, estimated=True)
    out = format_spend(ledger, names={"chrn_a": "hive-guard"})
    assert "hive-guard" in out and "$0.2500" in out
    assert "chrn_b" in out and "(upper bound)" in out
    assert "$1.2500 of $1.00 budget" in out


# --- client integration ------------------------------------------------------


def client_with(ledger):
    return HRClient("sk-hr-test", base_url=BASE, sleep=lambda s: None, poll_interval=0, ledger=ledger)


@respx.mock
def test_run_records_reported_cost(ledger):
    respx.post(f"{BASE}/chrn_1/v1/responses").respond(
        json={"id": "resp_1", "status": "completed", "output_text": "ok", "usage": {"cost_usd": 0.07}}
    )
    client_with(ledger).run("chrn_1", "hi", max_cost_usd=0.5)
    [entry] = ledger.entries()
    assert (entry["harness"], entry["cost_usd"], entry["estimated"]) == ("chrn_1", 0.07, False)


@respx.mock
def test_run_without_reported_cost_records_upper_bound(tmp_path):
    ledger = SpendLedger(tmp_path / "s.jsonl", daily_budget_usd=5.0)
    respx.post(f"{BASE}/chrn_1/v1/responses").respond(json={"id": "r", "status": "completed"})
    client_with(ledger).run("chrn_1", "hi", max_cost_usd=0.4)
    client_with(ledger).run("chrn_1", "hi")
    assert [(e["cost_usd"], e["estimated"]) for e in ledger.entries()] == [(0.4, True), (1.0, True)]


@respx.mock
def test_over_budget_blocks_before_any_request(ledger):
    route = respx.post(f"{BASE}/chrn_1/v1/responses").respond(json={"id": "r", "status": "completed"})
    ledger.record("chrn_1", 0.9, estimated=False)
    with pytest.raises(HRError) as ei:  # BudgetExceeded is an HRError: one except clause for callers
        client_with(ledger).run("chrn_1", "hi", max_cost_usd=0.5)
    assert ei.value.code == "budget_exceeded"
    assert not route.called


@respx.mock
def test_no_ledger_means_no_tracking(tmp_path):
    respx.post(f"{BASE}/chrn_1/v1/responses").respond(json={"id": "r", "status": "completed"})
    HRClient("sk-hr-test", base_url=BASE, poll_interval=0).run("chrn_1", "hi")


def test_from_env_attaches_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_ROUTER_KEY", "sk-hr-x")
    monkeypatch.setenv("HIVE_SPEND_PATH", str(tmp_path / "s.jsonl"))
    with HRClient.from_env() as c:
        assert isinstance(c.ledger, SpendLedger) and c.ledger.path == tmp_path / "s.jsonl"


@respx.mock
def test_failed_http_run_records_nothing(ledger):
    respx.post(f"{BASE}/chrn_1/v1/responses").mock(
        return_value=httpx.Response(400, json={"error": {"code": "invalid_input"}})
    )
    with pytest.raises(HRError):
        client_with(ledger).run("chrn_1", "")
    assert ledger.entries() == []
    assert json.dumps(ledger.by_harness()) == "{}"


def test_check_without_max_cost_projects_the_harness_cap(tmp_path):
    led = SpendLedger(tmp_path / "s.jsonl", daily_budget_usd=5.0)
    led.record("h", 4.5, estimated=False)
    with pytest.raises(BudgetExceeded):
        led.check()  # no max_cost_usd: assume $1, so 4.5 + 1 > 5
    led.check(0.25)


@pytest.mark.parametrize("bad", ["nan", "inf", "-1"])
def test_from_env_rejects_non_finite_or_negative(monkeypatch, bad):
    monkeypatch.setenv("HIVE_DAILY_BUDGET_USD", bad)
    with pytest.raises(ValueError, match="finite, non-negative"):
        SpendLedger.from_env()


@respx.mock
def test_run_that_fails_after_sending_is_recorded_as_upper_bound(ledger):
    respx.post(f"{BASE}/chrn_1/v1/responses").mock(
        return_value=httpx.Response(504, json={"error": {"code": "timeout"}})
    )
    with pytest.raises(HRError):
        client_with(ledger).run("chrn_1", "hi", max_cost_usd=0.3)
    [entry] = ledger.entries()
    assert (entry["cost_usd"], entry["estimated"], entry["status"]) == (0.3, True, "error")


@respx.mock
def test_store_write_failed_and_transport_errors_are_recorded(ledger):
    respx.post(f"{BASE}/chrn_1/v1/responses").mock(
        side_effect=[
            httpx.Response(502, json={"error": {"code": "store_write_failed"}}),
            httpx.ConnectError("x"),
        ]
        + [httpx.ConnectError("x")] * 5
    )
    for _ in range(2):
        with pytest.raises((HRError, httpx.TransportError)):
            client_with(ledger).run("chrn_1", "hi", max_cost_usd=0.1)
    assert [e["status"] for e in ledger.entries()] == ["error", "error"]
