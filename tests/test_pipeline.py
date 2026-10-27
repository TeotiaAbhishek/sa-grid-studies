from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from sagrid.nemweb.client import FetchedFile, sha256_bytes
from sagrid.pipeline import date_range, run_ingest
from tests.conftest import daily_archive, dispatchis_csv, rooftop_csv, scada_csv


class FakeClient:
    """Serves daily archives built in memory, standing in for NEMWEB."""

    def __init__(self, tmp_path, corrupt: set[str] = frozenset()):
        self.dir = tmp_path
        self.corrupt = corrupt
        self.fetches = 0

    def files_for_dates(self, report, days):
        prefix = {
            "dispatchis": "PUBLIC_DISPATCHIS",
            "dispatch_scada": "PUBLIC_DISPATCHSCADA",
            "rooftop_pv": "PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT",
        }[report]
        return [
            (f"https://nemweb.com.au/x/{prefix}_{d:%Y%m%d}.zip", f"{prefix}_{d:%Y%m%d}.zip")
            for d in days
        ]

    def fetch(self, url, name):
        self.fetches += 1
        path = self.dir / name
        if not path.exists():
            day = datetime.strptime(name.rsplit("_", 1)[1][:8], "%Y%m%d")
            if name in self.corrupt:
                data = b"not a zip"
            elif "ROOFTOP" in name:
                data = daily_archive(
                    day, rooftop_csv, "PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT", n=48, step_min=30
                )
            elif "SCADA" in name:
                data = daily_archive(day, scada_csv, "PUBLIC_DISPATCHSCADA")
            else:
                data = daily_archive(day, dispatchis_csv, "PUBLIC_DISPATCHIS")
            path.write_bytes(data)
        data = path.read_bytes()
        return FetchedFile(name, url, path, sha256_bytes(data), False)


START, END = date(2026, 10, 1), date(2026, 10, 7)


def test_full_week_loads_2016_sa_intervals(store, tmp_path):
    m = run_ingest(START, END, store, FakeClient(tmp_path))
    assert m["totals"]["files_failed"] == 0
    n = store.con.execute("SELECT count(*) FROM region_summary").fetchone()[0]
    assert n == 7 * 288 == 2016
    assert store.count("price") == 2016
    assert store.con.execute("SELECT count(*) FROM interconnector").fetchone()[0] == 2 * 2016
    assert store.con.execute("SELECT count(*) FROM unit_scada").fetchone()[0] == 2 * 2016
    assert store.count("rooftop_pv") == 7 * 48  # SA1 MEASUREMENT only


def test_rooftop_keeps_measurement_and_period_start(store, tmp_path):
    run_ingest(START, START, store, FakeClient(tmp_path), dataset_names=["rooftop_pv"])
    df = store.table("rooftop_pv")
    assert set(df["TYPE"]) == {"MEASUREMENT"} and set(df["POWER"]) == {1200.0}
    assert df["INTERVAL_DATETIME"].min() == pd.Timestamp("2026-10-01 00:00")
    assert df["INTERVAL_DATETIME"].max() == pd.Timestamp("2026-10-01 23:30")


def test_rerun_is_idempotent(store, tmp_path):
    client = FakeClient(tmp_path)
    run_ingest(START, START, store, client)
    before = {
        t: store.table_hash(t) for t in ("region_summary", "price", "interconnector", "constraint")
    }
    m2 = run_ingest(START, START, store, client)
    assert m2["totals"]["files_skipped"] == 3 and m2["totals"]["files_loaded"] == 0
    m3 = run_ingest(START, START, store, client, force=True)  # backfill
    assert m3["totals"]["files_loaded"] == 3
    after = {t: store.table_hash(t) for t in before}
    assert before == after


def test_corrupt_file_is_quarantined_and_run_continues(store, tmp_path):
    bad = "PUBLIC_DISPATCHIS_20261002.zip"
    m = run_ingest(
        START,
        date(2026, 10, 3),
        store,
        FakeClient(tmp_path, corrupt={bad}),
        dataset_names=["region_summary"],
    )
    failed = [f for f in m["files"] if f["status"] == "failed"]
    assert [f["name"] for f in failed] == [bad]
    assert store.con.execute("SELECT count(*) FROM region_summary").fetchone()[0] == 2 * 288


def test_manifest_records_lineage(store, tmp_path):
    m = run_ingest(START, START, store, FakeClient(tmp_path), dataset_names=["constraint"])
    f = m["files"][0]
    assert f["sha256"] and f["url"].startswith("https://nemweb.com.au/")
    assert m["git_commit"] and m["code_version"]
    assert store.con.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_date_range_validation():
    assert len(date_range(START, END)) == 7
    with pytest.raises(ValueError, match="before"):
        date_range(END, START)
    with pytest.raises(ValueError, match="400"):
        date_range(START, START + timedelta(days=500))


def test_reload_replaces_a_files_old_rejects(store):
    from sagrid.pipeline import load_texts
    from sagrid.schemas import DATASETS

    t = datetime(2026, 10, 1, 0, 5)
    bad = dispatchis_csv(t, price=30000.0)  # above the market price cap
    good = dispatchis_csv(t)
    price = [DATASETS["price"]]
    _, j = load_texts([("a.CSV", bad)], price, store, "r1", "A.zip")
    load_texts([("b.CSV", bad)], price, store, "r1", "B.zip")
    assert j["price"] == 1

    load_texts([("a.CSV", good)], price, store, "r2", "A.zip")  # A fixed upstream, reloaded
    left = store.con.execute("SELECT source_file, count(*) FROM rejects GROUP BY 1").fetchall()
    assert left == [("B.zip", 1)]
