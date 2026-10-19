"""Same-day correction in Home Assistant (0.1.35): counters, response
curves, signal, decisions, serving, cautious bands, nightly learning."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.open_meteo_solar_forecast import learning as L
from custom_components.open_meteo_solar_forecast import same_day as SD
from custom_components.open_meteo_solar_forecast.diagnostics import async_get_config_entry_diagnostics

from .fusion_fixtures import LAT, LON
from .test_learning_hub import ENERGY, basic, refresh, today_kwh

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


@pytest.fixture
async def zurich(hass):
    await hass.config.async_set_time_zone("Europe/Zurich")
    hass.config.latitude, hass.config.longitude = LAT, LON
    yield


COUNTER_ATTRS = {"unit_of_measurement": "kWh", "state_class": "total_increasing", "device_class": "energy"}
T_SIGNAL = datetime(2026, 10, 7, 9, 2, tzinfo=UTC)  # 11:02 local: 11:00 is a decision time


async def grey_morning(hass, freezer, per_band_kwh=0.02):
    """A hub with one member and a counter that rose by ``per_band_kwh``
    per quarter hour between 08:00 and 09:00 UTC (a grey sky)."""
    fake, array, hub_entry, hub = await basic(hass, freezer)
    value = 1000.0
    for minute in range(0, 61, 15):
        freezer.move_to(datetime(2026, 10, 7, 8, 0, tzinfo=UTC) + timedelta(minutes=minute))
        hass.states.async_set(ENERGY, f"{value:.2f}", COUNTER_ATTRS)
        await hass.async_block_till_done()
        value += per_band_kwh
    freezer.move_to(T_SIGNAL)
    fake.publish(T_SIGNAL.replace(minute=0))
    hass.states.async_set(ENERGY, f"{value - per_band_kwh:.2f}", COUNTER_ATTRS, force_update=True)
    await hass.async_block_till_done()
    await refresh(hass, array)  # the member reports its light and bands
    return fake, array, hub_entry, hub


async def test_counter_readings_are_kept_with_their_time_and_unit(hass, zurich, freezer):
    _, _, _, hub = await basic(hass, freezer)
    hass.states.async_set(ENERGY, "2500", {**COUNTER_ATTRS, "unit_of_measurement": "Wh"})
    await hass.async_block_till_done()
    assert hub.counters["10K"][-1][1] == pytest.approx(2.5)
    for minutes, value, unit in ((1, "unavailable", "kWh"), (2, "-3", "kWh"), (3, "5", "kW")):
        freezer.tick(timedelta(minutes=minutes))
        hass.states.async_set(ENERGY, value, {**COUNTER_ATTRS, "unit_of_measurement": unit})
        await hass.async_block_till_done()
        assert hub.counters["10K"][-1][1] == pytest.approx(2.5), value


async def test_grey_morning_gives_a_negative_signal_and_a_decision_record(hass, zurich, freezer):
    _, array, _, hub = await grey_morning(hass, freezer)
    await hub.async_run_same_day(T_SIGNAL)
    signal = hub.signal
    assert signal is not None and signal.valid, hub.last_same_day
    assert signal.k_meas < 0.2 and signal.e < -0.5  # the forecast is a clear sky
    assert signal.clear_kwh >= SD.MIN_WINDOW_CLEAR_KWH
    record = hub.data.d["same_day"]["days"]["2026-10-07"]
    band8 = str(int(datetime(2026, 10, 7, 8, 0, tzinfo=UTC).timestamp()))
    assert record["bands"][band8]["m"]["10K"] == pytest.approx(0.02)
    assert record["bands"][band8]["k"] is not None and record["bands"][band8]["k"] < 0.2
    decision = record["decisions"][str(int(T_SIGNAL.replace(minute=0).timestamp()))]
    assert decision["e"] == pytest.approx(signal.e, abs=1e-4)
    base, with_sdc, cautious = decision["rem"]["10K"]
    assert base > 0 and cautious is None  # no cautious bands without 10 usable days
    # the member's next refresh computes the correction in shadow: nothing served
    before = today_kwh(hass)
    coordinator = await refresh(hass, array)
    report = coordinator.learning_report["same_day"]
    assert report["valid"] and not report["served"] and report["state"] == "collecting"
    assert report["today_with_wh"] < report["today_without_wh"]
    assert today_kwh(hass) == before
    assert "watts_cautious" not in hass.states.get("sensor.south_energy_production_today").attributes
    assert hass.states.get("sensor.solar_forecast_learning_same_day_correction").state == str(round(signal.e, 3))


async def test_served_only_with_check_passed_and_switch_on(hass, zurich, freezer):
    _, array, _, hub = await grey_morning(hass, freezer)
    await hub.async_run_same_day(T_SIGNAL)
    before = today_kwh(hass)
    await hass.services.async_call("switch", "turn_on",
                                   {"entity_id": "switch.solar_forecast_learning_same_day_switch"}, blocking=True)
    await refresh(hass, array)
    assert today_kwh(hass) == before and not hub.same_day_applies  # switch on, check not passed
    await hass.services.async_call("switch", "turn_off",
                                   {"entity_id": "switch.solar_forecast_learning_same_day_switch"}, blocking=True)
    hub.data.d["same_day"]["gate"] = {"passed": True, "days": 8, "reasons": []}
    await refresh(hass, array)
    assert today_kwh(hass) == before  # check passed, switch off
    assert hub.same_day_state == "eligible"
    await hass.services.async_call("switch", "turn_on",
                                   {"entity_id": "switch.solar_forecast_learning_same_day_switch"}, blocking=True)
    assert hub.same_day_applies and hub.same_day_state == "applied"
    await refresh(hass, array)
    assert today_kwh(hass) < before
    # a signal from a run more than 15 min ago is never used
    hub.last_same_day["signal_time"] = T_SIGNAL - timedelta(minutes=16)
    await refresh(hass, array)
    assert today_kwh(hass) == before


async def test_cautious_bands_after_enough_days_never_above_served(hass, zurich, freezer):
    _, array, _, hub = await grey_morning(hass, freezer)
    await hub.async_run_same_day(T_SIGNAL)
    n = len(SD.LEAD_BUCKETS_H)
    table = SD.Table(beta={s: list(SD.PRIOR_BETA) for s in SD.SITUATIONS},
                     sigma={s: [0.2] * n for s in SD.SITUATIONS}, days={s: [10] * n for s in SD.SITUATIONS},
                     sigma_u=0.3, usable_days=SD.CAUTIOUS_MIN_DAYS)
    hub.data.d["same_day"]["table"] = {"computed": "x", **table.to_json()}
    await refresh(hass, array)
    attrs = hass.states.get("sensor.south_energy_production_today").attributes
    cautious, served = attrs["watts_cautious"], attrs["watts"]
    assert cautious and all(cautious[k] <= served[k] for k in cautious)
    assert sum(cautious.values()) < sum(served.values())


async def test_nothing_happens_before_sunrise_or_without_member_reports(hass, zurich, freezer):
    _, _, _, hub = await basic(hass, freezer)  # 06:30 local, before sunrise
    await hub.async_run_same_day(datetime(2026, 10, 7, 4, 30, tzinfo=UTC))
    assert hub.signal is not None and hub.signal.reason == "before sunrise"
    assert hub.data.d["same_day"]["days"] == {}
    hub.member_same_day.clear()
    freezer.move_to(T_SIGNAL)
    await hub.async_run_same_day(T_SIGNAL)
    assert "waiting" in hub.signal.reason and hub.last_same_day["same_day"] == "waiting for member refresh"
    assert not any(e[1] == "step_failed" for e in hub.data.d["events"])


async def test_a_failing_run_leaves_no_signal_and_the_forecast_unchanged(hass, zurich, freezer):
    _, array, _, hub = await grey_morning(hass, freezer)
    await hub.async_run_same_day(T_SIGNAL)
    assert hub.signal.valid

    async def broken(*_args, **_kwargs):
        raise RuntimeError("model failed")

    hub.curves = None
    coordinator = array.runtime_data
    original = coordinator.async_model_bands
    coordinator.async_model_bands = broken
    try:
        await hub.async_run_same_day(T_SIGNAL + timedelta(minutes=5))
    finally:
        coordinator.async_model_bands = original
    assert hub.signal is None and hub.last_same_day["same_day"].startswith("failed")
    report = (await refresh(hass, array)).learning_report["same_day"]
    assert not report["valid"] and report["today_with_wh"] is None


async def test_nightly_learns_and_checks(hass, zurich, freezer):
    _, _, _, hub = await grey_morning(hass, freezer)
    await hub.async_run_same_day(T_SIGNAL)
    night = datetime(2026, 10, 8, 1, 30, tzinfo=UTC)
    result = await hub._async_same_day_nightly(night)
    section = hub.data.d["same_day"]
    assert section["table"]["usable_days"] == 0 and section["gate"]["days"] == 0 and not section["gate"]["passed"]
    assert "usable days" in result and section["gate_history"]
    assert hass.states.get("sensor.solar_forecast_learning_same_day_improvement") is not None


async def test_same_day_in_diagnostics_and_store(hass, hass_storage, zurich, freezer):
    _, _, hub_entry, hub = await grey_morning(hass, freezer)
    await hub.async_run_same_day(T_SIGNAL)
    diag = await async_get_config_entry_diagnostics(hass, hub_entry)
    assert diag["data"]["same_day"]["signal"]["valid"] if "data" in diag else diag["same_day"]["signal"]["valid"]
    raw = L.LearningData.from_json(__import__("json").loads(__import__("json").dumps(hub.data.to_json())))
    assert raw.d["same_day"]["days"]["2026-10-07"]["decisions"]


async def test_a_missed_decision_time_is_not_recorded_late(hass, zurich, freezer):
    _, _, _, hub = await grey_morning(hass, freezer)
    late = T_SIGNAL.replace(minute=0) + timedelta(minutes=16)  # the 11:00 slot passed 16 min ago
    freezer.move_to(late)
    await hub.async_run_same_day(late)
    record = hub.data.d["same_day"]["days"]["2026-10-07"]
    assert str(int(T_SIGNAL.replace(minute=0).timestamp())) not in record["decisions"]


async def test_a_band_waits_for_its_counter_then_is_kept_as_unknown(hass, zurich, freezer):
    _, _, _, hub = await grey_morning(hass, freezer)
    hub.counters["10K"] = [r for r in hub.counters["10K"] if r[0] < datetime(2026, 10, 7, 8, 30, tzinfo=UTC).timestamp()]
    hass.states.async_remove(ENERGY)  # the counter no longer reports: nothing after 08:30 is known
    await hub.async_run_same_day(T_SIGNAL)
    record = hub.data.d["same_day"]["days"]["2026-10-07"]
    band_845 = str(int(datetime(2026, 10, 7, 8, 45, tzinfo=UTC).timestamp()))
    band_800 = str(int(datetime(2026, 10, 7, 8, 0, tzinfo=UTC).timestamp()))
    assert band_845 not in record["bands"]  # ended 17 min ago: may still be reported
    assert record["bands"][band_800]["m"]["10K"] is None  # ended over 30 min ago: kept as unknown
    assert not hub.signal.valid and "no counter reading" in hub.signal.reason


async def test_cautious_attribute_is_never_above_the_served_bands(hass, zurich, freezer):
    from dataclasses import replace

    _, array, _, hub = await grey_morning(hass, freezer)
    coordinator = array.runtime_data
    served = coordinator.data
    coordinator.cautious_estimate = replace(served, watts={k: v * 2 + 1 for k, v in served.watts.items()})
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    attrs = hass.states.get("sensor.south_energy_production_today").attributes
    assert attrs["watts_cautious"] and all(attrs["watts_cautious"][k] == attrs["watts"][k] for k in attrs["watts_cautious"])
