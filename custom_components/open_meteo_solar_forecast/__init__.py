"""The Open-Meteo Solar Forecast integration."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_AZIMUTH,
    CONF_DECLINATION,
    CONF_EFFICIENCY_FACTOR,
    CONF_HORIZON_FILEPATH,
    CONF_MODULES_POWER,
    CONF_PARTIAL_SHADING,
    CONF_TRACKING,
    CONF_USE_HORIZON,
    CONF_MAX_SNOWCOVER_DEPTH_CM,
    DOMAIN,
)
from .coordinator import (
    STORAGE_VERSION,
    OpenMeteoSolarForecastDataUpdateCoordinator,
    checkHorizonFile,
    storage_key,
)
from .local_provider import HISTORY_STORAGE_VERSION, history_storage_key

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_UPDATE_ARRAY_LOCATION = "update_array_location"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_LOCATION_OVERRIDE = "location_override"


def _finite_in_range(low: float, high: float):
    """Validator: a finite number in [low, high]; bool and text rejected."""

    def validator(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise vol.Invalid(f"expected a number, got {value!r}")
        number = float(value)
        if not math.isfinite(number) or not low <= number <= high:
            raise vol.Invalid(f"{number} is outside [{low}, {high}]")
        return number

    return validator


# 0.1.33.4 (external audit OMSF-002): server-side schema. The location
# selector also sends e.g. "radius", hence ALLOW_EXTRA on the inner mapping.
SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_LOCATION_OVERRIDE): vol.Schema(
            {
                vol.Required(CONF_LATITUDE): _finite_in_range(-90.0, 90.0),
                vol.Required(CONF_LONGITUDE): _finite_in_range(-180.0, 180.0),
            },
            extra=vol.ALLOW_EXTRA,
        ),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration-wide service once.

    0.1.33.4 (external audit OMSF-001): the service used to be registered by
    every config entry, so the last-loaded entry silently received every call
    and the handler outlived unloaded entries. It is now registered once and
    resolves its target explicitly.
    """

    async def async_update_array_location(call: ServiceCall) -> None:
        loaded = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        if entry_id:
            entry = next((e for e in loaded if e.entry_id == entry_id), None)
            if entry is None:
                raise ServiceValidationError(
                    f"No loaded {DOMAIN} entry with id {entry_id}"
                )
        elif len(loaded) == 1:
            entry = loaded[0]
        elif not loaded:
            raise ServiceValidationError(f"No loaded {DOMAIN} entry")
        else:
            raise ServiceValidationError(
                f"{len(loaded)} {DOMAIN} entries are loaded; "
                f"set '{ATTR_CONFIG_ENTRY_ID}' to choose one"
            )

        # Optional location override defaults to the Home Assistant location.
        location = call.data.get(ATTR_LOCATION_OVERRIDE) or {
            CONF_LATITUDE: hass.config.latitude,
            CONF_LONGITUDE: hass.config.longitude,
        }
        latitude, longitude = location[CONF_LATITUDE], location[CONF_LONGITUDE]
        # Updating the config entry reloads it, which updates the coordinator.
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_LATITUDE: latitude, CONF_LONGITUDE: longitude},
            options={**entry.options, CONF_LATITUDE: latitude, CONF_LONGITUDE: longitude},
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_ARRAY_LOCATION,
        async_update_array_location,
        schema=SERVICE_SCHEMA,
    )
    return True


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _entry_value(entry: ConfigEntry, key: str) -> Any:
    """Get config value from options with fallback to entry data."""
    return entry.options.get(key, entry.data.get(key))


def _resolve_array_count(entry: ConfigEntry) -> int:
    """Determine number of arrays from all array-capable values."""
    candidates = (
        _entry_value(entry, CONF_LATITUDE),
        _entry_value(entry, CONF_LONGITUDE),
        entry.options.get(CONF_DECLINATION),
        entry.options.get(CONF_AZIMUTH),
        entry.options.get(CONF_MODULES_POWER),
        entry.options.get(CONF_EFFICIENCY_FACTOR, 1.0),
        entry.options.get(CONF_TRACKING, "none"),
        entry.options.get(CONF_USE_HORIZON, False),
        entry.options.get(CONF_PARTIAL_SHADING, False),
        entry.options.get(CONF_HORIZON_FILEPATH),
        entry.options.get(CONF_MAX_SNOWCOVER_DEPTH_CM),
    )
    lengths = [len(value) for value in candidates if _is_sequence(value)]
    if not lengths:
        return 1

    array_count = max(lengths)
    for length in lengths:
        if length not in (1, array_count):
            raise ValueError(
                "Multi-array configuration has inconsistent list lengths "
                f"({length} vs {array_count})."
            )

    return array_count


def _normalize_option(value: Any, array_count: int, default: Any) -> list[Any]:
    """Normalize scalar or sequence option to list with array_count items."""
    if value is None:
        value = default

    if _is_sequence(value):
        normalized = list(value)
        if len(normalized) == 1 and array_count > 1:
            return normalized * array_count
        if len(normalized) != array_count:
            raise ValueError(
                "Multi-array configuration has inconsistent list lengths "
                f"({len(normalized)} vs {array_count})."
            )
        return normalized

    return [value] * array_count


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Solar Forecast from a config entry."""
    default_horizon_map: tuple[tuple[float, float], ...] = ((0.0, 0.0), (360.0, 0.0))
    default_horizon_path = "/config/custom_components/open_meteo_solar_forecast/horizon.txt"

    array_count = _resolve_array_count(entry)
    use_horizon_values = _normalize_option(
        entry.options.get(CONF_USE_HORIZON, False),
        array_count,
        default=False,
    )
    horizon_paths = _normalize_option(
        entry.options.get(CONF_HORIZON_FILEPATH, default_horizon_path),
        array_count,
        default=default_horizon_path,
    )

    checked_horizon_by_path: dict[str, tuple[tuple[float, float], ...]] = {}
    horizon_maps: list[tuple[tuple[float, float], ...]] = []

    for use_horizon, horizon_path in zip(use_horizon_values, horizon_paths, strict=True):
        if not use_horizon:
            horizon_maps.append(default_horizon_map)
            continue

        if horizon_path not in checked_horizon_by_path:
            checked_map, message = await hass.async_add_executor_job(
                checkHorizonFile, horizon_path
            )
            if checked_map is None:
                raise ValueError(message)
            checked_horizon_by_path[horizon_path] = checked_map

        horizon_maps.append(checked_horizon_by_path[horizon_path])

    horizon_map: tuple[tuple[float, float], ...] | list[tuple[tuple[float, float], ...]]
    if array_count == 1:
        horizon_map = horizon_maps[0]
    else:
        horizon_map = horizon_maps

    coordinator = OpenMeteoSolarForecastDataUpdateCoordinator(
        hass, entry, horizon_map
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the retained forecast storage for a removed config entry."""
    await Store(hass, STORAGE_VERSION, storage_key(entry.entry_id)).async_remove()
    await Store(
        hass, HISTORY_STORAGE_VERSION, history_storage_key(entry.entry_id)
    ).async_remove()


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)
