import pytest

from hive.store import get_store
from hive.store.sqlite import SqliteStore


def test_default_is_sqlite_at_hive_db_path(monkeypatch, tmp_path):
    monkeypatch.delenv("HIVE_STORE", raising=False)
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "x" / "hive.db"))
    store = get_store()
    assert isinstance(store, SqliteStore)
    assert (tmp_path / "x" / "hive.db").exists()


def test_unknown_store_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("HIVE_STORE", "redis")
    with pytest.raises(ValueError, match="redis"):
        get_store()
