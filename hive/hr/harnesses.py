"""Our background harnesses on HarnessRouter: idempotent provisioning plus a strict-JSON run helper.

    ids = ensure_harnesses(client)                       # create/update by name, write harnessrouter.json
    verdict = run_json(client, "hive-arbiter", text, ArbiterVerdict)

Prompts live in `prompts/<stem>.md`. `{{SCHEMA}}` in a prompt is replaced with the JSON schema of the
harness's output model at provisioning time, so prompts never drift from the pydantic models.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from hive.models import ArbiterVerdict, ModuleNote, Violation

if TYPE_CHECKING:
    from hive.hr.client import HRClient

T = TypeVar("T")

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts"
HARNESS_MAP_PATH = REPO_ROOT / "harnessrouter.json"
SCHEMA_PLACEHOLDER = "{{SCHEMA}}"


# --- harness output shapes (not shared contracts: T-15/T-53 map them onto hive.models) ---------------------


class ProposedDecision(BaseModel):
    title: str = Field(max_length=300)
    statement: str = Field(description="The rule, imperative")
    rationale: str = ""
    scope: list[str] = Field(default_factory=lambda: ["**"], description="Path globs the rule applies to")
    evidence: str | None = Field(default=None, description="Verbatim human quote the rule was mined from")


class DistilledSession(BaseModel):
    """What hive-distiller returns for one transcript. Hive fills in ids, member, branch and times."""

    summary: str
    ticket: str | None = None
    learned: list[str] = Field(default_factory=list)
    gotchas: list[str] = Field(default_factory=list)
    files_touched: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    proposed_decisions: list[ProposedDecision] = Field(default_factory=list)


class OracleAnswer(BaseModel):
    """What hive-oracle returns for `hive ask`."""

    answer: str
    citations: list[str] = Field(default_factory=list, description="e.g. 'handoff:<id>', 'decision:<id>'")
    found: bool = Field(description="False when the answer is not in the provided context")


# --- harness definitions -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class HarnessDef:
    name: str
    prompt: str  # file stem under prompts/
    output: Any  # pydantic model class, or a typing form such as list[Violation]
    model: str = "claude-haiku-5.5"
    base: str = "claude-code"
    max_cost_usd: float = 0.5
    timeout_seconds: int = 300
    max_step: int = 20

    def spec(self, prompts_dir: Path = PROMPTS_DIR) -> dict:
        return {
            "name": self.name,
            "base": self.base,
            "default_model": self.model,
            "system_prompt": render_prompt(self, prompts_dir),
            "max_cost_usd": self.max_cost_usd,
            "timeout_seconds": self.timeout_seconds,
            "max_step": self.max_step,
        }


# Budgets per ARCHITECTURE.md: max_cost_usd <= 1, timeout_seconds <= 600.
HARNESSES: dict[str, HarnessDef] = {
    h.name: h
    for h in [
        HarnessDef(
            "hive-distiller",
            "distiller",
            DistilledSession,
            max_cost_usd=1.0,
            timeout_seconds=600,
            max_step=40,
        ),
        HarnessDef("hive-guard", "guard", list[Violation]),
        HarnessDef("hive-arbiter", "arbiter", ArbiterVerdict),
        HarnessDef(
            "hive-oracle", "oracle", OracleAnswer, model="claude-sonnet-5.5", max_cost_usd=1.0, max_step=30
        ),
        # Stretch (T-21): provisioned only when asked for by name.
        HarnessDef(
            "hive-cartographer",
            "cartographer",
            list[ModuleNote],
            model="claude-sonnet-5.5",
            max_cost_usd=1.0,
            timeout_seconds=600,
            max_step=40,
        ),
    ]
}
CORE_HARNESSES = ["hive-distiller", "hive-guard", "hive-arbiter", "hive-oracle"]


class HarnessNotProvisioned(LookupError):
    """A harness name has no id in harnessrouter.json."""


class JSONOutputError(ValueError):
    """A harness reply that isn't valid JSON for the expected model. `.text` holds the raw reply."""

    def __init__(self, message: str, text: str) -> None:
        super().__init__(message)
        self.text = text


def render_prompt(defn: HarnessDef, prompts_dir: Path = PROMPTS_DIR) -> str:
    template = (prompts_dir / f"{defn.prompt}.md").read_text()
    schema = json.dumps(TypeAdapter(defn.output).json_schema(), indent=2)
    return template.replace(SCHEMA_PLACEHOLDER, schema)


# --- provisioning ------------------------------------------------------------------------------------------


def load_harness_ids(path: Path = HARNESS_MAP_PATH) -> dict[str, str]:
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else {}


def ensure_harnesses(
    client: HRClient,
    names: list[str] | None = None,
    *,
    path: Path = HARNESS_MAP_PATH,
    prompts_dir: Path = PROMPTS_DIR,
) -> dict[str, str]:
    """Create or update our harnesses **by name** (idempotent), then merge `{name: id}` into `path`.

    Existing harnesses are always PUT with the current spec, so prompt/budget edits roll out on the next run.
    Returns the ids of the harnesses ensured in this call.
    """
    names = list(names or CORE_HARNESSES)
    defs = [HARNESSES[n] for n in names]  # KeyError on an unknown name, before any API call

    existing: dict[str, str] = {}
    for h in client.harnesses():
        existing.setdefault(h.get("name", ""), h["id"])  # first wins if the workspace has duplicates

    ids: dict[str, str] = {}
    for defn in defs:
        spec = defn.spec(prompts_dir)
        if defn.name in existing:
            client.update_harness(existing[defn.name], spec)
            ids[defn.name] = existing[defn.name]
        else:
            ids[defn.name] = client.create_harness(spec)["id"]

    merged = {**load_harness_ids(path), **ids}
    Path(path).write_text(json.dumps(dict(sorted(merged.items())), indent=2) + "\n")
    return ids


def resolve_harness(name_or_id: str, path: Path = HARNESS_MAP_PATH) -> str:
    if name_or_id.startswith("chrn_"):
        return name_or_id
    ids = load_harness_ids(path)
    if name_or_id not in ids:
        raise HarnessNotProvisioned(f"harness {name_or_id!r} is not provisioned; run `hive init` first")
    return ids[name_or_id]


# --- strict JSON -------------------------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n?\s*```", re.DOTALL)


def _candidates(text: str) -> list[str]:
    out = [m.group(1) for m in _FENCE.finditer(text)]
    out.append(text.strip())
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if starts:
        start = min(starts)
        end = max(text.rfind("}"), text.rfind("]"))
        if end > start:
            out.append(text[start : end + 1])
    return out


def parse_json(text: str, model_cls: Any) -> Any:
    """Validate a harness reply as `model_cls`, tolerating code fences and prose around the JSON."""
    adapter = TypeAdapter(model_cls)
    errors: list[str] = []
    for candidate in _candidates(text):
        try:
            return adapter.validate_json(candidate)
        except ValidationError as e:
            errors.append(str(e).splitlines()[0] if str(e) else repr(e))
    raise JSONOutputError(errors[-1] if errors else "empty reply", text)


def run_json(
    client: HRClient,
    harness: str,
    input: Any,
    model_cls: Any,
    *,
    map_path: Path = HARNESS_MAP_PATH,
    **opts: Any,
) -> Any:
    """Run `harness` (name or id) and validate the reply as `model_cls`.

    On a parse failure it retries **once**, continuing the same session with the error (the input isn't
    resent). Raises `JSONOutputError` if the retry also fails. Extra kwargs go to `client.run`.
    """
    harness_id = resolve_harness(harness, map_path)
    result = client.run(harness_id, input, **opts)
    try:
        return parse_json(result.text, model_cls)
    except JSONOutputError as e:
        if not result.response_id:
            raise  # can't continue the session, and a retry without the input would be context-free
        fix = (
            f"Your reply was not valid JSON for the required schema ({e}). "
            "Reply again with ONLY the JSON value: no prose, no code fences."
        )
        cont = {"previous_response_id": result.response_id}
        if result.session_id:
            cont["metadata"] = {**opts.get("metadata", {}), "session_id": result.session_id}
        retry = client.run(harness_id, fix, **{**opts, **cont})
        return parse_json(retry.text, model_cls)
