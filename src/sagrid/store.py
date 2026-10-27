"""DuckDB storage with idempotent upserts and a rejects table."""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb
import pandas as pd

from sagrid.schemas import Dataset

IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def ident(name: str) -> str:
    """Table names are built into SQL, so only fixed lowercase identifiers are allowed."""
    if not IDENT.match(name):
        raise ValueError(f"invalid table name: {name!r}")
    return name


SQL_TYPES = {"datetime": "TIMESTAMP", "float": "DOUBLE", "int": "INTEGER", "str": "VARCHAR"}


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(self.path)
        self.con.execute(
            """
            CREATE TABLE IF NOT EXISTS rejects (
                run_id VARCHAR, dataset VARCHAR, source_file VARCHAR,
                reason VARCHAR, row_json VARCHAR
            )
            """
        )
        self.con.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id VARCHAR PRIMARY KEY, started_at TIMESTAMP, manifest_json VARCHAR
            )
            """
        )
        self.con.execute(
            """
            CREATE TABLE IF NOT EXISTS loaded_files (
                source_file VARCHAR PRIMARY KEY, sha256 VARCHAR, run_id VARCHAR
            )
            """
        )

    def ensure_table(self, ds: Dataset) -> None:
        cols = ", ".join(
            f'"{c}" {SQL_TYPES[t]} NOT NULL' if c in ds.key else f'"{c}" {SQL_TYPES[t]}'
            for c, t in ds.columns.items()
        )
        key = ", ".join(f'"{c}"' for c in ds.key)
        self.con.execute(
            f'CREATE TABLE IF NOT EXISTS "{ident(ds.name)}" ({cols}, PRIMARY KEY ({key}))'
        )
        # A table created by an older release may lack columns added since; add
        # them (empty) so loads keep working. Backfill fills them for past days.
        have = {
            r[0]
            for r in self.con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                [ds.name],
            ).fetchall()
        }
        for c, t in ds.columns.items():
            if c not in have:
                if not re.fullmatch(r"[A-Z0-9_]{1,64}", c):
                    raise ValueError(f"invalid column name: {c!r}")
                self.con.execute(f'ALTER TABLE "{ident(ds.name)}" ADD COLUMN "{c}" {SQL_TYPES[t]}')

    def upsert(self, ds: Dataset, df: pd.DataFrame) -> int:
        """Insert rows, replacing any existing row with the same key. Returns rows written."""
        if df.empty:
            return 0
        self.ensure_table(ds)
        self.con.register("incoming", df[list(ds.columns)])
        cols = ", ".join(f'"{c}"' for c in ds.columns)
        # Identifiers come from fixed Dataset definitions and are checked by ident().
        sql = f'INSERT OR REPLACE INTO "{ident(ds.name)}" ({cols}) SELECT {cols} FROM incoming'  # noqa: S608, E501
        self.con.execute(sql)
        self.con.unregister("incoming")
        return len(df)

    def add_rejects(
        self, run_id: str, dataset: str, source_file: str, rejects: pd.DataFrame
    ) -> int:
        if rejects.empty:
            return 0
        rows = [
            (run_id, dataset, source_file, str(r.pop("REASON")), json.dumps(r, default=str))
            for r in rejects.to_dict(orient="records")
        ]
        self.con.executemany("INSERT INTO rejects VALUES (?, ?, ?, ?, ?)", rows)
        return len(rows)

    def file_loaded(self, name: str, sha256: str) -> bool:
        row = self.con.execute(
            "SELECT sha256 FROM loaded_files WHERE source_file = ?", [name]
        ).fetchone()
        return row is not None and row[0] == sha256

    def mark_loaded(self, name: str, sha256: str, run_id: str) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO loaded_files VALUES (?, ?, ?)", [name, sha256, run_id]
        )

    def record_run(self, run_id: str, started_at, manifest: dict) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO runs VALUES (?, ?, ?)",
            [run_id, started_at, json.dumps(manifest, default=str)],
        )

    def scalar(self, sql: str, params: list | None = None):
        """Run a query that returns exactly one value."""
        row = self.con.execute(sql, params or []).fetchone()
        if row is None:
            raise RuntimeError(f"query returned no rows: {sql}")
        return row[0]

    def count(self, name: str) -> int:
        sql = f'SELECT count(*) FROM "{ident(name)}"'  # noqa: S608 - checked identifier
        return int(self.scalar(sql))

    def table(self, name: str) -> pd.DataFrame:
        sql = f'SELECT * FROM "{ident(name)}" ORDER BY ALL'  # noqa: S608 - checked identifier
        return self.con.execute(sql).df()

    def table_hash(self, name: str) -> str:
        """Order-independent content hash, used to prove re-runs change nothing."""
        sql = f'SELECT bit_xor(hash(t)) FROM "{ident(name)}" t'  # noqa: S608 - checked identifier
        return str(self.scalar(sql))

    def close(self) -> None:
        self.con.close()
