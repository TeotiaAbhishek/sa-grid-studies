"""Shared helpers: loading a window, data snapshots, statistics and report output."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from sagrid.pipeline import git_commit
from sagrid.schemas import DATASETS
from sagrid.store import Store, ident

FIVE_MIN = pd.Timedelta(minutes=5)
INTERCONNECTOR_NAMES = {"V-SA": "HEYWOOD", "V-S-MNSP1": "MURRAYLINK"}


@dataclass
class Window:
    start: date
    end: date

    @property
    def lo(self) -> datetime:
        """Exclusive lower bound on interval-ending timestamps."""
        return datetime.combine(self.start, datetime.min.time())

    @property
    def hi(self) -> datetime:
        """Inclusive upper bound on interval-ending timestamps."""
        return datetime.combine(self.end + timedelta(days=1), datetime.min.time())

    @property
    def n_intervals(self) -> int:
        return ((self.end - self.start).days + 1) * 288


@dataclass
class Report:
    """What an investigation produced, before it is written to disk."""

    key: str
    title: str
    markdown: str
    numbers: dict
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    figures: dict[str, object] = field(default_factory=dict)  # name -> matplotlib Figure


def tables_in(store: Store) -> set[str]:
    return {r[0] for r in store.con.execute("SHOW TABLES").fetchall()}


def read_window(
    store: Store, table: str, w: Window, extend: timedelta = timedelta(0)
) -> pd.DataFrame:
    """Rows of a dataset table whose interval falls inside the window (plus `extend` after it)."""
    ds = DATASETS[table]
    col = ident(ds.time_col.lower()).upper()
    if ds.time_marks == "end":
        cond = f'"{col}" > ? AND "{col}" <= ?'
    else:
        cond = f'"{col}" >= ? AND "{col}" < ?'
    sql = f'SELECT * FROM "{ident(table)}" WHERE {cond} ORDER BY ALL'  # noqa: S608 - checked
    return store.con.execute(sql, [w.lo, w.hi + extend]).df()


def snapshot(store: Store, w: Window, tables: list[str]) -> dict:
    """Row count and content hash of each table inside the window.

    Printed at the top of every report so a reader can check that a re-run
    used exactly the same data (AC-I1).
    """
    out: dict = {
        "window": f"{w.start} to {w.end}",
        "git_commit": git_commit(),
        "tables": {},
    }
    present = tables_in(store)
    for t in tables:
        if t not in present:
            out["tables"][t] = {"rows": 0, "hash": "absent"}
            continue
        df = read_window(store, t, w)
        h = pd.util.hash_pandas_object(df, index=False)
        acc = 0
        for v in h.to_numpy(dtype=np.uint64).tolist():  # order-independent, like Store.table_hash
            acc ^= v
        digest = format(acc, "016x")
        out["tables"][t] = {"rows": int(len(df)), "hash": digest}
    return out


def interval_frame(store: Store, w: Window) -> pd.DataFrame:
    """One row per 5-minute SA interval with demand, price, flows and rooftop PV."""
    rs = read_window(store, "region_summary", w)
    if rs.empty:
        raise ValueError(f"no region_summary rows for {w.start} to {w.end}; run sagrid ingest")
    wanted = [
        "SETTLEMENTDATE",
        "TOTALDEMAND",
        "DISPATCHABLEGENERATION",
        "NETINTERCHANGE",
        "SS_WIND_CLEAREDMW",
        "SS_SOLAR_CLEAREDMW",
        "SS_WIND_UIGF",
        "SS_SOLAR_UIGF",
    ]
    df = rs[[c for c in wanted if c in rs]].copy()
    if {"SS_WIND_UIGF", "SS_WIND_CLEAREDMW", "SS_SOLAR_UIGF", "SS_SOLAR_CLEAREDMW"} <= set(df):
        # Output the wind and solar farms could have produced but were not dispatched for.
        df["CURTAILMENT"] = (df["SS_WIND_UIGF"] - df["SS_WIND_CLEAREDMW"]).clip(lower=0) + (
            df["SS_SOLAR_UIGF"] - df["SS_SOLAR_CLEAREDMW"]
        ).clip(lower=0)
    present = tables_in(store)
    if "price" in present:
        price = read_window(store, "price", w)[["SETTLEMENTDATE", "RRP"]]
        df = df.merge(price, on="SETTLEMENTDATE", how="left")
    if "interconnector" in present:
        ic = read_window(store, "interconnector", w)
        for icid, label in INTERCONNECTOR_NAMES.items():
            part = ic[ic["INTERCONNECTORID"] == icid][
                ["SETTLEMENTDATE", "MWFLOW", "EXPORTLIMIT", "IMPORTLIMIT"]
            ].rename(
                columns={
                    "MWFLOW": f"{label}_FLOW",
                    "EXPORTLIMIT": f"{label}_EXPORTLIMIT",
                    "IMPORTLIMIT": f"{label}_IMPORTLIMIT",
                }
            )
            df = df.merge(part, on="SETTLEMENTDATE", how="left")
    if "rooftop_pv" in present:
        pv = read_window(store, "rooftop_pv", w)
        if not pv.empty:
            # A 5-minute interval ending at t lies in the half hour starting floor(t - 5min).
            period = (df["SETTLEMENTDATE"] - FIVE_MIN).dt.floor("30min")
            lookup = pv.set_index("INTERVAL_DATETIME")["POWER"]
            df["ROOFTOP_PV"] = period.map(lookup).to_numpy()
    start = df["SETTLEMENTDATE"] - FIVE_MIN
    df["HOUR"] = start.dt.hour  # hour in which the interval starts
    df["DAY"] = start.dt.normalize()
    return df.sort_values("SETTLEMENTDATE").reset_index(drop=True)


def at_limit(
    flow: pd.Series, export_limit: pd.Series, import_limit: pd.Series, pct: float = 0.05
) -> pd.Series:
    """Which limit, if any, the flow is within `pct` of: "export", "import" or "".

    The tolerance has a 5 MW floor so a limit near zero doesn't make every
    small flow look binding.
    """
    tol_e = np.maximum(pct * export_limit.abs(), 5.0)
    tol_i = np.maximum(pct * import_limit.abs(), 5.0)
    near_e = (export_limit - flow) <= tol_e
    near_i = (flow - import_limit) <= tol_i
    out = pd.Series("", index=flow.index, dtype=object)
    out[near_i] = "import"
    out[near_e] = "export"  # if both (limits pinched together), call it export
    out[flow.isna() | export_limit.isna() | import_limit.isna()] = ""
    return out


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion k/n."""
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def hour_adjusted_difference(
    values: pd.Series, flag: pd.Series, hour: pd.Series
) -> tuple[float, int]:
    """Mean of `values` when `flag` is true minus when false, compared within each hour.

    Each hour's difference is weighted by the number of flagged intervals in
    it. Hours where either group is empty are skipped. This removes the
    part of a raw difference that only reflects time of day. Returns
    (difference, flagged intervals used).
    """
    d = pd.DataFrame({"v": values, "f": flag.astype(bool), "h": hour}).dropna()
    total, weight = 0.0, 0
    for _, g in d.groupby("h"):
        on, off = g.loc[g["f"], "v"], g.loc[~g["f"], "v"]
        if len(on) and len(off):
            total += (on.mean() - off.mean()) * len(on)
            weight += len(on)
    return (total / weight if weight else math.nan, weight)


def runs(flag: pd.Series) -> pd.DataFrame:
    """Consecutive True runs in a boolean series: start position, length, and edge flags."""
    f = flag.fillna(False).astype(bool).to_numpy()
    if not f.any():
        return pd.DataFrame(columns=["start", "length", "at_start", "at_end"])
    padded = np.concatenate([[False], f, [False]])
    edges = np.flatnonzero(np.diff(padded.astype(int)))
    starts, ends = edges[0::2], edges[1::2]
    return pd.DataFrame(
        {
            "start": starts,
            "length": ends - starts,
            "at_start": starts == 0,
            "at_end": ends == len(f),
        }
    )


def kaplan_meier(durations: np.ndarray, observed: np.ndarray) -> pd.DataFrame:
    """Survival curve S(t) = P(episode lasts longer than t).

    Episodes cut off by the start or end of the window are censored: we know
    they lasted at least that long, not how long.
    """
    order = np.argsort(durations)
    d, o = durations[order], observed[order]
    times = np.unique(d[o.astype(bool)])
    surv, s = [], 1.0
    for t in times:
        at_risk = (d >= t).sum()
        events = ((d == t) & o.astype(bool)).sum()
        s *= 1 - events / at_risk
        surv.append(s)
    return pd.DataFrame({"duration": times, "survival": surv})


def fmt(x: float, nd: int = 1) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:,.{nd}f}"


def pct(x: float, nd: int = 1) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{100 * x:.{nd}f}%"


def md_table(df: pd.DataFrame, floatfmt: str = "{:,.2f}") -> str:
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for row in df.itertuples(index=False):
        cells = []
        for v in row:
            if isinstance(v, float):
                cells.append("n/a" if math.isnan(v) else floatfmt.format(v))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def snapshot_md(snap: dict) -> str:
    rows = [f"| {t} | {v['rows']:,} | `{v['hash']}` |" for t, v in snap["tables"].items()]
    return "\n".join(
        [
            f"Data window: {snap['window']} (AEMO market time). "
            f"Code: commit `{snap['git_commit']}`.",
            "",
            "| Table | Rows in window | Content hash |",
            "| --- | --- | --- |",
            *rows,
        ]
    )


def pyplot():
    """matplotlib.pyplot on the file-only backend, so no window opens on Windows."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def write_report(report: Report, out_dir: Path) -> list[Path]:
    """Write <key>.md, <key>.json (every number), figures and CSV tables."""
    out_dir = Path(out_dir)
    assets = out_dir / report.key
    assets.mkdir(parents=True, exist_ok=True)
    written = []
    md = out_dir / f"{report.key}.md"
    md.write_text(report.markdown, encoding="utf-8")
    written.append(md)
    js = out_dir / f"{report.key}.json"
    js.write_text(json.dumps(report.numbers, indent=2, default=str), encoding="utf-8")
    written.append(js)
    for name, df in report.tables.items():
        p = assets / f"{name}.csv"
        df.to_csv(p, index=False)
        written.append(p)
    for name, fig in report.figures.items():
        p = assets / f"{name}.png"
        fig.savefig(p, dpi=120, bbox_inches="tight")  # type: ignore[attr-defined]
        pyplot().close(fig)
        written.append(p)
    return written
