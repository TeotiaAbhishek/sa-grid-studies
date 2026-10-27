# Decision log

| Date | Decision | Why | Alternatives considered |
| --- | --- | --- | --- |
| 2026-10-27 | Use NEMWEB daily archives, falling back to 5-minute "Current" files for the last day or two | Daily archives are one download per day and keep about 13 months | Monthly MMSDM archives (very large files, a month behind) |
| 2026-10-27 | Store in DuckDB | Single file, no server, fast analytics, works the same on Windows and Linux | SQLite (slow for analytics), PostgreSQL (needs a server) |
| 2026-10-27 | Keep AEMO market time (AEST) and interval-ending timestamps | Matches every AEMO dataset and avoids daylight-saving gaps | Converting to local time or UTC |
| 2026-10-27 | Validate with pandera and quarantine failing rows | Declarative schemas, readable failure reasons, nothing lost | Dropping bad rows, failing whole files |
| 2026-10-27 | Batch all 288 five-minute files in a daily archive before validating | One validation and one upsert per day instead of 288 | Per-file processing (about 50 times slower in tests) |
| 2026-10-27 | Region balance tolerance 50 MW and SCADA tolerance 150 MW, provisional | Starting values only; to be tuned from the residual distribution on real data | None yet. Revisit after the first real week |
| 2026-10-27 | Keep interventions out (INTERVENTION = 0 only) | Intervention runs are what-if pricing runs, not physical dispatch | Storing both with a flag |
| 2026-10-27 | Add SA dispatch prices (DISPATCH_PRICE), with RRP valid from -1,000 to 25,000 $/MWh | Constraint and interconnector investigations need price to show cost. -1,000 is the market floor; the cap is 20,300 for 2025-26 and rises each July, so 25,000 allows for that while still catching a mis-scaled value | No upper bound (would let a units or parsing error through); exact current cap (would reject real prices after the next July increase) |
