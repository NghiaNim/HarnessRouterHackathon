import shutil
import subprocess

import pytest

from hive import config
from hive.config import ConfigError, MissingKeyError, Settings, current_member, git_context


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    monkeypatch.delenv("HIVE_MEMBER", raising=False)
    current_member.cache_clear()
    config.get_settings.cache_clear()
    yield
    current_member.cache_clear()
    config.get_settings.cache_clear()


# --- Settings -------------------------------------------------------------------------------------


def test_defaults_without_env_file_or_key():
    s = Settings.load(env_file="", environ={})
    assert s.project == "harness-router-hackathon"
    assert s.store == "memory"
    assert s.api_url == "http://localhost:8000"
    assert s.hr_base_url == "https://api.harnessrouter.ai"
    assert not s.has_key  # loading never fails just because the key is missing


def test_reads_env_file_with_inline_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "HARNESS_ROUTER_KEY=sk-hr-abc   # shared workspace key\n"
        "HIVE_PROJECT=demo\n"
        "HIVE_STORE=SQLite\n"
        "HIVE_API_URL=http://hive.local:9000/\n",
        encoding="utf-8",
    )
    s = Settings.load(env_file=env, environ={})
    assert s.require_key() == "sk-hr-abc"
    assert (s.project, s.store, s.api_url) == ("demo", "sqlite", "http://hive.local:9000")


def test_db_path_matches_store_factory_env_var():
    assert Settings.load(env_file="", environ={}).db_path == ".hive/hive.db"
    assert Settings.load(env_file="", environ={"HIVE_DB_PATH": "x/h.db"}).db_path == "x/h.db"


def test_real_env_overrides_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("HIVE_PROJECT=from-file\nHARNESS_ROUTER_KEY=sk-hr-file\n", encoding="utf-8")
    s = Settings.load(env_file=env, environ={"HIVE_PROJECT": "from-env"})
    assert s.project == "from-env"
    assert s.hr_key == "sk-hr-file"


@pytest.mark.parametrize("value", ["", "   ", "sk-hr-REPLACE_ME"])
def test_missing_or_placeholder_key_errors_only_on_require(value):
    s = Settings.load(env_file="", environ={"HARNESS_ROUTER_KEY": value})
    assert not s.has_key
    with pytest.raises(MissingKeyError, match="HARNESS_ROUTER_KEY is not set"):
        s.require_key()


def test_invalid_store_is_rejected():
    with pytest.raises(ConfigError, match="HIVE_STORE"):
        Settings.load(env_file="", environ={"HIVE_STORE": "postgres"})


def test_get_settings_finds_nearest_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("HIVE_PROJECT=walked-up\n", encoding="utf-8")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    monkeypatch.delenv("HIVE_PROJECT", raising=False)
    assert config.get_settings().project == "walked-up"


# --- identity -------------------------------------------------------------------------------------


def _fake_run(responses):
    calls = []

    def run(*cmd, cwd=None):
        calls.append(cmd[:2])
        return responses.get(cmd[:2])

    return run, calls


def test_member_prefers_gh_login(monkeypatch):
    run, calls = _fake_run({("gh", "api"): "Odysseyc", ("git", "config"): "Some Name"})
    monkeypatch.setattr(config, "_run", run)
    assert current_member() == "Odysseyc"
    assert current_member() == "Odysseyc"
    assert calls == [("gh", "api")]  # cached: gh is not called twice


def test_member_falls_back_to_git_then_os_user(monkeypatch):
    run, _ = _fake_run({("git", "config"): "Git Name"})
    monkeypatch.setattr(config, "_run", run)
    assert current_member() == "Git Name"

    current_member.cache_clear()
    monkeypatch.setattr(config, "_run", _fake_run({})[0])
    monkeypatch.setattr(config.getpass, "getuser", lambda: "osuser")
    assert current_member() == "osuser"


def test_member_env_override(monkeypatch):
    monkeypatch.setenv("HIVE_MEMBER", "demo-bob")
    monkeypatch.setattr(config, "_run", lambda *a, **k: pytest.fail("should not shell out"))
    assert current_member() == "demo-bob"


def test_run_returns_none_for_missing_binary():
    assert config._run("definitely-not-a-real-binary-hive") is None


# --- git context ----------------------------------------------------------------------------------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@needs_git
def test_git_context_outside_repo(tmp_path):
    ctx = git_context(cwd=tmp_path)
    assert not ctx.is_repo and ctx.changed_files == []


@needs_git
def test_git_context_changed_files_vs_base(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "base.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")

    _git(repo, "switch", "-q", "-c", "feat/x")
    (repo / "committed.py").write_text("y = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat")
    (repo / "base.py").write_text("x = 3\n")  # uncommitted edit
    (repo / "new.py").write_text("z = 4\n")  # untracked

    ctx = git_context(cwd=repo, base="main")
    assert ctx.is_repo
    assert ctx.branch == "feat/x"
    assert ctx.head_sha and len(ctx.head_sha) == 40
    assert ctx.changed_files == ["base.py", "committed.py", "new.py"]


@needs_git
def test_git_context_missing_base_still_reports_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("")  # no commits yet, no origin/main
    ctx = git_context(cwd=repo)
    assert ctx.is_repo and ctx.head_sha is None
    assert ctx.changed_files == ["a.py"]
