"""HarnessRouter HTTP client (sync, httpx). See docs/harnessrouter-api.md for the API this wraps.

- Bearer auth; `Idempotency-Key` on every POST (reused across retries of the same request).
- Error envelope `{"error": {code, message, ...}}` -> `HRError(code, status, message)`.
- Retries with exponential backoff on 429 / 5xx / transport errors, honoring `Retry-After`.
  Never retries `store_write_failed` (the work may have run; recover instead of resubmitting).
- `run()` streams SSE or polls; if a stream drops after `response.created` it polls instead of resubmitting.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values, find_dotenv

DEFAULT_BASE_URL = "https://api.harnessrouter.ai"
TERMINAL_STATUSES = {"completed", "failed", "incomplete", "cancelled"}
_TERMINAL_EVENTS = {"response.completed", "response.incomplete", "response.failed"}
_NO_RETRY_CODES = {"store_write_failed"}


class HRError(Exception):
    def __init__(self, code: str, status: int, message: str = "", detail: Any = None) -> None:
        super().__init__(f"[{status} {code}] {message}")
        self.code = code
        self.status = status
        self.message = message
        self.detail = detail


@dataclass
class RunResult:
    text: str
    response_id: str | None
    session_id: str | None
    status: str
    raw: dict = field(default_factory=dict, repr=False)


def iter_sse(lines: Iterable[str]) -> Iterator[str]:
    """Yield the `data` payload of each SSE event (multi-line data joined by '\\n'). Comments are skipped."""
    buf: list[str] = []
    for line in lines:
        line = line.rstrip("\r")
        if not line:
            if buf:
                yield "\n".join(buf)
                buf = []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if name == "data":
            buf.append(value[1:] if value.startswith(" ") else value)
    if buf:
        yield "\n".join(buf)


def _unwrap(body: Any) -> Any:
    """Memory/file list endpoints return `{"object": "list", "data": [...]}`; hand back the list."""
    if isinstance(body, dict) and body.get("object") == "list" and isinstance(body.get("data"), list):
        return body["data"]
    return body


def _pick(body: Any, key: str) -> Any:
    """Discovery endpoints wrap their list under a named key (verified live: bases, harnesses, backends)."""
    return body[key] if isinstance(body, dict) and key in body else body


def _rejected_before_work(e: Exception) -> bool:
    return (
        isinstance(e, HRError)
        and 400 <= e.status < 500
        and e.status not in (408, 429)
        and e.code not in _NO_RETRY_CODES
    )


def _response_text(resp: dict) -> str:
    if isinstance(resp.get("output_text"), str):
        return resp["output_text"]
    parts = []
    for item in resp.get("output") or []:
        for c in item.get("content") or [] if isinstance(item, dict) else []:
            if isinstance(c, dict) and c.get("type") == "output_text":
                parts.append(c.get("text", ""))
    return "".join(parts)


def _session_id(resp: dict) -> str | None:
    return (resp.get("metadata") or {}).get("session_id") or resp.get("session_id")


class HRClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 60.0,
        max_retries: int = 3,
        backoff: float = 0.5,
        poll_interval: float = 2.0,
        max_retry_after: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
        ledger: Any = None,
    ) -> None:
        """`ledger`: a `hive.hr.budget.SpendLedger` that guards and records every `run()` (T-26).
        `from_env()` attaches the default one; a bare constructor (tests) does not."""
        if not api_key:
            raise ValueError("HarnessRouter key missing: set HARNESS_ROUTER_KEY in .env")
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff = backoff
        self.poll_interval = poll_interval
        self.max_retry_after = max_retry_after  # never let a server freeze a hook/CLI for long (T-19)
        self._sleep = sleep
        self.ledger = ledger
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_env(cls, **kw: Any) -> HRClient:
        """Key/base URL from the environment, falling back to the nearest `.env` (env vars win; .env is not
        loaded into os.environ). TODO: build from `hive.config.get_settings()` once T-05 (#9) lands."""
        path = find_dotenv(usecwd=True)
        file_vals = dotenv_values(path) if path else {}

        def get(name: str) -> str:
            return (os.environ.get(name) or file_vals.get(name) or "").strip()

        if "ledger" not in kw:
            from hive.hr.budget import SpendLedger

            kw["ledger"] = SpendLedger.from_env()
        return cls(get("HARNESS_ROUTER_KEY"), get("HARNESS_ROUTER_URL") or DEFAULT_BASE_URL, **kw)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> HRClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- transport -----------------------------------------------------------

    @staticmethod
    def _error(resp: httpx.Response) -> HRError:
        try:
            e = resp.json().get("error") or {}
        except (ValueError, AttributeError):
            e = {}
        code = e.get("code") or f"http_{resp.status_code}"
        return HRError(code, resp.status_code, e.get("message") or resp.text[:200], e.get("detail"))

    def _delay(self, attempt: int, resp: httpx.Response | None) -> float:
        if resp is not None:
            try:
                return min(float(resp.headers["retry-after"]), self.max_retry_after)
            except (KeyError, ValueError):
                pass
        return self.backoff * (2**attempt)

    def _send(self, method: str, path: str, *, stream: bool = False, **kw: Any) -> httpx.Response:
        """Send with retries. Returns a successful response (opened lazily if `stream`)."""
        headers = dict(kw.pop("headers", None) or {})
        if method == "POST":
            headers.setdefault("Idempotency-Key", uuid.uuid4().hex)
        for attempt in range(self.max_retries + 1):
            last = attempt == self.max_retries
            try:
                req = self._http.build_request(method, path, headers=headers, **kw)
                resp = self._http.send(req, stream=stream)
            except httpx.TransportError:
                if last:
                    raise
                self._sleep(self._delay(attempt, None))
                continue
            if resp.status_code < 400:
                return resp
            if stream:
                resp.read()
                resp.close()
            error = self._error(resp)
            retryable = (
                resp.status_code == 429 or resp.status_code >= 500
            ) and error.code not in _NO_RETRY_CODES
            if not retryable or last:
                raise error
            self._sleep(self._delay(attempt, resp))
        raise AssertionError("unreachable")

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        resp = self._send(method, path, **kw)
        if resp.status_code == 204 or not resp.content:
            return None
        return _unwrap(resp.json())

    # --- discovery & harnesses -----------------------------------------------

    def bases(self) -> list[dict]:
        return _pick(self._request("GET", "/v1/bases"), "bases")

    def models(self) -> dict[str, dict]:
        """`{backend: {"default": model_id, "models": [...]}}`."""
        return _pick(self._request("GET", "/v1/models"), "backends")

    def harnesses(self) -> list[dict]:
        """Note: response fields are camelCase (`defaultModel`, `systemPrompt`, ...)."""
        return _pick(self._request("GET", "/v1/harnesses"), "harnesses")

    def get_harness(self, harness_id: str) -> dict:
        return self._request("GET", f"/v1/harnesses/{harness_id}")

    def create_harness(self, spec: dict) -> dict:
        return self._request("POST", "/v1/harnesses", json=spec)

    def update_harness(self, harness_id: str, spec: dict) -> dict:
        return self._request("PUT", f"/v1/harnesses/{harness_id}", json=spec)

    # --- responses -----------------------------------------------------------

    def get_response(self, response_id: str) -> dict:
        return self._request("GET", f"/v1/responses/{response_id}")

    def cancel_response(self, response_id: str) -> dict:
        return self._request("POST", f"/v1/responses/{response_id}/cancel")

    def wait(self, response_id: str, *, timeout: float = 900.0) -> RunResult:
        """Poll a response until it reaches a terminal status."""
        deadline = time.monotonic() + timeout
        while True:
            resp = self.get_response(response_id)
            if resp.get("status") in TERMINAL_STATUSES:
                return RunResult(
                    _response_text(resp), resp.get("id", response_id), _session_id(resp), resp["status"], resp
                )
            if time.monotonic() > deadline:
                raise HRError(
                    "timeout", 0, f"response {response_id} still {resp.get('status')} after {timeout}s"
                )
            self._sleep(self.poll_interval)

    def run(self, harness_id: str, input: Any, *, stream: bool = False, **opts: Any) -> RunResult:
        """Run a task on a harness. Extra kwargs go into the request body (max_cost_usd, metadata, ...).

        With a ledger, raises `BudgetExceeded` (an HRError) before sending anything if today's spend plus this
        run's `max_cost_usd` would pass the daily budget, and records the run's cost afterwards.
        """
        max_cost = opts.get("max_cost_usd")
        if self.ledger is not None:
            self.ledger.check(max_cost)
        try:
            result = self._run(harness_id, input, stream=stream, **opts)
        except Exception as e:
            # The request may have gone out and the work may have run (5xx, timeout, dropped stream,
            # store_write_failed): count an upper bound so retries can't spend without limit. A clean 4xx
            # rejection (bad input, auth) means nothing ran.
            if self.ledger is not None and not _rejected_before_work(e):
                self.ledger.record_run(harness_id, None, max_cost_usd=max_cost, status="error")
            raise
        if self.ledger is not None:
            self.ledger.record_run(
                harness_id,
                result.raw,
                max_cost_usd=max_cost,
                response_id=result.response_id,
                status=result.status,
            )
        return result

    def _run(self, harness_id: str, input: Any, *, stream: bool, **opts: Any) -> RunResult:
        body = {"input": input, **opts}
        path = f"/{harness_id}/v1/responses"
        if not stream:
            resp = self._request("POST", path, json={**body, "background": True})
            if resp.get("status") in TERMINAL_STATUSES:
                return RunResult(
                    _response_text(resp), resp.get("id"), _session_id(resp), resp["status"], resp
                )
            return self.wait(resp["id"])

        response_id = session_id = None
        chunks: list[str] = []
        try:
            with closing(self._send("POST", path, json={**body, "stream": True}, stream=True)) as r:
                for data in iter_sse(r.iter_lines()):
                    try:
                        ev = json.loads(data)
                    except ValueError:
                        continue
                    etype = ev.get("type")
                    if etype == "response.created":
                        response_id = ev["response"]["id"]
                        session_id = _session_id(ev["response"])
                    elif etype == "response.output_text.delta":
                        chunks.append(ev.get("delta", ""))
                    elif etype in _TERMINAL_EVENTS:
                        resp = ev.get("response") or {}
                        status = resp.get("status") or etype.removeprefix("response.")
                        return RunResult(
                            "".join(chunks) or _response_text(resp),
                            resp.get("id", response_id),
                            _session_id(resp) or session_id,
                            status,
                            resp,
                        )
        except httpx.TransportError:
            if response_id is None:
                raise
        if response_id is None:
            raise HRError("stream_dropped", 0, "stream ended before response.created")
        return self.wait(response_id)  # dropped mid-stream: recover, never resubmit

    # --- files ---------------------------------------------------------------

    def upload_file(
        self, file: str | Path | bytes, *, filename: str | None = None, purpose: str = "user_data"
    ) -> dict:
        if isinstance(file, bytes):
            content, name = file, filename or "upload.bin"
        else:
            p = Path(file)
            content, name = p.read_bytes(), filename or p.name
        return self._request("POST", "/v1/files", files={"file": (name, content)}, data={"purpose": purpose})

    # --- memory --------------------------------------------------------------

    def list_memories(self) -> list[dict]:
        return self._request("GET", "/v1/memories")

    def create_memory(
        self,
        name: str,
        *,
        parent_id: str | None = None,
        description: str | None = None,
        provider: str | None = None,
        restricted: bool | None = None,
    ) -> dict:
        body = {
            "name": name,
            "parent_id": parent_id,
            "description": description,
            "provider": provider,
            "restricted": restricted,
        }
        return self._request("POST", "/v1/memories", json={k: v for k, v in body.items() if v is not None})

    def write_record(self, memory_id: str, record: dict) -> dict:
        return self._request("POST", f"/v1/memories/{memory_id}/records", json=record)

    def recall(
        self,
        memory_id: str,
        query: str | None = None,
        *,
        text: str | None = None,
        filters: dict | None = None,
        types: list[str] | None = None,
        limit: int = 8,
    ) -> list[dict]:
        body = {"query": query, "text": text, "filters": filters, "types": types, "limit": limit}
        out = self._request(
            "POST", f"/v1/memories/{memory_id}/recall", json={k: v for k, v in body.items() if v is not None}
        )
        return out.get("results", []) if isinstance(out, dict) else out

    def list_records(self, memory_id: str, type: str | None = None) -> list[dict]:
        params = {"type": type} if type else None
        return self._request("GET", f"/v1/memories/{memory_id}/records", params=params)

    def get_record(self, memory_id: str, record_id: str) -> dict:
        return self._request("GET", f"/v1/memories/{memory_id}/records/{record_id}")

    def patch_record(self, memory_id: str, record_id: str, patch: dict) -> dict:
        return self._request("PATCH", f"/v1/memories/{memory_id}/records/{record_id}", json=patch)

    def delete_record(self, memory_id: str, record_id: str) -> None:
        self._request("DELETE", f"/v1/memories/{memory_id}/records/{record_id}")

    # --- events --------------------------------------------------------------

    def events(self) -> Iterator[dict]:
        """Workspace event stream. Open ONE per process and fan it out (per-org stream limit)."""
        resp = self._send(
            "GET",
            "/v1/events",
            stream=True,
            headers={"Accept": "text/event-stream"},
            timeout=httpx.Timeout(10, read=None),
        )
        with closing(resp) as r:
            for data in iter_sse(r.iter_lines()):
                try:
                    yield json.loads(data)
                except ValueError:
                    yield {"raw": data}
