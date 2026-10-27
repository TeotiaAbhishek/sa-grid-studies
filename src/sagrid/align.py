"""Time alignment: the 5-minute dispatch grid, gap detection and aggregates.

AEMO timestamps (SETTLEMENTDATE) mark the END of each 5-minute interval in
market time (AEST, UTC+10, no daylight saving). We keep that convention and
never convert to local time, so there are no daylight-saving gaps or repeats.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

INTERVAL = pd.Timedelta(minutes=5)


def expected_intervals(start: date, end: date) -> pd.DatetimeIndex:
    """All interval-ending timestamps for market days start..end inclusive.

    A NEM market day runs 04:00 to 04:00, but we use calendar days here
    (00:05 to 24:00), which is what the daily archive files contain.
    """
    first = datetime.combine(start, datetime.min.time()) + timedelta(minutes=5)
    last = datetime.combine(end + timedelta(days=1), datetime.min.time())
    return pd.date_range(first, last, freq=INTERVAL)


def find_gaps(timestamps: pd.Series, start: date, end: date) -> pd.DataFrame:
    """Return missing intervals grouped into runs: gap_start, gap_end, n_intervals."""
    expected = expected_intervals(start, end)
    present = pd.DatetimeIndex(pd.to_datetime(timestamps).unique())
    missing = expected.difference(present)
    if missing.empty:
        return pd.DataFrame(columns=["gap_start", "gap_end", "n_intervals"])
    gaps = pd.Series(missing)
    run_id = (gaps.diff() != INTERVAL).cumsum()
    runs = gaps.groupby(run_id)
    return pd.DataFrame(
        {
            "gap_start": runs.min().values,
            "gap_end": runs.max().values,
            "n_intervals": runs.size().values,
        }
    )


def aggregate(
    df: pd.DataFrame, value_cols: list[str], freq: str, by: list[str] | None = None
) -> pd.DataFrame:
    """Average interval values into longer periods (e.g. "30min", "1D").

    Periods are labelled by their end, matching AEMO. A coverage column
    shows what fraction of the 5-minute intervals were present, so a
    half-empty period is never mistaken for a complete one.
    """
    by = by or []
    per_period = pd.Timedelta(freq) / INTERVAL
    keyed = df.set_index("SETTLEMENTDATE")
    grouper = [pd.Grouper(freq=freq, label="right", closed="right"), *by]
    g = keyed.groupby(grouper)[value_cols]
    out = g.mean()
    out["coverage"] = g.size() / per_period
    return out.reset_index()
