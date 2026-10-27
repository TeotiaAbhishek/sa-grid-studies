# User guide

## First run

1. Install Python 3.11 or newer and Git.
2. Clone the repository and install it (see the README quick start).
3. Load a week: `sagrid ingest --start 2026-10-19 --end 2026-10-25`.
4. Check it: `sagrid check --start 2026-10-19 --end 2026-10-25`.

A week of the four datasets takes a few minutes and a few hundred MB of downloads. Unit SCADA is the largest; leave it out with `--datasets region_summary interconnector constraint` if you only need the region view.

## Reading the output

`ingest` prints totals: files loaded, skipped (already loaded unchanged) and failed, plus rows and rejects per dataset. Logs are JSON lines on stderr. Save them with `2> run.log`.

`check` prints a table:

| Check | Passes when |
| --- | --- |
| freshness | the newest interval is no older than `max_staleness_hours` |
| completeness | every 5-minute interval in the range is present |
| reject_rate | rejected rows are at most `max_reject_rate` of all rows |
| region_balance | at most 1% of intervals breach the balance tolerance |

## When something fails

| Symptom | What to do |
| --- | --- |
| `files_failed` above 0 | Look for `"file failed"` in the logs. Re-run the same command: loaded files are skipped and only failed ones retried |
| completeness fails | AEMO may not have published the day yet, or a file failed. Re-run, then check again |
| reject_rate fails | Query `SELECT reason, count(*) FROM rejects GROUP BY 1` to see why |
| AEMO republished data | `sagrid backfill --start D --end D` reloads the range |

## Adding the generator registry

Download the "NEM Registration and Exemption List" spreadsheet from AEMO's website, save it under `data/`, and set `registry_path` in `sagrid.toml`. `sagrid export` then adds a `dim_unit` table (region and fuel per unit), and the SCADA reconciliation becomes available.

## Querying the data directly

```python
import duckdb
con = duckdb.connect("data/sagrid.duckdb", read_only=True)
con.sql("SELECT date_trunc('day', SETTLEMENTDATE) d, min(TOTALDEMAND), max(TOTALDEMAND) FROM region_summary GROUP BY 1 ORDER BY 1").show()
```
