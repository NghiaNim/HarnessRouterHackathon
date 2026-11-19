from __future__ import annotations

import json
import os

import httpx
import pytest
import respx

from hive.hr.client import DEFAULT_BASE_URL, HRClient, HRError, RunResult, iter_sse

BASE = "https://hr.test"


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(sleeps):
    c = HRClient("sk-hr-test", base_url=BASE, sleep=sleeps.append, poll_interval=0)
    yield c
    c.close()


def err(status: int, code: str, message: str = "boom") -> httpx.Response:
    return httpx.Response(status, json={"error": {"type": "x", "code": code, "message": message}})


def sse(*frames: dict | str) -> bytes:
    out = [": connected\n\n"]
    for f in frames:
        out.append(f"data: {f if isinstance(f, str) else json.dumps(f)}\n\n")
    return "".join(out).encode()


# --- auth & basics -----------------------------------------------------------


@respx.mock
def test_bearer_auth_and_unwraps_named_lists(client):
    route = respx.get(f"{BASE}/v1/bases").respond(
        json={"bases": [{"id": "claude-code"}], "runtimeDefaults": {}}
    )
    assert client.bases() == [{"id": "claude-code"}]
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-hr-test"


def test_requires_key():
    with pytest.raises(ValueError, match="HARNESS_ROUTER_KEY"):
        HRClient("")


def test_from_env(monkeypatch):
    monkeypatch.setenv("HARNESS_ROUTER_KEY", "sk-hr-env")
    monkeypatch.delenv("HARNESS_ROUTER_URL", raising=False)
    with HRClient.from_env() as c:
        assert c.base_url == DEFAULT_BASE_URL


@respx.mock
def test_posts_send_idempotency_key_reused_on_retry(client, sleeps):
    route = respx.post(f"{BASE}/v1/harnesses").mock(
        side_effect=[err(503, "unavailable"), httpx.Response(200, json={"id": "chrn_1"})]
    )
    assert client.create_harness({"name": "hive-x", "base": "claude-code"}) == {"id": "chrn_1"}
    keys = [c.request.headers["idempotency-key"] for c in route.calls]
    assert len(keys) == 2 and keys[0] == keys[1] and keys[0]
    assert len(sleeps) == 1


# --- errors & retry ----------------------------------------------------------


@respx.mock
def test_error_envelope_maps_to_hrerror(client):
    respx.get(f"{BASE}/v1/harnesses/nope").mock(return_value=err(404, "not_found", "no such harness"))
    with pytest.raises(HRError) as ei:
        client.get_harness("nope")
    assert (ei.value.code, ei.value.status, ei.value.message) == ("not_found", 404, "no such harness")


@respx.mock
def test_non_json_error_still_maps(client):
    respx.get(f"{BASE}/v1/models").respond(400, text="bad gateway html")
    with pytest.raises(HRError) as ei:
        client.models()
    assert ei.value.status == 400 and ei.value.code == "http_400"


@respx.mock
def test_retries_429_with_retry_after_then_succeeds(client, sleeps):
    respx.get(f"{BASE}/v1/models").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"code": "rate_limited"}}),
            httpx.Response(200, json={"backends": {"claude": {}}}),
        ]
    )
    assert client.models() == {"claude": {}}
    assert sleeps == [2.0]


@respx.mock
def test_gives_up_after_max_retries(client, sleeps):
    route = respx.get(f"{BASE}/v1/models").mock(return_value=err(500, "internal_error"))
    with pytest.raises(HRError) as ei:
        client.models()
    assert ei.value.code == "internal_error"
    assert route.call_count == client.max_retries + 1
    assert sleeps == sorted(sleeps)  # exponential backoff


@respx.mock
def test_never_retries_store_write_failed(client):
    route = respx.post(f"{BASE}/v1/memories/hmem_1/records").mock(return_value=err(502, "store_write_failed"))
    with pytest.raises(HRError) as ei:
        client.write_record("hmem_1", {"title": "t"})
    assert ei.value.code == "store_write_failed"
    assert route.call_count == 1


@respx.mock
def test_does_not_retry_4xx(client):
    route = respx.get(f"{BASE}/v1/harnesses").mock(return_value=err(401, "invalid_credential"))
    with pytest.raises(HRError):
        client.harnesses()
    assert route.call_count == 1


@respx.mock
def test_retries_transport_errors(client):
    respx.get(f"{BASE}/v1/harnesses").mock(
        side_effect=[httpx.ConnectError("down"), httpx.Response(200, json={"harnesses": []})]
    )
    assert client.harnesses() == []


# --- SSE ---------------------------------------------------------------------


def test_iter_sse_parses_frames():
    lines = [
        "retry: 3000",
        ": connected",
        "",
        'data: {"a": 1}',
        "",
        "data: line1",
        "data: line2",
        "",
        "data: tail",
    ]
    assert list(iter_sse(lines)) == ['{"a": 1}', "line1\nline2", "tail"]


CREATED = {"type": "response.created", "response": {"id": "resp_1", "metadata": {"session_id": "sess_1"}}}


@respx.mock
def test_run_stream_accumulates_deltas(client):
    route = respx.post(f"{BASE}/chrn_1/v1/responses").respond(
        content=sse(
            CREATED,
            {"type": "response.output_item.added", "item": {"type": "function_call"}},
            {"type": "response.output_text.delta", "delta": "Hel"},
            {"type": "response.output_text.delta", "delta": "lo"},
            {"type": "response.completed", "response": {"id": "resp_1", "status": "completed"}},
        ),
        headers={"content-type": "text/event-stream"},
    )
    res = client.run("chrn_1", "hi", stream=True, max_cost_usd=0.5)
    assert res == RunResult(
        text="Hello", response_id="resp_1", session_id="sess_1", status="completed", raw=res.raw
    )
    body = json.loads(route.calls.last.request.content)
    assert body == {"input": "hi", "stream": True, "max_cost_usd": 0.5}
    assert route.calls.last.request.headers["idempotency-key"]


@respx.mock
def test_run_stream_drop_falls_back_to_polling_without_resubmit(client):
    post = respx.post(f"{BASE}/chrn_1/v1/responses").respond(
        content=sse(CREATED, {"type": "response.output_text.delta", "delta": "par"}),
    )
    respx.get(f"{BASE}/v1/responses/resp_1").mock(
        side_effect=[
            httpx.Response(200, json={"id": "resp_1", "status": "in_progress"}),
            httpx.Response(
                200,
                json={
                    "id": "resp_1",
                    "status": "completed",
                    "metadata": {"session_id": "sess_1"},
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "full"}]}],
                },
            ),
        ]
    )
    res = client.run("chrn_1", "hi", stream=True)
    assert (res.text, res.status, res.session_id) == ("full", "completed", "sess_1")
    assert post.call_count == 1


@respx.mock
def test_run_stream_failed_terminal(client):
    respx.post(f"{BASE}/chrn_1/v1/responses").respond(
        content=sse(CREATED, {"type": "response.failed", "response": {"id": "resp_1", "status": "failed"}})
    )
    assert client.run("chrn_1", "hi", stream=True).status == "failed"


@respx.mock
def test_run_background_polls_until_terminal(client):
    post = respx.post(f"{BASE}/chrn_1/v1/responses").respond(json={"id": "resp_2", "status": "queued"})
    respx.get(f"{BASE}/v1/responses/resp_2").respond(
        json={"id": "resp_2", "status": "completed", "output_text": "done", "metadata": {"session_id": "s2"}}
    )
    res = client.run("chrn_1", [{"role": "user", "content": "x"}])
    assert (res.text, res.response_id, res.session_id) == ("done", "resp_2", "s2")
    assert json.loads(post.calls.last.request.content)["background"] is True


@respx.mock
def test_events_iterates_json_frames(client):
    respx.get(f"{BASE}/v1/events").respond(
        content=b'retry: 3000\n: connected\n\ndata: {"type": "task.started"}\n\ndata: not-json\n\n'
    )
    assert list(client.events()) == [{"type": "task.started"}, {"raw": "not-json"}]


# --- memory ------------------------------------------------------------------


@respx.mock
def test_memory_calls(client):
    create = respx.post(f"{BASE}/v1/memories").respond(json={"id": "hmem_1"})
    recall = respx.post(f"{BASE}/v1/memories/hmem_1/recall").respond(json={"results": [{"score": 1}]})
    respx.get(f"{BASE}/v1/memories/hmem_1/records", params={"type": "note"}).respond(
        json={"object": "list", "data": [{"id": "r"}]}
    )
    patch = respx.patch(f"{BASE}/v1/memories/hmem_1/records/hrec_1").respond(json={"id": "hrec_1"})
    delete = respx.delete(f"{BASE}/v1/memories/hmem_1/records/hrec_1").respond(204)

    assert client.create_memory("decisions", parent_id="hmem_0") == {"id": "hmem_1"}
    assert json.loads(create.calls.last.request.content) == {"name": "decisions", "parent_id": "hmem_0"}

    assert client.recall("hmem_1", "httpx", filters={"attributes.kind": "decision"}, limit=3) == [
        {"score": 1}
    ]
    assert json.loads(recall.calls.last.request.content) == {
        "query": "httpx",
        "filters": {"attributes.kind": "decision"},
        "limit": 3,
    }
    assert client.list_records("hmem_1", type="note") == [{"id": "r"}]
    client.patch_record("hmem_1", "hrec_1", {"title": "new"})
    assert json.loads(patch.calls.last.request.content) == {"title": "new"}
    assert client.delete_record("hmem_1", "hrec_1") is None
    assert delete.called


@respx.mock
def test_upload_file(client, tmp_path):
    route = respx.post(f"{BASE}/v1/files").respond(json={"id": "file_1"})
    p = tmp_path / "diff.txt"
    p.write_text("hello")
    assert client.upload_file(p)["id"] == "file_1"
    body = route.calls.last.request.content
    assert b"hello" in body and b'name="purpose"' in body


@pytest.mark.skipif(os.environ.get("HIVE_LIVE") != "1", reason="live HarnessRouter test; set HIVE_LIVE=1")
def test_live_lists_bases():
    from dotenv import load_dotenv

    load_dotenv()
    with HRClient.from_env() as c:
        ids = {b.get("id") for b in c.bases()}
    assert "claude-code" in ids
