"""Home Assistant integration tests of the learning hub (0.1.34).

Real Home Assistant (pytest-homeassistant-custom-component), real library,
local mode with Fusion-shaped fixtures. Recorder tests use the in-memory
recorder of the test plugin.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_meteo_solar_forecast import learning as L
from custom_components.open_meteo_solar_forecast.const import (
    CONF_ENTRY_TYPE,
    CONF_GROUPS,
    CONF_LOCAL_FALLBACK_OPEN_METEO,
    CONF_LOCAL_IRRADIANCE_ENTITY,
    CONF_LOCAL_SNOW_DEPTH_ENTITY,
    CONF_LOCAL_WEATHER_ENTITY,
    CONF_STATION_ENTITY,
    CONF_WEATHER_SOURCE,
    DOMAIN,
    ENTRY_TYPE_LEARNING_HUB,
    LEARNING_HUB_UNIQUE_ID,
    LEARNING_STORAGE_KEY,
    SOURCE_HYBRID,
    SOURCE_LOCAL,
    SOURCE_OPEN_METEO,
)
from custom_components.open_meteo_solar_forecast.diagnostics import async_get_config_entry_diagnostics
from custom_components.open_meteo_solar_forecast.learning_hub import (
    get_hub,
    station_sample_from_state,
)

from .fusion_fixtures import LAT, LON, fusion_series

UTC = timezone.utc
# 06:30 local (CEST): after the sunrise capture time, before sunrise.
NOW = datetime(2026, 10, 7, 4, 30, tzinfo=UTC)
HOUR0 = NOW.replace(minute=0)
WEATHER = "weather.swissweather_fusion"
IRRADIANCE = "sensor.swissweather_fusion_solar_irradiance_ghi_hour_average"
SNOW = "sensor.swissweather_fusion_snow_depth"
STATION = "sensor.swissweather_fusion_reference_station"
ENERGY = "sensor.inverter_total_dc_input_energy"
ENERGY_2 = "sensor.inverter_total_dc_input_energy_2"

BASE = {
    "api_key": "",
    "base_url": "https://api.open-meteo.com",
    "model": "best_match",
    "inverter_power": 0,
    "max_snowcover_depth_cm": 0.0,
    "declination": 40,
    "azimuth": 197,
    "modules_power": 6000,
    "array_inverter_power": 0,
    "efficiency_factor": 0.9,
    "tracking": "none",
    "damping_morning": 0.0,
    "damping_evening": 0.0,
    "use_horizon": False,
    "partial_shading": False,
    "horizon_filepath": "/config/custom_components/open_meteo_solar_forecast/horizon.txt",
}
LOCAL = {
    **BASE,
    CONF_WEATHER_SOURCE: SOURCE_LOCAL,
    CONF_LOCAL_WEATHER_ENTITY: WEATHER,
    CONF_LOCAL_IRRADIANCE_ENTITY: IRRADIANCE,
    CONF_LOCAL_SNOW_DEPTH_ENTITY: SNOW,
    CONF_LOCAL_FALLBACK_OPEN_METEO: False,
}
FETCH_OM = (
    "custom_components.open_meteo_solar_forecast.coordinator."
    "OpenMeteoSolarForecastDataUpdateCoordinator._async_fetch_open_meteo"
)
NETWORK = "open_meteo_solar_forecast.OpenMeteoSolarForecast._request"


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture
async def zurich(hass: HomeAssistant):
    await hass.config.async_set_time_zone("Europe/Zurich")
    hass.config.latitude, hass.config.longitude = LAT, LON
    yield


def layered_forecast(first_hour: datetime, hours: int = 120, low: float = 20.0) -> list[dict]:
    return [
        {
            "datetime": (first_hour + timedelta(hours=h)).isoformat(),
            "temperature": 12.0,
            "dew_point": 8.0,
            "cloud_coverage": low,
            "cloud_coverage_low": low,
            "cloud_coverage_mid": 10.0,
            "cloud_coverage_high": 0.0,
            "condition": "partlycloudy",
        }
        for h in range(hours)
    ]


class Fake:
    """Fusion 0.3.4-shaped states, weather service, station and counters."""

    def __init__(self, hass: HomeAssistant, low: float = 20.0) -> None:
        self.hass = hass
        self.low = low
        self.layers = True
        hass.services.async_register(
            "weather", "get_forecasts", self._get_forecasts, supports_response=SupportsResponse.ONLY
        )

    async def _get_forecasts(self, call: ServiceCall):
        first = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        entities = call.data["entity_id"]
        entities = [entities] if isinstance(entities, str) else entities
        forecast = layered_forecast(first, low=self.low)
        if not self.layers:
            for item in forecast:
                for key in ("cloud_coverage_low", "cloud_coverage_mid", "cloud_coverage_high"):
                    item.pop(key)
        return {entity: {"forecast": forecast} for entity in entities}

    def publish(self, first_hour: datetime = HOUR0, hours: int = 120) -> None:
        self.hass.states.async_set(
            IRRADIANCE, "300.0",
            {"hourly_forecast": fusion_series(first_hour, hours), "unit_of_measurement": "W/m²"},
        )
        self.hass.states.async_set(
            WEATHER, "sunny", {"temperature": 12.0, "temperature_unit": "°C", "supported_features": 7}
        )
        self.hass.states.async_set(SNOW, "0.0", {"unit_of_measurement": "m"})
        for entity in (ENERGY, ENERGY_2):
            self.hass.states.async_set(
                entity, "12345.6",
                {"unit_of_measurement": "kWh", "state_class": "total_increasing", "device_class": "energy"},
            )

    def station(self, end: datetime, ghi: float = 0.0) -> None:
        self.hass.states.async_set(
            STATION, end.isoformat(),
            {"values": {"gre000z0": ghi, "ods000z0": ghi * 0.3, "sre000z0": 0, "oli000z0": 330,
                        "osr000z0": ghi * 0.1, "tre200s0": 8.7}, "age_minutes": 12.0},
        )


async def setup_array(hass, title="South", options=None):
    entry = MockConfigEntry(
        domain=DOMAIN, title=title, data={CONF_LATITUDE: LAT, CONF_LONGITUDE: LON}, options=options or LOCAL,
    )
    entry.add_to_hass(hass)
    with patch(FETCH_OM, AsyncMock(side_effect=AssertionError("Open-Meteo must not be contacted"))), patch(
        NETWORK, AsyncMock(side_effect=AssertionError("network"))
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def hub_options(groups):
    return {CONF_STATION_ENTITY: STATION, CONF_GROUPS: groups}


async def setup_hub(hass, members, energy=ENERGY):
    entry = MockConfigEntry(
        domain=DOMAIN, title="Solar forecast learning", unique_id=LEARNING_HUB_UNIQUE_ID,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_LEARNING_HUB},
        options=hub_options([{"name": "10K", "energy_entity": energy, "members": [m.entry_id for m in members]}]),
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, get_hub(hass)


async def refresh(hass, entry):
    coordinator = entry.runtime_data
    with patch(NETWORK, AsyncMock(side_effect=AssertionError("network"))):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    return coordinator


def today_kwh(hass, title="south"):
    return float(hass.states.get(f"sensor.{title}_energy_production_today").state)


async def basic(hass, freezer, low=20.0):
    freezer.move_to(NOW)
    fake = Fake(hass, low=low)
    fake.publish()
    fake.station(NOW - timedelta(minutes=10))
    array = await setup_array(hass)
    hub_entry, hub = await setup_hub(hass, [array])
    return fake, array, hub_entry, hub


def make_applied(hub, shift=-0.2):
    """Give every archived row a correction, pass the gate, switch on."""
    for buckets in hub.data.d["archive"].values():
        for row in buckets.values():
            row[L.R["shift_kt"]] = shift
    hub.data.d["gate"] = {"algorithm": L.ALGORITHM_VERSION, "passed": True, "days": 20, "reasons": [],
                          "groups": {}, "improvement": 0.2}


# ---------------------------------------------------------------------------
# Setup, entities, parity
# ---------------------------------------------------------------------------


async def test_hub_sets_up_with_entities_in_collecting_state(hass, zurich, freezer):
    _, array, hub_entry, hub = await basic(hass, freezer)
    assert hub_entry.state is ConfigEntryState.LOADED
    assert hub_entry.runtime_data is hub and DOMAIN not in hass.data  # owned by the entry
    assert hass.states.get("sensor.solar_forecast_learning_state").state == L.STATE_COLLECTING
    assert hass.states.get("switch.solar_forecast_learning_apply_learning").state == "off"
    assert hass.states.get("switch.solar_forecast_learning_freeze_learning").state == "off"
    for key in ("energy_error", "correction_today", "correction_tomorrow", "calibration"):
        assert hass.states.get(f"sensor.solar_forecast_learning_10k_{key}") is not None
    assert hub.groups == [L.GroupInfo("10K", 6.0, (array.entry_id,))]
    assert hub.weather_entity == WEATHER and hub.irradiance_entity == IRRADIANCE


async def test_member_forecast_is_unchanged_while_not_applied(hass, zurich, freezer):
    freezer.move_to(NOW)
    fake = Fake(hass)
    fake.publish()
    array = await setup_array(hass)
    before = today_kwh(hass)
    _, hub = await setup_hub(hass, [array])
    await hub.async_run_hourly(NOW)
    make_applied(hub)  # corrections exist, gate passed, but the switch is off
    hub._recompute_state(NOW)
    assert hub.state == L.STATE_ELIGIBLE
    coordinator = await refresh(hass, array)
    assert today_kwh(hass) == before
    report = coordinator.learning_report
    assert report["applied"] is False and report["state"] == L.STATE_ELIGIBLE
    assert report["today_corrected_wh"] < report["today_raw_wh"]  # shadow value is visible
    assert hass.states.get("sensor.south_forecast_source").attributes["learning"]["applied"] is False
    # the 06:30 refresh is recorded once for the energy half of the gate
    recorded = hub.data.d["entry_days"]["2026-10-07"][array.entry_id]
    assert recorded["raw"] == report["today_raw_wh"] and recorded["corr"] == report["today_corrected_wh"]


async def test_entry_without_hub_or_group_behaves_as_before(hass, zurich, freezer):
    freezer.move_to(NOW)
    fake = Fake(hass)
    fake.publish()
    array = await setup_array(hass)
    other = await setup_array(hass, "West", {**LOCAL, "azimuth": 268, "modules_power": 4000})
    coordinator = array.runtime_data
    assert coordinator.learning_report is None
    _, hub = await setup_hub(hass, [array])
    coordinator_other = await refresh(hass, other)
    assert coordinator_other.learning_report is None


async def test_applied_corrections_change_the_served_forecast(hass, zurich, freezer):
    fake, array, _, hub = await basic(hass, freezer)
    before = today_kwh(hass)
    await hub.async_run_hourly(NOW)
    make_applied(hub)
    with patch(NETWORK, AsyncMock(side_effect=AssertionError("network"))):
        await hub.async_set_apply(True)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hub.state == L.STATE_APPLIED
    coordinator = array.runtime_data
    after = today_kwh(hass)  # members are refreshed by the switch itself
    assert after < before * 0.85
    await refresh(hass, array)
    assert today_kwh(hass) == after
    assert after < before * 0.85  # kt 0.75 -> 0.55 on every hour
    assert coordinator.learning_report["applied"] is True
    assert coordinator.learning_report["today_corrected_wh"] == pytest.approx(after * 1000, rel=0.01)
    # switching off restores the raw forecast immediately (members refreshed)
    freezer.tick(timedelta(seconds=15))  # past the refresh debouncer's cooldown
    with patch(NETWORK, AsyncMock(side_effect=AssertionError("network"))):
        await hub.async_set_apply(False)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert today_kwh(hass) == before


async def test_correction_shows_per_group(hass, zurich, freezer):
    _, array, _, hub = await basic(hass, freezer)
    await hub.async_run_hourly(NOW)
    make_applied(hub)
    await refresh(hass, array)
    state = hass.states.get("sensor.solar_forecast_learning_10k_correction_today")
    assert float(state.state) < 0
    assert state.attributes["raw_kwh"] > state.attributes["corrected_kwh"]


# ---------------------------------------------------------------------------
# Hourly run
# ---------------------------------------------------------------------------


async def test_hourly_run_archives_predictors_and_explains_corrections(hass, zurich, freezer):
    _, array, _, hub = await basic(hass, freezer)
    await hub.async_run_hourly(NOW)
    assert hub.last_hourly["forecast"].endswith("06:00 forecast captured")
    keys = sorted(hub.data.d["archive"])
    assert keys and all(set(hub.data.d["archive"][k]) <= {0, 1, 2, 3} for k in keys)
    first = hub.data.d["archive"][keys[0]]
    assert 0 in first  # today's first daylight hour: lead < 6 h
    row = first[0]
    assert row[L.R["low"]] == 20.0 and row[L.R["mid"]] == 10.0 and row[L.R["temp"]] == 12.0
    assert row[L.R["shift_kt"]] == 0.0  # neutral start
    assert "2026-10-07" in hub.data.d["issue6"]
    state = hass.states.get("sensor.solar_forecast_learning_next_hours")
    hours = state.attributes["hours"]
    assert hours and hours[0]["kt"] == hours[0]["kt_corrected"]
    assert {"low_cloud", "spread_k", "similar_hours", "shrink"} <= set(hours[0])
    assert int(state.state) == 0


async def test_hourly_run_without_cloud_layers_is_recorded_not_raised(hass, zurich, freezer):
    fake, array, _, hub = await basic(hass, freezer)
    fake.layers = False
    await hub.async_run_hourly(NOW)
    assert "failed: HubInputError" in hub.last_hourly["forecast"]
    assert hub.data.d["archive"] == {}
    assert any(e[1] == "step_failed" for e in hub.data.d["events"])


async def test_station_samples_are_ingested_from_state_changes(hass, zurich, freezer):
    fake, _, _, hub = await basic(hass, freezer)
    for minutes in range(10, 70, 10):
        end = HOUR0 - timedelta(hours=1) + timedelta(minutes=minutes)
        freezer.move_to(end + timedelta(minutes=12))  # published 12 min later
        fake.station(end, ghi=100.0)
        await hass.async_block_till_done()
    # a state that arrives too late is not taken in
    fake.station(HOUR0 - timedelta(hours=3), ghi=999.0)
    await hass.async_block_till_done()
    assert hub.station_status["note"] == "older than 60 min"
    hub.data.finalize_station_hours(NOW)
    key = L.hour_key(HOUR0 - timedelta(hours=1))
    assert hub.data.d["station_hours"][key][0] == pytest.approx(100.0)
    assert hub.data.d["station_hours"][key][-1] == 6.0


def _state(state, attributes, updated=None):
    from homeassistant.core import State

    return State(STATION, state, attributes, last_updated=updated)


def test_station_time_sources():
    end = datetime(2026, 10, 7, 4, 20, tzinfo=UTC)
    values = {"gre000z0": 10.0}
    sample, source = station_sample_from_state(_state(end.isoformat(), {"values": values}), end)
    assert source == "state" and sample.end == int(end.timestamp())
    sample, source = station_sample_from_state(_state("ok", {"values": values, "measured_at": end.isoformat()}), end)
    assert source == "measured_at" and sample.end == int(end.timestamp())
    sample, source = station_sample_from_state(
        _state("ok", {"values": values, "age_minutes": 12.0}, updated=end + timedelta(minutes=12, seconds=40)), end
    )
    assert source == "age_minutes" and sample.end == int(end.timestamp())
    assert station_sample_from_state(_state(end.isoformat(), {"values": values}), end + timedelta(minutes=61)) == (
        None, "older than 60 min")
    assert station_sample_from_state(_state("unknown", {}), end)[0] is None
    assert station_sample_from_state(_state("ok", {"values": values}), end) == (None, "no measurement time")
    assert station_sample_from_state(None, end) == (None, "missing")


# ---------------------------------------------------------------------------
# Fail-safe
# ---------------------------------------------------------------------------


async def test_failing_correction_serves_the_raw_forecast(hass, zurich, freezer):
    _, array, _, hub = await basic(hass, freezer)
    before = today_kwh(hass)
    await hub.async_run_hourly(NOW)
    make_applied(hub)
    await hub.async_set_apply(True)
    with patch.object(L, "apply_corrections", side_effect=RuntimeError("boom")):
        coordinator = await refresh(hass, array)
    assert today_kwh(hass) == before
    assert coordinator.last_update_success
    assert coordinator.learning_report["note"] == "no correction available"


async def test_slow_correction_times_out_to_the_raw_forecast(hass, zurich, freezer):
    _, array, _, hub = await basic(hass, freezer)
    before = today_kwh(hass)
    await hub.async_run_hourly(NOW)
    make_applied(hub)
    await hub.async_set_apply(True)

    async def slow(*args, **kwargs):
        # The test clock is frozen: advance it while "working" so the
        # coordinator's own timeout can expire.
        for _ in range(200):
            freezer.tick(0.01)
            await asyncio.sleep(0)
        raise AssertionError("the timeout did not cancel the slow correction")

    with patch("custom_components.open_meteo_solar_forecast.coordinator.LEARNING_TIMEOUT_SECONDS", 0.05), patch.object(
        hub, "async_correct_snapshot", slow
    ):
        coordinator = await refresh(hass, array)
    assert today_kwh(hass) == before and coordinator.last_update_success
    assert coordinator.learning_report["error"].startswith("TimeoutError")


async def test_corrupt_store_starts_neutral_and_is_reported(hass, hass_storage, zurich, freezer):
    hass_storage[LEARNING_STORAGE_KEY] = {"version": 1, "minor_version": 1, "key": LEARNING_STORAGE_KEY,
                                          "data": {"schema": 1, "settings": {"apply": "yes"}}}
    _, _, _, hub = await basic(hass, freezer)
    assert hub.store_fault.startswith("malformed")
    assert hub.data.d["archive"] == {} and not hub.data.apply_switch
    assert any(e[1] == "store_corrupt" for e in hub.data.d["events"])


async def test_configuration_problem_pauses_without_touching_forecasts(hass, zurich, freezer):
    freezer.move_to(NOW)
    fake = Fake(hass)
    fake.publish()
    array = await setup_array(hass)
    _, hub = await setup_hub(hass, [array])
    hass.config_entries.async_update_entry(array, options={**LOCAL, CONF_WEATHER_SOURCE: SOURCE_OPEN_METEO})
    hub.resolve_members()
    hub._recompute_state(NOW)
    assert hub.state == L.STATE_PAUSED and "member_not_local" in hub.data.d["state"]["reason"]
    assert not hub.applies


# ---------------------------------------------------------------------------
# Persistence, switches, services, diagnostics
# ---------------------------------------------------------------------------


async def test_switches_persist_across_reload_and_freeze_moves_the_cutoff(hass, zurich, freezer):
    _, _, hub_entry, hub = await basic(hass, freezer)
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.solar_forecast_learning_apply_learning"},
                                   blocking=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.solar_forecast_learning_freeze_learning"},
                                   blocking=True)
    assert hub.data.apply_switch and hub.data.frozen
    await hass.config_entries.async_reload(hub_entry.entry_id)
    await hass.async_block_till_done()
    hub2 = get_hub(hass)
    assert hub2 is not hub and hub2.data.apply_switch and hub2.data.frozen
    assert hass.states.get("switch.solar_forecast_learning_apply_learning").state == "on"
    assert [e[1] for e in hub2.data.d["events"] if e[1] in ("apply_on", "frozen")] == ["apply_on", "frozen"]


async def test_reset_requires_confirmation_and_restarts_the_gate(hass, zurich, freezer):
    _, _, _, hub = await basic(hass, freezer)
    await hub.async_run_hourly(NOW)
    make_applied(hub)
    with pytest.raises(Exception):
        await hass.services.async_call(DOMAIN, "reset_learning", {"confirm": False, "scope": "all"}, blocking=True)
    with pytest.raises(Exception):
        await hass.services.async_call(DOMAIN, "reset_learning", {"scope": "all"}, blocking=True)
    assert hub.data.d["archive"]
    await hass.services.async_call(DOMAIN, "reset_learning", {"confirm": True, "scope": "stage1"}, blocking=True)
    assert hub.data.d["archive"] and hub.data.d["gate"] is None and hub.data.d["issue6"] == {}
    assert hub.data.d["resets"][-1][1] == "stage1"
    await hass.services.async_call(DOMAIN, "reset_learning", {"confirm": True, "scope": "all"}, blocking=True)
    assert hub.data.d["archive"] == {} and not hub.data.apply_switch
    assert any(e[1] == "reset" for e in hub.data.d["events"])


async def test_services_without_hub_are_refused(hass, zurich, freezer):
    freezer.move_to(NOW)
    fake = Fake(hass)
    fake.publish()
    await setup_array(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "export_learning", {}, blocking=True)


async def test_export_writes_the_fixed_folder(hass, zurich, freezer):
    _, _, _, hub = await basic(hass, freezer)
    await hub.async_run_hourly(NOW)
    await hass.services.async_call(DOMAIN, "export_learning", {}, blocking=True)
    base = Path(hass.config.path("open_meteo_solar_forecast", "export"))
    names = {p.name for p in base.iterdir()}
    assert {"archive_2026.csv", "station_hours_2026.csv", "days.csv", "events.csv", "settings.json",
            "forecast_0600.csv", "production_2026.csv", "snapshots"} <= names
    # retention removes only old yearly files, never the others
    (base / "archive_2020.csv").write_text("x")
    (base / "notes_2020.csv").write_text("x")
    await hass.services.async_call(DOMAIN, "export_learning", {}, blocking=True)
    assert not (base / "archive_2020.csv").exists() and (base / "notes_2020.csv").exists()
    assert (base / "forecast_0600.csv").exists()
    settings = json.loads((base / "settings.json").read_text())
    assert settings["groups"][0]["name"] == "10K" and settings["algorithm"] == L.ALGORITHM_VERSION
    assert not list(base.glob("*.tmp"))


async def test_hub_diagnostics_have_no_coordinates(hass, zurich, freezer):
    _, _, hub_entry, hub = await basic(hass, freezer)
    await hub.async_run_hourly(NOW)
    diag = await async_get_config_entry_diagnostics(hass, hub_entry)
    text = json.dumps(diag, default=str)
    assert str(LAT) not in text and str(LON) not in text
    assert diag["state"]["state"] == L.STATE_COLLECTING and diag["counts"]["archived_hours"] > 0


async def test_update_array_location_ignores_the_hub(hass, zurich, freezer):
    _, array, _, _ = await basic(hass, freezer)
    await hass.services.async_call(DOMAIN, "update_array_location",
                                   {"location_override": {"latitude": 47.0, "longitude": 8.0}}, blocking=True)
    await hass.async_block_till_done()
    assert array.data[CONF_LATITUDE] == 47.0


async def test_unloading_the_hub_saves_and_members_continue(hass, hass_storage, zurich, freezer):
    _, array, hub_entry, hub = await basic(hass, freezer)
    await hub.async_run_hourly(NOW)
    await hass.config_entries.async_unload(hub_entry.entry_id)
    await hass.async_block_till_done()
    assert get_hub(hass) is None
    assert hass_storage[LEARNING_STORAGE_KEY]["data"]["archive"]
    coordinator = await refresh(hass, array)
    assert coordinator.learning_report is None and coordinator.last_update_success


async def test_removing_the_hub_keeps_the_learning_store(hass, hass_storage, zurich, freezer):
    _, _, hub_entry, hub = await basic(hass, freezer)
    await hub.async_run_hourly(NOW)
    await hass.config_entries.async_remove(hub_entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[LEARNING_STORAGE_KEY]["data"]["archive"]


# ---------------------------------------------------------------------------
# Stage 2 model and the nightly run (recorder)
# ---------------------------------------------------------------------------


async def test_model_from_measured_irradiance_equals_the_live_model_for_the_same_input(hass, zurich, freezer):
    _, array, _, hub = await basic(hass, freezer)
    coordinator = array.runtime_data
    snapshot = await coordinator.local_reader.async_snapshot()
    coordinator.local_forecast.prepare(hass, snapshot)
    live = await coordinator.local_forecast.estimate()
    hourly = await coordinator.async_model_hours(dict(snapshot.hours), dict(snapshot.temperatures_c),
                                                 snapshot.utc_offset_seconds)
    for moment, wh in live.wh_period.items():
        assert hourly[L.hour_key(moment)] == pytest.approx(wh)


async def test_nightly_run_survives_missing_recorder(hass, zurich, freezer):
    _, _, _, hub = await basic(hass, freezer)
    await hub.async_run_nightly(NOW)
    assert hub.last_nightly["production"].startswith("failed")
    assert hub.data.d["last_nightly"] == NOW.isoformat()  # the other steps ran


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


async def test_flow_offers_the_hub_once_an_array_exists_and_validates(hass, zurich, freezer):
    freezer.move_to(NOW)
    fake = Fake(hass)
    fake.publish()
    fake.station(NOW - timedelta(minutes=10))
    array = await setup_array(hass)
    om = MockConfigEntry(domain=DOMAIN, title="OM", data={CONF_LATITUDE: LAT, CONF_LONGITUDE: LON},
                         options={**BASE, CONF_WEATHER_SOURCE: SOURCE_OPEN_METEO})
    om.add_to_hass(hass)
    om2 = await setup_array(hass, "East", {**LOCAL, CONF_WEATHER_SOURCE: SOURCE_HYBRID, "azimuth": 88})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["common", "learning_hub"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "learning_hub"})
    assert result["step_id"] == "learning_hub"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_STATION_ENTITY: IRRADIANCE})
    assert result["errors"] == {CONF_STATION_ENTITY: "station_invalid"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_STATION_ENTITY: STATION})
    assert result["step_id"] == "hub_group"
    bad = {"name": "10K", "energy_entity": IRRADIANCE, "members": [om.entry_id]}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], bad)
    assert result["errors"] == {"energy_entity": "energy_invalid", "members": "member_not_local"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": " ", "energy_entity": ENERGY, "members": []})
    assert result["errors"] == {"name": "group_name_invalid", "members": "members_required"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "10K", "energy_entity": ENERGY, "members": [array.entry_id], "add_another": True})
    assert result["step_id"] == "hub_group"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "10k", "energy_entity": ENERGY_2, "members": [array.entry_id]})
    assert result["errors"] == {"name": "group_name_duplicate", "members": "member_twice"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "5K", "energy_entity": ENERGY_2, "members": [om2.entry_id]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ENTRY_TYPE: ENTRY_TYPE_LEARNING_HUB}
    assert result["options"][CONF_GROUPS] == [
        {"name": "10K", "energy_entity": ENERGY, "members": [array.entry_id]},
        {"name": "5K", "energy_entity": ENERGY_2, "members": [om2.entry_id]},
    ]
    await hass.async_block_till_done()
    # a second hub is impossible; the menu is no longer offered
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"


async def test_hub_options_flow_walks_the_stored_groups(hass, zurich, freezer):
    _, array, hub_entry, _ = await basic(hass, freezer)
    result = await hass.config_entries.options.async_init(hub_entry.entry_id)
    assert result["step_id"] == "hub_station"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_STATION_ENTITY: STATION})
    assert result["step_id"] == "hub_group"
    schema_defaults = {str(k): k.default() for k in result["data_schema"].schema if hasattr(k, "default") and callable(k.default)}
    assert schema_defaults["name"] == "10K" and schema_defaults["members"] == [array.entry_id]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Big", "energy_entity": ENERGY, "members": [array.entry_id]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert hub_entry.options[CONF_GROUPS][0]["name"] == "Big"
    assert get_hub(hass).groups[0].name == "Big"


async def test_array_flow_unchanged_without_existing_entries(hass, zurich):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"


async def test_scheduled_runs_fire(hass, zurich, freezer):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    _, _, _, hub = await basic(hass, freezer)
    with patch.object(hub, "async_run_nightly", AsyncMock()) as nightly, patch.object(
        hub, "_async_backfill_station", AsyncMock(return_value="0 samples")
    ):
        freezer.tick(timedelta(seconds=61))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)  # jobs are entry background tasks
        assert "hours archived" in hub.last_hourly["forecast"]
        nightly.assert_awaited_once()  # never ran before: due at startup
    hub.last_hourly = {}
    freezer.move_to(datetime(2026, 10, 7, 5, 25, tzinfo=UTC))  # xx:25
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)  # jobs are entry background tasks
    assert "forecast" in hub.last_hourly
    with patch.object(hub, "async_run_nightly", AsyncMock()) as nightly:
        freezer.move_to(datetime(2026, 10, 8, 1, 30, tzinfo=UTC))  # 03:30 local
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)  # jobs are entry background tasks
        nightly.assert_awaited_once()


async def test_example_dashboard_matches_the_entities(hass, zurich, freezer):
    import yaml
    from homeassistant.helpers.template import Template

    freezer.move_to(NOW)
    fake = Fake(hass)
    fake.publish()
    fake.station(NOW - timedelta(minutes=10))
    south = await setup_array(hass)
    east = await setup_array(hass, "East", {**LOCAL, "azimuth": 88, "modules_power": 7000})
    entry = MockConfigEntry(
        domain=DOMAIN, title="Solar forecast learning", unique_id=LEARNING_HUB_UNIQUE_ID,
        data={CONF_ENTRY_TYPE: ENTRY_TYPE_LEARNING_HUB},
        options=hub_options([
            {"name": "10K", "energy_entity": ENERGY, "members": [south.entry_id]},
            {"name": "5K", "energy_entity": ENERGY_2, "members": [east.entry_id]},
        ]),
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await get_hub(hass).async_run_hourly(NOW)
    path = Path(__file__).resolve().parents[1] / "examples" / "dashboard" / "learning.yaml"
    text = path.read_text()
    dashboard = yaml.safe_load(text)
    import re

    referenced = set(re.findall(r"(?:sensor|switch)\.solar_forecast_learning_[a-z0-9_]+", text))
    assert len(referenced) >= 15
    missing = [entity for entity in referenced if hass.states.get(entity) is None]
    assert missing == []
    markdown = next(c for c in dashboard["views"][0]["cards"] if c["type"] == "markdown")
    rendered = Template(markdown["content"], hass).async_render(parse_result=False)
    assert "Shadow only" in rendered and rendered.count("\n|") >= 3


async def test_incomplete_days_are_not_recorded_for_the_gate(hass, zurich, freezer):
    _, array, _, hub = await basic(hass, freezer)
    hub.data.d["entry_days"].clear()
    from datetime import date as _date

    today = NOW.astimezone(hub.tz).date()
    hub.report_entry("x", {today: 5000.0}, {today: 4000.0}, {today + timedelta(days=1)}, NOW)
    assert hub.data.d["entry_days"] == {} and hub.entry_reports["x"]["today_raw_wh"] is None
    hub.report_entry("x", {today: 5000.0}, {today: 4000.0}, {today}, NOW)
    assert hub.data.d["entry_days"]["2026-10-07"]["x"]["raw"] == 5000.0
    assert isinstance(today, _date)


async def test_owner_actions_wait_for_a_running_job(hass, zurich, freezer):
    _, _, _, hub = await basic(hass, freezer)
    await hub._lock.acquire()  # a nightly run in progress
    task = hass.async_create_task(hub.async_set_apply(True))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not task.done() and not hub.data.apply_switch
    hub._lock.release()
    await task
    assert hub.data.apply_switch


async def test_heavy_work_runs_outside_the_event_loop(hass, zurich, freezer):
    import threading

    _, _, _, hub = await basic(hass, freezer)
    loop_thread = threading.current_thread()
    seen = {}
    real_export, real_corr = L.export_archive_csv, L.compute_corrections

    def export(*args):
        seen["export"] = threading.current_thread()
        return real_export(*args)

    def corr(*args):
        seen["corrections"] = threading.current_thread()
        return real_corr(*args)

    with patch.object(L, "export_archive_csv", export), patch.object(L, "compute_corrections", corr):
        await hub.async_run_hourly(NOW)
        await hub.async_export(NOW)
    assert seen["export"] is not loop_thread and seen["corrections"] is not loop_thread


async def test_platform_concurrency_and_runtime_ownership(hass, zurich, freezer):
    from custom_components.open_meteo_solar_forecast import sensor, switch
    from custom_components.open_meteo_solar_forecast.coordinator import (
        OpenMeteoSolarForecastDataUpdateCoordinator,
    )
    from custom_components.open_meteo_solar_forecast.energy import async_get_solar_forecast

    assert sensor.PARALLEL_UPDATES == 0 and switch.PARALLEL_UPDATES == 1
    _, array, hub_entry, hub = await basic(hass, freezer)
    assert isinstance(array.runtime_data, OpenMeteoSolarForecastDataUpdateCoordinator)
    assert array.runtime_data.config_entry is array
    assert hub_entry.runtime_data is hub
    assert await async_get_solar_forecast(hass, hub_entry.entry_id) is None
    assert (await async_get_solar_forecast(hass, array.entry_id))["wh_hours"]
    await hass.config_entries.async_unload(array.entry_id)
    await hass.async_block_till_done()
    assert await async_get_solar_forecast(hass, array.entry_id) is None  # unloaded: not served


async def test_scheduled_jobs_are_owned_by_the_hub_entry(hass, zurich, freezer):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    _, _, hub_entry, hub = await basic(hass, freezer)
    started = asyncio.Event()

    async def slow_run(now=None):
        started.set()
        await asyncio.sleep(3600)

    with patch.object(hub, "async_run_hourly", slow_run):
        freezer.move_to(datetime(2026, 10, 7, 5, 25, tzinfo=UTC))
        async_fire_time_changed(hass)
        await asyncio.wait_for(started.wait(), 5)
        tasks = [t for t in hub_entry._background_tasks if not t.done()]
        assert tasks  # tracked by the entry
        await hass.config_entries.async_unload(hub_entry.entry_id)
        await hass.async_block_till_done()
        assert all(t.done() for t in tasks)  # cancelled with the entry


async def test_frozen_learning_keeps_the_calibration(hass, zurich, freezer):
    _, _, _, hub = await basic(hass, freezer)
    await hub.async_set_frozen(True)
    marker = {"computed": "frozen-marker", "algorithm": L.ALGORITHM_VERSION, "groups": {}}
    hub.data.d["stage2"] = dict(marker)
    await hub.async_run_nightly(NOW)
    assert hub.last_nightly["stage2"] == "frozen"
    assert hub.data.d["stage2"] == marker and hub.data.d["stage2_history"] == []
    await hub.async_set_frozen(False)
    await hub.async_run_nightly(NOW + timedelta(days=1))
    assert hub.data.d["stage2"]["computed"] != "frozen-marker"
