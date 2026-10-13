"""Config flow for Open-Meteo Solar Forecast integration."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_API_KEY, CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_ENTRY_TYPE,
    CONF_GROUP_ENERGY_ENTITY,
    CONF_GROUP_MEMBERS,
    CONF_GROUP_NAME,
    CONF_GROUPS,
    CONF_STATION_ENTITY,
    DEFAULT_STATION_ENTITY,
    ENTRY_TYPE_LEARNING_HUB,
    LEARNING_HUB_UNIQUE_ID,
    CONF_ARRAY_INVERTER_POWER,
    CONF_AZIMUTH,
    CONF_BASE_URL,
    CONF_DAMPING_EVENING,
    CONF_DAMPING_MORNING,
    CONF_DECLINATION,
    CONF_EFFICIENCY_FACTOR,
    CONF_INVERTER_POWER,
    CONF_MODEL,
    CONF_USE_HORIZON,
    CONF_PARTIAL_SHADING,
    CONF_HORIZON_FILEPATH,
    CONF_SHADING_FILEPATH,
    CONF_MAX_SNOWCOVER_DEPTH_CM,
    CONF_MODULES_POWER,
    CONF_TRACKING,
    CONF_LOCAL_FALLBACK_OPEN_METEO,
    CONF_LOCAL_IRRADIANCE_ENTITY,
    CONF_LOCAL_SNOW_DEPTH_ENTITY,
    CONF_LOCAL_WEATHER_ENTITY,
    CONF_WEATHER_SOURCE,
    DOMAIN,
    LOCAL_BASED_SOURCES,
    SOURCE_OPEN_METEO,
    TRACKING_OPTIONS,
    WEATHER_SOURCES,
)

try:
    from homeassistant.config_entries import ConfigFlowResult  # >=2024.4.0b0
except ImportError:
    from homeassistant.data_entry_flow import FlowResult as ConfigFlowResult

CONF_ADD_ANOTHER = "add_another"

DEFAULT_HORIZON_FILEPATH = (
    "/config/custom_components/open_meteo_solar_forecast/horizon.txt"
)

# Fields that can differ per PV array. Stored as a scalar for a single array
# and as equal-length lists for multi-array setups (the format the
# coordinator already understands).
PER_ARRAY_KEYS = (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_DECLINATION,
    CONF_AZIMUTH,
    CONF_MODULES_POWER,
    CONF_ARRAY_INVERTER_POWER,
    CONF_EFFICIENCY_FACTOR,
    CONF_TRACKING,
    CONF_DAMPING_MORNING,
    CONF_DAMPING_EVENING,
    CONF_USE_HORIZON,
    CONF_PARTIAL_SHADING,
    CONF_HORIZON_FILEPATH,
    CONF_SHADING_FILEPATH,
)


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _array_defaults(latitude: float, longitude: float) -> dict[str, Any]:
    return {
        CONF_LATITUDE: latitude,
        CONF_LONGITUDE: longitude,
        CONF_DECLINATION: 25,
        CONF_AZIMUTH: 180,
        CONF_MODULES_POWER: None,
        CONF_ARRAY_INVERTER_POWER: 0,
        CONF_EFFICIENCY_FACTOR: 1.0,
        CONF_TRACKING: "none",
        CONF_DAMPING_MORNING: 0.0,
        CONF_DAMPING_EVENING: 0.0,
        CONF_USE_HORIZON: False,
        CONF_PARTIAL_SHADING: False,
        CONF_HORIZON_FILEPATH: DEFAULT_HORIZON_FILEPATH,
        CONF_SHADING_FILEPATH: "",
    }


def _array_schema(defaults: dict[str, Any], add_another_default: bool) -> vol.Schema:
    def _field(key: str) -> vol.Marker:
        default = defaults.get(key)
        if default is None:
            return vol.Required(key)
        return vol.Required(key, default=default)

    return vol.Schema(
        {
            _field(CONF_LATITUDE): NumberSelector(
                NumberSelectorConfig(
                    min=-90, max=90, step="any", mode=NumberSelectorMode.BOX
                )
            ),
            _field(CONF_LONGITUDE): NumberSelector(
                NumberSelectorConfig(
                    min=-180, max=180, step="any", mode=NumberSelectorMode.BOX
                )
            ),
            _field(CONF_DECLINATION): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=90,
                    step="any",
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="°",
                )
            ),
            _field(CONF_AZIMUTH): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=360,
                    step="any",
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="°",
                )
            ),
            _field(CONF_MODULES_POWER): vol.All(
                NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="Wp",
                    )
                ),
                vol.Coerce(int),
            ),
            _field(CONF_ARRAY_INVERTER_POWER): vol.All(
                NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="W",
                    )
                ),
                vol.Coerce(int),
            ),
            _field(CONF_TRACKING): SelectSelector(
                SelectSelectorConfig(
                    options=list(TRACKING_OPTIONS),
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="tracking",
                )
            ),
            _field(CONF_EFFICIENCY_FACTOR): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=1, step="any", mode=NumberSelectorMode.BOX
                )
            ),
            _field(CONF_DAMPING_MORNING): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=1, step="any", mode=NumberSelectorMode.BOX
                )
            ),
            _field(CONF_DAMPING_EVENING): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=1, step="any", mode=NumberSelectorMode.BOX
                )
            ),
            _field(CONF_USE_HORIZON): BooleanSelector(),
            _field(CONF_PARTIAL_SHADING): BooleanSelector(),
            vol.Optional(
                CONF_HORIZON_FILEPATH,
                default=defaults.get(CONF_HORIZON_FILEPATH, DEFAULT_HORIZON_FILEPATH),
            ): str,
            # 0.1.33.5: optional; empty = no shading. Suggested value (not a
            # default) so the field can be cleared again.
            vol.Optional(
                CONF_SHADING_FILEPATH,
                description={"suggested_value": defaults.get(CONF_SHADING_FILEPATH) or ""},
            ): str,
            vol.Optional(CONF_ADD_ANOTHER, default=add_another_default): BooleanSelector(),
        }
    )


def _normalize_array_input(user_input: dict[str, Any]) -> dict[str, Any]:
    array = {key: user_input.get(key) for key in PER_ARRAY_KEYS}
    filepath = str(array[CONF_HORIZON_FILEPATH] or "").strip()
    array[CONF_HORIZON_FILEPATH] = filepath or DEFAULT_HORIZON_FILEPATH
    array[CONF_SHADING_FILEPATH] = str(array.get(CONF_SHADING_FILEPATH) or "").strip()
    return array


async def _validate_array(hass: Any, array: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """0.1.33.5: check the shading file of one array; (errors, placeholders)."""
    path = array.get(CONF_SHADING_FILEPATH) or ""
    if not path:
        return {}, {}
    if array.get(CONF_TRACKING, "none") != "none":
        return {CONF_SHADING_FILEPATH: "shading_requires_fixed_array"}, {}
    from .shading_provider import validate_shading_file

    message = await hass.async_add_executor_job(validate_shading_file, path)
    if message is None:
        return {}, {}
    return {CONF_SHADING_FILEPATH: "invalid_shading_file"}, {"shading_error": message}


def _collapse_arrays(arrays: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse per-array dicts into scalar (one array) or list options."""
    if len(arrays) == 1:
        return dict(arrays[0])
    return {key: [array[key] for array in arrays] for key in PER_ARRAY_KEYS}


def _expand_arrays(entry: ConfigEntry) -> list[dict[str, Any]]:
    """Expand stored scalar-or-list options into one dict per array."""

    def _stored(key: str, default: Any) -> Any:
        return entry.options.get(key, entry.data.get(key, default))

    defaults = _array_defaults(0.0, 0.0)
    raw = {key: _stored(key, defaults[key]) for key in PER_ARRAY_KEYS}
    count = max(
        (len(value) for value in raw.values() if _is_sequence(value)), default=1
    )

    arrays = []
    for index in range(count):
        array = {}
        for key, value in raw.items():
            if _is_sequence(value):
                array[key] = value[index] if index < len(value) else value[0]
            else:
                array[key] = value
        arrays.append(array)
    return arrays


def _scalar(value: Any) -> Any:
    """Reduce a possibly-list legacy value to its first item."""
    if _is_sequence(value):
        return value[0] if value else None
    return value


LOCAL_KEYS = (
    CONF_LOCAL_WEATHER_ENTITY,
    CONF_LOCAL_IRRADIANCE_ENTITY,
    CONF_LOCAL_SNOW_DEPTH_ENTITY,
    CONF_LOCAL_FALLBACK_OPEN_METEO,
)


def _source_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=list(WEATHER_SOURCES),
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="weather_source",
        )
    )


def _local_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Entity pickers for the local weather source (0.1.33.2)."""

    def _optional(key: str) -> vol.Optional:
        value = defaults.get(key)
        if value:
            return vol.Optional(key, description={"suggested_value": value})
        return vol.Optional(key)

    weather_default = defaults.get(CONF_LOCAL_WEATHER_ENTITY)
    weather_key = (
        vol.Required(CONF_LOCAL_WEATHER_ENTITY, default=weather_default)
        if weather_default
        else vol.Required(CONF_LOCAL_WEATHER_ENTITY)
    )
    return vol.Schema(
        {
            weather_key: EntitySelector(EntitySelectorConfig(domain="weather")),
            _optional(CONF_LOCAL_IRRADIANCE_ENTITY): EntitySelector(
                EntitySelectorConfig(domain="sensor")
            ),
            _optional(CONF_LOCAL_SNOW_DEPTH_ENTITY): EntitySelector(
                EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(
                CONF_LOCAL_FALLBACK_OPEN_METEO,
                default=bool(defaults.get(CONF_LOCAL_FALLBACK_OPEN_METEO, False)),
            ): BooleanSelector(),
        }
    )


def _resolve_local_input(
    hass: Any, user_input: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Fill auto-discoverable entities and validate the local source.

    Empty irradiance / snow-depth fields are filled from the SwissWeather
    Fusion companions of the selected weather entity. The resolved entity
    ids are stored explicitly, so the running configuration never depends
    on discovery.
    """
    from .local_provider import (
        async_find_fusion_companions,
        validate_irradiance_entity,
        validate_weather_entity,
    )

    resolved = {key: user_input.get(key) for key in LOCAL_KEYS}
    resolved[CONF_LOCAL_FALLBACK_OPEN_METEO] = bool(
        resolved[CONF_LOCAL_FALLBACK_OPEN_METEO]
    )
    errors: dict[str, str] = {}
    weather = resolved[CONF_LOCAL_WEATHER_ENTITY]
    if error := validate_weather_entity(hass, weather):
        errors[CONF_LOCAL_WEATHER_ENTITY] = error
        return resolved, errors

    companions = async_find_fusion_companions(hass, weather)
    if not resolved[CONF_LOCAL_IRRADIANCE_ENTITY]:
        resolved[CONF_LOCAL_IRRADIANCE_ENTITY] = companions["irradiance"]
    if not resolved[CONF_LOCAL_SNOW_DEPTH_ENTITY]:
        resolved[CONF_LOCAL_SNOW_DEPTH_ENTITY] = companions["snow_depth"]
    if error := validate_irradiance_entity(hass, resolved[CONF_LOCAL_IRRADIANCE_ENTITY]):
        errors[CONF_LOCAL_IRRADIANCE_ENTITY] = error
    return resolved, errors


# --------------------------------------------------------------------------
# 0.1.34: learning hub (OMSF_v0_1_34_Learning_Spec.md §9)
# --------------------------------------------------------------------------

MAX_GROUP_NAME = 32
HUB_TITLE = "Solar forecast learning"


def _entries(hass: Any) -> list[ConfigEntry]:
    # Tolerates the minimal hass stand-in of the legacy wizard tests.
    manager = getattr(hass, "config_entries", None)
    return list(manager.async_entries(DOMAIN)) if manager is not None else []


def _array_entries(hass: Any) -> list[ConfigEntry]:
    from .runtime import is_hub_entry

    return [e for e in _entries(hass) if not is_hub_entry(e)]


def _hub_exists(hass: Any) -> bool:
    from .runtime import is_hub_entry

    return any(is_hub_entry(e) for e in _entries(hass))


def _station_schema(default: str | None) -> vol.Schema:
    key = (
        vol.Required(CONF_STATION_ENTITY, default=default)
        if default
        else vol.Required(CONF_STATION_ENTITY)
    )
    return vol.Schema({key: EntitySelector(EntitySelectorConfig(domain="sensor"))})


def _group_schema(hass: Any, defaults: dict[str, Any], add_another_default: bool) -> vol.Schema:
    options = [
        SelectOptionDict(value=e.entry_id, label=e.title or e.entry_id) for e in _array_entries(hass)
    ]
    name = defaults.get(CONF_GROUP_NAME)
    energy = defaults.get(CONF_GROUP_ENERGY_ENTITY)
    members = defaults.get(CONF_GROUP_MEMBERS) or []
    return vol.Schema(
        {
            (vol.Required(CONF_GROUP_NAME, default=name) if name else vol.Required(CONF_GROUP_NAME)): TextSelector(),
            (
                vol.Required(CONF_GROUP_ENERGY_ENTITY, default=energy)
                if energy
                else vol.Required(CONF_GROUP_ENERGY_ENTITY)
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Required(CONF_GROUP_MEMBERS, default=list(members)): SelectSelector(
                SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.LIST)
            ),
            vol.Optional(CONF_ADD_ANOTHER, default=add_another_default): BooleanSelector(),
        }
    )


def _validate_group(hass: Any, group: dict[str, Any], others: list[dict[str, Any]]) -> dict[str, str]:
    from .learning_hub import member_sources, validate_energy_entity

    errors: dict[str, str] = {}
    name = str(group.get(CONF_GROUP_NAME) or "").strip()
    if not name or len(name) > MAX_GROUP_NAME:
        errors[CONF_GROUP_NAME] = "group_name_invalid"
    elif any(str(o[CONF_GROUP_NAME]).casefold() == name.casefold() for o in others):
        errors[CONF_GROUP_NAME] = "group_name_duplicate"
    if error := validate_energy_entity(hass, group.get(CONF_GROUP_ENERGY_ENTITY)):
        errors[CONF_GROUP_ENERGY_ENTITY] = error
    members = list(group.get(CONF_GROUP_MEMBERS) or [])
    if not members:
        errors[CONF_GROUP_MEMBERS] = "members_required"
    else:
        taken = {m for o in others for m in o.get(CONF_GROUP_MEMBERS, [])}
        for member in members:
            problem, _, _ = member_sources(hass, member)
            if member in taken:
                problem = "member_twice"
            if problem:
                errors[CONF_GROUP_MEMBERS] = problem
                break
    return errors


def _normalize_group(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_GROUP_NAME: str(user_input.get(CONF_GROUP_NAME) or "").strip(),
        CONF_GROUP_ENERGY_ENTITY: user_input.get(CONF_GROUP_ENERGY_ENTITY),
        CONF_GROUP_MEMBERS: list(user_input.get(CONF_GROUP_MEMBERS) or []),
    }


class _HubSteps:
    """Station page + one page per inverter group; shared by the config
    flow and the hub's options flow."""

    _station: str | None
    _groups: list[dict[str, Any]]
    _stored_groups: list[dict[str, Any]]

    if TYPE_CHECKING:  # provided by ConfigFlow / OptionsFlow and the subclass
        hass: HomeAssistant

        def async_show_form(self, **kwargs: Any) -> ConfigFlowResult: ...

        def _finish_hub(self) -> ConfigFlowResult: ...

    async def _hub_station_step(self, step_id: str, user_input: dict[str, Any] | None, default: str | None):
        from .learning_hub import validate_station_entity

        errors: dict[str, str] = {}
        if user_input is not None:
            station = user_input.get(CONF_STATION_ENTITY)
            if error := validate_station_entity(self.hass, station):
                errors[CONF_STATION_ENTITY] = error
            else:
                self._station = station
                self._groups = []
                return await self.async_step_hub_group()
        return self.async_show_form(
            step_id=step_id,
            last_step=False,
            data_schema=_station_schema((user_input or {}).get(CONF_STATION_ENTITY) or default),
            errors=errors,
        )

    async def async_step_hub_group(self, user_input: dict[str, Any] | None = None):
        from .learning_hub import validate_groups

        errors: dict[str, str] = {}
        if user_input is not None:
            group = _normalize_group(user_input)
            errors = _validate_group(self.hass, group, self._groups)
            if not errors:
                self._groups.append(group)
                if user_input.get(CONF_ADD_ANOTHER):
                    return await self.async_step_hub_group()
                if problem := validate_groups(self.hass, self._groups):
                    self._groups.pop()
                    errors[CONF_GROUP_MEMBERS] = problem
                else:
                    return self._finish_hub()
        index = len(self._groups)
        if user_input is not None and errors:
            defaults = _normalize_group(user_input)
            add_default = bool(user_input.get(CONF_ADD_ANOTHER))
        elif index < len(self._stored_groups):
            defaults = self._stored_groups[index]
            add_default = index + 1 < len(self._stored_groups)
        else:
            defaults, add_default = {}, False
        return self.async_show_form(
            step_id="hub_group",
            data_schema=_group_schema(self.hass, defaults, add_default),
            description_placeholders={"group_number": str(index + 1)},
            errors=errors,
        )


class OpenMeteoSolarForecastFlowHandler(_HubSteps, ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Open-Meteo Solar Forecast."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._common: dict[str, Any] = {}
        self._local: dict[str, Any] = {}
        self._arrays: list[dict[str, Any]] = []
        self._station: str | None = None
        self._groups: list[dict[str, Any]] = []
        self._stored_groups: list[dict[str, Any]] = []

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OpenMeteoSolarForecastOptionFlowHandler:
        """Get the options flow for this handler."""
        return OpenMeteoSolarForecastOptionFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the common settings.

        0.1.34: once a PV array exists and no learning hub does, the first
        page is a menu: another PV array, or the learning hub.
        """
        if user_input is None and _array_entries(self.hass) and not _hub_exists(self.hass):
            return self.async_show_menu(step_id="user", menu_options=["common", "learning_hub"])
        return await self.async_step_common(user_input)

    async def async_step_common(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The common settings of a PV array entry (form id stays "user")."""
        if user_input is not None:
            self._common = user_input
            self._arrays = []
            if user_input.get(CONF_WEATHER_SOURCE) in LOCAL_BASED_SOURCES:
                return await self.async_step_local()
            return await self.async_step_array()

        return self.async_show_form(
            step_id="user",
            last_step=False,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NAME, default=self.hass.config.location_name
                    ): str,
                    vol.Optional(CONF_API_KEY, default=""): str,
                    vol.Required(
                        CONF_BASE_URL, default="https://api.open-meteo.com"
                    ): str,
                    vol.Optional(CONF_MODEL, default="best_match"): str,
                    vol.Required(
                        CONF_WEATHER_SOURCE, default=SOURCE_OPEN_METEO
                    ): _source_selector(),
                    vol.Required(CONF_INVERTER_POWER, default=0): vol.All(
                        NumberSelector(
                            NumberSelectorConfig(
                                min=0,
                                step=1,
                                mode=NumberSelectorMode.BOX,
                                unit_of_measurement="W",
                            )
                        ),
                        vol.Coerce(int),
                    ),
                    vol.Required(CONF_MAX_SNOWCOVER_DEPTH_CM, default=0.0): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            step="any",
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="cm",
                        )
                    ),
                }
            ),
        )

    async def async_step_learning_hub(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """0.1.34: the learning hub, page 1 (reference station)."""
        await self.async_set_unique_id(LEARNING_HUB_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        default = DEFAULT_STATION_ENTITY if self.hass.states.get(DEFAULT_STATION_ENTITY) else None
        return await self._hub_station_step("learning_hub", user_input, default)

    def _finish_hub(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title=HUB_TITLE,
            data={CONF_ENTRY_TYPE: ENTRY_TYPE_LEARNING_HUB},
            options={CONF_STATION_ENTITY: self._station, CONF_GROUPS: self._groups},
        )

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the local weather entities (0.1.33.2)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._local, errors = _resolve_local_input(self.hass, user_input)
            if not errors:
                return await self.async_step_array()
        return self.async_show_form(
            step_id="local",
            last_step=False,
            data_schema=_local_schema(self._local or user_input or {}),
            errors=errors,
        )

    async def async_step_array(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the settings of one PV array."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            add_another = user_input.get(CONF_ADD_ANOTHER, False)
            array = _normalize_array_input(user_input)
            errors, placeholders = await _validate_array(self.hass, array)
        if user_input is not None and not errors:
            self._arrays.append(array)
            if add_another:
                return await self.async_step_array()

            per_array = _collapse_arrays(self._arrays)
            source = self._common.get(CONF_WEATHER_SOURCE, SOURCE_OPEN_METEO)
            return self.async_create_entry(
                title=self._common[CONF_NAME],
                data={
                    CONF_LATITUDE: per_array[CONF_LATITUDE],
                    CONF_LONGITUDE: per_array[CONF_LONGITUDE],
                },
                options={
                    CONF_API_KEY: self._common[CONF_API_KEY],
                    CONF_BASE_URL: self._common[CONF_BASE_URL],
                    CONF_MODEL: self._common[CONF_MODEL],
                    CONF_WEATHER_SOURCE: source,
                    CONF_INVERTER_POWER: self._common[CONF_INVERTER_POWER],
                    CONF_MAX_SNOWCOVER_DEPTH_CM: self._common[
                        CONF_MAX_SNOWCOVER_DEPTH_CM
                    ],
                    **(self._local if source in LOCAL_BASED_SOURCES else {}),
                    **{key: per_array[key] for key in PER_ARRAY_KEYS},
                },
            )

        defaults = _array_defaults(
            self.hass.config.latitude, self.hass.config.longitude
        )
        if self._arrays:
            defaults = dict(self._arrays[-1])
        if errors:
            defaults = {**defaults, **array}
        return self.async_show_form(
            step_id="array",
            data_schema=_array_schema(defaults, add_another_default=False),
            description_placeholders={
                "array_number": str(len(self._arrays) + 1),
                "shading_error": placeholders.get("shading_error", ""),
            },
            errors=errors,
        )


class OpenMeteoSolarForecastOptionFlowHandler(_HubSteps, OptionsFlow):
    """Handle options."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._common: dict[str, Any] = {}
        self._local: dict[str, Any] = {}
        self._arrays: list[dict[str, Any]] = []
        self._stored_arrays: list[dict[str, Any]] = []
        self._station: str | None = None
        self._groups: list[dict[str, Any]] = []
        self._stored_groups: list[dict[str, Any]] = []

    async def async_step_hub_station(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """0.1.34: hub options, page 1."""
        options = self.config_entry.options
        self._stored_groups = [dict(g) for g in options.get(CONF_GROUPS) or []]
        return await self._hub_station_step("hub_station", user_input, options.get(CONF_STATION_ENTITY))

    def _finish_hub(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title="", data={CONF_STATION_ENTITY: self._station, CONF_GROUPS: self._groups}
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the common settings."""
        from .runtime import is_hub_entry

        if is_hub_entry(self.config_entry):
            return await self.async_step_hub_station(user_input)
        if user_input is not None:
            self._common = user_input
            self._arrays = []
            self._stored_arrays = _expand_arrays(self.config_entry)
            if user_input.get(CONF_WEATHER_SOURCE) in LOCAL_BASED_SOURCES:
                return await self.async_step_local()
            return await self.async_step_array()

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            last_step=False,
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_API_KEY,
                        description={
                            "suggested_value": options.get(CONF_API_KEY, "")
                        },
                    ): str,
                    vol.Required(
                        CONF_BASE_URL, default=options[CONF_BASE_URL]
                    ): str,
                    vol.Optional(
                        CONF_MODEL, default=options.get(CONF_MODEL, "best_match")
                    ): str,
                    vol.Required(
                        CONF_WEATHER_SOURCE,
                        default=options.get(CONF_WEATHER_SOURCE, SOURCE_OPEN_METEO),
                    ): _source_selector(),
                    vol.Required(
                        CONF_INVERTER_POWER,
                        default=options.get(CONF_INVERTER_POWER, 0),
                    ): vol.All(
                        NumberSelector(
                            NumberSelectorConfig(
                                min=0,
                                step=1,
                                mode=NumberSelectorMode.BOX,
                                unit_of_measurement="W",
                            )
                        ),
                        vol.Coerce(int),
                    ),
                    vol.Required(
                        CONF_MAX_SNOWCOVER_DEPTH_CM,
                        default=_scalar(
                            options.get(CONF_MAX_SNOWCOVER_DEPTH_CM, 0.0)
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            step="any",
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="cm",
                        )
                    ),
                }
            ),
        )

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the local weather entities (0.1.33.2)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._local, errors = _resolve_local_input(self.hass, user_input)
            if not errors:
                return await self.async_step_array()
        stored = {key: self.config_entry.options.get(key) for key in LOCAL_KEYS}
        return self.async_show_form(
            step_id="local",
            last_step=False,
            data_schema=_local_schema(self._local or user_input or stored),
            errors=errors,
        )

    async def async_step_array(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the settings of one PV array."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            add_another = user_input.get(CONF_ADD_ANOTHER, False)
            array = _normalize_array_input(user_input)
            errors, placeholders = await _validate_array(self.hass, array)
        if user_input is not None and not errors:
            self._arrays.append(array)
            if add_another:
                return await self.async_step_array()

            per_array = _collapse_arrays(self._arrays)
            source = self._common.get(CONF_WEATHER_SOURCE, SOURCE_OPEN_METEO)
            return self.async_create_entry(
                title="",
                data={
                    CONF_API_KEY: self._common.get(CONF_API_KEY),
                    CONF_BASE_URL: self._common[CONF_BASE_URL],
                    CONF_MODEL: self._common[CONF_MODEL],
                    CONF_WEATHER_SOURCE: source,
                    CONF_INVERTER_POWER: self._common[CONF_INVERTER_POWER],
                    CONF_MAX_SNOWCOVER_DEPTH_CM: self._common[
                        CONF_MAX_SNOWCOVER_DEPTH_CM
                    ],
                    **(self._local if source in LOCAL_BASED_SOURCES else {}),
                    **{key: per_array[key] for key in PER_ARRAY_KEYS},
                },
            )

        index = len(self._arrays)
        if index < len(self._stored_arrays):
            defaults = self._stored_arrays[index]
        else:
            defaults = dict(self._arrays[-1])
        # Default to walking through all previously configured arrays;
        # unchecking the box early drops the remaining arrays.
        add_another_default = index + 1 < len(self._stored_arrays)
        if errors:
            defaults = {**defaults, **array}
            add_another_default = bool(user_input.get(CONF_ADD_ANOTHER, False))
        return self.async_show_form(
            step_id="array",
            data_schema=_array_schema(defaults, add_another_default),
            description_placeholders={
                "array_number": str(index + 1),
                "shading_error": placeholders.get("shading_error", ""),
            },
            errors=errors,
        )
