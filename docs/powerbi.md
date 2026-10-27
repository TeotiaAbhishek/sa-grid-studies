# Power BI report

`sagrid export` writes a small star schema to `data/exports`. Load the Parquet files (or the CSVs) into Power BI Desktop with **Get data > Parquet** (or **Folder** to load all of them at once). No Power Query steps are needed beyond setting the data types it already detects.

## Model

| Table | Grain | Key columns | Relates to |
| --- | --- | --- | --- |
| `fact_region_summary` | SA1, 5 minutes | SETTLEMENTDATE, DATE | `dim_date[DATE]` |
| `fact_interconnector` | interconnector, 5 minutes | SETTLEMENTDATE, INTERCONNECTORID, DATE | `dim_date[DATE]`, `dim_interconnector[INTERCONNECTORID]` |
| `fact_constraint` | constraint, 5 minutes | SETTLEMENTDATE, CONSTRAINTID, DATE | `dim_date[DATE]` |
| `fact_price` | SA1, 5 minutes | SETTLEMENTDATE, DATE | `dim_date[DATE]` |
| `fact_rooftop_pv` | SA1, 30 minutes | INTERVAL_DATETIME, DATE | `dim_date[DATE]` |
| `fact_unit_scada` | unit, 5 minutes | SETTLEMENTDATE, DUID, DATE | `dim_date[DATE]`, `dim_unit[DUID]` (if the registry is loaded) |
| `fact_unit_solution` | unit, 5 minutes | SETTLEMENTDATE, DUID, DATE | `dim_date[DATE]`, `dim_unit[DUID]` |
| `fact_rejects` | one rejected row | run_id, dataset | none |
| `dim_date` | day | DATE | |
| `dim_interconnector` | interconnector | INTERCONNECTORID | |
| `dim_unit` | unit | DUID | |

Create the relationships in Model view as single-direction, many-to-one from each fact to its dimensions. Mark `dim_date` as the date table.

`DATE` is the market day an interval belongs to: an interval ending at 00:00 belongs to the previous day, matching AEMO. `fact_unit_solution` follows AEMO's trading day (04:05 to 04:00), so its first and last days are partly loaded; filter on `dim_date` as usual.

### Data quality columns

| Table | Column | Meaning |
| --- | --- | --- |
| `fact_unit_scada` | `SCADA_ISSUE` | The I3 issue this reading falls in (`stuck`, `spike`, `above_capacity`, `no_response`, `off_target`, `brief_departure`, `negative`), or empty. Blank when the reading wasn't screened (no registry, or a unit outside SA) |
| `fact_unit_scada` | `IS_SUSPECT` | True for stuck, spike and above-capacity readings: the value itself is likely wrong. Blank when not screened |
| `fact_constraint` | `IS_PENALTY` | True when the marginal value is 100,000 or more in size: a violated constraint, not a market price. Leave these out of price-based measures |

## Measures (DAX)

```
Avg Demand MW = AVERAGE ( fact_region_summary[TOTALDEMAND] )
Peak Demand MW = MAX ( fact_region_summary[TOTALDEMAND] )
Min Demand MW = MIN ( fact_region_summary[TOTALDEMAND] )
Energy GWh = SUM ( fact_region_summary[TOTALDEMAND] ) * 5 / 60 / 1000

Net Import MW = - AVERAGE ( fact_region_summary[NETINTERCHANGE] )

Binding Intervals =
    CALCULATE ( COUNTROWS ( fact_constraint ), fact_constraint[MARGINALVALUE] <> 0 )

Hours Binding = [Binding Intervals] * 5 / 60

Intervals At Import Limit =
    CALCULATE (
        COUNTROWS ( fact_interconnector ),
        FILTER ( fact_interconnector,
            ABS ( fact_interconnector[MWFLOW] - fact_interconnector[IMPORTLIMIT] ) <= 5
                || ABS ( fact_interconnector[MWFLOW] - fact_interconnector[EXPORTLIMIT] ) <= 5 )
    )

Reject Count = COUNTROWS ( fact_rejects )

Suspect Readings = CALCULATE ( COUNTROWS ( fact_unit_scada ), fact_unit_scada[IS_SUSPECT] = TRUE () )

Unit MW (clean) =
    CALCULATE ( AVERAGE ( fact_unit_scada[SCADAVALUE] ), fact_unit_scada[IS_SUSPECT] <> TRUE () )

Marginal Value (no penalty) =
    CALCULATE ( SUM ( fact_constraint[MARGINALVALUE] ), fact_constraint[IS_PENALTY] = FALSE () )

Peak Demand vs Same Month Last Year =
    VAR LastYear = CALCULATE ( [Peak Demand MW], SAMEPERIODLASTYEAR ( dim_date[DATE] ) )
    RETURN DIVIDE ( [Peak Demand MW] - LastYear, LastYear )
```

The 5 MW band in "Intervals At Import Limit" is a working definition of "at the limit". Record it in the report's notes page if you change it.

## Pages

1. **SA grid overview:** line chart of average demand by 30 minutes (axis: SETTLEMENTDATE, drill to day), cards for peak, minimum and energy, and a slicer on `dim_date[SEASON]`.
2. **Interconnectors:** line chart of MWFLOW with IMPORTLIMIT and EXPORTLIMIT for each interconnector (small multiples by `dim_interconnector[NAME]`), plus a card for intervals at the limit.
3. **Constraints:** bar chart of the top 15 constraints by Hours Binding, and a table with total marginal value. Add a tooltip with the constraint ID.
4. **Data quality and benchmarking:** reject count by dataset and reason, gap count from `sagrid check`, suspect readings by unit and issue, and month-by-month peak demand compared with the same month last year.

Add a final **Notes** page that states the data source (AEMO NEMWEB, © AEMO), the date range loaded, and that timestamps are AEST market time.

## Publishing

Save the report as `powerbi/sa-grid.pbix` in the repository and add screenshots of each page to `docs/img/` for the README. Keep the `.pbix` small by loading only the period you need (for example one or two months).
