"""Constants for the Open-Meteo Solar Forecast integration."""

from __future__ import annotations

import logging

DOMAIN = "open_meteo_solar_forecast"
LOGGER = logging.getLogger(__package__)

CONF_BASE_URL = "base_url"
CONF_DECLINATION = "declination"
CONF_AZIMUTH = "azimuth"
CONF_MODULES_POWER = "modules_power"
CONF_DAMPING_MORNING = "damping_morning"
CONF_DAMPING_EVENING = "damping_evening"
CONF_INVERTER_POWER = "inverter_power"
CONF_ARRAY_INVERTER_POWER = "array_inverter_power"
CONF_EFFICIENCY_FACTOR = "efficiency_factor"
CONF_TRACKING = "tracking"

TRACKING_OPTIONS = ("none", "azimuth", "tilt", "dual")
CONF_USE_HORIZON = "use_horizon"
CONF_PARTIAL_SHADING = "partial_shading"
CONF_HORIZON_FILEPATH = "horizon_filepath"
CONF_MAX_SNOWCOVER_DEPTH_CM = "max_snowcover_depth_cm"
CONF_MODEL = "model"

ATTR_WATTS = "watts"
ATTR_WH_PERIOD = "wh_period"
ATTR_WH_PERIOD_15M = "wh_period_15m"

# --- 0.1.33.2: local weather source -------------------------------------
# See OMSF_v0_1_33_2_Architecture_ICS.md. "open_meteo" keeps the original behaviour and is
# the default for every existing config entry (no migration needed).
CONF_WEATHER_SOURCE = "weather_source"
SOURCE_OPEN_METEO = "open_meteo"
SOURCE_LOCAL = "local"
# 0.1.33.3: local days + Open-Meteo for every day the local data does not
# fully cover, joined at local midnight (hybrid.py).
SOURCE_HYBRID = "hybrid"
WEATHER_SOURCES = (SOURCE_OPEN_METEO, SOURCE_LOCAL, SOURCE_HYBRID)
# Sources that read the local entities.
LOCAL_BASED_SOURCES = (SOURCE_LOCAL, SOURCE_HYBRID)

CONF_LOCAL_WEATHER_ENTITY = "local_weather_entity"
CONF_LOCAL_IRRADIANCE_ENTITY = "local_irradiance_entity"
CONF_LOCAL_SNOW_DEPTH_ENTITY = "local_snow_depth_entity"
CONF_LOCAL_FALLBACK_OPEN_METEO = "local_fallback_open_meteo"

# Values reported by the forecast-source diagnostic sensor.
ACTIVE_SOURCE_OPEN_METEO = "open_meteo"
ACTIVE_SOURCE_LOCAL = "local"
ACTIVE_SOURCE_FALLBACK = "open_meteo_fallback"
ACTIVE_SOURCE_RETAINED = "retained"
ACTIVE_SOURCE_HYBRID = "hybrid"  # local days + Open-Meteo days in one forecast
# 0.1.33.4 (audit OMSF-003): retained forecast older than the limit below;
# the forecast sensors are unavailable, only the source sensor stays up.
ACTIVE_SOURCE_STALE = "stale"
ACTIVE_SOURCES = (
    ACTIVE_SOURCE_OPEN_METEO,
    ACTIVE_SOURCE_LOCAL,
    ACTIVE_SOURCE_FALLBACK,
    ACTIVE_SOURCE_RETAINED,
    ACTIVE_SOURCE_HYBRID,
    ACTIVE_SOURCE_STALE,
)

# 0.1.33.4 (audit OMSF-003, owner decision 6 Oct 2026): a retained forecast is
# served for at most this long after the last successful refresh, in every
# mode. After that the forecast sensors become unavailable.
RETAINED_MAX_AGE_HOURS = 6

OPEN_METEO_UPDATE_MINUTES = 30
LOCAL_UPDATE_MINUTES = 10
# Hybrid: Open-Meteo part refreshed at its normal cadence, not every local
# cycle; a cached Open-Meteo estimate is used for at most this long when a
# refresh fails, after which the Open-Meteo days become unknown.
HYBRID_OPEN_METEO_REFRESH_MINUTES = 30
HYBRID_OPEN_METEO_MAX_AGE_MINUTES = 180

# A local series must reach at least this many hours past the current hour.
LOCAL_MIN_FUTURE_HOURS = 6
# Past hours kept in the local history store: today and yesterday (local
# time), which covers "energy today" and the energy dashboard's look-back.
LOCAL_HISTORY_DAYS = 1

# Unique-id suffixes of SwissWeather Fusion (>= 0.3.3) companion entities,
# used to auto-discover them from the selected weather entity.
FUSION_WEATHER_SUFFIX = "_weather"
FUSION_IRRADIANCE_SUFFIX = "_solar_ghi"
FUSION_SNOW_DEPTH_SUFFIX = "_snow_depth"
FUSION_SRF_IRRADIANCE_SUFFIX = "_srf_irradiance"
