from datetime import datetime

import pandas as pd
import pytest

from sagrid.nemweb.parser import parse_aemo_csv
from sagrid.schemas import (
    CONSTRAINT,
    INTERCONNECTOR,
    PRICE,
    REGIONSUM,
    select_and_type,
    validate,
)
from tests.conftest import dispatchis_csv

T0 = datetime(2026, 10, 1, 0, 5)


def raw(table, n=2, **kw):
    return parse_aemo_csv(dispatchis_csv(T0, n=n, **kw)).tables[table]


def test_filters_to_sa_and_types_columns():
    typed, rejects = select_and_type(raw("DISPATCH_REGIONSUM"), REGIONSUM)
    assert rejects.empty
    assert set(typed["REGIONID"]) == {"SA1"}
    assert str(typed["SETTLEMENTDATE"].dtype) == "datetime64[ns]"
    assert typed["TOTALDEMAND"].dtype == "float64"
    good, bad = validate(typed, REGIONSUM)
    assert len(good) == 2 and bad.empty


def test_keeps_only_sa_interconnectors():
    typed, _ = select_and_type(raw("DISPATCH_INTERCONNECTORRES"), INTERCONNECTOR)
    assert set(typed["INTERCONNECTORID"]) == {"V-SA", "V-S-MNSP1"}


def test_unparsable_value_becomes_reject_with_reason():
    df = raw("DISPATCH_CONSTRAINT")
    df.loc[0, "MARGINALVALUE"] = "abc"
    typed, rejects = select_and_type(df, CONSTRAINT)
    assert len(typed) == 1
    assert rejects.loc[0, "REASON"] == "unparsable MARGINALVALUE;"


def test_bad_timestamp_becomes_reject():
    df = raw("DISPATCH_CONSTRAINT")
    df.loc[1, "SETTLEMENTDATE"] = "2026-10-01T00:10"
    _, rejects = select_and_type(df, CONSTRAINT)
    assert "SETTLEMENTDATE" in rejects.loc[0, "REASON"]


def test_out_of_range_demand_rejected_by_schema():
    typed, _ = select_and_type(raw("DISPATCH_REGIONSUM", demand=99999), REGIONSUM)
    good, bad = validate(typed, REGIONSUM)
    assert good.empty and len(bad) == 2
    assert "TOTALDEMAND" in bad.loc[0, "REASON"]


def test_conflicting_duplicate_keys_rejected():
    typed, _ = select_and_type(raw("DISPATCH_REGIONSUM", n=1), REGIONSUM)
    dup = pd.concat([typed, typed.assign(TOTALDEMAND=1600.0)], ignore_index=True)
    good, bad = validate(dup, REGIONSUM)
    assert good.empty and len(bad) == 2


def test_missing_required_column_raises():
    with pytest.raises(KeyError, match="TOTALDEMAND"):
        select_and_type(raw("DISPATCH_REGIONSUM").drop(columns="TOTALDEMAND"), REGIONSUM)


def test_validate_empty_frame():
    typed, _ = select_and_type(raw("DISPATCH_REGIONSUM").iloc[0:0], REGIONSUM)
    good, bad = validate(typed, REGIONSUM)
    assert good.empty and bad.empty


def test_price_keeps_sa_only():
    typed, rejects = select_and_type(raw("DISPATCH_PRICE"), PRICE)
    assert rejects.empty
    assert set(typed["REGIONID"]) == {"SA1"}
    assert typed["RRP"].tolist() == [85.0, 85.0]
    good, bad = validate(typed, PRICE)
    assert len(good) == 2 and bad.empty


def test_price_at_floor_passes_and_beyond_cap_is_rejected():
    typed, _ = select_and_type(raw("DISPATCH_PRICE", price=-1000), PRICE)
    assert validate(typed, PRICE)[1].empty
    typed, _ = select_and_type(raw("DISPATCH_PRICE", price=99999), PRICE)
    good, bad = validate(typed, PRICE)
    assert good.empty and bad["REASON"].str.contains("RRP").all()


def test_cli_dataset_choices_match_datasets():
    from sagrid.cli import build_parser
    from sagrid.schemas import DATASETS

    action = next(
        a
        for a in build_parser()._subparsers._group_actions[0].choices["ingest"]._actions
        if a.dest == "datasets"
    )
    assert set(action.choices) == set(DATASETS)


def test_semischeduled_columns_are_loaded():
    typed, _ = select_and_type(raw("DISPATCH_REGIONSUM"), REGIONSUM)
    row = typed.iloc[0]
    assert (row["SS_WIND_CLEAREDMW"], row["SS_SOLAR_CLEAREDMW"]) == (350.0, 250.0)
    assert (row["SS_WIND_UIGF"], row["SS_SOLAR_UIGF"]) == (400.0, 300.0)


def test_older_files_without_optional_columns_still_load():
    old = raw("DISPATCH_REGIONSUM").drop(columns=list(REGIONSUM.optional))
    typed, rejects = select_and_type(old, REGIONSUM)
    assert rejects.empty and len(typed) == 2
    assert typed["SS_WIND_CLEAREDMW"].isna().all()
    good, bad = validate(typed, REGIONSUM)
    assert len(good) == 2 and bad.empty
