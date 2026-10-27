"""Investigations I1 to I3 on a synthetic week with known, planted behaviour.

The synthetic data is built so each finding is known in advance: Heywood
sits at its limit into SA from 10:00 to 15:00, an SA constraint binds when
rooftop PV is high, and specific telemetry faults are planted in SCADA.
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from sagrid.investigations import i1_constraints, i2_heywood, i3_scada
from sagrid.investigations.common import (
    Window,
    at_limit,
    hour_adjusted_difference,
    interval_frame,
    kaplan_meier,
    runs,
    snapshot,
    wilson,
    write_report,
)
from sagrid.schemas import DATASETS

W = Window(date(2026, 10, 19), date(2026, 10, 25))


def _grid():
    return pd.date_range("2026-10-19 00:05", "2026-10-26 00:00", freq="5min")


def load_synthetic_week(store, seed=1):
    rng = np.random.default_rng(seed)
    t = _grid()
    n = len(t)
    hour = (t - pd.Timedelta(minutes=5)).hour
    midday = (hour >= 10) & (hour < 15)
    pv = np.clip(np.sin(np.pi * (hour - 6) / 12), 0, None) * 1500  # MW, zero at night
    demand = 1300 - 0.5 * pv + rng.normal(0, 30, n)
    gen = demand + 200

    def put(name, df):
        store.upsert(DATASETS[name], df)

    put(
        "region_summary",
        pd.DataFrame(
            {
                "SETTLEMENTDATE": t,
                "REGIONID": "SA1",
                "INTERVENTION": 0,
                "TOTALDEMAND": demand,
                "DEMAND_AND_NONSCHEDGEN": demand + 50,
                "DISPATCHABLEGENERATION": gen,
                "DISPATCHABLELOAD": 0.0,
                "NETINTERCHANGE": 200.0,
                "TOTALINTERMITTENTGENERATION": 0.0,
                "SS_WIND_CLEAREDMW": 400 + rng.normal(0, 50, n),
                "SS_SOLAR_CLEAREDMW": 0.2 * pv,
                "SS_WIND_UIGF": 450 + rng.normal(0, 50, n),
                "SS_SOLAR_UIGF": 0.3 * pv,
                "BDU_CLEAREDMW_GEN": 0.0,
                "BDU_CLEAREDMW_LOAD": 0.0,
            }
        ),
    )
    put(
        "price",
        pd.DataFrame(
            {
                "SETTLEMENTDATE": t,
                "REGIONID": "SA1",
                "INTERVENTION": 0,
                "RRP": np.where(midday, -20.0, 110.0) + rng.normal(0, 5, n),
                "ROP": 0.0,
                "APCFLAG": 0,
                "MARKETSUSPENDEDFLAG": 0,
                "RAISEREGRRP": 10.0,
                "LOWERREGRRP": 5.0,
            }
        ),
    )
    hey_flow = np.where(midday, 600.0, 200.0)
    put(
        "interconnector",
        pd.concat(
            [
                pd.DataFrame(
                    {
                        "SETTLEMENTDATE": t,
                        "INTERCONNECTORID": "V-SA",
                        "INTERVENTION": 0,
                        "MWFLOW": hey_flow,
                        "METEREDMWFLOW": hey_flow,
                        "MWLOSSES": 5.0,
                        "EXPORTLIMIT": 600.0,
                        "IMPORTLIMIT": -550.0,
                    }
                ),
                pd.DataFrame(
                    {
                        "SETTLEMENTDATE": t,
                        "INTERCONNECTORID": "V-S-MNSP1",
                        "INTERVENTION": 0,
                        "MWFLOW": 50.0,
                        "METEREDMWFLOW": 50.0,
                        "MWLOSSES": 1.0,
                        "EXPORTLIMIT": 220.0,
                        "IMPORTLIMIT": -200.0,
                    }
                ),
            ]
        ),
    )
    # S>TEST_SA binds mostly when rooftop PV is high; F_TEST always; VT_PEN at penalty size.
    p_bind = 1 / (1 + np.exp(-(pv - 900) / 150))
    sa_mv = np.where(rng.random(n) < p_bind, -50.0, 0.0)
    con = pd.concat(
        [
            pd.DataFrame(
                {"SETTLEMENTDATE": t, "CONSTRAINTID": "S>TEST_SA", "MARGINALVALUE": sa_mv}
            ),
            pd.DataFrame({"SETTLEMENTDATE": t, "CONSTRAINTID": "F_TEST", "MARGINALVALUE": 3.0}),
            pd.DataFrame(
                {
                    "SETTLEMENTDATE": t,
                    "CONSTRAINTID": "VT_PEN",
                    "MARGINALVALUE": np.where(hour == 3, -2.0e7, 0.0),
                }
            ),
            pd.DataFrame({"SETTLEMENTDATE": t, "CONSTRAINTID": "N>NEVER", "MARGINALVALUE": 0.0}),
            # Binds at random: nothing in the inputs explains it.
            pd.DataFrame(
                {
                    "SETTLEMENTDATE": t,
                    "CONSTRAINTID": "S>RANDOM",
                    "MARGINALVALUE": np.where(rng.random(n) < 0.2, -5.0, 0.0),
                }
            ),
        ]
    ).assign(INTERVENTION=0, RHS=100.0, LHS=100.0, VIOLATIONDEGREE=0.0)
    put("constraint", con)
    halfhours = pd.date_range("2026-10-19 00:00", "2026-10-25 23:30", freq="30min")
    hh = halfhours.hour + halfhours.minute / 60
    put(
        "rooftop_pv",
        pd.DataFrame(
            {
                "INTERVAL_DATETIME": halfhours,
                "REGIONID": "SA1",
                "TYPE": "MEASUREMENT",
                "POWER": np.clip(np.sin(np.pi * (np.floor(hh) - 6) / 12), 0, None) * 1500,
                "QI": 1.0,
            }
        ),
    )

    # SCADA: WINDA wind (noisy, one spike, one stuck hour), GASB above capacity,
    # GASC negative, SOLD missing 10 intervals. Their sum tracks dispatch.
    wind = 50 + 30 * np.sin(np.arange(n) / 40) + rng.normal(0, 3, n)
    wind[500] = wind[499] + 70  # spike
    wind[1000:1015] = 42.123  # stuck 75 minutes
    gas_b = np.full(n, 150.0) + rng.normal(0, 2, n)
    gas_b[1500:1503] = 230.0  # above 200 MW capacity
    gas_c = np.full(n, 60.0) + rng.normal(0, 1, n)
    gas_c[1700:1702] = -25.0  # negative
    sol = np.full(n, gen - wind - gas_b - gas_c)
    scada = pd.concat(
        [
            pd.DataFrame({"SETTLEMENTDATE": t, "DUID": "WINDA", "SCADAVALUE": wind}),
            pd.DataFrame({"SETTLEMENTDATE": t, "DUID": "GASB", "SCADAVALUE": gas_b}),
            pd.DataFrame({"SETTLEMENTDATE": t, "DUID": "GASC", "SCADAVALUE": gas_c}),
            pd.DataFrame({"SETTLEMENTDATE": t, "DUID": "SOLD", "SCADAVALUE": sol}).drop(
                index=range(1200, 1210)
            ),
            pd.DataFrame({"SETTLEMENTDATE": t, "DUID": "VICX", "SCADAVALUE": 300.0}),
        ]
    )
    put("unit_scada", scada)


REGISTRY = pd.DataFrame(
    {
        "DUID": ["WINDA", "GASB", "GASC", "SOLD", "VICX"],
        "REGIONID": ["SA1", "SA1", "SA1", "SA1", "VIC1"],
        "STATION": ["Wind A", "Gas B", "Gas C", "Solar D", "Vic X"],
        "DISPATCHTYPE": ["GENERATOR"] * 5,
        "FUEL": ["Wind", "Gas", "Gas", "Solar", "Coal"],
        "REG_CAP_MW": [100.0, 200.0, 80.0, 5000.0, 500.0],
    }
)


@pytest.fixture
def week(store):
    load_synthetic_week(store)
    return store


# --- helpers -------------------------------------------------------------


def test_at_limit_checks_each_direction_with_a_floor():
    flow = pd.Series([600.0, 590.0, -545.0, 0.0, 2.0])
    exp = pd.Series([600.0, 600.0, 600.0, 600.0, 0.0])
    imp = pd.Series([-550.0, -550.0, -550.0, -550.0, -100.0])
    assert at_limit(flow, exp, imp).tolist() == ["export", "export", "import", "", "export"]


def test_wilson_interval_contains_the_proportion():
    lo, hi = wilson(30, 100)
    assert lo < 0.3 < hi and round(lo, 3) == 0.219 and round(hi, 3) == 0.396


def test_runs_and_kaplan_meier():
    r = runs(pd.Series([True, True, False, True, False, True, True, True]))
    assert r["length"].tolist() == [2, 1, 3]
    assert r["at_start"].tolist() == [True, False, False]
    assert r["at_end"].tolist() == [False, False, True]
    km = kaplan_meier(np.array([5, 10, 10, 20]), np.array([1, 1, 1, 0]))
    assert km["survival"].round(3).tolist() == [0.75, 0.25]  # the 20 is censored


def test_hour_adjusted_difference_removes_time_of_day():
    hour = pd.Series([12] * 50 + [19] * 50)
    price = pd.Series([0.0] * 50 + [100.0] * 50)
    flag = pd.Series([True] * 40 + [False] * 10 + [False] * 45 + [True] * 5)
    raw = price[flag].mean() - price[~flag].mean()
    adj, used = hour_adjusted_difference(price, flag, hour)
    assert raw < -50 and adj == 0.0 and used == 45


def test_change_points_find_a_step():
    x = pd.Series([0.0] * 100 + [30.0] * 100) + np.random.default_rng(0).normal(0, 1, 200)
    assert i3_scada.change_points(x) == [100]
    assert i3_scada.change_points(pd.Series(np.random.default_rng(0).normal(0, 1, 200))) == []
    small = pd.Series([0.0] * 100 + [5.0] * 100)  # real, but below the 20 MW that matters
    assert i3_scada.change_points(small) == []


def test_stuck_and_spike_detection():
    s = pd.Series(np.r_[np.linspace(10, 20, 30), [15.5] * 14, np.linspace(20, 10, 30)])
    stuck = i3_scada.find_stuck(s)
    assert stuck["start"].tolist() == [30] and stuck["length"].tolist() == [14]
    v = pd.Series(50 + np.sin(np.arange(100) / 5))
    v[40] = 120
    assert np.flatnonzero(i3_scada.find_spikes(v, 100.0)).tolist() == [40]


# --- the investigations ---------------------------------------------------


def test_interval_frame_aligns_rooftop_half_hours(week):
    f = interval_frame(week, W)
    assert len(f) == 2016
    noon = f[f["SETTLEMENTDATE"] == pd.Timestamp("2026-10-19 12:05")]
    assert noon["ROOFTOP_PV"].iloc[0] == pytest.approx(1500.0)  # half hour starting 12:00
    assert f["HOUR"].iloc[0] == 0 and f["HOUR"].iloc[-1] == 23


def test_i1_ranks_classifies_and_finds_the_planted_driver(week):
    rep = i1_constraints.run(week, W)
    nb = rep.numbers
    assert nb["constraints_binding"] == 4
    assert nb["penalty_constraints"] == ["VT_PEN"]
    assert nb["top"][0]["CONSTRAINTID"] == "F_TEST"
    assert nb["top"][0]["CATEGORY"] == "Frequency control (FCAS)"
    models = {m["constraint"]: m for m in nb["models"]}
    assert set(models) == {"S>TEST_SA", "S>RANDOM"}
    assert not models["S>RANDOM"]["useful"]
    assert "does little better than chance" in rep.markdown
    assert "best-explained SA constraint (held-out AUC" in rep.markdown
    model = models["S>TEST_SA"]
    odds = {r["INPUT"]: r for r in model["odds"]}
    assert odds["Rooftop PV"]["ODDS_RATIO_PER_SD"] > 1
    assert model["cv_auc_mean"] > 0.8 and model["useful"]
    assert "## Limitations" in rep.markdown and "## Recommended response" in rep.markdown


def test_i2_finds_midday_limit_episodes(week):
    rep = i2_heywood.run(week, W)
    nb = rep.numbers
    assert nb["share_export"] == pytest.approx(5 / 24)
    assert nb["share_import"] == 0
    assert nb["episodes_export"] == 7 and nb["episode_median_min"] == 300
    assert set(nb["peak_hours"]) <= set(range(10, 15))
    assert nb["decomposition"]["var_daily_share"] == pytest.approx(1.0)
    assert nb["decomposition"]["var_trend_share"] == pytest.approx(0.0)
    price = next(c for c in nb["conditions"] if c["CONDITION"] == "SA price ($/MWh)")
    # Raw gap is large, but within the same hours it vanishes: price is set by time of day here.
    assert price["MEAN_DIFF_RAW"] < -100
    assert np.isnan(price["MEAN_DIFF_SAME_HOUR"])  # no hour has both states


def test_i3_registers_each_planted_fault(week):
    rep = i3_scada.run(week, W, REGISTRY)
    reg = rep.tables["issues_register"]
    found = {(r.DUID, r.ISSUE) for r in reg.itertuples()}
    assert {
        ("WINDA", "spike"),
        ("WINDA", "stuck"),
        ("GASB", "above_capacity"),
        ("GASC", "negative"),
        ("SOLD", "missing"),
    } <= found
    assert "VICX" not in set(reg["DUID"])  # other regions are out of scope
    assert rep.numbers["sa_units_reporting"] == 4
    assert reg.loc[reg["ISSUE"] == "missing", "INTERVALS"].iloc[0] == 10
    # Nothing beyond what was planted (WINDA's spike also exceeds its capacity).
    assert sorted(found) == sorted(
        {
            ("WINDA", "spike"),
            ("WINDA", "stuck"),
            ("WINDA", "above_capacity"),
            ("GASB", "above_capacity"),
            ("GASC", "negative"),
            ("SOLD", "missing"),
        }
    )
    assert (reg["ISSUE"] == "stuck").sum() == 1


def test_i3_needs_a_registry(week):
    with pytest.raises(ValueError, match="registry"):
        i3_scada.run(week, W, None)


def test_reports_are_reproducible_and_written(week, tmp_path):
    a = i2_heywood.run(week, W)
    b = i2_heywood.run(week, W)
    assert a.markdown == b.markdown  # AC-I1: same data, same report
    paths = write_report(a, tmp_path)
    names = {p.name for p in paths}
    assert {"I2-heywood-limits.md", "I2-heywood-limits.json", "by_hour.png"} <= names
    snap = snapshot(week, W, ["region_summary", "rooftop_pv", "unit_scada"])
    assert snap["tables"]["region_summary"]["rows"] == 2016
    assert snap["tables"]["rooftop_pv"]["rows"] == 336
    assert snap == snapshot(week, W, ["region_summary", "rooftop_pv", "unit_scada"])


def test_cli_investigate(tmp_path, monkeypatch, capsys):
    from sagrid.cli import main
    from sagrid.store import Store

    db = tmp_path / "s.duckdb"
    s = Store(db)
    load_synthetic_week(s)
    s.close()
    monkeypatch.setenv("SAGRID_DB_PATH", str(db))
    out = tmp_path / "reports"
    args = ["investigate", "i2", "--start", "2026-10-19", "--end", "2026-10-25", "--out", str(out)]
    assert main(args) == 0
    assert (out / "I2-heywood-limits.md").exists()
    args[1] = "i3"  # no registry configured
    assert main(args) == 3
    assert "registry" in capsys.readouterr().err


@pytest.mark.parametrize(
    "cid,expected",
    [
        ("S>NIL_MHNW1_MHNW2", "SA network"),
        ("V_S_6CGH_SA_UFLS", "SA network"),
        ("L_PEC_X_6C_6G_6H", "SA network"),
        ("V::S_NIL_MAXG_AUTO", "SA network"),
        ("V_SH_DD_MAC_FLT_965", "Other region or system"),
        ("F_T+RREG_0050", "Frequency control (FCAS)"),
        ("Q>NIL_CPWU_CLWU_RAD", "Other region or system"),
        ("N>>N-NIL_TX", "Other region or system"),
    ],
)
def test_constraint_categories(cid, expected):
    assert i1_constraints.category(cid) == expected


def test_i1_leaves_thin_price_comparisons_blank(week):
    nb = i1_constraints.run(week, W).numbers
    always = next(r for r in nb["top"] if r["CONSTRAINTID"] == "F_TEST")
    assert np.isnan(always["RRP_DIFF_SAME_HOUR"])  # binds in every interval


def test_i3_with_dispatch_targets_separates_faults_from_normal_moves(week):
    # Targets for interval t are answered by the reading stamped t + 5 minutes.
    scada = week.table("unit_scada")
    t = pd.Series(sorted(scada["SETTLEMENTDATE"].unique()))
    i = np.arange(len(t))
    clean = {
        "WINDA": 50 + 30 * np.sin(i / 40),  # the synthetic wind without noise or faults
        "GASB": np.full(len(t), 150.0),
        "GASC": np.full(len(t), 60.0),
    }
    # New units: a battery that swings with its targets, a gas unit held at a
    # setpoint, and a unit that reads 0 MW for 30 minutes while dispatched.
    batt = np.where(i % 2 == 0, -50.0, 50.0)
    held = np.full(len(t), 40.0)
    tripped = np.full(len(t), 100.0)
    tripped[800:806] = 0.0
    # GASH is 40 MW off target either side of one on-target reading: that reading is not a spike.
    gas_h = np.full(len(t), 70.0)
    gas_h[[300, 302]] = 110.0
    # SOLH, a semi-scheduled solar farm, dips 40 MW below its forecast for one interval.
    sol_h = np.full(len(t), 80.0)
    sol_h[600] = 40.0
    # BATTK is told to charge at -100 MW for 30 minutes but swings between -10 and -40,
    # with one reading (-90) briefly back near target that must not split the episode.
    batt_k = np.zeros(len(t))
    batt_k[900:906] = [-10.0, -40.0, -90.0, -40.0, -10.0, -40.0]
    batt_k_target = np.zeros(len(t))
    batt_k_target[900:906] = -100.0
    # BATTM, not offered to the market, reads 119 MW for one sample.
    batt_m = np.zeros(len(t))
    batt_m[700] = 119.0
    extra = pd.concat(
        [
            pd.DataFrame({"SETTLEMENTDATE": t, "DUID": d, "SCADAVALUE": v})
            for d, v in (
                ("BATTE", batt),
                ("GASF", held),
                ("GASG", tripped),
                ("GASH", gas_h),
                ("SOLH", sol_h),
                ("BATTK", batt_k),
                ("BATTM", batt_m),
            )
        ]
    )
    week.upsert(DATASETS["unit_scada"], extra)
    clean |= {
        "BATTE": batt,
        "GASF": held,
        "GASG": np.full(len(t), 100.0),
        "GASH": np.full(len(t), 70.0),
        "SOLH": np.full(len(t), 80.0),
        "BATTK": batt_k_target,
        "BATTM": np.zeros(len(t)),
    }
    sol = pd.concat(
        [
            pd.DataFrame(
                {
                    "SETTLEMENTDATE": t - pd.Timedelta(minutes=5),
                    "DUID": d,
                    "INTERVENTION": 0,
                    "INITIALMW": np.nan,
                    "TOTALCLEARED": v,
                    "AVAILABILITY": 0.0 if d == "BATTM" else np.nan,
                    "SEMIDISPATCHCAP": 0.0,
                    "UIGF": np.nan,
                }
            )
            for d, v in clean.items()
        ]
    )
    week.upsert(DATASETS["unit_solution"], sol)
    registry = pd.concat(
        [
            REGISTRY,
            pd.DataFrame(
                {
                    "DUID": ["BATTE", "GASF", "GASG", "GASH", "SOLH", "BATTK", "BATTM"],
                    "REGIONID": "SA1",
                    "STATION": ["E", "F", "G", "H", "Solar H", "K", "M"],
                    "DISPATCHTYPE": ["BIDIRECTIONAL"] + ["GENERATOR"] * 4 + ["BIDIRECTIONAL"] * 2,
                    "SCHEDULE_TYPE": ["SCHEDULED"] * 4 + ["SEMI-SCHEDULED"] + ["SCHEDULED"] * 2,
                    "FUEL": ["Battery", "Gas", "Gas", "Gas", "Solar", "Battery", "Battery"],
                    "REG_CAP_MW": [60.0, 50.0, 120.0, 150.0, 100.0, 200.0, 150.0],
                }
            ),
        ]
    )

    rep = i3_scada.run(week, W, registry)
    reg = rep.tables["issues_register"]
    found = {(r.DUID, r.ISSUE) for r in reg.itertuples()}
    assert {("WINDA", "spike"), ("WINDA", "stuck"), ("GASG", "no_response")} <= found
    assert "BATTE" not in set(reg["DUID"])  # swings follow dispatch: not spikes
    gas_h = reg[reg["DUID"] == "GASH"]
    assert set(gas_h["ISSUE"]) <= {"spike"} and len(gas_h) <= 2  # never the on-target reading
    assert t.iloc[301] not in set(gas_h["FIRST"])
    assert set(reg.loc[reg["DUID"] == "SOLH", "ISSUE"]) == {"brief_departure"}
    k = reg[reg["DUID"] == "BATTK"]  # one conformance episode, not alternating spikes
    assert k["ISSUE"].tolist() == ["off_target"] and k["INTERVALS"].iloc[0] == 6
    assert "below target" not in k["EVIDENCE"].iloc[0]  # charging less than asked is above target
    m = reg[reg["DUID"] == "BATTM"]
    assert m["ISSUE"].tolist() == ["spike"] and "not offered" in m["EVIDENCE"].iloc[0]
    assert ("GASF", "stuck") not in found  # constant target: a setpoint, not frozen telemetry
    assert rep.numbers["targets_used"] and rep.numbers["stuck_at_setpoint"] >= 1
    stuck = reg[(reg["DUID"] == "WINDA") & (reg["ISSUE"] == "stuck")]["EVIDENCE"].iloc[0]
    assert "target moved" in stuck
    nr = reg[reg["ISSUE"] == "no_response"].iloc[0]
    assert nr["INTERVALS"] == 6 and "dispatched to 100 MW" in nr["EVIDENCE"]
    assert "Dispatch targets" in rep.markdown
