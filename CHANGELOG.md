# Changelog

All notable changes are recorded here. Versions follow [Semantic Versioning](https://semver.org).

## [Unreleased]

### Added
- NEMWEB download client: host allowlist, file name sanitisation, retries with backoff, checksum cache, zip size and nesting guards.
- Parser for AEMO's multi-table CSV format that records malformed rows instead of failing the file.
- Typed, validated datasets (region summary, SA dispatch prices, interconnectors, constraints, SA rooftop PV, unit SCADA) with a rejects table.
- DuckDB storage with idempotent upserts, loaded-file tracking and run manifests.
- 5-minute interval grid, gap detection and aggregates with coverage.
- Region balance and SCADA-vs-dispatch reconciliation checks.
- `sagrid` CLI: ingest, backfill, check, export, status.
- Power BI star-schema exports.
- CI on Windows and Linux with lint, format, type checking (mypy), coverage gate and dependency audit.
- Nightly ingest workflow with health checks and automatic alert issues.
- Dockerfile (non-root) with a CI smoke test, and a tag-driven release workflow.
- Live-data integration test (`pytest -m network`).
- `sagrid registry`: builds the generator registry from NEMWEB's MMSDM registration tables.
- `sagrid investigate`: investigations I1 (binding constraints and their drivers), I2 (Heywood at its limit) and I3 (SCADA data quality, with an issues register), each writing a reproducible report with a data snapshot hash.
- `unit_solution` dataset: each unit's dispatch target from AEMO's Next Day Dispatch report, fetched by trading day (Current first, then the monthly archive). Large archives are loaded in batches so memory stays bounded.
- I3 uses dispatch targets when they are loaded: spikes are judged against the target, constant readings at a constant target are not flagged as stuck, and a new "no response" check finds units reading 0 MW while dispatched. One-interval departures by semi-scheduled wind and solar farms are listed as low-severity "brief departures", since their target is a forecast. A scheduled unit that stays well off target for 15 minutes or more is one "off target" conformance episode instead of a run of spikes, and a spike from a unit not offered to the market says so.
- The parser keeps only the columns a dataset uses and reads lines without copying the file, which cut peak memory for a synthetic day of unit targets from 688 MB to 150 MB.
- Power BI exports: unit SCADA readings carry `SCADA_ISSUE` (the I3 issue each reading falls in, by I3's rules) and `IS_SUSPECT` (stuck, spike or above capacity), so a report can mask bad readings instead of deleting them. Readings I3 can't screen (no registry, or a unit outside SA) are left blank rather than marked clean. Constraint rows carry `IS_PENALTY` (marginal value of 100,000 or more in size).
- `sagrid check` reports the share of intervals Heywood spent at a transfer limit and its longest episode, using I2's definition. Setting `heywood_max_at_limit_share` turns it into an alert.
- Region summary now carries wind and solar farm output (dispatched and forecast) and battery charge and discharge; older files without these columns still load, and existing databases gain the columns automatically.

### Fixed
- The freshness check failed on any past window once 36 hours had passed since it was loaded. It now measures the newest interval against the end of the window checked, or against now while the window is still open.
- Unit SCADA rejected real DUIDs containing `-`, `#` or `/` (for example OSB-AG).
- The registry left capacity and fuel blank for 170 units across the NEM (27 in SA, including Osborne, Pelican Point and Quarantine), because it took the latest date and highest version separately. It now takes the latest effective date, then the highest version on that date.
- `sagrid registry` also reads older MMSDM months, which use `PUBLIC_DVD_<TABLE>_<month>.zip` file names.
- Reloading a file left its earlier rejects in place, so `sagrid check` kept counting rows that now load. A reload now replaces that file's rejects.
- The SCADA-vs-dispatch check left out battery output, which dispatched generation includes, and counted non-scheduled units, which it does not. It also compared each target with the reading from the start of the interval; it now uses the reading at the end.
- Tests read a real `data/registry.csv` or `sagrid.toml` from the working folder; each test now runs in its own folder with no `SAGRID_*` settings.

## [0.0.1] - 2026-10-27
### Added
- Requirements, repository setup and ways of working.
