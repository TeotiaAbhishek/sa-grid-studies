"""Test helpers that build small files in AEMO's multi-table CSV format.

These fixtures are constructed for testing, following the published MMS
file layout. They are not real AEMO data; the real-data check is the
integration test in tests/test_integration_real.py.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta

import pytest

from sagrid.store import Store

REGIONSUM_COLS = (
    "SETTLEMENTDATE,RUNNO,REGIONID,DISPATCHINTERVAL,INTERVENTION,TOTALDEMAND,"
    "DEMAND_AND_NONSCHEDGEN,DISPATCHABLEGENERATION,DISPATCHABLELOAD,"
    "NETINTERCHANGE,TOTALINTERMITTENTGENERATION,SS_WIND_CLEAREDMW,SS_SOLAR_CLEAREDMW,"
    "SS_WIND_UIGF,SS_SOLAR_UIGF,BDU_CLEAREDMW_GEN,BDU_CLEAREDMW_LOAD"
)
PRICE_COLS = (
    "SETTLEMENTDATE,RUNNO,REGIONID,DISPATCHINTERVAL,INTERVENTION,RRP,EEP,ROP,APCFLAG,"
    "MARKETSUSPENDEDFLAG,LASTCHANGED,RAISEREGRRP,LOWERREGRRP,PRICE_STATUS"
)
IC_COLS = (
    "SETTLEMENTDATE,RUNNO,INTERCONNECTORID,DISPATCHINTERVAL,INTERVENTION,MWFLOW,"
    "METEREDMWFLOW,MWLOSSES,EXPORTLIMIT,IMPORTLIMIT"
)
CON_COLS = (
    "SETTLEMENTDATE,RUNNO,CONSTRAINTID,DISPATCHINTERVAL,INTERVENTION,RHS,"
    "MARGINALVALUE,VIOLATIONDEGREE,LHS"
)


def ts(dt: datetime) -> str:
    return dt.strftime('"%Y/%m/%d %H:%M:%S"')


def dispatchis_csv(
    start: datetime,
    n: int = 1,
    footer: bool = True,
    demand: float = 1500.0,
    price: float = 85.0,
    extra_rows: list[str] | None = None,
) -> str:
    lines = ["C,NEMP.WORLD,DISPATCHIS,AEMO,PUBLIC,2026/10/01,00:00:05,1,DISPATCHIS,1"]
    lines.append("I,DISPATCH,REGIONSUM,9," + REGIONSUM_COLS)
    for i in range(n):
        t = ts(start + timedelta(minutes=5 * i))
        # gen 1300 - load 0 - netinterchange(-200, i.e. importing 200) = demand 1500
        lines.append(
            f"D,DISPATCH,REGIONSUM,9,{t},1,SA1,1,0,{demand},1550,1300,0,-200,0,350,250,400,300,20,0"
        )
        lines.append(
            f"D,DISPATCH,REGIONSUM,9,{t},1,VIC1,1,0,5000,5100,5200,0,200,0,900,100,950,100,0,0"
        )
    lines.append("I,DISPATCH,PRICE,5," + PRICE_COLS)
    for i in range(n):
        t = ts(start + timedelta(minutes=5 * i))
        lines.append(f"D,DISPATCH,PRICE,5,{t},1,SA1,1,0,{price},0,{price},0,0,{t},12.5,8.1,FIRM")
        lines.append(f"D,DISPATCH,PRICE,5,{t},1,VIC1,1,0,60,0,60,0,0,{t},10,5,FIRM")
    lines.append("I,DISPATCH,INTERCONNECTORRES,3," + IC_COLS)
    for i in range(n):
        t = ts(start + timedelta(minutes=5 * i))
        lines.append(f"D,DISPATCH,INTERCONNECTORRES,3,{t},1,V-SA,1,0,150,148,5,600,-550")
        lines.append(f"D,DISPATCH,INTERCONNECTORRES,3,{t},1,V-S-MNSP1,1,0,50,49,1,220,-200")
        lines.append(f"D,DISPATCH,INTERCONNECTORRES,3,{t},1,N-Q-MNSP1,1,0,10,9,0,100,-100")
    lines.append("I,DISPATCH,CONSTRAINT,5," + CON_COLS)
    for i in range(n):
        t = ts(start + timedelta(minutes=5 * i))
        lines.append(f"D,DISPATCH,CONSTRAINT,5,{t},1,S>>V_TEST_1,1,0,500,-12.5,0,500")
    lines += extra_rows or []
    if footer:
        lines.append(f'C,"END OF REPORT",{len(lines) + 1}')
    return "\n".join(lines) + "\n"


def scada_csv(start: datetime, n: int = 1) -> str:
    lines = [
        "C,NEMP.WORLD,DISPATCHSCADA,AEMO,PUBLIC,2026/10/01,00:00:05,1,DISPATCHSCADA,1",
        "I,DISPATCH,UNIT_SCADA,1,SETTLEMENTDATE,DUID,SCADAVALUE,LASTCHANGED",
    ]
    for i in range(n):
        t = ts(start + timedelta(minutes=5 * i))
        lines.append(f"D,DISPATCH,UNIT_SCADA,1,{t},TORRB1,700,{t}")
        lines.append(f"D,DISPATCH,UNIT_SCADA,1,{t},HALLWF1,600,{t}")
    lines.append(f'C,"END OF REPORT",{len(lines) + 1}')
    return "\n".join(lines) + "\n"


def rooftop_csv(period_end: datetime, n: int = 1, power: float = 1200.0) -> str:
    """Half-hourly rooftop PV file. INTERVAL_DATETIME is the START of the half hour."""
    lines = [
        "C,NEMP.WORLD,ROOFTOP_PV_ACTUAL_MEASUREMENT,AEMO,PUBLIC,2026/10/01,00:00:04,1,DEMAND,1",
        "I,ROOFTOP,ACTUAL,2,INTERVAL_DATETIME,REGIONID,POWER,QI,TYPE,LASTCHANGED",
    ]
    for i in range(n):
        start = period_end + timedelta(minutes=30 * i) - timedelta(minutes=30)
        t = ts(start)
        lines.append(f"D,ROOFTOP,ACTUAL,2,{t},SA1,{power},1,MEASUREMENT,{t}")
        lines.append(f"D,ROOFTOP,ACTUAL,2,{t},SA1,{power + 200},0.6,SATELLITE,{t}")
        lines.append(f"D,ROOFTOP,ACTUAL,2,{t},VIC1,3000,1,MEASUREMENT,{t}")
    lines.append(f'C,"END OF REPORT",{len(lines) + 1}')
    return "\n".join(lines) + "\n"


def zip_of(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def daily_archive(
    day: datetime, csv_builder, prefix: str, n: int = 288, step_min: int = 5
) -> bytes:
    """A zip of 5-minute (or 30-minute) zips, like NEMWEB's archive files."""
    inner = {}
    for i in range(n):
        t = day + timedelta(minutes=step_min * (i + 1))
        name = f"{prefix}_{t:%Y%m%d%H%M}_0000000{i:09d}"
        inner[name + ".zip"] = zip_of({name + ".CSV": csv_builder(t, 1).encode()})
    return zip_of(inner)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()
