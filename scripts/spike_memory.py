"""T-01 spike: which HarnessRouter memory provider round-trips Hive records? (throwaway)

For each provider: build `hive-spike-<provider>` with children decisions/handoffs/codebase, write a record
with title + attributes, read it back, recall by query and by attributes.kind filter, PATCH it, and recall
from the root.
Prints one compact line per step; the full log goes to the path given as argv[1] (default: spike_memory.log).

Costs ~10 memory ops per provider (~$0.002 each). Usage:
    uv run python scripts/spike_memory.py [log_path] [provider ...]
"""
# ruff: noqa: B023  (each lambda is invoked immediately by step())

from __future__ import annotations

import json
import sys
import time

from dotenv import load_dotenv

from hive.hr.client import HRClient, HRError

PROVIDERS = ["reme", "graphiti", "hindsight", "mem0"]


def main() -> None:
    load_dotenv()
    log_path = sys.argv[1] if len(sys.argv) > 1 else "spike_memory.log"
    providers = sys.argv[2:] or PROVIDERS
    log = open(log_path, "a")

    def step(provider: str, name: str, fn):
        t = time.monotonic()
        try:
            out = fn()
            ok = "OK "
        except HRError as e:
            out, ok = {"error": e.code, "status": e.status, "message": e.message, "detail": e.detail}, "ERR"
        dt = time.monotonic() - t
        text = json.dumps(out, default=str)
        print(f"[{provider:9}] {ok} {name:22} {dt:5.1f}s  {text[:220]}")
        log.write(f"### {provider} {name} ({dt:.1f}s)\n{json.dumps(out, indent=2, default=str)}\n\n")
        return out

    with HRClient.from_env(timeout=120) as c:
        for p in providers:
            root = step(
                p,
                "create root",
                lambda p=p: c.create_memory(
                    f"hive-spike-{p}", provider=p, description="T-01 spike, safe to delete"
                ),
            )
            if "error" in root:
                continue
            kids = {}
            for child in ("decisions", "handoffs", "codebase"):
                kids[child] = step(
                    p,
                    f"create {child}",
                    lambda child=child, p=p: c.create_memory(child, parent_id=root["id"], provider=p),
                )
            dec = kids["decisions"].get("id")
            if not dec:
                continue
            record = {
                "type": None,  # filled below
                "title": "HTTP client is httpx",
                "content": [
                    {"type": "text", "text": "Use httpx with a timeout; never requests. Why: sync+async."}
                ],
                "attributes": {
                    "kind": "decision",
                    "status": "proposed",
                    "scope": ["hive/hr/**"],
                    "hive_id": "spike0001",
                    "proposed_by": "NghiaNim",
                },
            }
            types = {"reme": "note", "graphiti": "fact", "hindsight": "fact", "mem0": "fact"}
            record["type"] = types[p]
            w = step(p, "write record", lambda: c.write_record(dec, record))
            rid = w.get("id") if isinstance(w, dict) else None
            if rid:
                step(p, "get record", lambda: c.get_record(dec, rid))
            step(p, "list records", lambda: c.list_records(dec))
            step(
                p, "recall query (child)", lambda: c.recall(dec, "which http library should we use", limit=3)
            )
            step(
                p,
                "recall filter kind",
                lambda: c.recall(dec, "http", filters={"attributes.kind": "decision"}, limit=3),
            )
            step(p, "recall text", lambda: c.recall(dec, text="httpx", limit=3))
            if rid:
                step(
                    p,
                    "patch record",
                    lambda: c.patch_record(
                        dec, rid, {"attributes": {**record["attributes"], "status": "accepted"}}
                    ),
                )
                step(p, "get after patch", lambda: c.get_record(dec, rid))
            step(p, "recall root", lambda: c.recall(root["id"], "http library", limit=3))
    log.close()


if __name__ == "__main__":
    main()
