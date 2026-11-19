"""Settings, identity, and git context (T-05).

- `get_settings()` reads `.env` (nearest one walking up from cwd) with real env vars taking precedence.
  A missing HarnessRouter key is NOT an error here; it only fails in `Settings.require_key()`, which
  the HarnessRouter client calls right before a network call. Offline / SQLite use never needs a key.
- `current_member()` = GitHub login (`gh api user`), else `git config user.name`, else the OS user.
- `git_context()` = branch, HEAD sha, and files changed vs `origin/main` (committed + uncommitted).

Every subprocess has a short timeout: these run inside Claude Code hooks and must never hang a session.
"""

from __future__ import annotations

import getpass
import os
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values, find_dotenv

StoreKind = Literal["memory", "sqlite"]
STORE_KINDS: tuple[str, ...] = ("memory", "sqlite")

DEFAULT_HR_BASE_URL = "https://api.harnessrouter.ai"
DEFAULT_PROJECT = "harness-router-hackathon"
DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_DB_PATH = ".hive/hive.db"
PLACEHOLDER_KEY = "sk-hr-REPLACE_ME"
SUBPROCESS_TIMEOUT_S = 3.0


class ConfigError(ValueError):
    """Invalid configuration value (e.g. unknown HIVE_STORE)."""


class MissingKeyError(RuntimeError):
    """Raised only when a HarnessRouter call is attempted without a key."""


@dataclass(frozen=True)
class Settings:
    hr_key: str | None = None
    project: str = DEFAULT_PROJECT
    store: StoreKind = "memory"
    api_url: str = DEFAULT_API_URL
    hr_base_url: str = DEFAULT_HR_BASE_URL
    db_path: str = DEFAULT_DB_PATH
    env_file: str | None = field(default=None, compare=False)

    @property
    def has_key(self) -> bool:
        return bool(self.hr_key)

    def require_key(self) -> str:
        if not self.hr_key:
            where = self.env_file or "a .env file at the repo root"
            raise MissingKeyError(
                "HARNESS_ROUTER_KEY is not set. Copy .env.example to .env and paste the shared workspace "
                f"key (ask the orchestrator), checked {where}. Or set HIVE_STORE=sqlite to work offline."
            )
        return self.hr_key

    @classmethod
    def load(cls, env_file: str | os.PathLike | None = None, environ: dict | None = None) -> Settings:
        """Settings from `env_file` (default: nearest .env), overlaid by `environ` (default: os.environ)."""
        path = str(env_file) if env_file is not None else find_dotenv(usecwd=True)
        file_vals = {k: v for k, v in dotenv_values(path).items() if v is not None} if path else {}
        env = {**file_vals, **(os.environ if environ is None else environ)}

        def get(name: str, default: str | None = None) -> str | None:
            value = (env.get(name) or "").strip()
            return value or default

        key = get("HARNESS_ROUTER_KEY")
        if key == PLACEHOLDER_KEY:
            key = None
        store = (get("HIVE_STORE", "memory") or "memory").lower()
        if store not in STORE_KINDS:
            raise ConfigError(f"HIVE_STORE must be one of {STORE_KINDS}, got {store!r}")
        return cls(
            hr_key=key,
            project=get("HIVE_PROJECT", DEFAULT_PROJECT),
            store=store,  # type: ignore[arg-type]
            api_url=get("HIVE_API_URL", DEFAULT_API_URL).rstrip("/"),
            hr_base_url=get("HARNESS_ROUTER_URL", DEFAULT_HR_BASE_URL).rstrip("/"),
            db_path=get("HIVE_DB_PATH", DEFAULT_DB_PATH),
            env_file=path or None,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Call `get_settings.cache_clear()` in tests after changing env."""
    return Settings.load()


# --- subprocess helper ------------------------------------------------------------------------------


def _run(*cmd: str, cwd: str | os.PathLike | None = None) -> str | None:
    """Run a command; return stripped stdout, or None on any failure / timeout / empty output."""
    try:
        out = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


# --- identity ---------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def current_member() -> str:
    """Who this machine acts as. `HIVE_MEMBER` overrides (demos/tests). Cached for the process."""
    return (
        (os.environ.get("HIVE_MEMBER") or "").strip()
        or _run("gh", "api", "user", "-q", ".login")
        or _run("git", "config", "user.name")
        or getpass.getuser()
    )


# --- git context ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GitContext:
    branch: str | None = None
    head_sha: str | None = None
    changed_files: list[str] = field(default_factory=list)
    repo_root: str | None = None

    @property
    def is_repo(self) -> bool:
        return self.repo_root is not None


def git_context(cwd: str | os.PathLike | None = None, base: str = "origin/main") -> GitContext:
    """Branch, HEAD sha, and files changed vs `base` (merge-base diff + uncommitted + untracked).

    Never raises: outside a git repo, returns an empty GitContext.
    """
    root = _run("git", "rev-parse", "--show-toplevel", cwd=cwd)
    if root is None:
        return GitContext()
    branch = _run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    head = _run("git", "rev-parse", "HEAD", cwd=root)

    changed: set[str] = set()
    merge_base = _run("git", "merge-base", "HEAD", base, cwd=root) if head else None
    if merge_base:
        changed.update(_lines(_run("git", "diff", "--name-only", merge_base, "HEAD", cwd=root)))
    if head:
        changed.update(_lines(_run("git", "diff", "--name-only", "HEAD", cwd=root)))
    changed.update(_lines(_run("git", "ls-files", "--others", "--exclude-standard", cwd=root)))

    return GitContext(
        branch=None if branch == "HEAD" else branch,  # detached HEAD
        head_sha=head,
        changed_files=sorted(changed),
        repo_root=str(Path(root)),
    )


def _lines(text: str | None) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()] if text else []
