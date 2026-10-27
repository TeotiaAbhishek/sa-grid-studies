from datetime import date, datetime, timedelta

import pytest

from sagrid.nemweb.client import FetchedFile, sha256_bytes
from sagrid.pipeline import date_range, run_ingest
from tests.conftest import daily_archive, dispatchis_csv, scada_csv


class FakeClient:
    """Serves daily archives built in memory, standing in for NEMWEB."""

    def __init__(self, tmp_path, corrupt: set[str] = frozenset()):
        self.dir = tmp_path
        self.corrupt = corrupt
        self.fetches = 0

    def files_for_dates(self, report, days):
        prefix = "PUBLIC_DISPATCHIS" if report == "dispatchis" else "PUBLIC_DISPATCHSCADA"
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
    assert store.con.execute("SELECT count(*) FROM interconnector").fetchone()[0] == 2 * 2016
    assert store.con.execute("SELECT count(*) FROM unit_scada").fetchone()[0] == 2 * 2016


def test_rerun_is_idempotent(store, tmp_path):
    client = FakeClient(tmp_path)
    run_ingest(START, START, store, client)
    before = {t: store.table_hash(t) for t in ("region_summary", "interconnector", "constraint")}
    m2 = run_ingest(START, START, store, client)
    assert m2["totals"]["files_skipped"] == 2 and m2["totals"]["files_loaded"] == 0
    m3 = run_ingest(START, START, store, client, force=True)  # backfill
    assert m3["totals"]["files_loaded"] == 2
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
