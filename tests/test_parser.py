from datetime import datetime

import pytest

from sagrid.nemweb.parser import ParseError, parse_aemo_csv
from tests.conftest import dispatchis_csv

T0 = datetime(2026, 10, 1, 0, 5)


def test_parses_every_table():
    p = parse_aemo_csv(dispatchis_csv(T0, n=3))
    assert set(p.tables) == {
        "DISPATCH_REGIONSUM",
        "DISPATCH_PRICE",
        "DISPATCH_INTERCONNECTORRES",
        "DISPATCH_CONSTRAINT",
    }
    assert len(p.tables["DISPATCH_REGIONSUM"]) == 6  # 3 intervals x 2 regions
    assert p.footer_seen
    assert not p.bad_rows


def test_wanted_filter_skips_other_tables():
    p = parse_aemo_csv(dispatchis_csv(T0), wanted={"DISPATCH_CONSTRAINT"})
    assert list(p.tables) == ["DISPATCH_CONSTRAINT"]


def test_short_row_is_recorded_not_fatal():
    bad = 'D,DISPATCH,CONSTRAINT,5,"2026/10/01 00:05:00",1,ONLY_THREE'
    p = parse_aemo_csv(dispatchis_csv(T0, extra_rows=[bad]))
    assert len(p.bad_rows) == 1
    assert "expected" in p.bad_rows[0][1]
    assert len(p.tables["DISPATCH_CONSTRAINT"]) == 1


def test_data_before_header_is_recorded():
    text = "D,DISPATCH,REGIONSUM,9,x\n" + dispatchis_csv(T0)
    p = parse_aemo_csv(text)
    assert any("before header" in r for _, r in p.bad_rows)


def test_missing_footer_still_parses(caplog):
    p = parse_aemo_csv(dispatchis_csv(T0, footer=False))
    assert not p.footer_seen
    assert "truncated" in caplog.text


def test_garbage_raises():
    with pytest.raises(ParseError):
        parse_aemo_csv("this is not an aemo file\n")


def test_keep_limits_columns_but_still_checks_row_length():
    text = (
        "C,NEMP.WORLD,X,AEMO,PUBLIC,2026/10/01,00:00:00,1,X,1\n"
        "I,DISPATCH,UNIT_SOLUTION,6,SETTLEMENTDATE,DUID,A,B,TOTALCLEARED\n"
        'D,DISPATCH,UNIT_SOLUTION,6,"2026/10/01 00:05:00",HPR1,1,2,-59\n'
        'D,DISPATCH,UNIT_SOLUTION,6,"2026/10/01 00:05:00",HPR2,1,-59\n'
        'C,"END OF REPORT",5\n'
    )
    keep = {"DISPATCH_UNIT_SOLUTION": {"SETTLEMENTDATE", "DUID", "TOTALCLEARED"}}
    p = parse_aemo_csv(text, keep=keep)
    df = p.tables["DISPATCH_UNIT_SOLUTION"]
    assert list(df.columns) == ["SETTLEMENTDATE", "DUID", "TOTALCLEARED"]
    assert df["TOTALCLEARED"].tolist() == ["-59"]
    assert len(p.bad_rows) == 1  # the short row is caught against the full header
