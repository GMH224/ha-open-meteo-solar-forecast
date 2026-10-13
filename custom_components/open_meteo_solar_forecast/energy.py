"""Energy platform."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .runtime import array_coordinator


async def async_get_solar_forecast(
    hass: HomeAssistant, config_entry_id: str
) -> dict[str, dict[str, float | int]] | None:
    """Get solar forecast for a config entry ID (None for the learning hub)."""
    if (coordinator := array_coordinator(hass, config_entry_id)) is None or coordinator.data is None:
        return None

    return {
        "wh_hours": {
            timestamp.isoformat(): val
            for timestamp, val in coordinator.data.wh_period.items()
        }
    }
