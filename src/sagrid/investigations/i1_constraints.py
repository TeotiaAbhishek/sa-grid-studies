"""I1: Which network constraints bind most often in SA, and what drives them?

A constraint "binds" in an interval when its marginal value is non-zero.
We rank constraints by how often they bind, attach the SA price when they
do, and for the most frequent SA constraints model the probability of
binding from demand, wind and solar farm output, rooftop PV and
interconnector flows.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from sagrid.investigations.common import (
    Report,
    Window,
    fmt,
    hour_adjusted_difference,
    interval_frame,
    md_table,
    pct,
    pyplot,
    read_window,
    snapshot,
    snapshot_md,
    wilson,
)
from sagrid.store import Store

# Marginal values this large are constraint violation penalties, not prices.
# They are reported separately so they don't swamp the averages.
PENALTY_MV = 1e5

# AEMO constraint IDs start with the region or system they act on.
# S = South Australia; V::S, V^^S, V>>S, V_S and S>>V cover the Victoria-SA
# connection, and PEC is Project EnergyConnect (SA-NSW). F = frequency
# control (FCAS) and N/Q/T/V = other regions.
SA_PATTERN = re.compile(
    r"^(S|V::S|V\^\^S|V>>S|V_S_|S>>V)|_SA_|_SA$|HEYWOOD|MURRAY|PEC", re.IGNORECASE
)

# A comparison group smaller than this is too thin to report a price difference on.
MIN_COMPARISON = 30
# Odds ratios beyond this mean the input almost separates binding from not
# binding; the direction is meaningful but the size is not.
SEPARATION_OR = 100.0
# Below this held-out-day AUC, or with fewer usable folds, a driver model
# explains too little to act on.
USEFUL_AUC = 0.75
MIN_FOLDS = 3

TABLES = ("region_summary", "price", "interconnector", "constraint", "rooftop_pv")

FEATURES = [
    "TOTALDEMAND",
    "SS_WIND_CLEAREDMW",
    "SS_SOLAR_CLEAREDMW",
    "ROOFTOP_PV",
    "HEYWOOD_FLOW",
    "MURRAYLINK_FLOW",
    "HOUR_SIN",
    "HOUR_COS",
]
FEATURE_LABELS = {
    "TOTALDEMAND": "SA demand",
    "SS_WIND_CLEAREDMW": "Wind farm output",
    "SS_SOLAR_CLEAREDMW": "Solar farm output",
    "ROOFTOP_PV": "Rooftop PV",
    "HEYWOOD_FLOW": "Heywood flow into SA",
    "MURRAYLINK_FLOW": "Murraylink flow into SA",
    "HOUR_SIN": "Time of day (sine)",
    "HOUR_COS": "Time of day (cosine)",
}


def is_sa(constraint_id: str) -> bool:
    return bool(SA_PATTERN.search(constraint_id))


def category(constraint_id: str) -> str:
    c = constraint_id.upper()
    if c.startswith("F_"):
        return "Frequency control (FCAS)"
    if is_sa(c):
        return "SA network"
    return "Other region or system"


def rank_constraints(con: pd.DataFrame, frame: pd.DataFrame, n_intervals: int) -> pd.DataFrame:
    """One row per constraint that bound at least once."""
    binding = con[con["MARGINALVALUE"] != 0]
    rrp = frame.set_index("SETTLEMENTDATE")["RRP"] if "RRP" in frame else None
    hour = frame.set_index("SETTLEMENTDATE")["HOUR"]
    rows = []
    for cid, g in binding.groupby("CONSTRAINTID"):
        k = len(g)
        lo, hi = wilson(k, n_intervals)
        penalty = (g["MARGINALVALUE"].abs() >= PENALTY_MV).mean()
        row = {
            "CONSTRAINTID": cid,
            "CATEGORY": category(str(cid)),
            "BINDING_INTERVALS": k,
            "SHARE": k / n_intervals,
            "SHARE_LO": lo,
            "SHARE_HI": hi,
            "MEDIAN_MV": float(g["MARGINALVALUE"].median()),
            "PENALTY_SHARE": float(penalty),
        }
        if rrp is not None:
            flag = rrp.index.isin(g["SETTLEMENTDATE"])
            row["RRP_BINDING"] = float(rrp[flag].mean())
            row["RRP_NOT_BINDING"] = float(rrp[~flag].mean()) if (~flag).any() else np.nan
            diff, _ = hour_adjusted_difference(
                rrp, pd.Series(flag, index=rrp.index), hour.reindex(rrp.index)
            )
            # With almost no intervals on one side the comparison means little.
            thin = min(int(flag.sum()), int((~flag).sum())) < MIN_COMPARISON
            row["RRP_DIFF_SAME_HOUR"] = np.nan if thin else diff
        rows.append(row)
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values(
        ["BINDING_INTERVALS", "CONSTRAINTID"], ascending=[False, True]
    ).reset_index(drop=True)


def _design(frame: pd.DataFrame) -> pd.DataFrame:
    x = frame.copy()
    x["HOUR_SIN"] = np.sin(2 * np.pi * x["HOUR"] / 24)
    x["HOUR_COS"] = np.cos(2 * np.pi * x["HOUR"] / 24)
    # Inputs that are missing or never change carry no information.
    cols = [c for c in FEATURES if c in x and x[c].notna().any() and x[c].nunique() > 1]
    return x[cols]


def drivers(
    frame: pd.DataFrame, con: pd.DataFrame, cid: str, n_boot: int = 200, seed: int = 0
) -> dict:
    """Model P(constraint binds) for one constraint.

    - Logistic regression on standardised inputs: the odds ratio for a one
      standard deviation rise in each input, with a 95% interval from a
      bootstrap that resamples whole days (intervals within a day are not
      independent).
    - Gradient boosting checked with cross-validation that holds out whole
      days, and permutation importance on the held-out days.
    """
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.inspection import permutation_importance
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold

    bound = set(
        con.loc[(con["CONSTRAINTID"] == cid) & (con["MARGINALVALUE"] != 0), "SETTLEMENTDATE"]
    )
    x_all = _design(frame)
    y_all = frame["SETTLEMENTDATE"].isin(bound).astype(int)
    keep = x_all.notna().all(axis=1)
    x, y, days = x_all[keep], y_all[keep], frame.loc[keep, "DAY"]
    if y.nunique() < 2:
        return {"constraint": cid, "skipped": "binds in every interval or none"}

    mu, sd = x.mean(), x.std(ddof=0).replace(0, 1)
    z = (x - mu) / sd
    lr = LogisticRegression(max_iter=1000).fit(z, y)
    coef = pd.Series(lr.coef_[0], index=x.columns)

    rng = np.random.default_rng(seed)
    uniq = days.unique()
    boots = []
    for _ in range(n_boot):
        pick = rng.choice(uniq, size=len(uniq), replace=True)
        idx = np.concatenate([np.flatnonzero(days.to_numpy() == d) for d in pick])
        yb = y.iloc[idx]
        if yb.nunique() < 2:
            continue
        m = LogisticRegression(max_iter=1000).fit(z.iloc[idx], yb)
        boots.append(m.coef_[0])
    b = np.array(boots) if boots else np.full((1, len(coef)), np.nan)
    odds = pd.DataFrame(
        {
            "INPUT": [FEATURE_LABELS[c] for c in x.columns],
            "ODDS_RATIO_PER_SD": np.exp(coef.to_numpy()),
            "CI_LO": np.exp(np.nanpercentile(b, 2.5, axis=0)),
            "CI_HI": np.exp(np.nanpercentile(b, 97.5, axis=0)),
        }
    )

    n_splits = min(5, len(uniq))
    aucs, imps = [], []
    if n_splits >= 2:
        for tr, te in GroupKFold(n_splits=n_splits).split(x, y, groups=days):
            if y.iloc[te].nunique() < 2 or y.iloc[tr].nunique() < 2:
                continue
            gb = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.1, random_state=seed)
            gb.fit(x.iloc[tr], y.iloc[tr])
            aucs.append(roc_auc_score(y.iloc[te], gb.predict_proba(x.iloc[te])[:, 1]))
            pi = permutation_importance(
                gb, x.iloc[te], y.iloc[te], scoring="roc_auc", n_repeats=5, random_state=seed
            )
            imps.append(pi.importances_mean)
    importance = pd.DataFrame(
        {
            "INPUT": [FEATURE_LABELS[c] for c in x.columns],
            "AUC_DROP_WHEN_SHUFFLED": np.mean(imps, axis=0) if imps else np.nan,
        }
    ).sort_values("AUC_DROP_WHEN_SHUFFLED", ascending=False)
    separated = odds.loc[
        (odds["ODDS_RATIO_PER_SD"] > SEPARATION_OR)
        | (odds["ODDS_RATIO_PER_SD"] < 1 / SEPARATION_OR),
        "INPUT",
    ].tolist()
    auc = float(np.mean(aucs)) if aucs else float("nan")
    return {
        "constraint": cid,
        "useful": len(aucs) >= MIN_FOLDS and auc >= USEFUL_AUC,
        "separated_inputs": separated,
        "binding_share": float(y.mean()),
        "intervals_modelled": int(len(y)),
        "odds": odds,
        "importance": importance.reset_index(drop=True),
        "cv_auc_mean": auc,
        "cv_folds": len(aucs),
        "inputs": list(x.columns),
    }


def _figure(ranked: pd.DataFrame, top: int = 15):
    plt = pyplot()

    colours = {
        "SA network": "#1f6f8b",
        "Frequency control (FCAS)": "#99a8b2",
        "Other region or system": "#d8d3cd",
    }
    t = ranked.head(top).iloc[::-1]
    fig, ax = plt.subplots(figsize=(8, 0.35 * len(t) + 1))
    ax.barh(t["CONSTRAINTID"], 100 * t["SHARE"], color=[colours[c] for c in t["CATEGORY"]])
    ax.set_xlabel("Share of 5-minute intervals binding (%)")
    ax.set_title("Most frequently binding constraints")
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in colours.values()]
    ax.legend(handles, colours.keys(), loc="lower right", fontsize=8)
    return fig


def run(store: Store, w: Window, top_models: int = 3) -> Report:
    frame = interval_frame(store, w)
    con = read_window(store, "constraint", w)
    n = len(frame)
    snap = snapshot(
        store, w, ["region_summary", "price", "interconnector", "constraint", "rooftop_pv"]
    )
    ranked = rank_constraints(con, frame, n)
    clean = ranked[ranked["PENALTY_SHARE"] < 0.5]
    penalty = ranked[ranked["PENALTY_SHARE"] >= 0.5]
    sa = clean[clean["CATEGORY"] == "SA network"]

    candidates = sa[(sa["SHARE"] >= 0.05) & (sa["SHARE"] <= 0.95)].head(top_models)
    models = [drivers(frame, con, cid) for cid in candidates["CONSTRAINTID"]]
    models = [m for m in models if "skipped" not in m]

    numbers: dict = {
        "snapshot": snap,
        "intervals": n,
        "constraints_in_data": int(con["CONSTRAINTID"].nunique()),
        "constraints_binding": int(len(ranked)),
        "penalty_constraints": penalty["CONSTRAINTID"].tolist(),
        "by_category": clean.groupby("CATEGORY")["CONSTRAINTID"].count().to_dict(),
        "top": clean.head(20).to_dict(orient="records"),
        "models": [
            {
                k: (v.to_dict(orient="records") if isinstance(v, pd.DataFrame) else v)
                for k, v in m.items()
            }
            for m in models
        ],
        "has_price": "RRP" in frame,
        "has_rooftop": "ROOFTOP_PV" in frame and frame["ROOFTOP_PV"].notna().any(),
        "rooftop_missing_intervals": int(frame["ROOFTOP_PV"].isna().sum())
        if "ROOFTOP_PV" in frame
        else n,
    }

    tables = {"ranked_constraints": ranked}
    for m in models:
        safe = re.sub(r"[^A-Za-z0-9_]+", "_", m["constraint"])
        tables[f"drivers_{safe}_odds"] = m["odds"]
        tables[f"drivers_{safe}_importance"] = m["importance"]

    md = _markdown(numbers, clean, penalty, sa, models)
    return Report(
        "I1-binding-constraints",
        "Binding constraints",
        md,
        numbers,
        tables,
        {"top_constraints": _figure(clean)},
    )


def _markdown(numbers, clean, penalty, sa, models) -> str:
    n = numbers["intervals"]
    show = clean.head(15)[
        [
            c
            for c in [
                "CONSTRAINTID",
                "CATEGORY",
                "BINDING_INTERVALS",
                "SHARE",
                "RRP_BINDING",
                "RRP_DIFF_SAME_HOUR",
            ]
            if c in clean
        ]
    ].copy()
    show["SHARE"] = show["SHARE"].map(lambda v: pct(v))
    show = show.rename(
        columns={
            "CONSTRAINTID": "Constraint",
            "CATEGORY": "Type",
            "BINDING_INTERVALS": "Intervals binding",
            "SHARE": "Share",
            "RRP_BINDING": "Mean SA price when binding ($/MWh)",
            "RRP_DIFF_SAME_HOUR": "Price difference vs same hours not binding ($/MWh)",
        }
    )
    cats = numbers["by_category"]
    parts = [
        "# I1. Which network constraints bind most often in SA, and what drives them?",
        "",
        snapshot_md(numbers["snapshot"]),
        "",
        "## Question",
        "Which constraints in AEMO's dispatch engine bind most often, which of them act on the SA "
        "network, and what conditions make the SA ones bind?",
        "",
        "## Data",
        f"{n:,} five-minute dispatch intervals. {numbers['constraints_in_data']:,} constraints "
        f"appear in the data and {numbers['constraints_binding']:,} bound at least once (non-zero "
        "marginal value). Price is SA's regional reference price (RRP)."
        + (
            " Rooftop PV was not loaded for this window."
            if not numbers["has_rooftop"]
            else (
                f" Rooftop PV is missing for {numbers['rooftop_missing_intervals']} intervals "
                "(half hours AEMO did not publish); models use intervals with every input."
                if numbers["rooftop_missing_intervals"]
                else ""
            )
        ),
        "",
        "## Method",
        "1. Count binding intervals per constraint, with a 95% Wilson interval on the share.",
        "2. Classify constraints from their IDs: frequency control (`F_`), SA network, or another "
        "region.",
        f"3. Set aside constraints whose marginal value is mostly at penalty size (|value| of "
        f"{PENALTY_MV:,.0f} or more). These signal a violated constraint, not a market price.",
        "4. Compare SA price when a constraint binds against the same hours of day when it does "
        "not. This stops the evening peak or the midday solar trough from looking like a "
        f"constraint effect. The difference is left blank when either side has fewer than "
        f"{MIN_COMPARISON} intervals.",
        "5. For the most frequent SA constraints that bind in 5% to 95% of intervals, fit a "
        "logistic regression (odds ratios per standard deviation, with a bootstrap over days) "
        "and a gradient boosting model checked by holding out whole days.",
        "",
        "## Findings",
        "Binding constraints by type, leaving out penalty-valued ones: "
        + ", ".join(f"{k}: {v}" for k, v in sorted(cats.items()))
        + ".",
        "",
        md_table(show),
        "",
        "![Most frequently binding constraints](I1-binding-constraints/top_constraints.png)",
        "",
    ]
    if len(penalty):
        parts += [
            f"Set aside as penalty-valued: {', '.join(f'`{c}`' for c in penalty['CONSTRAINTID'])}.",
            "",
        ]
    if sa.empty:
        parts += ["No SA network constraint bound in this window.", ""]
    else:
        sa_show = sa.head(10)[
            [
                c
                for c in [
                    "CONSTRAINTID",
                    "BINDING_INTERVALS",
                    "SHARE",
                    "RRP_BINDING",
                    "RRP_DIFF_SAME_HOUR",
                ]
                if c in sa
            ]
        ].copy()
        sa_show["SHARE"] = sa_show["SHARE"].map(lambda v: pct(v))
        sa_show = sa_show.rename(
            columns={
                "CONSTRAINTID": "Constraint",
                "BINDING_INTERVALS": "Intervals binding",
                "SHARE": "Share",
                "RRP_BINDING": "Mean SA price when binding ($/MWh)",
                "RRP_DIFF_SAME_HOUR": "Price difference vs same hours not binding ($/MWh)",
            }
        )
        parts += [
            "### SA network constraints",
            f"{len(sa)} SA network constraints bound at least once. The most frequent:",
            "",
            md_table(sa_show),
            "",
        ]
    for m in models:
        odds = m["odds"].copy()
        odds = odds.rename(
            columns={
                "INPUT": "Input",
                "ODDS_RATIO_PER_SD": "Odds ratio per 1 SD",
                "CI_LO": "95% CI low",
                "CI_HI": "95% CI high",
            }
        )
        top_imp = m["importance"].iloc[0]
        parts += [
            f"### What drives `{m['constraint']}`",
            f"It binds in {pct(m['binding_share'])} of the {m['intervals_modelled']:,} intervals "
            f"with complete inputs. Held-out-day AUC of the gradient boosting model: "
            f"{fmt(m['cv_auc_mean'], 2)} over {m['cv_folds']} folds (0.5 is chance).",
            "",
            md_table(odds),
            "",
            *(
                [
                    "This model does little better than chance on days it did not see "
                    f"(fewer than {MIN_FOLDS} usable folds or AUC below {USEFUL_AUC}), so the "
                    "odds ratios below are not a reliable explanation. The constraint is likely "
                    "driven by something outside these inputs, such as conditions in another "
                    "region or a network outage.",
                    "",
                ]
                if not m["useful"]
                else []
            ),
            *(
                [
                    f"Note: {', '.join(m['separated_inputs'])} almost separates binding from "
                    "non-binding intervals on its own, so its odds ratio is unstable. Read it as "
                    "a very strong association in the direction shown, not as a size.",
                    "",
                ]
                if m["separated_inputs"]
                else []
            ),
            f"Shuffling **{top_imp['INPUT']}** costs the most accuracy on held-out days "
            f"(AUC drop {fmt(top_imp['AUC_DROP_WHEN_SHUFFLED'], 3)}). An odds ratio above 1 means "
            "the constraint is more likely to bind when that input is higher; an interval that "
            "crosses 1 means the direction is not settled by this data.",
            "",
        ]
    parts += [
        "## Limitations",
        "- Correlation, not causation. Inputs move together (rooftop PV, demand and time of day "
        "especially), so an odds ratio describes this data, not a physical mechanism.",
        "- Public data only. Constraint equations, outage plans and network ratings behind each "
        "constraint are not used; a network owner would check findings against them.",
        "- A short window can't show seasonal behaviour. The intervals and day-level bootstrap "
        "are wide for that reason.",
        "- Classification by ID prefix is a heuristic and can mislabel unusual constraint names.",
        "",
        "## Recommended response",
    ]
    if len(sa):
        top_sa = sa.iloc[0]["CONSTRAINTID"]
        parts.append(
            f"- Monitor `{top_sa}`, the most frequent SA constraint: report its daily binding "
            "share with the pipeline's health checks, and alert when it rises well above this "
            "baseline."
        )
    explained = [m for m in models if m["useful"]]
    if explained:
        best = max(explained, key=lambda m: m["cv_auc_mean"])
        parts.append(
            f"- Stage 2 study: `{best['constraint']}` is the best-explained SA constraint "
            f"(held-out AUC {fmt(best['cv_auc_mean'], 2)}). Run the N-1 study at the operating "
            "points where it binds, to see which element limits transfer."
        )
    unexplained = [m["constraint"] for m in models if not m["useful"]]
    if unexplained:
        parts.append(
            f"- Investigate {', '.join(f'`{c}`' for c in unexplained)} with outage and "
            "neighbouring-region data, since SA conditions alone don't explain when they bind."
        )
    if len(penalty):
        parts.append(
            "- Data quality: flag penalty-sized marginal values in the export so dashboards "
            "exclude them from averages by default."
        )
    parts.append(
        "- Extend the window to at least one full season before drawing conclusions about drivers."
    )
    parts.append("")
    return "\n".join(parts)
