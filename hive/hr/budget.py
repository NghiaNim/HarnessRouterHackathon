"""Cost guardrail for HarnessRouter spend (T-26).

Every `HRClient.run()` is checked against today's (UTC) spend before any request is sent, and its cost is
appended to a JSONL ledger afterwards. Cost = the response's own cost/usage field when present, else the run's
`max_cost_usd`, else `DEFAULT_UPPER_BOUND_USD` (all our harnesses cap at $1, see ARCHITECTURE.md). Estimated
costs are flagged `estimated: true`.

Config: `HIVE_DAILY_BUDGET_USD` (default 5), `HIVE_SPEND_PATH` (default `~/.cache/hive/spend.jsonl`).
"""

from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from hive.hr.client import HRError

DEFAULT_DAILY_BUDGET_USD = 5.0
DEFAULT_UPPER_BOUND_USD = 1.0
DEFAULT_PATH = Path.home() / ".cache" / "hive" / "spend.jsonl"
_COST_KEYS = ("cost_usd", "total_cost_usd", "cost")


class BudgetExceeded(HRError):
    def __init__(self, spent: float, budget: float, projected: float) -> None:
        super().__init__(
            "budget_exceeded",
            0,
            f"HarnessRouter daily budget reached: ${spent:.2f} spent today (+${projected:.2f} for this run) "
            f"of ${budget:.2f}. Raise HIVE_DAILY_BUDGET_USD or wait until tomorrow (UTC).",
        )
        self.spent, self.budget, self.projected = spent, budget, projected


def extract_cost(raw: dict | None) -> float | None:
    """Cost reported by a response, if any (top level or under `usage`)."""
    for container in (raw or {}, (raw or {}).get("usage") or {}):
        if not isinstance(container, dict):
            continue
        for key in _COST_KEYS:
            value = container.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                return float(value)
    return None


class SpendLedger:
    def __init__(
        self, path: str | Path = DEFAULT_PATH, daily_budget_usd: float = DEFAULT_DAILY_BUDGET_USD
    ) -> None:
        self.path = Path(path).expanduser()
        self.daily_budget_usd = daily_budget_usd
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> SpendLedger:
        raw_budget = os.environ.get("HIVE_DAILY_BUDGET_USD", "").strip()
        try:
            budget = float(raw_budget) if raw_budget else DEFAULT_DAILY_BUDGET_USD
        except ValueError as e:
            raise ValueError(f"HIVE_DAILY_BUDGET_USD must be a number, got {raw_budget!r}") from e
        return cls(os.environ.get("HIVE_SPEND_PATH") or DEFAULT_PATH, budget)

    # --- write ---------------------------------------------------------------

    def record(
        self,
        harness: str,
        cost_usd: float,
        *,
        estimated: bool,
        response_id: str | None = None,
        status: str | None = None,
        at: datetime | None = None,
    ) -> dict:
        entry = {
            "at": (at or datetime.now(UTC)).astimezone(UTC).isoformat(),
            "harness": harness,
            "cost_usd": round(float(cost_usd), 6),
            "estimated": estimated,
            "response_id": response_id,
            "status": status,
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        return entry

    def record_run(
        self, harness: str, raw: dict | None, *, max_cost_usd: float | None = None, **kw: Any
    ) -> dict:
        cost = extract_cost(raw)
        if cost is not None:
            return self.record(harness, cost, estimated=False, **kw)
        bound = max_cost_usd if max_cost_usd is not None else DEFAULT_UPPER_BOUND_USD
        return self.record(harness, bound, estimated=True, **kw)

    # --- read ----------------------------------------------------------------

    def entries(self, day: date | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        day = day or datetime.now(UTC).date()
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                if datetime.fromisoformat(entry["at"]).astimezone(UTC).date() == day:
                    out.append(entry)
            except (ValueError, KeyError, TypeError):
                continue  # a torn/corrupt line must never break the guardrail
        return out

    def by_harness(self, day: date | None = None) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for e in self.entries(day):
            totals[e["harness"]] += float(e.get("cost_usd") or 0)
        return dict(totals)

    def total(self, day: date | None = None) -> float:
        return sum(self.by_harness(day).values())

    # --- enforce -------------------------------------------------------------

    def check(self, projected_usd: float | None = None) -> None:
        """Raise BudgetExceeded if today's spend plus this run's projected cost would pass the budget."""
        spent = self.total()
        projected = projected_usd or 0.0
        if spent >= self.daily_budget_usd or spent + projected > self.daily_budget_usd:
            raise BudgetExceeded(spent, self.daily_budget_usd, projected)


def format_spend(ledger: SpendLedger, names: dict[str, str] | None = None, day: date | None = None) -> str:
    """Today's spend per harness, for `hive spend`. `names` maps harness id -> name (harnessrouter.json)."""
    names = names or {}
    totals = ledger.by_harness(day)
    estimated = {e["harness"] for e in ledger.entries(day) if e.get("estimated")}
    lines = [f"HarnessRouter spend {(day or datetime.now(UTC).date()).isoformat()} (UTC)"]
    for harness, cost in sorted(totals.items(), key=lambda kv: -kv[1]):
        flag = " (upper bound)" if harness in estimated else ""
        lines.append(f"  {names.get(harness, harness):<24} ${cost:.4f}{flag}")
    lines.append(f"  {'total':<24} ${sum(totals.values()):.4f} of ${ledger.daily_budget_usd:.2f} budget")
    return "\n".join(lines)
