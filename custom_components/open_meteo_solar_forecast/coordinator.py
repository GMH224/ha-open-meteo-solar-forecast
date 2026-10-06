"""DataUpdateCoordinator for the Open-Meteo Solar Forecast integration."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from open_meteo_solar_forecast import Estimate, OpenMeteoSolarForecast

from .const import (
    CONF_ARRAY_INVERTER_POWER,
    CONF_AZIMUTH,
    CONF_BASE_URL,
    CONF_DAMPING_EVENING,
    CONF_DAMPING_MORNING,
    CONF_DECLINATION,
    CONF_EFFICIENCY_FACTOR,
    CONF_INVERTER_POWER,
    CONF_USE_HORIZON,
    CONF_PARTIAL_SHADING,
    CONF_MAX_SNOWCOVER_DEPTH_CM,
    CONF_MODEL,
    CONF_MODULES_POWER,
    CONF_TRACKING,
    CONF_LOCAL_FALLBACK_OPEN_METEO,
    CONF_LOCAL_IRRADIANCE_ENTITY,
    CONF_LOCAL_SNOW_DEPTH_ENTITY,
    CONF_LOCAL_WEATHER_ENTITY,
    CONF_WEATHER_SOURCE,
    ACTIVE_SOURCE_FALLBACK,
    ACTIVE_SOURCE_HYBRID,
    ACTIVE_SOURCE_LOCAL,
    ACTIVE_SOURCE_OPEN_METEO,
    ACTIVE_SOURCE_RETAINED,
    DOMAIN,
    HYBRID_OPEN_METEO_MAX_AGE_MINUTES,
    HYBRID_OPEN_METEO_REFRESH_MINUTES,
    LOCAL_BASED_SOURCES,
    LOCAL_UPDATE_MINUTES,
    LOGGER,
    OPEN_METEO_UPDATE_MINUTES,
    SOURCE_HYBRID,
    SOURCE_OPEN_METEO,
)
from .hybrid import DAY_SOURCE_LOCAL, DAY_SOURCE_OPEN_METEO, merge_hybrid
from .local_provider import (
    LocalDataError,
    LocalOpenMeteoSolarForecast,
    LocalWeatherReader,
)
from .local_source import complete_local_dates

import numpy

STORAGE_VERSION = 2


class RetainedForecastStore(Store[dict[str, Any]]):
    """Store that discards retained forecasts from older storage versions."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        return None

# The upstream library performs the API request without any timeout, so an
# unreachable API can hang a refresh (and config entry setup) indefinitely.
API_TIMEOUT_SECONDS = 60


def storage_key(entry_id: str) -> str:
    """Return the storage key for the retained forecast of a config entry."""
    return f"{DOMAIN}.{entry_id}"


def _config_fingerprint(entry: ConfigEntry) -> str:
    """Fingerprint the settings that affect forecast values.

    A retained forecast computed with a different configuration (e.g. changed
    azimuth or panel power) must not be served after an options reload.
    """
    values = {**entry.data, **entry.options}
    return json.dumps(values, sort_keys=True, default=str)


def _datetime_dict_to_json(data: dict[datetime, int]) -> dict[str, int]:
    return {timestamp.isoformat(): value for timestamp, value in data.items()}


def _datetime_dict_from_json(data: dict[str, int]) -> dict[datetime, int]:
    return {datetime.fromisoformat(timestamp): value for timestamp, value in data.items()}


def _date_dict_from_json(data: dict[str, int]) -> dict[date, int]:
    return {date.fromisoformat(day): value for day, value in data.items()}


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _normalize_array_value(value: Any, array_count: int, transform: Any = None) -> Any:
    """Normalize scalar or sequence values to scalar or list for the forecast API."""
    if _is_sequence(value):
        normalized = list(value)
        if len(normalized) == 1 and array_count > 1:
            normalized = normalized * array_count
        elif len(normalized) != array_count:
            raise ValueError(
                "Multi-array configuration has inconsistent list lengths "
                f"({len(normalized)} vs {array_count})."
            )
        if transform is not None:
            normalized = [transform(item) for item in normalized]
        return normalized

    if transform is not None:
        value = transform(value)

    if array_count > 1:
        return [value] * array_count
    return value


def _resolve_array_count(*values: Any) -> int:
    lengths = [len(value) for value in values if _is_sequence(value)]
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


def _entry_value(entry: ConfigEntry, key: str) -> Any:
    """Get config value from options with fallback to entry data."""
    return entry.options.get(key, entry.data.get(key))

def checkHorizonFile(horizon_filepath):
    horizon_data_valid = True
    message = ""
    
    try:
        open(horizon_filepath)
    except FileNotFoundError:
        horizon_data_valid = False
        message = "Invalid horizon file: Horizon file '" + horizon_filepath + "' not found! Specify path like e.g. '/config/www/horizon.txt'"
    
    if horizon_data_valid:
        horizon_data = numpy.genfromtxt(horizon_filepath , delimiter="\t", dtype=float)
        hm = ((0,90),(360,90))
        
        # ... check array shape (error)
        sh = horizon_data.shape
        if isinstance(sh, tuple) and len(sh) == 2:
            if sh[0] < 2 or not sh[1] == 2:
                horizon_data_valid = False
                message = "Invalid horizon file: The array shape is " + str(sh) + ", which is invalid. It has to be at least two rows and exactly two columns (N>1 , 2). Please check (two columns, tab delimiter, decimal points)."
            else:
                hm = tuple([tuple(row) for row in horizon_data])
        else:
            horizon_data_valid = False
            message = "Invalid horizon file: The array shape cannot be determined. It has to be at least two rows and exactly two columns (N>1 , 2). Please check (two columns, tab delimiter, decimal points)."
        
        # ... check for floats (error) - via valid sum of floats or NaN
        if numpy.isnan(numpy.sum(hm)):
            horizon_data_valid = False
            message = "Invalid horizon file: The data seems to contain non-float values. Please check (two columns, tab delimiter, decimal points)."
        
        # ... check range 0...360° (warning only)
        if horizon_data_valid:
            hm_0 = int(hm[0][0])
            hm_n = int(hm[-1][0])
            if not hm_0 == 0 or not hm_n == 360:
                horizon_data_valid = False
                message = "Invalid horizon file: Azimuth values (" + str(hm_0) + "° to " + str(hm_n) + "°) do not contain 0° and/or 360°. I cannot judge whether the full range of applicable azimuths is covered by the horizon file. Please check..."
            
            # ... check ascending azimuths (warning only)
            n = sh[0]
            for i in range(1,n):
                a1 = horizon_data[i-1][0]
                a2 = horizon_data[i][0]
                if not (a2 > a1):
                    message = "Invalid horizon file: Azimuth values are not ascending around value of " + str(a1) + ". Please check..."
                    horizon_data_valid = False
    
    if horizon_data_valid:
        return hm, message
    else:
        return None, message  

class OpenMeteoSolarForecastDataUpdateCoordinator(DataUpdateCoordinator[Estimate]):
    """The Solar Forecast Data Update Coordinator."""

    config_entry: ConfigEntry
    
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        horizon_map: tuple[tuple[float, float], ...] | list[tuple[tuple[float, float], ...]],
    ) -> None:
        """Initialize the Solar Forecast coordinator."""
        self.config_entry = entry

        # Our option flow may cause it to be an empty string,
        # this if statement is here to catch that.
        api_key = entry.options.get(CONF_API_KEY) or None

        # Handle new options that were added after the initial release
        ac_kwp = entry.options.get(CONF_INVERTER_POWER, 0)
        ac_kwp = ac_kwp / 1000 if ac_kwp else None
        self._last_successful_update: datetime | None = None
        self._store: Store[dict[str, Any]] = RetainedForecastStore(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        self._config_fingerprint = _config_fingerprint(entry)

        array_count = _resolve_array_count(
            _entry_value(entry, CONF_LATITUDE),
            _entry_value(entry, CONF_LONGITUDE),
            entry.options[CONF_DECLINATION],
            entry.options[CONF_AZIMUTH],
            entry.options[CONF_MODULES_POWER],
            entry.options.get(CONF_ARRAY_INVERTER_POWER, 0),
            entry.options.get(CONF_EFFICIENCY_FACTOR, 1.0),
            entry.options.get(CONF_TRACKING, "none"),
            entry.options.get(CONF_USE_HORIZON, False),
            entry.options.get(CONF_PARTIAL_SHADING, False),
        )

        latitude = _normalize_array_value(
            _entry_value(entry, CONF_LATITUDE), array_count
        )
        longitude = _normalize_array_value(
            _entry_value(entry, CONF_LONGITUDE), array_count
        )
        azimuth = _normalize_array_value(
            entry.options[CONF_AZIMUTH],
            array_count,
            transform=lambda value: value - 180,
        )
        dc_kwp = _normalize_array_value(
            entry.options[CONF_MODULES_POWER],
            array_count,
            transform=lambda value: value / 1000,
        )
        declination = _normalize_array_value(
            entry.options[CONF_DECLINATION],
            array_count,
        )
        efficiency_factor = _normalize_array_value(
            entry.options.get(CONF_EFFICIENCY_FACTOR, 1.0),
            array_count,
        )
        tracking = _normalize_array_value(
            entry.options.get(CONF_TRACKING, "none"),
            array_count,
        )
        use_horizon = _normalize_array_value(
            entry.options.get(CONF_USE_HORIZON, False),
            array_count,
        )
        partial_shading = _normalize_array_value(
            entry.options.get(CONF_PARTIAL_SHADING, False),
            array_count,
        )

        # Per-array inverter capacities (0 = no dedicated inverter for that
        # array). If any array has its own inverter, pass a list to the
        # library so each array's output is clamped individually; this
        # overrides the shared inverter capacity configured above.
        array_ac_kwp = _normalize_array_value(
            entry.options.get(CONF_ARRAY_INVERTER_POWER, 0),
            array_count,
            transform=lambda value: value / 1000 if value else None,
        )
        if _is_sequence(array_ac_kwp):
            if any(value is not None for value in array_ac_kwp):
                ac_kwp = list(array_ac_kwp)
        elif array_ac_kwp is not None:
            # Single array with its own inverter: behaves like a shared one.
            ac_kwp = array_ac_kwp

        forecast_kwargs: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "azimuth": azimuth,
            "ac_kwp": ac_kwp,
            "dc_kwp": dc_kwp,
            "declination": declination,
            "efficiency_factor": efficiency_factor,
            "tracking": tracking,
            "damping_morning": entry.options.get(CONF_DAMPING_MORNING, 0.0),
            "damping_evening": entry.options.get(CONF_DAMPING_EVENING, 0.0),
            "use_horizon": use_horizon,
            "partial_shading": partial_shading,
            "horizon_map": horizon_map,
            "max_snowcover_depth_cm": entry.options.get(CONF_MAX_SNOWCOVER_DEPTH_CM, 0.0),
        }
        self.forecast = OpenMeteoSolarForecast(
            api_key=api_key,
            session=async_get_clientsession(hass),
            base_url=entry.options[CONF_BASE_URL],
            weather_model=entry.options.get(CONF_MODEL, "best_match"),
            **forecast_kwargs,
        )

        # 0.1.33.2: optional local weather source (OMSF_v0_1_33_2_Architecture_ICS.md).
        self.weather_source: str = entry.options.get(
            CONF_WEATHER_SOURCE, SOURCE_OPEN_METEO
        )
        self.fallback_to_open_meteo: bool = bool(
            entry.options.get(CONF_LOCAL_FALLBACK_OPEN_METEO, False)
        )
        # Local and hybrid both read the local entities.
        self.uses_local: bool = self.weather_source in LOCAL_BASED_SOURCES
        self.active_source: str | None = None
        self.last_source_error: str | None = None
        self.last_local_stats: list[dict[str, Any]] = []
        # Dates the current forecast covers completely (local/hybrid only)
        # and which source each one came from. None = not applicable:
        # sensors behave exactly as in 0.1.33.1.
        self.local_complete_dates: set[date] | None = None
        self.day_sources: dict[date, str] | None = None
        # Hybrid: cached Open-Meteo estimate and the state of that part.
        self._hybrid_om_cache: tuple[datetime, Estimate] | None = None
        self.hybrid_status: dict[str, Any] | None = None
        self.local_reader: LocalWeatherReader | None = None
        self.local_forecast: LocalOpenMeteoSolarForecast | None = None
        if self.uses_local:
            first_lat = latitude[0] if _is_sequence(latitude) else latitude
            first_lon = longitude[0] if _is_sequence(longitude) else longitude
            self.local_reader = LocalWeatherReader(
                hass,
                entry.entry_id,
                weather_entity_id=entry.options[CONF_LOCAL_WEATHER_ENTITY],
                irradiance_entity_id=entry.options[CONF_LOCAL_IRRADIANCE_ENTITY],
                snow_depth_entity_id=entry.options.get(CONF_LOCAL_SNOW_DEPTH_ENTITY),
                latitude=float(first_lat),
                longitude=float(first_lon),
            )
            # Same physics parameters as the Open-Meteo instance; only the
            # data request differs. No API key, URL or session: the local
            # adapter never performs network I/O.
            self.local_forecast = LocalOpenMeteoSolarForecast(**forecast_kwargs)
            update_interval = timedelta(minutes=LOCAL_UPDATE_MINUTES)
        else:
            update_interval = timedelta(minutes=OPEN_METEO_UPDATE_MINUTES)

        super().__init__(hass, LOGGER, name=DOMAIN, update_interval=update_interval)

    async def _async_load_retained_estimate(self) -> Estimate | None:
        """Load the retained forecast persisted across restarts."""
        stored = await self._store.async_load()
        if not stored:
            return None

        if stored.get("config_fingerprint") != self._config_fingerprint:
            LOGGER.debug(
                "Discarding retained forecast computed with a different configuration"
            )
            return None

        try:
            last_update = stored["last_successful_update"]
            estimate = Estimate(
                watts=_datetime_dict_from_json(stored["watts"]),
                wh_period_15m=_datetime_dict_from_json(stored["wh_period_15m"]),
                wh_period=_datetime_dict_from_json(stored["wh_period"]),
                wh_days=_date_dict_from_json(stored["wh_days"]),
                api_timezone=timezone(
                    timedelta(seconds=stored["api_timezone_offset"])
                ),
            )
        except (KeyError, TypeError, ValueError):
            LOGGER.warning("Discarding malformed retained forecast data")
            return None

        self._last_successful_update = dt_util.parse_datetime(last_update)
        if self.uses_local:
            # Absent key: the retained forecast came from the Open-Meteo
            # fallback, so nothing is masked (D-07). Malformed: mask all.
            raw_sources = stored.get("day_sources")
            raw_dates = stored.get("local_complete_dates")
            try:
                if raw_sources is not None:
                    self._set_day_sources(
                        {date.fromisoformat(day): src for day, src in raw_sources.items()}
                    )
                elif raw_dates is not None:  # written by 0.1.33.2
                    self._set_day_sources(
                        {date.fromisoformat(day): DAY_SOURCE_LOCAL for day in raw_dates}
                    )
                else:
                    self._set_day_sources(None)
            except (AttributeError, TypeError, ValueError):
                self._set_day_sources({})
        return estimate

    def _set_day_sources(self, sources: dict[date, str] | None) -> None:
        """Set per-day provenance and the derived masking set together."""
        self.day_sources = sources
        self.local_complete_dates = None if sources is None else set(sources)

    def _save_retained_estimate(self, estimate: Estimate) -> None:
        """Persist the forecast so retention survives restarts and reloads."""
        data = {
            "config_fingerprint": self._config_fingerprint,
            "last_successful_update": self._last_successful_update.isoformat(),
            "watts": _datetime_dict_to_json(estimate.watts),
            "wh_period": _datetime_dict_to_json(estimate.wh_period),
            "wh_days": _datetime_dict_to_json(estimate.wh_days),
            "wh_period_15m": _datetime_dict_to_json(estimate.wh_period_15m),
            "api_timezone_offset": estimate.api_timezone.utcoffset(
                None
            ).total_seconds(),
        }
        if self.local_complete_dates is not None:
            data["local_complete_dates"] = sorted(
                day.isoformat() for day in self.local_complete_dates
            )
        if self.day_sources is not None:
            data["day_sources"] = {
                day.isoformat(): src for day, src in sorted(self.day_sources.items())
            }
        self._store.async_delay_save(lambda: data, 60)

    async def _async_update_data(self) -> Estimate:
        """Fetch Open-Meteo Solar Forecast estimates."""
        # On the first refresh after a restart or reload, reuse the stored
        # forecast if it is younger than the update interval instead of
        # hitting the API again.
        if self.data is None:
            retained = await self._async_load_retained_estimate()
            if (
                retained is not None
                and self._last_successful_update is not None
                and dt_util.utcnow() - self._last_successful_update
                < self.update_interval
            ):
                LOGGER.debug(
                    "Using stored forecast from %s, skipping fetch",
                    self._last_successful_update,
                )
                self.active_source = ACTIVE_SOURCE_RETAINED
                return retained

        try:
            estimate, source = await self._async_fetch_estimate()
        except Exception as err:
            self.last_source_error = str(err) or type(err).__name__
            retained = self.data
            if retained is None:
                retained = await self._async_load_retained_estimate()
            if retained is None:
                self.active_source = None
                if not self.uses_local:
                    # Message unchanged from 0.1.33.1 (parity invariant R6).
                    raise UpdateFailed(f"Error communicating with API: {err}") from err
                raise UpdateFailed(f"Error reading local weather source: {err}") from err

            if not self.uses_local:
                # Logging unchanged from 0.1.33.1 (parity invariant R6).
                LOGGER.warning(
                    "Unable to refresh forecast data, using retained forecast",
                    exc_info=err,
                )
            elif self.active_source != ACTIVE_SOURCE_RETAINED:
                # Local mode refreshes every 10 min: log transitions only.
                LOGGER.warning(
                    "Local weather source unusable, keeping the forecast from %s: %s",
                    self._last_successful_update,
                    err,
                )
            self.active_source = ACTIVE_SOURCE_RETAINED
            return retained

        if self.uses_local and (
            self.active_source == ACTIVE_SOURCE_RETAINED
            or (
                self.active_source == ACTIVE_SOURCE_FALLBACK
                and source in (ACTIVE_SOURCE_LOCAL, ACTIVE_SOURCE_HYBRID)
            )
        ):
            LOGGER.info("Forecast refresh recovered, source: %s", source)
        self.active_source = source
        if source != ACTIVE_SOURCE_FALLBACK:
            self.last_source_error = None
        self._last_successful_update = dt_util.utcnow()
        self._save_retained_estimate(estimate)
        return estimate

    async def _async_fetch_open_meteo(self) -> Estimate:
        async with asyncio.timeout(API_TIMEOUT_SECONDS):
            return await self.forecast.estimate()

    async def _async_fetch_estimate(self) -> tuple[Estimate, str]:
        """Fetch a new estimate from the configured source.

        Local mode: read and validate the local entities, then let the
        library compute the estimate from the synthesised data. On any
        local failure either fall back to Open-Meteo (only if explicitly
        enabled) or propagate, so the caller serves the retained forecast.
        """
        if not self.uses_local:
            return await self._async_fetch_open_meteo(), ACTIVE_SOURCE_OPEN_METEO

        if self.local_reader is None or self.local_forecast is None:
            raise LocalDataError("local source not initialised")
        try:
            # One bound for the whole local path (D-06): entity reads, the
            # weather service call and the synthesis together.
            async with asyncio.timeout(API_TIMEOUT_SECONDS):
                snapshot = await self.local_reader.async_snapshot()
                self.local_forecast.prepare(self.hass, snapshot)
                estimate = await self.local_forecast.estimate()
            self.last_local_stats = list(self.local_forecast.local_stats or [])
            local_days = await self.hass.async_add_executor_job(
                complete_local_dates,
                snapshot.hours,
                self.local_reader.latitude,
                self.local_reader.longitude,
                snapshot.utc_offset_seconds,
            )
        except Exception as err:
            if isinstance(err, LocalDataError):
                message = f"local source unusable: {err}"
            else:
                message = f"local source failed: {type(err).__name__}: {err}"
            if not self.fallback_to_open_meteo:
                raise LocalDataError(message) from err
            if self.active_source != ACTIVE_SOURCE_FALLBACK:
                LOGGER.warning("%s; falling back to Open-Meteo", message)
            self.last_source_error = message
            estimate = await self._async_fetch_open_meteo()
            self._set_day_sources(None)  # Open-Meteo data: no masking
            return estimate, ACTIVE_SOURCE_FALLBACK

        if self.weather_source != SOURCE_HYBRID:
            self._set_day_sources({day: DAY_SOURCE_LOCAL for day in local_days})
            return estimate, ACTIVE_SOURCE_LOCAL

        # Hybrid (0.1.33.3): whole local days from the local estimate, every
        # other day from Open-Meteo. The Open-Meteo part never fails the
        # refresh: without it, the local days are still served.
        open_meteo = await self._async_hybrid_open_meteo()
        merged, sources = merge_hybrid(estimate, open_meteo, set(local_days))
        self._set_day_sources(sources)
        if self.hybrid_status is not None:
            self.hybrid_status["open_meteo_days"] = sorted(
                day.isoformat() for day, src in sources.items()
                if src == DAY_SOURCE_OPEN_METEO
            )
        if DAY_SOURCE_OPEN_METEO in sources.values():
            return merged, ACTIVE_SOURCE_HYBRID
        return merged, ACTIVE_SOURCE_LOCAL

    async def _async_hybrid_open_meteo(self) -> Estimate | None:
        """Open-Meteo estimate for the hybrid days, cached.

        Refreshed at most every HYBRID_OPEN_METEO_REFRESH_MINUTES. If a
        refresh fails, a cached estimate younger than
        HYBRID_OPEN_METEO_MAX_AGE_MINUTES is still used; otherwise None
        (the Open-Meteo days become unknown). Never raises.
        """
        now = dt_util.utcnow()
        cache = self._hybrid_om_cache
        if cache is not None and now - cache[0] < timedelta(
            minutes=HYBRID_OPEN_METEO_REFRESH_MINUTES
        ):
            self.hybrid_status = {
                "open_meteo": "cached",
                "fetched_at": cache[0].isoformat(),
                "error": None,
            }
            return cache[1]
        try:
            estimate = await self._async_fetch_open_meteo()
        except Exception as err:  # noqa: BLE001 - local days must survive
            error = str(err) or type(err).__name__
            if cache is not None and now - cache[0] < timedelta(
                minutes=HYBRID_OPEN_METEO_MAX_AGE_MINUTES
            ):
                if (self.hybrid_status or {}).get("open_meteo") != "stale_cache":
                    LOGGER.warning(
                        "Hybrid: Open-Meteo refresh failed, using the estimate from %s: %s",
                        cache[0],
                        error,
                    )
                self.hybrid_status = {
                    "open_meteo": "stale_cache",
                    "fetched_at": cache[0].isoformat(),
                    "error": error,
                }
                return cache[1]
            if (self.hybrid_status or {}).get("open_meteo") != "unavailable":
                LOGGER.warning(
                    "Hybrid: Open-Meteo unavailable, days beyond the local data are unknown: %s",
                    error,
                )
            self._hybrid_om_cache = None
            self.hybrid_status = {"open_meteo": "unavailable", "fetched_at": None, "error": error}
            return None
        self._hybrid_om_cache = (now, estimate)
        self.hybrid_status = {"open_meteo": "fresh", "fetched_at": now.isoformat(), "error": None}
        return estimate

    @property
    def last_successful_update(self) -> datetime | None:
        """UTC time of the last successful (non-retained) refresh."""
        return self._last_successful_update
