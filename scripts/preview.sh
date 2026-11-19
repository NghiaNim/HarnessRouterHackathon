#!/usr/bin/env bash
# Build a LOCAL preview of the platform: origin/main + every open PR merged on top.
# Nothing is pushed; your own checkout and branches are untouched.
#
#   scripts/preview.sh                 # builds ../hive-preview (a separate git worktree)
#   scripts/preview.sh /some/dir       # or any directory
#
# Then: cd ../hive-preview && scripts/preview.sh --run   (see docs/PREVIEW.md)
set -euo pipefail

# Find uv even when a pyenv shim hides it (pyenv: "uv: command not found").
UV="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV" ] || ! "$UV" --version >/dev/null 2>&1; then
  UV=""
  for c in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv "$HOME"/.pyenv/versions/*/bin/uv; do
    if [ -x "$c" ] && "$c" --version >/dev/null 2>&1; then UV="$c"; break; fi
  done
fi
[ -n "$UV" ] || { echo "uv not found: install it with 'brew install uv' (or curl -LsSf https://astral.sh/uv/install.sh | sh)"; exit 1; }

if [[ "${1:-}" == "--run" ]]; then
  # Start the API + dashboard on the preview's local SQLite store.
  set -a; [ -f .env ] && . ./.env; set +a
  export HIVE_STORE=sqlite
  export HIVE_SQLITE_PATH="${HIVE_SQLITE_PATH:-$PWD/.hive/hive.db}" HIVE_DB_PATH="${HIVE_SQLITE_PATH:-$PWD/.hive/hive.db}"
  mkdir -p "$(dirname "$HIVE_SQLITE_PATH")"
  echo "store: $HIVE_SQLITE_PATH"
  echo "open:  http://localhost:${PORT:-8000}/decisions"
  exec "$UV" run uvicorn hive.api.app:app --port "${PORT:-8000}" --reload
fi

REPO="$(git rev-parse --show-toplevel)"
DIR="${1:-$REPO/../hive-preview}"
git -C "$REPO" fetch -q --prune origin

if [ -e "$DIR/.git" ]; then
  git -C "$DIR" merge --abort 2>/dev/null || true
  git -C "$DIR" checkout -q --detach origin/main
  git -C "$DIR" reset -q --hard origin/main      # preview dir only, it's disposable
else
  git -C "$REPO" worktree add -q --detach "$DIR" origin/main
fi
cd "$DIR"
echo "preview at $(pwd) = origin/main $(git rev-parse --short HEAD) + open PRs:"

for row in $(gh pr list --state open --json number,headRefName --jq 'sort_by(.number) | .[] | "\(.number):\(.headRefName)"'); do
  n="${row%%:*}"; b="${row#*:}"
  if git merge -q --no-edit "origin/$b" >/dev/null 2>&1; then
    echo "  + #$n $b"
  else
    echo "  ! #$n $b skipped (conflicts with the PRs above)"; git merge --abort
  fi
done

# .env lives in the MAIN checkout (worktrees don't share untracked files). Replace a placeholder copy.
MAIN="$(cd "$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir)/.." && pwd)"
if [ ! -f .env ] || grep -q 'REPLACE_ME' .env; then
  if [ -f "$MAIN/.env" ]; then cp "$MAIN/.env" .env; elif [ -f "$REPO/.env" ]; then cp "$REPO/.env" .env;
  else cp .env.example .env; echo "  ! no .env found: paste HARNESS_ROUTER_KEY into $(pwd)/.env"; fi
fi
if ! grep -q '^HIVE_STORE=' .env; then
  [ -n "$(tail -c1 .env)" ] && echo >> .env          # .env may lack a trailing newline; don't glue onto the key line
  echo 'HIVE_STORE=sqlite' >> .env                   # HarnessRouter memory needs credit (T-39)
fi

"$UV" sync -q
"$UV" run ruff check . -q && echo "lint: ok"
"$UV" run pytest -q 2>&1 | tail -1
echo
echo "next:  cd $(pwd) && scripts/preview.sh --run"
