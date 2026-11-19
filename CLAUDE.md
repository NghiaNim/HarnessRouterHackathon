# CLAUDE.md — Team Workflow for HarnessRouterHackathon

Three people work on this repo, each with their **own GitHub account** and their **own Claude Code session**.
Every Claude reading this file must follow the rules below. They exist so three agents can work in parallel
without stepping on each other, and so every change to `main` is reviewed by someone other than its author.

---

## 1. Team roster

| Member | GitHub handle | Focus area (optional) |
|--------|---------------|-----------------------|
| A      | `NghiaNim`    | _TBD_                 |
| B      | `<handle-B>`  | _TBD_                 |
| C      | `<handle-C>`  | _TBD_                 |

> Fill in the handles once everyone has accepted the repo invite. Focus areas are a guide for avoiding
> conflicts, not a hard boundary.

### Who am I?
At the start of every session, determine whose machine you are on — **never assume**:

```bash
ME=$(gh api user -q .login)   # the GitHub account this Claude acts as
```

Everything below that says "you" means `$ME`. Do not act as, impersonate, or approve on behalf of another member.

---

## 2. Golden rules

1. **Never commit or push directly to `main`.** All changes land via Pull Request. (The only exception was the initial bootstrap commit.)
2. **One feature = one branch = one PR.** Keep PRs small and focused (aim for < ~400 changed lines).
3. **Every PR needs an approving review from at least one *other* member** before merging.
4. **Never approve or merge your own PR.** Never force-push to someone else's branch.
5. **Never commit secrets.** `.env` is gitignored — keep it that way. Add new required variables to `.env.example` (names only, no values).
6. **Claim before you build.** Work starts from a GitHub Issue assigned to you (see §3), so two Claudes never build the same thing.

---

## 3. Task coordination (GitHub Issues = shared task board)

The three Claudes do not share memory. GitHub is the only shared state, so use it as the source of truth.

- **Find work:** `gh issue list --state open --search "no:assignee"`
- **Claim it:** `gh issue edit <N> --add-assignee @me` — then comment a one-line plan: `gh issue comment <N> -b "Plan: ..."`.
- **Already assigned to someone else?** Do not touch it. Pick another issue or ask your human.
- **New work discovered mid-task?** Open a new issue (`gh issue create`) rather than expanding scope.
- **Touching shared/core files** (config, shared interfaces, dependency files like `requirements.txt` / `pyproject.toml`)? Mention it in the issue so the others know, and keep that change in its own small PR when possible — these are the most common merge-conflict sources.

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
  --body "Closes #<issue>

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

> Fill in as the project takes shape (Python project — see `.gitignore`).

```bash
# setup:    python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
# test:     pytest
# lint:     ruff check .
# run:      <TBD>
```

---

## 10. Session checklist (every Claude, every session)

1. `ME=$(gh api user -q .login)` — know who you are.
2. `git fetch origin` — see what's new.
3. `gh pr list --search "review-requested:@me is:open"` — review teammates first.
4. `gh pr list --author @me` — handle feedback on your own PRs.
5. Continue your assigned issue, or claim a new one (§3).
6. Branch → build → test → rebase → PR → review → author merges.

---

## 11. Personal / local settings

This file is shared. Put personal preferences in `CLAUDE.local.md` and machine-specific Claude settings
in `.claude/settings.local.json` — both are gitignored and never committed.
