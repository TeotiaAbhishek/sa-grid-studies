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
- Region summary now carries wind and solar farm output (dispatched and forecast) and battery charge and discharge; older files without these columns still load, and existing databases gain the columns automatically.

### Fixed
- Unit SCADA rejected real DUIDs containing `-`, `#` or `/` (for example OSB-AG).

## [0.0.1] - 2026-10-27
### Added
- Requirements, repository setup and ways of working.
