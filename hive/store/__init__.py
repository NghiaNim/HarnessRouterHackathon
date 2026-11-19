"""Store selection. Everything above the store calls `get_store()` and depends only on `ContextStore`."""

from __future__ import annotations

import os

from hive.store.base import ContextStore, NotFound

__all__ = ["ContextStore", "NotFound", "get_store"]

DEFAULT_DB_PATH = ".hive/hive.db"


def get_store(kind: str | None = None) -> ContextStore:
    """Build the store named by `kind` or `$HIVE_STORE` (default: sqlite).

    sqlite uses `$HIVE_SQLITE_PATH` (default `.hive/hive.db`, gitignored).
    """
    kind = (kind or os.environ.get("HIVE_STORE") or "sqlite").strip().lower()
    if kind == "sqlite":
        from hive.store.sqlite import SqliteStore

        return SqliteStore(os.environ.get("HIVE_SQLITE_PATH") or DEFAULT_DB_PATH)
    if kind == "memory":
        try:
            from hive.store.memory import HarnessMemoryStore
        except ImportError as e:  # lands later in T-04
            raise RuntimeError("HIVE_STORE=memory is not available yet; set HIVE_STORE=sqlite") from e
        return HarnessMemoryStore.from_env()
    raise ValueError(f"unknown HIVE_STORE={kind!r} (expected 'sqlite' or 'memory')")
