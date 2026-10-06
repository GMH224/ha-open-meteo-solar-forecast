"""Translation coverage for the 0.1.33.2 additions (reachability class:
a key that the code can emit but no translation covers shows up as a raw
key in the UI)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from custom_components.open_meteo_solar_forecast.const import ACTIVE_SOURCES, WEATHER_SOURCES

ROOT = Path(__file__).resolve().parents[1] / "custom_components/open_meteo_solar_forecast"
LOCAL_FIELDS = {
    "local_weather_entity",
    "local_irradiance_entity",
    "local_snow_depth_entity",
    "local_fallback_open_meteo",
}


def _error_keys_emitted_by_validators() -> set[str]:
    tree = ast.parse((ROOT / "local_provider.py").read_text())
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("validate_"):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Constant) and isinstance(sub.value.value, str):
                    keys.add(sub.value.value)
    return keys


def test_the_validators_emit_the_expected_error_keys():
    assert _error_keys_emitted_by_validators() == {
        "weather_not_found", "weather_no_hourly", "irradiance_not_found", "irradiance_no_series",
    }


@pytest.mark.parametrize("filename", ["strings.json", "translations/en.json", "translations/de.json"])
def test_every_new_ui_string_is_translated(filename):
    data = json.loads((ROOT / filename).read_text())
    for category, first_step in (("config", "user"), ("options", "init")):
        assert "weather_source" in data[category]["step"][first_step]["data"]
        local = data[category]["step"]["local"]
        assert set(local["data"]) == LOCAL_FIELDS
        assert set(local["data_description"]) == LOCAL_FIELDS
        assert _error_keys_emitted_by_validators() <= set(data[category]["error"])
    assert set(data["selector"]["weather_source"]["options"]) == set(WEATHER_SOURCES)
    assert set(data["entity"]["sensor"]["forecast_source"]["state"]) == set(ACTIVE_SOURCES)


def test_english_translation_matches_strings_for_the_new_keys():
    strings = json.loads((ROOT / "strings.json").read_text())
    english = json.loads((ROOT / "translations/en.json").read_text())
    for category in ("config", "options"):
        assert strings[category]["step"]["local"] == english[category]["step"]["local"]
    assert strings["selector"]["weather_source"] == english["selector"]["weather_source"]
    assert strings["entity"]["sensor"]["forecast_source"] == english["entity"]["sensor"]["forecast_source"]
