"""Integration platform for recorder."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback

from .const import ATTR_WATTS, ATTR_WH_PERIOD, ATTR_WH_PERIOD_15M


@callback
def exclude_attributes(hass: HomeAssistant) -> set[str]:
    """Exclude potentially large attributes from being recorded in the database."""
    # 0.1.33.4 (audit OMSF-019): the 15-minute series is excluded as well.
    return {ATTR_WATTS, ATTR_WH_PERIOD, ATTR_WH_PERIOD_15M}
