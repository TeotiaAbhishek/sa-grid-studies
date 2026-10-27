"""Parse AEMO's multi-table CSV format.

NEMWEB files mix several tables in one CSV. Each row starts with a record type:

    C  comment / file header and footer
    I  table header:  I,<report>,<table>,<version>,<col1>,<col2>,...
    D  data row:      D,<report>,<table>,<version>,<val1>,<val2>,...

A table is identified by "<REPORT>_<TABLE>" (for example DISPATCH_REGIONSUM).
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field

import pandas as pd

log = logging.getLogger(__name__)


class ParseError(ValueError):
    """Raised when a file is not a well-formed AEMO multi-table CSV."""


@dataclass
class ParsedFile:
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    bad_rows: list[tuple[int, str]] = field(default_factory=list)  # (line number, reason)
    footer_seen: bool = False


def parse_aemo_csv(
    text: str, wanted: set[str] | None = None, keep: dict[str, set[str]] | None = None
) -> ParsedFile:
    """Parse one AEMO CSV document into a DataFrame per table.

    Rows that do not match their table header are recorded in ``bad_rows``
    rather than raising, so one malformed row never loses a whole file.
    A file with no ``I`` rows at all raises ParseError.

    ``keep`` limits a table to the named columns. Rows are still checked
    against the full header; only the kept values are held in memory, which
    matters for wide tables (UNIT_SOLUTION has 69 columns, of which we use 8).
    """
    headers: dict[str, list[str]] = {}
    picks: dict[str, list[int] | None] = {}
    rows: dict[str, list[list[str]]] = {}
    result = ParsedFile()

    reader = csv.reader(io.StringIO(text))
    for lineno, rec in enumerate(reader, start=1):
        if not rec:
            continue
        kind = rec[0].strip()
        if kind == "C":
            if len(rec) > 1 and rec[1].strip().upper() == "END OF REPORT":
                result.footer_seen = True
            continue
        if kind not in {"I", "D"} or len(rec) < 4:
            result.bad_rows.append((lineno, f"unknown record type or short row: {rec[:3]}"))
            continue

        name = f"{rec[1].strip().upper()}_{rec[2].strip().upper()}"
        if wanted is not None and name not in wanted:
            continue

        if kind == "I":
            headers[name] = [c.strip().upper() for c in rec[4:]]
            cols_kept = (keep or {}).get(name)
            picks[name] = (
                [i for i, c in enumerate(headers[name]) if c in cols_kept] if cols_kept else None
            )
            rows.setdefault(name, [])
            continue

        cols = headers.get(name)
        if cols is None:
            result.bad_rows.append((lineno, f"data row before header for {name}"))
            continue
        values = rec[4:]
        if len(values) != len(cols):
            result.bad_rows.append(
                (lineno, f"{name}: expected {len(cols)} fields, got {len(values)}")
            )
            continue
        idx = picks[name]
        rows[name].append(values if idx is None else [values[i] for i in idx])

    if not headers:
        raise ParseError("no table header (I) rows found")
    if not result.footer_seen:
        log.warning("file has no END OF REPORT footer; it may be truncated")

    for name, cols in headers.items():
        idx = picks[name]
        kept = cols if idx is None else [cols[i] for i in idx]
        result.tables[name] = pd.DataFrame(rows[name], columns=kept, dtype="string")
    return result
