"""Regression tests for the 0.1.37.1 remediation of the external 0.1.37.0
review (OMSF_v0_1_37_0_ICS_Quality_Review.md, findings ICS-01 to ICS-07).

Each test asserts the corrected behaviour and fails against 0.1.37.0 (checked
on a copy of 0.1.37.0 before the fix; see the 0.1.37.1 ICS test report).
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.open_meteo_solar_forecast import house_ghi as HG
from custom_components.open_meteo_solar_forecast import learning as L
from custom_components.open_meteo_solar_forecast import shading_provider
from custom_components.open_meteo_solar_forecast import solcast as SC
from custom_components.open_meteo_solar_forecast.const import (
    ACTIVE_SOURCE_LOCAL,
    DOMAIN,
    LEARNING_STORAGE_KEY,
)
from custom_components.open_meteo_solar_forecast.coordinator import _config_fingerprint
from custom_components.open_meteo_solar_forecast.local_provider import (
    SNOW_HOLD,
    LocalWeatherReader,
    history_storage_key,
)

from .fusion_fixtures import LAT, LON
from .test_house_ghi_hub import ts
from .test_learning_engine import DAY, GROUP, NIGHT_AFTER, TZ, _full_day
from .test_learning_hub import LOCAL, NOW, Fake, basic, setup_array, setup_hub
from .test_local_integration import FETCH_OM, HOUR0, LOCAL_OPTIONS, FakeFusion, _coordinator
from .test_local_integration import NOW as LOCAL_NOW
from .test_same_day_hub import COUNTER_ATTRS, T_SIGNAL
from .test_learning_hub import ENERGY
from .test_shading_integration import morning_shade_table
from .test_solcast_hub import SLOT, URL, call_at, configured

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture
async def zurich(hass):
    await hass.config.async_set_time_zone("Europe/Zurich")
    hass.config.latitude, hass.config.longitude = LAT, LON
    yield


def _daylight(day):
    return [k for k in L.local_day_hours(day, TZ) if L.hour_geometry(k, LAT, LON)[1] >= L.MIN_ELEVATION_DEG]


# ---------------------------------------------------------------------------
# ICS-01 — partial model days are completed; the ratio check uses the hours
# that have both station light and model
# ---------------------------------------------------------------------------


async def test_ics01_station_hours_that_arrive_later_are_modelled(hass, zurich, freezer):
    _, _, _, hub = await basic(hass, freezer)
    yesterday = (NOW.astimezone(hub.tz).date() - timedelta(days=1)).isoformat()
    daylight = [k for k in L.local_day_hours(yesterday, hub.tz)
                if L.hour_geometry(k, LAT, LON)[1] >= L.MIN_ELEVATION_DEG]
    first, rest = daylight[: len(daylight) // 2], daylight[len(daylight) // 2:]

    def station(keys):
        for key in keys:
            ghi = 0.5 * L.hour_geometry(key, LAT, LON)[0]
            hub.data.d["station_hours"][key] = [ghi, 0.4 * ghi, 20.0, 330.0, 0.1 * ghi, 10.0, 6.0]

    station(first)
    await hub._async_update_model(NOW)
    model, free = hub.data.d["model"]["10K"], hub.data.d["model_free"]["10K"]
    assert set(first) <= set(model) and not set(rest) & set(model)
    model[first[0]] = free[first[0]] = 9.87654  # an hour already modelled stays as it is
    station(rest)  # the station's missing hours arrive later
    assert "group-days" in await hub._async_update_model(NOW)
    assert set(daylight) <= set(model) and set(daylight) <= set(free)
    assert model[first[0]] == free[first[0]] == 9.87654
    assert all(model[k] > 0 for k in rest)
    assert await hub._async_update_model(NOW) == "nothing to do"  # complete: no further model runs
    # an hour in one map only (store from before 0.1.34.2): only the missing map is filled
    model[first[1]] = 1.2345
    del free[first[1]]
    await hub._async_update_model(NOW)
    assert model[first[1]] == 1.2345 and free[first[1]] > 0


def test_ics01_a_missing_station_hour_keeps_the_ratio_check():
    """0.1.37.0 accepted such a day after the wait without the ratio check."""
    data = L.LearningData()
    _full_day(data, DAY, prod_per_hour=0.3, model_per_hour=1.0)  # far outside the ratio band
    gap = _daylight(DAY)[3]
    del data.d["station_hours"][gap]
    del data.d["model"]["10K"][gap]  # no station light: never modelled
    status, details = L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, NIGHT_AFTER)
    assert status == "not_representative"
    assert details["ratio"]["10K"] == pytest.approx(0.3)
    n = len(_daylight(DAY))
    assert details["ratio_hours"]["10K"] == f"{n - 1}/{n}" and "ratio_unchecked" not in details


def test_ics01_ratio_uses_only_hours_with_station_and_model():
    data = L.LearningData()
    _full_day(data, DAY)
    gap = _daylight(DAY)[5]
    del data.d["station_hours"][gap]
    del data.d["model"]["10K"][gap]
    data.d["production"]["10K"][gap] = 5.0  # would push the ratio out of the band if counted
    status, details = L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, NIGHT_AFTER)
    assert status == L.DAY_OK and details["ratio"]["10K"] == pytest.approx(1.0)


def test_ics01_complete_day_details_are_unchanged():
    data = L.LearningData()
    _full_day(data, DAY)
    status, details = L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, NIGHT_AFTER)
    assert status == L.DAY_OK and "ratio_hours" not in details and "ratio_unchecked" not in details


def test_ics01_an_hour_with_station_light_waits_for_its_model_then_is_left_out():
    data = L.LearningData()
    _full_day(data, DAY, prod_per_hour=0.3)
    del data.d["model"]["10K"][_daylight(DAY)[4]]  # station light present, model not yet
    assert L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, NIGHT_AFTER)[0] is None
    late = NIGHT_AFTER + timedelta(days=L.MODEL_WAIT_DAYS + 1)
    status, details = L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, late)
    assert status == "not_representative" and "ratio_hours" in details


def test_ics01_too_few_modelled_hours_stay_unchecked():
    data = L.LearningData()
    _full_day(data, DAY)
    for key in _daylight(DAY)[: math.ceil(len(_daylight(DAY)) * 0.25)]:
        del data.d["model"]["10K"][key]
    late = NIGHT_AFTER + timedelta(days=L.MODEL_WAIT_DAYS + 1)
    status, details = L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, late)
    assert status == L.DAY_OK and details["ratio_unchecked"] == ["10K"] and "ratio" not in details


def test_ics01_exactly_80_percent_is_checked():
    data = L.LearningData()
    _full_day(data, DAY, prod_per_hour=0.3)
    daylight = _daylight(DAY)
    drop = len(daylight) - math.ceil(L.DAY_STATION_COMPLETENESS * len(daylight))
    for key in daylight[:drop]:
        del data.d["model"]["10K"][key]
    late = NIGHT_AFTER + timedelta(days=L.MODEL_WAIT_DAYS + 1)
    assert L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, late)[0] == "not_representative"
    del data.d["model"]["10K"][daylight[drop]]
    assert L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, late)[0] == L.DAY_OK  # below 80 %: unchecked


# ---------------------------------------------------------------------------
# ICS-02 — the house-light clipping check uses the forecast's inverter limits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shared", "per_array", "count", "expected"),
    [
        (10000, 0, 1, 10000.0),  # shared only
        (0, 0, 1, None),  # no limit
        (None, None, 1, None),
        (0, 4000, 1, 4000.0),  # single array with its own inverter
        (10000, 4000, 1, 4000.0),  # the array's own inverter wins (as in the forecast)
        (0, [3000, 2000], 2, 5000.0),  # per array: the limits add up
        (10000, [3000, 2000], 2, 5000.0),  # mixed: the shared value is ignored
        (10000, [3000, 0], 2, None),  # one array without a limit: the total has none
        (10000, [0, 0], 2, 10000.0),  # no array limit: shared
        (0, [2500], 3, 7500.0),  # one value for three arrays
        (0, 2500, 3, 7500.0),
        (float("nan"), 0, 1, None),
        (True, 0, 1, None),
        (-5, [-1, 0], 2, None),
        (10000, [float("inf"), 0], 2, 10000.0),
    ],
)
def test_ics02_effective_limit_matches_the_forecast_rules(shared, per_array, count, expected):
    assert HG.effective_ac_limit_w(shared, per_array, count) == expected


async def test_ics02_per_array_inverter_gives_the_clipping_flag(hass, zurich, freezer):
    freezer.move_to(NOW)
    fake = Fake(hass)
    fake.publish()
    array = await setup_array(hass, options={**LOCAL, "inverter_power": 0, "array_inverter_power": 1000})
    _, hub = await setup_hub(hass, [array])
    assert hub._ac_limits_kwh() == {"10K": pytest.approx(0.25)}  # 0.1.37.0: None (no check)
    value = 1000.0
    for minute in range(0, 61, 15):
        freezer.move_to(datetime(2026, 10, 7, 8, 0, tzinfo=UTC) + timedelta(minutes=minute))
        hass.states.async_set(ENERGY, f"{value:.2f}", COUNTER_ATTRS)
        await hass.async_block_till_done()
        value += 0.24 if minute < 30 else 0.22
    freezer.move_to(T_SIGNAL)
    hass.states.async_set(ENERGY, f"{value - 0.22:.2f}", COUNTER_ATTRS, force_update=True)
    fake.publish(T_SIGNAL.replace(minute=0))
    fake.station(T_SIGNAL - timedelta(minutes=12), 200.0)
    await hass.async_block_till_done()
    hub.house_curves = None
    hub.data.d["house_ghi"]["windows"].clear()
    await hub.async_run_same_day(T_SIGNAL)
    windows = hub.data.d["house_ghi"]["windows"]
    assert windows[str(ts(8, 0))][3:5] == [HG.BAD, "clipping"]
    assert windows[str(ts(8, 30))][3:5] == [HG.UNCERTAIN, "clipping"]


async def test_ics02_multi_array_entry_limits(hass, zurich, freezer):
    _, array, _, hub = await basic(hass, freezer)
    two = {"declination": [40, 30], "azimuth": [197, 107], "modules_power": [6000, 4000]}
    for per_array, expected in (([3000, 1000], 1.0), ([3000, 0], None), ([0, 0], None), (2000, 1.0), ([2000], 1.0)):
        hass.config_entries.async_update_entry(
            array, options={**array.options, **two, "inverter_power": 0, "array_inverter_power": per_array})
        assert hub._ac_limits_kwh() == {"10K": expected if expected is None else pytest.approx(expected)}


# ---------------------------------------------------------------------------
# ICS-03 — HTTP 200 without valid JSON is a failure with one retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", [b"{not json", b"", b"\xff\xfe\x00garbage"])
async def test_ics03_invalid_json_counts_as_failure_and_retries(hass, zurich, freezer, aioclient_mock, body):
    _, _, _, hub = await configured(hass, freezer, aioclient_mock)
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, content=body, status=200)
    result = await call_at(hass, freezer, hub, SLOT)
    section = hub.data.d["solcast"]
    assert "not valid JSON" in result
    assert section["failures"] == 1 and "not valid JSON" in section["last_error"]
    assert section["retry_at"] is not None  # 0.1.37.0: no failure, no retry
    assert sum(section["calls"].values()) == 1 and section["issues"] == {}
    assert "very-secret" not in section["last_error"]
    # the retry runs once and is counted; it schedules no further retry
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, content=body, status=200)
    retry_time = datetime.fromtimestamp(section["retry_at"], UTC) + timedelta(seconds=1)
    assert (await call_at(hass, freezer, hub, retry_time)).startswith("retry")
    assert section["failures"] == 2 and section["retry_at"] is None and sum(section["calls"].values()) == 2


# ---------------------------------------------------------------------------
# ICS-04 — every check result field that is read back is validated
# ---------------------------------------------------------------------------

GOOD_GATE = {"passed": True, "days": 20, "improvement": 0.12, "better_recent": 6, "reasons": [],
             "algorithm": L.ALGORITHM_VERSION, "evaluated": "2026-10-07T01:00:00+00:00"}
BAD_FIELDS = [
    ("passed", "yes"), ("passed", None), ("days", "broken"), ("days", True), ("days", -1), ("days", 1.5),
    ("days", float("nan")), ("days", None), ("better_recent", "x"), ("better_recent", -2),
    ("improvement", "x"), ("improvement", float("nan")), ("improvement", float("inf")), ("improvement", False),
    ("reasons", "text"), ("reasons", [1]),
]


def test_ics04_gate_problem():
    assert L.gate_problem(None) is None and L.gate_problem(GOOD_GATE) is None
    assert L.gate_problem({"passed": False}) is None  # absent fields read as defaults
    assert L.gate_problem({**GOOD_GATE, "improvement": None}) is None
    assert L.gate_problem([]) == "not a mapping"
    for name, value in BAD_FIELDS:
        assert L.gate_problem({**GOOD_GATE, name: value}) == name, (name, value)


def _stored_with(holder: str, gate, history=None):
    raw = json.loads(json.dumps(L.LearningData().to_json()))
    target = raw if holder == "stage-1" else raw[{"same-day": "same_day", "solcast": "solcast"}[holder]]
    target["gate"] = gate
    if history is not None:
        target["gate_history"] = history
    return raw, target


@pytest.mark.parametrize("holder", ["stage-1", "same-day", "solcast"])
@pytest.mark.parametrize(("name", "value"), BAD_FIELDS[:8])
def test_ics04_malformed_check_is_dropped_on_load(holder, name, value):
    raw, _ = _stored_with(holder, {**GOOD_GATE, name: value},
                          history=[{**GOOD_GATE}, {**GOOD_GATE, "days": "x"}, "junk"])
    data = L.LearningData.from_json(json.loads(json.dumps(raw)))
    target = data.d if holder == "stage-1" else data.d[{"same-day": "same_day", "solcast": "solcast"}[holder]]
    assert target["gate"] is None and target["gate_history"] == [GOOD_GATE]
    assert data.repaired == [f"{holder} check ({name})", f"{holder} check history (2 entries)"]
    # the states are computed without an error
    assert SC.state_of(True, data.d["solcast"]["gate"], True) == "collecting"
    assert L.derive_state(data.d["gate"], True, True, False)[0] == L.STATE_COLLECTING


def test_ics04_valid_checks_load_unchanged():
    raw, _ = _stored_with("solcast", GOOD_GATE, history=[GOOD_GATE])
    raw["gate"] = GOOD_GATE
    raw["same_day"]["gate"] = {**GOOD_GATE, "improvement": None}
    data = L.LearningData.from_json(json.loads(json.dumps(raw)))
    assert data.repaired == [] and data.d["gate"] == GOOD_GATE and data.d["solcast"]["gate"] == GOOD_GATE
    assert data.d["same_day"]["gate"]["improvement"] is None


def test_ics04_history_that_is_not_a_list_is_still_malformed():
    raw, _ = _stored_with("same-day", None, history={})
    with pytest.raises(L.LearningDataError):
        L.LearningData.from_json(raw)


async def test_ics04_hub_drops_the_check_reports_it_and_keeps_the_rest(hass, hass_storage, zurich, freezer):
    raw, _ = _stored_with("solcast", {"passed": True, "days": "broken"})
    key = L.hour_key(NOW.replace(minute=0))
    raw["station_hours"] = {str(key): [400.0, 120.0, 60.0, 300.0, 40.0, 12.0, 6.0]}
    hass_storage[LEARNING_STORAGE_KEY] = {"version": 1, "minor_version": 1, "key": LEARNING_STORAGE_KEY,
                                          "data": raw}
    _, _, _, hub = await basic(hass, freezer)
    assert hub.store_fault is None and hub.data.d["station_hours"][key][0] == 400.0  # not quarantined
    assert hub.data.d["solcast"]["gate"] is None
    assert any(e[1] == "store_repaired" and "solcast check (days)" in e[2] for e in hub.data.d["events"])
    assert hub.solcast_state == "not_configured"
    state = hass.states.get("sensor.solar_forecast_learning_solcast_blend")
    assert state is not None and state.state != "unavailable"


# ---------------------------------------------------------------------------
# ICS-05 — a retained time without its zone discards the retained forecast
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stamp", ["naive", "2026-10-07T04:00:00", "2026-10-07 04:00"])
async def test_ics05_naive_retained_time_is_discarded_and_fetched_fresh(hass, zurich, freezer, hass_storage, stamp):
    freezer.move_to(LOCAL_NOW)
    FakeFusion(hass).publish(HOUR0)
    entry = MockConfigEntry(domain=DOMAIN, title="Home", data={CONF_LATITUDE: LAT, CONF_LONGITUDE: LON},
                            options=LOCAL_OPTIONS)
    last = dt_util.utcnow().replace(tzinfo=None).isoformat() if stamp == "naive" else stamp
    data = {"config_fingerprint": _config_fingerprint(entry), "last_successful_update": last,
            "watts": {}, "wh_period": {}, "wh_period_15m": {}, "wh_days": {}, "api_timezone_offset": 7200}
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {"version": 2, "minor_version": 1,
                                                 "key": f"{DOMAIN}.{entry.entry_id}", "data": data}
    entry.add_to_hass(hass)
    with patch(FETCH_OM, AsyncMock(side_effect=AssertionError)):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED  # 0.1.37.0: setup failed (and failed again on every retry)
    coordinator = _coordinator(hass, entry)
    assert coordinator.active_source == ACTIVE_SOURCE_LOCAL
    assert coordinator._last_successful_update.utcoffset() is not None


async def test_ics05_retained_time_with_zone_is_still_used(hass, zurich, freezer, hass_storage):
    freezer.move_to(LOCAL_NOW)
    FakeFusion(hass).publish(HOUR0)
    entry = MockConfigEntry(domain=DOMAIN, title="Home", data={CONF_LATITUDE: LAT, CONF_LONGITUDE: LON},
                            options=LOCAL_OPTIONS)
    data = {"config_fingerprint": _config_fingerprint(entry),
            "last_successful_update": (dt_util.utcnow() - timedelta(minutes=5)).isoformat(),
            "watts": {}, "wh_period": {}, "wh_period_15m": {}, "wh_days": {}, "api_timezone_offset": 7200}
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {"version": 2, "minor_version": 1,
                                                 "key": f"{DOMAIN}.{entry.entry_id}", "data": data}
    entry.add_to_hass(hass)
    with patch(FETCH_OM, AsyncMock(side_effect=AssertionError)):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert _coordinator(hass, entry).active_source == "retained"


# ---------------------------------------------------------------------------
# ICS-06 — the last valid snow depth survives a restart
# ---------------------------------------------------------------------------

SNOW_ENTITY = "sensor.station_snow_depth"


def _reader(hass, entity=SNOW_ENTITY):
    return LocalWeatherReader(hass, "entry1", "weather.x", "sensor.irr", entity, LAT, LON)


def _stored(hass_storage, snow):
    key = history_storage_key("entry1")
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key,
                         "data": {"hours": [], "temperatures": {}, "snow": snow}}


async def _saved_data(reader):
    captured = {}
    with patch.object(reader._store, "async_delay_save",
                      side_effect=lambda func, delay: captured.update(data=func())):
        reader._schedule_save()
    return captured["data"]


async def test_ics06_snow_hold_survives_restart_for_the_rest_of_24_hours(hass, zurich, freezer, hass_storage):
    freezer.move_to(NOW)
    first = _reader(hass)
    hass.states.async_set(SNOW_ENTITY, "0.05", {"unit_of_measurement": "m"})
    await first._async_load()
    assert first._read_snow_depth()[1]["quality"] == "sensor"
    saved = await _saved_data(first)
    assert saved["snow"] == {"entity_id": SNOW_ENTITY, "time": NOW.isoformat(), "depth_m": 0.05}
    # restart 20 h later: the sensor is still unavailable
    _stored(hass_storage, saved["snow"])
    freezer.move_to(NOW + timedelta(hours=20))
    hass.states.async_set(SNOW_ENTITY, "unavailable")
    second = _reader(hass)
    await second._async_load()
    depth, report = second._read_snow_depth()
    assert depth == 0.05 and report["quality"] == "held"  # 0.1.37.0: 0 (hold lost)
    # the hold still ends 24 h after the original reading, not after the restart
    freezer.move_to(NOW + SNOW_HOLD + timedelta(minutes=1))
    depth, report = second._read_snow_depth()
    assert depth == 0.0 and report["quality"] == "unavailable_assumed_0"


@pytest.mark.parametrize(
    "snow",
    [
        {"entity_id": "sensor.other", "time": NOW.isoformat(), "depth_m": 0.05},  # another sensor
        {"entity_id": SNOW_ENTITY, "time": "2026-10-07T04:30:00", "depth_m": 0.05},  # no zone
        {"entity_id": SNOW_ENTITY, "time": (NOW + timedelta(hours=1)).isoformat(), "depth_m": 0.05},  # future
        {"entity_id": SNOW_ENTITY, "time": NOW.isoformat(), "depth_m": -0.1},
        {"entity_id": SNOW_ENTITY, "time": NOW.isoformat(), "depth_m": 99.0},
        {"entity_id": SNOW_ENTITY, "time": NOW.isoformat(), "depth_m": float("nan")},
        {"entity_id": SNOW_ENTITY, "time": NOW.isoformat(), "depth_m": "0.05"},
        {"entity_id": SNOW_ENTITY, "time": NOW.isoformat(), "depth_m": True},
        {"entity_id": SNOW_ENTITY, "time": "garbage", "depth_m": 0.05},
        {"entity_id": SNOW_ENTITY, "depth_m": 0.05},
        "junk",
        [1, 2],
    ],
)
async def test_ics06_implausible_stored_snow_is_ignored(hass, zurich, freezer, hass_storage, snow):
    freezer.move_to(NOW + timedelta(minutes=1))
    _stored(hass_storage, snow)
    hass.states.async_set(SNOW_ENTITY, "unavailable")
    reader = _reader(hass)
    await reader._async_load()
    assert reader._snow_last_valid is None
    assert reader._read_snow_depth()[1]["quality"] == "unavailable_assumed_0"


async def test_ics06_future_time_within_clock_slack_is_kept(hass, zurich, freezer, hass_storage):
    freezer.move_to(NOW)
    _stored(hass_storage, {"entity_id": SNOW_ENTITY, "time": (NOW + timedelta(minutes=4)).isoformat(),
                           "depth_m": 0.05})
    reader = _reader(hass)
    await reader._async_load()
    assert reader._snow_last_valid == (NOW + timedelta(minutes=4), 0.05)


async def test_ics06_without_snow_sensor_nothing_is_stored_or_restored(hass, zurich, freezer, hass_storage):
    freezer.move_to(NOW)
    _stored(hass_storage, {"entity_id": None, "time": NOW.isoformat(), "depth_m": 0.05})
    reader = _reader(hass, entity=None)
    await reader._async_load()
    assert reader._snow_last_valid is None
    assert "snow" not in await _saved_data(reader)


async def test_ics06_history_hours_survive_a_bad_snow_entry(hass, zurich, freezer, hass_storage):
    freezer.move_to(NOW)
    key = history_storage_key("entry1")
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key,
                         "data": {"hours": [], "temperatures": {NOW.isoformat(): 12.5}, "snow": "junk"}}
    reader = _reader(hass)
    await reader._async_load()
    assert reader._temps == {NOW: 12.5} and reader._snow_last_valid is None


# ---------------------------------------------------------------------------
# ICS-07 — the shading cache follows the file content
# ---------------------------------------------------------------------------


def test_ics07_same_size_and_time_with_new_content_is_read_again(tmp_path):
    path = morning_shade_table(tmp_path / "same.txt")
    first = shading_provider.read_shading_file(str(path))
    stat = os.stat(path)
    text = path.read_text()
    changed = text.replace("\t0.0\n", "\t0.5\n")  # same length
    assert len(changed) == len(text) and changed != text
    path.write_text(changed)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # a tool that keeps the time
    assert os.stat(path).st_size == stat.st_size and os.stat(path).st_mtime_ns == stat.st_mtime_ns
    second = shading_provider.read_shading_file(str(path))
    assert second is not first and second.digest != first.digest
    assert second.summary["min_factor"] == 0.5  # 0.1.37.0: the old table (min 0.0)


def test_ics07_unchanged_content_is_not_parsed_again(tmp_path):
    path = morning_shade_table(tmp_path / "keep.txt")
    first = shading_provider.read_shading_file(str(path))
    os.utime(path, ns=(1, 1))  # only the time changes
    with patch.object(shading_provider, "load_shading_text", side_effect=AssertionError("parsed again")):
        assert shading_provider.read_shading_file(str(path)) is first


def test_ics07_cache_stays_bounded(tmp_path):
    shading_provider._CACHE.clear()
    for i in range(shading_provider._CACHE_MAX + 5):
        shading_provider.read_shading_file(str(morning_shade_table(tmp_path / f"t{i}.txt")))
    assert len(shading_provider._CACHE) == shading_provider._CACHE_MAX


def test_ics02_house_light_version_2_is_recorded_after_version_1():
    """Rule 16: a changed quality rule raises the version; the history keeps 1."""
    assert HG.VERSION == 2
    section = HG.new_section()
    section["version_history"] = [["2026-11-16T10:00:00+00:00", 1, "first"]]
    now = datetime(2026, 11, 17, 12, tzinfo=UTC)
    assert HG.note_version(section, now) is True
    assert [v for _t, v, _n in section["version_history"]] == [1, 2]
    assert HG.version_at(section, int(datetime(2026, 11, 17, 11, tzinfo=UTC).timestamp())) == 1
    assert HG.version_at(section, int(now.timestamp()) + 60) == 2
    assert HG.note_version(section, now) is False  # once
