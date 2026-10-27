"""Command-line interface.

sagrid ingest   --start 2026-10-01 --end 2026-10-07
sagrid backfill --start 2026-10-01 --end 2026-10-07   (reloads even unchanged files)
sagrid check    --start 2026-10-01 --end 2026-10-07   (exit code 1 if any check fails)
sagrid export                                          (Power BI star schema)
sagrid status
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sagrid.config import ConfigError, Settings, load_settings
from sagrid.logging_utils import setup_logging

log = logging.getLogger("sagrid")

EXIT_OK, EXIT_CHECKS_FAILED, EXIT_RUN_FAILED, EXIT_BAD_INPUT = 0, 1, 2, 3


def _date(s: str) -> date:
    if s == "yesterday":
        return (datetime.now(UTC) + timedelta(hours=10)).date() - timedelta(days=1)
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD or 'yesterday'): {s!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sagrid", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--config", type=Path, help="TOML settings file (default: sagrid.toml if present)"
    )
    sub = p.add_subparsers(dest="command", required=True)

    for name in ("ingest", "backfill", "check"):
        sp = sub.add_parser(name)
        sp.add_argument("--start", type=_date, required=True)
        sp.add_argument("--end", type=_date, required=True)
        if name != "check":
            sp.add_argument(
                "--datasets",
                nargs="+",
                choices=[
                    "region_summary",
                    "price",
                    "interconnector",
                    "constraint",
                    "rooftop_pv",
                    "unit_scada",
                    "unit_solution",
                ],
            )
        if name == "check":
            sp.add_argument("--json", action="store_true", help="print results as JSON")
    inv = sub.add_parser("investigate", help="run investigations I1 to I3 and write reports")
    inv.add_argument("which", choices=["i1", "i2", "i3", "all"])
    inv.add_argument("--start", type=_date, required=True)
    inv.add_argument("--end", type=_date, required=True)
    inv.add_argument("--out", type=Path, default=Path("docs/investigations"))
    reg = sub.add_parser("registry", help="build the generator registry from NEMWEB")
    reg.add_argument(
        "--on", type=_date, default=None, help="date the registry should describe (default: today)"
    )
    sub.add_parser("export")
    sub.add_parser("status")
    return p


def _settings(path: Path | None) -> Settings:
    if path is None and Path("sagrid.toml").exists():
        path = Path("sagrid.toml")
    return load_settings(path)


def _registry_file(s: Settings) -> Path | None:
    """The configured registry, or the one `sagrid registry` built, if any."""
    if s.registry_path:
        return s.registry_path
    built = s.data_dir / "registry.csv"
    return built if built.exists() else None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        s = _settings(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    setup_logging(s.log_level)

    from sagrid.store import Store  # imported late so --help stays fast

    store = Store(s.db_path)
    try:
        if args.command in ("ingest", "backfill"):
            from sagrid.nemweb.client import NemwebClient
            from sagrid.pipeline import run_ingest

            client = NemwebClient(
                s.cache_dir, retries=s.retries, backoff=s.backoff_s, timeout=s.timeout_s
            )
            try:
                manifest = run_ingest(
                    args.start,
                    args.end,
                    store,
                    client,
                    args.datasets,
                    force=args.command == "backfill",
                )
            except ValueError as exc:
                print(f"input error: {exc}", file=sys.stderr)
                return EXIT_BAD_INPUT
            print(json.dumps(manifest["totals"], indent=2))
            return EXIT_RUN_FAILED if manifest["totals"]["files_failed"] else EXIT_OK

        if args.command == "check":
            from sagrid.monitor import as_json, report, run_checks

            checks = run_checks(
                store,
                args.start,
                args.end,
                s.max_staleness_hours,
                s.max_reject_rate,
                s.balance_tolerance_mw,
            )
            print(as_json(checks) if args.json else report(checks))
            return EXIT_OK if all(c.passed for c in checks) else EXIT_CHECKS_FAILED

        if args.command == "export":
            from sagrid.export import export_powerbi

            registry = None
            if reg_file := _registry_file(s):
                from sagrid.registry import load_registry

                registry = load_registry(reg_file)
            paths = export_powerbi(store, s.export_dir, registry)
            print(f"wrote {len(paths)} files to {s.export_dir}")
            return EXIT_OK

        if args.command == "registry":
            from sagrid.nemweb.client import NemwebClient
            from sagrid.registry import RegistryError, fetch_registry

            client = NemwebClient(
                s.cache_dir, retries=s.retries, backoff=s.backoff_s, timeout=s.timeout_s
            )
            on = args.on or datetime.now(UTC).date()
            try:
                built, month = fetch_registry(client, on)
            except RegistryError as exc:
                print(f"registry: {exc}", file=sys.stderr)
                return EXIT_RUN_FAILED
            out = s.data_dir / "registry.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            built.to_csv(out, index=False)
            counts = built["REGIONID"].value_counts().to_dict()
            print(f"wrote {len(built)} units ({counts}) from MMSDM {month} to {out}")
            return EXIT_OK

        if args.command == "investigate":
            from sagrid.investigations import i1_constraints, i2_heywood, i3_scada
            from sagrid.investigations.common import Window, write_report

            w = Window(args.start, args.end)
            which = ["i1", "i2", "i3"] if args.which == "all" else [args.which]
            registry = None
            reg_file = _registry_file(s)
            if "i3" in which and reg_file:
                from sagrid.registry import load_registry

                registry = load_registry(reg_file)
            failed = 0
            for key in which:
                try:
                    if key == "i1":
                        rep = i1_constraints.run(store, w)
                    elif key == "i2":
                        rep = i2_heywood.run(store, w)
                    else:
                        rep = i3_scada.run(store, w, registry)
                except ValueError as exc:
                    print(f"{key}: {exc}", file=sys.stderr)
                    failed += 1
                    continue
                paths = write_report(rep, args.out)
                print(f"{key}: wrote {paths[0]} and {len(paths) - 1} supporting files")
            return EXIT_BAD_INPUT if failed else EXIT_OK

        if args.command == "status":
            row = store.con.execute(
                "SELECT run_id, started_at, manifest_json FROM runs "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                print("no runs yet")
                return EXIT_OK
            totals = json.loads(row[2])["totals"]
            print(f"last run {row[0]} at {row[1]}")
            print(json.dumps(totals, indent=2))
            return EXIT_OK
    finally:
        store.close()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
