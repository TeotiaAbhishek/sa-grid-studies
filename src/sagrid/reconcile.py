"""Reconciliation checks: do independent numbers agree?

Check 1 (region balance), from DISPATCH_REGIONSUM alone:

    TOTALDEMAND ~= DISPATCHABLEGENERATION - DISPATCHABLELOAD - NETINTERCHANGE

NETINTERCHANGE is the region's net export (positive = SA exporting).
The two sides differ by the region's share of interconnector losses, so we
allow a tolerance and flag intervals outside it.

Check 2 (SCADA vs dispatch), needs the unit registry:

    sum of SA generating units' SCADA output ~= DISPATCHABLEGENERATION

SCADA is metered output and dispatch is a target, so this tolerance is wider.
Both tolerances are documented in docs/decisions.md and should be tuned
from real data, not guessed.
"""

from __future__ import annotations

import pandas as pd

BALANCE_TOLERANCE_MW = 50.0
SCADA_TOLERANCE_MW = 150.0


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
    """Compare summed SA generator SCADA with dispatched generation per interval."""
    sa_gens = registry.loc[
        (registry["REGIONID"] == "SA1") & (registry["DISPATCHTYPE"] == "GENERATOR"), "DUID"
    ]
    s = scada[scada["DUID"].isin(sa_gens)]
    total = s.groupby("SETTLEMENTDATE")["SCADAVALUE"].sum().rename("scada_generation").reset_index()
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
