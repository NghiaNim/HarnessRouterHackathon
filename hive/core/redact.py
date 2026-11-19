"""Secret redaction before anything leaves the machine (T-28).

Call `redact()` / `redact_transcript()` / `Redactor.redact_obj()` on every outbound path: file uploads,
memory writes, harness inputs. Each secret becomes `[REDACTED:<kind>]`; assignments keep their name
(`password=[REDACTED:password]`).

Sources of secrets:
- values of the local `.env` (nearest one from cwd), except known non-secret config keys and values < 8 chars,
  which would otherwise erase ordinary words like "sqlite" everywhere;
- well-known token shapes (HarnessRouter / OpenAI-style `sk-…`, GitHub, AWS, JWT, PEM private keys);
- `password=` / `token=` / `secret=` / `api_key=` style assignments.
In transcripts, a tool result whose tool call read a secret file (`.env*`, `*.pem`, `id_*`) is dropped
entirely.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import PurePath
from typing import Any

from dotenv import dotenv_values, find_dotenv

SECRET_FILE_PLACEHOLDER = "[REDACTED:secret-file]"
MIN_ENV_VALUE_LEN = 8
NON_SECRET_ENV_KEYS = frozenset(
    {
        "HIVE_PROJECT",
        "HIVE_STORE",
        "HIVE_API_URL",
        "HIVE_DB_PATH",
        "HIVE_MEMBER",
        "HARNESS_ROUTER_URL",
        "HIVE_LIVE",
    }
)

# Order matters: specific shapes before generic ones.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S),
    ),
    ("harnessrouter_key", re.compile(r"\bsk-hr-[A-Za-z0-9_-]{8,}")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws_secret", re.compile(r"(?i)(?<=aws_secret_access_key)(\s*[=:]\s*)[\"']?[A-Za-z0-9/+=]{30,}[\"']?")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
]

# name = value, where name contains password/token/secret/api_key. Value must look like a credential:
# no spaces, >= 4 chars, not purely numeric (so `max_tokens: 4096` survives), not already redacted.
_ASSIGNMENT = re.compile(
    r"(?i)\b(?P<name>[A-Za-z0-9_]*?(?P<kind>password|passwd|token|secret|api_?key)[A-Za-z0-9_]*)"
    r"(?P<sep>[ \t]*[=:][ \t]*)"
    # quoted, or bare up to a delimiter; a bare value followed by `(`/`[`/`.` is code, not a credential
    r"(?P<value>\"[^\"\n]{4,}\"|'[^'\n]{4,}'|(?!\[REDACTED)[^\s\"'`,;()\[\]{}]{4,}(?=[\s\"'`,;)\]}]|$))"
)
# All-lowercase identifiers/dotted names are variables. Trade-off: an all-lowercase-letters password
# (`password=letmein`) is not caught by this layer; real credentials nearly always have digits/case/symbols.
_VARIABLE = re.compile(r"[a-z_][a-z_.]*")
_CODE_LITERALS = frozenset({"none", "true", "false", "null", "undefined", "str", "bool", "optional"})

_SECRET_NAME = re.compile(r"^(?:\.env(?:\..+)?|.+\.pem|id_[A-Za-z0-9_]+(?:\.pub)?)$")
_PATH_TOKEN = re.compile(r"[^\s\"'`;|&<>()]+")


def is_secret_path(path: str) -> bool:
    """`.env*` (but not `.env.example`/`.env.sample`), `*.pem`, `id_*` (SSH keys)."""
    name = PurePath(path).name
    if name in {".env.example", ".env.sample", ".env.template"}:
        return False
    return bool(_SECRET_NAME.match(name))


def load_env_secrets(path: str | None = None) -> dict[str, str]:
    """Values from the nearest `.env` (or `path`) that should be treated as secrets."""
    path = path or find_dotenv(usecwd=True)
    if not path:
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v}


class Redactor:
    def __init__(self, env_values: Mapping[str, str] | None = None) -> None:
        env = load_env_secrets() if env_values is None else env_values
        pairs = [
            (k, v.strip())
            for k, v in env.items()
            if k not in NON_SECRET_ENV_KEYS and v and len(v.strip()) >= MIN_ENV_VALUE_LEN
        ]
        self._env = sorted(pairs, key=lambda kv: len(kv[1]), reverse=True)  # longest first

    def redact(self, text: str) -> str:
        for key, value in self._env:
            text = text.replace(value, f"[REDACTED:env:{key}]")
        for kind, pattern in _PATTERNS:
            if kind == "aws_secret":
                text = pattern.sub(lambda m: f"{m.group(1)}[REDACTED:aws_secret]", text)
            else:
                text = pattern.sub(f"[REDACTED:{kind}]", text)
        return _ASSIGNMENT.sub(self._assignment, text)

    @staticmethod
    def _assignment(m: re.Match[str]) -> str:
        value = m.group("value").strip("\"'")
        if (
            value.isdigit()
            or value.lower() in _CODE_LITERALS
            or "[REDACTED" in value
            or _VARIABLE.fullmatch(
                value
            )  # `budget_tokens=budget`, `token=self.token`: code, not a credential
        ):
            return m.group(0)
        kind = m.group("kind").lower().replace("passwd", "password").replace("apikey", "api_key")
        return f"{m.group('name')}{m.group('sep')}[REDACTED:{kind}]"

    def redact_obj(self, obj: Any) -> Any:
        """Redact every string inside a JSON-like structure (keys are left alone)."""
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, list):
            return [self.redact_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.redact_obj(v) for k, v in obj.items()}
        return obj

    def redact_transcript(self, jsonl: str) -> str:
        """Redact a Claude Code JSONL transcript; tool results of secret-file reads are replaced wholesale."""
        out: list[str] = []
        secret_tool_ids: set[str] = set()
        for line in jsonl.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                out.append(self.redact(line))
                continue
            for block in _content_blocks(event):
                if block.get("type") == "tool_use" and _reads_secret_file(block.get("input")):
                    secret_tool_ids.add(block.get("id"))
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in secret_tool_ids:
                    block["content"] = SECRET_FILE_PLACEHOLDER
            out.append(json.dumps(self.redact_obj(event), ensure_ascii=False))
        return "\n".join(out) + ("\n" if out else "")


def _content_blocks(event: Any) -> list[dict]:
    content = (event.get("message") or {}).get("content") if isinstance(event, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _reads_secret_file(tool_input: Any) -> bool:
    if not isinstance(tool_input, dict):
        return False
    for key in ("file_path", "path", "notebook_path"):
        if isinstance(tool_input.get(key), str) and is_secret_path(tool_input[key]):
            return True
    command = tool_input.get("command")
    return isinstance(command, str) and any(is_secret_path(tok) for tok in _PATH_TOKEN.findall(command))


def redact(text: str, *, env_values: Mapping[str, str] | None = None) -> str:
    return Redactor(env_values).redact(text)


def redact_transcript(jsonl: str, *, env_values: Mapping[str, str] | None = None) -> str:
    return Redactor(env_values).redact_transcript(jsonl)
