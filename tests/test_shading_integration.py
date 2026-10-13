"""Home Assistant integration tests for partial shading (0.1.33.5).

The Open-Meteo path runs the real library against a synthetic clear-sky
response (the library's network method is replaced, sockets are disabled by
the plugin), so the shading step is exercised exactly where it runs in
production: between the response and the library's power calculation.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd
import pvlib
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from open_meteo_solar_forecast import OpenMeteoSolarForecast
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_meteo_solar_forecast import shading_provider
from custom_components.open_meteo_solar_forecast.const import (
    CONF_SHADING_FILEPATH,
    CONF_WEATHER_SOURCE,
    DOMAIN,
    SOURCE_HYBRID,
)
from custom_components.open_meteo_solar_forecast.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.open_meteo_solar_forecast.shading_provider import (
    ShadedOpenMeteoSolarForecast,
)

from .fusion_fixtures import LAT, LON
from .test_local_integration import (
    BASE_OPTIONS,
    FETCH_OM,
    HOUR0,
    LOCAL_OPTIONS,
    NETWORK,
    NOW,
    FakeFusion,
    _coordinator,
    _setup,
)

ROOT = Path(__file__).resolve().parents[1]
SOUTH_GEOMETRY = ROOT / "examples" / "shading" / "south_geometry.yaml"


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture
async def zurich(hass):
    await hass.config.async_set_time_zone("Europe/Zurich")
    hass.config.latitude, hass.config.longitude = LAT, LON
    yield


def clear_sky_response(params: dict) -> dict:
    """Open-Meteo-shaped response for the requested plane (clear sky)."""
    tilt = float(params["tilt"])
    compass = (float(params["azimuth"]) + 180.0) % 360.0
    start = pd.Timestamp(dt_util.utcnow()).floor("D") - pd.Timedelta(days=1)
    times = pd.date_range(start, periods=96 * 4, freq="15min", tz="UTC") + pd.Timedelta("15min")
    loc = pvlib.location.Location(LAT, LON, altitude=473)

    def plane(moments):
        sp = loc.get_solarposition(moments)
        cs = loc.get_clearsky(moments, solar_position=sp)
        poa = pvlib.irradiance.get_total_irradiance(tilt, compass, sp["apparent_zenith"], sp["azimuth"],
                                                    cs["dni"], cs["ghi"], cs["dhi"], model="isotropic")
        return poa["poa_global"].fillna(0).clip(lower=0), (cs["ghi"] - cs["dhi"]).clip(lower=0), cs["dhi"]

    gti, direct, dhi = plane(times - pd.Timedelta("7.5min"))
    gti_i, direct_i, dhi_i = plane(times)
    days = pd.date_range(start, periods=5, freq="D", tz="UTC")
    sun = loc.get_sun_rise_set_transit(days, method="spa")
    return {
        "utc_offset_seconds": 7200,
        "minutely_15": {
            "time": [int(t.timestamp()) for t in times],
            "temperature_2m": [12.0] * len(times),
            "global_tilted_irradiance": [float(v) for v in gti],
            "global_tilted_irradiance_instant": [float(v) for v in gti_i],
            "diffuse_radiation": [float(v) for v in dhi],
            "diffuse_radiation_instant": [float(v) for v in dhi_i],
            "direct_radiation": [float(v) for v in direct],
            "direct_radiation_instant": [float(v) for v in direct_i],
            "snow_depth": [0.0] * len(times),
        },
        "daily": {
            "time": [int(d.timestamp()) for d in days],
            "sunrise": [int(t.timestamp()) for t in sun["sunrise"]],
            "sunset": [int(t.timestamp()) for t in sun["sunset"]],
        },
    }


async def fake_request(self, uri, *, params=None):  # noqa: ARG001
    return clear_sky_response(dict(params))


def morning_shade_table(path: Path) -> Path:
    """Sun east of 150 deg fully blocks direct light; otherwise unshaded."""
    rows = [f"{az}\t{el}\t{0.0 if az < 150 else 1.0}" for az in range(0, 360, 10) for el in (0, 90)]
    path.write_text("# test table\n" + "\n".join(rows) + "\n")
    return path


async def setup_om(hass, options):
    entry = MockConfigEntry(domain=DOMAIN, title="Home", data={CONF_LATITUDE: LAT, CONF_LONGITUDE: LON},
                            options=options)
    entry.add_to_hass(hass)
    with patch(NETWORK, fake_request):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def hourly(entry, hass):
    return _coordinator(hass, entry).data.wh_period


# ---------------------------------------------------------------------------
# Open-Meteo mode
# ---------------------------------------------------------------------------
async def test_open_meteo_mode_without_shading_keeps_the_library_class(hass, zurich, freezer):
    freezer.move_to(NOW)
    entry = await setup_om(hass, BASE_OPTIONS)
    coordinator = _coordinator(hass, entry)
    assert type(coordinator.forecast) is OpenMeteoSolarForecast  # parity: unmodified class
    assert coordinator.shading_report == [{"array": 0, "file": None, "state": "none", "error": None,
                                           "digest": None, "summary": {}, "warnings": []}]


async def test_open_meteo_mode_shading_reduces_only_shaded_hours(hass, zurich, freezer, tmp_path):
    freezer.move_to(NOW)
    plain = await setup_om(hass, BASE_OPTIONS)
    reference = dict(hourly(plain, hass))
    table = morning_shade_table(tmp_path / "shade.txt")
    shaded = await setup_om(hass, {**BASE_OPTIONS, CONF_SHADING_FILEPATH: str(table)})
    coordinator = _coordinator(hass, shaded)
    assert isinstance(coordinator.forecast, ShadedOpenMeteoSolarForecast)
    values = hourly(shaded, hass)
    morning = [t for t in reference if 7 <= t.hour <= 9 and reference[t] > 50]
    afternoon = [t for t in reference if 14 <= t.hour <= 16 and reference[t] > 50]
    assert morning and afternoon
    for t in morning:
        assert values[t] < 0.6 * reference[t]
    for t in afternoon:
        assert values[t] == pytest.approx(reference[t], rel=1e-6)
    report = coordinator.shading_report[0]
    assert report["state"] == "table"
    assert report["last_open_meteo"]["removed_share"] > 0.05
    # every refresh maps the responses to the arrays afresh (counter reset)
    with patch(NETWORK, fake_request):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert coordinator.forecast.shading_stats[0]["removed_share"] > 0.05
    assert hourly(shaded, hass)[morning[0]] < 0.6 * reference[morning[0]]


async def test_geometry_file_for_the_south_array(hass, zurich, freezer):
    freezer.move_to(NOW)
    options = {**BASE_OPTIONS, "azimuth": 197, "declination": 40}  # 0.1.34: 40 deg refit
    plain = await setup_om(hass, options)
    reference = dict(hourly(plain, hass))
    shaded = await setup_om(hass, {**options, CONF_SHADING_FILEPATH: str(SOUTH_GEOMETRY)})
    values = hourly(shaded, hass)
    report = _coordinator(hass, shaded).shading_report[0]
    assert report["state"] == "geometry"
    assert report["summary"]["modules"] == 15
    assert report["warnings"] == []
    # October: shaded from sunrise to ~10:30 local, free afterwards
    day = dt_util.as_local(NOW).date() + timedelta(days=1)
    early = [t for t in reference if t.date() == day and t.hour == 9]
    late = [t for t in reference if t.date() == day and t.hour == 13]
    assert values[early[0]] < 0.8 * reference[early[0]]
    assert values[late[0]] == pytest.approx(reference[late[0]], rel=1e-6)
    total_ref = sum(v for t, v in reference.items() if t.date() == day)
    total = sum(v for t, v in values.items() if t.date() == day)
    assert 0.02 < 1 - total / total_ref < 0.12  # analysis: ~4.5 % in October


async def test_geometry_orientation_mismatch_is_reported(hass, zurich, freezer):
    freezer.move_to(NOW)
    entry = await setup_om(hass, {**BASE_OPTIONS, CONF_SHADING_FILEPATH: str(SOUTH_GEOMETRY)})
    warnings = _coordinator(hass, entry).shading_report[0]["warnings"]
    assert any("azimuth" in w for w in warnings) and any("tilt" in w for w in warnings)


async def test_invalid_file_never_stops_the_forecast(hass, zurich, freezer, tmp_path):
    freezer.move_to(NOW)
    bad = tmp_path / "bad.txt"
    bad.write_text("0 0 7\n")
    entry = await setup_om(hass, {**BASE_OPTIONS, CONF_SHADING_FILEPATH: str(bad)})
    assert entry.state is ConfigEntryState.LOADED
    coordinator = _coordinator(hass, entry)
    assert type(coordinator.forecast) is OpenMeteoSolarForecast
    report = coordinator.shading_report[0]
    assert report["state"] == "error" and "factor" in report["error"]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"shading_{entry.entry_id}_0")
    assert issue is not None and issue.translation_key == "invalid_shading_file"
    source = hass.states.get("sensor.home_forecast_source")
    assert source.attributes["shading"][0]["state"] == "error"


async def test_missing_file_is_an_issue_and_fixing_it_clears_the_issue(hass, zurich, freezer, tmp_path):
    freezer.move_to(NOW)
    path = tmp_path / "later.txt"
    entry = await setup_om(hass, {**BASE_OPTIONS, CONF_SHADING_FILEPATH: str(path)})
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"shading_{entry.entry_id}_0") is not None
    morning_shade_table(path)
    with patch(NETWORK, fake_request):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"shading_{entry.entry_id}_0") is None
    assert _coordinator(hass, entry).shading_report[0]["state"] == "table"


async def test_removing_the_entry_removes_its_issues(hass, zurich, freezer, tmp_path):
    freezer.move_to(NOW)
    entry = await setup_om(hass, {**BASE_OPTIONS, CONF_SHADING_FILEPATH: str(tmp_path / "none.txt")})
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"shading_{entry.entry_id}_0") is not None
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"shading_{entry.entry_id}_0") is None


async def test_tracking_array_with_shading_file_is_refused_gracefully(hass, zurich, freezer, tmp_path):
    freezer.move_to(NOW)
    table = morning_shade_table(tmp_path / "shade.txt")
    entry = await setup_om(hass, {**BASE_OPTIONS, "tracking": "dual", CONF_SHADING_FILEPATH: str(table)})
    assert entry.state is ConfigEntryState.LOADED
    report = _coordinator(hass, entry).shading_report[0]
    assert report["state"] == "error" and "tracking" in report["error"]


async def test_multi_array_entry_shades_only_the_array_with_a_file(hass, zurich, freezer, tmp_path):
    freezer.move_to(NOW)
    table = morning_shade_table(tmp_path / "shade.txt")
    two = {**BASE_OPTIONS, "azimuth": [180, 180], "declination": [30, 30], "modules_power": [5000, 5000],
           "efficiency_factor": [1.0, 1.0], "tracking": ["none", "none"], "use_horizon": [False, False],
           "partial_shading": [False, False], "array_inverter_power": [0, 0],
           "horizon_filepath": [BASE_OPTIONS["horizon_filepath"]] * 2,
           CONF_SHADING_FILEPATH: ["", str(table)]}
    entry = await setup_om(hass, two)
    coordinator = _coordinator(hass, entry)
    stats = coordinator.forecast.shading_stats
    assert stats[0] is None and stats[1]["removed_share"] > 0.05
    assert [r["state"] for r in coordinator.shading_report] == ["none", "table"]


async def test_cached_file_is_not_parsed_again(hass, zurich, freezer, tmp_path):
    freezer.move_to(NOW)
    table = morning_shade_table(tmp_path / "cache.txt")
    calls = []
    real = shading_provider.load_shading_text

    def counting(text, name):
        calls.append(name)
        return real(text, name)

    with patch.object(shading_provider, "load_shading_text", counting):
        entry = await setup_om(hass, {**BASE_OPTIONS, CONF_SHADING_FILEPATH: str(table)})
        with patch(NETWORK, fake_request):
            await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
    assert len(calls) == 1


async def test_diagnostics_contain_the_shading_report(hass, zurich, freezer, tmp_path):
    freezer.move_to(NOW)
    table = morning_shade_table(tmp_path / "shade.txt")
    entry = await setup_om(hass, {**BASE_OPTIONS, CONF_SHADING_FILEPATH: str(table)})
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["shading"][0]["state"] == "table"
    assert diag["shading"][0]["file"] == "shade.txt"  # file name only, not the full path


# ---------------------------------------------------------------------------
# Local and hybrid mode
# ---------------------------------------------------------------------------
async def test_local_mode_applies_the_same_shading(hass, zurich, freezer, tmp_path):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    plain, _ = await _setup(hass, LOCAL_OPTIONS)
    reference = dict(_coordinator(hass, plain).data.wh_period)
    table = morning_shade_table(tmp_path / "shade.txt")
    shaded, _ = await _setup(hass, {**LOCAL_OPTIONS, CONF_SHADING_FILEPATH: str(table)})
    coordinator = _coordinator(hass, shaded)
    assert type(coordinator.forecast) is ShadedOpenMeteoSolarForecast
    values = coordinator.data.wh_period
    tomorrow = dt_util.as_local(NOW).date() + timedelta(days=1)
    morning = [t for t in reference if t.date() == tomorrow and t.hour == 9]
    afternoon = [t for t in reference if t.date() == tomorrow and t.hour == 15]
    assert values[morning[0]] < 0.7 * reference[morning[0]]
    assert values[afternoon[0]] == pytest.approx(reference[afternoon[0]], rel=1e-6)
    assert coordinator.shading_report[0]["last_local"]["removed_share"] > 0.03


async def test_hybrid_mode_shades_both_parts(hass, zurich, freezer, tmp_path):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    table = morning_shade_table(tmp_path / "shade.txt")
    options = {**LOCAL_OPTIONS, CONF_WEATHER_SOURCE: SOURCE_HYBRID, CONF_SHADING_FILEPATH: str(table)}
    entry, _ = await _setup(hass, options, om_estimate=AsyncMock(side_effect=RuntimeError("down")))
    coordinator = _coordinator(hass, entry)
    assert coordinator.local_forecast.shading_tables is coordinator.forecast.shading_tables or (
        coordinator.local_forecast.shading_tables == coordinator.forecast.shading_tables
    )
    assert coordinator.local_forecast.shading_tables[0] is not None


# ---------------------------------------------------------------------------
# Config and options flow
# ---------------------------------------------------------------------------
async def _to_array_step(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Roof", "api_key": "", "base_url": "https://api.open-meteo.com", "model": "best_match",
         "weather_source": "open_meteo", "inverter_power": 0, "max_snowcover_depth_cm": 0.0},
    )
    assert result["step_id"] == "array"
    return result


ARRAY = {"declination": 45, "azimuth": 197, "modules_power": 6000, "array_inverter_power": 0,
         "efficiency_factor": 0.9, "tracking": "none", "damping_morning": 0.0, "damping_evening": 0.0,
         "use_horizon": False, "partial_shading": False}


async def test_config_flow_rejects_an_invalid_shading_file(hass, zurich, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\narray: {}\n")
    result = await _to_array_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ARRAY, CONF_SHADING_FILEPATH: str(bad)})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SHADING_FILEPATH: "invalid_shading_file"}
    assert "array.azimuth" in result["description_placeholders"]["shading_error"]


async def test_config_flow_rejects_shading_on_a_tracker(hass, zurich, tmp_path):
    table = morning_shade_table(tmp_path / "shade.txt")
    result = await _to_array_step(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ARRAY, "tracking": "dual", CONF_SHADING_FILEPATH: str(table)})
    assert result["errors"] == {CONF_SHADING_FILEPATH: "shading_requires_fixed_array"}


async def test_config_flow_stores_a_valid_geometry_file(hass, zurich):
    result = await _to_array_step(hass)
    with patch("custom_components.open_meteo_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**ARRAY, CONF_SHADING_FILEPATH: f"  {SOUTH_GEOMETRY}  "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_SHADING_FILEPATH] == str(SOUTH_GEOMETRY)


async def test_config_flow_without_shading_stores_an_empty_path(hass, zurich):
    result = await _to_array_step(hass)
    with patch("custom_components.open_meteo_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], ARRAY)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_SHADING_FILEPATH] == ""


async def test_options_flow_validates_and_can_clear_the_shading_file(hass, zurich, freezer, tmp_path):
    freezer.move_to(NOW)
    table = morning_shade_table(tmp_path / "shade.txt")
    entry = await setup_om(hass, {**BASE_OPTIONS, CONF_SHADING_FILEPATH: str(table)})
    common = {"base_url": "https://api.open-meteo.com", "model": "best_match", "inverter_power": 0,
              "max_snowcover_depth_cm": 0.0, "weather_source": "open_meteo"}
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], common)
    assert result["step_id"] == "array"
    # prefilled with the stored path (suggested value)
    schema_keys = {str(k): k for k in result["data_schema"].schema}
    assert schema_keys[CONF_SHADING_FILEPATH].description["suggested_value"] == str(table)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**ARRAY, CONF_SHADING_FILEPATH: str(tmp_path / "missing.txt")})
    assert result["errors"] == {CONF_SHADING_FILEPATH: "invalid_shading_file"}
    assert "not found" in result["description_placeholders"]["shading_error"]
    with patch(NETWORK, fake_request):
        result = await hass.config_entries.options.async_configure(result["flow_id"], ARRAY)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SHADING_FILEPATH] == ""


# ---------------------------------------------------------------------------
# Upgrade path
# ---------------------------------------------------------------------------
async def test_entry_from_0_1_33_4_without_the_key_runs_unshaded(hass, zurich, freezer):
    freezer.move_to(NOW)
    assert CONF_SHADING_FILEPATH not in BASE_OPTIONS
    entry = await setup_om(hass, BASE_OPTIONS)
    assert entry.state is ConfigEntryState.LOADED
    assert _coordinator(hass, entry).shading_report[0]["state"] == "none"
    assert np.isfinite(float(hass.states.get("sensor.home_energy_production_tomorrow").state))


_ = FETCH_OM  # imported for symmetry with the other integration tests


def test_changed_file_is_read_again(tmp_path):
    import os

    path = morning_shade_table(tmp_path / "change.txt")
    first = shading_provider.read_shading_file(str(path))
    path.write_text("0 0 0.5\n0 90 0.5\n180 0 0.5\n180 90 0.5\n")
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    second = shading_provider.read_shading_file(str(path))
    assert first.digest != second.digest
    assert second.summary["min_factor"] == 0.5


def test_unreadable_and_oversized_files_give_messages(tmp_path):
    assert "not found" in shading_provider.validate_shading_file(str(tmp_path / "nope.txt"))
    assert "regular file" in shading_provider.validate_shading_file(str(tmp_path))
    big = tmp_path / "big.txt"
    big.write_text("#" * (shading_provider.SHADING_FILE_MAX_BYTES + 1))
    assert "limit" in shading_provider.validate_shading_file(str(big))
    binary = tmp_path / "bin.txt"
    binary.write_bytes(b"\xff\xfe\x00")
    assert "cannot be read" in shading_provider.validate_shading_file(str(binary))
