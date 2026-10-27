"""The generator registry: region, dispatch type, fuel and capacity per unit (DUID).

Two sources are supported:

- ``sagrid registry`` builds it from AEMO's monthly MMSDM registration
  tables on NEMWEB (DUDETAILSUMMARY, DUDETAIL, DUALLOC, GENUNITS). This is
  the default: it needs no manual step and comes from the same host as the
  rest of the data.
- AEMO's NEM Registration and Exemption List workbook, downloaded by hand.
  Sheet names and headings have changed over the years, so every sheet is
  searched for a header row containing DUID and Region.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from sagrid.schemas import DUID_PATTERN

log = logging.getLogger(__name__)

# canonical column -> accepted headings (lower-cased, whitespace collapsed)
ALIASES = {
    "DUID": {"duid"},
    "REGIONID": {"region"},
    "STATION": {"station name"},
    "DISPATCHTYPE": {"dispatch type"},
    "FUEL": {"fuel source - primary", "fuel source - descriptor", "fuel source primary"},
    "TECHNOLOGY": {"technology type - primary", "technology type"},
    "REG_CAP_MW": {"reg cap (mw)", "reg cap generation (mw)", "reg cap (mw) generation"},
}
REQUIRED = ("DUID", "REGIONID", "DISPATCHTYPE")


class RegistryError(ValueError):
    pass


def _norm(s: object) -> str:
    return " ".join(str(s).strip().lower().split())


def _find_table(raw: pd.DataFrame) -> pd.DataFrame | None:
    for i in range(min(len(raw), 20)):
        heads = [_norm(v) for v in raw.iloc[i].tolist()]
        if "duid" in heads and "region" in heads:
            df = raw.iloc[i + 1 :].copy()
            df.columns = heads
            return df
    return None


def load_registry(path: Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise RegistryError(f"registry file not found: {path}")
    if path.suffix.lower() in {".csv", ".parquet"}:
        built = pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)
        missing = [c for c in REQUIRED if c not in built]
        if missing:
            raise RegistryError(f"{path.name} is missing columns {missing}")
        return built
    sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=str)
    frames = []
    for name, raw in sheets.items():
        table = _find_table(raw)
        if table is None:
            continue
        out = pd.DataFrame()
        for canon, names in ALIASES.items():
            match = next((c for c in table.columns if c in names), None)
            if match is not None:
                out[canon] = table[match].astype("string").str.strip()
        if all(c in out for c in REQUIRED):
            log.info("registry sheet used", extra={"sheet": name, "rows": len(out)})
            frames.append(out)
    if not frames:
        raise RegistryError(f"no sheet in {path.name} has DUID, Region and Dispatch Type columns")

    reg = pd.concat(frames, ignore_index=True)
    reg = reg.dropna(subset=["DUID"])
    reg = reg[reg["DUID"].str.match(DUID_PATTERN, na=False)]
    reg["DISPATCHTYPE"] = reg["DISPATCHTYPE"].str.upper().str.replace(" UNIT", "", regex=False)
    if "REG_CAP_MW" in reg:
        reg["REG_CAP_MW"] = pd.to_numeric(reg["REG_CAP_MW"], errors="coerce")
    # A DUID can be listed on more than one sheet; keep the first, warn on conflicts.
    dup = reg[reg.duplicated("DUID", keep=False)]
    conflicts = dup.groupby("DUID")["REGIONID"].nunique()
    for duid in conflicts[conflicts > 1].index:
        log.warning("DUID listed in more than one region", extra={"duid": duid})
    return reg.drop_duplicates("DUID").reset_index(drop=True)


# --- Building the registry from NEMWEB's MMSDM monthly archive --------------

MMSDM_DIR = (
    "/Data_Archive/Wholesale_Electricity/MMSDM/{y}/MMSDM_{y}_{m:02d}/"
    "MMSDM_Historical_Data_SQLLoader/DATA/"
)
MMSDM_TABLES = ("DUDETAILSUMMARY", "DUDETAIL", "DUALLOC", "GENUNITS")
MMS_TIME = "%Y/%m/%d %H:%M:%S"


def mmsdm_file(table: str, y: int, m: int) -> tuple[str, str]:
    """(URL path, safe local name) of one monthly registration table.

    AEMO names these files with "#" separators, which must be sent as %23.
    """
    remote = f"PUBLIC_ARCHIVE%23{table}%23FILE01%23{y}{m:02d}010000.zip"
    local = f"PUBLIC_ARCHIVE_{table}_FILE01_{y}{m:02d}010000.zip"
    return MMSDM_DIR.format(y=y, m=m) + remote, local


def _dates(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s.str.strip(), format=MMS_TIME, errors="coerce")


def _latest(df: pd.DataFrame, on: datetime, date_col: str) -> pd.DataFrame:
    """Rows in force on `on`: effective by then, latest date and version per DUID."""
    d = df.assign(_D=_dates(df[date_col]))
    d = d[d["_D"] <= on]
    if "VERSIONNO" in d:
        d = d.assign(_V=pd.to_numeric(d["VERSIONNO"], errors="coerce"))
    else:
        d = d.assign(_V=0)
    top = d.groupby("DUID")[["_D", "_V"]].transform("max")
    return d[(d["_D"] == top["_D"]) & (d["_V"] == top["_V"])]


def build_registry(tables: dict[str, pd.DataFrame], on: datetime) -> pd.DataFrame:
    """Combine the four registration tables into one row per DUID, as in force on `on`."""
    summary = tables["DUDETAILSUMMARY"].copy()
    start, end = _dates(summary["START_DATE"]), _dates(summary["END_DATE"])
    summary = summary[(start <= on) & (end > on)].assign(_S=start)
    summary = summary.sort_values("_S").drop_duplicates("DUID", keep="last")
    reg = pd.DataFrame(
        {
            "DUID": summary["DUID"].str.strip(),
            "REGIONID": summary["REGIONID"].str.strip(),
            "STATION": summary["STATIONID"].str.strip(),
            "DISPATCHTYPE": summary["DISPATCHTYPE"].str.strip().str.upper(),
            "SCHEDULE_TYPE": summary["SCHEDULE_TYPE"].str.strip(),
        }
    )

    detail = _latest(tables["DUDETAIL"], on, "EFFECTIVEDATE").drop_duplicates("DUID")
    caps = pd.DataFrame(
        {
            "DUID": detail["DUID"].str.strip(),
            "REG_CAP_MW": pd.to_numeric(detail["REGISTEREDCAPACITY"], errors="coerce"),
            "MAX_CAP_MW": pd.to_numeric(detail["MAXCAPACITY"], errors="coerce"),
        }
    )
    reg = reg.merge(caps, on="DUID", how="left")

    # A DUID can be made of several generating sets (e.g. two stages of a wind farm).
    alloc = _latest(tables["DUALLOC"], on, "EFFECTIVEDATE")[["DUID", "GENSETID"]]
    gens = tables["GENUNITS"][["GENSETID", "CO2E_ENERGY_SOURCE"]]
    fuel = (
        alloc.assign(DUID=alloc["DUID"].str.strip(), GENSETID=alloc["GENSETID"].str.strip())
        .merge(gens.assign(GENSETID=gens["GENSETID"].str.strip()), on="GENSETID", how="left")
        .groupby("DUID")["CO2E_ENERGY_SOURCE"]
        .agg(lambda v: "; ".join(sorted({x.strip() for x in v.dropna() if x.strip()})))
        .rename("FUEL")
        .reset_index()
    )
    reg = reg.merge(fuel, on="DUID", how="left")
    reg["FUEL"] = reg["FUEL"].replace("", pd.NA)
    reg = reg[reg["DUID"].str.match(DUID_PATTERN, na=False)]
    return reg.sort_values("DUID").reset_index(drop=True)


def fetch_registry(client, on: date, months_back: int = 3) -> tuple[pd.DataFrame, str]:
    """Download the newest monthly registration tables published before `on` and build the registry.

    MMSDM months are published a few weeks after they end, so up to
    `months_back` earlier months are tried. Returns (registry, month used).
    """
    from urllib.parse import urljoin

    from sagrid.nemweb.client import BASE_URL, DownloadError, iter_csv_texts
    from sagrid.nemweb.parser import parse_aemo_csv

    y, m = on.year, on.month
    last_error: Exception | None = None
    for _ in range(months_back):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        try:
            tables = {}
            for table in MMSDM_TABLES:
                path, local = mmsdm_file(table, y, m)
                fetched = client.fetch(urljoin(BASE_URL, path), local)
                for _member, text in iter_csv_texts(fetched.path.read_bytes()):
                    parsed = parse_aemo_csv(text, wanted={f"PARTICIPANT_REGISTRATION_{table}"})
                    tables[table] = parsed.tables[f"PARTICIPANT_REGISTRATION_{table}"]
            on_dt = datetime.combine(on, datetime.min.time())
            return build_registry(tables, on_dt), f"{y}-{m:02d}"
        except (DownloadError, KeyError) as exc:
            log.warning(
                "registry month not available", extra={"month": f"{y}-{m:02d}", "error": str(exc)}
            )
            last_error = exc
    raise RegistryError(f"no MMSDM registration tables found before {on}: {last_error}")
