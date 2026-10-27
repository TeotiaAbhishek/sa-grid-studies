"""Integration test against the live NEMWEB site.

Skipped by default. Run it with SAGRID_NETWORK_TESTS=1 on a machine that can
reach nemweb.com.au. It loads one real day and checks the acceptance
criteria that only real data can prove (AC-P3, AC-P4).
"""

import os
from datetime import date, timedelta

import pytest

from sagrid.monitor import run_checks
from sagrid.nemweb.client import NemwebClient
from sagrid.pipeline import run_ingest

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("SAGRID_NETWORK_TESTS") != "1",
        reason="set SAGRID_NETWORK_TESTS=1 to hit the live NEMWEB site",
    ),
]


def test_one_real_day(store, tmp_path):
    day = date.today() - timedelta(days=3)  # safely inside the daily archive
    m = run_ingest(
        day,
        day,
        store,
        NemwebClient(tmp_path),
        dataset_names=["region_summary", "interconnector", "constraint"],
    )
    assert m["totals"]["files_failed"] == 0, m["files"]
    assert store.count("region_summary") == 288
    checks = {c.name: c for c in run_checks(store, day, day, 24 * 7, 0.01, 50)}
    assert checks["completeness"].passed, checks["completeness"].detail
    assert checks["reject_rate"].passed, checks["reject_rate"].detail
    # Reconciliation tolerance is provisional until tuned on real data, so report it.
    print(checks["region_balance"].detail)
