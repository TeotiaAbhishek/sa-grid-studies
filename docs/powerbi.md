# Power BI report

`sagrid export` writes a small star schema to `data/exports` as Parquet and CSV. This page builds the report in Power BI Desktop from the Parquet files: loading, model, measures, pages and publishing. Follow the sections in order.

## 1. Load the tables

Each export file is its own table, so load them one by one rather than with **Get data > Folder** (which appends every file into a single table). A path parameter and a small function keep it to one line per table and let the report refresh from another folder.

1. **Home > Transform data** to open Power Query.
2. **Manage Parameters > New Parameter**: name `ExportFolder`, type Text, current value the export folder with a trailing backslash, for example `C:\Users\Abhi\Documents\sa-grid-studies\data\exports\`.
3. **New Source > Blank Query**, open **Advanced Editor**, paste the function below and rename the query `fnLoad`:

   ```
   (name as text) as table =>
   let
       Source = Parquet.Document(File.Contents(ExportFolder & name & ".parquet")),
       // DATE arrives as a datetime at midnight; relationships need a date on both sides
       Typed = if Table.HasColumns(Source, "DATE")
           then Table.TransformColumnTypes(Source, {{"DATE", type date}})
           else Source
   in
       Typed
   ```

4. For each table below, **New Source > Blank Query**, type the formula in the formula bar and rename the query to the table name:

   | Query name | Formula |
   | --- | --- |
   | `dim_date` | `= fnLoad("dim_date")` |
   | `dim_interconnector` | `= fnLoad("dim_interconnector")` |
   | `dim_unit` | `= fnLoad("dim_unit")` |
   | `fact_region_summary` | `= fnLoad("fact_region_summary")` |
   | `fact_price` | `= fnLoad("fact_price")` |
   | `fact_interconnector` | `= fnLoad("fact_interconnector")` |
   | `fact_constraint` | `= fnLoad("fact_constraint")` |
   | `fact_rooftop_pv` | `= fnLoad("fact_rooftop_pv")` |
   | `fact_rejects` | `= fnLoad("fact_rejects")` |

5. `fact_unit_scada` and `fact_unit_solution` hold every unit in the NEM. Keep only South Australian units so the `.pbix` stays small. Create each with a blank query and this code in Advanced Editor (change the table name for the second one):

   ```
   let
       SaUnits = List.Buffer(Table.SelectRows(dim_unit, each [REGIONID] = "SA1")[DUID]),
       Source = fnLoad("fact_unit_scada"),
       SaOnly = Table.SelectRows(Source, each List.Contains(SaUnits, [DUID]))
   in
       SaOnly
   ```

6. Right-click `fnLoad` and clear **Enable load** so it does not appear as a table. **Close & Apply**.

`dim_unit` exists only when the generator registry was loaded before `sagrid export` (run `sagrid registry` first). Without it, skip step 5's filter and the unit pages.

## 2. Model

| Table | Grain | Key columns | Relates to |
| --- | --- | --- | --- |
| `fact_region_summary` | SA1, 5 minutes | SETTLEMENTDATE, DATE | `dim_date[DATE]` |
| `fact_price` | SA1, 5 minutes | SETTLEMENTDATE, DATE | `dim_date[DATE]` |
| `fact_interconnector` | interconnector, 5 minutes | SETTLEMENTDATE, INTERCONNECTORID, DATE | `dim_date[DATE]`, `dim_interconnector[INTERCONNECTORID]` |
| `fact_constraint` | constraint, 5 minutes | SETTLEMENTDATE, CONSTRAINTID, DATE | `dim_date[DATE]` |
| `fact_rooftop_pv` | SA1, 30 minutes | INTERVAL_DATETIME, DATE | `dim_date[DATE]` |
| `fact_unit_scada` | unit, 5 minutes | SETTLEMENTDATE, DUID, DATE | `dim_date[DATE]`, `dim_unit[DUID]` |
| `fact_unit_solution` | unit, 5 minutes | SETTLEMENTDATE, DUID, DATE | `dim_date[DATE]`, `dim_unit[DUID]` |
| `fact_rejects` | one rejected row | run_id, dataset | none |
| `dim_date` | day | DATE | |
| `dim_hour` | hour of day | HOUR | see below |
| `dim_interconnector` | interconnector | INTERCONNECTORID | |
| `dim_unit` | unit | DUID | |

In **Model view**, drag each fact key onto its dimension key. Every relationship is many-to-one (fact to dimension) with single cross-filter direction. There are no relationships between facts.

Then:

- Select `dim_date` > **Table tools > Mark as date table**, column `DATE`. Power BI refuses if a day is missing, which is also a quick gap check on the load.
- Sort `dim_date[MONTH_NAME]` by `MONTH` (**Column tools > Sort by column**).
- Hide the fact `DATE`, `DUID` and `INTERCONNECTORID` columns from report view so slicers always use the dimensions.

`DATE` is the market day an interval belongs to: an interval ending at 00:00 belongs to the previous day, matching AEMO. All timestamps are AEST market time (UTC+10, no daylight saving).

### Calculated columns and the hour dimension

Prices and demand follow the time of day (negative prices cluster around 1 to 4 pm), so add an hour dimension shared by the 5-minute and 30-minute facts. **Modeling > New table**:

```
dim_hour = SELECTCOLUMNS ( GENERATESERIES ( 0, 23 ), "HOUR", [Value] )
```

Then select each fact and add a **New column** (type only the part after the table name, for example `HOUR = ...`). `SETTLEMENTDATE` marks the end of an interval, so step back five minutes to get the hour the interval starts in. `INTERVAL_DATETIME` already marks the start.

```
fact_region_summary[HOUR] = HOUR ( fact_region_summary[SETTLEMENTDATE] - TIME ( 0, 5, 0 ) )
fact_price[HOUR]          = HOUR ( fact_price[SETTLEMENTDATE] - TIME ( 0, 5, 0 ) )
fact_rooftop_pv[HOUR]     = HOUR ( fact_rooftop_pv[INTERVAL_DATETIME] )
```

Relate each `HOUR` column to `dim_hour[HOUR]` (many-to-one, single direction).

Two more columns:

```
-- Price for the same interval, so demand-weighted price is a plain SUMX
fact_region_summary[RRP] =
    LOOKUPVALUE ( fact_price[RRP], fact_price[SETTLEMENTDATE], fact_region_summary[SETTLEMENTDATE] )

-- Marginal values of 100,000 or more are violation penalties, not prices (docs/decisions.md)
fact_constraint[IS_PENALTY] = ABS ( fact_constraint[MARGINALVALUE] ) >= 100000
```

Once `sagrid export` writes `IS_PENALTY` itself, delete the calculated column and use the exported one.

## 3. Measures (DAX)

Create a table to hold them (**Home > Enter data**, name it `_Measures`, load it, then delete its column) and add each with **New measure**. Set the format in **Measure tools** as noted.

Demand and energy (`fact_region_summary`; `TOTALDEMAND` is operational demand, so it excludes rooftop PV):

```
Avg Demand MW = AVERAGE ( fact_region_summary[TOTALDEMAND] )
Peak Demand MW = MAX ( fact_region_summary[TOTALDEMAND] )
Min Demand MW = MIN ( fact_region_summary[TOTALDEMAND] )
Energy GWh = SUM ( fact_region_summary[TOTALDEMAND] ) * 5 / 60 / 1000

-- NETINTERCHANGE is positive when SA exports
Net Import MW = - AVERAGE ( fact_region_summary[NETINTERCHANGE] )
```

Price (`fact_price`, $/MWh, format as currency with 2 decimals):

```
Avg Price = AVERAGE ( fact_price[RRP] )
Max Price = MAX ( fact_price[RRP] )
Min Price = MIN ( fact_price[RRP] )

Demand Weighted Price =
    DIVIDE (
        SUMX ( fact_region_summary, fact_region_summary[TOTALDEMAND] * fact_region_summary[RRP] ),
        SUM ( fact_region_summary[TOTALDEMAND] )
    )

-- format as percentage
Negative Price Share =
    DIVIDE ( CALCULATE ( COUNTROWS ( fact_price ), fact_price[RRP] < 0 ), COUNTROWS ( fact_price ) )
```

Wind and solar (`SS_*_CLEAREDMW` is the dispatch target for semi-scheduled farms, `fact_rooftop_pv` is a 30-minute estimate):

```
Wind GWh = SUM ( fact_region_summary[SS_WIND_CLEAREDMW] ) * 5 / 60 / 1000
Solar Farm GWh = SUM ( fact_region_summary[SS_SOLAR_CLEAREDMW] ) * 5 / 60 / 1000
Rooftop PV GWh = SUM ( fact_rooftop_pv[POWER] ) * 30 / 60 / 1000

-- share of underlying demand (operational demand plus rooftop PV); format as percentage
Wind and Solar Share =
    DIVIDE ( [Wind GWh] + [Solar Farm GWh] + [Rooftop PV GWh], [Energy GWh] + [Rooftop PV GWh] )
```

Interconnectors (`fact_interconnector`; positive `MWFLOW` is VIC to SA, `EXPORTLIMIT` bounds that direction and `IMPORTLIMIT`, usually negative, bounds SA to VIC):

```
Avg Flow MW = AVERAGE ( fact_interconnector[MWFLOW] )

Intervals At VIC to SA Limit =
    CALCULATE (
        COUNTROWS ( fact_interconnector ),
        FILTER ( fact_interconnector, fact_interconnector[MWFLOW] >= fact_interconnector[EXPORTLIMIT] - 5 )
    )

Intervals At SA to VIC Limit =
    CALCULATE (
        COUNTROWS ( fact_interconnector ),
        FILTER ( fact_interconnector, fact_interconnector[MWFLOW] <= fact_interconnector[IMPORTLIMIT] + 5 )
    )

-- format as percentage
Share At Limit =
    DIVIDE (
        [Intervals At VIC to SA Limit] + [Intervals At SA to VIC Limit],
        COUNTROWS ( fact_interconnector )
    )
```

The 5 MW band is a working definition of "at the limit". Record it on the Notes page if you change it.

Constraints (`fact_constraint`; a constraint binds when its marginal value is non-zero):

```
Binding Intervals =
    CALCULATE (
        COUNTROWS ( fact_constraint ),
        fact_constraint[MARGINALVALUE] <> 0,
        fact_constraint[IS_PENALTY] = FALSE ()
    )

Hours Binding = [Binding Intervals] * 5 / 60

Total Marginal Value =
    CALCULATE ( SUM ( fact_constraint[MARGINALVALUE] ), fact_constraint[IS_PENALTY] = FALSE () )

Penalty Intervals = CALCULATE ( COUNTROWS ( fact_constraint ), fact_constraint[IS_PENALTY] = TRUE () )
```

Units (`fact_unit_scada` and `fact_unit_solution`, SA units only after the load filter):

```
Unit Energy GWh = SUM ( fact_unit_scada[SCADAVALUE] ) * 5 / 60 / 1000

-- average over intervals of the total across the units in context
Avg Output MW =
    AVERAGEX ( VALUES ( fact_unit_scada[SETTLEMENTDATE] ), CALCULATE ( SUM ( fact_unit_scada[SCADAVALUE] ) ) )

Avg Target MW =
    AVERAGEX ( VALUES ( fact_unit_solution[SETTLEMENTDATE] ), CALCULATE ( SUM ( fact_unit_solution[TOTALCLEARED] ) ) )
```

Data quality:

```
Reject Count = COUNTROWS ( fact_rejects )
Intervals Loaded = DISTINCTCOUNT ( fact_region_summary[SETTLEMENTDATE] )
Expected Intervals = COUNTROWS ( dim_date ) * 288
Completeness = DIVIDE ( [Intervals Loaded], [Expected Intervals] )
```

## 4. Pages

Use one canvas size (16:9), the same slicer panel on the left of every page (`dim_date[DATE]` as a between slider, `dim_date[IS_WEEKEND]`), and **View > Sync slicers** so a date range carries across pages. Put a one-line text box under each title saying what the page answers.

1. **SA grid overview**: "How much power did SA use, and how much came from wind and sun?"
   - Cards: Avg Price, Negative Price Share, Peak Demand MW, Energy GWh, Wind and Solar Share.
   - Line chart: `fact_region_summary[SETTLEMENTDATE]` on the axis (turn off the date hierarchy so it plots each interval), Avg Demand MW and Net Import MW as lines.
   - Stacked column chart: `dim_date[DATE]` on the axis, Wind GWh, Solar Farm GWh and Rooftop PV GWh as values.

2. **Prices**: "When are prices negative, and does demand explain it?"
   - Matrix heatmap: rows `dim_hour[HOUR]`, columns `dim_date[DATE]`, value Avg Price. **Format > Cell elements > Background color**, diverging rule with 0 at the centre (blue below, red above).
   - Clustered column: `dim_hour[HOUR]` on the axis, Negative Price Share.
   - Line chart: `dim_hour[HOUR]` on the axis, Avg Price and Avg Demand MW (secondary axis).
   - Cards: Demand Weighted Price, Max Price, Min Price.

3. **Interconnectors**: "How hard are Heywood and Murraylink being pushed?"
   - Line chart: `fact_interconnector[SETTLEMENTDATE]` on the axis, `MWFLOW`, `EXPORTLIMIT` and `IMPORTLIMIT` (average) as lines, **Small multiples** `dim_interconnector[NAME]`.
   - Table: `dim_interconnector[NAME]`, Avg Flow MW, Intervals At VIC to SA Limit, Intervals At SA to VIC Limit, Share At Limit.

4. **Constraints**: "Which network limits shape the SA market?"
   - Bar chart: `fact_constraint[CONSTRAINTID]`, Hours Binding, **Filters on this visual > Top N** 15 by Hours Binding.
   - Table: CONSTRAINTID, Hours Binding, Total Marginal Value, Penalty Intervals, sorted by Hours Binding.
   - Card: Penalty Intervals, with a note that penalty-sized values are excluded from the other measures.

5. **Generators** (only with `dim_unit`): "What ran, and did units follow their dispatch targets?"
   - Page filter: `dim_unit[DISPATCHTYPE]` is not `LOAD`, so loads do not count as generation.
   - Stacked area: `fact_unit_scada[SETTLEMENTDATE]` on the axis, Avg Output MW, legend `dim_unit[FUEL]`.
   - Table: `dim_unit[STATION]`, `dim_unit[DUID]`, `dim_unit[FUEL]`, `dim_unit[REG_CAP_MW]`, Unit Energy GWh, Avg Output MW, Avg Target MW.

6. **Data quality**: "Can the numbers on the other pages be trusted?"
   - Cards: Completeness, Intervals Loaded, Reject Count.
   - Table: `fact_rejects[dataset]`, `fact_rejects[reason]`, Reject Count.
   - Text box: the summary printed by `sagrid check` for the same period (gaps, staleness, demand balance).

7. **Notes**: data source (AEMO NEMWEB, © AEMO, used under AEMO's copyright permissions), the date range loaded, that timestamps are AEST market time, the definitions used (5 MW limit band, 100,000 penalty threshold, operational versus underlying demand), and the git commit `sagrid export` ran from.

## 5. Refresh

After pulling new pipeline changes or loading more days, run `sagrid export` and then **Home > Refresh** in Power BI. If a new column appears in the export (for example `IS_PENALTY`), it shows up after the refresh without changing the queries.

## 6. Publishing

Save the report as `powerbi/sa-grid.pbix` in the repository and export a screenshot of each page (**File > Export > Export to PDF**, or a screen capture) to `docs/img/` for the README. Keep the `.pbix` small by loading only the period you need (for example one or two months); the SA-only unit filter in step 1.5 is what keeps it small.
