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
    load_day(store, n=100)
    checks = {
        c.name: c for c in run_checks(store, D, D, 36, 0.01, 50, now_utc=datetime(2026, 10, 10))
    }
    assert not checks["freshness"].passed
    assert not checks["completeness"].passed
    assert "188 missing" in checks["completeness"].detail


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
