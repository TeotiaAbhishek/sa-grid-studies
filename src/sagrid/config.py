"""Settings from a TOML file with SAGRID_* environment overrides, validated at start-up."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data")
    db_path: Path = Path("data/sagrid.duckdb")
    cache_dir: Path = Path("data/cache")
    export_dir: Path = Path("data/exports")
    registry_path: Path | None = None
    log_level: str = "INFO"
    retries: int = 4
    backoff_s: float = 2.0
    timeout_s: float = 60.0
    max_reject_rate: float = 0.01
    max_staleness_hours: float = 36.0
    balance_tolerance_mw: float = 50.0


def _coerce(name: str, value, target):
    try:
        if target in (Path, "Path"):
            return Path(value)
        if "Path | None" in str(target):
            return Path(value) if value not in (None, "") else None
        if target in (int, "int"):
            return int(value)
        if target in (float, "float"):
            return float(value)
        return str(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"setting {name!r} has invalid value {value!r}") from exc


def load_settings(path: Path | None = None, env: dict | None = None) -> Settings:
    env = os.environ if env is None else env
    raw: dict = {}
    if path is not None:
        if not Path(path).exists():
            raise ConfigError(f"config file not found: {path}")
        with open(path, "rb") as fh:
            raw = tomllib.load(fh).get("sagrid", {})
    known = {f.name: f.type for f in fields(Settings)}
    unknown = set(raw) - set(known)
    if unknown:
        raise ConfigError(f"unknown settings in {path}: {sorted(unknown)}")
    for name in known:
        key = f"SAGRID_{name.upper()}"
        if key in env:
            raw[name] = env[key]
    values = {k: _coerce(k, v, known[k]) for k, v in raw.items()}
    s = Settings(**values)
    if s.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigError(f"log_level must be DEBUG, INFO, WARNING or ERROR, not {s.log_level}")
    if not 0 <= s.max_reject_rate <= 1:
        raise ConfigError("max_reject_rate must be between 0 and 1")
    if s.retries < 0 or s.timeout_s <= 0:
        raise ConfigError("retries must be >= 0 and timeout_s > 0")
    return s
