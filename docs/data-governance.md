# Data governance

Written as if for review by a data governance team.

## Sources

| Dataset | Publisher | Location | Refresh |
| --- | --- | --- | --- |
| DISPATCHIS reports (region summary, prices, interconnectors, constraints) | AEMO | NEMWEB `Reports/Archive/DispatchIS_Reports` (daily) and `Reports/Current/DispatchIS_Reports` (5-minute) | Every 5 minutes; daily archive kept about 13 months |
| Dispatch SCADA | AEMO | NEMWEB `Reports/Archive/Dispatch_SCADA` and `Reports/Current/Dispatch_SCADA` | As above |
| Rooftop PV actual (MEASUREMENT estimate) | AEMO | NEMWEB `Reports/Archive/ROOFTOP_PV/ACTUAL` (weekly zips) and `Reports/Current/ROOFTOP_PV/ACTUAL` (30-minute) | Every 30 minutes; weekly archive appears about two weeks after the week starts |
| MMSDM registration tables (DUDETAILSUMMARY, DUDETAIL, DUALLOC, GENUNITS) | AEMO | NEMWEB `Data_Archive/Wholesale_Electricity/MMSDM` (monthly) | Monthly, a few weeks after month end |
| NEM Registration and Exemption List (optional alternative) | AEMO | aemo.com.au (downloaded by hand) | Updated by AEMO as units register or change |

## Licence and attribution

AEMO material is © AEMO and is used with attribution under AEMO's published copyright permissions. Check those permissions before any commercial use. Exports carry the source table name so attribution survives into reports.

## Lineage

- Every downloaded file is cached with its SHA-256 hash.
- Every run records a manifest in the `runs` table: source URLs, file hashes, row and reject counts, code version and git commit.
- `loaded_files` records which file version produced the stored rows.
- Rejected rows keep the source file name and the reason in the `rejects` table.

## Quality controls

- Type and range checks per column, and unique keys per dataset (`schemas.py`).
- Rows that fail go to `rejects`; nothing is silently dropped.
- Post-run checks: freshness, completeness (2,016 intervals per week), reject rate, and region balance reconciliation (`sagrid check`).

## Retention

- Raw files in `data/cache` can be deleted at any time; they are re-downloadable while AEMO keeps them.
- The DuckDB database is the system of record for this project. Back it up before upgrading.
- Nothing is committed to the repository except code, docs and small test fixtures.

## Time convention

All timestamps are AEMO market time (AEST, UTC+10, no daylight saving) and mark the end of each 5-minute interval.
