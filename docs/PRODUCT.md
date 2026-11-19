# Hive — shared brain for teams of coding agents

> Working name. Rename freely.

## Problem

When three people each run their own coding agent (Claude Code, Codex, …) on one codebase:

1. **Every agent rediscovers the codebase from scratch.** Each session burns tokens reading the same files to
   rebuild the same mental model a teammate's agent built an hour ago.
2. **Human decisions don't reach the other agents.** "We use httpx, not requests." "Auth tokens never go to the
   browser." These get agreed in Slack or in one person's chat, and the other agents never see them, so they
   break them.
3. **Context transfer is manual.** People paste summaries into Slack, re-explain the same things to their own
   agent, and lose the "what I learned / watch out for" from finished sessions.

## Solution

Hive is a **shared, persistent context layer** that every teammate's agent reads from and writes to
automatically. It is built on **HarnessRouter Memory** (shared store) and **HarnessRouter harnesses** (background agents that
distill and check context).

| Concept | What it is | Who writes it | Who reads it |
|---|---|---|---|
| **Decision** | A binding rule from a *human* ("Use httpx for HTTP"), with rationale and a file scope (globs). | Agents may only **propose**. Only humans **accept/reject**. | Injected into every agent session whose work touches the scope. |
| **Handoff** | What an agent session learned: summary, facts discovered, gotchas, files touched, open questions. | Written automatically at session end (distilled from the transcript). | Teammates' agents at session start ("what changed since I left?"). |
| **Module note** | A cached map of the codebase: per-path summary, key symbols, dependencies, at a commit SHA. | Cartographer harness, updated incrementally. | Agents instead of re-reading the codebase. |

### Core loop (the MVP demo)

1. **Alice** accepts a decision in the dashboard: *"All HTTP calls use httpx with a timeout; never `requests`."* (scope `hive/**`).
2. **Bob** opens Claude Code. The SessionStart hook runs `hive brief`, which injects about 1–2k tokens: relevant
   accepted decisions, teammates' recent handoffs, and the module map for the area he's touching. **Bob's agent
   knows the rule and the codebase without re-reading it.**
3. Bob's agent finishes. The session-end hook sends the transcript to the **Distiller** harness, which writes a **Handoff**
   and proposes any decision-like statements it heard ("Bob said: never call HR from the browser").
4. Alice sees the proposed decision in the dashboard and accepts it. Every agent now follows it.
5. **Carol's** agent writes `import requests`. `hive check` (Decision Guard harness) flags that the diff violates Alice's decision
   before the PR is merged.
6. The dashboard shows a live feed (HarnessRouter `/v1/events`) and a **tokens-saved meter**.

### Product principles

- **Humans decide, agents propose.** An agent can never accept a decision. This is the trust boundary.
- **Zero copy-paste.** Context moves through hooks and MCP, not through people.
- **Token-budgeted.** A brief has a hard budget. Relevance ranking beats completeness.
- **Scoped.** Decisions and notes carry path globs, so an agent only gets what applies to the files it touches.
- **Dogfood it.** We use Hive to build Hive during the hackathon.

### Non-goals (for the hackathon)

Auth/multi-tenant SaaS, fine-grained permissions, non-git VCS, or a polished SPA frontend.
