"""I2: How often is the Heywood interconnector at its limit, and under what conditions?

Heywood (V-SA) is SA's AC link to Victoria. Positive flow is Victoria to
SA. AEMO's EXPORTLIMIT caps flow in that direction and IMPORTLIMIT caps the
reverse (SA to Victoria), so both are expressed on the same signed axis.
"""

from __future__ import annotations

import pandas as pd

from sagrid.investigations.common import (
    Report,
    Window,
    at_limit,
    fmt,
    hour_adjusted_difference,
    interval_frame,
    kaplan_meier,
    md_table,
    pct,
    pyplot,
    runs,
    snapshot,
    snapshot_md,
    wilson,
)
from sagrid.store import Store

TABLES = ("region_summary", "price", "interconnector", "rooftop_pv")
CONDITIONS = {
    "TOTALDEMAND": "SA demand (MW)",
    "TOTALINTERMITTENTGENERATION": "Wind and solar farm output (MW)",
    "ROOFTOP_PV": "Rooftop PV (MW)",
    "RRP": "SA price ($/MWh)",
}


def decompose_daily(hourly: pd.Series) -> dict:
    """How much of the variation in an hourly series is daily pattern, and how much day to day.

    The daily pattern is the mean for each hour of day; its share of the
    variance is eta squared (between-hour sum of squares over total). The
    day-to-day share is the same measure grouping by calendar day.
    """
    s = hourly.astype(float).dropna()
    idx = pd.DatetimeIndex(s.index)
    total = float(((s - s.mean()) ** 2).sum())

    def eta2(groups) -> float:
        means = s.groupby(groups).transform("mean")
        return float(((means - s.mean()) ** 2).sum() / total) if total else float("nan")

    pattern = s.groupby(idx.hour).mean()
    return {
        "daily_pattern": pattern,
        "var_daily_share": eta2(idx.hour),
        "var_trend_share": eta2(idx.normalize()),
    }


def episodes(state: pd.Series, direction: str) -> pd.DataFrame:
    r = runs(state == direction)
    if r.empty:
        return r.assign(minutes=pd.Series(dtype=float), censored=pd.Series(dtype=bool))
    r["minutes"] = 5 * r["length"]
    r["censored"] = r["at_start"] | r["at_end"]
    return r


def condition_tests(frame: pd.DataFrame, flag: pd.Series) -> pd.DataFrame:
    """Compare conditions at the limit with the rest, raw and within the same hour of day."""
    from scipy.stats import mannwhitneyu

    rows = []
    for col, label in CONDITIONS.items():
        if col not in frame or frame[col].isna().all():
            continue
        on, off = frame.loc[flag, col].dropna(), frame.loc[~flag, col].dropna()
        if len(on) < 5 or len(off) < 5:
            continue
        p = mannwhitneyu(on, off, alternative="two-sided").pvalue
        adj, _ = hour_adjusted_difference(frame[col], flag, frame["HOUR"])
        rows.append(
            {
                "CONDITION": label,
                "MEDIAN_AT_LIMIT": float(on.median()),
                "MEDIAN_OTHERWISE": float(off.median()),
                "MEAN_DIFF_RAW": float(on.mean() - off.mean()),
                "MEAN_DIFF_SAME_HOUR": adj,
                "MANN_WHITNEY_P": float(p),
            }
        )
    return pd.DataFrame(rows)


def _figures(by_hour: pd.DataFrame, km: pd.DataFrame):
    plt = pyplot()

    f1, ax = plt.subplots(figsize=(8, 3.2))
    ax.bar(by_hour["HOUR"], 100 * by_hour["SHARE_EXPORT"], color="#1f6f8b", label="into SA")
    ax.bar(
        by_hour["HOUR"],
        100 * by_hour["SHARE_IMPORT"],
        bottom=100 * by_hour["SHARE_EXPORT"],
        color="#e3a857",
        label="out of SA",
    )
    ax.set_xlabel("Hour of day (interval start, AEST)")
    ax.set_ylabel("Intervals at limit (%)")
    ax.set_xticks(range(0, 24, 2))
    ax.legend(fontsize=8)
    ax.set_title("Heywood at its limit, by hour of day")

    f2, ax2 = plt.subplots(figsize=(6, 3.2))
    if not km.empty:
        ax2.step(km["duration"], km["survival"], where="post", color="#1f6f8b")
    ax2.set_xlabel("Episode length (minutes)")
    ax2.set_ylabel("Share of episodes lasting longer")
    ax2.set_ylim(0, 1.02)
    ax2.set_title("How long Heywood stays at its limit (into SA)")
    return {"by_hour": f1, "episode_survival": f2}


def run(store: Store, w: Window) -> Report:
    from scipy.stats import chi2_contingency

    frame = interval_frame(store, w)
    if "HEYWOOD_FLOW" not in frame:
        raise ValueError("interconnector data is missing; run sagrid ingest")
    snap = snapshot(store, w, list(TABLES))
    state = at_limit(
        frame["HEYWOOD_FLOW"], frame["HEYWOOD_EXPORTLIMIT"], frame["HEYWOOD_IMPORTLIMIT"]
    )
    frame["STATE"] = state
    n = int(frame["HEYWOOD_FLOW"].notna().sum())
    k_e, k_i = int((state == "export").sum()), int((state == "import").sum())
    flag = state != ""

    by_hour = (
        frame.groupby("HOUR")
        .agg(
            INTERVALS=("STATE", "size"),
            SHARE_EXPORT=("STATE", lambda s: (s == "export").mean()),
            SHARE_IMPORT=("STATE", lambda s: (s == "import").mean()),
            MEAN_FLOW=("HEYWOOD_FLOW", "mean"),
        )
        .reset_index()
    )
    weekday = frame["DAY"].dt.day_name()
    by_day = (
        frame.assign(WEEKDAY=weekday)
        .groupby(["DAY", "WEEKDAY"])
        .agg(SHARE_AT_LIMIT=("STATE", lambda s: (s != "").mean()))
        .reset_index()
    )

    table = pd.crosstab(frame["HOUR"], flag)
    chi2_p = float(chi2_contingency(table).pvalue) if table.shape[1] == 2 else float("nan")

    hourly = frame.set_index(frame["SETTLEMENTDATE"] - pd.Timedelta(minutes=5))["STATE"].ne("")
    hourly_share = hourly.resample("1h").mean()
    dec = decompose_daily(hourly_share)

    ep = episodes(state, "export")
    km = (
        kaplan_meier(ep["minutes"].to_numpy(), (~ep["censored"]).to_numpy())
        if len(ep)
        else pd.DataFrame(columns=["duration", "survival"])
    )

    def surv_at(minutes: int) -> float:
        if km.empty:
            return float("nan")
        s = km.loc[km["duration"] <= minutes, "survival"]
        return float(s.iloc[-1]) if len(s) else 1.0

    cond = condition_tests(frame, state == "export")

    numbers = {
        "snapshot": snap,
        "intervals": n,
        "at_export_limit": k_e,
        "at_import_limit": k_i,
        "share_export": k_e / n if n else float("nan"),
        "share_export_ci": wilson(k_e, n),
        "share_import": k_i / n if n else float("nan"),
        "share_import_ci": wilson(k_i, n),
        "flow_mean": float(frame["HEYWOOD_FLOW"].mean()),
        "export_limit_median": float(frame["HEYWOOD_EXPORTLIMIT"].median()),
        "import_limit_median": float(frame["HEYWOOD_IMPORTLIMIT"].median()),
        "chi2_hour_p": chi2_p,
        "decomposition": {k: v for k, v in dec.items() if k != "daily_pattern"},
        "episodes_export": int(len(ep)),
        "episode_median_min": float(ep["minutes"].median()) if len(ep) else float("nan"),
        "episode_p90_min": float(ep["minutes"].quantile(0.9)) if len(ep) else float("nan"),
        "episode_max_min": float(ep["minutes"].max()) if len(ep) else float("nan"),
        "episodes_censored": int(ep["censored"].sum()) if len(ep) else 0,
        "survival_30_min": surv_at(30),
        "survival_60_min": surv_at(60),
        "survival_120_min": surv_at(120),
        "conditions": cond.to_dict(orient="records"),
        "peak_hours": by_hour.nlargest(3, "SHARE_EXPORT")["HOUR"].tolist(),
    }
    tables = {"by_hour": by_hour, "by_day": by_day, "episodes_into_sa": ep, "conditions": cond}
    md = _markdown(numbers, by_hour, by_day, cond)
    return Report("I2-heywood-limits", "Heywood limits", md, numbers, tables, _figures(by_hour, km))


def _p(p: float) -> str:
    if p != p:  # NaN
        return "p = n/a"
    return "p < 1e-10" if p < 1e-10 else f"p = {p:.2g}"


def _markdown(nb, by_hour, by_day, cond) -> str:
    lo_e, hi_e = nb["share_export_ci"]
    dec = nb["decomposition"]
    days = by_day.copy()
    days["DAY"] = days["DAY"].dt.strftime("%a %d %b")
    days["SHARE_AT_LIMIT"] = days["SHARE_AT_LIMIT"].map(lambda v: pct(v))
    days = days.drop(columns="WEEKDAY").rename(columns={"DAY": "Day", "SHARE_AT_LIMIT": "At limit"})
    c = cond.rename(
        columns={
            "CONDITION": "Condition",
            "MEDIAN_AT_LIMIT": "Median at limit",
            "MEDIAN_OTHERWISE": "Median otherwise",
            "MEAN_DIFF_RAW": "Mean difference",
            "MEAN_DIFF_SAME_HOUR": "Mean difference, same hours",
            "MANN_WHITNEY_P": "Mann-Whitney p",
        }
    )
    peak = ", ".join(f"{h:02d}:00" for h in nb["peak_hours"])
    parts = [
        "# I2. How often is Heywood at its limit, and under what conditions?",
        "",
        snapshot_md(nb["snapshot"]),
        "",
        "## Question",
        "How often does flow on the Heywood interconnector sit at its dispatch limit, when does it "
        "happen, how long does it last, and what are SA's conditions at those times?",
        "",
        "## Data",
        f"{nb['intervals']:,} five-minute intervals of Heywood (V-SA) flow and limits from AEMO "
        f"dispatch. Mean flow {fmt(nb['flow_mean'])} MW into SA; median limits "
        f"{fmt(nb['export_limit_median'])} MW into SA and {fmt(nb['import_limit_median'])} MW out "
        "of SA (negative means SA to Victoria).",
        "",
        "## Method",
        "- An interval is *at the limit* when flow is within 5% of the limit (at least 5 MW), "
        "checked separately for each direction.",
        "- Share at the limit with 95% Wilson intervals, by hour of day and by day.",
        "- Chi-square test of whether being at the limit depends on hour of day.",
        "- Decomposition of the hourly at-limit share: how much of its variance is the daily "
        "pattern (hour of day) and how much is day to day (eta squared).",
        "- Episodes: runs of consecutive intervals at the limit. Their lengths are summarised with "
        "a Kaplan-Meier survival curve, treating episodes cut off by the window edges as censored.",
        "- Conditions at the limit compared with the rest (Mann-Whitney), and again within the "
        "same hours of day so time of day isn't mistaken for a cause.",
        "",
        "## Findings",
        f"- Heywood was at its limit **into SA** in {pct(nb['share_export'])} of intervals "
        f"(95% CI {pct(lo_e)} to {pct(hi_e)}), and at its limit **out of SA** in "
        f"{pct(nb['share_import'])}.",
        f"- It depends on time of day (chi-square {_p(nb['chi2_hour_p'])}). The hours most "
        f"often at the limit into SA start at {peak}. The daily pattern explains "
        f"{pct(dec['var_daily_share'])} of the variance in the hourly at-limit share; the "
        f"differences between days explain {pct(dec['var_trend_share'])}.",
        f"- There were {nb['episodes_export']} episodes at the limit into SA "
        f"({nb['episodes_censored']} cut off by the window). Median length "
        f"{fmt(nb['episode_median_min'], 0)} minutes, 90th percentile "
        f"{fmt(nb['episode_p90_min'], 0)}, longest {fmt(nb['episode_max_min'], 0)}. "
        f"Share lasting longer than 30 minutes: {pct(nb['survival_30_min'])}; longer than one "
        f"hour: {pct(nb['survival_60_min'])}; longer than two hours: "
        f"{pct(nb['survival_120_min'])}.",
        "",
        "![At limit by hour](I2-heywood-limits/by_hour.png)",
        "",
        md_table(days),
        "",
        "### Conditions when Heywood is at its limit into SA",
        "",
        md_table(c, floatfmt="{:,.3g}"),
        "",
        "Where the same-hours difference is much smaller than the raw difference, most of the "
        "raw gap is time of day rather than the interconnector.",
        "",
        "![Episode survival](I2-heywood-limits/episode_survival.png)",
        "",
        "## Limitations",
        "- Correlation, not causation. Being at the limit and low prices can share a cause "
        "(for example high Victorian wind) that this data doesn't include.",
        "- The limit used is AEMO's dispatch limit, which already reflects constraints and "
        "outages. It is not the physical thermal or stability rating of the line.",
        "- A short window can't show seasonal or outage-driven behaviour.",
        "- Intervals within an episode are not independent, so p-values here are optimistic.",
        "",
        "## Recommended response",
        "- Add a Heywood at-limit share and longest-episode metric to `sagrid check`, so a change "
        "in transfer behaviour is noticed the day it happens.",
        "- Stage 2 study: an N-1 study at a typical at-limit operating point, to show which "
        "contingency sets the transfer limit.",
        "- Repeat over a full year to separate seasonal effects from this week's weather.",
        "",
    ]
    return "\n".join(parts)
