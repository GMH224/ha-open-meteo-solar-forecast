"""Home Assistant glue for the learning engine (0.1.34).

Design record: ``OMSF_v0_1_34_Learning_Spec.md``. The algorithms live in
the pure module ``learning.py``; this module only

* reads Home Assistant state (station sensor, Fusion series, weather
  forecast, recorder statistics of the production counters),
* schedules the hourly (xx:25) and nightly (03:30 local) runs,
* owns the shared store ``.storage/open_meteo_solar_forecast.learning``,
* hands corrections to the array coordinators, and
* writes the export files.

Fail-safe rule: nothing here may stop or delay a forecast. Every failure
is caught, logged, recorded as an event and leaves the forecast raw.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_LATITUDE as CONF_LATITUDE_KEY,
    CONF_LONGITUDE as CONF_LONGITUDE_KEY,
    EVENT_HOMEASSISTANT_STARTED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import learning as L
from .const import (
    CONF_GROUP_ENERGY_ENTITY,
    CONF_GROUP_MEMBERS,
    CONF_GROUP_NAME,
    CONF_GROUPS,
    CONF_LOCAL_IRRADIANCE_ENTITY,
    CONF_LOCAL_WEATHER_ENTITY,
    CONF_MODULES_POWER,
    CONF_STATION_ENTITY,
    CONF_WEATHER_SOURCE,
    DOMAIN,
    LEARNING_EXPORT_SUBDIR,
    LEARNING_STORAGE_KEY,
    LEARNING_STORAGE_VERSION,
    LOCAL_BASED_SOURCES,
    LOGGER,
)
from .errors import sanitize_error
from .runtime import EntryReport, GroupConfig, array_coordinator, is_hub_entry, loaded_hub
from .local_source import HourRecord, LocalSnapshot, _to_float, parse_irradiance_series, parse_utc

HOURLY_MINUTE = 25
NIGHTLY_HOUR, NIGHTLY_MINUTE = 3, 30
STARTUP_DELAY_S = 60
SERVICE_TIMEOUT_S = 20
MODEL_DAYS = 10
PRODUCTION_REFRESH_DAYS = 10
DECIDE_LOOKBACK_DAYS = 30
NEXT_HOURS = 36
STATION_TIME_ATTRIBUTES = (
    "measured_at", "measurement_time", "observation_time", "reference_time", "time", "timestamp",
)
STATION_STALE = timedelta(minutes=60)
STATION_BACKFILL_FIRST = timedelta(days=10)
STATION_BACKFILL_RESTART = timedelta(hours=6)
YEARLY_EXPORTS = ("archive", "station_hours", "production")


def get_hub(hass: HomeAssistant) -> LearningHub | None:
    """The loaded learning hub (``ConfigEntry.runtime_data``), else None."""
    return loaded_hub(hass)


def _entry_value(entry: ConfigEntry, key: str) -> Any:
    return entry.options.get(key, entry.data.get(key))


def _first(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _sum_power(value: Any) -> float:
    values = value if isinstance(value, (list, tuple)) else [value]
    total = 0.0
    for item in values:
        number = _to_float(item)
        if number is not None and number > 0:
            total += number
    return total


# --------------------------------------------------------------------------
# Validation helpers (also used by the config flow)
# --------------------------------------------------------------------------


def station_sample_from_state(
    state: State | None, now: datetime | None
) -> tuple[L.StationSample | None, str]:
    """Parse the Fusion reference-station sensor. Returns (sample, note).

    The measurement time is the sensor state (a timestamp); some versions
    may carry it as an attribute instead; as a last resort it is derived
    from ``age_minutes`` and rounded to the 10-minute grid. ``now`` given:
    records older than 60 minutes are ignored (Fusion marks them unknown).
    """
    if state is None:
        return None, "missing"
    if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None, state.state
    attrs = state.attributes
    end = parse_utc(state.state)
    source = "state"
    if end is None:
        for key in STATION_TIME_ATTRIBUTES:
            end = parse_utc(attrs.get(key))
            if end is not None:
                source = key
                break
    if end is None:
        age = _to_float(attrs.get("age_minutes"))
        if age is not None and 0 <= age <= 24 * 60:
            raw = state.last_updated - timedelta(minutes=age)
            ts = round(raw.timestamp() / 600.0) * 600
            end = datetime.fromtimestamp(ts, timezone.utc)
            source = "age_minutes"
    if end is None:
        return None, "no measurement time"
    if now is not None and now - end > STATION_STALE:
        return None, "older than 60 min"
    sample = L.parse_station_values(attrs.get("values"), end)
    if sample is None:
        return None, "no usable global radiation"
    return sample, source


def validate_station_entity(hass: HomeAssistant, entity_id: str | None) -> str | None:
    if not entity_id:
        return "station_invalid"
    state = hass.states.get(entity_id)
    if state is None:
        return "station_invalid"
    values = state.attributes.get("values")
    if not isinstance(values, Mapping) or "gre000z0" not in values:
        return "station_invalid"
    return None


ENERGY_UNITS = {"Wh", "kWh", "MWh"}


def validate_energy_entity(hass: HomeAssistant, entity_id: str | None) -> str | None:
    if not entity_id:
        return "energy_invalid"
    state = hass.states.get(entity_id)
    if state is None:
        return "energy_invalid"
    if state.attributes.get("unit_of_measurement") not in ENERGY_UNITS:
        return "energy_invalid"
    if state.attributes.get("state_class") not in ("total", "total_increasing"):
        return "energy_invalid"
    return None


def member_sources(hass: HomeAssistant, entry_id: str) -> tuple[str | None, str | None, str | None]:
    """(problem, weather entity, irradiance entity) of an array entry."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or is_hub_entry(entry):
        return "member_missing", None, None
    if entry.options.get(CONF_WEATHER_SOURCE) not in LOCAL_BASED_SOURCES:
        return "member_not_local", None, None
    return None, entry.options.get(CONF_LOCAL_WEATHER_ENTITY), entry.options.get(CONF_LOCAL_IRRADIANCE_ENTITY)


def validate_groups(hass: HomeAssistant, groups: Sequence[Mapping[str, Any]]) -> str | None:
    """Consistency across all groups: members once, one weather source."""
    seen: set[str] = set()
    sources: set[tuple[str | None, str | None]] = set()
    for group in groups:
        for member in group.get(CONF_GROUP_MEMBERS) or []:
            if member in seen:
                return "member_twice"
            seen.add(member)
            problem, weather, irradiance = member_sources(hass, member)
            if problem:
                return problem
            sources.add((weather, irradiance))
    if len(sources) > 1:
        return "member_source_mismatch"
    return None


# --------------------------------------------------------------------------
# The hub
# --------------------------------------------------------------------------


class HubInputError(Exception):
    """An input the hourly run needs is not available."""


class LearningHub:
    """One per Home Assistant instance (single config entry)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        self.hass = hass
        self.entry = entry
        options = {**entry.data, **entry.options}
        self.station_entity: str = options.get(CONF_STATION_ENTITY) or ""
        self.group_configs: list[GroupConfig] = [
            GroupConfig(
                name=str(g.get(CONF_GROUP_NAME, "")),
                energy_entity=str(g.get(CONF_GROUP_ENERGY_ENTITY, "")),
                members=[str(m) for m in g.get(CONF_GROUP_MEMBERS) or []],
            )
            for g in options.get(CONF_GROUPS) or []
            if isinstance(g, Mapping)
        ]
        self.data = L.LearningData()
        self._store: Store[dict[str, Any]] = Store(hass, LEARNING_STORAGE_VERSION, LEARNING_STORAGE_KEY)
        self._lock = asyncio.Lock()
        self._unsubs: list[Callable[[], None]] = []
        self._listeners: list[Callable[[], None]] = []
        self.tz = ZoneInfo(hass.config.time_zone)
        self.latitude = float(hass.config.latitude)
        self.longitude = float(hass.config.longitude)
        self.groups: list[L.GroupInfo] = []
        self.weather_entity: str | None = None
        self.irradiance_entity: str | None = None
        self.config_problem: str | None = None
        self.store_fault: str | None = None
        self.station_status: dict[str, Any] = {}
        self.next_hours: list[dict[str, Any]] = []
        self.evidence_info: dict[str, Any] = {}
        self.entry_reports: dict[str, EntryReport] = {}
        self.last_hourly: dict[str, Any] = {}
        self.last_nightly: dict[str, Any] = {}
        self.last_startup: dict[str, Any] = {}
        self.store_size: int | None = None

    # -- lifecycle -----------------------------------------------------------

    async def async_setup(self) -> None:
        await self._async_load_store()
        self.resolve_members()
        now = dt_util.utcnow()
        if self.data.d["created"] is None:
            self.data.d["created"] = now.isoformat()
            self.data.event(now, "created", "learning store created (neutral start)")
        self._recompute_state(now, refresh=False)
        if not self.data.d["state"].get("since"):
            self.data.d["state"]["since"] = now.isoformat()
        if self.station_entity:
            self._unsubs.append(
                async_track_state_change_event(self.hass, [self.station_entity], self._on_station_event)
            )
        self._unsubs.append(
            async_track_time_change(self.hass, self._hourly_cb, minute=HOURLY_MINUTE, second=0)
        )
        self._unsubs.append(
            async_track_time_change(self.hass, self._nightly_cb, hour=NIGHTLY_HOUR, minute=NIGHTLY_MINUTE, second=0)
        )
        if self.hass.state is CoreState.running:
            self._unsubs.append(async_call_later(self.hass, STARTUP_DELAY_S, self._startup_cb))
        else:
            self._unsubs.append(
                self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._started_cb)
            )
        self._save()

    async def async_unload(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:  # noqa: BLE001 - one-shot listeners may have fired
                pass
        self._unsubs.clear()
        await self._store.async_save(self.data.to_json())

    @callback
    def _started_cb(self, _event: Event) -> None:
        self._unsubs.append(async_call_later(self.hass, STARTUP_DELAY_S, self._startup_cb))

    @callback
    def _startup_cb(self, _now: datetime) -> None:
        self.entry.async_create_background_task(
            self.hass, self.async_run_startup(), "open_meteo_solar_forecast learning startup"
        )

    async def async_run_startup(self) -> None:
        """Backfill the station from the recorder, then run the jobs that
        are due (the hourly run always; the nightly run if the last one is
        older than 20 h)."""
        now = dt_util.utcnow()
        first = not self.data.d["station_hours"]
        report: dict[str, Any] = {"started": now.isoformat()}
        await self._step(report, "station_backfill", self._async_backfill_station(
            now, STATION_BACKFILL_FIRST if first else STATION_BACKFILL_RESTART))
        self.last_startup = report  # kept apart: the hourly run replaces its own report
        await self.async_run_hourly(now)
        last = parse_utc(self.data.d.get("last_nightly"))
        if last is None or now - last > timedelta(hours=20):
            await self.async_run_nightly(now)

    @callback
    def _hourly_cb(self, _now: datetime) -> None:
        # Owned by the entry: cancelled when the hub is unloaded.
        self.entry.async_create_background_task(
            self.hass, self.async_run_hourly(), "open_meteo_solar_forecast learning hourly run"
        )

    @callback
    def _nightly_cb(self, _now: datetime) -> None:
        self.entry.async_create_background_task(
            self.hass, self.async_run_nightly(), "open_meteo_solar_forecast learning nightly run"
        )

    # -- listeners for entities ------------------------------------------------

    @callback
    def add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(update)

        def remove() -> None:
            if update in self._listeners:
                self._listeners.remove(update)

        return remove

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    # -- store -------------------------------------------------------------------

    def _save(self) -> None:
        self._store.async_delay_save(self.data.to_json, 60)

    async def _async_load_store(self) -> None:
        now = dt_util.utcnow()
        try:
            raw = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - corrupt JSON must not block setup
            await self._async_quarantine(now, f"unreadable: {sanitize_error(err)}")
            return
        if raw is None:
            return
        try:
            self.data = L.LearningData.from_json(raw)
        except L.LearningDataError as err:
            await self._async_quarantine(now, f"malformed: {sanitize_error(err)}")
            return
        if self.data.migrated_from is not None:
            self.data.event(now, "migrated", f"store {self.data.migrated_from} -> {L.STORE_SCHEMA_VERSION}: "
                            "archived forecasts dropped (no total cloud); measurements and production kept")
        if self.data.d.get("algorithm") != L.ALGORITHM_VERSION:
            old = self.data.d.get("algorithm")
            self.data.d["algorithm"] = L.ALGORITHM_VERSION
            self.data.d["gate"] = None
            self.data.event(now, "algorithm_changed",
                            f"algorithm {old} -> {L.ALGORITHM_VERSION}: skill gate restarts")

    async def _async_quarantine(self, now: datetime, reason: str) -> None:
        path = self.hass.config.path(".storage", LEARNING_STORAGE_KEY)
        target = f"{path}.corrupt-{now.strftime('%Y%m%dT%H%M%S')}"

        def copy() -> bool:
            if os.path.isfile(path):
                shutil.copy2(path, target)
                return True
            return False

        try:
            copied = await self.hass.async_add_executor_job(copy)
        except OSError:
            copied = False
        self.data = L.LearningData()
        self.store_fault = reason
        self.data.event(now, "store_corrupt",
                        f"learning store {reason}; " + ("copy kept as " + os.path.basename(target) if copied else "no copy")
                        + "; starting neutral")
        LOGGER.warning("Learning store %s; starting neutral", reason)

    # -- configuration -------------------------------------------------------------

    def resolve_members(self) -> None:
        """Group membership, kWp, weather inputs and location from the
        member entries (re-checked before every run)."""
        groups: list[L.GroupInfo] = []
        problem = None
        sources: set[tuple[str | None, str | None]] = set()
        location = None
        for config in self.group_configs:
            members = tuple(config["members"])
            kwp = 0.0
            for member in members:
                issue, weather, irradiance = member_sources(self.hass, member)
                if issue:
                    problem = problem or f"{issue}: {member}"
                    continue
                entry = self.hass.config_entries.async_get_entry(member)
                if entry is None:
                    continue
                kwp += _sum_power(entry.options.get(CONF_MODULES_POWER)) / 1000.0
                sources.add((weather, irradiance))
                if location is None:
                    lat = _to_float(_first(_entry_value(entry, CONF_LATITUDE_KEY)))
                    lon = _to_float(_first(_entry_value(entry, CONF_LONGITUDE_KEY)))
                    if lat is not None and lon is not None:
                        location = (lat, lon)
            groups.append(L.GroupInfo(config["name"], kwp, members))
        if len(sources) > 1:
            problem = problem or "member entries use different weather sources"
        if not groups:
            problem = problem or "no inverter group configured"
        self.groups = groups
        if sources:
            self.weather_entity, self.irradiance_entity = next(iter(sorted(sources, key=str)))
        if location is not None:
            self.latitude, self.longitude = location
        self.config_problem = problem

    def group_for(self, entry_id: str) -> L.GroupInfo | None:
        for group in self.groups:
            if entry_id in group.members:
                return group
        return None

    def energy_entity(self, group: str) -> str | None:
        for config in self.group_configs:
            if config["name"] == group:
                return config["energy_entity"]
        return None

    # -- state -----------------------------------------------------------------------

    @property
    def state(self) -> str:
        return str(self.data.d["state"]["state"])

    @property
    def applies(self) -> bool:
        """True only in state ``applied`` (gate passed and switch on)."""
        return self.state == L.STATE_APPLIED

    def _recompute_state(self, now: datetime, refresh: bool = True) -> None:
        rep = self.data.d.get("representativeness") or {}
        representative = rep.get("representative", True)
        ever_passed = any(
            item.get("passed") and item.get("algorithm") == L.ALGORITHM_VERSION
            for item in self.data.d["gate_history"]
        )
        state, reason = L.derive_state(self.data.d["gate"], self.data.apply_switch, representative, ever_passed)
        if self.config_problem:
            state, reason = L.STATE_PAUSED, f"configuration: {self.config_problem}"
        current = self.data.d["state"]
        before_applies = current.get("state") == L.STATE_APPLIED
        if current.get("state") != state or current.get("reason") != reason:
            if current.get("state") != state:
                self.data.event(now, "state", f"{current.get('state')} -> {state}: {reason}")
                current["since"] = now.isoformat()
            current["state"] = state
            current["reason"] = reason
        if refresh and before_applies != (state == L.STATE_APPLIED):
            self._refresh_members()

    def _refresh_members(self) -> None:
        for group in self.groups:
            for member in group.members:
                coordinator = array_coordinator(self.hass, member)
                if coordinator is not None and coordinator.config_entry is not None:
                    # Owned by the member entry it refreshes.
                    coordinator.config_entry.async_create_background_task(
                        self.hass, coordinator.async_request_refresh(),
                        "open_meteo_solar_forecast learning member refresh",
                    )

    # -- owner controls -------------------------------------------------------------

    async def async_set_apply(self, value: bool) -> None:
        # Owner actions are serialised with the hourly and nightly runs.
        async with self._lock:
            now = dt_util.utcnow()
            changed = self.data.set_apply(value, now)
            if changed:
                self._recompute_state(now)
                self._save()
        if changed:
            self._notify()

    async def async_set_frozen(self, value: bool) -> None:
        async with self._lock:
            changed = self.data.set_frozen(value, dt_util.utcnow())
            if changed:
                self._save()
        if changed:
            self._notify()

    async def async_reset(self, scope: str) -> None:
        """Owner reset (service, confirmation enforced by the schema)."""
        async with self._lock:
            now = dt_util.utcnow()
            if scope == "all":
                settings = dict(self.data.d["settings"])
                events = list(self.data.d["events"])
                self.data = L.LearningData()
                self.data.d["created"] = now.isoformat()
                self.data.d["events"] = events
                settings["apply"] = False
                self.data.d["settings"] = settings
            else:
                ts = int(now.timestamp())
                self.data.d["resets"].append([ts, scope])
                if scope == "stage2":
                    self.data.d["stage2"]["groups"] = {}
                    self.data.d["stage2"]["computed"] = now.isoformat()
                # Past skill records describe the old model: drop them.
                self.data.d["issue6"] = {}
                self.data.d["entry_days"] = {}
            self.data.d["gate"] = None
            self.data.d["gate_history"] = []
            self.data.event(now, "reset", f"owner reset ({scope})")
            self._recompute_state(now)
            self._save()
        self._notify()

    # -- station ----------------------------------------------------------------------

    @callback
    def _on_station_event(self, event: Event[EventStateChangedData]) -> None:
        self._ingest_station(event.data.get("new_state"), dt_util.utcnow())

    def _ingest_station(self, state: State | None, now: datetime | None) -> bool:
        """The latest record (state + ``values``) and, from Fusion 0.3.5,
        every record of the last hours (``recent_values``). ``now`` None:
        replay from history (no staleness check; the list is judged
        against the state's own update time)."""
        added, sample, note, recent_total, recent_rejected = self._merge_station_state(state, now)
        status: dict[str, Any] = {
            "entity_id": self.station_entity,
            "note": note,
            "last_sample_end": None if sample is None else L.key_to_dt(sample.end).isoformat(),
            "checked": (now or dt_util.utcnow()).isoformat(),
            "recent_values": recent_total,
        }
        if recent_rejected:
            status["recent_values_rejected"] = recent_rejected
        self.station_status = status
        return added > 0

    def _merge_station_state(
        self, state: State | None, now: datetime | None
    ) -> tuple[int, L.StationSample | None, str, int, int]:
        """Merge the latest record and the ``recent_values`` list of one
        state. Returns (records added or improved, latest sample, note,
        list records accepted, list records rejected)."""
        sample, note = station_sample_from_state(state, now)
        added = 0
        recent_total = recent_rejected = 0
        if state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            reference = now or state.last_updated
            recent, recent_rejected = L.parse_station_list(state.attributes.get("recent_values"), reference)
            recent_total = len(recent)
            for item in recent:
                added += int(self.data.add_station_sample(item))
        if sample is not None:
            added += int(self.data.add_station_sample(sample))
        return added, sample, note, recent_total, recent_rejected

    async def _async_backfill_station(self, now: datetime, span: timedelta) -> str:
        from homeassistant.components.recorder import get_instance, history

        start = now - span
        states = await get_instance(self.hass).async_add_executor_job(
            lambda: history.state_changes_during_period(
                self.hass, start, now, self.station_entity, include_start_time_state=True
            )
        )
        added = 0
        for state in states.get(self.station_entity, []):
            added += self._merge_station_state(state, None)[0]
        hours = self.data.finalize_station_hours(now)
        return f"{added} samples, {hours} hours"

    # -- coordinator interface ------------------------------------------------------------

    async def async_correct_snapshot(
        self, snapshot: LocalSnapshot, entry_id: str
    ) -> tuple[LocalSnapshot, dict[str, Any]] | None:
        """Corrected copy of a member's snapshot, or None (not a member,
        configuration problem). Never raises into the forecast path."""
        group = self.group_for(entry_id)
        if group is None or self.config_problem:
            return None
        try:
            keys = [L.hour_key(start) for start in snapshot.hours]
            rows = self.data.latest_rows(keys)
            shifts = {key: L.correction_from_row(row) for key, row in rows.items()}
            temps = {key: float(row[L.R["temp"]]) for key, row in rows.items()}
            stage2 = self.data.d["stage2"]["groups"].get(group.name)
            hours, summary = await self.hass.async_add_executor_job(
                L.apply_corrections, snapshot.hours, shifts, stage2, self.latitude, self.longitude, temps
            )
        except Exception as err:  # noqa: BLE001 - fail-safe: raw forecast
            LOGGER.warning("Learning correction failed, forecast stays raw: %s", sanitize_error(err))
            return None
        summary["group"] = group.name
        return replace(snapshot, hours=hours), summary

    def report_entry(
        self,
        entry_id: str,
        raw_days: Mapping[date, float],
        corr_days: Mapping[date, float],
        complete_days: set[date] | None,
        now: datetime,
    ) -> None:
        """Day totals of both forecasts from a member's refresh."""
        today = now.astimezone(self.tz).date()
        tomorrow = today + timedelta(days=1)

        def value(days: Mapping[date, float], day: date) -> float | None:
            if complete_days is not None and day not in complete_days:
                return None
            number = days.get(day)
            return None if number is None else float(number)

        report: EntryReport = {
            "updated": now.isoformat(),
            "today_raw_wh": value(raw_days, today),
            "today_corrected_wh": value(corr_days, today),
            "tomorrow_raw_wh": value(raw_days, tomorrow),
            "tomorrow_corrected_wh": value(corr_days, tomorrow),
        }
        self.entry_reports[entry_id] = report
        if report["today_raw_wh"] is not None and report["today_corrected_wh"] is not None:
            if L.record_entry_day(self.data, entry_id, today.isoformat(), report["today_raw_wh"],
                                  report["today_corrected_wh"], now, self.tz):
                self._save()
        self._notify()

    def group_correction(self, group: L.GroupInfo, day: str) -> dict[str, Any]:
        """Sum of the members' raw and corrected totals; ``day`` is
        "today" or "tomorrow"."""
        raw = corr = 0.0
        for member in group.members:
            report = self.entry_reports.get(member)
            if report is None:
                return {"raw_kwh": None, "corrected_kwh": None, "difference_kwh": None}
            if day == "today":
                pair = (report["today_raw_wh"], report["today_corrected_wh"])
            else:
                pair = (report["tomorrow_raw_wh"], report["tomorrow_corrected_wh"])
            if pair[0] is None or pair[1] is None:
                return {"raw_kwh": None, "corrected_kwh": None, "difference_kwh": None}
            raw += pair[0] / 1000.0
            corr += pair[1] / 1000.0
        return {"raw_kwh": round(raw, 2), "corrected_kwh": round(corr, 2), "difference_kwh": round(corr - raw, 2)}

    # -- hourly run ---------------------------------------------------------------------------

    async def _step(self, report: dict[str, Any], name: str, job: Any) -> Any:
        """Run one step; failures are recorded, never propagated."""
        try:
            result = await job
        except Exception as err:  # noqa: BLE001 - every step is independent
            message = f"{type(err).__name__}: {sanitize_error(err)}"
            report[name] = f"failed: {message}"
            LOGGER.warning("Learning step %s failed: %s", name, message)
            self.data.event(dt_util.utcnow(), "step_failed", f"{name}: {message}")
            return None
        report[name] = result if isinstance(result, str) else "ok"
        return result

    async def async_run_hourly(self, now: datetime | None = None) -> None:
        now = now or dt_util.utcnow()
        report: dict[str, Any] = {"started": now.isoformat()}
        async with self._lock:
            self.resolve_members()
            self._ingest_station(self.hass.states.get(self.station_entity), now)
            report["station_hours_added"] = self.data.finalize_station_hours(now)
            await self._step(report, "forecast", self._async_hourly_forecast(now))
            self._recompute_state(now)
            self.last_hourly = report
            self._save()
        self._notify()

    async def _async_hourly_forecast(self, now: datetime) -> str:
        predictors = await self._async_read_predictors(now)
        corrections = await self.hass.async_add_executor_job(
            L.compute_corrections, self.data.view(), predictors, now, self.tz
        )
        stored = self.data.archive_forecast(predictors, corrections, now)
        captured = L.capture_issue6(self.data, predictors, corrections, now, self.tz)
        self.next_hours = self._summarise(predictors, corrections, now)
        self.evidence_info = await self.hass.async_add_executor_job(
            _evidence_summary, self.data.view(), now, self.tz
        )
        return f"{stored} hours archived" + (", 06:00 forecast captured" if captured else "")

    async def _async_read_predictors(self, now: datetime) -> dict[int, L.Predictors]:
        if not self.irradiance_entity or not self.weather_entity:
            raise HubInputError("no member entry with a local weather source")
        state = self.hass.states.get(self.irradiance_entity)
        if state is None:
            raise HubInputError(f"{self.irradiance_entity} does not exist")
        records, _ = parse_irradiance_series(state.attributes.get("hourly_forecast"), now)
        weather = self.hass.states.get(self.weather_entity)
        if weather is None:
            raise HubInputError(f"{self.weather_entity} does not exist")
        async with asyncio.timeout(SERVICE_TIMEOUT_S):
            response = await self.hass.services.async_call(
                "weather", "get_forecasts",
                {"entity_id": self.weather_entity, "type": "hourly"},
                blocking=True, return_response=True,
            )
        forecast = None
        if isinstance(response, dict):
            item = response.get(self.weather_entity)
            if isinstance(item, dict):
                forecast = item.get("forecast")
        instants, _ = L.parse_weather_layers(forecast, weather.attributes.get("temperature_unit"), now)
        if not any(item[0] is not None for item in instants.values()):
            raise HubInputError("the weather forecast has no cloud layers (SwissWeather Fusion >= 0.3.4 needed)")
        predictors = await self.hass.async_add_executor_job(
            L.build_predictors, records, instants, self.latitude, self.longitude
        )
        now_ts = int(now.timestamp())
        return {
            key: pred for key, pred in predictors.items()
            if key + L.HOUR_S > now_ts and L.lead_bucket((key - now_ts) / L.HOUR_S) is not None
        }

    def _summarise(
        self, predictors: Mapping[int, L.Predictors], corrections: Mapping[int, L.Correction], now: datetime
    ) -> list[dict[str, Any]]:
        now_ts = int(now.timestamp())
        out = []
        for key in sorted(predictors):
            if key >= now_ts + NEXT_HOURS * L.HOUR_S:
                break
            pred = predictors[key]
            corr = corrections.get(key) or L.NEUTRAL
            kt_c, kd_c = L._adjust(pred.kt, pred.kd, corr.shift_kt, corr.shift_kd, 1.0)
            bucket = L.lead_bucket((key - now_ts) / L.HOUR_S)
            out.append({
                "hour": L.key_to_dt(key).astimezone(self.tz).isoformat(),
                "lead": L.BUCKET_NAMES[bucket] if bucket is not None else None,
                "kt": round(pred.kt, 3),
                "kt_corrected": round(kt_c, 3),
                "kd": round(pred.kd, 3),
                "kd_corrected": round(kd_c, 3),
                "ghi_w_m2": round(pred.kt * pred.e0h),
                "ghi_corrected_w_m2": round(kt_c * pred.e0h),
                "low_cloud": round(pred.low),
                "mid_high_cloud": round(max(pred.mid, pred.high)),
                "spread_k": round(pred.temp - pred.dew, 1),
                "elevation": round(pred.elevation, 1),
                "analogs": corr.analogs,
                "n_eff": round(corr.n_eff, 1),
                "shrink": round(corr.shrink, 3),
                "pooled": corr.pooled,
                "similar_hours": [
                    {
                        "hour": L.key_to_dt(h).astimezone(self.tz).strftime("%Y-%m-%d %H:%M"),
                        "lead": L.BUCKET_NAMES[b],
                        "distance": d,
                        "error_kt": e,
                    }
                    for h, b, d, e in corr.top
                ],
            })
        return out

    # -- nightly run ---------------------------------------------------------------------------

    async def async_run_nightly(self, now: datetime | None = None) -> None:
        now = now or dt_util.utcnow()
        started = time.monotonic()
        report: dict[str, Any] = {"started": now.isoformat()}
        async with self._lock:
            self.resolve_members()
            report["station_hours_added"] = self.data.finalize_station_hours(now)
            await self._step(report, "production", self._async_update_production(now))
            await self._step(report, "model_from_station", self._async_update_model(now))
            report["days_decided"] = self._decide_days(now)
            rep = await self.hass.async_add_executor_job(
                L.representativeness, self.data.view(), self.groups, now, self.latitude, self.longitude
            )
            rep["evaluated"] = now.isoformat()
            previous = self.data.d.get("representativeness") or {}
            if previous.get("representative", True) and not rep["representative"]:
                self.data.event(now, "station_unrepresentative",
                                f"correlation {rep['r']} below {L.REPRESENTATIVE_MIN_R}: learning paused")
            self.data.d["representativeness"] = rep
            if not self.data.frozen:
                await self._step(report, "stage2", self._async_stage2(now))
            else:
                report["stage2"] = "frozen"
            gate = await self.hass.async_add_executor_job(
                L.evaluate_gate, self.data.view(), self.groups, now, self.tz
            )
            self.data.d["gate"] = gate
            self.data.d["gate_history"].append(
                {k: gate[k] for k in ("evaluated", "algorithm", "days", "improvement", "passed")}
            )
            self._recompute_state(now)
            report["purged"] = self.data.purge(now, self.tz)
            await self._step(report, "store_size", self._async_check_size(now))
            await self._step(report, "export", self.async_export(now))
            self.evidence_info = await self.hass.async_add_executor_job(
            _evidence_summary, self.data.view(), now, self.tz
        )
            self.data.d["last_nightly"] = now.isoformat()
            report["duration_s"] = round(time.monotonic() - started, 2)
            self.last_nightly = report
            self._save()
        self._notify()

    async def _async_update_production(self, now: datetime) -> str:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        end = dt_util.as_utc(now).replace(minute=0, second=0, microsecond=0)
        notes = []
        for config in self.group_configs:
            name = config["name"]
            entity = config["energy_entity"]
            if not entity:
                continue
            tag = f"{name}|{entity}"
            backfilled = self.data.d["production_backfilled"]
            span = PRODUCTION_REFRESH_DAYS if tag in backfilled else L.RETENTION_DAYS
            start = end - timedelta(days=span)
            result = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period, self.hass, start, end, {entity}, "hour",
                {"energy": "kWh"}, {"change"},
            )
            hours = self.data.d["production"].setdefault(name, {})
            count = 0
            for row in result.get(entity, []):
                change = _to_float(row.get("change"))
                begin = row.get("start")
                if change is None or begin is None:
                    continue
                ts = begin.timestamp() if isinstance(begin, datetime) else float(begin)
                key = int(ts) - int(ts) % L.HOUR_S
                hours[key] = round(change, 5)
                count += 1
            if tag not in backfilled:
                backfilled.append(tag)
            notes.append(f"{name}: {count} hours")
        return ", ".join(notes) or "no groups"

    def _model_input(self, day: str) -> tuple[dict[datetime, HourRecord], dict[datetime, float], int] | None:
        hours = L.local_day_hours(day, self.tz)
        records: dict[datetime, HourRecord] = {}
        temps: dict[datetime, float] = {}
        for key in hours:
            meas = self.data.d["station_hours"].get(key)
            if meas is None or meas[L.H["ghi"]] is None or meas[L.H["dhi"]] is None:
                continue
            ghi, dhi = meas[L.H["ghi"]], min(meas[L.H["dhi"]], meas[L.H["ghi"]])
            e0h, _ = L.hour_geometry(key, self.latitude, self.longitude)
            dni = max(ghi - dhi, 0.0) / max(e0h / 1361.0, 0.05)
            start = L.key_to_dt(key)
            records[start] = HourRecord(start=start, ghi=ghi, dni=min(dni, 1500.0), dhi=dhi)
            if meas[L.H["temp"]] is not None:
                temps[start + timedelta(minutes=30)] = meas[L.H["temp"]]
        if not records:
            return None
        noon = datetime.fromisoformat(day).replace(hour=12, tzinfo=self.tz)
        offset = int((noon.utcoffset() or timedelta(0)).total_seconds())
        return records, temps, offset

    async def _async_update_model(self, now: datetime) -> str:
        today = now.astimezone(self.tz).date()
        computed = []
        for offset in range(1, MODEL_DAYS + 1):
            day = (today - timedelta(days=offset)).isoformat()
            model_input = None
            for group in self.groups:
                model = self.data.d["model"].setdefault(group.name, {})
                model_free = self.data.d["model_free"].setdefault(group.name, {})
                hours = L.local_day_hours(day, self.tz)
                if any(k in model for k in hours) and any(k in model_free for k in hours):
                    continue
                if model_input is None:
                    model_input = self._model_input(day)
                    if model_input is None:
                        break
                records, temps, utc_offset = model_input
                members = [c for m in group.members if (c := array_coordinator(self.hass, m)) is not None]
                if not members or len(members) != len(group.members):
                    continue
                total: dict[int, float] = {}
                total_free: dict[int, float] = {}
                for coordinator in members:
                    hourly = await coordinator.async_model_hours(records, temps, utc_offset)
                    hourly_free = (
                        await coordinator.async_model_hours(records, temps, utc_offset, shading=False)
                        if coordinator.has_shading else hourly
                    )
                    for key, wh in hourly.items():
                        total[key] = total.get(key, 0.0) + wh
                    for key, wh in hourly_free.items():
                        total_free[key] = total_free.get(key, 0.0) + wh
                for start in records:
                    key = L.hour_key(start)
                    model[key] = round(total.get(key, 0.0) / 1000.0, 5)
                    model_free[key] = round(total_free.get(key, 0.0) / 1000.0, 5)
                computed.append(f"{group.name} {day}")
        return f"{len(computed)} group-days" if computed else "nothing to do"

    def _decide_days(self, now: datetime) -> int:
        today = now.astimezone(self.tz).date()
        created = parse_utc(self.data.d.get("created")) or now
        first = max(created.astimezone(self.tz).date(), today - timedelta(days=DECIDE_LOOKBACK_DAYS))
        decided = 0
        day = first
        while day < today:
            iso = day.isoformat()
            if iso not in self.data.d["days"]:
                status, details = L.decide_day(
                    self.data, iso, self.groups, self.tz, self.latitude, self.longitude, now
                )
                if status is not None:
                    self.data.d["days"][iso] = {"decided": int(now.timestamp()), "status": status, "details": details}
                    decided += 1
                    if status != L.DAY_OK:
                        self.data.event(now, "day_excluded", f"{iso}: {status} {json.dumps(details, sort_keys=True)}")
            day += timedelta(days=1)
        return decided

    async def _async_stage2(self, now: datetime) -> str:
        groups = {}
        for group in self.groups:
            groups[group.name] = await self.hass.async_add_executor_job(
                L.stage2_factors, self.data.view(), group.name, now, self.tz, self.latitude, self.longitude
            )
        self.data.d["stage2"] = {"computed": now.isoformat(), "algorithm": L.ALGORITHM_VERSION, "groups": groups}
        self.data.d["stage2_history"].append({"computed": now.isoformat(), "groups": groups})
        return "ok"

    async def _async_check_size(self, now: datetime) -> str:
        # to_json() in the event loop (consistent copy), serialising in the executor.
        size = len(await self.hass.async_add_executor_job(json.dumps, self.data.to_json()))
        trimmed = 0
        while size > L.STORE_MAX_BYTES and trimmed < L.RETENTION_DAYS:
            self.data.trim_oldest_days(30, self.tz)
            trimmed += 30
            size = len(await self.hass.async_add_executor_job(json.dumps, self.data.to_json()))
        if trimmed:
            self.data.event(now, "store_trimmed", f"store above {L.STORE_MAX_BYTES} bytes: oldest {trimmed} days dropped")
        self.store_size = size
        return f"{size} bytes"

    # -- export -------------------------------------------------------------------------------------

    def export_dir(self) -> Path:
        return Path(self.hass.config.path(*LEARNING_EXPORT_SUBDIR))

    async def async_export(self, now: datetime | None = None) -> str:
        """Build and write the export files, entirely in the executor (on a
        consistent view of the data, so the event loop is never blocked)."""
        now = now or dt_util.utcnow()
        local_day = now.astimezone(self.tz).date()
        oldest_year = (now - timedelta(days=L.RETENTION_DAYS)).year
        oldest_day = (local_day - timedelta(days=L.RETENTION_DAYS)).isoformat()
        written = await self.hass.async_add_executor_job(
            _build_and_write_export, self.export_dir(), self.data.view(), list(self.groups), self.tz,
            self.latitude, self.longitude, now, oldest_year, oldest_day,
        )
        return f"{written} files"


def _build_and_write_export(
    base: Path, data: L.LearningData, groups: list[L.GroupInfo], tz: ZoneInfo, latitude: float,
    longitude: float, now: datetime, oldest_year: int, oldest_day: str,
) -> int:
    files: dict[str, str] = {}
    years = {L.key_to_dt(k).year for k in data.d["archive"]} | {now.year}
    years |= {L.key_to_dt(k).year for k in data.d["station_hours"]}
    for year in sorted(years):
        files[f"archive_{year}.csv"] = L.export_archive_csv(data, year)
        files[f"station_hours_{year}.csv"] = L.export_station_csv(data, year)
        files[f"production_{year}.csv"] = L.export_production_csv(data, year)
    files["days.csv"] = L.export_days_csv(data, groups, tz)
    files["forecast_0600.csv"] = L.export_issue6_csv(data)
    files["events.csv"] = L.export_events_csv(data)
    settings = L.export_settings(data, groups, tz, latitude, longitude)
    settings["exported"] = now.isoformat()
    files["settings.json"] = json.dumps(settings, indent=1, default=str)
    local_day = now.astimezone(tz).date()
    if local_day.weekday() == 0:
        files[f"snapshots/model_{local_day.isoformat()}.json"] = files["settings.json"]
    return _write_export(base, files, oldest_year, oldest_day)


def _write_export(base: Path, files: Mapping[str, str], oldest_year: int, oldest_day: str) -> int:
    """Atomic writes into the fixed export folder; retention cleanup.

    Names come only from this module (years, dates, constants), never from
    user input, so no path can leave the folder.
    """
    (base / "snapshots").mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        target = base / name
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    for prefix in YEARLY_EXPORTS:
        for path in base.glob(f"{prefix}_*.csv"):
            year = path.stem[len(prefix) + 1:]
            if year.isdigit() and len(year) == 4 and int(year) < oldest_year:
                path.unlink()
    for path in (base / "snapshots").glob("model_*.json"):
        if path.stem[len("model_"):] < oldest_day:
            path.unlink()
    return len(files)


def _evidence_summary(data: L.LearningData, now: datetime, tz: ZoneInfo) -> dict[str, Any]:
    """Executor job on a ``LearningData.view()``."""
    ev = L.build_evidence(data, data.evidence_cutoff(int(now.timestamp())), tz)
    per_bucket = {name: int((ev.buckets == i).sum()) for i, name in enumerate(L.BUCKET_NAMES)}
    today = now.astimezone(tz).date()
    window = {(today - timedelta(days=i)).isoformat() for i in range(1, 29)}
    statuses: dict[str, int] = {}
    for day, item in data.d["days"].items():
        if day in window:
            statuses[item["status"]] = statuses.get(item["status"], 0) + 1
    return {
        "rows": ev.size,
        "hours": int(len(set(ev.hours.tolist()))),
        "per_lead": per_bucket,
        "days_last_28": statuses,
        "archived_hours": len(data.d["archive"]),
        "station_hours": len(data.d["station_hours"]),
    }
