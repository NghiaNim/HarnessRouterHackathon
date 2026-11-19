# HarnessRouter API — verified team reference

> **Read this instead of re-researching HarnessRouter.** It was compiled from the docs at
> https://harnessrouter.ai/docs and **verified against the live API on 2026-11-18** with our workspace key.
> If you discover something new or wrong, fix this file in your PR.

## What HarnessRouter is

It is **not** a model router. It runs complete **agent harnesses** (Claude Code, Codex, Hermes, Gemini CLI, …: the
runtime loop with tools, a working directory, and session state) behind one API that follows the
**OpenAI Responses API** shape. It also offers **Harness Memory**, a long-term memory store shared across
harnesses and sessions (protocol: Unified Harness Protocol, https://unifiedharnessprotocol.org).

We use it for two things:
1. **Memory API**: the shared store for decisions, handoffs, and module notes.
2. **Responses API**: runs our background harnesses (Distiller, Cartographer, Guard, Arbiter).

## Basics

- Base URL: `https://api.harnessrouter.ai`
- Auth: `Authorization: Bearer $HARNESS_ROUTER_KEY` (workspace key, prefix `sk-hr-`, lives in `.env`, **never commit**)
- Send `Idempotency-Key: <uuid4>` on every task-creating POST. Reuse it only when retrying the same request.
- Error envelope: `{"error": {"type", "code", "message", "param", "detail"}}`. **Match on `code`.**
  Codes: `invalid_input`(400), `invalid_credential`(401), `payment_required`(402), **`no_key`(402, verified live: the workspace has no model
  provider key; the free trial covers agent work and memory, but model usage needs BYOK in the console)**, `insufficient_scope`(403),
  `not_found`(404), `conflict`(409), `session_busy`(409), `file_too_large`(413), `invalid_request`(422),
  `rate_limited`/`too_many_streams`(429), `internal_error|upstream_error|unavailable|timeout`(5xx).
  A 502 `store_write_failed` may mean the work ran, so recover the session and **don't resubmit**.

## Discovery (verified ✅)

| Call | Notes |
|---|---|
| `GET /v1/bases` | Base harness ids + default model + tool list. Verified ids include `claude-code` (default `claude-sonnet-5.5`), `codex`, `hermes`, `gemini`, `opencode`, `pi`, `qwen`, `kimi`, `goose`, `cline`, … |
| `GET /v1/models` | Catalog keyed by backend. `claude` backend: `claude-sonnet-5.5` (default), `claude-opus-5.5`, `claude-haiku-5.5`, `claude-fable-5-2`, … |
| `GET /v1/harnesses` | Configured agents in our workspace. |
| `GET /v1/harnesses/{id}/models` | Models allowed for one harness. |

**Response envelopes differ per endpoint (verified ✅ 2026-11-18, T-02).** `HRClient` unwraps them for you:
- `GET /v1/bases` → `{"bases": [...], "runtimeDefaults": {...}, "capabilities": {...}}`
- `GET /v1/models` → `{"backends": {"<backend>": {"default": "<model>", "models": [...]}}}`
- `GET /v1/harnesses` → `{"harnesses": [...]}`. Fields come back **camelCase** (`defaultModel`, `systemPrompt`, …).
- `GET /v1/memories`, `GET /v1/memories/providers` → `{"object": "list", "data": [...]}`

## Harnesses (configured agents)

`POST /v1/harnesses`, `GET/PUT/DELETE /v1/harnesses/{id}`
```json
{"name": "hive-distiller", "base": "claude-code", "default_model": "claude-haiku-5.5",
 "system_prompt": "...", "mcp_servers": [], "skills": [],
 "max_step": 40, "timeout_seconds": 600, "max_cost_usd": 1.0}
```
Required: `name`, `base`. Optional: `default_model`, `system_prompt`, `mcp_servers`, `skills`, `plugins`,
`disabled_tools`, `env`, `max_step`, `timeout_seconds`, `max_cost_usd`. Defaults are 400 steps, 7200 s, and $10. **Always set
tight budgets on our harnesses.** Harness ids look like `chrn_<hex>`.

Memory config per harness ✅: `GET/PUT /v1/harnesses/{id}/memories` returns `{default_memory_id, observe, data:[...]}`.

## Running a task (Responses API)

Both forms are routed by the live API ✅ (both returned `400 invalid_input: empty input` to an empty body):
```
POST /{harness_id}/v1/responses                                  # path form (preferred, used by most docs)
POST /v1/responses   body.metadata.harness_id = "<harness_id>"     # metadata form
```
Body:
```json
{"input": "text or [{role, content}] or content blocks",
 "stream": true,            // SSE; or "background": true then poll
 "reasoning": {"effort": "low"},
 "max_cost_usd": 0.5, "timeout_seconds": 600, "max_step": 40,
 "metadata": {"any": "of our keys"},
 "user": "<stable person id, for per-person memory>"}
```
- Content blocks: `{"type":"input_text","text":...}`, `{"type":"input_file","file_id":"file_..."}`.
- Continue: add `previous_response_id` + `metadata.session_id`. Branch: `previous_response_id` + `"branch": true`.
- Read: `GET /v1/responses/{id}` (poll until `completed|failed|incomplete|cancelled`), `POST /v1/responses/{id}/cancel`.

### Streaming (SSE)
`data:`-only frames, so the event name is the JSON `type`:
`response.created` (save `response.id` + `response.metadata.session_id` immediately) →
`response.output_text.delta` (append `delta`) / `response.output_item.added` (tool activity) →
exactly one terminal: `response.completed` | `response.incomplete` | `response.failed`.
If the stream drops after `response.created`, poll `GET /v1/responses/{id}`. **Don't resubmit.**

## Files & sessions

- Upload: `POST /v1/files` (multipart: `file`, `purpose`), max 25 MiB, so reference it as an `input_file` block.
- Write into a session between turns: `PUT /v1/sessions/{id}/files/{path}` `{"content": "..."}` (≤4 MiB).
- List: `GET /v1/sessions/{id}/files[?changed=true]`, zip: `GET /v1/sessions/{id}/files/archive`.
- Rename: `PATCH /v1/sessions/{id}` `{"title"}`. Public read-only share link: `POST /v1/sessions/{id}/share`.

## Workspace events (verified ✅)

`GET /v1/events` with `Accept: text/event-stream` gives an SSE stream (starts with `retry: 3000` / `: connected`).
Frames say a task started or ended (task, harness, member, when), **without content**. Read the task for that.
There is a per-org limit on open streams (`429 too_many_streams`), so **open one server-side stream and fan it out**.

## Memory API (verified ✅ list/providers; write/recall per UHP spec, verify in T-01)

Memories form a **tree**. Recall on a memory searches it **and its descendants**.

| Call | Purpose |
|---|---|
| `GET /v1/memories` ✅ | List memories (id `hmem_...`, provider, privileges, record counts) |
| `GET /v1/memories/providers` ✅ | `graphiti`, `hindsight`, `mem0`, `reme` (capabilities differ, see below) |
| `POST /v1/memories` | Create `{name, description, parent_id, restricted, provider}` |
| `POST /v1/memories/{id}/records` | Write one record as stated |
| `POST /v1/memories/{id}/observe` | Send raw episodes; provider derives records (async, returns a job) |
| `POST /v1/memories/{id}/recall` | Search: `{query, text, filters, types, limit (≤100, default 8)}` → `results[{record, memory, score, why}]` |
| `GET /v1/memories/{id}/records[?type=]` / `.../records/{rid}` | List / get |
| `PATCH /v1/memories/{id}/records/{rid}` | Revise (new version); `GET .../history` |
| `DELETE /v1/memories/{id}/records/{rid}` | Forget |
| `GET /v1/memories/{id}/prime` | Standing records (what gets auto-injected) |
| `POST /v1/memories/{id}/graph` | `{around, hops}` → nodes/edges |
| `GET/POST/DELETE /v1/memories/{id}/grants` | Sharing: `{principal: "member:<id>", privileges: [read,write,create,delete]}` |

Record fields: `id` (`hrec_...`), `type`, `title` (≤300 chars), `content` (parts), `attributes` (our structured
fields go here), `status` (`active|superseded|forgotten`), `time`, `written_by`, `references`
(`rel`: `derived_from|part_of|about|subject|object|contradicts`). Every read comes back as `trust: "untrusted"`.

Providers (live): `reme` (types `episode`,`note`; entities *stated*; text+query recall), `graphiti`
(`episode`,`entity`,`fact`), `hindsight` (`episode`,`fact`,`entity`), `mem0` (`fact`,`entity`; no text search).
**T-01 decides which provider Hive uses.**

## Pricing to keep in mind

Agent work is about $0.02 per active minute plus model cost at provider price, and memory operations cost about $0.0019 each. The free plan allows **2 concurrent
tasks**. Keep harness budgets small and avoid running harnesses in loops.
