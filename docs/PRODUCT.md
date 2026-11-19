# Hive: the coordination layer for teams of coding agents

> Working name. Rename freely.

**One-liner:** Your team's agents work like a team. They obey the rules your humans set, see what each other are doing
in real time, and answer each other's questions, so nobody pastes context into Slack and no agent burns tokens rediscovering
the codebase.

## Problem

When three people each run their own coding agent on one codebase:

1. **Human decisions don't reach the other agents.** "Use httpx, not requests." "Tokens never go to the browser." Someone
   says it once, to *their* agent or in Slack, and the other agents never hear it, so they break it.
2. **Agents collide.** Two agents rewrite the same module at the same time, and nobody finds out until the merge conflict.
3. **Context transfer is manual.** "Why did your agent change the retry logic?" turns into a Slack ping, a context switch, and a
   pasted summary.
4. **Every agent rediscovers the codebase**, burning tokens to rebuild a model a teammate's agent built an hour ago.

## Positioning: why this isn't "just shared memory"

Shared memory is crowded. A committed `CLAUDE.md`, Cursor rules, Cline Memory Bank, Mem0/Zep, Byterover and codebase
indexers (Cursor, Augment, Sourcegraph) all store and retrieve notes. **Hive's wedge is coordination, not storage:**

| | Static rules files (`CLAUDE.md`, Cursor rules) | Memory layers (Mem0, Byterover, …) | **Hive** |
|---|---|---|---|
| Where rules come from | Someone remembers to write docs | Whatever the agent saves | **Mined from human corrections**, with the quote as evidence |
| Who can make a rule binding | Anyone who edits the file | The agent | **Humans only.** Agents propose. |
| Scope | Whole repo / folder | Global | **Path globs**, so only relevant rules get injected |
| Enforcement | None | None | **Guard checks diffs**, citing the rule and the original quote |
| Awareness of teammates' live work | None | None | **Live intents + collision warnings before an edit** |
| Asking a teammate's agent a question | Slack | None | **`hive ask @bob`** answers from Bob's agent's actual context |
| Staleness | Rots silently | Rots silently | **Tied to commits.** Stale context is flagged or dropped. |
| Agent harness | Tool-specific | Usually one | **Any harness** (Claude Code hooks, MCP, AGENTS.md), via HarnessRouter |
| Secrets | Whatever you paste | Whatever the agent saves | **Redacted before anything leaves the machine** |
| Prompt injection | N/A | Agent-saved notes can carry injected instructions | **Only human-quoted rules bind. Teammates' notes are fenced as data and screened** |

### The four pillars

1. **Corrections become rules.** You already correct your agent ("no, use httpx"). Hive turns that sentence into a proposed
   decision, a human approves it with one click, and every agent gets it when it touches files in scope. Mechanical rules
   are enforced **at edit time** (a forbid pattern denies the write in < 50 ms, T-56), and everything else by the Guard on the diff.
2. **Collision radar.** Every agent session broadcasts an *intent* (ticket and paths). Before an agent edits a file that a
   teammate's agent is working on right now, it gets warned. The dashboard shows who's working where.
3. **Ask the agent, not the person.** `hive ask @bob "why did you switch to polling?"` answers from Bob's session handoffs and
   transcript, with citations, or says "not in Bob's context". Bob isn't interrupted.
4. **Fresh or flagged.** Every handoff is pinned to a commit, and context whose files changed since then is marked stale or dropped.

### Why HarnessRouter matters to the pitch

- **Memory** (the shared, tree-scoped store with history and provenance) is the team brain.
- **Harnesses** run our background agents (Distiller, Guard, Arbiter, Oracle) on budgets, off the developer's machine.
- **Sessions and files** let `hive ask` continue a conversation grounded in a teammate's transcript.
- **Events** drive the live dashboard.
- **Harness-agnostic:** teammates can use Claude Code, Codex, or Gemini CLI and still share one brain.

## Concepts

| Concept | What it is | Who writes it | Who reads it |
|---|---|---|---|
| **Decision** | A binding rule with rationale, path scope, and *evidence* (the human quote). | Agents **propose** (mined from corrections). Humans **accept/reject**. | Injected into sessions touching the scope. Enforced by the Guard. |
| **Handoff** | What a session learned: summary, facts, gotchas, files, open questions, transcript. | Written automatically at session end. | Teammates' agents (brief) and `hive ask`. |
| **Intent** | What an agent is working on *right now*: member, branch, ticket, paths, heartbeat. | Session hooks, automatically. | Collision warnings, brief, radar. |
| **Module note** | A cached per-path codebase summary. | *(stretch)* Cartographer. | Brief. |

## Demo script (about 3 minutes)

1. **Radar.** The dashboard shows Bob's agent live on `hive/api/**` (T-16) and Carol's agent on `hive/store/**`.
2. **A correction becomes a rule.** Bob tells his agent *"no, never call HarnessRouter from the browser, it leaks the key."* His session ends. A
   proposed decision appears on the dashboard, quoting Bob. Alice clicks **Accept**.
3. **The brief.** Carol starts a new session. Her agent's first context includes Bob's rule, "Bob is active on `hive/api/**`", and
   Bob's fresh handoff. It knows all of this without re-reading the codebase.
4. **Collision.** Carol's agent goes to edit `hive/api/app.py` and is warned that Bob's agent is in there right now. Carol runs
   `hive ask @bob "what's the response shape of /api/decisions?"` and gets a cited answer in seconds, with no Slack.
5. **Enforcement.** Carol's diff adds a `fetch()` to HarnessRouter in a template. `hive check` blocks it, citing the decision and Bob's quote.
6. **Impact strip:** tokens saved · rules mined from corrections · violations caught · collisions warned · questions answered.

## Product principles

- **Humans decide, agents propose.** An agent can never accept a decision. This is the trust boundary.
- **Zero copy-paste.** Context moves through hooks and MCP, not through people.
- **Never slow the agent.** Hooks fail open, have hard timeouts, and run heavy work in the background.
- **Token-budgeted and scoped.** Relevance beats completeness.
- **Show the evidence.** Every rule cites its quote, every answer cites its source, and every number explains how it's computed.
- **Dogfood it.** We use Hive to build Hive. Our own 3-Claude hackathon is the case study.

## Non-goals (for the hackathon)

Auth/multi-tenant SaaS, fine-grained permissions, non-git VCS, a polished SPA frontend, or full codebase indexing.
