from datetime import date, datetime

import pandas as pd

from sagrid.export import date_dimension, export_powerbi
from sagrid.monitor import report, run_checks
from sagrid.pipeline import load_texts
from sagrid.schemas import DATASETS
from tests.conftest import dispatchis_csv

D = date(2026, 10, 1)


def load_day(store, n=288):
    text = dispatchis_csv(datetime(2026, 10, 1, 0, 5), n=n)
    load_texts([("f.CSV", text)], list(DATASETS.values()), store, "run1", "f.zip")


def test_checks_pass_on_complete_fresh_day(store):
    load_day(store)
    checks = run_checks(store, D, D, 36, 0.01, 50, now_utc=datetime(2026, 10, 1, 20))
    assert all(c.passed for c in checks), report(checks)


def test_checks_fail_when_stale_and_incomplete(store):
    load_day(store, n=100)  # stops at 08:20, almost 16 hours before the end of the day
    checks = {
        c.name: c for c in run_checks(store, D, D, 12, 0.01, 50, now_utc=datetime(2026, 10, 10))
    }
    assert not checks["freshness"].passed
    assert not checks["completeness"].passed
    assert "188 missing" in checks["completeness"].detail


def test_freshness_is_judged_at_the_end_of_a_past_window(store):
    load_day(store)
    checks = {
        c.name: c for c in run_checks(store, D, D, 36, 0.01, 50, now_utc=datetime(2026, 11, 20))
    }
    assert checks["freshness"].passed, checks["freshness"].detail
    assert "0.0 h before 2026-10-02 00:00" in checks["freshness"].detail


def test_freshness_is_judged_at_now_while_the_window_is_open(store):
    load_day(store, n=100)  # up to 08:20 market time
    now_market_1820 = datetime(2026, 10, 1, 8, 20)  # 18:20 AEST
    checks = {c.name: c for c in run_checks(store, D, D, 6, 0.01, 50, now_utc=now_market_1820)}
    assert not checks["freshness"].passed
    assert "10.0 h before 2026-10-01 18:20" in checks["freshness"].detail


def test_heywood_check_reports_and_alerts_above_threshold(store):
    load_day(store)
    # Put Heywood at its import limit (-550 MW) for 6 intervals in a row.
    store.con.execute(
        "UPDATE interconnector SET MWFLOW = -548 WHERE INTERCONNECTORID = 'V-SA' "
        "AND SETTLEMENTDATE > TIMESTAMP '2026-10-01 10:00' "
        "AND SETTLEMENTDATE <= TIMESTAMP '2026-10-01 10:30'"
    )
    now = datetime(2026, 10, 1, 20)
    hw = {c.name: c for c in run_checks(store, D, D, 36, 0.01, 50, now_utc=now)}["heywood_at_limit"]
    assert hw.passed and "2.1% of 288" in hw.detail and "longest episode 30 min" in hw.detail
    assert "import 2.1%" in hw.detail and "no alert threshold" in hw.detail
    alert = {
        c.name: c
        for c in run_checks(store, D, D, 36, 0.01, 50, now_utc=now, heywood_max_at_limit_share=0.01)
    }["heywood_at_limit"]
    assert not alert.passed and "alert above 1.0%" in alert.detail


def test_checks_when_no_data(store):
    checks = run_checks(store, D, D, 36, 0.01, 50)
    assert not checks[0].passed


def test_export_writes_star_schema(store, tmp_path):
    load_day(store, n=12)
    reg = pd.DataFrame({"DUID": ["A"], "REGIONID": ["SA1"], "DISPATCHTYPE": ["GENERATOR"]})
    paths = export_powerbi(store, tmp_path, reg)
    names = {p.name for p in paths}
    for n in (
        "fact_region_summary",
        "fact_price",
        "fact_interconnector",
        "fact_constraint",
        "dim_date",
        "dim_interconnector",
        "dim_unit",
        "fact_rejects",
    ):
        assert f"{n}.parquet" in names and f"{n}.csv" in names
    fact = pd.read_parquet(tmp_path / "fact_region_summary.parquet")
    assert (fact["DATE"] == pd.Timestamp("2026-10-01")).all()


def test_date_dimension_midnight_belongs_to_previous_day():
    d = date_dimension(pd.Series(pd.to_datetime(["2026-10-02 00:00", "2026-10-02 00:05"])))
    assert d["DATE"].tolist() == [pd.Timestamp("2026-10-01"), pd.Timestamp("2026-10-02")]
    assert d.loc[0, "SEASON"] == "Spring"


def test_store_rejects_bad_identifiers(store):
    import pytest

    with pytest.raises(ValueError, match="invalid table name"):
        store.table('x"; DROP TABLE runs; --')


def test_export_dates_interval_start_tables_by_their_start(store, tmp_path):
    from tests.conftest import rooftop_csv

    # Half hours starting 23:30 on 30 Sep and 00:00 on 1 Oct.
    text = rooftop_csv(datetime(2026, 10, 1, 0, 0), n=2)
    load_texts([("r.CSV", text)], [DATASETS["rooftop_pv"]], store, "run1", "r.zip")
    export_powerbi(store, tmp_path)
    fact = pd.read_parquet(tmp_path / "fact_rooftop_pv.parquet")
    assert fact["DATE"].tolist() == [pd.Timestamp("2026-09-30"), pd.Timestamp("2026-10-01")]
    dates = pd.read_parquet(tmp_path / "dim_date.parquet")["DATE"].tolist()
    assert dates == [pd.Timestamp("2026-09-30"), pd.Timestamp("2026-10-01")]


def test_store_adds_columns_new_releases_introduce(store):
    from sagrid.schemas import REGIONSUM

    # A table as an older release created it, without the semi-scheduled columns.
    old_cols = {c: t for c, t in REGIONSUM.columns.items() if c not in REGIONSUM.optional}
    store.con.execute(
        "CREATE TABLE region_summary ("
        + ", ".join(
            f'"{c}" {"TIMESTAMP" if t == "datetime" else "VARCHAR" if t == "str" else "DOUBLE"}'
            for c, t in old_cols.items()
        )
        + ', PRIMARY KEY ("SETTLEMENTDATE", "REGIONID"))'
    )
    load_day(store, n=3)
    df = store.table("region_summary")
    assert df["SS_WIND_CLEAREDMW"].tolist() == [350.0] * 3


def test_export_flags_suspect_readings_and_penalty_values(store, tmp_path):
    from tests.conftest import scada_csv

    load_day(store, n=12)
    store.con.execute(
        'UPDATE "constraint" SET MARGINALVALUE = -27000000 '
        "WHERE SETTLEMENTDATE = TIMESTAMP '2026-10-01 00:10'"
    )
    # TORRB1 holds 700 MW for 20 intervals (stuck); HALLWF1 is not in the registry.
    text = scada_csv(datetime(2026, 10, 1, 0, 5), n=20)
    load_texts([("s.CSV", text)], [DATASETS["unit_scada"]], store, "run1", "s.zip")
    reg = pd.DataFrame(
        {
            "DUID": ["TORRB1"],
            "REGIONID": ["SA1"],
            "DISPATCHTYPE": ["GENERATOR"],
            "REG_CAP_MW": [800.0],
        }
    )
    export_powerbi(store, tmp_path, reg)
    scada = pd.read_parquet(tmp_path / "fact_unit_scada.parquet")
    torrens = scada[scada["DUID"] == "TORRB1"]
    assert (torrens["SCADA_ISSUE"] == "stuck").all() and torrens["IS_SUSPECT"].all()
    hallett = scada[scada["DUID"] == "HALLWF1"]
    assert hallett["SCADA_ISSUE"].isna().all() and hallett["IS_SUSPECT"].isna().all()

    export_powerbi(store, tmp_path)  # no registry: nothing checked, nothing claimed clean
    assert pd.read_parquet(tmp_path / "fact_unit_scada.parquet")["IS_SUSPECT"].isna().all()
    con = pd.read_parquet(tmp_path / "fact_constraint.parquet").set_index("SETTLEMENTDATE")
    assert con["IS_PENALTY"].sum() == 1 and con.loc["2026-10-01 00:10", "IS_PENALTY"].all()


def test_flag_readings_prefers_the_most_severe_overlapping_issue():
    from sagrid.investigations.i3_scada import flag_readings

    t = pd.date_range("2026-10-01 00:05", periods=4, freq="5min")
    scada = pd.DataFrame({"SETTLEMENTDATE": list(t) * 2, "DUID": ["A"] * 4 + ["B"] * 4})
    issues = pd.DataFrame(
        {
            "DUID": ["A", "A", "A", "B"],
            "ISSUE": ["stuck", "negative", "missing", "spike"],
            "FIRST": [t[0], t[1], t[3], t[2]],
            "LAST": [t[2], t[3], t[3], t[2]],
            "SEVERITY": ["high", "low", "medium", "medium"],
        }
    )
    got = flag_readings(scada, issues).tolist()
    assert got == ["stuck", "stuck", "stuck", "negative", "", "", "spike", ""]


def test_export_names_checked_clean_readings(store, tmp_path):
    from tests.conftest import scada_csv

    text = scada_csv(datetime(2026, 10, 1, 0, 5), n=5)  # too short to be stuck
    load_texts([("s.CSV", text)], [DATASETS["unit_scada"]], store, "run1", "s.zip")
    reg = pd.DataFrame(
        {
            "DUID": ["TORRB1"],
            "REGIONID": ["SA1"],
            "DISPATCHTYPE": ["GENERATOR"],
            "REG_CAP_MW": [800.0],
        }
    )
    export_powerbi(store, tmp_path, reg)
    scada = pd.read_parquet(tmp_path / "fact_unit_scada.parquet").set_index("DUID")
    assert (scada.loc["TORRB1", "SCADA_ISSUE"] == "none").all()
    assert not scada.loc["TORRB1", "IS_SUSPECT"].any()
    assert scada.loc["HALLWF1", "SCADA_ISSUE"].isna().all()
