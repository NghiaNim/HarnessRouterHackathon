# Architecture

## System diagram

```
 ┌──────────── each teammate's machine ────────────┐
 │  Claude Code / Codex                             │
 │    ├─ SessionStart hook ── `hive brief` ─────┐   │
 │    ├─ SessionEnd hook ──── `hive handoff` ───┤   │
 │    └─ MCP tools ────────── `hive mcp` ───────┤   │
 │                                              ▼   │
 │                     hive core library (Python)   │
 └──────────────────────────────┬───────────────────┘
                                │ HTTPS (workspace key from .env)
 ┌─────────── Hive API server (FastAPI) ───┐  │
 │  REST + dashboard (Jinja + HTMX + SSE)  │  │
 │  fans out one /v1/events stream         │  │
 └──────────────────┬──────────────────────┘  │
                    ▼                         ▼
 ┌──────────────────── HarnessRouter (api.harnessrouter.ai) ─────────────────────┐
 │  Memory tree:  hive/<project>                                                  │
 │                  ├─ decisions     (Decision records)                           │
 │                  ├─ handoffs      (Handoff records)                            │
 │                  ├─ intents       (Intent records, PATCHed per member+branch)  │
 │                  └─ codebase      (ModuleNote records, stretch)                │
 │  Harnesses:    hive-distiller · hive-guard · hive-arbiter · hive-oracle        │
 │  Events:       /v1/events (task started/ended)                                 │
 └────────────────────────────────────────────────────────────────────────────────┘
```

Every teammate uses the **same HarnessRouter workspace key**, so they all see the same memory tree. The CLI/MCP
use the core library directly. The API server is needed only for the dashboard and the event fan-out.

## Tech stack (decided, don't relitigate)

| Concern | Choice |
|---|---|
| Language | Python ≥ 3.11 |
| Data models | pydantic v2 (`hive/models.py` is **the contract**) |
| HTTP client | `httpx` (sync for the CLI, async allowed in the server) |
| CLI | `typer` (entry point `hive`) |
| MCP server | official `mcp` Python SDK (`FastMCP`), stdio transport |
| API server | FastAPI + uvicorn |
| Dashboard | Jinja2 templates + HTMX + SSE. **No Node build step.** |
| Local fallback store | SQLite (stdlib `sqlite3`) |
| Tests / lint | pytest, ruff |
| Package manager | `uv` (`uv sync`, `uv run ...`) |

## Package layout & ownership

Each module belongs to a **track**. Stay in your ticket's files to avoid merge conflicts.

```
hive/
  models.py              CONTRACT  Decision, Handoff, ModuleNote, Brief, Violation, RecallHit   (all, via contract PR)
  store/base.py          CONTRACT  ContextStore protocol                                         (all, via contract PR)
  config.py              B  settings (.env), identity (gh login → git user), project id          T-05
  store/sqlite.py        A  SqliteStore: local/offline + tests                                    T-04
  store/memory.py        A  HarnessMemoryStore: HarnessRouter Memory API                          T-04
  hr/client.py           A  HarnessRouter HTTP client: auth, retries, errors, SSE, memory calls   T-02
  hr/harnesses.py        A  idempotent provisioning of our harnesses → harnessrouter.json        T-03
  agents/distiller.py    A  transcript/diff → Handoff (+ proposed decisions)                      T-15
  agents/oracle.py       A  `hive ask @member`: answers from a teammate's handoffs + transcript     T-53
  agents/cartographer.py A  (stretch) repo → ModuleNotes, incremental by git SHA                  T-21
  core/intents.py        B  intent broadcast + collision check (hot path, < 300 ms, fail open)     T-50, T-51
  core/freshness.py      A  stale/partly-stale tagging vs git                                     T-54
  agents/arbiter.py      A  new decision vs existing → duplicate / contradicts                     T-22
  agents/guard.py        B  diff vs accepted decisions → Violations                                T-20
  core/decisions.py      A  lifecycle: propose → accept/reject/supersede (humans only accept)      T-10
  core/brief.py          A  token-budgeted brief composer                                          T-11
  cli.py                 B  `hive` CLI                                                             T-12
  mcp_server.py          B  MCP tools for agents                                                   T-13
  integrations/claude_code.py B  hooks + .mcp.json installer                                       T-14, T-19
  api/app.py             C  FastAPI REST                                                           T-16
  api/events.py          C  SSE fan-out (HR /v1/events + Hive events)                              T-23
  web/templates/, web/static/  C  dashboard                                                        T-17, T-18, T-24, T-55
prompts/                 A  system prompts for each harness (markdown)
harnessrouter.json       A  generated harness ids (committed; no secrets)
tests/                   all
```

**Contract rule:** `hive/models.py` and `hive/store/base.py` are shared interfaces. Changing them needs its own
small PR that **both** other members review. Additive changes (new optional field) are fine. Don't rename or remove anything.

## Memory mapping (HarnessRouter records ⇄ Hive models)

| Hive model | Memory child | Record `title` | Record `content` | Record `attributes` |
|---|---|---|---|---|
| Decision | `decisions` | `decision.title` | `statement` + `rationale` | `kind=decision, status, scope, proposed_by, decided_by, supersedes, tags, hive_id` |
| Handoff | `handoffs` | `"{member}@{branch}: {summary[:80]}"` | full markdown render | `kind=handoff, member, branch, ticket, files_touched, commit_sha, hr_session_id, hive_id` |
| ModuleNote | `codebase` | `path` | summary + key symbols | `kind=module, path, commit_sha, depends_on, hive_id` |

- `hive_id` is our own stable id (a uuid4 hex), so models round-trip regardless of provider record ids.
- A superseded decision gets a `references` entry `{rel: "contradicts"|"derived_from"}` to its replacement and `status=superseded`.
- Recall on the **root** searches all three children. Recall on a child is scoped to that kind.

## Background harnesses (HarnessRouter Responses API)

| Harness | Base / model | Input | Output (JSON, validated into models) |
|---|---|---|---|
| `hive-distiller` | claude-code / claude-haiku-5.5 | session transcript (trimmed) + git diff stat | `Handoff` + `proposed_decisions[]` |
| `hive-oracle` | claude-code / claude-sonnet-5.5 | question + teammate's handoffs + transcript files | answer with citations, or "not in context" |
| `hive-cartographer` *(stretch)* | claude-code / claude-sonnet-5.5 | files changed since the last mapped SHA (uploaded) | `ModuleNote[]` |
| `hive-guard` | claude-code / claude-haiku-5.5 | unified diff + accepted decisions in scope | `Violation[]` |
| `hive-arbiter` | claude-code / claude-haiku-5.5 | proposed decision + top-k similar decisions | `{duplicate_of?, contradicts[], note}` |

All of them return **strict JSON** (the prompt says so, and the client strips code fences and validates with pydantic, retrying once on a parse
failure). Budgets: `max_cost_usd ≤ 1`, `timeout_seconds ≤ 600`. Free plan = 2 concurrent tasks, so session-end
distillation runs in the background and never blocks the user's agent.

## Agent integration (Claude Code)

- **SessionStart hook** → `hive brief --hook`: reads hook JSON on stdin (cwd, session id), computes branch +
  changed paths, and prints a brief (≤ 2,000 tokens) as `additionalContext`.
- **SessionEnd hook** → `hive handoff --hook`: reads `transcript_path`, spawns the distiller **detached**, and returns at once.
- **PostToolUse hook** (`Edit|Write|MultiEdit`) → `hive intent touch <path>`: adds the path to my intent and bumps the heartbeat (debounced).
- **PreToolUse hook** (`Edit|Write|MultiEdit`) → `hive intent check <path>`: warns (never blocks) if a teammate's live intent
  covers the path. It answers from a local cache in < 300 ms.
- **MCP tools** (`hive mcp`): `hive_brief`, `hive_recall`, `hive_decisions`, `hive_propose_decision`,
  `hive_publish_handoff`, `hive_check`, `hive_ask`, `hive_who`. There is **no accept tool**, by design.
- `hive install` writes the hooks to `.claude/settings.json` and the server to `.mcp.json`. Both are project-scoped and committed.

## Configuration

`.env` (gitignored; copy `.env.example`):
```
HARNESS_ROUTER_KEY=sk-hr-...        # shared workspace key, get it from the team lead
HIVE_PROJECT=harness-router-hackathon
HIVE_STORE=memory                   # memory | sqlite
HIVE_API_URL=http://localhost:8000  # dashboard/server
```
