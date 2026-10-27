"""Download files from NEMWEB defensively.

- Only hosts on an allowlist are ever requested.
- File names come from the remote listing, so they are sanitised before
  touching the local disk.
- Network errors are retried with exponential backoff.
- Downloads are cached by name with a SHA-256 checksum, so re-runs are cheap
  and a corrupted cache file is detected and fetched again.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger(__name__)

ALLOWED_HOSTS = {"nemweb.com.au", "www.nemweb.com.au"}
BASE_URL = "https://nemweb.com.au"

# Daily archives keep roughly the last 13 months; "Current" keeps the last day or two.
REPORTS: dict[str, dict[str, str | int]] = {
    "dispatchis": {
        "archive": "/Reports/Archive/DispatchIS_Reports/",
        "current": "/Reports/Current/DispatchIS_Reports/",
        "pattern": r"PUBLIC_DISPATCHIS_(\d{8})",
    },
    "dispatch_scada": {
        "archive": "/Reports/Archive/Dispatch_SCADA/",
        "current": "/Reports/Current/Dispatch_SCADA/",
        "pattern": r"PUBLIC_DISPATCHSCADA_(\d{8})",
    },
    # Rooftop PV estimates are half-hourly. The archive holds one zip per
    # week, named by its first day. A Current file is named by the end of
    # its half hour, so the 00:00 file belongs to the previous day.
    "rooftop_pv": {
        "archive": "/Reports/Archive/ROOFTOP_PV/ACTUAL/",
        "current": "/Reports/Current/ROOFTOP_PV/ACTUAL/",
        "pattern": r"PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT_(\d{8})(\d{6})?",
        "archive_days": 7,
        "name_marks_end_min": 30,
    },
}

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")
MAX_ZIP_MEMBER_BYTES = 500 * 1024 * 1024  # guard against zip bombs
HREF = re.compile(r'href="([^"]+\.zip)"', re.IGNORECASE)


class DownloadError(RuntimeError):
    pass


@dataclass
class FetchedFile:
    name: str
    url: str
    path: Path
    sha256: str
    from_cache: bool


def check_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise DownloadError(f"refusing to fetch non-allowlisted URL: {url}")
    return url


def safe_name(name: str) -> str:
    name = name.rsplit("/", 1)[-1]
    if not SAFE_NAME.match(name) or name in {".", ".."}:
        raise DownloadError(f"unsafe file name from remote listing: {name!r}")
    return name


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class NemwebClient:
    def __init__(
        self,
        cache_dir: Path,
        session: requests.Session | None = None,
        retries: int = 4,
        backoff: float = 2.0,
        timeout: float = 60.0,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "sagrid/0.1 (portfolio project)")
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout

    def _get(self, url: str) -> bytes:
        check_url(url)
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 404:
                    raise DownloadError(f"not found: {url}")
                resp.raise_for_status()
                return resp.content
            except DownloadError:
                raise
            except requests.RequestException as exc:
                last = exc
                if attempt < self.retries:
                    wait = self.backoff * (2**attempt)
                    log.warning(
                        "fetch failed, retrying",
                        extra={
                            "url": url,
                            "attempt": attempt + 1,
                            "wait_s": wait,
                            "error": str(exc),
                        },
                    )
                    time.sleep(wait)
        raise DownloadError(f"giving up on {url} after {self.retries + 1} attempts: {last}")

    def list_files(self, directory: str) -> list[str]:
        """Return the .zip file names in a NEMWEB directory listing."""
        url = urljoin(BASE_URL, directory)
        html = self._get(url).decode("utf-8", errors="replace")
        return sorted({safe_name(h) for h in HREF.findall(html)})

    def files_for_dates(self, report: str, days: list[date]) -> list[tuple[str, str]]:
        """Pick the (url, name) for each requested day, trying the archive first."""
        spec = REPORTS[report]
        pattern = re.compile(str(spec["pattern"]))
        span = int(spec.get("archive_days", 1))
        wanted = {d.strftime("%Y%m%d") for d in days}
        picked: dict[str, list[tuple[str, str]]] = {}

        # Archive: one zip per day (or per week for some reports). The pattern
        # matches the leading YYYYMMDD, so it also matches the 5-minute names
        # in Current (YYYYMMDDHHMM_...).
        for name in self.list_files(str(spec["archive"])):
            m = pattern.match(name)
            if not m:
                continue
            first = datetime.strptime(m.group(1), "%Y%m%d")
            covered = {(first + timedelta(days=i)).strftime("%Y%m%d") for i in range(span)}
            hits = sorted(covered & wanted)
            if hits:
                url = urljoin(BASE_URL, str(spec["archive"]) + name)
                picked.setdefault(hits[0], []).append((url, name))
                for d in hits[1:]:
                    picked.setdefault(d, [])

        missing = wanted - picked.keys()
        if missing:
            # Recent days are only published as 5-minute (or 30-minute) files in Current.
            for name in self.list_files(str(spec["current"])):
                m = pattern.match(name)
                if m and _current_day(m, spec) in missing:
                    picked.setdefault(_current_day(m, spec), []).append(
                        (urljoin(BASE_URL, str(spec["current"]) + name), name)
                    )

        for d in sorted(wanted - picked.keys()):
            log.warning("no file found for day", extra={"report": report, "day": d})
        return [f for d in sorted(picked) for f in picked[d]]

    def fetch(self, url: str, name: str) -> FetchedFile:
        name = safe_name(name)
        path = self.cache_dir / name
        sidecar = path.with_suffix(path.suffix + ".sha256")
        if path.exists() and sidecar.exists():
            data = path.read_bytes()
            if sha256_bytes(data) == sidecar.read_text().strip():
                return FetchedFile(name, url, path, sidecar.read_text().strip(), True)
            log.warning("cache checksum mismatch, re-downloading", extra={"file": name})

        data = self._get(url)
        digest = sha256_bytes(data)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic: a crash never leaves a half-written cache file
        sidecar.write_text(digest)
        return FetchedFile(name, url, path, digest, False)


def _current_day(m: re.Match, spec: dict) -> str:
    """The market day a Current file belongs to."""
    shift = int(spec.get("name_marks_end_min", 0))
    if not shift or m.lastindex is None or m.lastindex < 2 or m.group(2) is None:
        return m.group(1)
    stamp = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    return (stamp - timedelta(minutes=shift)).strftime("%Y%m%d")


def iter_csv_texts(zip_bytes: bytes, depth: int = 0):
    """Yield (member name, csv text) from a zip, descending into nested zips.

    Daily archives are zips of 5-minute zips, so one level of nesting is normal.
    """
    if depth > 2:
        raise DownloadError("zip nesting deeper than expected")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if info.file_size > MAX_ZIP_MEMBER_BYTES:
                raise DownloadError(f"zip member too large: {info.filename}")
            data = zf.read(info)
            lower = info.filename.lower()
            if lower.endswith(".zip"):
                yield from iter_csv_texts(data, depth + 1)
            elif lower.endswith(".csv"):
                yield info.filename, data.decode("utf-8", errors="replace")
