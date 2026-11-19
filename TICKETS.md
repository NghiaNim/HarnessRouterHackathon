# Ticket Board

> **Never edit a `Status:` line by hand.** Use the script. It claims atomically on `origin/main`, so two people
> can't grab the same ticket:
> ```bash
> python scripts/ticket.py list --open        # what's available
> python scripts/ticket.py claim T-02         # mark it yours BEFORE writing code
> python scripts/ticket.py review T-02 --pr 7 # when the PR is open
> python scripts/ticket.py done T-02 --pr 7   # after merge
> python scripts/ticket.py release T-02       # giving it up
> ```
> Status values: `OPEN` → `CLAIMED @you date` → `IN REVIEW @you PR #n` → `DONE @you PR #n`.
>
> **Picking order:** take the lowest-numbered OPEN ticket in your track whose `Depends on` are all DONE (or that
> you can build against the contract with a fake). Hold **at most 2** claimed tickets at a time.
> Read `docs/PRODUCT.md`, `docs/ARCHITECTURE.md` and `docs/harnessrouter-api.md` first.

**Tracks:** **A = Brain** (HarnessRouter client, memory store, background harnesses, core logic) ·
**B = Agent integration** (config, CLI, MCP, Claude Code hooks, guard) · **C = Surface** (API server, dashboard,
live feed, demo). Anyone can take any track. The tracks only exist to keep file ownership separate (see ARCHITECTURE.md).

**Sizes:** S ≈ < 1h · M ≈ 1–3h · L ≈ 3h+

---

## M0 · Foundation (unblocks everything, land first)

### T-00 · Scaffold, contracts, docs, ticket board
- **Status:** DONE @NghiaNim bootstrap
- **Track:** PM · **Size:** M · **Depends on:** —
- Package layout, `hive/models.py` + `hive/store/base.py` contracts, docs, this board, `scripts/ticket.py`.

### T-01 · Spike: choose memory provider + bootstrap the memory tree
- **Status:** OPEN
- **Track:** A · **Size:** M · **Depends on:** —
- **Files:** `scripts/spike_memory.py` (throwaway OK), `docs/harnessrouter-api.md`
- Create `hive/<project>` with children `decisions`, `handoffs`, `codebase` (`POST /v1/memories` with `parent_id`)
  for each candidate provider (`reme`, `graphiti`, `hindsight`, `mem0`). For each one: write a record with `title` + `attributes`,
  read it back, `recall` with `query` and with `filters` on `attributes.kind`, `PATCH` it, and check that root recall
  reaches the children.
- **Done when:** the chosen provider and the exact working request/response shapes are written into
  `docs/harnessrouter-api.md` (replace the "verify in T-01" note), and spike memories are deleted or clearly named.

### T-02 · HarnessRouter HTTP client
- **Status:** OPEN
- **Track:** A · **Size:** M · **Depends on:** —
- **Files:** `hive/hr/client.py`, `tests/test_hr_client.py`
- `HRClient(api_key, base_url)` on httpx: bearer auth, `Idempotency-Key` on POSTs, error envelope mapped to
  `HRError(code, status, message)`, retry with backoff on 429/5xx (never on `store_write_failed`).
- Methods: `bases()`, `models()`, `harnesses()`, `create_harness()`, `update_harness()`,
  `run(harness_id, input, *, stream=False, **opts) -> RunResult(text, response_id, session_id, status)`
  (SSE parser for `data:` frames, plus a fallback to poll `GET /v1/responses/{id}` if the stream drops),
  `upload_file()`, the memory methods (`create_memory`, `write_record`, `recall`, `list_records`,
  `patch_record`, `delete_record`), and `events()` (an SSE iterator).
- **Done when:** unit tests with `respx` cover auth, the error mapping, retry, and SSE parsing, plus one opt-in live test
  (`HIVE_LIVE=1`) that lists bases.

### T-03 · Harness provisioning + prompts
- **Status:** OPEN
- **Track:** A · **Size:** M · **Depends on:** T-02
- **Files:** `hive/hr/harnesses.py`, `prompts/{distiller,cartographer,guard,arbiter}.md`, `harnessrouter.json`
- `ensure_harnesses()` creates or updates the 4 harnesses **by name** (idempotent) with the bases/models/budgets
  from ARCHITECTURE.md, then writes the `{name: id}` map to `harnessrouter.json` (committed).
- Each prompt demands strict JSON that matches the target pydantic model (include the JSON schema in the prompt).
- Add a `run_json(harness, input, model_cls)` helper that strips code fences, validates, and retries once on a parse failure.
- **Done when:** running it twice creates no duplicate harnesses, and the JSON helper is tested with a fake client.

### T-04 · Store implementations (SQLite + HarnessRouter Memory)
- **Status:** OPEN
- **Track:** A · **Size:** L · **Depends on:** T-01, T-02 (SqliteStore depends on nothing, so do it first)
- **Files:** `hive/store/sqlite.py`, `hive/store/memory.py`, `hive/store/__init__.py` (`get_store()` factory
  from `HIVE_STORE`), `tests/test_store_contract.py`
- One shared **contract test suite**, parametrized over both stores (the memory store runs only with `HIVE_LIVE=1`).
- `SqliteStore.recall` can be a simple LIKE/keyword score. `HarnessMemoryStore` follows the mapping table in
  ARCHITECTURE.md and stores the model JSON in `attributes` so it round-trips exactly.
- **Done when:** both stores pass the same suite. **Ship SqliteStore early as its own PR**, because B and C build on it.

### T-05 · Config & identity
- **Status:** OPEN
- **Track:** B · **Size:** S · **Depends on:** —
- **Files:** `hive/config.py`, `tests/test_config.py`
- `Settings` loaded from `.env` (python-dotenv): key, project, store, api url.
  `current_member()`: `gh api user -q .login`, falling back to `git config user.name`, cached.
  `git_context()`: branch, HEAD sha, files changed vs `origin/main`.
- **Done when:** tests cover the fallbacks, and a missing key gives a clear error only when a HarnessRouter call is attempted.

### T-06 · CI: ruff + pytest on PRs
- **Status:** OPEN
- **Track:** B · **Size:** S · **Depends on:** —
- **Files:** `.github/workflows/ci.yml`
- Use uv: `uv sync`, `uv run ruff check .`, `uv run pytest`. Live tests are skipped (no secret in CI).
- **Done when:** a PR shows a green check.

---

## M1 · MVP loop (decision → brief → handoff, end to end)

### T-10 · Decision lifecycle service
- **Status:** OPEN
- **Track:** A · **Size:** M · **Depends on:** T-04 (Sqlite)
- **Files:** `hive/core/decisions.py`, `tests/test_decisions.py`
- `propose(...)`, `accept(id, by)`, `reject(id, by, reason)`, `supersede(old_id, new)`, `in_scope(paths)`.
- **Trust rule:** `accept`/`reject` refuse when `by` starts with `agent:`. Accepting a decision whose `supersedes` is
  set marks the old one `SUPERSEDED`.
- **Done when:** the state machine is fully unit-tested, including the agent-can't-accept rule.

### T-11 · Brief composer (the token saver)
- **Status:** OPEN
- **Track:** A · **Size:** M · **Depends on:** T-04 (Sqlite), T-05
- **Files:** `hive/core/brief.py`, `tests/test_brief.py`
- `compose_brief(member, branch, changed_paths, ticket=None, budget_tokens=2000) -> Brief`.
  Order: (1) accepted decisions in scope, (2) teammates' handoffs since my last session (newest first, skip my own),
  (3) module notes for the touched directories, (4) open proposed decisions, as a "pending" footnote.
  Estimate tokens as len/4 and truncate lowest-priority items first. Never drop accepted in-scope decisions.
- **Done when:** a golden-output test on fixture data passes and the budget is enforced.

### T-12 · CLI `hive`
- **Status:** OPEN
- **Track:** B · **Size:** M · **Depends on:** T-05, T-04 (Sqlite), T-10, T-11 (build against a fake until they land)
- **Files:** `hive/cli.py`, `tests/test_cli.py`
- Commands: `init` (memory tree + harnesses), `decide "<statement>" --title --scope --rationale` (propose),
  `accept/reject <id>`, `decisions [--status] [--path]`, `brief [--hook]`, `handoff [--hook|--summary]`,
  `recall "<query>"`, `map`, `check`.
- `--hook` modes read Claude Code hook JSON on stdin and print the format Claude Code expects.
- **Done when:** every command works against SqliteStore and is tested with `typer.testing.CliRunner`.

### T-13 · MCP server for agents
- **Status:** OPEN
- **Track:** B · **Size:** M · **Depends on:** T-10, T-11
- **Files:** `hive/mcp_server.py`, `tests/test_mcp.py`; `hive mcp` starts it on stdio
- Tools: `hive_brief`, `hive_recall`, `hive_decisions`, `hive_propose_decision`, `hive_publish_handoff`,
  `hive_check`. **There is no accept tool.** Tool descriptions tell the agent *when* to call each one (for example, "before editing files
  in an unfamiliar area, call hive_recall").
- **Done when:** it is registered in Claude Code via `.mcp.json` and an agent can list, propose, and recall in a live session.

### T-14 · Claude Code integration installer + hooks
- **Status:** OPEN
- **Track:** B · **Size:** M · **Depends on:** T-12
- **Files:** `hive/integrations/claude_code.py`, `.claude/settings.json`, `.mcp.json`
- `hive install` merges (doesn't clobber) the SessionStart hook (`hive brief --hook`), the SessionEnd hook
  (`hive handoff --hook`, which **detaches** and returns in < 1s), and the MCP server entry.
- **Done when:** a fresh Claude Code session in this repo shows the brief in context, and ending a session produces a
  handoff record within about 2 min.

### T-15 · Distiller harness: transcript → Handoff
- **Status:** OPEN
- **Track:** A · **Size:** M · **Depends on:** T-03, T-04
- **Files:** `hive/agents/distiller.py`, `tests/test_distiller.py`
- Read the Claude Code JSONL transcript and trim it (drop tool outputs over N chars and keep user/assistant text plus the edited file
  list), add `git diff --stat`, and run `hive-distiller` to get a `Handoff` plus `proposed_decisions[]`. Then save the handoff and
  call `decisions.propose()` with `proposed_by="agent:<member>"` for each proposed decision.
- **Done when:** a fixture transcript gives a valid Handoff (fake client in the test, one live run recorded in the PR).

### T-16 · API server (REST)
- **Status:** OPEN
- **Track:** C · **Size:** M · **Depends on:** T-04 (Sqlite), T-10
- **Files:** `hive/api/app.py`, `tests/test_api.py`; run with `uv run uvicorn hive.api.app:app --reload`
- `GET/POST /api/decisions`, `POST /api/decisions/{id}/accept|reject`, `GET /api/handoffs`,
  `GET /api/modules`, `GET /api/recall?q=`, `GET /api/brief?member=&branch=`. The server holds the HarnessRouter key,
  and the browser never sees it.
- **Done when:** it is tested with FastAPI TestClient on SqliteStore.

### T-17 · Dashboard: decisions board (human-in-the-loop)
- **Status:** OPEN
- **Track:** C · **Size:** M · **Depends on:** T-16
- **Files:** `hive/web/templates/*`, `hive/web/static/*`, routes in `hive/api/app.py`
- Columns: Proposed / Accepted / Superseded. Accept and reject buttons (HTMX), and a "proposed by agent:x" badge,
  scope chips, and a supersede link. Plus a form to add a decision directly as a human.
- **Done when:** someone can accept an agent-proposed decision in the UI and `hive decisions` shows it as accepted.

### T-18 · Dashboard: team timeline (handoffs)
- **Status:** OPEN
- **Track:** C · **Size:** S · **Depends on:** T-16
- A feed of handoffs per member and branch, with expandable learned/gotchas/open questions. Link to the HarnessRouter
  session share link when there is one.

---

## M2 · Smart features (the "wow")

### T-20 · Decision Guard: `hive check`
- **Status:** OPEN
- **Track:** B · **Size:** M · **Depends on:** T-03, T-10
- **Files:** `hive/agents/guard.py`, `tests/test_guard.py`
- Take the diff vs `origin/main`, pick the accepted decisions whose scope matches the changed files, and run `hive-guard`
  to get `Violation[]`. Exit non-zero if any violation is `block`. Usable as a pre-PR step and as an MCP tool.
- **Done when:** the demo scenario (`import requests` vs the httpx decision) is flagged with the decision id and file.

### T-21 · Cartographer: incremental codebase map
- **Status:** OPEN
- **Track:** A · **Size:** L · **Depends on:** T-03, T-04
- **Files:** `hive/agents/cartographer.py`, `tests/test_cartographer.py`
- `hive map`: find paths changed since the last mapped SHA (stored on the notes), upload them in batches, run
  `hive-cartographer` to get `ModuleNote[]`, and upsert them. Do a full map on the first run. Skip binaries, `.env*`, and lockfiles.
- **Done when:** a second run with no changes makes zero harness calls, and the brief includes notes for touched dirs.

### T-22 · Arbiter: duplicate & contradiction detection
- **Status:** OPEN
- **Track:** A · **Size:** M · **Depends on:** T-03, T-10
- **Files:** `hive/agents/arbiter.py`
- On `propose`, recall the top-5 similar decisions and run `hive-arbiter` to get an `ArbiterVerdict`. Store the verdict
  on the proposal (`tags`/`source`, or a contract PR to add a field) and show a warning in the dashboard.
- **Done when:** proposing "Use requests for HTTP" is flagged as contradicting the httpx decision.

### T-23 · Live activity feed (SSE fan-out)
- **Status:** OPEN
- **Track:** C · **Size:** M · **Depends on:** T-02, T-16
- **Files:** `hive/api/events.py`, dashboard widget
- The server opens **one** HarnessRouter `/v1/events` stream (respect `too_many_streams`), merges it with Hive events
  (decision proposed/accepted, handoff written), and pushes to browsers over SSE. Reconnect with backoff.
- **Done when:** the dashboard updates live while a teammate's agent finishes a session.

### T-24 · Tokens-saved meter
- **Status:** OPEN
- **Track:** C · **Size:** S · **Depends on:** T-11, T-16
- Estimate the baseline as the tokens of the files the module notes summarize, minus the brief tokens actually injected. Log each
  brief (`member`, `tokens`, `baseline`) and show the running total on the dashboard.
- **Done when:** the number is visible and the method is explained in a tooltip (be honest that it is an estimate).

### T-25 · Recall search + codebase map UI
- **Status:** OPEN
- **Track:** C · **Size:** S · **Depends on:** T-16, T-21
- A search box (calls `/api/recall`) with typed result cards, and a collapsible tree of module notes.

---

## M3 · Demo & polish

### T-30 · Demo scenario + seed script
- **Status:** OPEN
- **Track:** C · **Size:** M · **Depends on:** T-14, T-17, T-20
- **Files:** `scripts/seed_demo.py`, `docs/DEMO.md`
- A reproducible 3-minute script that follows the core loop in PRODUCT.md, with seed data and a reset command. Do a dry run twice.

### T-31 · README + 5-minute setup
- **Status:** OPEN
- **Track:** any · **Size:** S · **Depends on:** T-14
- Covers the pitch, an architecture image, `uv sync`, `.env`, `hive init`, `hive install`, and `uvicorn`. Have a teammate test it on a clean clone.

### T-32 · Pitch deck / 1-pager
- **Status:** OPEN
- **Track:** any · **Size:** S · **Depends on:** T-30
- Cover the problem, the loop, the HarnessRouter usage (Memory tree, 4 harnesses, Events), the tokens-saved number, and what's next.

---

## Stretch (only after M2 works end to end)

### T-40 · Slack/thread import → proposed decisions
- **Status:** OPEN
- **Track:** A · **Size:** M · **Depends on:** T-15
- `hive import-thread < thread.txt` runs the distiller in "extract decisions" mode and proposes them with `source` set.

### T-41 · Guard as a GitHub Action PR comment
- **Status:** OPEN
- **Track:** B · **Size:** M · **Depends on:** T-20, T-06
- Run `hive check` in CI with the key as a repo secret and post the violations as a PR comment.

### T-42 · "Resume teammate's session"
- **Status:** OPEN
- **Track:** B · **Size:** M · **Depends on:** T-15
- From a handoff with `hr_session_id`, branch that HarnessRouter session (`branch: true`) so you can continue a teammate's
  agent with its full context.

### T-43 · Private vs shared notes (per-person memory)
- **Status:** OPEN
- **Track:** A · **Size:** M · **Depends on:** T-04
- A per-member child memory with restricted grants, plus the `user` field on runs, for scratch notes that aren't shared.
