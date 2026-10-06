"""Home Assistant glue for the local weather source (0.1.33.2).

Responsibilities:

* read the configured entities **live** (the Fusion series attribute is not
  recorded in history, by design of that integration),
* validate and freshness-check the data (``local_source``),
* keep the elapsed hours of today/yesterday in a persistent store, because
  the source only publishes the current hour onwards,
* hand a frozen ``LocalSnapshot`` to ``LocalOpenMeteoSolarForecast``, a
  subclass of the upstream library class that answers the library's API
  request from the snapshot instead of the network.

Nothing here talks to the internet. See OMSF_v0_1_33_2_Architecture_ICS.md.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.weather import WeatherEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from open_meteo_solar_forecast import OpenMeteoSolarForecast

from .const import (
    DOMAIN,
    FUSION_IRRADIANCE_SUFFIX,
    FUSION_SNOW_DEPTH_SUFFIX,
    FUSION_SRF_IRRADIANCE_SUFFIX,
    FUSION_WEATHER_SUFFIX,
    LOCAL_HISTORY_DAYS,
    LOCAL_MIN_FUTURE_HOURS,
    LOGGER,
)
from .local_source import (
    HourRecord,
    LocalSnapshot,
    SynthesisStats,
    build_open_meteo_payload,
    floor_hour,
    merge_history,
    parse_irradiance_series,
    parse_snow_depth,
    parse_temperature_forecast,
    series_dni_consistency,
    series_freshness_problem,
)

HISTORY_STORAGE_VERSION = 1
SERVICE_TIMEOUT_SECONDS = 20  # below the coordinator's 60 s bound


class LocalDataError(HomeAssistantError):
    """The local source cannot deliver a usable forecast right now."""


def history_storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.local_history"


# --------------------------------------------------------------------------
# Companion discovery
# --------------------------------------------------------------------------


def async_find_fusion_companions(
    hass: HomeAssistant, weather_entity_id: str
) -> dict[str, str | None]:
    """Find SwissWeather Fusion sensors that belong to a weather entity.

    Matching is by config entry and unique-id suffix in the entity registry,
    so it survives entity renames. Returns ``None`` values when the weather
    entity is not a Fusion entity or a companion does not exist.
    """
    result: dict[str, str | None] = {"irradiance": None, "snow_depth": None, "srf_irradiance": None}
    registry = er.async_get(hass)
    weather = registry.async_get(weather_entity_id)
    if weather is None or not weather.config_entry_id:
        return result
    if not (weather.unique_id or "").endswith(FUSION_WEATHER_SUFFIX):
        return result
    prefix = weather.unique_id[: -len(FUSION_WEATHER_SUFFIX)]
    wanted = {
        f"{prefix}{FUSION_IRRADIANCE_SUFFIX}": "irradiance",
        f"{prefix}{FUSION_SNOW_DEPTH_SUFFIX}": "snow_depth",
        f"{prefix}{FUSION_SRF_IRRADIANCE_SUFFIX}": "srf_irradiance",
    }
    for entity in er.async_entries_for_config_entry(registry, weather.config_entry_id):
        if entity.domain == "sensor" and entity.unique_id in wanted:
            result[wanted[entity.unique_id]] = entity.entity_id
    return result


def validate_irradiance_entity(hass: HomeAssistant, entity_id: str | None) -> str | None:
    """Return an error key if the entity cannot serve as irradiance input."""
    if not entity_id:
        return "irradiance_not_found"
    state = hass.states.get(entity_id)
    if state is None:
        return "irradiance_not_found"
    series = state.attributes.get("hourly_forecast")
    if not isinstance(series, list) or not series:
        return "irradiance_no_series"
    records, _ = parse_irradiance_series(series)
    if not any(rec.has_average for rec in records.values()):
        return "irradiance_no_series"
    return None


def validate_weather_entity(hass: HomeAssistant, entity_id: str | None) -> str | None:
    """Return an error key if the entity cannot supply hourly temperatures."""
    if not entity_id:
        return "weather_not_found"
    state = hass.states.get(entity_id)
    if state is None:
        return "weather_not_found"
    features = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0) or 0
    if not int(features) & WeatherEntityFeature.FORECAST_HOURLY:
        return "weather_no_hourly"
    return None


# --------------------------------------------------------------------------
# Library adapter
# --------------------------------------------------------------------------


class LocalOpenMeteoSolarForecast(OpenMeteoSolarForecast):
    """Upstream forecast class fed from a local snapshot.

    Only ``_request`` is overridden: the library calls it once per array
    with that array's latitude, longitude, tilt and azimuth, and receives a
    synthesised Open-Meteo response. All PV physics stay in the library.
    """

    hass: HomeAssistant | None = None
    local_snapshot: LocalSnapshot | None = None
    local_stats: list[dict[str, Any]] | None = None

    def prepare(self, hass: HomeAssistant, snapshot: LocalSnapshot) -> None:
        """Bind the snapshot used by the next ``estimate()`` call."""
        self.hass = hass
        self.local_snapshot = snapshot
        self.local_stats = []

    async def _request(self, uri: str, *, params: dict[str, Any] | None = None) -> Any:
        if self.local_snapshot is None or self.hass is None:
            raise LocalDataError("local snapshot not prepared")
        if uri != "/v1/forecast" or params is None:
            raise LocalDataError(f"unexpected request {uri!r}")
        stats = SynthesisStats()
        payload = await self.hass.async_add_executor_job(
            _build_payload_job,
            self.local_snapshot,
            float(params["latitude"]),
            float(params["longitude"]),
            params["tilt"],
            params["azimuth"],
            stats,
        )
        if self.local_stats is not None:
            self.local_stats.append(asdict(stats))
        return payload


def _build_payload_job(
    snapshot: LocalSnapshot,
    latitude: float,
    longitude: float,
    tilt: str,
    azimuth: str,
    stats: SynthesisStats,
) -> dict[str, Any]:
    return build_open_meteo_payload(snapshot, latitude, longitude, tilt, azimuth, stats)


# --------------------------------------------------------------------------
# Reader with persistent history
# --------------------------------------------------------------------------


class LocalWeatherReader:
    """Reads, validates and accumulates the local weather data."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        weather_entity_id: str,
        irradiance_entity_id: str,
        snow_depth_entity_id: str | None,
        latitude: float,
        longitude: float,
    ) -> None:
        self.hass = hass
        self.weather_entity_id = weather_entity_id
        self.irradiance_entity_id = irradiance_entity_id
        self.snow_depth_entity_id = snow_depth_entity_id or None
        self.latitude = latitude
        self.longitude = longitude
        self._store: Store[dict[str, Any]] = Store(
            hass, HISTORY_STORAGE_VERSION, history_storage_key(entry_id)
        )
        self._loaded = False
        self._hours: dict[datetime, HourRecord] = {}
        self._temps: dict[datetime, float] = {}
        self.last_report: dict[str, Any] = {}

    # -- persistence -----------------------------------------------------

    async def _async_load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            stored = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - corrupt store must not block setup
            LOGGER.warning("Discarding unreadable local history: %s", err)
            return
        if not stored:
            return
        try:
            self._hours = {
                rec.start: rec
                for rec in (HourRecord.from_json(item) for item in stored.get("hours", []))
            }
            self._temps = {
                datetime.fromisoformat(key).astimezone(timezone.utc): float(value)
                for key, value in stored.get("temperatures", {}).items()
            }
        except (KeyError, TypeError, ValueError) as err:
            LOGGER.warning("Discarding malformed local history: %s", err)
            self._hours, self._temps = {}, {}

    def _schedule_save(self) -> None:
        data = {
            "hours": [rec.to_json() for rec in self._hours.values()],
            "temperatures": {key.isoformat(): value for key, value in self._temps.items()},
        }
        self._store.async_delay_save(lambda: data, 30)

    async def async_remove(self) -> None:
        await self._store.async_remove()

    # -- reading ---------------------------------------------------------

    def _read_irradiance(self, now: datetime) -> tuple[dict[datetime, HourRecord], dict[str, Any]]:
        state = self.hass.states.get(self.irradiance_entity_id)
        if state is None:
            raise LocalDataError(f"{self.irradiance_entity_id} does not exist")
        if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN) and "hourly_forecast" not in state.attributes:
            raise LocalDataError(f"{self.irradiance_entity_id} is {state.state}")
        records, report = parse_irradiance_series(state.attributes.get("hourly_forecast"))
        problem = series_freshness_problem(records, now, LOCAL_MIN_FUTURE_HOURS)
        details = {
            "entity_id": self.irradiance_entity_id,
            "received": report.received,
            "accepted_average": report.accepted_average,
            "accepted_instant": report.accepted_instant,
            "rejected": report.rejected,
            "first_hour": min(records).isoformat() if records else None,
            "last_hour": max(records).isoformat() if records else None,
            "current_hour_sources": getattr(records.get(floor_hour(now)), "sources", None),
            "entity_last_updated": state.last_updated.isoformat(),
        }
        if problem:
            details["problem"] = problem
            raise LocalDataError(problem)
        return records, details

    async def _async_read_temperatures(self) -> tuple[dict[datetime, float], dict[str, Any]]:
        state = self.hass.states.get(self.weather_entity_id)
        if state is None:
            raise LocalDataError(f"{self.weather_entity_id} does not exist")
        unit = state.attributes.get("temperature_unit")
        details: dict[str, Any] = {"entity_id": self.weather_entity_id, "unit": unit}
        temps: dict[datetime, float] = {}
        try:
            # Bounded (D-06): a hung weather entity must not stall the cycle.
            async with asyncio.timeout(SERVICE_TIMEOUT_SECONDS):
                response = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"entity_id": self.weather_entity_id, "type": "hourly"},
                    blocking=True,
                    return_response=True,
                )
            forecast = (response or {}).get(self.weather_entity_id, {}).get("forecast")
            temps, rejected = parse_temperature_forecast(forecast, unit)
            details["received"] = len(forecast) if isinstance(forecast, list) else 0
            details["rejected"] = rejected
        except Exception as err:  # noqa: BLE001 - degrade to current temperature
            details["error"] = str(err) or type(err).__name__
        current, _ = parse_temperature_forecast(
            [{"datetime": dt_util.utcnow().isoformat(), "temperature": state.attributes.get("temperature")}],
            unit,
        )
        details["current_temperature_c"] = next(iter(current.values()), None)
        details["accepted"] = len(temps)
        return temps, details

    def _read_snow_depth(self) -> tuple[float, dict[str, Any]]:
        if not self.snow_depth_entity_id:
            return 0.0, {"configured": False}
        state = self.hass.states.get(self.snow_depth_entity_id)
        depth = None
        if state is not None:
            depth = parse_snow_depth(state.state, state.attributes.get("unit_of_measurement"))
        details = {"configured": True, "entity_id": self.snow_depth_entity_id, "depth_m": depth}
        if depth is None:
            # Unknown snow is treated as no snow (no derating). Flagged.
            details["problem"] = "snow depth unavailable, treated as 0"
            return 0.0, details
        return depth, details

    async def async_snapshot(self, now: datetime | None = None) -> LocalSnapshot:
        """Read everything and return an immutable snapshot.

        Raises ``LocalDataError`` when the irradiance series is missing,
        invalid or stale, or when no temperature at all is available.
        """
        await self._async_load()
        now = now or dt_util.utcnow()
        report: dict[str, Any] = {"read_at": now.isoformat()}
        self.last_report = report
        try:
            fresh_hours, report["irradiance"] = self._read_irradiance(now)
        except LocalDataError as err:
            report["irradiance"] = {"entity_id": self.irradiance_entity_id, "problem": str(err)}
            raise

        fresh_temps, report["temperature"] = await self._async_read_temperatures()
        fallback_temp = report["temperature"].get("current_temperature_c")
        if not fresh_temps and fallback_temp is None:
            raise LocalDataError(
                f"no temperature available from {self.weather_entity_id}"
            )
        snow_m, report["snow_depth"] = self._read_snow_depth()

        local_now = dt_util.as_local(now)
        keep_from = dt_util.as_utc(
            dt_util.start_of_local_day(local_now) - timedelta(days=LOCAL_HISTORY_DAYS)
        )
        self._hours = merge_history(self._hours, fresh_hours, now, keep_from)
        self._temps = merge_history(self._temps, fresh_temps, now, keep_from)
        self._schedule_save()

        offset = local_now.utcoffset() or timedelta(0)
        report["history"] = {
            "hours": len(self._hours),
            "first_hour": min(self._hours).isoformat() if self._hours else None,
            "keep_from": keep_from.isoformat(),
        }
        report["dni_consistency_mad_w_m2"] = await self.hass.async_add_executor_job(
            series_dni_consistency,
            [rec for start, rec in fresh_hours.items() if start < floor_hour(now) + timedelta(hours=24)],
            self.latitude,
            self.longitude,
        )
        report["comparison"] = self._comparison(now)
        return LocalSnapshot(
            hours=dict(self._hours),
            temperatures_c=dict(self._temps),
            utc_offset_seconds=int(offset.total_seconds()),
            snow_depth_m=snow_m,
            fallback_temperature_c=fallback_temp,
        )

    def _comparison(self, now: datetime) -> dict[str, Any]:
        """Current-hour ICON GHI next to SRF's independent GHI (if found).

        Comparison only; SRF never feeds the forecast (its averaging basis
        is undocumented).
        """
        current = self._hours.get(floor_hour(now))
        result: dict[str, Any] = {"icon_ghi_hour_average": getattr(current, "ghi", None)}
        companions = async_find_fusion_companions(self.hass, self.weather_entity_id)
        srf_id = companions.get("srf_irradiance")
        if srf_id and (state := self.hass.states.get(srf_id)) is not None:
            try:
                result["srf_ghi"] = float(state.state)
            except (TypeError, ValueError):
                result["srf_ghi"] = None
            result["srf_entity_id"] = srf_id
        return result
