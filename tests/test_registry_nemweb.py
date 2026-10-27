"""Building the generator registry from NEMWEB's MMSDM registration tables.

The headers are copied from AEMO's September 2026 files; the rows are
constructed for the test.
"""

from datetime import date, datetime

import pandas as pd
import pytest
import responses

from sagrid.nemweb.client import BASE_URL, NemwebClient
from sagrid.nemweb.parser import parse_aemo_csv
from sagrid.registry import RegistryError, build_registry, fetch_registry, mmsdm_files
from sagrid.schemas import UNIT_SCADA, select_and_type, validate
from tests.conftest import zip_of

HEADERS = {
    "DUDETAILSUMMARY": "DUID,START_DATE,END_DATE,DISPATCHTYPE,CONNECTIONPOINTID,REGIONID,"
    "STATIONID,PARTICIPANTID,LASTCHANGED,TRANSMISSIONLOSSFACTOR,STARTTYPE,"
    "DISTRIBUTIONLOSSFACTOR,MINIMUM_ENERGY_PRICE,MAXIMUM_ENERGY_PRICE,SCHEDULE_TYPE",
    "DUDETAIL": "EFFECTIVEDATE,DUID,VERSIONNO,CONNECTIONPOINTID,VOLTLEVEL,REGISTEREDCAPACITY,"
    "AGCCAPABILITY,DISPATCHTYPE,MAXCAPACITY",
    "DUALLOC": "EFFECTIVEDATE,VERSIONNO,DUID,GENSETID,LASTCHANGED",
    "GENUNITS": "GENSETID,STATIONID,SETLOSSFACTOR,CDINDICATOR,AGCFLAG,SPINNINGFLAG,VOLTLEVEL,"
    "REGISTEREDCAPACITY,DISPATCHTYPE,STARTTYPE,MKTGENERATORIND,NORMALSTATUS,MAXCAPACITY,"
    "GENSETTYPE,GENSETNAME,LASTCHANGED,CO2E_EMISSIONS_FACTOR,CO2E_ENERGY_SOURCE",
}
T = '"{}/01 00:00:00"'
OPEN = "2999/12"


def summary(duid, start, end, dtype, region, station):
    return (
        f"{duid},{T.format(start)},{T.format(end)},{dtype},CP,{region},{station},P,x,1,SLOW,1,0,0,"
        "SCHEDULED"
    )


ROWS = {
    "DUDETAILSUMMARY": [
        # LKBONNY2's first row ended on 1 Jul and was replaced; OLDGEN has retired.
        summary("LKBONNY2", "2026/01", "2026/07", "GENERATOR", "SA1", "LKBONNY_2"),
        summary("LKBONNY2", "2026/07", OPEN, "GENERATOR", "SA1", "LKBONNY_2"),
        summary("HPR1", "2026/07", OPEN, "BIDIRECTIONAL", "SA1", "HORNSDPR"),
        summary("OSB-AG", "2020/01", OPEN, "GENERATOR", "SA1", "OSBORNE"),
        summary("OLDGEN", "2010/01", "2020/01", "GENERATOR", "SA1", "OLD"),
        summary("VICX", "2020/01", OPEN, "GENERATOR", "VIC1", "VICX"),
    ],
    "DUDETAIL": [
        f"{T.format('2020/01')},LKBONNY2,1,SMAY2,33,140,N,GENERATOR,140",
        f"{T.format('2024/06')},LKBONNY2,1,SMAY2,33,159,N,GENERATOR,159",
        f"{T.format('2030/01')},LKBONNY2,1,SMAY2,33,999,N,GENERATOR,999",  # future: ignored
        f"{T.format('2024/09')},HPR1,1,SMTL3H,275,150,Y,BIDIRECTIONAL,150",
        # Real pattern: an old row with a high version, newer rows back at version 1,
        # not in date order, and a same-day correction (version 2 wins over 1).
        f"{T.format('2024/06')},OSB-AG,1,SOSB,66,170,Y,GENERATOR,170",
        f"{T.format('2000/02')},OSB-AG,39,SOSB,66,180,Y,GENERATOR,204",
        f"{T.format('2024/06')},OSB-AG,2,SOSB,66,180,Y,GENERATOR,204",
        f"{T.format('2011/01')},OSB-AG,1,SOSB,66,180,Y,GENERATOR,204",
        f"{T.format('2020/01')},VICX,1,VX,66,500,Y,GENERATOR,500",
    ],
    "DUALLOC": [
        f"{T.format('2007/05')},1,LKBONNY2,LKBONNEY2A,x",
        f"{T.format('2007/05')},1,LKBONNY2,LKBONNEY2B,x",
        f"{T.format('2024/09')},1,HPR1,HPR1,x",
        f"{T.format('2000/02')},39,OSB-AG,OSB-OLD,x",
        f"{T.format('2016/07')},1,OSB-AG,OSB01,x",
        f"{T.format('2016/07')},1,OSB-AG,OSB02,x",
    ],
    "GENUNITS": [
        "LKBONNEY2A,,,Y,N,N,33,63,GROSS/NET,SLOW,Y,N,63,GENERATOR,,x,0,Wind",
        "LKBONNEY2B,,,Y,N,N,33,96,GROSS/NET,SLOW,Y,N,96,GENERATOR,,x,0,Wind",
        'HPR1,,,N,Y,N,275,150,GROSS/NET,SLOW,Y,N,150,BIDIRECTIONAL,,x,0,"Battery Storage"',
        'OSB01,,,N,Y,N,66,118,GROSS/NET,SLOW,Y,N,136,GENERATOR,,x,0,"Natural Gas (Pipeline)"',
        'OSB02,,,N,Y,N,66,62,GROSS/NET,SLOW,Y,N,68,GENERATOR,,x,0,"Natural Gas (Pipeline)"',
        "OSB-OLD,,,N,Y,N,66,180,GROSS/NET,SLOW,Y,N,180,GENERATOR,,x,0,Diesel",
    ],
}


def mmsdm_csv(table: str) -> str:
    lines = [f"C,SETP.WORLD,DVD_{table},AEMO,PUBLIC,2026/10/09,15:36:34,1,MONTHLY_ARCHIVE,1"]
    lines.append(f"I,PARTICIPANT_REGISTRATION,{table},1," + HEADERS[table])
    lines += [f"D,PARTICIPANT_REGISTRATION,{table},1," + r for r in ROWS[table]]
    lines.append(f'C,"END OF REPORT",{len(lines) + 1}')
    return "\n".join(lines) + "\n"


def parsed_tables():
    return {
        t: parse_aemo_csv(mmsdm_csv(t)).tables[f"PARTICIPANT_REGISTRATION_{t}"] for t in HEADERS
    }


def test_build_registry_uses_rows_in_force_and_joins_fuel():
    reg = build_registry(parsed_tables(), datetime(2026, 10, 19)).set_index("DUID")
    assert sorted(reg.index) == ["HPR1", "LKBONNY2", "OSB-AG", "VICX"]  # OLDGEN retired
    assert reg.loc["LKBONNY2", "REG_CAP_MW"] == 159  # latest effective, not the future row
    assert reg.loc["LKBONNY2", "FUEL"] == "Wind"  # two gensets, one fuel
    assert reg.loc["HPR1", "DISPATCHTYPE"] == "BIDIRECTIONAL"
    assert reg.loc["HPR1", "FUEL"] == "Battery Storage"
    assert reg.loc["OSB-AG", "STATION"] == "OSBORNE"
    # latest date first, then the highest version on that date
    assert reg.loc["OSB-AG", ["REG_CAP_MW", "MAX_CAP_MW"]].tolist() == [180, 204]
    assert (
        reg.loc["OSB-AG", "FUEL"] == "Natural Gas (Pipeline)"
    )  # both current gensets, not OSB-OLD
    assert pd.isna(reg.loc["VICX", "FUEL"])  # no genset mapping
    assert reg.loc["VICX", "REGIONID"] == "VIC1"


@responses.activate
def test_fetch_registry_falls_back_to_an_earlier_month(tmp_path):
    for table in HEADERS:
        for path, _ in mmsdm_files(table, 2026, 9):
            responses.get(BASE_URL + path, status=404)
        path, _ = mmsdm_files(table, 2026, 8)[0]
        body = zip_of(
            {f"PUBLIC_ARCHIVE#{table}#FILE01#202608010000.CSV": mmsdm_csv(table).encode()}
        )
        responses.get(BASE_URL + path, body=body)
    reg, month = fetch_registry(NemwebClient(tmp_path, retries=0), date(2026, 10, 19))
    assert month == "2026-08" and len(reg) == 4
    assert "%23DUDETAILSUMMARY%23" in responses.calls[0].request.url  # "#" sent encoded


@responses.activate
def test_fetch_registry_reads_older_dvd_file_names(tmp_path):
    for table in HEADERS:
        new, old = mmsdm_files(table, 2024, 1)
        responses.get(BASE_URL + new[0], status=404)
        body = zip_of({f"PUBLIC_DVD_{table}_202401010000.CSV": mmsdm_csv(table).encode()})
        responses.get(BASE_URL + old[0], body=body)
    reg, month = fetch_registry(NemwebClient(tmp_path, retries=0), date(2024, 2, 19))
    assert month == "2024-01" and "OSB-AG" in set(reg["DUID"])
    assert "PUBLIC_DVD_DUDETAIL_202401010000.zip" in {
        c.request.url.rsplit("/", 1)[1] for c in responses.calls
    }


@responses.activate
def test_fetch_registry_gives_up_cleanly(tmp_path):
    responses.add(responses.GET, BASE_URL + "/", status=404)  # nothing else registered
    with pytest.raises(RegistryError, match="no MMSDM registration tables"):
        fetch_registry(NemwebClient(tmp_path, retries=0), date(2026, 10, 19), months_back=2)


@pytest.mark.parametrize("duid", ["OSB-AG", "TARONG#1", "W/HOE#1", "BARRON-2", "HPR1"])
def test_real_duids_with_punctuation_are_valid(duid):
    raw = pd.DataFrame(
        {"SETTLEMENTDATE": ["2026/10/19 00:05:00"], "DUID": [duid], "SCADAVALUE": ["10"]}
    )
    typed, rejects = select_and_type(raw, UNIT_SCADA)
    good, bad = validate(typed, UNIT_SCADA)
    assert rejects.empty and bad.empty and len(good) == 1


def test_bad_duids_still_rejected():
    raw = pd.DataFrame(
        {
            "SETTLEMENTDATE": ["2026/10/19 00:05:00"] * 2,
            "DUID": ["a b", "X;DROP"],
            "SCADAVALUE": ["1", "1"],
        }
    )
    typed, _ = select_and_type(raw, UNIT_SCADA)
    good, bad = validate(typed, UNIT_SCADA)
    assert good.empty and len(bad) == 2


def test_cli_registry_writes_csv_that_export_and_i3_pick_up(tmp_path, monkeypatch):
    import sagrid.registry as registry_mod
    from sagrid.cli import _registry_file, main
    from sagrid.config import load_settings

    reg = build_registry(parsed_tables(), datetime(2026, 10, 19))
    monkeypatch.setattr(registry_mod, "fetch_registry", lambda client, on: (reg, "2026-09"))
    monkeypatch.setenv("SAGRID_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SAGRID_DB_PATH", str(tmp_path / "s.duckdb"))
    monkeypatch.setenv("SAGRID_CACHE_DIR", str(tmp_path / "cache"))
    assert main(["registry", "--on", "2026-10-19"]) == 0
    s = load_settings(None)
    assert _registry_file(s) == tmp_path / "registry.csv"
    loaded = registry_mod.load_registry(tmp_path / "registry.csv")
    assert set(loaded["DUID"]) == {"HPR1", "LKBONNY2", "OSB-AG", "VICX"}
