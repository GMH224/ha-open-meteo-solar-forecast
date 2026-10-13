"""Entities of the learning hub (0.1.34): the transparency layer.

Every number the learner uses to decide is visible here or in the hub's
diagnostics; see OMSF_v0_1_34_Learning_Spec.md §10.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from . import learning as L
from .const import ATTR_LEARNING_HOURS, DOMAIN
from .learning_hub import LearningHub

HUB_DEVICE_NAME = "Solar forecast learning"
ENTITY_PREFIX = "solar_forecast_learning"


def hub_device_info(entry_id: str) -> DeviceInfo:
    return DeviceInfo(
        entry_type=DeviceEntryType.SERVICE,
        identifiers={(DOMAIN, entry_id)},
        manufacturer="Open-Meteo Solar Forecast",
        name=HUB_DEVICE_NAME,
    )


class HubEntity(Entity):
    """Pushed by the hub after every run and every owner action."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: LearningHub, key: str, group: str | None = None) -> None:
        self.hub = hub
        self.group = group
        suffix = f"{slugify(group)}_{key}" if group else key
        self._attr_unique_id = f"{hub.entry.entry_id}_{suffix}"
        self._attr_device_info = hub_device_info(hub.entry.entry_id)
        self._attr_translation_key = key
        if group:
            self._attr_translation_placeholders = {"group": group}
        self.entity_id = f"{self.platform_domain}.{ENTITY_PREFIX}_{suffix}"

    platform_domain = "sensor"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.add_listener(self.async_write_ha_state))

    def group_info(self) -> L.GroupInfo | None:
        return next((g for g in self.hub.groups if g.name == self.group), None)


# --------------------------------------------------------------------------
# Sensors
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class HubSensorDescription(SensorEntityDescription):
    value: Callable[[LearningHub, str | None], Any]
    attributes: Callable[[LearningHub, str | None], dict[str, Any]] | None = None
    per_group: bool = False


def _gate(hub: LearningHub) -> dict[str, Any]:
    return hub.data.d.get("gate") or {}


def _state_attributes(hub: LearningHub, _group: str | None) -> dict[str, Any]:
    state = hub.data.d["state"]
    return {
        "reason": state.get("reason"),
        "since": state.get("since"),
        "algorithm": L.ALGORITHM_VERSION,
        "apply_switch": hub.data.apply_switch,
        "frozen": hub.data.frozen,
        "configuration_problem": hub.config_problem,
        "store_fault": hub.store_fault,
        "groups": [{"name": g.name, "kwp": round(g.kwp, 2), "members": list(g.members)} for g in hub.groups],
        "station": hub.station_status,
    }


def _improvement(hub: LearningHub, _group: str | None) -> float | None:
    value = _gate(hub).get("improvement")
    return None if value is None else round(value * 100, 1)


def _gate_attributes(hub: LearningHub, _group: str | None) -> dict[str, Any]:
    gate = _gate(hub)
    return {
        "rmse_raw_w_m2": gate.get("rmse_raw_w_m2"),
        "rmse_corrected_w_m2": gate.get("rmse_corrected_w_m2"),
        "days": gate.get("days"),
        "hours": gate.get("hours"),
        "passed": gate.get("passed"),
        "reasons": gate.get("reasons"),
        "evaluated": gate.get("evaluated"),
        "required_improvement_percent": L.GATE_MIN_IMPROVEMENT * 100,
        "required_days": L.GATE_MIN_DAYS,
    }


def _group_gate(hub: LearningHub, group: str | None) -> dict[str, Any]:
    result: dict[str, Any] = (_gate(hub).get("groups") or {}).get(group or "", {})
    return result


def _correction(day: str) -> Callable[[LearningHub, str | None], Any]:
    def value(hub: LearningHub, group: str | None) -> Any:
        info = next((g for g in hub.groups if g.name == group), None)
        return None if info is None else hub.group_correction(info, day)["difference_kwh"]

    return value


def _correction_attributes(day: str) -> Callable[[LearningHub, str | None], dict[str, Any]]:
    def attributes(hub: LearningHub, group: str | None) -> dict[str, Any]:
        info = next((g for g in hub.groups if g.name == group), None)
        if info is None:
            return {}
        result = hub.group_correction(info, day)
        result["applied"] = hub.applies
        return result

    return attributes


def _stage2(hub: LearningHub, group: str | None) -> dict[str, Any]:
    result: dict[str, Any] = (hub.data.d["stage2"].get("groups") or {}).get(group or "", {})
    return result


def _calibration_value(hub: LearningHub, group: str | None) -> float | None:
    item = _stage2(hub, group).get("overcast")
    return None if item is None else item["factor"]


def _calibration_attributes(hub: LearningHub, group: str | None) -> dict[str, Any]:
    bins = _stage2(hub, group)
    clear = bins.get("clear") or {}
    suggestion = None
    if clear.get("hours", 0) >= 40:
        suggestion = round((clear.get("ratio", 1.0) - 1.0) * 100, 1)
    return {
        "bins": bins,
        "computed": hub.data.d["stage2"].get("computed"),
        "suggested_efficiency_change_percent": suggestion,
        "note": "a clear-sky ratio settling away from 1 means the efficiency setting should change",
    }


def _station_r(hub: LearningHub, _group: str | None) -> float | None:
    rep = hub.data.d.get("representativeness") or {}
    return rep.get("r")


def _station_attributes(hub: LearningHub, _group: str | None) -> dict[str, Any]:
    rep = dict(hub.data.d.get("representativeness") or {})
    excluded = [
        (day, item["status"]) for day, item in sorted(hub.data.d["days"].items()) if item["status"] != L.DAY_OK
    ]
    rep["last_excluded_day"] = excluded[-1] if excluded else None
    rep["required_r"] = L.REPRESENTATIVE_MIN_R
    return rep


def _albedo(hub: LearningHub, _group: str | None) -> float | None:
    for key in sorted(hub.data.d["station_hours"], reverse=True)[:48]:
        meas = hub.data.d["station_hours"][key]
        ghi, refl = meas[L.H["ghi"]], meas[L.H["refl"]]
        if ghi is not None and refl is not None and ghi > L.SNOW_MIN_GHI:
            return round(float(refl) / float(ghi), 3)
    return None


def _albedo_attributes(hub: LearningHub, group: str | None) -> dict[str, Any]:
    value = _albedo(hub, group)
    return {"snow_likely": None if value is None else value > L.SNOW_ALBEDO, "threshold": L.SNOW_ALBEDO}


def _next_hours_value(hub: LearningHub, _group: str | None) -> int:
    return sum(1 for item in hub.next_hours if item["kt_corrected"] != item["kt"] or item["kd_corrected"] != item["kd"])


def _next_hours_attributes(hub: LearningHub, _group: str | None) -> dict[str, Any]:
    return {ATTR_LEARNING_HOURS: hub.next_hours, "applied": hub.applies}


def _evidence_value(hub: LearningHub, _group: str | None) -> int | None:
    return hub.evidence_info.get("hours")


def _evidence_attributes(hub: LearningHub, _group: str | None) -> dict[str, Any]:
    return dict(hub.evidence_info)


def _last_run(hub: LearningHub, _group: str | None) -> datetime | None:
    value = hub.data.d.get("last_nightly")
    return dt_util.parse_datetime(value) if value else None


def _last_run_attributes(hub: LearningHub, _group: str | None) -> dict[str, Any]:
    return {"nightly": hub.last_nightly, "hourly": hub.last_hourly, "store_bytes": hub.store_size}


SENSORS: tuple[HubSensorDescription, ...] = (
    HubSensorDescription(
        key="state", translation_key="state", device_class=SensorDeviceClass.ENUM,
        options=list(L.STATES), value=lambda hub, _g: hub.state, attributes=_state_attributes,
    ),
    HubSensorDescription(
        key="evidence", translation_key="evidence", state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="h", value=_evidence_value, attributes=_evidence_attributes,
    ),
    HubSensorDescription(
        key="irradiance_improvement", translation_key="irradiance_improvement",
        state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=PERCENTAGE,
        value=_improvement, attributes=_gate_attributes,
    ),
    HubSensorDescription(
        key="next_hours", translation_key="next_hours", state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="h", value=_next_hours_value, attributes=_next_hours_attributes,
    ),
    HubSensorDescription(
        key="station_correlation", translation_key="station_correlation",
        state_class=SensorStateClass.MEASUREMENT, value=_station_r, attributes=_station_attributes,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    HubSensorDescription(
        key="station_albedo", translation_key="station_albedo", state_class=SensorStateClass.MEASUREMENT,
        value=_albedo, attributes=_albedo_attributes, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    HubSensorDescription(
        key="last_run", translation_key="last_run", device_class=SensorDeviceClass.TIMESTAMP,
        value=_last_run, attributes=_last_run_attributes, entity_category=EntityCategory.DIAGNOSTIC,
    ),
    HubSensorDescription(
        key="energy_error", translation_key="energy_error", per_group=True,
        state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value=lambda hub, g: _group_gate(hub, g).get("mae_corrected_kwh"),
        attributes=lambda hub, g: _group_gate(hub, g),
    ),
    HubSensorDescription(
        key="correction_today", translation_key="correction_today", per_group=True,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, state_class=SensorStateClass.MEASUREMENT,
        value=_correction("today"), attributes=_correction_attributes("today"),
    ),
    HubSensorDescription(
        key="correction_tomorrow", translation_key="correction_tomorrow", per_group=True,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, state_class=SensorStateClass.MEASUREMENT,
        value=_correction("tomorrow"), attributes=_correction_attributes("tomorrow"),
    ),
    HubSensorDescription(
        key="calibration", translation_key="calibration", per_group=True,
        state_class=SensorStateClass.MEASUREMENT, value=_calibration_value, attributes=_calibration_attributes,
    ),
)


class HubSensor(HubEntity, SensorEntity):
    entity_description: HubSensorDescription

    def __init__(self, hub: LearningHub, description: HubSensorDescription, group: str | None = None) -> None:
        self.entity_description = description
        super().__init__(hub, description.key, group)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.hub, self.group)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(self.hub, self.group)


def hub_sensors(hub: LearningHub) -> list[HubSensor]:
    entities: list[HubSensor] = []
    for description in SENSORS:
        if description.per_group:
            entities += [HubSensor(hub, description, group.name) for group in hub.groups]
        else:
            entities.append(HubSensor(hub, description))
    return entities


# --------------------------------------------------------------------------
# Switches
# --------------------------------------------------------------------------


class ApplyLearningSwitch(HubEntity, SwitchEntity):
    """Owner's authority: corrections act only when this is on AND the
    skill gate has passed (state ``applied``)."""

    platform_domain = "switch"

    def __init__(self, hub: LearningHub) -> None:
        super().__init__(hub, "apply_learning")

    @property
    def is_on(self) -> bool:
        return self.hub.data.apply_switch

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"state": self.hub.state, "changed": self.hub.data.d["settings"].get("apply_changed")}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hub.async_set_apply(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hub.async_set_apply(False)


class FreezeLearningSwitch(HubEntity, SwitchEntity):
    """Frozen: evidence is still recorded, the model no longer changes."""

    platform_domain = "switch"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub: LearningHub) -> None:
        super().__init__(hub, "freeze_learning")

    @property
    def is_on(self) -> bool:
        return self.hub.data.frozen

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"intervals": self.hub.data.d["freeze"]}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hub.async_set_frozen(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hub.async_set_frozen(False)

