"""I3: Is the SCADA data trustworthy?

SCADA is the metered output each generating unit reports every 5 minutes.
This investigation screens every SA generating unit for readings that look
like telemetry faults rather than real output, and lists each one in an
issues register with a suggested response.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from sagrid.investigations.common import (
    Report,
    Window,
    md_table,
    pyplot,
    read_window,
    runs,
    snapshot,
    snapshot_md,
)
from sagrid.reconcile import scada_vs_dispatch
from sagrid.store import Store

TABLES = ("region_summary", "unit_scada")

STUCK_MIN_INTERVALS = 12  # one hour of identical, non-zero readings
SPIKE_ROBUST_Z = 8.0
SPIKE_MIN_MW = 20.0
CAPACITY_MARGIN = 1.05
NEGATIVE_FLOOR_MW = 5.0

RESPONSES = {
    "stuck": "Possible frozen telemetry. Exclude the period from analysis and raise it with the "
    "participant or AEMO if it recurs. Add a stuck-value rule to the pipeline checks.",
    "spike": "One-interval excursion that reverts. Treat as a telemetry glitch: exclude or "
    "interpolate it in analysis and track how often it recurs for this unit.",
    "above_capacity": "Output above registered capacity. Check whether the registry is out of date "
    "(an upgrade) or the telemetry is scaled wrongly; escalate if it persists.",
    "negative": "Negative output beyond normal auxiliary load. Confirm the unit type in the "
    "registry (storage or load) before treating it as a fault.",
    "missing": "Unit stopped reporting for part of the window. Check for decommissioning, a "
    "registry change or a dropped feed.",
}
SEVERITY = {
    "stuck": "high",
    "above_capacity": "high",
    "missing": "medium",
    "spike": "medium",
    "negative": "low",
}


def find_stuck(s: pd.Series, min_len: int = STUCK_MIN_INTERVALS) -> pd.DataFrame:
    """Runs of identical non-zero readings at least `min_len` intervals long."""
    v = s.to_numpy(dtype=float)
    same = np.zeros(len(v), dtype=bool)
    same[1:] = np.isclose(v[1:], v[:-1], rtol=0, atol=1e-6) & (np.abs(v[1:]) > 1.0)
    r = runs(pd.Series(same))
    if r.empty:
        return r
    r["start"] = r["start"] - 1  # include the first reading of the run
    r["length"] = r["length"] + 1
    return r[r["length"] >= min_len].reset_index(drop=True)


def find_spikes(s: pd.Series, capacity: float | None) -> pd.Series:
    """Boolean mask of single-interval excursions that revert.

    A reading is a spike when it is far from both neighbours while the
    neighbours agree with each other. The scale is the unit's median
    absolute deviation of that gap, so a noisy wind farm needs a bigger jump
    to be flagged than a steady gas unit.
    """
    v = s.astype(float)
    before, after = v.shift(1), v.shift(-1)
    resid = v - (before + after) / 2
    mad = 1.4826 * (resid - resid.median()).abs().median()
    floor = max(SPIKE_MIN_MW, 0.1 * capacity) if capacity and capacity > 0 else SPIKE_MIN_MW
    threshold = max(SPIKE_ROBUST_Z * mad, floor)
    reverts = (before - after).abs() < threshold / 2
    return (resid.abs() > threshold) & reverts


def change_points(
    x: pd.Series, min_size: int = 12, min_shift: float = 20.0, penalty: float | None = None
) -> list[int]:
    """Positions where the mean of a series shifts, by binary segmentation.

    A split is kept when it reduces the squared error by more than a
    BIC-style penalty (2 * noise variance * log n, with the noise estimated
    robustly from successive differences) and the two means differ by at
    least `min_shift`, so only shifts that matter in MW terms are reported.
    """
    v = x.dropna().to_numpy(dtype=float)
    n = len(v)
    if n < 2 * min_size:
        return []
    if penalty is None:
        d = np.diff(v)
        sigma = 1.4826 * np.median(np.abs(d - np.median(d))) / np.sqrt(2)
        penalty = 2 * max(sigma, 1.0) ** 2 * np.log(n)

    def best_split(a: np.ndarray) -> tuple[int, float, float]:
        m = len(a)
        cs, cs2 = np.cumsum(a), np.cumsum(a**2)
        best, gain, shift = -1, 0.0, 0.0
        total = cs2[-1] - cs[-1] ** 2 / m
        for k in range(min_size, m - min_size + 1):
            left = cs2[k - 1] - cs[k - 1] ** 2 / k
            right = (cs2[-1] - cs2[k - 1]) - (cs[-1] - cs[k - 1]) ** 2 / (m - k)
            g = total - left - right
            if g > gain:
                best, gain = k, g
                shift = abs(cs[k - 1] / k - (cs[-1] - cs[k - 1]) / (m - k))
        return best, gain, shift

    found: list[int] = []
    stack = [(0, n)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2 * min_size:
            continue
        k, gain, shift = best_split(v[lo:hi])
        if k > 0 and gain > penalty and shift >= min_shift:
            found.append(lo + k)
            stack += [(lo, lo + k), (lo + k, hi)]
    return sorted(found)


def screen_units(scada: pd.DataFrame, units: pd.DataFrame, grid: pd.DatetimeIndex) -> pd.DataFrame:
    """Run every check on every unit. Returns one row per issue."""
    caps = (
        units.set_index("DUID")["REG_CAP_MW"] if "REG_CAP_MW" in units else pd.Series(dtype=float)
    )
    types = units.set_index("DUID")["DISPATCHTYPE"]
    issues = []
    for duid, g in scada.groupby("DUID"):
        s = g.set_index("SETTLEMENTDATE")["SCADAVALUE"].sort_index()
        cap = caps.get(duid)
        cap = float(cap) if cap is not None and pd.notna(cap) else None

        missing = grid.difference(pd.DatetimeIndex(s.index))
        if len(missing):
            issues.append(
                _issue(
                    duid,
                    "missing",
                    missing.min(),
                    missing.max(),
                    len(missing),
                    f"{len(missing)} of {len(grid)} intervals absent",
                )
            )

        for _, r in find_stuck(s).iterrows():
            seg = s.iloc[int(r["start"]) : int(r["start"]) + int(r["length"])]
            issues.append(
                _issue(
                    duid,
                    "stuck",
                    seg.index[0],
                    seg.index[-1],
                    len(seg),
                    f"held at {seg.iloc[0]:.3f} MW for {5 * len(seg)} minutes",
                )
            )

        spikes = s[find_spikes(s, cap)]
        for t, v in spikes.items():
            issues.append(
                _issue(
                    duid,
                    "spike",
                    t,
                    t,
                    1,
                    f"{v:.1f} MW against neighbours "
                    f"{s.shift(1).get(t, np.nan):.1f} and {s.shift(-1).get(t, np.nan):.1f}",
                )
            )

        if cap:
            over = s > CAPACITY_MARGIN * cap + 1.0
            for _, r in runs(over).iterrows():
                seg = s.iloc[int(r["start"]) : int(r["start"]) + int(r["length"])]
                issues.append(
                    _issue(
                        duid,
                        "above_capacity",
                        seg.index[0],
                        seg.index[-1],
                        len(seg),
                        f"peak {seg.max():.1f} MW vs registered {cap:.1f} MW",
                    )
                )

        if types.get(duid) == "GENERATOR":
            floor = -max(NEGATIVE_FLOOR_MW, 0.02 * cap) if cap else -NEGATIVE_FLOOR_MW
            neg = s < floor
            for _, r in runs(neg).iterrows():
                seg = s.iloc[int(r["start"]) : int(r["start"]) + int(r["length"])]
                issues.append(
                    _issue(
                        duid,
                        "negative",
                        seg.index[0],
                        seg.index[-1],
                        len(seg),
                        f"down to {seg.min():.1f} MW",
                    )
                )
    cols = ["DUID", "ISSUE", "FIRST", "LAST", "INTERVALS", "EVIDENCE", "SEVERITY", "RESPONSE"]
    out = pd.DataFrame(issues, columns=cols)
    return out.merge(
        units[[c for c in ("DUID", "STATION", "FUEL") if c in units]], on="DUID", how="left"
    )


def _issue(duid, kind, first, last, n, evidence) -> dict:
    return {
        "DUID": duid,
        "ISSUE": kind,
        "FIRST": first,
        "LAST": last,
        "INTERVALS": int(n),
        "EVIDENCE": evidence,
        "SEVERITY": SEVERITY[kind],
        "RESPONSE": RESPONSES[kind],
    }


def _figure(recon: pd.DataFrame, cps: list[int]):
    plt = pyplot()

    fig, ax = plt.subplots(figsize=(9, 3.2))
    ax.plot(recon["SETTLEMENTDATE"], recon["residual_mw"], lw=0.7, color="#1f6f8b")
    for k in cps:
        ax.axvline(recon["SETTLEMENTDATE"].iloc[k], color="#c0392b", lw=1, ls="--")
    ax.axhline(0, color="#888", lw=0.5)
    ax.set_ylabel("SCADA minus dispatch (MW)")
    ax.set_title("SA generator SCADA total vs dispatched generation, with detected shifts")
    return fig


def run(store: Store, w: Window, registry: pd.DataFrame | None) -> Report:
    if registry is None:
        raise ValueError(
            "I3 needs the generator registry (NEM Registration and Exemption List); "
            "set registry_path in sagrid.toml"
        )
    scada = read_window(store, "unit_scada", w)
    if scada.empty:
        raise ValueError(
            "no unit_scada rows in the window; run sagrid ingest --datasets unit_scada"
        )
    rs = read_window(store, "region_summary", w)
    snap = snapshot(store, w, list(TABLES))

    units = registry[registry["REGIONID"] == "SA1"].copy()
    units = units[units["DISPATCHTYPE"].isin(["GENERATOR", "BIDIRECTIONAL"])]
    sa = scada[scada["DUID"].isin(units["DUID"])]
    reporting = sa["DUID"].nunique()
    grid = pd.date_range(w.lo + pd.Timedelta(minutes=5), w.hi, freq="5min")

    issues = screen_units(sa, units, grid)
    summary = (
        (
            issues.groupby(["ISSUE", "SEVERITY"])
            .agg(
                UNITS=("DUID", "nunique"),
                OCCURRENCES=("DUID", "size"),
                INTERVALS=("INTERVALS", "sum"),
            )
            .reset_index()
            .sort_values("INTERVALS", ascending=False)
        )
        if len(issues)
        else pd.DataFrame(columns=["ISSUE", "SEVERITY", "UNITS", "OCCURRENCES", "INTERVALS"])
    )

    recon = (
        scada_vs_dispatch(rs, scada, registry).sort_values("SETTLEMENTDATE").reset_index(drop=True)
    )
    resid = recon["residual_mw"]
    cps = change_points(resid.fillna(resid.median()))
    segments = []
    bounds = [0, *cps, len(recon)]
    for a, b in zip(bounds[:-1], bounds[1:], strict=True):
        seg = recon.iloc[a:b]
        segments.append(
            {
                "FROM": seg["SETTLEMENTDATE"].iloc[0],
                "TO": seg["SETTLEMENTDATE"].iloc[-1],
                "INTERVALS": len(seg),
                "MEAN_RESIDUAL_MW": float(seg["residual_mw"].mean()),
            }
        )
    seg_df = pd.DataFrame(segments)

    affected = issues["DUID"].nunique() if len(issues) else 0
    numbers = {
        "snapshot": snap,
        "sa_units_registered": int(len(units)),
        "sa_units_reporting": int(reporting),
        "units_with_issues": int(affected),
        "issues": int(len(issues)),
        "by_issue": summary.to_dict(orient="records"),
        "residual_mean_mw": float(resid.mean()),
        "residual_p95_abs_mw": float(resid.abs().quantile(0.95)),
        "residual_missing_intervals": int(resid.isna().sum()),
        "change_points": [str(recon["SETTLEMENTDATE"].iloc[k]) for k in cps],
        "segments": seg_df.to_dict(orient="records"),
        "thresholds": {
            "stuck_min_minutes": 5 * STUCK_MIN_INTERVALS,
            "spike_robust_z": SPIKE_ROBUST_Z,
            "spike_min_mw": SPIKE_MIN_MW,
            "capacity_margin": CAPACITY_MARGIN,
            "negative_floor_mw": NEGATIVE_FLOOR_MW,
        },
    }
    tables = {"issues_register": issues, "issue_summary": summary, "residual_segments": seg_df}
    md = _markdown(numbers, issues, summary, seg_df)
    return Report(
        "I3-scada-quality", "SCADA quality", md, numbers, tables, {"residual": _figure(recon, cps)}
    )


def _markdown(nb, issues, summary, seg_df) -> str:
    th = nb["thresholds"]
    top = (
        issues.sort_values(["SEVERITY", "INTERVALS"], ascending=[True, False]).head(15)[
            ["DUID", "STATION", "ISSUE", "FIRST", "INTERVALS", "EVIDENCE"]
        ]
        if len(issues) and "STATION" in issues
        else issues.head(15)
    )
    segs = seg_df.copy()
    parts = [
        "# I3. Is the SCADA data trustworthy?",
        "",
        snapshot_md(nb["snapshot"]),
        "",
        "## Question",
        "Do SA generating units' SCADA readings contain faults (frozen values, spikes, impossible "
        "values or gaps) that would mislead analysis, and does the SA total agree with dispatch?",
        "",
        "## Data",
        f"{nb['sa_units_registered']} SA generating and storage units in the registry, of which "
        f"{nb['sa_units_reporting']} reported SCADA in the window.",
        "",
        "## Method",
        f"- **Stuck:** the same non-zero reading for at least {th['stuck_min_minutes']} minutes.",
        f"- **Spike:** a single reading more than {th['spike_robust_z']:g} robust standard "
        f"deviations (median absolute deviation) and at least {th['spike_min_mw']:g} MW (or 10% "
        "of capacity) from the median of its neighbours, where the neighbours agree with each "
        "other.",
        f"- **Above capacity:** more than {th['capacity_margin']:.0%} of registered capacity.",
        f"- **Negative:** a generator below -{th['negative_floor_mw']:g} MW (or 2% of capacity), "
        "beyond normal auxiliary load. Storage is excluded.",
        "- **Missing:** intervals with no reading from a unit that reported at other times.",
        "- **Change points** in the gap between the SA SCADA total and dispatched generation, "
        "found by binary segmentation with a BIC-style penalty. A step in that gap points to a "
        "unit whose telemetry changed.",
        "",
        "## Findings",
        f"- {nb['issues']} issues across {nb['units_with_issues']} of {nb['sa_units_reporting']} "
        f"reporting units.",
        "",
        md_table(summary.rename(columns=str.title)) if len(summary) else "No issues found.",
        "",
        f"- SCADA total minus dispatched generation: mean {nb['residual_mean_mw']:.1f} MW, 95th "
        f"percentile of the absolute gap {nb['residual_p95_abs_mw']:.1f} MW. "
        + (
            f"Detected shifts at: {', '.join(nb['change_points'])}."
            if nb["change_points"]
            else "No shift in the gap was detected."
        ),
        "",
        md_table(segs, floatfmt="{:,.1f}") if len(segs) else "",
        "",
        "![Residual](I3-scada-quality/residual.png)",
        "",
        "### Issues register (top 15; the full register is in the CSV)",
        "",
        md_table(top) if len(top) else "No issues found.",
        "",
        "## Limitations",
        "- Without each unit's dispatch target we can't tell a unit that is truly offline from one "
        "whose telemetry dropped, so offline-but-reporting is not tested.",
        "- Some flags can be real behaviour: a unit held at a fixed setpoint can look stuck, and a "
        "fast battery can look like a spike. Each flag is a lead to check, not a verdict.",
        "- The registry is a snapshot. Units upgraded since it was published can look over "
        "capacity.",
        "",
        "## Recommended response",
        "- Add the stuck-value and spike rules to the pipeline as warnings, so new issues are "
        "flagged as data arrives instead of found later.",
        "- Mask flagged intervals in the Power BI export with an `IS_SUSPECT` column rather than "
        "deleting them.",
        "- Send the high-severity items in the register to whoever owns the telemetry, with the "
        "evidence column as the starting point.",
        "",
    ]
    return "\n".join(parts)
