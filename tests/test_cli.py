import json

import pytest
from typer.testing import CliRunner

from hive import cli, config
from hive.config import GitContext
from hive.models import DecisionStatus

runner = CliRunner()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated CLI: temp sqlite, fixed member, fake git context, not inside Claude Code."""
    db = tmp_path / "hive.db"
    monkeypatch.setenv("HIVE_STORE", "sqlite")
    monkeypatch.setenv("HIVE_SQLITE_PATH", str(db))
    monkeypatch.setenv("HIVE_MEMBER", "alice")
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env picked up from the repo
    ctx = GitContext(branch="alice/feat/x", head_sha="a" * 40, changed_files=["hive/hr/client.py"],
                     repo_root=str(tmp_path))
    monkeypatch.setattr(cli, "git_context", lambda cwd=None: ctx)
    monkeypatch.setattr(cli, "interactive", lambda: True)  # a human at a real terminal
    config.get_settings.cache_clear()
    config.current_member.cache_clear()
    yield tmp_path
    config.get_settings.cache_clear()
    config.current_member.cache_clear()


def run(*args, input=None):
    result = runner.invoke(cli.app, list(args), input=input)
    return result


def propose(statement="Use httpx; never requests.", *extra):
    r = run("decide", statement, "--json", *extra)
    assert r.exit_code == 0, r.output
    return json.loads(r.stdout)


def accept(d):
    return run("accept", d["id"], input=d["id"][:8] + "\n")


def test_decide_accept_and_list(env):
    d = propose("Use httpx; never requests.", "--title", "HTTP is httpx", "--scope", "hive/**",
                "--rationale", "sync+async", "--tag", "http")
    assert d["status"] == "proposed" and d["proposed_by"] == "alice"
    assert d["scope"] == ["hive/**"] and d["tags"] == ["http"]

    r = run("accept", d["id"][:8], input=d["id"][:8] + "\n")  # unique prefix, as shown in briefs
    assert r.exit_code == 0 and "accepted" in r.stdout

    r = run("decisions", "--status", "accepted", "--json")
    [listed] = json.loads(r.stdout)
    assert listed["id"] == d["id"] and listed["decided_by"] == "alice"

    assert "HTTP is httpx" in run("decisions", "--path", "hive/hr/client.py").stdout
    assert run("decisions", "--path", "README.md").stdout.strip() == "no decisions"


def test_reject_with_reason(env):
    d = propose()
    r = run("reject", d["id"], "--reason", "vendored SDK needs requests", input=d["id"][:8] + "\n")
    assert r.exit_code == 0
    [listed] = json.loads(run("decisions", "--json").stdout)
    assert listed["status"] == DecisionStatus.REJECTED.value
    assert "vendored SDK" in listed["rationale"]


def test_agent_inside_claude_code_cannot_accept(env, monkeypatch):
    d = propose()
    monkeypatch.setenv("CLAUDECODE", "1")
    r = run("accept", d["id"])
    assert r.exit_code == 3
    assert "only propose" in r.stderr and "own terminal" in r.stderr
    # ...but an agent may still propose, and is labelled as one
    assert propose("Never commit .env")["proposed_by"] == "agent:alice"


def test_unknown_and_ambiguous_ids(env):
    assert run("accept", "zzzz").exit_code == 1
    propose("a")
    propose("b")
    r = run("accept", "")  # empty prefix matches everything
    assert r.exit_code == 1 and "ambiguous" in r.stderr


def test_invalid_transition_is_a_clean_error(env):
    d = propose()
    accept(d)
    r = accept(d)
    assert r.exit_code == 1 and "proposed decision" in r.stderr


def test_supersede_via_decide(env):
    old = propose("Use httpx")
    accept(old)
    new = propose("Use httpx with a 10s timeout", "--supersedes", old["id"][:8])
    assert new["supersedes"] == old["id"]
    accept(new)
    statuses = {d["id"]: d["status"] for d in json.loads(run("decisions", "--json").stdout)}
    assert statuses == {old["id"]: "superseded", new["id"]: "accepted"}


def test_brief_and_hook_output(env):
    accept(propose("Use httpx", "--scope", "hive/**"))
    accept(propose("No Node build", "--scope", "hive/web/**"))

    md = run("brief").stdout
    assert "Use httpx" in md and "No Node build" not in md  # only in-scope for hive/hr/client.py

    r = run("brief", "--hook", input=json.dumps({"cwd": str(env), "session_id": "s1"}))
    assert r.exit_code == 0
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart" and "Use httpx" in out["additionalContext"]


def test_brief_hook_fails_open(env, monkeypatch):
    def boom(**_):
        raise RuntimeError("store down")

    monkeypatch.setattr(cli, "build_brief", boom)
    r = run("brief", "--hook", input="not json either")
    assert r.exit_code == 0 and r.stdout == ""


def test_handoff_and_recall(env):
    r = run("handoff", "--summary", "Wired httpx retries", "--learned", "429 needs backoff",
            "--gotcha", "never retry store_write_failed", "--ticket", "T-02")
    assert r.exit_code == 0 and "1 files touched" in r.stdout
    propose("Use httpx")

    hits = json.loads(run("recall", "httpx", "--json").stdout)
    assert {h["kind"] for h in hits} == {"handoff", "decision"}
    handoff = next(h["item"] for h in hits if h["kind"] == "handoff")
    assert handoff["member"] == "alice" and handoff["branch"] == "alice/feat/x"
    assert handoff["files_touched"] == ["hive/hr/client.py"] and handoff["ticket"] == "T-02"

    assert [h["kind"] for h in json.loads(run("recall", "httpx", "--kind", "decision", "--json").stdout)] == [
        "decision"
    ]
    assert run("recall", "httpx", "--kind", "bogus").exit_code == 1
    assert run("recall", "kubernetes").stdout.strip() == "no matches"


def test_handoff_requires_summary_and_hook_is_noop(env):
    assert run("handoff").exit_code == 1
    assert run("handoff", "--hook", input="{}").exit_code == 0


def test_pending_engine_commands_exit_2(env):
    for command in ("init", "map", "check"):
        r = run(command)
        assert r.exit_code == 2 and "hasn't merged yet" in r.stderr


def test_relative_sqlite_path_is_anchored_at_repo_root(env, monkeypatch):
    monkeypatch.setenv("HIVE_SQLITE_PATH", ".hive/hive.db")
    config.get_settings.cache_clear()
    sub = env / "deep" / "dir"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    propose()
    assert (env / ".hive" / "hive.db").exists()
    assert not (sub / ".hive").exists()


def test_scope_globs_are_not_expanded_on_windows(env, monkeypatch):
    # Regression: Click expands `hive/**` into matching files on Windows unless told not to.
    (env / "hive").mkdir()
    (env / "hive" / "a.py").write_text("")
    monkeypatch.setattr("sys.argv", ["hive", "decide", "x", "--scope", "hive/**", "--json"])
    with pytest.raises(SystemExit) as exit_info:
        cli.app()
    assert exit_info.value.code in (0, None)
    [d] = json.loads(run("decisions", "--json").stdout)
    assert d["scope"] == ["hive/**"]


def test_accept_requires_a_terminal_even_without_claudecode(env, monkeypatch):
    # Regression: `env -u CLAUDECODE hive accept` from an agent tool call must still be refused.
    d = propose()
    monkeypatch.setattr(cli, "interactive", lambda: False)
    for verb in ("accept", "reject"):
        r = run(verb, d["id"], input=d["id"][:8] + "\n")
        assert r.exit_code == 3 and "interactive terminal" in r.stderr
    assert json.loads(run("decisions", "--json").stdout)[0]["status"] == "proposed"


def test_accept_requires_typed_confirmation(env):
    d = propose()
    r = run("accept", d["id"], input="yes\n")
    assert r.exit_code == 3 and "did not match" in r.stderr
    assert json.loads(run("decisions", "--json").stdout)[0]["status"] == "proposed"


def test_unavailable_memory_store_fails_loudly_but_hook_degrades(env, monkeypatch):
    monkeypatch.setenv("HIVE_STORE", "memory")
    config.get_settings.cache_clear()
    r = run("decisions")
    assert r.exit_code == 1 and "HIVE_STORE=sqlite" in r.stderr
    r = run("brief", "--hook", input="{}")
    assert r.exit_code == 0 and "SessionStart" in r.stdout


def test_store_errors_are_messages_not_tracebacks(env, monkeypatch):
    monkeypatch.setenv("HIVE_SQLITE_PATH", str(env / "not-a-dir.txt" / "hive.db"))
    (env / "not-a-dir.txt").write_text("")
    config.get_settings.cache_clear()
    for args in (["decisions"], ["brief"], ["recall", "x"]):
        r = run(*args)
        assert r.exit_code == 1 and "error:" in r.stderr and "Traceback" not in r.output
