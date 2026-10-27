"""Power BI exports: a small star schema as Parquet and CSV.

Facts: one table per dataset at 5-minute grain. Unit SCADA readings carry
the I3 issue they fall in (SCADA_ISSUE) and IS_SUSPECT, so a report can mask
bad readings instead of losing them. Constraint rows carry IS_PENALTY.
Dimensions: date (one row per day), unit (from the registry), interconnector.
Power BI loads these directly; no Power Query transformation is needed.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from sagrid.investigations.i1_constraints import PENALTY_MV
from sagrid.investigations.i3_scada import SUSPECT_KINDS, flag_readings, sa_units, screen_units
from sagrid.schemas import DATASETS
from sagrid.store import Store

INTERCONNECTORS = pd.DataFrame(
    {
        "INTERCONNECTORID": ["V-SA", "V-S-MNSP1"],
        "NAME": ["Heywood", "Murraylink"],
        "TYPE": ["AC", "DC (market network service provider)"],
        "FROM_REGION": ["VIC1", "VIC1"],
        "TO_REGION": ["SA1", "SA1"],
    }
)


def _write(df: pd.DataFrame, out_dir: Path, name: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    pq, csv = out_dir / f"{name}.parquet", out_dir / f"{name}.csv"
    df.to_parquet(pq, index=False)
    df.to_csv(csv, index=False)
    return [pq, csv]


def date_dimension(timestamps: pd.Series) -> pd.DataFrame:
    # An interval ending at 00:00 belongs to the previous day.
    days = (pd.to_datetime(timestamps) - pd.Timedelta(minutes=5)).dt.normalize().drop_duplicates()
    d = pd.DataFrame({"DATE": days.sort_values().reset_index(drop=True)})
    d["YEAR"] = d["DATE"].dt.year
    d["MONTH"] = d["DATE"].dt.month
    d["MONTH_NAME"] = d["DATE"].dt.strftime("%b")
    d["WEEKDAY"] = d["DATE"].dt.strftime("%a")
    d["IS_WEEKEND"] = d["DATE"].dt.day_of_week >= 5
    d["SEASON"] = d["MONTH"].map(
        {
            12: "Summer",
            1: "Summer",
            2: "Summer",
            3: "Autumn",
            4: "Autumn",
            5: "Autumn",
            6: "Winter",
            7: "Winter",
            8: "Winter",
            9: "Spring",
            10: "Spring",
            11: "Spring",
        }
    )
    return d


def scada_flags(
    store: Store, scada: pd.DataFrame, registry: pd.DataFrame | None
) -> tuple[pd.Series, pd.Series]:
    """I3's issue and a suspect flag for each reading, by the same rules as I3.

    Checked readings with no issue are "none". Readings I3 doesn't screen (no
    registry, or a unit outside SA) get blanks (null), not "none" or False, so
    "not checked" is never read as "checked and fine".
    """
    issue = pd.Series(pd.NA, index=scada.index, dtype="string")
    suspect = pd.Series(pd.NA, index=scada.index, dtype="boolean")
    if registry is None:
        return issue, suspect
    units = sa_units(registry)
    checked = scada["DUID"].isin(units["DUID"])
    sa = scada[checked]
    if sa.empty:
        return issue, suspect
    grid = pd.date_range(sa["SETTLEMENTDATE"].min(), sa["SETTLEMENTDATE"].max(), freq="5min")
    targets = None
    if "unit_solution" in {r[0] for r in store.con.execute("SHOW TABLES").fetchall()}:
        targets = store.con.execute(
            "SELECT SETTLEMENTDATE, DUID, TOTALCLEARED, AVAILABILITY FROM unit_solution"
        ).df()
        targets = targets[targets["DUID"].isin(units["DUID"])]
        targets = targets if len(targets) else None
    issues, _ = screen_units(sa, units, grid, targets)
    found = flag_readings(scada, issues)
    # Power BI shows both "" and null as (Blank), so name the clean state.
    issue[checked] = found[checked].replace("", "none")
    suspect[checked] = found[checked].isin(SUSPECT_KINDS)
    return issue, suspect


def export_powerbi(store: Store, out_dir: Path, registry: pd.DataFrame | None = None) -> list[Path]:
    written: list[Path] = []
    existing = {r[0] for r in store.con.execute("SHOW TABLES").fetchall()}
    all_ts = []
    for name, ds in DATASETS.items():
        if name not in existing:
            continue
        df = store.table(name)
        # An interval-ending stamp at 00:00 belongs to the previous day.
        shift = pd.Timedelta(minutes=5) if ds.time_marks == "end" else pd.Timedelta(0)
        df["DATE"] = (df[ds.time_col] - shift).dt.normalize()
        if name == "unit_scada":
            df["SCADA_ISSUE"], df["IS_SUSPECT"] = scada_flags(store, df, registry)
        if name == "constraint":
            df["IS_PENALTY"] = df["MARGINALVALUE"].abs() >= PENALTY_MV
        all_ts.append(df["DATE"] + pd.Timedelta(minutes=5))  # date_dimension takes interval ends
        written += _write(df, out_dir, f"fact_{name}")
    if all_ts:
        written += _write(date_dimension(pd.concat(all_ts)), out_dir, "dim_date")
    written += _write(INTERCONNECTORS, out_dir, "dim_interconnector")
    if registry is not None:
        written += _write(registry, out_dir, "dim_unit")
    rejects = store.con.execute("SELECT run_id, dataset, source_file, reason FROM rejects").df()
    written += _write(rejects, out_dir, "fact_rejects")
    return written
