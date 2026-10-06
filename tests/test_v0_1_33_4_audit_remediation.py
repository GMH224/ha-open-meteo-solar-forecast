"""Regression tests for the 0.1.33.4 remediation of the external 0.1.33.3
audit (triage: omsf_v0.1.33.3_external_audit_triage.md).

Each finding was first reproduced by a test asserting the faulty behaviour
(verification copy, 14 reproductions). The tests here assert the corrected
behaviour; each would fail against 0.1.33.3.
"""

from __future__ import annotations

import math
from datetime import timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, STATE_UNAVAILABLE
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_meteo_solar_forecast import local_source as ls
from custom_components.open_meteo_solar_forecast.const import (
    ACTIVE_SOURCE_LOCAL,
    ACTIVE_SOURCE_OPEN_METEO,
    ACTIVE_SOURCE_RETAINED,
    ACTIVE_SOURCE_STALE,
    DOMAIN,
    RETAINED_MAX_AGE_HOURS,
)
from custom_components.open_meteo_solar_forecast.coordinator import (
    _config_fingerprint,
    checkHorizonFile,
)
from custom_components.open_meteo_solar_forecast.errors import MAX_ERROR_LENGTH, sanitize_error
from custom_components.open_meteo_solar_forecast.local_provider import validate_weather_entity

from .fusion_fixtures import LAT, LON, fusion_series
from .test_hybrid_integration import HYBRID_OPTIONS
from .test_local_integration import (
    BASE_OPTIONS,
    FETCH_OM,
    HOUR0,
    LOCAL_OPTIONS,
    NETWORK,
    NOW,
    SNOW,
    FakeFusion,
    _coordinator,
    _estimate_stub,
    _setup,
)

TESTER = (
    __import__("pathlib").Path(__file__).resolve().parents[1]
    / "custom_components/open_meteo_solar_forecast/tester"
)


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture
async def zurich(hass):
    await hass.config.async_set_time_zone("Europe/Zurich")
    hass.config.latitude, hass.config.longitude = LAT, LON
    yield


def _guards(stub=None):
    return (
        patch(FETCH_OM, stub or AsyncMock(return_value=_estimate_stub())),
        patch(NETWORK, AsyncMock(side_effect=AssertionError("network"))),
    )


# ---------------------------------------------------------------------------
# OMSF-001 / OMSF-002 — service
# ---------------------------------------------------------------------------
async def _two_entries(hass):
    stub = AsyncMock(return_value=_estimate_stub())
    e1, _ = await _setup(hass, BASE_OPTIONS, om_estimate=stub)
    e2, _ = await _setup(hass, BASE_OPTIONS, om_estimate=stub)
    return e1, e2, stub


async def test_omsf001_service_with_several_entries_requires_an_entry_id(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass)
    e1, e2, stub = await _two_entries(hass)
    a, b = _guards(stub)
    with a, b:
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, "update_array_location",
                {"location_override": {"latitude": 1.0, "longitude": 2.0}}, blocking=True)
        assert e1.data[CONF_LATITUDE] == LAT and e2.data[CONF_LATITUDE] == LAT
        await hass.services.async_call(
            DOMAIN, "update_array_location",
            {"config_entry_id": e1.entry_id, "location_override": {"latitude": 1.0, "longitude": 2.0}},
            blocking=True)
        await hass.async_block_till_done()
    assert e1.data[CONF_LATITUDE] == 1.0 and e1.options[CONF_LONGITUDE] == 2.0
    assert e2.data[CONF_LATITUDE] == LAT  # the other entry is untouched


async def test_omsf001_single_entry_needs_no_id_and_defaults_to_home(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass)
    stub = AsyncMock(return_value=_estimate_stub())
    entry, _ = await _setup(hass, BASE_OPTIONS, om_estimate=stub)
    hass.config.latitude, hass.config.longitude = 46.0, 7.0
    a, b = _guards(stub)
    with a, b:
        await hass.services.async_call(DOMAIN, "update_array_location", {}, blocking=True)
        await hass.async_block_till_done()
    assert (entry.data[CONF_LATITUDE], entry.data[CONF_LONGITUDE]) == (46.0, 7.0)


async def test_omsf001_unloaded_entries_are_never_targeted(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass)
    e1, e2, stub = await _two_entries(hass)
    await hass.config_entries.async_unload(e2.entry_id)
    a, b = _guards(stub)
    with a, b:
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, "update_array_location",
                {"config_entry_id": e2.entry_id, "location_override": {"latitude": 1.0, "longitude": 2.0}},
                blocking=True)
        await hass.config_entries.async_unload(e1.entry_id)
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(DOMAIN, "update_array_location", {}, blocking=True)
    assert e1.data[CONF_LATITUDE] == LAT and e2.data[CONF_LATITUDE] == LAT


@pytest.mark.parametrize(
    "location",
    [
        {"latitude": "abc", "longitude": 7.0},
        {"latitude": 47.0, "longitude": 999},
        {"latitude": float("nan"), "longitude": 7.0},
        {"latitude": 47.0, "longitude": float("inf")},
        {"latitude": True, "longitude": 7.0},
        {"latitude": 47.0},
        {},
        "here",
    ],
)
async def test_omsf002_invalid_coordinates_are_rejected_and_not_persisted(hass, zurich, freezer, location):
    freezer.move_to(NOW)
    FakeFusion(hass)
    stub = AsyncMock(return_value=_estimate_stub())
    entry, _ = await _setup(hass, BASE_OPTIONS, om_estimate=stub)
    a, b = _guards(stub)
    with a, b, pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "update_array_location", {"location_override": location}, blocking=True)
    assert entry.data[CONF_LATITUDE] == LAT and entry.data[CONF_LONGITUDE] == LON


async def test_omsf002_location_selector_extras_are_accepted(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass)
    stub = AsyncMock(return_value=_estimate_stub())
    entry, _ = await _setup(hass, BASE_OPTIONS, om_estimate=stub)
    a, b = _guards(stub)
    with a, b:
        await hass.services.async_call(
            DOMAIN, "update_array_location",
            {"location_override": {"latitude": 47.0, "longitude": 9.0, "radius": 100}}, blocking=True)
        await hass.async_block_till_done()
    assert entry.data[CONF_LATITUDE] == 47.0


# ---------------------------------------------------------------------------
# V-1 / OMSF-004 — hybrid keeps partial local data; never returns empty
# ---------------------------------------------------------------------------
async def test_v1_hybrid_with_open_meteo_down_reports_local_power(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0, hours=45)
    entry, _ = await _setup(hass, HYBRID_OPTIONS, om_estimate=AsyncMock(side_effect=RuntimeError("down")))
    hybrid_power = float(hass.states.get("sensor.home_power_production_now").state)
    await _setup(hass, LOCAL_OPTIONS)
    local_power = float(hass.states.get("sensor.home_power_production_now_2").state)
    assert hybrid_power > 1000 and hybrid_power == local_power
    assert hass.states.get("sensor.home_energy_production_today").state == "unknown"


async def test_omsf004_no_complete_day_and_open_meteo_down_still_serves_local_data(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0, hours=20)
    entry, _ = await _setup(hass, HYBRID_OPTIONS, om_estimate=AsyncMock(side_effect=RuntimeError("down")))
    c = _coordinator(hass, entry)
    assert entry.state is ConfigEntryState.LOADED and c.data.watts
    assert c.day_sources == {} and c.active_source == ACTIVE_SOURCE_LOCAL


async def test_omsf004_an_empty_result_is_a_failure_not_a_success(hass, zurich, freezer):
    from open_meteo_solar_forecast.models import Estimate

    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0, hours=45)
    empty = Estimate(watts={}, wh_period={}, wh_days={}, wh_period_15m={},
                     api_timezone=timezone(timedelta(hours=2)))
    with patch("custom_components.open_meteo_solar_forecast.coordinator.merge_hybrid",
               return_value=(empty, {})):
        entry, _ = await _setup(hass, HYBRID_OPTIONS, om_estimate=AsyncMock(side_effect=RuntimeError("down")))
    assert entry.state is ConfigEntryState.SETUP_RETRY


# ---------------------------------------------------------------------------
# OMSF-007 / OMSF-008 — input bounds
# ---------------------------------------------------------------------------
def test_omsf007_entries_outside_the_time_window_are_rejected():
    series = fusion_series(HOUR0, 48)
    far_future = {**series[-1], "period_start": "2036-10-06T09:00:00+00:00", "period_end": "2036-10-06T10:00:00+00:00"}
    far_past = {**series[0], "period_start": "2026-10-01T09:00:00+00:00", "period_end": "2026-10-01T10:00:00+00:00"}
    records, report = ls.parse_irradiance_series(series + [far_future, far_past], NOW)
    assert len(records) == 48
    assert sum("outside the accepted time window" in r for r in report.rejected) == 2
    span = max(records) - min(records)
    assert span < ls.SERIES_WINDOW_PAST + ls.SERIES_WINDOW_FUTURE


def test_omsf007_window_edges():
    inside = ls.floor_hour(NOW + ls.SERIES_WINDOW_FUTURE)
    outside = inside + timedelta(hours=1)
    entries = [{"period_start": t.isoformat(), "ghi": 1, "dni": 1, "dhi": 1} for t in (inside, outside)]
    records, _ = ls.parse_irradiance_series(entries, NOW)
    assert list(records) == [inside]


def test_omsf008_an_oversized_series_is_rejected_as_a_whole():
    big = fusion_series(HOUR0, 1) * (ls.MAX_SERIES_ENTRIES + 1)
    records, report = ls.parse_irradiance_series(big, NOW)
    assert records == {} and report.oversize and "limit" in report.rejected[0]
    ok = [dict(e) for e in fusion_series(HOUR0, ls.MAX_SERIES_ENTRIES)]
    assert ls.parse_irradiance_series(ok)[1].oversize is False


def test_omsf008_temperature_list_is_bounded_too():
    raw = [{"datetime": (HOUR0 + timedelta(hours=h)).isoformat(), "temperature": 10} for h in range(ls.MAX_SERIES_ENTRIES + 1)]
    temps, rejected = ls.parse_temperature_forecast(raw, "°C", NOW)
    assert temps == {} and rejected == ls.MAX_SERIES_ENTRIES + 1
    far = [{"datetime": "2036-01-01T00:00:00+00:00", "temperature": 10}]
    assert ls.parse_temperature_forecast(far, "°C", NOW) == ({}, 1)


async def test_omsf008_an_oversized_series_is_a_local_data_error(hass, zurich, freezer):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.publish(HOUR0)
    big = fusion_series(HOUR0, 1) * (ls.MAX_SERIES_ENTRIES + 1)
    hass.states.async_set("sensor.swissweather_fusion_solar_irradiance_ghi_hour_average", "1", {"hourly_forecast": big})
    entry, om = await _setup(hass, LOCAL_OPTIONS)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    om.assert_not_called()


# ---------------------------------------------------------------------------
# OMSF-009 — horizon file
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "accepted"),
    [("horizon_ok.txt", True), ("horizon_endpoints.txt", False), ("horizon_messedup.txt", False),
     ("horizon_unsorted.txt", False), ("horizon_wrong_decimals.txt", False), ("horizon_wrong_delimiter.txt", False)],
)
def test_omsf009_shipped_sample_files_keep_their_classification(name, accepted):
    hm, message = checkHorizonFile(str(TESTER / name))
    assert (hm is not None) is accepted
    assert (message == "") is accepted


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        ("0\t10\n180\tinf\n360\t10\n", "non-finite"),
        ("inf\t10\n180\t5\n360\t10\n", "non-finite"),
        ("0\t10\n180\tnan\n360\t10\n", "non-finite"),
        ("0\t10\n180\t95\n360\t10\n", "Elevation"),
        ("0\t10\n", "shape"),
        ("", "shape"),
    ],
)
def test_omsf009_bad_content_gives_a_controlled_message(tmp_path, content, fragment):
    path = tmp_path / "h.txt"
    path.write_text(content)
    hm, message = checkHorizonFile(str(path))
    assert hm is None and fragment in message


def test_omsf009_io_problems_give_a_controlled_message(tmp_path):
    assert "not a regular file" in checkHorizonFile(str(tmp_path))[1]
    assert "not found" in checkHorizonFile(str(tmp_path / "missing.txt"))[1]
    big = tmp_path / "big.txt"
    big.write_text("0\t1\n" * 20000)
    # The byte cap must fire before parsing (mutation M70 escaped while the
    # row limit produced a similar message).
    assert "bytes, limit" in checkHorizonFile(str(big))[1]
    binary = tmp_path / "bin.txt"
    binary.write_bytes(b"\xff\xfe\x00\x01")
    assert "cannot be read" in checkHorizonFile(str(binary))[1]


def test_omsf009_endpoint_tolerance_is_kept_for_existing_files(tmp_path):
    """Deliberate (triage): 0.9 / 360.9 truncate to 0 / 360 as before, so no
    installation breaks on upgrade; interpolation clamps at the ends."""
    path = tmp_path / "h.txt"
    path.write_text("0.9\t10\n180\t5\n360.9\t10\n")
    hm, _ = checkHorizonFile(str(path))
    assert hm == ((0.9, 10.0), (180.0, 5.0), (360.9, 10.0))


# ---------------------------------------------------------------------------
# OMSF-012 — malformed retained store
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "mutation",
    [
        {"watts": []},
        {"wh_days": "x"},
        {"watts": {"2026-10-06T10:00:00+02:00": "abc"}},
        {"watts": {"2026-10-06T10:00:00+02:00": float("nan")}},
        {"watts": {"2026-10-06T10:00:00": 1}},
        {"api_timezone_offset": "x"},
        {"last_successful_update": "garbage"},
    ],
)
async def test_omsf012_malformed_retained_store_is_discarded(hass, zurich, freezer, hass_storage, mutation):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    entry = MockConfigEntry(domain=DOMAIN, title="Home", data={CONF_LATITUDE: LAT, CONF_LONGITUDE: LON},
                            options=LOCAL_OPTIONS)
    data = {"config_fingerprint": _config_fingerprint(entry), "last_successful_update": dt_util.utcnow().isoformat(),
            "watts": {}, "wh_period": {}, "wh_period_15m": {}, "wh_days": {}, "api_timezone_offset": 7200}
    data.update(mutation)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {"version": 2, "minor_version": 1, "key": f"{DOMAIN}.{entry.entry_id}", "data": data}
    entry.add_to_hass(hass)
    with patch(FETCH_OM, AsyncMock(side_effect=AssertionError)):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert _coordinator(hass, entry).active_source == ACTIVE_SOURCE_LOCAL


async def test_omsf012_a_non_mapping_store_root_is_discarded(hass, zurich, freezer, hass_storage):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    entry = MockConfigEntry(domain=DOMAIN, title="Home", data={CONF_LATITUDE: LAT, CONF_LONGITUDE: LON}, options=LOCAL_OPTIONS)
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {"version": 2, "minor_version": 1, "key": f"{DOMAIN}.{entry.entry_id}", "data": ["x"]}
    entry.add_to_hass(hass)
    with patch(FETCH_OM, AsyncMock(side_effect=AssertionError)):
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED


# ---------------------------------------------------------------------------
# OMSF-003 — bounded retention (6 h), all modes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["open_meteo", "local"])
async def test_omsf003_retained_forecast_expires_after_six_hours(hass, zurich, freezer, mode):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.publish(HOUR0)
    stub = AsyncMock(return_value=_estimate_stub())
    entry, _ = await _setup(hass, BASE_OPTIONS if mode == "open_meteo" else LOCAL_OPTIONS, om_estimate=stub)
    coordinator = _coordinator(hass, entry)
    failing = AsyncMock(side_effect=RuntimeError("down"))
    a, b = _guards(failing)
    with a, b:
        freezer.move_to(NOW + timedelta(hours=RETAINED_MAX_AGE_HOURS) - timedelta(minutes=1))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.active_source == ACTIVE_SOURCE_RETAINED
        assert hass.states.get("sensor.home_energy_production_tomorrow").state != STATE_UNAVAILABLE

        freezer.move_to(NOW + timedelta(hours=RETAINED_MAX_AGE_HOURS, minutes=1))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert not coordinator.last_update_success
    assert coordinator.active_source == ACTIVE_SOURCE_STALE
    assert hass.states.get("sensor.home_energy_production_tomorrow").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.home_power_production_now").state == STATE_UNAVAILABLE
    source = hass.states.get("sensor.home_forecast_source")
    assert source.state == ACTIVE_SOURCE_STALE and source.attributes["last_error"]
    if mode == "open_meteo":
        assert "down" in source.attributes["last_error"]
    else:  # local mode: the reason is the stalled series
        assert "stale" in source.attributes["last_error"]

    # Recovery
    later = NOW + timedelta(hours=RETAINED_MAX_AGE_HOURS, minutes=11)
    freezer.move_to(later)
    fusion.publish(later.replace(minute=0))
    a, b = _guards(stub)
    with a, b:
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert coordinator.last_update_success
    expected = ACTIVE_SOURCE_OPEN_METEO if mode == "open_meteo" else ACTIVE_SOURCE_LOCAL
    assert coordinator.active_source == expected
    assert hass.states.get("sensor.home_energy_production_tomorrow").state != STATE_UNAVAILABLE


async def test_omsf003_a_stored_forecast_older_than_six_hours_is_not_served_at_startup(hass, zurich, freezer, hass_storage):
    from .test_local_integration import _setup_with_retained

    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0 - timedelta(hours=8))  # local source stale too
    coordinator = await _setup_with_retained(hass, hass_storage, {})
    entry = coordinator.config_entry
    # stored last_successful_update = NOW: within the limit -> retained
    assert coordinator.active_source == ACTIVE_SOURCE_RETAINED
    await hass.config_entries.async_unload(entry.entry_id)
    freezer.move_to(NOW + timedelta(hours=7))
    with patch(FETCH_OM, AsyncMock(side_effect=AssertionError)):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


# ---------------------------------------------------------------------------
# OMSF-013 — history store read retried
# ---------------------------------------------------------------------------
async def test_omsf013_history_load_is_retried_after_a_transient_failure(hass, zurich, freezer):
    from homeassistant.helpers.storage import Store

    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    calls = {"n": 0}
    real_load = Store.async_load

    async def flaky(self):
        if self.key.endswith(".local_history"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk busy")
        return await real_load(self)

    with patch.object(Store, "async_load", flaky):
        entry, _ = await _setup(hass, LOCAL_OPTIONS)
        reader = _coordinator(hass, entry).local_reader
        assert reader._loaded is False
        await _coordinator(hass, entry).async_refresh()
    assert reader._loaded is True and calls["n"] == 2


# ---------------------------------------------------------------------------
# OMSF-015 / OMSF-016
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("features", "expected"), [("abc", "weather_no_hourly"), (None, "weather_no_hourly"),
                                                    ("7", None), (7, None), (1, "weather_no_hourly"), (True, "weather_no_hourly")])
def test_omsf015_supported_features_are_parsed_defensively(hass, features, expected):
    hass.states.async_set("weather.x", "sunny", {"supported_features": features})
    assert validate_weather_entity(hass, "weather.x") == expected


@pytest.mark.parametrize("value", ["inf", "-inf"])
def test_omsf016_non_finite_geometry_is_rejected_at_the_boundary(value):
    with pytest.raises(ValueError, match="non-finite"):
        ls._parse_geometry(value)
    assert ls._parse_geometry("nan") is None  # the tracker sentinel stays


# ---------------------------------------------------------------------------
# OMSF-018 — sanitised error text
# ---------------------------------------------------------------------------
def _client_error(url: str):
    from aiohttp import ClientResponseError, RequestInfo
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    info = RequestInfo(URL(url), "GET", CIMultiDictProxy(CIMultiDict()), URL(url))
    return ClientResponseError(info, (), status=500, message="Internal Server Error")


def test_omsf018_sanitize_removes_query_strings_and_secrets():
    err = _client_error("https://api.open-meteo.com/v1/forecast?latitude=47.5&apikey=SECRET123")
    assert "SECRET123" in str(err)  # premise
    text = sanitize_error(err)
    assert "SECRET123" not in text and "latitude=47.5" not in text
    assert "api.open-meteo.com/v1/forecast?<redacted>" in text
    assert "SECRET" not in sanitize_error("failed with apikey=SECRET token: abc")
    assert len(sanitize_error("x" * 1000)) == MAX_ERROR_LENGTH


async def test_omsf018_no_secret_reaches_the_source_sensor_or_diagnostics(hass, zurich, freezer):
    from custom_components.open_meteo_solar_forecast.diagnostics import async_get_config_entry_diagnostics

    freezer.move_to(NOW)
    FakeFusion(hass)
    stub = AsyncMock(return_value=_estimate_stub())
    entry, _ = await _setup(hass, {**BASE_OPTIONS, "api_key": "SECRET123"}, om_estimate=stub)
    coordinator = _coordinator(hass, entry)
    failing = AsyncMock(side_effect=_client_error("https://api.open-meteo.com/v1/forecast?apikey=SECRET123"))
    a, b = _guards(failing)
    with a, b:
        freezer.move_to(NOW + timedelta(minutes=31))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    attrs = hass.states.get("sensor.home_forecast_source").attributes
    assert attrs["last_error"] and "SECRET123" not in attrs["last_error"]
    import json
    assert "SECRET123" not in json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str)


# ---------------------------------------------------------------------------
# OMSF-019 — recorder
# ---------------------------------------------------------------------------
def test_omsf019_all_large_series_are_excluded_from_the_recorder():
    from custom_components.open_meteo_solar_forecast.recorder import exclude_attributes

    assert exclude_attributes(None) == {"watts", "wh_period", "wh_period_15m"}


# ---------------------------------------------------------------------------
# OMSF-005 / OMSF-006 — snow hold and data quality
# ---------------------------------------------------------------------------
async def test_omsf005_snow_depth_is_held_for_24_hours_then_assumed_zero(hass, zurich, freezer):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.publish(HOUR0)
    hass.states.async_set(SNOW, "0.05", {"unit_of_measurement": "m"})
    entry, _ = await _setup(hass, {**LOCAL_OPTIONS, "max_snowcover_depth_cm": 10.0})
    coordinator = _coordinator(hass, entry)
    later = NOW + timedelta(hours=23)
    freezer.move_to(later)
    fusion.publish(later.replace(minute=0))
    hass.states.async_set(SNOW, "unavailable")  # after publish(), which resets it
    await coordinator.async_refresh()
    snow = coordinator.local_reader.last_report["snow_depth"]
    assert snow["quality"] == "held" and snow["depth_m"] == 0.05
    much_later = NOW + timedelta(hours=25)
    freezer.move_to(much_later)
    fusion.publish(much_later.replace(minute=0))
    hass.states.async_set(SNOW, "unavailable")
    await coordinator.async_refresh()
    snow = coordinator.local_reader.last_report["snow_depth"]
    assert snow["quality"] == "unavailable_assumed_0"


async def test_omsf006_degraded_temperature_is_visible_on_the_source_sensor(hass, zurich, freezer):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.fail_service = True
    fusion.publish(HOUR0)
    await _setup(hass, LOCAL_OPTIONS)
    quality = hass.states.get("sensor.home_forecast_source").attributes["data_quality"]
    assert quality == {"temperature": "current_value_fallback", "snow_depth": "sensor"}


async def test_omsf006_healthy_inputs_report_forecast_quality(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    await _setup(hass, LOCAL_OPTIONS)
    quality = hass.states.get("sensor.home_forecast_source").attributes["data_quality"]
    assert quality["temperature"] == "forecast"


def test_finite_check_helper_rejects_bool_and_non_finite():
    from custom_components.open_meteo_solar_forecast.coordinator import _finite_number

    for bad in (True, "1", float("nan"), float("inf"), None):
        with pytest.raises(ValueError):
            _finite_number(bad)
    assert _finite_number(3) == 3 and math.isclose(_finite_number(2.5), 2.5)




async def test_happy_path_uses_the_forecast_temperatures(hass, zurich, freezer):
    """Guard for test-harness defect T-1 (0.1.33.4): the fake weather service
    returned its response keyed by the characters of the entity id, so every
    integration test since 0.1.33.2 silently ran on the temperature fallback."""
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    entry, _ = await _setup(hass, LOCAL_OPTIONS)
    report = _coordinator(hass, entry).local_reader.last_report["temperature"]
    assert report["accepted"] == report["received"] > 100
    assert "error" not in report
