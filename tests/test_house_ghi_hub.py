"""Measured light at the house (0.1.36) in Home Assistant: windows, the
sensor, independence from forecasts and learning, clipping, waiting for the
counters, snow, version, reset, export, diagnostics; and the fresh same-day
signal reaching the members before a decision is recorded."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from custom_components.open_meteo_solar_forecast import house_ghi as HG
from custom_components.open_meteo_solar_forecast import learning as L
from custom_components.open_meteo_solar_forecast import same_day as SD
from custom_components.open_meteo_solar_forecast.diagnostics import async_get_config_entry_diagnostics

from .fusion_fixtures import LAT, LON
from .test_learning_hub import ENERGY, LOCAL, Fake, NOW, refresh, setup_array, setup_hub
from .test_same_day_hub import COUNTER_ATTRS, T_SIGNAL, grey_morning

UTC = timezone.utc
SENSOR = "sensor.solar_forecast_learning_house_ghi"
REQUIRED = ("quality", "reason", "window_start", "window_end", "clear_sky_index", "clear_sky_ghi",
            "hour_ghi", "hour_quality", "recent", "version")


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture
async def zurich(hass):
    await hass.config.async_set_time_zone("Europe/Zurich")
    hass.config.latitude, hass.config.longitude = LAT, LON
    yield


def ts(hour, minute):
    return int(datetime(2026, 10, 7, hour, minute, tzinfo=UTC).timestamp())


async def station_now(hass, fake, hub, end=None, ghi=200.0):
    """A fresh station sample (measured air temperature 8.7 °C); the house
    curves are rebuilt with it and earlier windows are assessed again."""
    fake.station(end or T_SIGNAL - timedelta(minutes=12), ghi)
    await hass.async_block_till_done()
    hub.house_curves = None
    hub.data.d["house_ghi"]["windows"].clear()


async def test_windows_and_the_sensor(hass, zurich, freezer):
    fake, _, _, hub = await grey_morning(hass, freezer)
    await station_now(hass, fake, hub)
    await hub.async_run_same_day(T_SIGNAL)
    windows = hub.data.d["house_ghi"]["windows"]
    for minute in (0, 15, 30, 45):
        row = windows[str(ts(8, minute))]
        assert row[3] == HG.GOOD and row[4] is None, (minute, row)
        assert row[0] == pytest.approx(row[1] * row[2], abs=0.1)  # GHI = k x clear-sky GHI
        assert 0.0 < row[1] < 0.2  # 0.02 kWh per quarter hour: a dark sky
    assert windows[str(ts(7, 45))][3:5] == [HG.BAD, "counter"]  # no reading before 08:00
    assert str(ts(9, 0)) not in windows  # not complete
    state = hass.states.get(SENSOR)
    assert float(state.state) == pytest.approx(windows[str(ts(8, 45))][0])
    assert state.attributes["unit_of_measurement"] == "W/m²"
    assert state.attributes["device_class"] == "irradiance" and state.attributes["state_class"] == "measurement"
    assert all(key in state.attributes for key in REQUIRED)
    assert state.attributes["window_start"] == datetime(2026, 10, 7, 8, 45, tzinfo=UTC).isoformat()
    assert state.attributes["window_end"] == datetime(2026, 10, 7, 9, 0, tzinfo=UTC).isoformat()
    assert state.attributes["hour_quality"] == HG.GOOD and state.attributes["hour_ghi"] is not None
    assert state.attributes["version"] == HG.VERSION
    assert state.attributes["bad_counts_today"]["counter"] >= 1


async def test_a_bad_window_makes_the_sensor_unavailable(hass, zurich, freezer):
    fake, _, _, hub = await grey_morning(hass, freezer)
    await station_now(hass, fake, hub)
    await hub.async_run_same_day(T_SIGNAL)
    section = hub.data.d["house_ghi"]
    section["windows"][str(ts(8, 45))] = [None, 0.1, 300.0, HG.BAD, "snow", 25.0]
    hub._notify()
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "unavailable"
    section["windows"][str(ts(8, 45))] = [30.0, 0.1, 300.0, HG.UNCERTAIN, "mixed_sky", 25.0]
    hub._notify()
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "30.0"
    assert hass.states.get(SENSOR).attributes["reason"] == "mixed_sky"


async def test_no_forecast_and_no_learning_in_the_value(hass, zurich, freezer):
    fake, array, _, hub = await grey_morning(hass, freezer)
    await station_now(hass, fake, hub)
    coordinator = array.runtime_data
    calls = []
    original = coordinator.async_model_bands

    async def spy(records, temperatures, offset):
        calls.append((records, dict(temperatures)))
        return await original(records, temperatures, offset)

    coordinator.async_model_bands = spy
    hub.curves = None  # the same-day curves use the forecast temperature (12 °C)
    await hub.async_run_same_day(T_SIGNAL)
    house_calls = [c for c in calls if set(c[1].values()) == {8.7}]  # the same-day curves use 12 °C
    assert len(house_calls) == len(SD.K_GRID)
    for index, (records, temperatures) in enumerate(house_calls):
        assert set(temperatures.values()) == {8.7}  # the station's measured air temperature
        keys = sorted(L.hour_key(start) for start in records)
        assert records == SD.curve_records(keys, float(SD.K_GRID[index]), LAT, LON)  # synthetic light only
    before = list(hub.data.d["house_ghi"]["windows"][str(ts(8, 30))])
    # learned corrections, the same-day correction and its switch change nothing
    hub.data.d["stage2"]["groups"] = {"10K": {c: {"factor": 0.85} for c in L.STAGE2_CELLS}}
    hub.data.d["gate"] = {"passed": True, "days": 20, "reasons": [], "groups": {}, "improvement": 0.2}
    hub.data.d["same_day"]["gate"] = {"passed": True, "days": 8, "reasons": []}
    await hub.async_set_apply(True)
    await hub.async_set_same_day(True)
    del hub.data.d["house_ghi"]["windows"][str(ts(8, 30))]
    hub.house_curves = None
    await hub.async_run_same_day(T_SIGNAL + timedelta(minutes=5))
    assert hub.data.d["house_ghi"]["windows"][str(ts(8, 30))] == before


async def test_without_station_temperature_the_value_is_uncertain(hass, zurich, freezer):
    _, _, _, hub = await grey_morning(hass, freezer)  # the only station sample is 4.5 h old
    hub.house_curves = None
    hub.data.d["house_ghi"]["windows"].clear()
    await hub.async_run_same_day(T_SIGNAL)
    row = hub.data.d["house_ghi"]["windows"][str(ts(8, 30))]
    assert row[3:5] == [HG.UNCERTAIN, "temperature"]
    assert hub.house_temperature_ok is False


async def test_clipping_from_the_inverter_setting(hass, zurich, freezer):
    freezer.move_to(NOW)
    fake = Fake(hass)
    fake.publish()
    array = await setup_array(hass, options={**LOCAL, "inverter_power": 1000})  # 0.25 kWh per window
    _, hub = await setup_hub(hass, [array])
    assert hub._ac_limits_kwh() == {"10K": pytest.approx(0.25)}
    value = 1000.0
    for minute in range(0, 61, 15):
        freezer.move_to(datetime(2026, 10, 7, 8, 0, tzinfo=UTC) + timedelta(minutes=minute))
        hass.states.async_set(ENERGY, f"{value:.2f}", COUNTER_ATTRS)
        await hass.async_block_till_done()
        value += 0.24 if minute < 30 else 0.22  # 96 %, then 88 % of the limit
    freezer.move_to(T_SIGNAL)
    hass.states.async_set(ENERGY, f"{value - 0.22:.2f}", COUNTER_ATTRS, force_update=True)
    fake.publish(T_SIGNAL.replace(minute=0))
    fake.station(T_SIGNAL - timedelta(minutes=12), 200.0)
    await hass.async_block_till_done()
    hub.house_curves = None
    hub.data.d["house_ghi"]["windows"].clear()
    await hub.async_run_same_day(T_SIGNAL)
    windows = hub.data.d["house_ghi"]["windows"]
    assert windows[str(ts(8, 0))][3:5] == [HG.BAD, "clipping"] and windows[str(ts(8, 0))][0] is None
    assert windows[str(ts(8, 30))][3:5] == [HG.UNCERTAIN, "clipping"]


async def test_a_window_waits_for_its_counter_then_is_bad(hass, zurich, freezer):
    fake, _, _, hub = await grey_morning(hass, freezer)
    await station_now(hass, fake, hub)
    hub.counters["10K"] = [r for r in hub.counters["10K"] if r[0] < ts(8, 30)]
    hass.states.async_remove(ENERGY)
    await hub.async_run_same_day(T_SIGNAL)
    windows = hub.data.d["house_ghi"]["windows"]
    assert str(ts(8, 30)) not in windows and str(ts(8, 45)) not in windows  # may still be reported
    assert windows[str(ts(8, 0))][3:5] == [HG.BAD, "counter"]  # ended 47 min ago: no longer waits
    assert "waiting for counters" in hub.last_house_ghi["house_ghi"]
    later = datetime(2026, 10, 7, 9, 16, tzinfo=UTC)
    freezer.move_to(later)
    await hub.async_run_house_ghi(later)
    assert windows[str(ts(8, 30))][3:5] == [HG.BAD, "counter"]  # ended 31 min ago
    assert str(ts(8, 45)) not in windows  # ended 16 min ago: still waiting


async def test_snow_from_the_last_two_hours(hass, zurich, freezer):
    _, _, _, hub = await grey_morning(hass, freezer)
    section = hub.data.d["house_ghi"]
    start = ts(11, 0)
    for hour in (9, 10):
        key = ts(hour, 0)
        clear = sum(HG.window_clear_ghi(key + i * 900, LAT, LON) for i in range(4)) / 4
        row = [0.0] * len(L.HOUR_FIELDS)
        row[L.H["ghi"]], row[L.H["temp"]] = 0.9 * clear, 0.5
        hub.data.d["station_hours"][key] = row
        for i in range(4):
            section["windows"][str(key + i * 900)] = [None, 0.2, clear, HG.BAD, "snow", 30.0]
    assert hub._house_snow(start, section) is True
    hub.data.d["station_hours"][ts(10, 0)][L.H["temp"]] = 4.0
    assert hub._house_snow(start, section) is False
    # the station cannot tell (grey): the previous window decides
    for hour in (9, 10):
        hub.data.d["station_hours"][ts(hour, 0)][L.H["ghi"]] = 20.0
    assert hub._house_snow(start, section) is True
    section["windows"][str(ts(10, 45))][3:5] = [HG.UNCERTAIN, "mixed_sky"]
    section["windows"][str(ts(10, 45))][0] = 50.0
    assert hub._house_snow(start, section) is False


async def test_version_is_recorded_and_a_full_reset_keeps_the_windows(hass, zurich, freezer):
    fake, _, _, hub = await grey_morning(hass, freezer)
    await station_now(hass, fake, hub)
    await hub.async_run_same_day(T_SIGNAL)
    await hub.async_run_same_day(T_SIGNAL + timedelta(minutes=5))
    versions = [e for e in hub.data.d["events"] if e[1] == "house_ghi_version"]
    assert len(versions) == 1 and hub.data.d["house_ghi"]["version_history"][0][1] == HG.VERSION
    windows = dict(hub.data.d["house_ghi"]["windows"])
    await hub.async_reset("all")
    assert hub.data.d["house_ghi"]["windows"] == windows
    assert hub.data.d["same_day"]["days"] == {}


async def test_export_and_diagnostics(hass, zurich, freezer, tmp_path):
    fake, _, hub_entry, hub = await grey_morning(hass, freezer)
    await station_now(hass, fake, hub)
    await hub.async_run_same_day(T_SIGNAL)
    with patch.object(type(hub), "export_dir", lambda self: tmp_path):
        await hub.async_export(T_SIGNAL)
    lines = (tmp_path / "house_ghi.csv").read_text().splitlines()
    assert lines[0] == ",".join(HG.WINDOW_HEADER)
    assert len(lines) - 1 == len(hub.data.d["house_ghi"]["windows"])
    days = (tmp_path / "house_ghi_days.csv").read_text().splitlines()
    assert days[0].startswith("date,windows,good,uncertain,bad") and days[1].startswith("2026-10-07,")
    diag = await async_get_config_entry_diagnostics(hass, hub_entry)
    block = diag["data"]["house_ghi"] if "data" in diag else diag["house_ghi"]
    assert block["attributes"]["version"] == HG.VERSION and block["today"]
    assert block["version_history"][0][1] == HG.VERSION


async def test_a_new_signal_reaches_the_members_before_the_decision(hass, zurich, freezer):
    _, array, _, hub = await grey_morning(hass, freezer)
    await hass.async_block_till_done(wait_background_tasks=True)  # runs started by timers of the setup
    hub.signal = hub.signal_time = None  # whatever they did: no signal now
    hub.data.d["same_day"]["days"].pop("2026-10-07", None)
    await refresh(hass, array)  # the members report without a signal
    assert hub.member_same_day[array.entry_id]["bands"]["sdc"] is None
    await hub.async_run_same_day(T_SIGNAL)
    assert hub.signal.valid and "members refreshed" in hub.last_same_day["same_day"]
    decision = hub.data.d["same_day"]["days"]["2026-10-07"]["decisions"][str(ts(9, 0))]
    without, with_sdc, _ = decision["rem"]["10K"]
    assert with_sdc < without  # recorded with the fresh signal
    assert hub.member_same_day[array.entry_id]["updated"] == T_SIGNAL
