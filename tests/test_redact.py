"""T-28: nothing secret leaves the machine.

Fake secrets are assembled from pieces so repo secret scanners don't flag this file.
"""

from __future__ import annotations

import json

import pytest

from hive.core.redact import Redactor, is_secret_path, load_env_secrets, redact, redact_transcript

HR_KEY = "sk-hr-" + "a1B2c3D4e5F6g7H8i9J0"
OPENAI_KEY = "sk-" + "proj-" + "Zx9" * 8
GH_TOKEN = "ghp_" + "R4nd0mT0k3n" * 3
GH_PAT = "github_pat_" + "11ABCDEFG" * 4
AWS_ID = "AKIA" + "IOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"
JWT = (
    "eyJ"
    + "hbGciOiJIUzI1NiJ9"
    + ".eyJ"
    + "zdWIiOiIxMjM0NTY3ODkwIn0"
    + "."
    + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)
PEM = (
    "-----BEGIN "
    + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0Z3VS5JJcds3\nxfn/ygWyF8PbnGy0AHB7\n-----END "
    + "RSA PRIVATE KEY-----"
)
DB_PASSWORD = "hunter2-correct-horse"

ALL_SECRETS = [HR_KEY, OPENAI_KEY, GH_TOKEN, GH_PAT, AWS_ID, AWS_SECRET, JWT, PEM, DB_PASSWORD]


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (f"key is {HR_KEY} ok", "harnessrouter_key"),
        (f"OPENAI={OPENAI_KEY}", "api_key"),
        (f"push with {GH_TOKEN}", "github_token"),
        (f"push with {GH_PAT}", "github_token"),
        (f"id {AWS_ID}", "aws_access_key"),
        (f"aws_secret_access_key = {AWS_SECRET}", "aws_secret"),
        (f"Authorization: Bearer {JWT}", "jwt"),
        (f"key:\n{PEM}\nafter", "private_key"),
    ],
)
def test_patterns(text, kind):
    out = redact(text, env_values={})
    assert f"[REDACTED:{kind}]" in out
    for s in ALL_SECRETS:
        assert s not in out


def test_assignments_keep_the_name():
    out = redact(
        f"password={DB_PASSWORD} and token: \"abc123xyz789\" and api_key = 'k-998877'", env_values={}
    )
    assert "password=[REDACTED:password]" in out
    assert "token: [REDACTED:token]" in out
    assert "api_key = [REDACTED:api_key]" in out
    assert DB_PASSWORD not in out and "abc123xyz789" not in out


@pytest.mark.parametrize(
    "text",
    [
        "set max_tokens: 4096 and the password policy is strict; token budget = 2000",
        "token_estimate=estimate_tokens(markdown),",
        "api_key: str,\n    base_url: str = DEFAULT",
        "token = None",
        "secret_ids: list[str] = []",
        "self.api_key = settings.require_key()",
        "compose_brief(store=store, budget_tokens=full.token_estimate - 5)",
        "scrub `password=`/`token=` assignments (so `max_tokens: 4096` survives)",
        'PLACEHOLDER_TOKEN = "[REDACTED:secret-file]"',
    ],
)
def test_assignments_leave_code_and_prose_alone(text):
    assert redact(text, env_values={}) == text


def test_env_values_redacted_except_config_and_short_values():
    env = {
        "MY_DB_URL": "postgres://u:pw@db.internal/x",
        "HIVE_STORE": "sqlite",
        "HIVE_PROJECT": "hackathon-x",
        "SHORT": "abc",
    }
    out = redact(
        "db postgres://u:pw@db.internal/x store sqlite project hackathon-x short abc", env_values=env
    )
    assert "postgres://u:pw@db.internal/x" not in out and "[REDACTED:env:MY_DB_URL]" in out
    assert "sqlite" in out and "hackathon-x" in out and "short abc" in out


def test_load_env_secrets_reads_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(f"HARNESS_ROUTER_KEY={HR_KEY}  # comment\nHIVE_STORE=sqlite\n")
    monkeypatch.chdir(tmp_path)
    env = load_env_secrets()
    assert env["HARNESS_ROUTER_KEY"] == HR_KEY
    assert redact(f"x {HR_KEY} y", env_values=env).count("[REDACTED:") == 1


def test_idempotent():
    once = redact(f"password={DB_PASSWORD} {GH_TOKEN}", env_values={})
    assert redact(once, env_values={}) == once


@pytest.mark.parametrize(
    ("path", "secret"),
    [
        (".env", True),
        ("/repo/.env.local", True),
        ("certs/server.pem", True),
        ("~/.ssh/id_ed25519", True),
        (".env.example", False),
        ("hive/env.py", False),
        ("docs/identity.md", False),
    ],
)
def test_is_secret_path(path, secret):
    assert is_secret_path(path) is secret


def _line(i, typ, content):
    return json.dumps(
        {
            "type": typ,
            "uuid": f"u{i}",
            "message": {"role": "user" if typ == "user" else "assistant", "content": content},
        }
    )


def test_transcript_fixture_with_every_secret_comes_out_clean():
    lines = [
        _line(1, "user", f"Use this key {HR_KEY} and deploy with {GH_TOKEN}"),
        _line(
            2,
            "assistant",
            [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/repo/.env"}}],
        ),
        _line(
            3,
            "user",
            [{"type": "tool_result", "tool_use_id": "t1", "content": "NOT_PATTERNED_SECRET=zzz-qqq-111"}],
        ),
        _line(
            4,
            "assistant",
            [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "cat ~/.ssh/id_rsa"}}],
        ),
        _line(
            5,
            "user",
            [{"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "opaque"}]}],
        ),
        _line(
            6, "assistant", [{"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "env"}}]
        ),
        _line(
            7,
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "t3",
                    "content": f"AWS_ACCESS_KEY_ID={AWS_ID}\naws_secret_access_key={AWS_SECRET}\nJWT={JWT}\n"
                    f"OPENAI={OPENAI_KEY}\nPAT={GH_PAT}\npassword={DB_PASSWORD}\n{PEM}",
                }
            ],
        ),
        _line(8, "assistant", [{"type": "text", "text": "Done; I kept the README as is."}]),
    ]
    out = redact_transcript("\n".join(lines) + "\n", env_values={})
    for s in [*ALL_SECRETS, "NOT_PATTERNED_SECRET=zzz-qqq-111", "opaque"]:
        assert s not in out
    parsed = [json.loads(x) for x in out.splitlines()]
    assert len(parsed) == 8  # still valid JSONL, nothing dropped wholesale
    assert parsed[2]["message"]["content"][0]["content"] == "[REDACTED:secret-file]"
    assert parsed[4]["message"]["content"][0]["content"] == "[REDACTED:secret-file]"
    assert parsed[7]["message"]["content"][0]["text"] == "Done; I kept the README as is."


def test_transcript_skips_unparseable_lines_safely():
    out = redact_transcript(f"not json {GH_TOKEN}\n", env_values={})
    assert GH_TOKEN not in out


def test_redactor_obj_walks_nested_structures():
    r = Redactor(env_values={})
    obj = {"a": [GH_TOKEN, {"b": f"x {JWT}"}], "n": 3, "none": None}
    out = r.redact_obj(obj)
    assert out["n"] == 3 and out["none"] is None
    assert GH_TOKEN not in json.dumps(out) and JWT not in json.dumps(out)
