"""Table definitions and validation.

Each dataset says which AEMO table it comes from, which columns it keeps,
how to type them, its primary key, and the row checks it must pass.
Rows that fail are returned as rejects with a reason. They are never
silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import pandera.pandas as pa

NEM_TIME_FORMAT = "%Y/%m/%d %H:%M:%S"  # AEMO market time, AEST (UTC+10, no daylight saving)

SA_REGION = "SA1"
SA_INTERCONNECTORS = ("V-SA", "V-S-MNSP1")  # Heywood, Murraylink


@dataclass(frozen=True)
class Dataset:
    name: str
    source_table: str  # "<REPORT>_<TABLE>" as written in the AEMO file
    report: str  # key into nemweb.client.REPORTS
    columns: dict[str, str]  # column -> "datetime" | "float" | "int" | "str"
    key: tuple[str, ...]
    schema: pa.DataFrameSchema
    row_filter: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _num(lo: float | None = None, hi: float | None = None, nullable: bool = False) -> pa.Column:
    checks = []
    if lo is not None and hi is not None:
        checks.append(pa.Check.in_range(lo, hi))
    return pa.Column(float, checks=checks, nullable=nullable, coerce=False)


TS = pa.Column("datetime64[ns]", nullable=False)

REGIONSUM = Dataset(
    name="region_summary",
    source_table="DISPATCH_REGIONSUM",
    report="dispatchis",
    columns={
        "SETTLEMENTDATE": "datetime",
        "REGIONID": "str",
        "INTERVENTION": "int",
        "TOTALDEMAND": "float",
        "DEMAND_AND_NONSCHEDGEN": "float",
        "DISPATCHABLEGENERATION": "float",
        "DISPATCHABLELOAD": "float",
        "NETINTERCHANGE": "float",
        "TOTALINTERMITTENTGENERATION": "float",
    },
    key=("SETTLEMENTDATE", "REGIONID"),
    row_filter={"REGIONID": (SA_REGION,), "INTERVENTION": ("0",)},
    schema=pa.DataFrameSchema(
        {
            "SETTLEMENTDATE": TS,
            "REGIONID": pa.Column(str, pa.Check.isin([SA_REGION])),
            "INTERVENTION": pa.Column(int, pa.Check.isin([0])),
            "TOTALDEMAND": _num(-1000, 5000),
            "DEMAND_AND_NONSCHEDGEN": _num(-1000, 6000, nullable=True),
            "DISPATCHABLEGENERATION": _num(-1000, 6000),
            "DISPATCHABLELOAD": _num(0, 3000),
            "NETINTERCHANGE": _num(-2000, 2000),
            "TOTALINTERMITTENTGENERATION": _num(-100, 6000, nullable=True),
        },
        unique=["SETTLEMENTDATE", "REGIONID"],
    ),
)

INTERCONNECTOR = Dataset(
    name="interconnector",
    source_table="DISPATCH_INTERCONNECTORRES",
    report="dispatchis",
    columns={
        "SETTLEMENTDATE": "datetime",
        "INTERCONNECTORID": "str",
        "INTERVENTION": "int",
        "MWFLOW": "float",
        "METEREDMWFLOW": "float",
        "MWLOSSES": "float",
        "EXPORTLIMIT": "float",
        "IMPORTLIMIT": "float",
    },
    key=("SETTLEMENTDATE", "INTERCONNECTORID"),
    row_filter={"INTERCONNECTORID": SA_INTERCONNECTORS, "INTERVENTION": ("0",)},
    schema=pa.DataFrameSchema(
        {
            "SETTLEMENTDATE": TS,
            "INTERCONNECTORID": pa.Column(str, pa.Check.isin(list(SA_INTERCONNECTORS))),
            "INTERVENTION": pa.Column(int, pa.Check.isin([0])),
            "MWFLOW": _num(-1500, 1500),
            "METEREDMWFLOW": _num(-1500, 1500, nullable=True),
            "MWLOSSES": _num(-200, 200, nullable=True),
            "EXPORTLIMIT": _num(-2000, 2000, nullable=True),
            "IMPORTLIMIT": _num(-2000, 2000, nullable=True),
        },
        unique=["SETTLEMENTDATE", "INTERCONNECTORID"],
    ),
)

CONSTRAINT = Dataset(
    name="constraint",
    source_table="DISPATCH_CONSTRAINT",
    report="dispatchis",
    columns={
        "SETTLEMENTDATE": "datetime",
        "CONSTRAINTID": "str",
        "INTERVENTION": "int",
        "RHS": "float",
        "LHS": "float",
        "MARGINALVALUE": "float",
        "VIOLATIONDEGREE": "float",
    },
    key=("SETTLEMENTDATE", "CONSTRAINTID"),
    row_filter={"INTERVENTION": ("0",)},
    schema=pa.DataFrameSchema(
        {
            "SETTLEMENTDATE": TS,
            "CONSTRAINTID": pa.Column(str, pa.Check.str_length(1, 40)),
            "INTERVENTION": pa.Column(int, pa.Check.isin([0])),
            "RHS": _num(nullable=True),
            "LHS": _num(nullable=True),
            "MARGINALVALUE": _num(),
            "VIOLATIONDEGREE": _num(nullable=True),
        },
        unique=["SETTLEMENTDATE", "CONSTRAINTID"],
    ),
)

UNIT_SCADA = Dataset(
    name="unit_scada",
    source_table="DISPATCH_UNIT_SCADA",
    report="dispatch_scada",
    columns={"SETTLEMENTDATE": "datetime", "DUID": "str", "SCADAVALUE": "float"},
    key=("SETTLEMENTDATE", "DUID"),
    schema=pa.DataFrameSchema(
        {
            "SETTLEMENTDATE": TS,
            "DUID": pa.Column(str, pa.Check.str_matches(r"^[A-Z0-9_]{1,20}$")),
            "SCADAVALUE": _num(-1000, 2500),
        },
        unique=["SETTLEMENTDATE", "DUID"],
    ),
)

DATASETS = {d.name: d for d in (REGIONSUM, INTERCONNECTOR, CONSTRAINT, UNIT_SCADA)}


def select_and_type(raw: pd.DataFrame, ds: Dataset) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Filter rows, keep the dataset's columns and coerce types.

    Returns (typed, rejects). A value that cannot be parsed becomes a reject
    with the column named in the reason, instead of a silent NaN.
    """
    missing = [c for c in ds.columns if c not in raw.columns]
    if missing:
        raise KeyError(f"{ds.source_table} is missing required columns: {missing}")

    df = raw[list(ds.columns)].copy()
    for col, allowed in ds.row_filter.items():
        df = df[df[col].str.strip().isin(allowed)]
    df = df.reset_index(drop=True)

    bad = pd.Series("", index=df.index, dtype="string")
    out = pd.DataFrame(index=df.index)
    for col, kind in ds.columns.items():
        s = df[col].str.strip()
        blank = s.isna() | (s == "")
        if kind == "datetime":
            v = pd.to_datetime(s, format=NEM_TIME_FORMAT, errors="coerce").astype("datetime64[ns]")
        elif kind in {"float", "int"}:
            v = pd.to_numeric(s, errors="coerce")
        else:
            v = s
        unparsable = v.isna() & ~blank
        bad = bad.where(~unparsable, bad + f"unparsable {col}; ")
        if kind == "int":
            v = v.astype("Int64")
        out[col] = v

    rejects = df[bad != ""].assign(REASON=bad[bad != ""].str.strip())
    typed = out[bad == ""].reset_index(drop=True)
    for col, kind in ds.columns.items():
        if kind == "int" and not typed[col].isna().any():
            typed[col] = typed[col].astype("int64")
        elif kind == "float":
            typed[col] = typed[col].astype("float64")
        elif kind == "str":
            typed[col] = typed[col].astype(object)
    return typed, rejects.reset_index(drop=True)


def validate(df: pd.DataFrame, ds: Dataset) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run the dataset's schema. Returns (valid rows, rejected rows with REASON)."""
    if df.empty:
        return df, df.assign(REASON=pd.Series(dtype="string"))
    try:
        ds.schema.validate(df, lazy=True)
        return df, df.iloc[0:0].assign(REASON=pd.Series(dtype="string"))
    except pa.errors.SchemaErrors as err:
        cases = err.failure_cases
        reasons: dict[int, list[str]] = {}
        whole_frame: list[str] = []
        for _, case in cases.iterrows():
            idx = case.get("index")
            msg = f"{case.get('column')}: {case.get('check')}"
            if idx is None or pd.isna(idx):
                whole_frame.append(msg)
            else:
                reasons.setdefault(int(idx), []).append(msg)
        if whole_frame and not reasons:
            raise ValueError(
                f"{ds.name}: schema failed for the whole table: {whole_frame}"
            ) from err
        bad_idx = sorted(reasons)
        rejects = df.loc[bad_idx].copy()
        rejects["REASON"] = ["; ".join(reasons[i]) for i in bad_idx]
        return df.drop(index=bad_idx).reset_index(drop=True), rejects.reset_index(drop=True)
