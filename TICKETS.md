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
> **Picking order:** sections are in **priority order**, so work top to bottom. Take the first OPEN ticket in your
> track whose `Depends on` are all DONE (or that you can build against the contract with a fake). Hold **at most 2**
> claimed tickets at a time. **M1.5 (Differentiators) beats M2.** It's what makes Hive more than "shared memory".
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
- **Status:** CLAIMED @NghiaNim 2026-11-18
- **Track:** A · **Size:** M · **Depends on:** —
- **Files:** `scripts/spike_memory.py` (throwaway OK), `docs/harnessrouter-api.md`
- Create `hive/<project>` with children `decisions`, `handoffs`, `codebase` (`POST /v1/memories` with `parent_id`)
  for each candidate provider (`reme`, `graphiti`, `hindsight`, `mem0`). For each one: write a record with `title` + `attributes`,
  read it back, `recall` with `query` and with `filters` on `attributes.kind`, `PATCH` it, and check that root recall
  reaches the children.
- **Done when:** the chosen provider and the exact working request/response shapes are written into
  `docs/harnessrouter-api.md` (replace the "verify in T-01" note), and spike memories are deleted or clearly named.

### T-02 · HarnessRouter HTTP client
- **Status:** IN REVIEW @NghiaNim PR #10
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
- **Status:** IN REVIEW @NghiaNim PR #14
- **Track:** A · **Size:** M · **Depends on:** T-02
- **Files:** `hive/hr/harnesses.py`, `prompts/{distiller,guard,arbiter,oracle}.md`, `harnessrouter.json`
- `ensure_harnesses()` creates or updates the harnesses (distiller, guard, arbiter, oracle; cartographer only if T-21 is picked up) **by name** (idempotent) with the bases/models/budgets
  from ARCHITECTURE.md, then writes the `{name: id}` map to `harnessrouter.json` (committed).
- Each prompt demands strict JSON that matches the target pydantic model (include the JSON schema in the prompt).
- Add a `run_json(harness, input, model_cls)` helper that strips code fences, validates, and retries once on a parse failure.
- **Done when:** running it twice creates no duplicate harnesses, and the JSON helper is tested with a fake client.

### T-04 · SqliteStore + `get_store()` factory + shared store contract tests
- **Status:** DONE @NghiaNim PR #8
- **Track:** A · **Size:** M · **Depends on:** —
- **Files:** `hive/store/sqlite.py`, `hive/store/__init__.py` (`get_store()` factory from `HIVE_STORE`),
  `tests/test_store_contract.py`
- A **contract test suite** that any `ContextStore` must pass, parametrized so T-09 can plug the memory store in.
  `SqliteStore.recall` can be a simple LIKE/keyword score.
- **Done when:** SqliteStore passes the suite. B and C build on it.

### T-09 · HarnessMemoryStore (HarnessRouter Memory API)
- **Status:** OPEN
- **Track:** A · **Size:** L · **Depends on:** T-01, T-02, T-04
- **Files:** `hive/store/memory.py`, register it in `hive/store/__init__.py`, plus a param in `tests/test_store_contract.py`
- Follow the mapping table in ARCHITECTURE.md. Store the model JSON in `attributes` so it round-trips exactly. Use the
  `decisions`/`handoffs`/`intents`/`codebase` children under the project root memory, with the provider chosen in T-01.
- **Done when:** it passes the same contract suite with `HIVE_LIVE=1`, and `HIVE_STORE=memory` works end to end for `hive decisions`.

### T-05 · Config & identity
- **Status:** IN REVIEW @Odysseyc PR #9
- **Track:** B · **Size:** S · **Depends on:** —
- **Files:** `hive/config.py`, `tests/test_config.py`
- `Settings` loaded from `.env` (python-dotenv): key, project, store, api url.
  `current_member()`: `gh api user -q .login`, falling back to `git config user.name`, cached.
  `git_context()`: branch, HEAD sha, files changed vs `origin/main`.
- **Done when:** tests cover the fallbacks, and a missing key gives a clear error only when a HarnessRouter call is attempted.

### T-06 · CI: ruff + pytest on PRs
- **Status:** DONE @Odysseyc PR #7
- **Track:** B · **Size:** S · **Depends on:** —
- **Files:** `.github/workflows/ci.yml`
- Use uv: `uv sync`, `uv run ruff check .`, `uv run pytest`. Live tests are skipped (no secret in CI).
- **Done when:** a PR shows a green check.

### T-07 · Test fakes & fixtures (unblocks B and C without live calls)
- **Status:** IN REVIEW @NghiaNim PR #11
- **Track:** A · **Size:** S · **Depends on:** — (match T-02's method names; coordinate in the PR)
- **Files:** `tests/fakes.py`, `tests/conftest.py`, `tests/fixtures/` (sample transcript JSONL, sample diff, seed decisions)
- `FakeHRClient` with canned `run()` outputs keyed by harness name, and an in-memory `ContextStore` fixture
  (`store` fixture) that defaults to SqliteStore on `:memory:` once T-04 lands.
- **Done when:** T-10/T-11/T-15/T-16 can be tested with no network and no key.

### T-08 · Contract addition: `HiveEvent`, `BriefLog`, `Intent`, evidence fields
- **Status:** IN REVIEW @NghiaNim PR #12
- **Track:** any · **Size:** S · **Depends on:** — (contract PR, so all other active members review) · **Blocks the M1.5 differentiators, so do it soon**
- **Files:** `hive/models.py`, `hive/store/base.py`, `tests/test_contract.py`
- `HiveEvent(id, type: decision_proposed|decision_accepted|decision_rejected|handoff_written|brief_served|check_run|
  collision_warned|question_answered, actor, ref_id, at, summary)` for the live feed (T-23) and impact counters.
- `BriefLog(member, branch, tokens, baseline_tokens, at)` for the tokens-saved meter (T-24).
- `Intent(member, branch, ticket: str|None, summary, paths: list[str], started_at, heartbeat_at, ended_at: datetime|None)`
  is "what my agent is working on right now" (T-50/T-51). It's keyed by `(member, branch)`.
- New optional fields: `Decision.evidence: str|None` (the verbatim human quote it was mined from, T-15) and
  `Handoff.transcript_file_id: str|None` (the HarnessRouter file id of the trimmed transcript, used by `hive ask`, T-53).
- New store methods: `log_event`, `list_events(since, limit)`, `put_intent` (upsert), and
  `list_intents(active_within_minutes=15)`. These are additive only.
- **Done when:** merged with approval from all other active members, and the stores (T-04/T-09/T-52) know they need to implement it.

---

## M1 · MVP loop (decision → brief → handoff, end to end)

### T-10 · Decision lifecycle service
- **Status:** CLAIMED @Odysseyc 2026-11-18
- **Track:** A · **Size:** M · **Depends on:** T-04 (Sqlite)
- **Files:** `hive/core/decisions.py`, `tests/test_decisions.py`
- `propose(...)`, `accept(id, by)`, `reject(id, by, reason)`, `supersede(old_id, new)`, `in_scope(paths)`.
- **Trust rule:** `accept`/`reject` refuse when `by` starts with `agent:`. Accepting a decision whose `supersedes` is
  set marks the old one `SUPERSEDED`.
- **Done when:** the state machine is fully unit-tested, including the agent-can't-accept rule.

### T-11 · Brief composer (the token saver)
- **Status:** IN REVIEW @NghiaNim PR #13
- **Track:** A · **Size:** M · **Depends on:** T-04 (Sqlite), T-05
- **Files:** `hive/core/brief.py`, `tests/test_brief.py`
- `compose_brief(member, branch, changed_paths, ticket=None, budget_tokens=2000) -> Brief`.
  Order: (0) "who's active where": teammates' live intents (once T-52 lands), (1) accepted decisions in scope, (2) teammates' handoffs since my last session (newest first, skip my own),
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
- **Correction mining (differentiator #1):** the prompt specifically hunts for **human corrections and directives** in
  user turns ("no, use httpx", "never call HR from the browser", "always add a timeout"). Each one becomes a proposed decision
  with `evidence` = the verbatim user quote and `scope` inferred from the files being edited at that moment. Agent opinions
  are **not** proposals. Only human words are.
- Also upload the trimmed transcript (`POST /v1/files`) and set `Handoff.transcript_file_id` (for `hive ask`, T-53).
- **Done when:** a fixture transcript with 2 human corrections and 1 agent opinion gives exactly 2 proposed decisions with
  correct evidence quotes (fake client in the test, one live run recorded in the PR).

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

### T-19 · Hook resilience: fail open, fast, cached
- **Status:** OPEN
- **Track:** B · **Size:** S · **Depends on:** T-14
- **Files:** `hive/integrations/claude_code.py`, `hive/cli.py` (hook paths only)
- A Hive failure must **never** break or slow a teammate's Claude Code session. `brief --hook` has a hard 3s timeout,
  falls back to the last cached brief in `~/.cache/hive/<project>/brief.md`, and if that fails it prints nothing and exits 0.
  `handoff --hook` writes the transcript path to a local queue file before detaching, and `hive handoff --flush` retries queued ones.
- **Done when:** tests prove the timeout and fallback paths, and running with no network or no key still exits 0 quickly.

---

## M1.5 · Differentiators (priority over M2, this is the pitch)

> Shared memory alone isn't differentiated. These three pillars are: **(1) corrections become enforced team rules**
> (T-15 mining + T-20 guard), **(2) live collision radar between concurrent agents** (T-50…T-52, T-55), and
> **(3) ask a teammate's agent instead of Slack** (T-53). Plus **(4) context that knows when it's stale** (T-54).
> See `docs/PRODUCT.md` → Positioning.

### T-50 · Intent broadcasting from agent sessions
- **Status:** OPEN
- **Track:** B · **Size:** M · **Depends on:** T-08, T-12, T-52 (build against SqliteStore first)
- **Files:** `hive/core/intents.py`, `hive/cli.py` (`intent` subcommands), `hive/integrations/claude_code.py`
- SessionStart publishes an `Intent` (member, branch, the ticket parsed from the branch name or from your CLAIMED tickets in
  TICKETS.md, and a summary from the first user prompt once available). A **PostToolUse** hook on `Edit|Write|MultiEdit` adds the path
  and bumps `heartbeat_at`, debounced locally to ≤ 1 store write per 30s. SessionEnd sets `ended_at`.
- **Done when:** two local sessions on different branches show up in `hive who` with correct paths and tickets.

### T-51 · Collision warnings before an agent edits
- **Status:** OPEN
- **Track:** B · **Size:** M · **Depends on:** T-50, T-19
- **Files:** `hive/core/intents.py`, hook entry in `hive/integrations/claude_code.py`
- A **PreToolUse** hook on `Edit|Write|MultiEdit`: if the target path (or its directory) is in another member's active
  intent (heartbeat < 15 min), return non-blocking `additionalContext`, for example
  *"⚠ @bob's agent is editing `hive/api/app.py` for T-16 (2 min ago). Avoid overlapping changes, or run `hive ask @bob ...`."*
  It must answer in **< 300 ms**, from a local intents cache refreshed in the background, and fail open. Log a `collision_warned` event.
  Also warn once at SessionStart if *my* changed files overlap someone's active intent.
- **Done when:** a scripted two-session test produces the warning, and the hook's p95 is < 300 ms with the network off.

### T-52 · Intent storage in both stores
- **Status:** OPEN
- **Track:** A · **Size:** S · **Depends on:** T-08, T-04, T-09
- **Files:** `hive/store/sqlite.py`, `hive/store/memory.py`
- **SqliteStore half already ships in PR #12 (T-08)**, so this ticket is the HarnessMemoryStore half only (do it with or after T-09).
- `put_intent` upserts by `(member, branch)`. In the memory store, use an `intents` child memory with one record per member+branch
  that is PATCHed, not appended (keep memory ops low). `list_intents(active_within_minutes)` filters by heartbeat.
- **Done when:** the contract test suite covers intents for both stores.

### T-53 · `hive ask @member` — ask a teammate's agent
- **Status:** OPEN
- **Track:** A · **Size:** M · **Depends on:** T-15, T-03
- **Files:** `hive/agents/oracle.py`, `prompts/oracle.md`, CLI `ask` command, MCP tool `hive_ask`
- Collect @member's recent handoffs (scored against the question) and attach their transcript files (`input_file` blocks
  with `transcript_file_id`), then run the `hive-oracle` harness to get an answer **with citations** (handoff id or transcript quote).
  If the evidence doesn't answer the question, the oracle must say "not in Bob's context". No guessing. Follow-ups continue the same
  HarnessRouter session (`previous_response_id`). Log a `question_answered` event that the member can see on the dashboard.
- **Done when:** a fixture handoff and transcript answer "why did you change X?" with a citation, and an unrelated question gets
  "not in context".

### T-54 · Staleness: context that knows it's out of date
- **Status:** OPEN
- **Track:** A · **Size:** S · **Depends on:** T-11
- **Files:** `hive/core/freshness.py`, used by `core/brief.py` and recall
- For handoffs, run `git diff --name-only <commit_sha>..HEAD` over `files_touched` and tag the result *fresh*, *partly stale (k/n files changed)* or
  *stale*. The brief drops stale handoffs first and marks partly stale ones. For decisions, flag in-scope globs that match **no files**
  (dead rules) on the dashboard.
- **Done when:** a test repo fixture shows each freshness state, and the brief output marks them.

### T-55 · Radar: who's working where (dashboard)
- **Status:** OPEN
- **Track:** C · **Size:** M · **Depends on:** T-16, T-52 (poll first, then live via T-23)
- **Files:** `hive/web/templates/radar.html`, route in `hive/api/app.py` (`GET /api/intents`)
- A directory tree with each active member's paths highlighted in their color. Overlaps (collisions) are red, and each one links to
  the ticket and the last heartbeat. This is **the opening shot of the demo**, so make it legible on a projector.
- **Done when:** two live sessions show up on the radar, and an overlap turns red within about 30s.

---

## M2 · Smart features (the "wow")

### T-20 · Decision Guard: `hive check`
- **Status:** OPEN
- **Track:** B · **Size:** M · **Depends on:** T-03, T-10
- **Files:** `hive/agents/guard.py`, `tests/test_guard.py`
- Take the diff vs `origin/main`, pick the accepted decisions whose scope matches the changed files, and run `hive-guard`
  to get `Violation[]`. Exit non-zero if any violation is `block`. Usable as a pre-PR step and as an MCP tool.
- **Done when:** the demo scenario (`import requests` vs the httpx decision) is flagged with the decision id and file.

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
- Estimate the baseline as the tokens of the files referenced by the injected handoffs and decision scopes (the files the agent would have
  re-read), minus the brief tokens actually injected. Log each brief (`member`, `tokens`, `baseline`) and show the running total on the dashboard.
- Next to it, add an **impact strip** counted from `HiveEvent`: rules mined from corrections · violations caught · collisions
  warned · questions answered without Slack. These four numbers are the pitch.
- **Done when:** the numbers are visible and the method is explained in a tooltip (be honest that tokens saved is an estimate).

### T-25 · Recall search + codebase map UI
- **Status:** OPEN
- **Track:** C · **Size:** S · **Depends on:** T-16, T-21
- A search box (calls `/api/recall`) with typed result cards, and a collapsible tree of module notes.

### T-26 · Cost guardrail for HarnessRouter spend
- **Status:** OPEN
- **Track:** A · **Size:** S · **Depends on:** T-02
- **Files:** `hive/hr/client.py` (hook point), `hive/hr/budget.py`
- Track the cost of each `run()` (from the response usage/cost if present, otherwise the harness `max_cost_usd` as an upper bound) in
  `~/.cache/hive/spend.jsonl`. Enforce `HIVE_DAILY_BUDGET_USD` (default 5) by refusing new runs with a clear message.
  `hive spend` prints today's total per harness.
- **Done when:** exceeding the budget blocks runs in a test, and the dashboard can read the total (expose it via `/api/spend` in a follow-up).

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

### T-33 · Dogfood: run Hive on this repo for all 3 members
- **Status:** OPEN
- **Track:** any · **Size:** S · **Depends on:** T-14, T-09, T-19
- Commit the `hive install` output for this repo and have every member's Claude Code use it for the rest of the
  hackathon. Seed the real decisions we've already made (the stack table in ARCHITECTURE.md, "humans decide, agents
  propose", contract rule) as accepted decisions. Collect 2–3 real "it caught/saved us" moments for the pitch in `docs/DEMO.md`.
- **Done when:** all 3 members have had at least one session start with a brief and end with a handoff.

### T-34 · Shared dashboard deployment
- **Status:** OPEN
- **Track:** C · **Size:** S · **Depends on:** T-16, T-17
- Make one URL the whole team (and judges) can open: run the server on one machine and expose it with a tunnel, or
  deploy a container. The key lives only in the server env. Document it in the README.
- **Done when:** a teammate on another network can accept a decision through the shared URL.

---

## Stretch (only after M2 works end to end)

### T-21 · Cartographer: incremental codebase map (deprioritized)
- **Status:** OPEN
- **Track:** A · **Size:** L · **Depends on:** T-03, T-04
- **Why it's stretch:** codebase indexing is commodity (Cursor, Augment, Sourcegraph) and costs a lot of harness time. Handoffs'
  `learned` lists already give us cheap, human-relevant codebase knowledge. Only pick this up if M1.5 and M2 are done.
- `hive map`: find paths changed since the last mapped SHA, upload them in batches, run `hive-cartographer` to get `ModuleNote[]`, and upsert them.
- **Done when:** a second run with no changes makes zero harness calls.

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

### T-44 · Non-Claude agents: AGENTS.md brief export
- **Status:** OPEN
- **Track:** B · **Size:** S · **Depends on:** T-11, T-12
- `hive brief --write-agents-md` writes or refreshes a marked `<!-- hive:brief -->` section in `AGENTS.md`, so Codex
  and other harnesses get the same decisions with no hooks. This shows Hive is harness-agnostic, which ties into the HarnessRouter pitch.

### T-45 · Decision history & provenance view
- **Status:** OPEN
- **Track:** C · **Size:** S · **Depends on:** T-17, T-09
- On the decision detail page, show the version history (`GET /v1/memories/{id}/records/{rid}/history`), who proposed and who
  accepted it, the supersede chain, and the source (session/PR/thread).
