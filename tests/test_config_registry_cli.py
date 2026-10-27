import json
from datetime import datetime

import pandas as pd
import pytest

from sagrid.cli import EXIT_BAD_INPUT, EXIT_CHECKS_FAILED, EXIT_OK, main
from sagrid.config import ConfigError, load_settings
from sagrid.registry import RegistryError, load_registry


def test_defaults_and_env_override():
    s = load_settings(env={"SAGRID_RETRIES": "1", "SAGRID_MAX_REJECT_RATE": "0.05"})
    assert s.retries == 1 and s.max_reject_rate == 0.05
    assert s.heywood_max_at_limit_share is None


def test_optional_float_setting():
    s = load_settings(env={"SAGRID_HEYWOOD_MAX_AT_LIMIT_SHARE": "0.4"})
    assert s.heywood_max_at_limit_share == 0.4
    assert (
        load_settings(env={"SAGRID_HEYWOOD_MAX_AT_LIMIT_SHARE": ""}).heywood_max_at_limit_share
        is None
    )


def test_toml_file(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[sagrid]\nlog_level = "DEBUG"\ndb_path = "x/y.duckdb"\n')
    s = load_settings(p, env={})
    assert s.log_level == "DEBUG" and str(s.db_path).endswith("y.duckdb")


@pytest.mark.parametrize(
    "env,msg",
    [
        ({"SAGRID_RETRIES": "many"}, "invalid value"),
        ({"SAGRID_LOG_LEVEL": "LOUD"}, "log_level"),
        ({"SAGRID_MAX_REJECT_RATE": "2"}, "between 0 and 1"),
        ({"SAGRID_HEYWOOD_MAX_AT_LIMIT_SHARE": "1.5"}, "heywood_max_at_limit_share"),
        ({"SAGRID_HEYWOOD_MAX_AT_LIMIT_SHARE": "half"}, "invalid value"),
    ],
)
def test_invalid_settings_fail_fast(env, msg):
    with pytest.raises(ConfigError, match=msg):
        load_settings(env=env)


def test_unknown_setting_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[sagrid]\ntypo_setting = 1\n")
    with pytest.raises(ConfigError, match="unknown"):
        load_settings(p, env={})


def test_registry_finds_header_row(tmp_path):
    p = tmp_path / "reg.xlsx"
    with pd.ExcelWriter(p) as xw:
        pd.DataFrame([["About this file"], [""]]).to_excel(
            xw, sheet_name="Readme", header=False, index=False
        )
        rows = [
            ["NEM list", "", "", "", ""],
            ["Station Name", "Region", "Dispatch Type", "Fuel Source - Primary", "DUID"],
            ["Torrens B", "SA1", "Generator", "Fossil", "TORRB1"],
            ["Hornsdale", "SA1", "Bidirectional Unit", "Battery", "HPR1"],
            ["", "", "", "", "-"],
        ]
        pd.DataFrame(rows).to_excel(
            xw, sheet_name="PU and Scheduled Loads", header=False, index=False
        )
    reg = load_registry(p)
    assert reg["DUID"].tolist() == ["TORRB1", "HPR1"]
    assert reg["DISPATCHTYPE"].tolist() == ["GENERATOR", "BIDIRECTIONAL"]


def test_registry_errors(tmp_path):
    with pytest.raises(RegistryError, match="not found"):
        load_registry(tmp_path / "missing.xlsx")
    p = tmp_path / "bad.xlsx"
    pd.DataFrame({"a": [1]}).to_excel(p, index=False)
    with pytest.raises(RegistryError, match="no sheet"):
        load_registry(p)


def test_cli_bad_date_exits(capsys):
    with pytest.raises(SystemExit):
        main(["ingest", "--start", "1/10/2026", "--end", "2026-10-02"])


def test_cli_reversed_range(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SAGRID_DB_PATH", str(tmp_path / "db.duckdb"))
    monkeypatch.setenv("SAGRID_CACHE_DIR", str(tmp_path / "cache"))
    assert main(["ingest", "--start", "2026-10-05", "--end", "2026-10-01"]) == EXIT_BAD_INPUT


def test_cli_bad_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SAGRID_RETRIES", "x")
    assert main(["status"]) == EXIT_BAD_INPUT


def test_cli_status_and_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SAGRID_DB_PATH", str(tmp_path / "db.duckdb"))
    assert main(["status"]) == EXIT_OK
    assert "no runs yet" in capsys.readouterr().out
    assert (
        main(["check", "--start", "2026-10-01", "--end", "2026-10-01", "--json"])
        == EXIT_CHECKS_FAILED
    )
    out = json.loads(capsys.readouterr().out)
    assert out[0]["name"] == "data_present"


def test_cli_export(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SAGRID_DB_PATH", str(tmp_path / "db.duckdb"))
    monkeypatch.setenv("SAGRID_EXPORT_DIR", str(tmp_path / "out"))
    assert main(["export"]) == EXIT_OK
    assert (tmp_path / "out" / "dim_interconnector.csv").exists()


def test_yesterday_keyword():
    from sagrid.cli import _date

    assert _date("yesterday") < datetime.now().date() + pd.Timedelta(days=1)
