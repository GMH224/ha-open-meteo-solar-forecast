"""Typed runtime ownership (0.1.34).

Every config entry owns exactly one runtime object, stored in
``ConfigEntry.runtime_data`` (Home Assistant >= 2024.4): a PV-array entry
owns its coordinator, the learning-hub entry owns the hub. Nothing is kept
in ``hass.data``; the object lives and dies with its entry, and Home
Assistant discards it on unload.

Lookups across entries (the hub finding its member coordinators, the
energy platform, the coordinator finding the hub) go through the typed
helpers below, which only return objects of *loaded* entries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_LEARNING_HUB

if TYPE_CHECKING:
    from .coordinator import OpenMeteoSolarForecastDataUpdateCoordinator
    from .learning_hub import LearningHub

type ArrayConfigEntry = ConfigEntry[OpenMeteoSolarForecastDataUpdateCoordinator]
type HubConfigEntry = ConfigEntry[LearningHub]
type SolarForecastConfigEntry = ConfigEntry[OpenMeteoSolarForecastDataUpdateCoordinator | LearningHub]


class GroupConfig(TypedDict):
    """One inverter group as stored in the hub's options."""

    name: str
    energy_entity: str
    members: list[str]


class LearningReport(TypedDict, total=False):
    """``forecast_source`` attribute ``learning`` of a member entry."""

    group: str
    state: str
    applied: bool
    note: str
    error: str
    hours_changed: int | None
    today_raw_wh: float | None
    today_corrected_wh: float | None
    tomorrow_raw_wh: float | None
    tomorrow_corrected_wh: float | None
    same_day: dict[str, Any] | None


class EntryReport(TypedDict):
    """Day totals of a member's latest refresh, held by the hub."""

    updated: str
    today_raw_wh: float | None
    today_corrected_wh: float | None
    tomorrow_raw_wh: float | None
    tomorrow_corrected_wh: float | None


def is_hub_entry(entry: ConfigEntry[Any]) -> bool:
    return entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_LEARNING_HUB


def _loaded_runtime(entry: ConfigEntry[Any] | None) -> Any:
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        return None
    return getattr(entry, "runtime_data", None)


def array_coordinator(
    hass: HomeAssistant, entry_id: str
) -> OpenMeteoSolarForecastDataUpdateCoordinator | None:
    """The coordinator of a loaded PV-array entry, else None."""
    from .coordinator import OpenMeteoSolarForecastDataUpdateCoordinator

    runtime = _loaded_runtime(hass.config_entries.async_get_entry(entry_id))
    return runtime if isinstance(runtime, OpenMeteoSolarForecastDataUpdateCoordinator) else None


def loaded_hub(hass: HomeAssistant) -> LearningHub | None:
    """The learning hub of the loaded hub entry, else None (at most one)."""
    from .learning_hub import LearningHub

    for entry in hass.config_entries.async_entries(DOMAIN):
        if is_hub_entry(entry):
            runtime = _loaded_runtime(entry)
            if isinstance(runtime, LearningHub):
                return runtime
    return None
