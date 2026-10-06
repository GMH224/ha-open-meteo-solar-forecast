"""Diagnostics support for Open-Meteo Solar Forecast integration."""

from __future__ import annotations

from typing import Any

from open_meteo_solar_forecast import Estimate

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

TO_REDACT = {
    CONF_API_KEY,
    CONF_LATITUDE,
    CONF_LONGITUDE,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: DataUpdateCoordinator[Estimate] = hass.data[DOMAIN][entry.entry_id]

    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(entry.data, TO_REDACT),
            "options": async_redact_data(entry.options, TO_REDACT),
        },
        "data": {
            "energy_production_today": coordinator.data.energy_production_today,
            "energy_production_today_remaining": coordinator.data.energy_production_today_remaining,
            "energy_production_tomorrow": coordinator.data.energy_production_tomorrow,
            "energy_current_hour": coordinator.data.energy_current_hour,
            "power_production_now": coordinator.data.power_production_now,
            "watts": {
                watt_datetime.isoformat(): watt_value
                for watt_datetime, watt_value in coordinator.data.watts.items()
            },
            "wh_days": {
                wh_datetime.isoformat(): wh_value
                for wh_datetime, wh_value in coordinator.data.wh_days.items()
            },
            "wh_period": {
                wh_datetime.isoformat(): wh_value
                for wh_datetime, wh_value in coordinator.data.wh_period.items()
            },
        },
        "account": {
            "timezone": coordinator.data.timezone,
        },
        "source": _source_diagnostics(coordinator),
    }


def _source_diagnostics(coordinator: Any) -> dict[str, Any]:
    """Which weather source produced the data, and how healthy it is."""
    last_update = getattr(coordinator, "last_successful_update", None)
    result: dict[str, Any] = {
        "configured": getattr(coordinator, "weather_source", None),
        "active": getattr(coordinator, "active_source", None),
        "fallback_to_open_meteo": getattr(coordinator, "fallback_to_open_meteo", None),
        "last_error": getattr(coordinator, "last_source_error", None),
        "last_successful_update": last_update.isoformat() if last_update else None,
        "day_sources": (
            {
                day.isoformat(): src
                for day, src in sorted(coordinator.day_sources.items())
            }
            if getattr(coordinator, "day_sources", None) is not None
            else None
        ),
        "hybrid_open_meteo": getattr(coordinator, "hybrid_status", None),
        "update_interval_seconds": (
            coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None
        ),
    }
    reader = getattr(coordinator, "local_reader", None)
    if reader is not None:
        result["local"] = {
            "weather_entity": reader.weather_entity_id,
            "irradiance_entity": reader.irradiance_entity_id,
            "snow_depth_entity": reader.snow_depth_entity_id,
            "last_read": async_redact_data(reader.last_report, TO_REDACT),
            "synthesis_per_array": getattr(coordinator, "last_local_stats", []),
        }
    return result
