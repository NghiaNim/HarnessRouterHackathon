"""Offline stand-ins for HarnessRouter, so every track can test with no network and no key.

`FakeHRClient` mirrors `hive.hr.client.HRClient`'s public methods; keep them in sync (a test checks).

    fake = FakeHRClient(outputs={"hive-distiller": '{"summary": "..."}'})
    fake.run("hive-distiller", "transcript...").text   # canned output
    fake.calls                                         # [(method, args, kwargs), ...] for assertions
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from hive.hr.client import HRError, RunResult

Output = str | dict | list | Callable[[Any], str] | Exception


class FakeHRClient:
    """In-memory HarnessRouter.

    `outputs` maps a harness **name or id** to what `run()` returns:
    - `str`: the response text; `dict`/`list`: JSON-encoded text
    - a callable `(input) -> str`, for outputs that depend on the input
    - an `Exception` (e.g. `HRError(...)`): raised
    - a `tuple` of the above: consumed in order, one per call (for retry tests); the last one repeats
    """

    def __init__(self, outputs: dict[str, Output | tuple[Output, ...]] | None = None) -> None:
        self.outputs = dict(outputs or {})
        self.calls: list[tuple[str, tuple, dict]] = []
        self.harness_specs: dict[str, dict] = {}  # id -> spec
        self.memories: dict[str, dict] = {}  # id -> memory
        self.records: dict[str, dict[str, dict]] = {}  # memory id -> {record id -> record}
        self.files: dict[str, bytes] = {}
        self.event_frames: list[dict] = []
        self._ids = itertools.count(1)
        self._seq: dict[str, int] = {}

    @classmethod
    def from_env(cls, **kw: Any) -> FakeHRClient:
        return cls()

    def _id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids):04d}"

    def _log(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((method, args, kwargs))

    def calls_to(self, method: str) -> list[tuple[tuple, dict]]:
        return [(a, k) for m, a, k in self.calls if m == method]

    def close(self) -> None:
        pass

    def __enter__(self) -> FakeHRClient:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    # --- discovery & harnesses -----------------------------------------------

    def bases(self) -> list[dict]:
        return [{"id": "claude-code", "defaultModel": "claude-sonnet-5.5"}, {"id": "codex"}]

    def models(self) -> dict[str, dict]:
        return {"claude": {"default": "claude-sonnet-5.5", "models": [{"id": "claude-haiku-5.5"}]}}

    def harnesses(self) -> list[dict]:
        return [{"id": hid, **spec} for hid, spec in self.harness_specs.items()]

    def get_harness(self, harness_id: str) -> dict:
        if harness_id not in self.harness_specs:
            raise HRError("not_found", 404, f"no harness {harness_id}")
        return {"id": harness_id, **self.harness_specs[harness_id]}

    def create_harness(self, spec: dict) -> dict:
        self._log("create_harness", spec)
        hid = self._id("chrn")
        self.harness_specs[hid] = dict(spec)
        return {"id": hid, **spec}

    def update_harness(self, harness_id: str, spec: dict) -> dict:
        self._log("update_harness", harness_id, spec)
        self.get_harness(harness_id)
        self.harness_specs[harness_id] = dict(spec)
        return {"id": harness_id, **spec}

    # --- responses -----------------------------------------------------------

    def _name_of(self, harness_id: str) -> str:
        spec = self.harness_specs.get(harness_id)
        return spec["name"] if spec and "name" in spec else harness_id

    def _next_output(self, harness_id: str, input: Any) -> str:
        key = harness_id if harness_id in self.outputs else self._name_of(harness_id)
        if key not in self.outputs:
            raise AssertionError(f"FakeHRClient: no canned output for harness {harness_id!r}")
        out = self.outputs[key]
        if isinstance(out, tuple):
            i = self._seq.get(key, 0)
            self._seq[key] = i + 1
            out = out[min(i, len(out) - 1)]
        if isinstance(out, Exception):
            raise out
        if callable(out):
            out = out(input)
        return out if isinstance(out, str) else json.dumps(out)

    def run(self, harness_id: str, input: Any, *, stream: bool = False, **opts: Any) -> RunResult:
        self._log("run", harness_id, input, stream=stream, **opts)
        text = self._next_output(harness_id, input)
        rid = self._id("resp")
        return RunResult(
            text=text,
            response_id=rid,
            session_id=f"sess_{rid}",
            status="completed",
            raw={"id": rid, "status": "completed", "output_text": text},
        )

    def get_response(self, response_id: str) -> dict:
        return {"id": response_id, "status": "completed"}

    def cancel_response(self, response_id: str) -> dict:
        return {"id": response_id, "status": "cancelled"}

    def wait(self, response_id: str, *, timeout: float = 900.0) -> RunResult:
        return RunResult("", response_id, None, "completed")

    # --- files ---------------------------------------------------------------

    def upload_file(
        self, file: str | Path | bytes, *, filename: str | None = None, purpose: str = "user_data"
    ) -> dict:
        content = file if isinstance(file, bytes) else Path(file).read_bytes()
        fid = self._id("file")
        self.files[fid] = content
        self._log("upload_file", fid, filename=filename, purpose=purpose)
        return {"id": fid, "bytes": len(content), "purpose": purpose}

    # --- memory --------------------------------------------------------------

    def list_memories(self) -> list[dict]:
        return list(self.memories.values())

    def create_memory(
        self,
        name: str,
        *,
        parent_id: str | None = None,
        description: str | None = None,
        provider: str | None = None,
        restricted: bool | None = None,
    ) -> dict:
        mid = self._id("hmem")
        self.memories[mid] = {
            "id": mid,
            "name": name,
            "parent_id": parent_id,
            "description": description or "",
            "provider": provider or "reme",
            "restricted": bool(restricted),
        }
        self.records[mid] = {}
        return self.memories[mid]

    def _memory(self, memory_id: str) -> dict[str, dict]:
        if memory_id not in self.records:
            raise HRError("not_found", 404, f"no memory {memory_id}")
        return self.records[memory_id]

    def _subtree(self, memory_id: str) -> list[str]:
        out = [memory_id]
        for mid, m in self.memories.items():
            if m["parent_id"] == memory_id:
                out += self._subtree(mid)
        return out

    def write_record(self, memory_id: str, record: dict) -> dict:
        rid = self._id("hrec")
        rec = {"id": rid, "status": "active", "attributes": {}, "trust": "untrusted", **record}
        self._memory(memory_id)[rid] = rec
        return rec

    def get_record(self, memory_id: str, record_id: str) -> dict:
        try:
            return self._memory(memory_id)[record_id]
        except KeyError:
            raise HRError("not_found", 404, f"no record {record_id}") from None

    def list_records(self, memory_id: str, type: str | None = None) -> list[dict]:
        return [r for r in self._memory(memory_id).values() if type is None or r.get("type") == type]

    def patch_record(self, memory_id: str, record_id: str, patch: dict) -> dict:
        rec = self.get_record(memory_id, record_id)
        rec.update(patch)
        return rec

    def delete_record(self, memory_id: str, record_id: str) -> None:
        self.get_record(memory_id, record_id)
        del self._memory(memory_id)[record_id]

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
        """Searches the memory and its descendants. Filters are `{"attributes.<k>": v}` equality matches."""
        terms = [t for t in (query or text or "").lower().split() if t]
        results = []
        for mid in self._subtree(memory_id):
            for rec in self._memory(mid).values():
                if types and rec.get("type") not in types:
                    continue
                if filters and any(_get_path(rec, k) != v for k, v in filters.items()):
                    continue
                blob = json.dumps({k: rec.get(k) for k in ("title", "content", "attributes")}).lower()
                score = sum(t in blob for t in terms) / len(terms) if terms else 1.0
                if score > 0:
                    results.append(
                        {"record": rec, "memory": mid, "score": score, "why": "fake keyword match"}
                    )
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]

    # --- events --------------------------------------------------------------

    def events(self) -> Iterator[dict]:
        yield from self.event_frames


def _get_path(obj: dict, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj
