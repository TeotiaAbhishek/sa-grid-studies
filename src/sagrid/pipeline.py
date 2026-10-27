"""The ingest pipeline: download -> parse -> type -> validate -> store, with a run manifest."""

from __future__ import annotations

import logging
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zipfile import BadZipFile

import pandas as pd

from sagrid import __version__
from sagrid.nemweb.client import DownloadError, NemwebClient, iter_csv_texts
from sagrid.nemweb.parser import ParseError, parse_aemo_csv
from sagrid.schemas import DATASETS, Dataset, select_and_type, validate
from sagrid.store import Store

log = logging.getLogger(__name__)


@dataclass
class FileResult:
    name: str
    url: str
    sha256: str
    status: str  # loaded | skipped_unchanged | failed
    rows: dict[str, int] = field(default_factory=dict)
    rejects: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def git_commit() -> str:
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def date_range(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError(f"end date {end} is before start date {start}")
    if (end - start).days > 400:
        raise ValueError("date range longer than 400 days; NEMWEB daily archives keep ~13 months")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def load_texts(
    texts, datasets: list[Dataset], store: Store, run_id: str, source: str
) -> tuple[dict[str, int], dict[str, int]]:
    """Parse CSV documents and load every dataset they contain.

    A daily archive holds 288 five-minute files, so tables are collected
    across all of them first and typed, validated and stored in one batch.
    """
    wanted = {d.source_table for d in datasets}
    collected: dict[str, list[pd.DataFrame]] = {}
    malformed: list[tuple[str, int, str]] = []
    for member, text in texts:
        parsed = parse_aemo_csv(text, wanted=wanted)
        malformed += [(member, n, r) for n, r in parsed.bad_rows]
        for name, df in parsed.tables.items():
            collected.setdefault(name, []).append(df)

    rows: dict[str, int] = {}
    rejects: dict[str, int] = {}
    store.clear_rejects("_malformed", source)
    if malformed:
        bad = pd.DataFrame(malformed, columns=["member", "line", "REASON"])
        rejects["_malformed"] = store.add_rejects(run_id, "_malformed", source, bad)
    for ds in datasets:
        frames = collected.get(ds.source_table)
        if not frames:
            continue
        raw = pd.concat(frames, ignore_index=True)
        typed, bad_types = select_and_type(raw, ds)
        typed = typed.drop_duplicates()  # identical repeats are harmless; conflicts are not
        good, bad_checks = validate(typed, ds)
        rows[ds.name] = store.upsert(ds, good)
        store.clear_rejects(ds.name, source)
        rejects[ds.name] = store.add_rejects(
            run_id, ds.name, source, bad_types
        ) + store.add_rejects(run_id, ds.name, source, bad_checks)
    return rows, rejects


def run_ingest(
    start: date,
    end: date,
    store: Store,
    client: NemwebClient,
    dataset_names: list[str] | None = None,
    force: bool = False,
) -> dict:
    """Ingest a date range. One bad file is logged and skipped; the run carries on."""
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    started = datetime.now(UTC)
    days = date_range(start, end)
    selected = [DATASETS[n] for n in (dataset_names or list(DATASETS))]
    by_report: dict[str, list[Dataset]] = {}
    for ds in selected:
        by_report.setdefault(ds.report, []).append(ds)

    results: list[FileResult] = []
    for report, datasets in by_report.items():
        try:
            files = client.files_for_dates(report, days)
        except DownloadError as exc:
            log.error("could not list files", extra={"report": report, "error": str(exc)})
            results.append(FileResult(report, "", "", "failed", error=str(exc)))
            continue

        for url, name in files:
            try:
                fetched = client.fetch(url, name)
                if not force and store.file_loaded(fetched.name, fetched.sha256):
                    results.append(FileResult(name, url, fetched.sha256, "skipped_unchanged"))
                    continue
                texts = iter_csv_texts(fetched.path.read_bytes())
                r, j = load_texts(texts, datasets, store, run_id, name)
                res = FileResult(name, url, fetched.sha256, "loaded", rows=r, rejects=j)
                store.mark_loaded(fetched.name, fetched.sha256, run_id)
                results.append(res)
                log.info(
                    "file loaded", extra={"file": name, "rows": res.rows, "rejects": res.rejects}
                )
            except (DownloadError, ParseError, BadZipFile, OSError, ValueError, KeyError) as exc:
                # Error recovery: quarantine the file in the manifest and keep going.
                log.error("file failed", extra={"file": name, "error": repr(exc)})
                results.append(FileResult(name, url, "", "failed", error=repr(exc)))

    manifest = {
        "run_id": run_id,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "datasets": [d.name for d in selected],
        "code_version": __version__,
        "git_commit": git_commit(),
        "files": [vars(r) for r in results],
        "totals": {
            "files_loaded": sum(r.status == "loaded" for r in results),
            "files_skipped": sum(r.status == "skipped_unchanged" for r in results),
            "files_failed": sum(r.status == "failed" for r in results),
            "rows": _sum_dicts(r.rows for r in results),
            "rejects": _sum_dicts(r.rejects for r in results),
        },
    }
    store.record_run(run_id, started, manifest)
    return manifest


def _sum_dicts(ds) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in ds:
        for k, v in d.items():
            out[k] = out.get(k, 0) + v
    return out
