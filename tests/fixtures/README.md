# Test fixtures

| File | Fixture | What it's for |
|---|---|---|
| `transcript.jsonl` | `transcript_path` | Claude Code session JSONL. Contains **2 human corrections** ("use httpx, never requests"; "always set a timeout … never retry store_write_failed") and **1 agent opinion** ("cache /v1/bases"), which must not become a proposal. It also has a long tool output to trim and Edit/Write calls on `hive/hr/client.py` and `tests/test_hr_client.py`. Used by T-15. |
| `sample.diff` | `sample_diff` | Complies with "HTTP client is httpx". **Violates** "Browser never sees the HarnessRouter key" (`hive/web/static/app.js`). Used by T-20. |
| `seed_decisions.json` | `seed_decisions` | 3 accepted, 1 proposed (by `agent:alice`), and 1 rejected. Ids are fixed (`d000…001` through `d000…005`). |
| `seed_handoffs.json` | `seed_handoffs` | alice (T-02) and bob (T-12). |
| `seed_module_notes.json` | `seed_module_notes` | `hive/hr/client.py`, `hive/store/sqlite.py`. |

`seeded_store` loads all three seeds into an in-memory `SqliteStore`. `fake_hr` is a `tests.fakes.FakeHRClient`.
