"""Post-run health checks: freshness, completeness, reject rate, reconciliation.

`sagrid check` exits non-zero when any check fails, so a scheduler (GitHub
Actions here) can raise an alert.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta

from sagrid.align import find_gaps
from sagrid.reconcile import region_balance, summarise
from sagrid.store import Store

NEM_UTC_OFFSET = timedelta(hours=10)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def run_checks(
    store: Store,
    start: date,
    end: date,
    max_staleness_hours: float,
    max_reject_rate: float,
    balance_tolerance_mw: float,
    now_utc: datetime | None = None,
) -> list[Check]:
    checks: list[Check] = []
    tables = {r[0] for r in store.con.execute("SHOW TABLES").fetchall()}
    if "region_summary" not in tables:
        return [Check("data_present", False, "region_summary table does not exist yet")]

    rs = store.con.execute(
        "SELECT * FROM region_summary WHERE SETTLEMENTDATE > ? AND SETTLEMENTDATE <= ?",
        [
            datetime.combine(start, datetime.min.time()),
            datetime.combine(end + timedelta(days=1), datetime.min.time()),
        ],
    ).df()

    latest = store.scalar("SELECT max(SETTLEMENTDATE) FROM region_summary")
    now_utc = now_utc or datetime.now(UTC).replace(tzinfo=None)
    now_market = now_utc + NEM_UTC_OFFSET
    age_h = (now_market - latest).total_seconds() / 3600 if latest else float("inf")
    checks.append(
        Check(
            "freshness",
            age_h <= max_staleness_hours,
            f"latest interval {latest}, {age_h:.1f} h old (limit {max_staleness_hours} h)",
        )
    )

    gaps = find_gaps(rs["SETTLEMENTDATE"], start, end)
    missing = int(gaps["n_intervals"].sum()) if len(gaps) else 0
    checks.append(
        Check(
            "completeness",
            missing == 0,
            f"{missing} missing 5-minute intervals in {len(gaps)} gap(s)",
        )
    )

    loaded = sum(store.count(t) for t in tables if t not in {"rejects", "runs", "loaded_files"})
    rejected = store.count("rejects")
    rate = rejected / (loaded + rejected) if (loaded + rejected) else 0.0
    checks.append(
        Check(
            "reject_rate",
            rate <= max_reject_rate,
            f"{rejected} rejected of {loaded + rejected} rows ({rate:.3%})",
        )
    )

    if len(rs):
        s = summarise(region_balance(rs, balance_tolerance_mw))
        share = s["flagged"] / s["intervals"]
        checks.append(Check("region_balance", share <= 0.01, json.dumps(s)))
    return checks


def report(checks: list[Check]) -> str:
    lines = ["| Check | Result | Detail |", "| --- | --- | --- |"]
    lines += [f"| {c.name} | {'PASS' if c.passed else 'FAIL'} | {c.detail} |" for c in checks]
    return "\n".join(lines)


def as_json(checks: list[Check]) -> str:
    return json.dumps([asdict(c) for c in checks], indent=2)
