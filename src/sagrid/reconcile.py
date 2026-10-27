"""Reconciliation checks: do independent numbers agree?

Check 1 (region balance), from DISPATCH_REGIONSUM alone:

    TOTALDEMAND ~= DISPATCHABLEGENERATION - DISPATCHABLELOAD - NETINTERCHANGE

NETINTERCHANGE is the region's net export (positive = SA exporting).
On real data (first week, Oct 2026) the two sides agree to within 0.01 MW,
so a 1 MW tolerance flags any parsing or typing error. Because AEMO
computes TOTALDEMAND this way, this is a consistency check; check 2 is
the independent one.

Check 2 (SCADA vs dispatch), needs the unit registry:

    sum of SA dispatched units' SCADA output ~= DISPATCHABLEGENERATION

DISPATCHABLEGENERATION covers scheduled and semi-scheduled units,
including the generating side of batteries (bidirectional units), so
those are summed too: a battery's positive SCADA reading is output, a
negative one is charging and belongs to DISPATCHABLELOAD instead.
Non-scheduled units are left out because dispatch does not include them.
DISPATCH_UNIT_SCADA stamped t is the reading at the start of interval t
(on real data it equals UNIT_SOLUTION.INITIALMW for t exactly), so the
output at the end of interval t is the reading stamped t + 5 minutes.
SCADA is metered output and dispatch is a target, so this tolerance is wider.
Both tolerances and the evidence behind them are in docs/decisions.md.
"""

from __future__ import annotations

import pandas as pd

BALANCE_TOLERANCE_MW = 1.0
SCADA_TOLERANCE_MW = 150.0
SCADA_LAG = pd.Timedelta(minutes=5)


def region_balance(regionsum: pd.DataFrame, tol_mw: float = BALANCE_TOLERANCE_MW) -> pd.DataFrame:
    df = regionsum[
        [
            "SETTLEMENTDATE",
            "TOTALDEMAND",
            "DISPATCHABLEGENERATION",
            "DISPATCHABLELOAD",
            "NETINTERCHANGE",
        ]
    ].copy()
    df["implied_demand"] = (
        df["DISPATCHABLEGENERATION"] - df["DISPATCHABLELOAD"] - df["NETINTERCHANGE"]
    )
    df["residual_mw"] = df["TOTALDEMAND"] - df["implied_demand"]
    df["flag"] = df["residual_mw"].abs() > tol_mw
    return df


def scada_vs_dispatch(
    regionsum: pd.DataFrame,
    scada: pd.DataFrame,
    registry: pd.DataFrame,
    tol_mw: float = SCADA_TOLERANCE_MW,
) -> pd.DataFrame:
    """Compare summed SA generation SCADA with dispatched generation per interval."""
    keep = (registry["REGIONID"] == "SA1") & registry["DISPATCHTYPE"].isin(
        ["GENERATOR", "BIDIRECTIONAL"]
    )
    if "SCHEDULE_TYPE" in registry:  # the hand-downloaded workbook has no schedule type
        keep &= registry["SCHEDULE_TYPE"].isin(["SCHEDULED", "SEMI-SCHEDULED"])
    s = scada[scada["DUID"].isin(registry.loc[keep, "DUID"])]
    s = s.assign(SETTLEMENTDATE=s["SETTLEMENTDATE"] - SCADA_LAG)  # reading at end of interval
    storage = s["DUID"].isin(registry.loc[registry["DISPATCHTYPE"] == "BIDIRECTIONAL", "DUID"])
    value = s["SCADAVALUE"].where(~storage, s["SCADAVALUE"].clip(lower=0))
    total = value.groupby(s["SETTLEMENTDATE"]).sum().rename("scada_generation").reset_index()
    df = regionsum[["SETTLEMENTDATE", "DISPATCHABLEGENERATION"]].merge(
        total, on="SETTLEMENTDATE", how="left"
    )
    df["residual_mw"] = df["scada_generation"] - df["DISPATCHABLEGENERATION"]
    df["flag"] = df["residual_mw"].isna() | (df["residual_mw"].abs() > tol_mw)
    return df


def summarise(check: pd.DataFrame) -> dict:
    r = check["residual_mw"].dropna()
    return {
        "intervals": int(len(check)),
        "flagged": int(check["flag"].sum()),
        "residual_mean_mw": round(float(r.mean()), 2) if len(r) else None,
        "residual_p95_abs_mw": round(float(r.abs().quantile(0.95)), 2) if len(r) else None,
    }
