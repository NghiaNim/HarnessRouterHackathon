# Run the platform locally (preview)

`main` holds the library layers. The runnable pieces (CLI, API, dashboard, distiller) may still be in open PRs.
`scripts/preview.sh` builds **origin/main + every open PR** into a separate worktree (`../hive-preview`). Nothing is
pushed, and your own checkout is never touched.

## 0. One-time: make `uv` available

If `uv --version` says *pyenv: uv: command not found*, run `brew install uv` (or `curl -LsSf https://astral.sh/uv/install.sh | sh`).
The preview script finds a pyenv-hidden uv by itself, but you'll want it on PATH anyway.

## 1. Build the preview (about 1 min)

```bash
cd ~/HarnessRouterHackathon          # your normal checkout, any branch
git fetch origin
git show origin/main:scripts/preview.sh > /tmp/preview.sh && bash /tmp/preview.sh
```
It prints which PRs merged, then `lint: ok` and the test count. It copies your `.env` and adds `HIVE_STORE=sqlite`
(HarnessRouter Memory needs credit, see T-39).

## 2. Start the server + dashboard

```bash
cd ../hive-preview
scripts/preview.sh --run             # → http://localhost:8000/decisions  (PORT=8001 to change)
```

## 3. Walk the core loop (second terminal, inside ../hive-preview)

```bash
cd ../hive-preview
export HIVE_STORE=sqlite HIVE_SQLITE_PATH=$PWD/.hive/hive.db HIVE_DB_PATH=$PWD/.hive/hive.db
export PATH="$PWD/.venv/bin:$PATH"     # puts `hive` on PATH (no uv needed)

# an "agent" proposes a rule (piping stdin makes it non-interactive, so it's labelled agent:bob)
HIVE_MEMBER=bob hive decide "All HTTP calls use httpx with a timeout; never requests." \
  --title "HTTP client is httpx" --scope "hive/**" </dev/null

# the agent can't accept its own rule (exit 3)
HIVE_MEMBER=bob hive accept <id> </dev/null
```
Now open **http://localhost:8000/decisions**, type your handle in "You are", and click **Accept**. Then:
```bash
hive decisions --status accepted                 # the CLI sees the dashboard's accept
HIVE_MEMBER=bob hive handoff --summary "GOTCHA: accept must go through DecisionService" </dev/null
echo '{"session_id":"s1","cwd":"'$PWD'","hook_event_name":"SessionStart"}' \
  | HIVE_MEMBER=carol hive brief --hook          # what Carol's Claude Code would receive at session start
hive recall httpx
```

## 4. (Optional) Wire it into your real Claude Code

Until `hive install` lands (T-14), add this to `../hive-preview/.claude/settings.local.json` and start `claude` in `../hive-preview`:
```json
{ "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "\"$CLAUDE_PROJECT_DIR\"/.venv/bin/hive brief --hook" }] }],
    "SessionEnd":   [{ "hooks": [{ "type": "command", "command": "\"$CLAUDE_PROJECT_DIR\"/.venv/bin/hive handoff --hook" }] }] } }
```
The brief appears in the session's context. SessionEnd hands the transcript to the distiller in the background.

## What works / what doesn't yet

| Works now | Blocked or not built |
|---|---|
| Propose (CLI/API/dashboard), human-only accept, decisions board | **Distiller / guard / arbiter / oracle**: HarnessRouter returns `402 no_key` / `no_credit` until credit + BYOK (T-39) |
| Brief (CLI hook + API), handoffs, recall, 6 dashboard pages | HarnessRouter Memory store (T-09), so the preview is single-machine SQLite |
| Secret redaction, spend ledger, scope matching | Intents/collision radar (T-50–T-55), `hive ask` (T-53), `hive check` (T-20) |

Rebuild any time with `bash /tmp/preview.sh`. It resets `../hive-preview` to the newest main + open PRs. Your local `.hive/hive.db` is untracked and survives.
