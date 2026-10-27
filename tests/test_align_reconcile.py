from datetime import date, datetime

import pandas as pd

from sagrid.align import aggregate, expected_intervals, find_gaps
from sagrid.reconcile import SCADA_LAG as LAG
from sagrid.reconcile import region_balance, scada_vs_dispatch, summarise

D = date(2026, 10, 1)


def test_expected_intervals_one_day():
    idx = expected_intervals(D, D)
    assert len(idx) == 288
    assert idx[0] == datetime(2026, 10, 1, 0, 5) and idx[-1] == datetime(2026, 10, 2, 0, 0)


def test_find_gaps_groups_runs():
    ts = pd.Series(expected_intervals(D, D)).drop(index=[10, 11, 12, 100])
    gaps = find_gaps(ts, D, D)
    assert gaps["n_intervals"].tolist() == [3, 1]


def test_no_gaps():
    assert find_gaps(pd.Series(expected_intervals(D, D)), D, D).empty


def test_aggregate_reports_coverage():
    ts = expected_intervals(D, D)[:6]  # first half hour complete
    df = pd.DataFrame(
        {"SETTLEMENTDATE": list(ts) + [ts[5] + pd.Timedelta(minutes=5)], "v": [1.0] * 7}
    )
    out = aggregate(df, ["v"], "30min")
    assert out.loc[0, "coverage"] == 1.0
    assert round(out.loc[1, "coverage"], 3) == round(1 / 6, 3)


def _rs(demand):
    return pd.DataFrame(
        {
            "SETTLEMENTDATE": expected_intervals(D, D)[:3],
            "TOTALDEMAND": demand,
            "DISPATCHABLEGENERATION": [1300.0] * 3,
            "DISPATCHABLELOAD": [0.0] * 3,
            "NETINTERCHANGE": [-200.0] * 3,
        }
    )


def test_region_balance_flags_outliers():
    out = region_balance(_rs([1500.0, 1520.0, 1700.0]), tol_mw=50)
    assert out["flag"].tolist() == [False, False, True]
    s = summarise(out)
    assert s["flagged"] == 1 and s["intervals"] == 3


def test_scada_vs_dispatch_uses_only_sa_generators():
    rs = _rs([1500.0] * 3)
    reg = pd.DataFrame(
        {
            "DUID": ["A", "B", "VICX"],
            "REGIONID": ["SA1", "SA1", "VIC1"],
            "DISPATCHTYPE": ["GENERATOR"] * 3,
        }
    )
    scada = pd.DataFrame(
        {
            "SETTLEMENTDATE": list(rs["SETTLEMENTDATE"] + LAG) * 3,
            "DUID": ["A"] * 3 + ["B"] * 3 + ["VICX"] * 3,
            "SCADAVALUE": [700.0] * 3 + [600.0] * 3 + [9999.0] * 3,
        }
    )
    out = scada_vs_dispatch(rs, scada.iloc[:-1], reg)
    assert out["residual_mw"].tolist() == [0.0, 0.0, 0.0]
    assert not out["flag"].any()


def test_scada_vs_dispatch_counts_battery_output_but_not_charging_or_non_scheduled():
    rs = _rs([1500.0] * 3)  # 1300 MW dispatched generation in each interval
    reg = pd.DataFrame(
        {
            "DUID": ["GEN", "BATT", "SMALL"],
            "REGIONID": ["SA1"] * 3,
            "DISPATCHTYPE": ["GENERATOR", "BIDIRECTIONAL", "GENERATOR"],
            "SCHEDULE_TYPE": ["SCHEDULED", "SCHEDULED", "NON-SCHEDULED"],
        }
    )
    t = list(rs["SETTLEMENTDATE"] + LAG)
    scada = pd.DataFrame(
        {
            "SETTLEMENTDATE": t * 3,
            "DUID": ["GEN"] * 3 + ["BATT"] * 3 + ["SMALL"] * 3,
            # the battery discharges 200 MW, then idles, then charges 100 MW
            "SCADAVALUE": [1100.0, 1300.0, 1300.0, 200.0, 0.0, -100.0] + [40.0] * 3,
        }
    )
    out = scada_vs_dispatch(rs, scada, reg)
    assert out["scada_generation"].tolist() == [1300.0, 1300.0, 1300.0]
    assert not out["flag"].any()


def test_scada_reading_stamped_next_interval_is_compared_with_dispatch():
    # A unit ramping 1000 -> 1300 MW: dispatch targets 1300 for the first interval, and the
    # reading stamped at its end (start of the next interval) is what shows it got there.
    rs = _rs([1500.0] * 3)
    reg = pd.DataFrame({"DUID": ["A"], "REGIONID": ["SA1"], "DISPATCHTYPE": ["GENERATOR"]})
    t = list(rs["SETTLEMENTDATE"])
    scada = pd.DataFrame(
        {"SETTLEMENTDATE": t + [t[-1] + LAG], "DUID": "A", "SCADAVALUE": [1000.0] + [1300.0] * 3}
    )
    out = scada_vs_dispatch(rs, scada, reg)
    assert out["residual_mw"].tolist() == [0.0, 0.0, 0.0]


def test_scada_missing_interval_is_flagged():
    rs = _rs([1500.0] * 3)
    reg = pd.DataFrame({"DUID": ["A"], "REGIONID": ["SA1"], "DISPATCHTYPE": ["GENERATOR"]})
    scada = pd.DataFrame(
        {"SETTLEMENTDATE": rs["SETTLEMENTDATE"][:2] + LAG, "DUID": "A", "SCADAVALUE": 1300.0}
    )
    assert scada_vs_dispatch(rs, scada, reg)["flag"].tolist() == [False, False, True]
