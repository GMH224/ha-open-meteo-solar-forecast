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
WEATHER_SOURCES = (SOURCE_OPEN_METEO, SOURCE_LOCAL)

CONF_LOCAL_WEATHER_ENTITY = "local_weather_entity"
CONF_LOCAL_IRRADIANCE_ENTITY = "local_irradiance_entity"
CONF_LOCAL_SNOW_DEPTH_ENTITY = "local_snow_depth_entity"
CONF_LOCAL_FALLBACK_OPEN_METEO = "local_fallback_open_meteo"

# Values reported by the forecast-source diagnostic sensor.
ACTIVE_SOURCE_OPEN_METEO = "open_meteo"
ACTIVE_SOURCE_LOCAL = "local"
ACTIVE_SOURCE_FALLBACK = "open_meteo_fallback"
ACTIVE_SOURCE_RETAINED = "retained"
ACTIVE_SOURCES = (
    ACTIVE_SOURCE_OPEN_METEO,
    ACTIVE_SOURCE_LOCAL,
    ACTIVE_SOURCE_FALLBACK,
    ACTIVE_SOURCE_RETAINED,
)

OPEN_METEO_UPDATE_MINUTES = 30
LOCAL_UPDATE_MINUTES = 10

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
