#!/usr/bin/env python3
"""Atomic ticket board operations on TICKETS.md, pushed straight to origin/main.

Works from any branch: it edits a throwaway worktree of origin/main, commits ONLY TICKETS.md and pushes.
If someone else pushed first, it re-fetches and re-checks. So two people can never claim the same ticket.

  python scripts/ticket.py list [--open]
  python scripts/ticket.py claim  T-07
  python scripts/ticket.py review T-07 --pr 12
  python scripts/ticket.py done   T-07 --pr 12
  python scripts/ticket.py release T-07
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
STATUS_RE = re.compile(r"^- \*\*Status:\*\* (.*)$")
HEADER_RE = re.compile(r"^### (T-\d+)\b")
MAX_ATTEMPTS = 5


def sh(*args: str, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=check)


def me() -> str:
    r = sh("gh", "api", "user", "-q", ".login", check=False)
    if r.returncode != 0 or not r.stdout.strip():
        sys.exit("Could not determine GitHub login. Run `gh auth login` first.")
    return r.stdout.strip()


def find_status(lines: list[str], ticket: str) -> int:
    in_ticket = False
    for i, line in enumerate(lines):
        h = HEADER_RE.match(line)
        if h:
            in_ticket = h.group(1) == ticket
        elif in_ticket and STATUS_RE.match(line):
            return i
    sys.exit(f"{ticket} not found in TICKETS.md")


def board(text: str) -> list[tuple[str, str, str]]:
    out, cur, title = [], None, ""
    for line in text.splitlines():
        h = HEADER_RE.match(line)
        if h:
            cur, title = h.group(1), line[4:].strip()
        elif cur and (s := STATUS_RE.match(line)):
            out.append((cur, s.group(1), title))
            cur = None
    return out


def transition(ticket: str, action: str, pr: int | None) -> None:
    login = me()
    today = dt.date.today().isoformat()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        sh("git", "fetch", "origin", "main")
        with tempfile.TemporaryDirectory(prefix="ticket-") as tmp:
            wt = Path(tmp) / "wt"
            sh("git", "worktree", "add", "--detach", str(wt), "origin/main")
            try:
                path = wt / "TICKETS.md"
                lines = path.read_text().splitlines()
                i = find_status(lines, ticket)
                current = STATUS_RE.match(lines[i]).group(1)  # type: ignore[union-attr]
                owner = re.search(r"@([\w-]+)", current)
                if action == "claim":
                    if not current.startswith("OPEN"):
                        sys.exit(f"{ticket} is not open: {current}")
                    new = f"CLAIMED @{login} {today}"
                else:
                    if not owner or owner.group(1) != login:
                        sys.exit(f"{ticket} is not yours: {current}")
                    new = {
                        "review": f"IN REVIEW @{login} PR #{pr}",
                        "done": f"DONE @{login} PR #{pr}",
                        "release": "OPEN",
                    }[action]
                lines[i] = f"- **Status:** {new}"
                path.write_text("\n".join(lines) + "\n")
                sh("git", "add", "TICKETS.md", cwd=wt)
                sh("git", "commit", "-q", "-m", f"tickets: {action} {ticket} (@{login})", cwd=wt)
                push = sh("git", "push", "origin", "HEAD:main", cwd=wt, check=False)
                if push.returncode == 0:
                    print(f"{ticket}: {current}  ->  {new}")
                    return
                print(f"push rejected (attempt {attempt}), someone else updated main; retrying...")
            finally:
                sh("git", "worktree", "remove", "--force", str(wt), check=False)
    sys.exit("Gave up after repeated push conflicts. Try again.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--open", action="store_true")
    for name in ("claim", "review", "done", "release"):
        sp = sub.add_parser(name)
        sp.add_argument("ticket")
        if name in ("review", "done"):
            sp.add_argument("--pr", type=int, required=True)
    a = p.parse_args()

    if a.cmd == "list":
        sh("git", "fetch", "origin", "main")
        text = sh("git", "show", "origin/main:TICKETS.md").stdout
        for _tid, status, title in board(text):
            if not a.open or status.startswith("OPEN"):
                print(f"{status:<32} {title}")
        return
    transition(a.ticket.upper(), a.cmd, getattr(a, "pr", None))


if __name__ == "__main__":
    main()
