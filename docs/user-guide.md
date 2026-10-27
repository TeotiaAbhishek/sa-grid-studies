# User guide

## First run

1. Install Python 3.11 or newer and Git.
2. Clone the repository and install it (see the README quick start).
3. Load a week: `sagrid ingest --start 2026-10-19 --end 2026-10-25`.
4. Check it: `sagrid check --start 2026-10-19 --end 2026-10-25`.

A week of the six datasets takes a few minutes and a few hundred MB of downloads. Unit SCADA is the largest; leave it out with `--datasets region_summary price interconnector constraint rooftop_pv` if you only need the region view.

Rooftop PV is published half-hourly, and its weekly archive appears about two weeks after the week starts. Until then the last fortnight comes from the 30-minute files in NEMWEB's Current folder, which is about 48 small downloads per day.

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

```powershell
sagrid registry                      # or: sagrid registry --on 2026-10-19
```

This builds `data/registry.csv` (one row per unit: region, dispatch type, station, fuel, registered capacity) from AEMO's monthly MMSDM registration tables on NEMWEB (DUDETAILSUMMARY, DUDETAIL, DUALLOC and GENUNITS), using the rows in force on the given date. MMSDM months appear a few weeks after they end, so the newest available month is used. Once the file exists, `sagrid export` adds a `dim_unit` table and `sagrid investigate i3` can run.

AEMO's "NEM Registration and Exemption List" workbook also works: download it from aemo.com.au and set `registry_path` in `sagrid.toml`. That setting takes priority over `data/registry.csv`.

## Querying the data directly

```python
import duckdb
con = duckdb.connect("data/sagrid.duckdb", read_only=True)
con.sql("SELECT date_trunc('day', SETTLEMENTDATE) d, min(TOTALDEMAND), max(TOTALDEMAND) FROM region_summary GROUP BY 1 ORDER BY 1").show()
```

## Adding a dataset to data you already loaded

`sagrid ingest` skips archives it has already loaded, so a dataset or column added in a new release (for example `price`, or the wind and solar columns in `region_summary`) is not filled in for past days automatically. New columns appear empty until you backfill. Re-read the cached archives with:

```powershell
sagrid backfill --start 2026-10-19 --end 2026-10-25 --datasets price
```

Archives come from the local cache, so this is quick and downloads nothing that is already there.
