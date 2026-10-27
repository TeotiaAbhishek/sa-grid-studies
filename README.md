# SA Grid Studies

Automated, defensive collection of South Australia's electricity grid data from AEMO, plus (in later releases) investigations and reproducible power system studies built on it.

Built to production standards: tested on Windows and Linux in CI, validated row by row, idempotent, logged, and traceable from every stored row back to the AEMO file it came from.

> **Status:** v0.1 in progress (Phase 1: data pipeline). See [REQUIREMENTS.md](REQUIREMENTS.md) for the full scope and acceptance criteria.

## What it does

`sagrid ingest` downloads AEMO NEMWEB dispatch data for a date range and stores five validated datasets in a local DuckDB database:

| Dataset | Source table | What it holds |
| --- | --- | --- |
| `region_summary` | DISPATCH_REGIONSUM | SA demand, generation, net interchange (5-minute) |
| `price` | DISPATCH_PRICE | SA spot price (RRP), price flags and regulation FCAS prices (5-minute) |
| `interconnector` | DISPATCH_INTERCONNECTORRES | Heywood (V-SA) and Murraylink (V-S-MNSP1) flows and limits |
| `constraint` | DISPATCH_CONSTRAINT | Network constraints, their marginal values and violations |
| `unit_scada` | DISPATCH_UNIT_SCADA | Metered output of every NEM generating unit |

Every run:

- only fetches from an allowlisted host and sanitises remote file names
- retries network failures with exponential backoff, and caches files with a SHA-256 checksum
- types every value and sends rows that fail parsing or schema checks to a `rejects` table with the reason (nothing is dropped silently)
- skips files it has already loaded unchanged, so re-running is safe
- logs one JSON line per event and records a manifest (source URLs, file hashes, row and reject counts, git commit)
- carries on past a corrupt file and reports it, instead of stopping

## Quick start (Windows PowerShell)

```powershell
git clone https://github.com/TeotiaAbhishek/sa-grid-studies.git
cd sa-grid-studies
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

sagrid ingest --start 2026-10-19 --end 2026-10-25     # one week
sagrid check  --start 2026-10-19 --end 2026-10-25     # health checks, exit 1 on failure
sagrid export                                          # Power BI files in data\exports
sagrid status
```

On macOS or Linux use `python3 -m venv .venv` and `source .venv/bin/activate`.

## Commands

| Command | Purpose |
| --- | --- |
| `sagrid ingest --start D --end D [--datasets ...]` | Load a date range (skips files already loaded unchanged) |
| `sagrid backfill --start D --end D` | Reload a range even if files are unchanged, e.g. after an AEMO correction |
| `sagrid check --start D --end D [--json]` | Freshness, completeness, reject rate and reconciliation checks |
| `sagrid export` | Write the Power BI star schema (Parquet and CSV) |
| `sagrid status` | Show the last run's totals |

Dates are `YYYY-MM-DD` or `yesterday`. Exit codes: 0 ok, 1 checks failed, 2 a file failed, 3 bad input or config.

## Running it unattended

- **Nightly job:** `.github/workflows/nightly.yml` ingests the previous day at 01:30 AEST, runs `sagrid check`, keeps the database between runs, and uploads logs. If ingest or any check fails, it opens (or comments on) a GitHub issue labelled `alert`. It can also be started by hand from the Actions tab with a date range, which doubles as a backfill.
- **Docker:** `docker build -t sagrid .` then `docker run --rm -v ${PWD}/data:/home/sagrid/data sagrid ingest --start yesterday --end yesterday`. The image runs as a non-root user.
- **Releases:** pushing a tag such as `v0.1.0` runs the tests, builds the package and Docker image, pushes the image to GitHub Container Registry and creates a GitHub release.

## Configuration

Copy `sagrid.example.toml` to `sagrid.toml`, or set `SAGRID_*` environment variables. Settings are validated at start-up and an unknown or invalid setting stops the run with a clear message.

## Tests

```powershell
pytest --cov=sagrid                          # unit tests, no network
$env:SAGRID_NETWORK_TESTS=1; pytest -m network   # one real day from NEMWEB
```

Unit tests use small files built in AEMO's published CSV layout. The network test is the real-data check.

## Documentation

- [User guide](docs/user-guide.md)
- [Data governance](docs/data-governance.md): sources, licence, lineage, retention
- [Decision log](docs/decisions.md)
- [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)

## Data source and attribution

Data is published by the Australian Energy Market Operator (AEMO) on [NEMWEB](https://nemweb.com.au) and is © AEMO. This project uses public data only and is not affiliated with AEMO or any network business.
