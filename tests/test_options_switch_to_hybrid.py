"""Live-path check (0.1.33.5): an existing *local* entry switched to Hybrid
through the options dialog must save ``hybrid``, reload, and then serve
Open-Meteo days beyond the local horizon. This is exactly what the owner does
on the live system; no earlier test walked the full dialog from local."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.data_entry_flow import FlowResultType

from custom_components.open_meteo_solar_forecast.const import (
    ACTIVE_SOURCE_HYBRID,
    CONF_WEATHER_SOURCE,
    SOURCE_HYBRID,
)

import pytest

from .fusion_fixtures import LAT, LON
from .test_hybrid_integration import TODAY, _om_week
from .test_local_integration import (
    FETCH_OM, HOUR0, LOCAL_OPTIONS, NETWORK, NOW, FakeFusion, _coordinator, _setup,
)


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture
async def zurich(hass):
    await hass.config.async_set_time_zone("Europe/Zurich")
    hass.config.latitude, hass.config.longitude = LAT, LON
    yield


async def test_existing_local_entry_switched_to_hybrid_via_options(hass, zurich, freezer):
    freezer.move_to(NOW)
    FakeFusion(hass).publish(HOUR0)
    om = AsyncMock(return_value=_om_week())
    with patch(FETCH_OM, om), patch(NETWORK, AsyncMock(side_effect=AssertionError("network"))):
        entry, _ = await _setup(hass, LOCAL_OPTIONS)
        assert _coordinator(hass, entry).weather_source == "local"

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"base_url": "https://api.open-meteo.com", "model": "best_match",
             "inverter_power": 10000, "max_snowcover_depth_cm": 0.0,
             "weather_source": SOURCE_HYBRID},
        )
        assert result["step_id"] == "local"
        # Owner just presses Submit on the prefilled entity page ...
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {k: v for k, v in LOCAL_OPTIONS.items() if k.startswith("local_")},
        )
        assert result["step_id"] == "array"
        # ... and on the prefilled array page.
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

        assert entry.options[CONF_WEATHER_SOURCE] == SOURCE_HYBRID
        coordinator = _coordinator(hass, entry)
        assert coordinator.weather_source == SOURCE_HYBRID
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.active_source == ACTIVE_SOURCE_HYBRID
        assert om.await_count >= 1
        later = {d for d, s in coordinator.day_sources.items() if s == "open_meteo"}
        assert any(d.toordinal() - TODAY.toordinal() >= 2 for d in later), coordinator.day_sources
