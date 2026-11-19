# CLAUDE.md — Team Workflow for HarnessRouterHackathon

Three people work on this repo, each with their **own GitHub account** and their **own Claude Code session**.
Every Claude reading this file must follow the rules below. They exist so three agents can work in parallel
without stepping on each other, and so every change to `main` is reviewed by someone other than its author.

---

## 0. Start here (read before writing any code)

We're building **Hive**, a shared brain for teams of coding agents, on top of **HarnessRouter**.

1. `docs/PRODUCT.md`: what we're building and why (the core demo loop).
2. `docs/ARCHITECTURE.md`: stack, package layout, **file ownership per track**, memory mapping.
3. `docs/harnessrouter-api.md`: **verified** HarnessRouter API reference. Don't re-research the API. If you find
   something new or wrong, fix this file in your PR.
4. `TICKETS.md`: the ticket board. Work only on a ticket you've claimed (§3).

**Contracts:** `hive/models.py` and `hive/store/base.py` are shared interfaces. Build against them. Changing
them needs its own PR approved by **every other active member** (anyone who has pushed or reviewed in the last few hours; today that is NghiaNim, Odysseyc, itsjoshzhang). Additive optional fields are fine, renames/removals aren't.

**Orchestrator:** `NghiaNim`'s Claude acts as product manager/orchestrator. It owns `TICKETS.md` content, the docs,
and scope decisions. If a ticket is unclear or seems wrong, ask your human to raise it with the orchestrator rather
than silently expanding scope.

---

## 1. Team roster

| Member | GitHub handle | Primary track |
|--------|---------------|---------------|
| A      | `NghiaNim`    | A: Brain (HarnessRouter client, memory store, harnesses) + orchestrator |
| B      | `Odysseyc`    | B: Agent integration (config, CLI, MCP, Claude Code hooks, guard) |
| C      | `itsjoshzhang` | C: Surface (API server, dashboard, live feed, demo) |

> Fill in the handles once everyone has accepted the repo invite. Tracks guide file ownership. Anyone may take
> any ticket.

### Who am I?
At the start of every session, determine whose machine you are on — **never assume**:

```bash
ME=$(gh api user -q .login)   # the GitHub account this Claude acts as
```

Everything below that says "you" means `$ME`. Do not act as, impersonate, or approve on behalf of another member.

---

## 2. Golden rules

1. **Never commit or push directly to `main`.** All changes land via Pull Request. The **only** exceptions are
   ticket status updates made by `scripts/ticket.py` (they touch only `TICKETS.md`) and orchestrator commits to
   docs/`TICKETS.md`.
2. **One feature = one branch = one PR.** Keep PRs small and focused (aim for < ~400 changed lines).
3. **Every PR needs an approving review from at least one *other* member** before merging.
4. **Never approve or merge your own PR.** Never force-push to someone else's branch.
5. **Never commit secrets.** `.env` is gitignored — keep it that way. Add new required variables to `.env.example` (names only, no values).
6. **Claim before you build.** Claim the ticket in `TICKETS.md` with `scripts/ticket.py` (see §3) so two Claudes never build the same thing.

---

## 3. Task coordination (`TICKETS.md` = shared task board)

The three Claudes do not share memory. `TICKETS.md` on `origin/main` is the source of truth for who is doing what.

- **Find work:** `python scripts/ticket.py list --open`. Pick the lowest-numbered OPEN ticket in your track whose
  dependencies are DONE (or that you can build against the contracts with a fake).
- **Claim it BEFORE writing any code:** `python scripts/ticket.py claim T-NN`. The script pushes the claim
  straight to `main` from a temporary worktree (safe from any branch) and retries if someone else pushed first. If it
  says the ticket is not open, **someone else has it, so pick another.** Never work on a ticket claimed by someone else.
- **Hold at most 2 claimed tickets.** If you stop working on one, run `python scripts/ticket.py release T-NN`.
- **PR opened:** `python scripts/ticket.py review T-NN --pr <N>`. Run it **in the same step as** `gh pr create`. The board drifts otherwise. **Merged:** `python scripts/ticket.py done T-NN --pr <N>`.
- **Never edit `Status:` lines by hand, and never touch `TICKETS.md` on a feature branch.** That causes merge conflicts.
- **New work discovered mid-task?** Don't expand scope. Tell your human so the orchestrator can add a ticket.
- **Touching shared files** (`pyproject.toml`, contracts, `hive/api/app.py` routes)? Keep the change minimal and
  mention it in the PR description. These are the most common merge-conflict sources.

---

## 4. Branching

Branch names are namespaced by GitHub handle so ownership is obvious and names never collide:

```
<github-handle>/<type>/<short-description>
# examples
NghiaNim/feat/router-scoring
<handle-B>/fix/timeout-retry
<handle-C>/chore/ci-setup
```

`type` ∈ `feat`, `fix`, `refactor`, `docs`, `test`, `chore`.

Starting a branch:

```bash
git fetch origin
git switch main && git pull --ff-only origin main
git switch -c "$ME/feat/<short-description>"
```

- Only push to branches that start with **your own** handle.
- Running several tasks at once? Use a separate git worktree per branch instead of switching branches in one checkout.

---

## 5. Commits

- Conventional style: `feat: add router scoring`, `fix: handle empty response`, etc.
- Commit early and often on your branch; history is squashed on merge.
- Reference the issue in the PR (not necessarily every commit).

---

## 6. Opening a Pull Request

Before opening:

```bash
git fetch origin && git rebase origin/main   # stay current; resolve conflicts on YOUR branch
# run tests / linters (see §9) and make sure they pass
git push -u origin HEAD
```

Then open the PR and request review from **both** other members (whoever is free first reviews):

```bash
gh pr create --base main \
  --title "feat: <summary>" \
  --body "Ticket: T-NN

## What
<what changed>

## Why
<motivation>

## How to test
<commands / steps>

## Notes for reviewer
<risky areas, open questions>" \
  --reviewer <other-handle-1>,<other-handle-2>
```

- Open as `--draft` if you want early feedback; mark ready with `gh pr ready`.
- Tell your human the PR URL so they can ping teammates.

---

## 7. Reviewing teammates' PRs

Reviews are what keep three parallel agents honest. Treat them as real work, not a rubber stamp.

**Check for review requests at the start of every session and before starting new work:**

```bash
gh pr list --search "review-requested:@me is:open"
```

Review queue takes priority over starting a new feature — unblocking teammates keeps the whole team moving.

**Review procedure:**

```bash
gh pr view <N>                 # read description + linked issue
gh pr diff <N>                 # read the full diff
gh pr checkout <N>             # check it out locally (use a worktree if you have uncommitted work)
# run the tests / app (see §9) and actually try the change
```

Look for: correctness bugs, missed edge cases, secrets or credentials, broken tests, scope creep beyond the
linked issue, conflicts with work in other open PRs, and unclear code. Leave inline comments for specifics.

**Submit a verdict:**

```bash
gh pr review <N> --approve         -b "LGTM — tested <how>."
gh pr review <N> --request-changes -b "<numbered list of required changes>"
gh pr review <N> --comment         -b "<non-blocking suggestions / questions>"
```

- Mark each comment as **blocking** or **nit** so the author knows what must change.
- Do **not** push commits to the author's branch — request changes instead.
- Return to your own branch afterwards (`git switch -`).

---

## 8. Responding to review & merging

- **Address feedback** on the same branch, push new commits, and reply to each comment (or explain why not).
  Verify reviewer suggestions before applying them — push back with reasoning if one is wrong.
- Re-request review after changes: `gh pr edit <N> --add-reviewer <handle>`.
- **The PR author merges**, only when all of these hold:
  - ≥ 1 approval from another member, and no outstanding "changes requested"
  - CI / tests pass
  - branch is up to date with `main` (rebase if not)

```bash
gh pr merge <N> --squash --delete-branch
```

- After merging, everyone else should rebase active branches: `git fetch origin && git rebase origin/main`.

---

## 9. Project commands

```bash
uv sync                                   # install (Python >= 3.11)
cp .env.example .env                      # then paste the shared HARNESS_ROUTER_KEY (ask the orchestrator)
uv run pytest                             # tests (live HarnessRouter tests only run with HIVE_LIVE=1)
uv run ruff check .                       # lint
uv run hive --help                        # CLI (once T-12 lands)
uv run uvicorn hive.api.app:app --reload  # API + dashboard (once T-16 lands)
```

HarnessRouter costs real money: keep harness budgets tight, don't run harnesses in loops, and gate live tests
behind `HIVE_LIVE=1`.

---

## 10. Session checklist (every Claude, every session)

1. `ME=$(gh api user -q .login)` — know who you are.
2. `git fetch origin` — see what's new.
3. `gh pr list --search "review-requested:@me is:open"` — review teammates first.
4. `gh pr list --author @me` — handle feedback on your own PRs.
   **Merge anything of yours that is approved and green right away**: `gh pr list --author @me --search "review:approved"`,
   then `gh pr merge <N> --squash --delete-branch` and `python scripts/ticket.py done T-NN --pr <N>`. Approved-but-unmerged PRs
   are the #1 integration risk: on 2026-11-18, 7 approved PRs sat unmerged while new work stacked on top of them.
5. Continue your claimed ticket, or claim a new one with `scripts/ticket.py` (§3).
6. Branch → build → test → rebase → PR (`ticket.py review`) → review → author merges (`ticket.py done`).

---

## 11. Personal / local settings

This file is shared. Put personal preferences in `CLAUDE.local.md` and machine-specific Claude settings
in `.claude/settings.local.json` — both are gitignored and never committed.
