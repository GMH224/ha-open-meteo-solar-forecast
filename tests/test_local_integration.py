"""Home Assistant integration tests for the local weather source (0.1.33.2).

Real Home Assistant via pytest-homeassistant-custom-component. Sockets are
disabled by the plugin; additionally the Open-Meteo instance's ``estimate``
is replaced by a recorder, so "local mode never contacts Open-Meteo" is
asserted, not assumed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from open_meteo_solar_forecast import Estimate
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.open_meteo_solar_forecast.const import (
    ACTIVE_SOURCE_FALLBACK,
    ACTIVE_SOURCE_LOCAL,
    ACTIVE_SOURCE_OPEN_METEO,
    ACTIVE_SOURCE_RETAINED,
    CONF_LOCAL_FALLBACK_OPEN_METEO,
    CONF_LOCAL_IRRADIANCE_ENTITY,
    CONF_LOCAL_SNOW_DEPTH_ENTITY,
    CONF_LOCAL_WEATHER_ENTITY,
    CONF_WEATHER_SOURCE,
    DOMAIN,
    SOURCE_LOCAL,
    SOURCE_OPEN_METEO,
)
from custom_components.open_meteo_solar_forecast.coordinator import _config_fingerprint
from custom_components.open_meteo_solar_forecast.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.open_meteo_solar_forecast.local_provider import (
    LocalDataError,
    history_storage_key,
)

from .fusion_fixtures import LAT, LON, fusion_series, weather_hourly

UTC = timezone.utc
NOW = datetime(2026, 10, 6, 9, 5, tzinfo=UTC)  # 11:05 Europe/Zurich
HOUR0 = NOW.replace(minute=0)
WEATHER = "weather.swissweather_fusion"
IRRADIANCE = "sensor.swissweather_fusion_solar_irradiance_ghi_hour_average"
SNOW = "sensor.swissweather_fusion_snow_depth"

BASE_OPTIONS = {
    "api_key": "",
    "base_url": "https://api.open-meteo.com",
    "model": "best_match",
    "inverter_power": 0,
    "max_snowcover_depth_cm": 0.0,
    "declination": 30,
    "azimuth": 180,
    "modules_power": 10000,
    "array_inverter_power": 0,
    "efficiency_factor": 1.0,
    "tracking": "none",
    "damping_morning": 0.0,
    "damping_evening": 0.0,
    "use_horizon": False,
    "partial_shading": False,
    "horizon_filepath": "/config/custom_components/open_meteo_solar_forecast/horizon.txt",
}
LOCAL_OPTIONS = {
    **BASE_OPTIONS,
    CONF_WEATHER_SOURCE: SOURCE_LOCAL,
    CONF_LOCAL_WEATHER_ENTITY: WEATHER,
    CONF_LOCAL_IRRADIANCE_ENTITY: IRRADIANCE,
    CONF_LOCAL_SNOW_DEPTH_ENTITY: SNOW,
    CONF_LOCAL_FALLBACK_OPEN_METEO: False,
}


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture
async def zurich(hass: HomeAssistant):
    await hass.config.async_set_time_zone("Europe/Zurich")
    hass.config.latitude, hass.config.longitude = LAT, LON
    yield


class FakeFusion:
    """Publishes Fusion-shaped states and answers weather.get_forecasts."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.forecast_calls = 0
        self.fail_service = False
        hass.services.async_register(
            "weather", "get_forecasts", self._get_forecasts, supports_response=SupportsResponse.ONLY
        )

    async def _get_forecasts(self, call: ServiceCall):
        self.forecast_calls += 1
        if self.fail_service:
            raise RuntimeError("weather service down")
        first = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        return {entity: {"forecast": weather_hourly(first)} for entity in call.data["entity_id"]}

    def publish(self, first_hour: datetime, hours: int = 120, **kwargs) -> None:
        self.hass.states.async_set(
            IRRADIANCE, "300.0",
            {"hourly_forecast": fusion_series(first_hour, hours, **kwargs), "unit_of_measurement": "W/m²"},
        )
        self.hass.states.async_set(
            WEATHER, "sunny",
            {"temperature": 13.4, "temperature_unit": "°C", "supported_features": 7},
        )
        self.hass.states.async_set(SNOW, "0.0", {"unit_of_measurement": "m"})


def _estimate_stub() -> Estimate:
    """Minimal Open-Meteo-mode estimate covering today and tomorrow (the
    library raises for a peak-time day without data, pre-existing P-08)."""
    tz = timezone(timedelta(hours=2))
    now = dt_util.utcnow().astimezone(tz).replace(minute=0, second=0, microsecond=0)
    tomorrow = now + timedelta(days=1)
    return Estimate(
        watts={now: 1234, tomorrow: 2345},
        wh_period={now: 1000, tomorrow: 2000},
        wh_days={now.date(): 5000, tomorrow.date(): 6000},
        wh_period_15m={now: 250, tomorrow: 500},
        api_timezone=tz,
    )


async def _setup(hass, options, *, om_estimate=None):
    entry = MockConfigEntry(
        domain=DOMAIN, title="Home", data={CONF_LATITUDE: LAT, CONF_LONGITUDE: LON}, options=options,
    )
    entry.add_to_hass(hass)
    om = om_estimate or AsyncMock(side_effect=AssertionError("Open-Meteo must not be contacted"))
    # Two guards: the coordinator's Open-Meteo path is a recorder, and the
    # library's network method raises if anything reaches it anyway.
    with patch(FETCH_OM, om), patch(NETWORK, AsyncMock(side_effect=AssertionError("network"))):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, om


FETCH_OM = (
    "custom_components.open_meteo_solar_forecast.coordinator."
    "OpenMeteoSolarForecastDataUpdateCoordinator._async_fetch_open_meteo"
)
NETWORK = "open_meteo_solar_forecast.OpenMeteoSolarForecast._request"


def _coordinator(hass, entry):
    return hass.data[DOMAIN][entry.entry_id]


# ---------------------------------------------------------------------------
# Local mode happy path
# ---------------------------------------------------------------------------
async def test_local_mode_produces_a_forecast_without_contacting_open_meteo(hass, zurich, freezer):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.publish(HOUR0)
    entry, om = await _setup(hass, LOCAL_OPTIONS)
    assert entry.state is ConfigEntryState.LOADED
    om.assert_not_called()
    coordinator = _coordinator(hass, entry)
    assert coordinator.active_source == ACTIVE_SOURCE_LOCAL
    assert coordinator.update_interval == timedelta(minutes=10)
    assert fusion.forecast_calls == 1
    tomorrow_kwh = float(hass.states.get("sensor.home_energy_production_tomorrow").state)
    # 10 kWp, 30° south, idealised early-October clear day (fixture kt 0.75):
    # 4-7.5 kWh/kWp. The sensor's suggested unit is kWh.
    assert 40 < tomorrow_kwh < 75
    assert hass.states.get("sensor.home_forecast_source").state == ACTIVE_SOURCE_LOCAL
    assert float(hass.states.get("sensor.home_power_production_now").state) > 1000


async def test_open_meteo_mode_is_unchanged_and_never_reads_local_entities(hass, zurich, freezer):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    stub = AsyncMock(return_value=_estimate_stub())
    entry, om = await _setup(hass, BASE_OPTIONS, om_estimate=stub)
    coordinator = _coordinator(hass, entry)
    assert om.await_count == 1
    assert coordinator.weather_source == SOURCE_OPEN_METEO
    assert coordinator.active_source == ACTIVE_SOURCE_OPEN_METEO
    assert coordinator.update_interval == timedelta(minutes=30)
    assert coordinator.local_reader is None and coordinator.local_forecast is None
    assert fusion.forecast_calls == 0


async def test_open_meteo_mode_passes_the_same_library_arguments_as_before(hass, zurich, freezer):
    """Parity with 0.1.33.1: the constructor receives exactly the keyword
    arguments the previous release passed (names and values)."""
    freezer.move_to(NOW)
    captured = {}
    import custom_components.open_meteo_solar_forecast.coordinator as coord

    real = coord.OpenMeteoSolarForecast

    def spy(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    with patch.object(coord, "OpenMeteoSolarForecast", side_effect=spy), patch.object(
        real, "estimate", AsyncMock(return_value=_estimate_stub())
    ):
        entry = MockConfigEntry(domain=DOMAIN, title="Home", data={CONF_LATITUDE: LAT, CONF_LONGITUDE: LON},
                                options=BASE_OPTIONS)
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert set(captured) == {
        "api_key", "session", "latitude", "longitude", "azimuth", "base_url", "ac_kwp", "dc_kwp",
        "declination", "efficiency_factor", "tracking", "damping_morning", "damping_evening",
        "use_horizon", "partial_shading", "horizon_map", "max_snowcover_depth_cm", "weather_model",
    }
    assert captured["azimuth"] == 0 and captured["dc_kwp"] == 10.0
    assert captured["weather_model"] == "best_match" and captured["api_key"] is None


def test_an_existing_entry_keeps_its_retained_forecast_fingerprint():
    """Upgrading must not invalidate a 0.1.33.1 retained forecast: the
    fingerprint depends only on stored options, and nothing is migrated."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LATITUDE: LAT, CONF_LONGITUDE: LON}, options=BASE_OPTIONS)
    import json
    assert _config_fingerprint(entry) == json.dumps(
        {CONF_LATITUDE: LAT, CONF_LONGITUDE: LON, **BASE_OPTIONS}, sort_keys=True, default=str
    )


# ---------------------------------------------------------------------------
# Failure handling (fail-safe by default)
# ---------------------------------------------------------------------------
async def test_stale_local_data_at_startup_without_fallback_keeps_the_entry_retrying(hass, zurich, freezer):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.publish(HOUR0 - timedelta(hours=5))  # source stalled 5 h ago
    entry, om = await _setup(hass, LOCAL_OPTIONS)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    om.assert_not_called()


async def test_stale_local_data_later_serves_the_retained_forecast_and_says_so(hass, zurich, freezer):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.publish(HOUR0)
    entry, om = await _setup(hass, LOCAL_OPTIONS)
    coordinator = _coordinator(hass, entry)
    good = coordinator.data

    freezer.move_to(NOW + timedelta(hours=3))  # Fusion stops updating
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success
    assert coordinator.data is good
    assert coordinator.active_source == ACTIVE_SOURCE_RETAINED
    assert "stale" in coordinator.last_source_error
    state = hass.states.get("sensor.home_forecast_source")
    assert state.state == ACTIVE_SOURCE_RETAINED
    assert "stale" in state.attributes["last_error"]
    om.assert_not_called()

    # Recovery: Fusion publishes again.
    fusion.publish(HOUR0 + timedelta(hours=3))
    await coordinator.async_refresh()
    assert coordinator.active_source == ACTIVE_SOURCE_LOCAL
    assert coordinator.last_source_error is None


async def test_fallback_to_open_meteo_only_when_explicitly_enabled(hass, zurich, freezer):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.publish(HOUR0 - timedelta(hours=5))
    stub = AsyncMock(return_value=_estimate_stub())
    entry, om = await _setup(hass, {**LOCAL_OPTIONS, CONF_LOCAL_FALLBACK_OPEN_METEO: True}, om_estimate=stub)
    coordinator = _coordinator(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert om.await_count == 1
    assert coordinator.active_source == ACTIVE_SOURCE_FALLBACK
    assert "stale" in coordinator.last_source_error

    fusion.publish(HOUR0)
    with patch(FETCH_OM, stub), patch(NETWORK, AsyncMock(side_effect=AssertionError("network"))):
        await coordinator.async_refresh()
    assert coordinator.active_source == ACTIVE_SOURCE_LOCAL
    assert om.await_count == 1  # recovered without another Open-Meteo call


async def test_a_missing_irradiance_entity_is_a_local_data_error(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    hass.states.async_remove(IRRADIANCE)
    entry, _ = await _setup(hass, LOCAL_OPTIONS)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_failing_weather_service_degrades_to_the_current_temperature(hass, zurich, freezer):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.fail_service = True
    fusion.publish(HOUR0)
    entry, _ = await _setup(hass, LOCAL_OPTIONS)
    coordinator = _coordinator(hass, entry)
    assert coordinator.active_source == ACTIVE_SOURCE_LOCAL
    report = coordinator.local_reader.last_report["temperature"]
    assert "weather service down" in report["error"]
    assert report["current_temperature_c"] == 13.4
    assert all(s["temperature_fallback"] > 0 for s in coordinator.last_local_stats)


async def test_no_temperature_at_all_is_refused(hass, zurich, freezer):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.fail_service = True
    fusion.publish(HOUR0)
    hass.states.async_set(WEATHER, "sunny", {"temperature_unit": "°C", "supported_features": 7})
    entry, _ = await _setup(hass, LOCAL_OPTIONS)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_an_exception_inside_the_library_is_contained_like_bad_data(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    with patch(
        "custom_components.open_meteo_solar_forecast.local_provider.build_open_meteo_payload",
        side_effect=ZeroDivisionError("boom"),
    ):
        entry, _ = await _setup(hass, LOCAL_OPTIONS)
    assert entry.state is ConfigEntryState.SETUP_RETRY


# ---------------------------------------------------------------------------
# History: elapsed hours of today survive although the source drops them
# ---------------------------------------------------------------------------
async def test_energy_today_keeps_elapsed_hours_after_the_source_moves_on(hass, zurich, freezer):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.publish(HOUR0)
    entry, _ = await _setup(hass, LOCAL_OPTIONS)
    coordinator = _coordinator(hass, entry)
    today = dt_util.as_local(NOW).date()
    first = coordinator.data.wh_days[today]

    later = NOW + timedelta(hours=3)
    freezer.move_to(later)
    fusion.publish(later.replace(minute=0))  # series now starts 3 h later
    await coordinator.async_refresh()
    # Same weather, same day: energy today must not lose the 3 elapsed hours.
    assert coordinator.data.wh_days[today] == pytest.approx(first, rel=0.01)
    hours = coordinator.local_reader.last_report["history"]["hours"]
    assert hours == 120 + 3


async def test_history_survives_a_restart(hass, zurich, freezer, hass_storage):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.publish(HOUR0)
    entry, _ = await _setup(hass, LOCAL_OPTIONS)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=40))
    await hass.async_block_till_done()
    key = history_storage_key(entry.entry_id)
    assert key in hass_storage
    stored_hours = hass_storage[key]["data"]["hours"]
    assert stored_hours[0]["start"] == HOUR0.isoformat()


async def test_removing_the_entry_deletes_the_local_history(hass, zurich, freezer, hass_storage):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    entry, _ = await _setup(hass, LOCAL_OPTIONS)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=40))
    await hass.async_block_till_done()
    assert history_storage_key(entry.entry_id) in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert history_storage_key(entry.entry_id) not in hass_storage


async def test_corrupt_history_is_discarded_not_fatal(hass, zurich, freezer, hass_storage):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    entry = MockConfigEntry(domain=DOMAIN, title="Home", data={CONF_LATITUDE: LAT, CONF_LONGITUDE: LON},
                            options=LOCAL_OPTIONS)
    hass_storage[history_storage_key(entry.entry_id)] = {
        "version": 1, "minor_version": 1, "key": history_storage_key(entry.entry_id),
        "data": {"hours": [{"start": "not a date"}], "temperatures": {}},
    }
    entry.add_to_hass(hass)
    with patch(FETCH_OM, AsyncMock(side_effect=AssertionError)):
        assert await hass.config_entries.async_setup(entry.entry_id)
    assert _coordinator(hass, entry).active_source == ACTIVE_SOURCE_LOCAL


# ---------------------------------------------------------------------------
# Snow
# ---------------------------------------------------------------------------
async def test_snow_depth_reduces_output_only_when_snow_derating_is_enabled(hass, zurich, freezer):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.publish(HOUR0)
    hass.states.async_set(SNOW, "0.05", {"unit_of_measurement": "m"})
    entry, _ = await _setup(hass, {**LOCAL_OPTIONS, "max_snowcover_depth_cm": 10.0})
    coordinator = _coordinator(hass, entry)
    tomorrow = dt_util.as_local(NOW).date() + timedelta(days=1)
    half = coordinator.data.wh_days[tomorrow]
    hass.states.async_set(SNOW, "0.0", {"unit_of_measurement": "m"})
    await coordinator.async_refresh()
    full = coordinator.data.wh_days[tomorrow]
    # Irradiance is halved (5 cm of 10 cm). Energy falls slightly less than
    # half: cooler cells at lower irradiance are more efficient (library's
    # temperature model). Both bounds matter: no derating, or derating
    # applied twice, fail.
    assert 0.50 * full < half < 0.56 * full


async def test_unavailable_snow_sensor_is_flagged_and_treated_as_no_snow(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    hass.states.async_set(SNOW, "unavailable")
    entry, _ = await _setup(hass, LOCAL_OPTIONS)
    report = _coordinator(hass, entry).local_reader.last_report["snow_depth"]
    assert report["problem"] and report["depth_m"] is None


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
async def test_diagnostics_report_the_source_and_redact_coordinates(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    entry, _ = await _setup(hass, LOCAL_OPTIONS)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    import json
    json.dumps(diag, default=str)  # HA serialises the export
    source = diag["source"]
    assert source["configured"] == SOURCE_LOCAL and source["active"] == ACTIVE_SOURCE_LOCAL
    assert source["local"]["irradiance_entity"] == IRRADIANCE
    assert source["local"]["last_read"]["irradiance"]["accepted_average"] == 120
    assert len(source["local"]["synthesis_per_array"]) == 1
    assert diag["entry"]["data"][CONF_LATITUDE] == "**REDACTED**"
    assert str(LAT) not in json.dumps(diag, default=str)


# ---------------------------------------------------------------------------
# Multi-array: each array gets its own geometry from the same snapshot
# ---------------------------------------------------------------------------
async def test_multi_array_east_west_uses_one_snapshot_and_two_geometries(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    options = {**LOCAL_OPTIONS, "azimuth": [90, 270], "modules_power": [5000, 5000],
               "declination": [30, 30]}
    entry, _ = await _setup(hass, options)
    coordinator = _coordinator(hass, entry)
    assert len(coordinator.last_local_stats) == 2
    watts = coordinator.data.watts
    morning = max(v for t, v in watts.items() if dt_util.as_local(t).hour == 9)
    assert morning > 0


async def test_local_data_error_is_a_home_assistant_error():
    from homeassistant.exceptions import HomeAssistantError
    assert issubclass(LocalDataError, HomeAssistantError)


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------
def _register_fusion(hass) -> None:
    fusion_entry = MockConfigEntry(domain="swissweather_fusion", entry_id="01FUSION")
    fusion_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for domain, suffix, object_id in (
        ("weather", "weather", "swissweather_fusion"),
        ("sensor", "solar_ghi", "swissweather_fusion_solar_irradiance_ghi_hour_average"),
        ("sensor", "snow_depth", "swissweather_fusion_snow_depth"),
        ("sensor", "srf_irradiance", "swissweather_fusion_srf_global_irradiance"),
        ("sensor", "solar_dni", "swissweather_fusion_solar_irradiance_dni_hour_average"),
    ):
        registry.async_get_or_create(
            domain, "swissweather_fusion", f"01FUSION_{suffix}",
            config_entry=fusion_entry, suggested_object_id=object_id,
        )


async def _user_step(hass, source: str):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Home", "base_url": "https://api.open-meteo.com", "inverter_power": 0,
         "max_snowcover_depth_cm": 0.0, "weather_source": source},
    )


async def test_config_flow_local_auto_discovers_the_fusion_sensors(hass, zurich, freezer):
    freezer.move_to(NOW)
    _register_fusion(hass)
    FakeFusion(hass).publish(HOUR0)
    result = await _user_step(hass, SOURCE_LOCAL)
    assert result["step_id"] == "local"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCAL_WEATHER_ENTITY: WEATHER, CONF_LOCAL_FALLBACK_OPEN_METEO: False}
    )
    assert result["step_id"] == "array"
    with patch("custom_components.open_meteo_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"modules_power": 8000}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    options = result["options"]
    assert options[CONF_WEATHER_SOURCE] == SOURCE_LOCAL
    assert options[CONF_LOCAL_IRRADIANCE_ENTITY] == IRRADIANCE
    assert options[CONF_LOCAL_SNOW_DEPTH_ENTITY] == SNOW
    assert options[CONF_LOCAL_FALLBACK_OPEN_METEO] is False


async def test_config_flow_rejects_a_weather_entity_without_hourly_forecast(hass, zurich):
    hass.states.async_set("weather.daily_only", "sunny", {"supported_features": 1})
    result = await _user_step(hass, SOURCE_LOCAL)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCAL_WEATHER_ENTITY: "weather.daily_only", CONF_LOCAL_FALLBACK_OPEN_METEO: False}
    )
    assert result["step_id"] == "local"
    assert result["errors"] == {CONF_LOCAL_WEATHER_ENTITY: "weather_no_hourly"}


async def test_config_flow_rejects_an_irradiance_sensor_without_a_series(hass, zurich):
    hass.states.async_set("weather.other", "sunny", {"supported_features": 7})
    hass.states.async_set("sensor.plain", "5", {})
    result = await _user_step(hass, SOURCE_LOCAL)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_LOCAL_WEATHER_ENTITY: "weather.other", CONF_LOCAL_IRRADIANCE_ENTITY: "sensor.plain",
         CONF_LOCAL_FALLBACK_OPEN_METEO: False},
    )
    assert result["errors"] == {CONF_LOCAL_IRRADIANCE_ENTITY: "irradiance_no_series"}


async def test_config_flow_without_fusion_and_without_sensor_says_not_found(hass, zurich):
    hass.states.async_set("weather.other", "sunny", {"supported_features": 7})
    result = await _user_step(hass, SOURCE_LOCAL)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCAL_WEATHER_ENTITY: "weather.other", CONF_LOCAL_FALLBACK_OPEN_METEO: False}
    )
    assert result["errors"] == {CONF_LOCAL_IRRADIANCE_ENTITY: "irradiance_not_found"}


async def test_config_flow_open_meteo_skips_the_local_step_and_stores_no_local_keys(hass, zurich):
    result = await _user_step(hass, SOURCE_OPEN_METEO)
    assert result["step_id"] == "array"
    with patch("custom_components.open_meteo_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"modules_power": 8000})
    assert result["options"][CONF_WEATHER_SOURCE] == SOURCE_OPEN_METEO
    assert not {CONF_LOCAL_WEATHER_ENTITY, CONF_LOCAL_IRRADIANCE_ENTITY} & set(result["options"])


async def test_options_flow_switching_back_to_open_meteo_drops_local_keys(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    entry, _ = await _setup(hass, LOCAL_OPTIONS)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"base_url": "https://api.open-meteo.com", "inverter_power": 0, "max_snowcover_depth_cm": 0.0,
         "weather_source": SOURCE_OPEN_METEO},
    )
    assert result["step_id"] == "array"
    with patch("custom_components.open_meteo_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_WEATHER_SOURCE] == SOURCE_OPEN_METEO
    assert CONF_LOCAL_IRRADIANCE_ENTITY not in result["data"]


async def test_options_flow_local_step_is_prefilled_with_the_stored_entities(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    entry, _ = await _setup(hass, LOCAL_OPTIONS)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"base_url": "https://api.open-meteo.com", "inverter_power": 0, "max_snowcover_depth_cm": 0.0,
         "weather_source": SOURCE_LOCAL},
    )
    assert result["step_id"] == "local"
    defaults = result["data_schema"]({})
    assert defaults[CONF_LOCAL_WEATHER_ENTITY] == WEATHER



# ---------------------------------------------------------------------------
# Defaults and day coverage
# ---------------------------------------------------------------------------
async def test_fallback_is_off_when_the_option_is_absent(hass, zurich, freezer):
    """The fail-safe default must not depend on the option being stored
    (mutation M22: every other test set it explicitly)."""
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0 - timedelta(hours=5))
    options = {k: v for k, v in LOCAL_OPTIONS.items() if k != CONF_LOCAL_FALLBACK_OPEN_METEO}
    entry, om = await _setup(hass, options)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    om.assert_not_called()


async def test_days_beyond_the_local_horizon_are_unknown_not_zero(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0, hours=120)  # to 2026-10-11 09:00 UTC
    entry, _ = await _setup(hass, LOCAL_OPTIONS)
    coordinator = _coordinator(hass, entry)
    today = dt_util.as_local(NOW).date()
    assert today not in coordinator.local_complete_dates  # morning not in history
    assert hass.states.get("sensor.home_energy_production_today").state == "unknown"
    assert float(hass.states.get("sensor.home_energy_production_tomorrow").state) > 0
    assert float(hass.states.get("sensor.home_energy_production_d4").state) > 0
    for key in ("d5", "d6", "d7"):  # d5 partly covered, d6/d7 not at all
        assert hass.states.get(f"sensor.home_energy_production_{key}").state == "unknown"
    # Remaining-today is a future-only quantity and stays available.
    assert float(hass.states.get("sensor.home_energy_production_today_remaining").state) > 0


async def test_today_becomes_known_once_history_covers_the_morning(hass, zurich, freezer, hass_storage):
    early = datetime(2026, 10, 6, 3, 5, tzinfo=UTC)  # 05:05 local, before sunrise
    freezer.move_to(early)
    fusion = FakeFusion(hass)
    fusion.publish(early.replace(minute=0))
    entry, _ = await _setup(hass, LOCAL_OPTIONS)
    assert float(hass.states.get("sensor.home_energy_production_today").state) > 0
    later = datetime(2026, 10, 6, 12, 5, tzinfo=UTC)
    freezer.move_to(later)
    fusion.publish(later.replace(minute=0))
    await _coordinator(hass, entry).async_refresh()
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.home_energy_production_today").state) > 0


async def test_open_meteo_mode_never_masks_day_sensors(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass)
    entry, _ = await _setup(hass, BASE_OPTIONS, om_estimate=AsyncMock(return_value=_estimate_stub()))
    assert _coordinator(hass, entry).local_complete_dates is None
    # The stub has no data for d7: 0 Wh exactly as in 0.1.33.1.
    assert float(hass.states.get("sensor.home_energy_production_d7").state) == 0


async def test_day_coverage_survives_a_restart_with_the_retained_forecast(hass, zurich, freezer, hass_storage):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    entry, _ = await _setup(hass, LOCAL_OPTIONS)
    coverage = _coordinator(hass, entry).local_complete_dates
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=70))
    await hass.async_block_till_done()
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert stored["local_complete_dates"] == sorted(d.isoformat() for d in coverage)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _coordinator(hass, entry).local_complete_dates == coverage



# ---------------------------------------------------------------------------
# Review fixes (R1, D-06, D-07) and mutation M33
# ---------------------------------------------------------------------------
async def test_fallback_clears_the_local_day_mask(hass, zurich, freezer):
    """Open-Meteo data covers all days; a local coverage mask left in place
    would hide valid values (mutation M33)."""
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.publish(HOUR0)
    stub = AsyncMock(return_value=_estimate_stub())
    entry, _ = await _setup(hass, {**LOCAL_OPTIONS, CONF_LOCAL_FALLBACK_OPEN_METEO: True}, om_estimate=stub)
    coordinator = _coordinator(hass, entry)
    assert coordinator.local_complete_dates  # local data first
    fusion.publish(HOUR0 - timedelta(hours=5))  # source stalls
    with patch(FETCH_OM, stub), patch(NETWORK, AsyncMock(side_effect=AssertionError("network"))):
        await coordinator.async_refresh()
    assert coordinator.active_source == ACTIVE_SOURCE_FALLBACK
    assert coordinator.local_complete_dates is None


@pytest.mark.timeout(20)
async def test_a_hung_weather_service_degrades_within_the_bound(hass, zurich):
    """D-06: a blocking service call that never returns must not stall the
    refresh cycle. Runs on the real clock: a frozen clock also freezes the
    asyncio timers this test is about."""
    fusion = FakeFusion(hass)
    fusion.publish(dt_util.utcnow().replace(minute=0, second=0, microsecond=0))

    release = asyncio.Event()

    async def _hang(call):
        await release.wait()

    hass.services.async_register("weather", "get_forecasts", _hang, supports_response=SupportsResponse.ONLY)
    with patch("custom_components.open_meteo_solar_forecast.local_provider.SERVICE_TIMEOUT_SECONDS", 0.05):
        entry, _ = await _setup(hass, LOCAL_OPTIONS)
    release.set()
    coordinator = _coordinator(hass, entry)
    assert coordinator.active_source == ACTIVE_SOURCE_LOCAL
    assert coordinator.local_reader.last_report["temperature"]["error"] == "TimeoutError"


async def _setup_with_retained(hass, hass_storage, extra: dict):
    entry = MockConfigEntry(domain=DOMAIN, title="Home", data={CONF_LATITUDE: LAT, CONF_LONGITUDE: LON},
                            options=LOCAL_OPTIONS)
    stub = _estimate_stub()
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 2, "minor_version": 1, "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {
            "config_fingerprint": _config_fingerprint(entry),
            "last_successful_update": dt_util.utcnow().isoformat(),
            "watts": {k.isoformat(): v for k, v in stub.watts.items()},
            "wh_period": {k.isoformat(): v for k, v in stub.wh_period.items()},
            "wh_days": {k.isoformat(): v for k, v in stub.wh_days.items()},
            "wh_period_15m": {k.isoformat(): v for k, v in stub.wh_period_15m.items()},
            "api_timezone_offset": 7200,
            **extra,
        },
    }
    entry.add_to_hass(hass)
    with patch(FETCH_OM, AsyncMock(side_effect=AssertionError)):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return _coordinator(hass, entry)


async def test_a_retained_fallback_forecast_is_not_masked_after_restart(hass, zurich, freezer, hass_storage):
    """D-07: no stored coverage means the data came from Open-Meteo."""
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    coordinator = await _setup_with_retained(hass, hass_storage, {})
    assert coordinator.active_source == ACTIVE_SOURCE_RETAINED
    assert coordinator.local_complete_dates is None
    assert float(hass.states.get("sensor.home_energy_production_tomorrow").state) == 6.0


async def test_a_retained_local_forecast_restores_its_mask_after_restart(hass, zurich, freezer, hass_storage):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    tomorrow = (dt_util.as_local(NOW) + timedelta(days=1)).date()
    coordinator = await _setup_with_retained(
        hass, hass_storage, {"local_complete_dates": [tomorrow.isoformat()]}
    )
    assert coordinator.local_complete_dates == {tomorrow}
    assert hass.states.get("sensor.home_energy_production_today").state == "unknown"
    assert float(hass.states.get("sensor.home_energy_production_tomorrow").state) == 6.0


async def test_open_meteo_mode_failure_text_and_logging_are_unchanged(hass, zurich, freezer, caplog):
    """Parity R6: message and per-failure warning exactly as in 0.1.33.1."""
    freezer.move_to(NOW)
    FakeFusion(hass)
    failing = AsyncMock(side_effect=RuntimeError("api down"))
    entry, _ = await _setup(hass, BASE_OPTIONS, om_estimate=failing)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert "Error communicating with API: api down" in caplog.text

    caplog.clear()
    entry2, _ = await _setup(hass, BASE_OPTIONS, om_estimate=AsyncMock(return_value=_estimate_stub()))
    coordinator = _coordinator(hass, entry2)
    caplog.set_level(logging.WARNING)
    with patch(FETCH_OM, failing):
        await coordinator.async_refresh()
        await coordinator.async_refresh()
    assert caplog.text.count("Unable to refresh forecast data, using retained forecast") == 2
