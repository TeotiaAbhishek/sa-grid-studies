"""Load AEMO's NEM Registration and Exemption List (generator registry).

AEMO publishes it as an Excel workbook on aemo.com.au. Sheet names and
column headings have changed over the years, so we search every sheet for a
header row containing DUID and Region rather than hard-coding positions.
The file is downloaded by hand (see docs/user-guide.md) because its URL is
not stable.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

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
    reg = reg[reg["DUID"].str.fullmatch(r"[A-Z0-9_]{1,20}", na=False)]
    reg["DISPATCHTYPE"] = reg["DISPATCHTYPE"].str.upper().str.replace(" UNIT", "", regex=False)
    if "REG_CAP_MW" in reg:
        reg["REG_CAP_MW"] = pd.to_numeric(reg["REG_CAP_MW"], errors="coerce")
    # A DUID can be listed on more than one sheet; keep the first, warn on conflicts.
    dup = reg[reg.duplicated("DUID", keep=False)]
    conflicts = dup.groupby("DUID")["REGIONID"].nunique()
    for duid in conflicts[conflicts > 1].index:
        log.warning("DUID listed in more than one region", extra={"duid": duid})
    return reg.drop_duplicates("DUID").reset_index(drop=True)
