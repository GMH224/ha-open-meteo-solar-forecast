"""Home Assistant integration tests for hybrid mode (0.1.33.3).

Hybrid = whole local days where the local data covers them, Open-Meteo for
every other day (joined at local midnight). Reuses the harness of
``test_local_integration``: sockets disabled, the library's network method
guarded, the coordinator's Open-Meteo fetch replaced by a recorder.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from open_meteo_solar_forecast import Estimate

from custom_components.open_meteo_solar_forecast.const import (
    ACTIVE_SOURCE_FALLBACK,
    ACTIVE_SOURCE_HYBRID,
    ACTIVE_SOURCE_LOCAL,
    ACTIVE_SOURCE_RETAINED,
    CONF_LOCAL_FALLBACK_OPEN_METEO,
    CONF_LOCAL_IRRADIANCE_ENTITY,
    CONF_LOCAL_WEATHER_ENTITY,
    CONF_WEATHER_SOURCE,
    DOMAIN,
    SOURCE_HYBRID,
)

from .fusion_fixtures import LAT, LON
from .test_local_integration import (
    BASE_OPTIONS,
    FETCH_OM,
    HOUR0,
    LOCAL_OPTIONS,
    NETWORK,
    NOW,
    WEATHER,
    FakeFusion,
    _coordinator,
    _register_fusion,
    _setup,
    _user_step,
)

ROOT = Path(__file__).resolve().parents[1]
CEST = timezone(timedelta(hours=2))
HYBRID_OPTIONS = {**LOCAL_OPTIONS, CONF_WEATHER_SOURCE: SOURCE_HYBRID}
TODAY = date(2026, 10, 6)
OM_HOURLY_WH = 500  # Open-Meteo stub: 500 Wh per daylight hour (08-17)


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture
async def zurich(hass):
    await hass.config.async_set_time_zone("Europe/Zurich")
    hass.config.latitude, hass.config.longitude = LAT, LON
    yield


def _om_week() -> Estimate:
    """Open-Meteo-shaped estimate: today and the next 7 days, full hours."""
    watts, wh_period, wh_15m = {}, {}, {}
    for d in range(8):
        day = TODAY + timedelta(days=d)
        for h in range(24):
            start = datetime(day.year, day.month, day.day, h, tzinfo=CEST)
            value = OM_HOURLY_WH if 8 <= h < 18 else 0
            wh_period[start] = value
            for q in range(4):
                watts[start + timedelta(minutes=15 * q)] = value
                wh_15m[start + timedelta(minutes=15 * q)] = value / 4
    wh_days = {}
    for start, value in wh_period.items():
        wh_days[start.date()] = wh_days.get(start.date(), 0) + value
    return Estimate(watts=watts, wh_period=wh_period, wh_days=wh_days,
                    wh_period_15m=wh_15m, api_timezone=CEST)


OM_DAY_KWH = 10 * OM_HOURLY_WH / 1000  # 5.0 kWh per Open-Meteo day


def _state(hass, key):
    return hass.states.get(f"sensor.home_energy_production_{key}")


# ---------------------------------------------------------------------------
# The owner's case: Fusion covers ~45 h
# ---------------------------------------------------------------------------
async def test_short_local_horizon_is_completed_by_open_meteo_day_by_day(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0, hours=45)
    om = AsyncMock(return_value=_om_week())
    entry, _ = await _setup(hass, HYBRID_OPTIONS, om_estimate=om)
    coordinator = _coordinator(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert om.await_count == 1
    assert coordinator.active_source == ACTIVE_SOURCE_HYBRID
    assert coordinator.update_interval == timedelta(minutes=10)
    tomorrow = TODAY + timedelta(days=1)
    assert coordinator.day_sources[tomorrow] == "local"
    # Today has no local history yet -> Open-Meteo; day 2..7 -> Open-Meteo.
    assert coordinator.day_sources[TODAY] == "open_meteo"
    for d in range(2, 8):
        assert coordinator.day_sources[TODAY + timedelta(days=d)] == "open_meteo"

    for key in ("today", "d2", "d3", "d4", "d5", "d6", "d7"):
        state = _state(hass, key)
        assert float(state.state) == pytest.approx(OM_DAY_KWH), key
        assert state.attributes["source"] == "open_meteo"
    tomorrow_state = _state(hass, "tomorrow")
    assert tomorrow_state.attributes["source"] == "local"
    assert float(tomorrow_state.state) > 20  # local physics, 10 kWp south
    assert hass.states.get("sensor.home_forecast_source").state == ACTIVE_SOURCE_HYBRID


async def test_long_local_horizon_uses_open_meteo_only_beyond_it(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0, hours=120)  # to 2026-10-11 09:00 UTC
    entry, _ = await _setup(hass, HYBRID_OPTIONS, om_estimate=AsyncMock(return_value=_om_week()))
    sources = _coordinator(hass, entry).day_sources
    for d in (1, 2, 3, 4):
        assert sources[TODAY + timedelta(days=d)] == "local"
    for d in (0, 5, 6, 7):  # today: no history; d5 only partly local
        assert sources[TODAY + timedelta(days=d)] == "open_meteo"


async def test_the_seam_is_at_midnight_with_no_day_split(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0, hours=45)
    entry, _ = await _setup(hass, HYBRID_OPTIONS, om_estimate=AsyncMock(return_value=_om_week()))
    data = _coordinator(hass, entry).data
    tomorrow = TODAY + timedelta(days=1)
    local_hours = [m for m in data.wh_period if m.date() == tomorrow]
    assert len(local_hours) == 24
    # No Open-Meteo value (exactly 500 or 0 on the hour grid) leaks into the
    # local day's daylight hours, and the day after is pure Open-Meteo.
    assert any(v not in (0, OM_HOURLY_WH) for m, v in data.wh_period.items() if m.date() == tomorrow)
    day2 = TODAY + timedelta(days=2)
    assert {v for m, v in data.wh_period.items() if m.date() == day2} == {0, OM_HOURLY_WH}
    # Power is zero on both sides of each midnight seam.
    for seam in (datetime(2026, 10, 7, 0, 0, tzinfo=CEST), datetime(2026, 10, 8, 0, 0, tzinfo=CEST)):
        assert data.watts[seam] == 0
        assert data.watts[seam - timedelta(minutes=15)] == 0


# ---------------------------------------------------------------------------
# Open-Meteo part: cadence, cache, failure
# ---------------------------------------------------------------------------
async def test_open_meteo_is_refreshed_every_30_minutes_not_every_cycle(hass, zurich, freezer):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.publish(HOUR0, hours=45)
    om = AsyncMock(return_value=_om_week())
    entry, _ = await _setup(hass, HYBRID_OPTIONS, om_estimate=om)
    coordinator = _coordinator(hass, entry)
    with patch(FETCH_OM, om), patch(NETWORK, AsyncMock(side_effect=AssertionError("network"))):
        freezer.move_to(NOW + timedelta(minutes=10))
        await coordinator.async_refresh()
        freezer.move_to(NOW + timedelta(minutes=20))
        await coordinator.async_refresh()
        assert om.await_count == 1
        assert coordinator.hybrid_status["open_meteo"] == "cached"
        freezer.move_to(NOW + timedelta(minutes=31))
        await coordinator.async_refresh()
    assert om.await_count == 2
    assert coordinator.hybrid_status["open_meteo"] == "fresh"


async def test_open_meteo_down_keeps_the_local_days_and_marks_the_rest_unknown(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0, hours=45)
    failing = AsyncMock(side_effect=RuntimeError("api down"))
    entry, _ = await _setup(hass, HYBRID_OPTIONS, om_estimate=failing)
    coordinator = _coordinator(hass, entry)
    assert entry.state is ConfigEntryState.LOADED  # local days carry the forecast
    assert coordinator.active_source == ACTIVE_SOURCE_LOCAL
    status = coordinator.hybrid_status
    assert status["open_meteo"] == "unavailable" and status["error"] == "api down"
    assert status["fetched_at"] is None and status["open_meteo_days"] == []
    assert float(_state(hass, "tomorrow").state) > 20
    for key in ("today", "d2", "d7"):
        assert _state(hass, key).state == "unknown"
    attrs = hass.states.get("sensor.home_forecast_source").attributes
    assert attrs["hybrid_open_meteo"]["open_meteo"] == "unavailable"


async def test_a_failed_open_meteo_refresh_uses_the_cache_for_three_hours(hass, zurich, freezer):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.publish(HOUR0, hours=45)
    entry, _ = await _setup(hass, HYBRID_OPTIONS, om_estimate=AsyncMock(return_value=_om_week()))
    coordinator = _coordinator(hass, entry)
    failing = AsyncMock(side_effect=RuntimeError("api down"))
    with patch(FETCH_OM, failing), patch(NETWORK, AsyncMock(side_effect=AssertionError("network"))):
        later = NOW + timedelta(hours=2)
        freezer.move_to(later)
        fusion.publish(later.replace(minute=0), hours=43)
        await coordinator.async_refresh()
        assert coordinator.hybrid_status["open_meteo"] == "stale_cache"
        assert coordinator.day_sources[TODAY + timedelta(days=3)] == "open_meteo"
        much_later = NOW + timedelta(hours=3, minutes=5)
        freezer.move_to(much_later)
        fusion.publish(much_later.replace(minute=0), hours=42)
        await coordinator.async_refresh()
    assert coordinator.hybrid_status["open_meteo"] == "unavailable"
    assert TODAY + timedelta(days=3) not in coordinator.day_sources


# ---------------------------------------------------------------------------
# Local part failing in hybrid mode: same rules as local mode
# ---------------------------------------------------------------------------
async def test_stale_local_data_in_hybrid_mode_does_not_silently_become_open_meteo(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0 - timedelta(hours=5))
    om = AsyncMock(return_value=_om_week())
    entry, _ = await _setup(hass, HYBRID_OPTIONS, om_estimate=om)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    om.assert_not_called()


async def test_stale_local_data_in_hybrid_mode_with_fallback_is_full_open_meteo(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0 - timedelta(hours=5))
    entry, _ = await _setup(
        hass, {**HYBRID_OPTIONS, CONF_LOCAL_FALLBACK_OPEN_METEO: True},
        om_estimate=AsyncMock(return_value=_om_week()),
    )
    coordinator = _coordinator(hass, entry)
    assert coordinator.active_source == ACTIVE_SOURCE_FALLBACK
    assert coordinator.day_sources is None and coordinator.local_complete_dates is None


async def test_hybrid_retains_and_recovers_like_local_mode(hass, zurich, freezer):
    freezer.move_to(NOW)
    fusion = FakeFusion(hass)
    fusion.publish(HOUR0, hours=45)
    om = AsyncMock(return_value=_om_week())
    entry, _ = await _setup(hass, HYBRID_OPTIONS, om_estimate=om)
    coordinator = _coordinator(hass, entry)
    with patch(FETCH_OM, om), patch(NETWORK, AsyncMock(side_effect=AssertionError("network"))):
        freezer.move_to(NOW + timedelta(hours=3))
        await coordinator.async_refresh()
        assert coordinator.active_source == ACTIVE_SOURCE_RETAINED
        fusion.publish(HOUR0 + timedelta(hours=3), hours=45)
        await coordinator.async_refresh()
    assert coordinator.active_source == ACTIVE_SOURCE_HYBRID


async def test_day_sources_survive_a_restart(hass, zurich, freezer, hass_storage):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0, hours=45)
    om = AsyncMock(return_value=_om_week())
    entry, _ = await _setup(hass, HYBRID_OPTIONS, om_estimate=om)
    sources = dict(_coordinator(hass, entry).day_sources)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=70))
    await hass.async_block_till_done()
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert stored["day_sources"] == {d.isoformat(): s for d, s in sorted(sources.items())}
    with patch(FETCH_OM, om), patch(NETWORK, AsyncMock(side_effect=AssertionError("network"))):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = _coordinator(hass, entry)
    assert coordinator.day_sources == sources
    assert coordinator.active_source == ACTIVE_SOURCE_RETAINED


async def test_a_retained_forecast_from_0_1_33_2_is_read_as_local_days(hass, zurich, freezer, hass_storage):
    """Upgrade path: 0.1.33.2 stored only local_complete_dates."""
    from .test_local_integration import _setup_with_retained

    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    tomorrow = TODAY + timedelta(days=1)
    coordinator = await _setup_with_retained(hass, hass_storage, {"local_complete_dates": [tomorrow.isoformat()]})
    assert coordinator.day_sources == {tomorrow: "local"}


# ---------------------------------------------------------------------------
# Parity and provenance in the other modes
# ---------------------------------------------------------------------------
async def test_local_mode_marks_its_days_local_and_never_calls_open_meteo(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0, hours=45)
    entry, om = await _setup(hass, LOCAL_OPTIONS)
    om.assert_not_called()
    assert _state(hass, "tomorrow").attributes["source"] == "local"
    assert _state(hass, "d2").state == "unknown"
    assert _coordinator(hass, entry).hybrid_status is None


async def test_open_meteo_mode_day_sensors_have_no_source_attribute(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass)
    entry, _ = await _setup(hass, BASE_OPTIONS, om_estimate=AsyncMock(return_value=_om_week()))
    assert "source" not in _state(hass, "tomorrow").attributes
    assert _coordinator(hass, entry).day_sources is None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
async def test_config_flow_hybrid_asks_for_the_local_entities(hass, zurich, freezer):
    freezer.move_to(NOW)
    _register_fusion(hass)
    FakeFusion(hass).publish(HOUR0)
    result = await _user_step(hass, SOURCE_HYBRID)
    assert result["step_id"] == "local"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCAL_WEATHER_ENTITY: WEATHER, CONF_LOCAL_FALLBACK_OPEN_METEO: False}
    )
    with patch("custom_components.open_meteo_solar_forecast.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"modules_power": 8000})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_WEATHER_SOURCE] == SOURCE_HYBRID
    assert result["options"][CONF_LOCAL_IRRADIANCE_ENTITY]


async def test_diagnostics_show_the_day_sources_and_open_meteo_state(hass, zurich, freezer):
    from custom_components.open_meteo_solar_forecast.diagnostics import async_get_config_entry_diagnostics

    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0, hours=45)
    entry, _ = await _setup(hass, HYBRID_OPTIONS, om_estimate=AsyncMock(return_value=_om_week()))
    diag = await async_get_config_entry_diagnostics(hass, entry)
    json.dumps(diag, default=str)
    assert diag["source"]["day_sources"]["2026-10-07"] == "local"
    assert diag["source"]["day_sources"]["2026-10-13"] == "open_meteo"
    assert diag["source"]["hybrid_open_meteo"]["open_meteo"] == "fresh"
    assert "2026-10-08" in diag["source"]["hybrid_open_meteo"]["open_meteo_days"]


# ---------------------------------------------------------------------------
# Release process (owner requirement, audit backlog item 7 of 0.1.33.2)
# ---------------------------------------------------------------------------
def test_hacs_installs_from_the_tag_without_a_release_zip():
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert "zip_release" not in hacs and "filename" not in hacs
    assert not (ROOT / ".github/workflows/release.yml").exists()
