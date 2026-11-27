"""0.1.37.3: the station check (representativeness) uses only accepted days,
grows its window in winter, and pauses for a systematically unlike station
through the share of days rejected as not representative.

Owner case (22-27 Nov 2026): two broken-cloud days the day check had already
excluded pulled the 14-day correlation from 0.89 to 0.79 and paused a learning
whose out-of-sample improvement was +9.4 %.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.open_meteo_solar_forecast import learning as L

from .test_learning_engine import GROUP, LAT, LON, TZ, _day_hours_with_sun, _full_day
from .test_learning_hub import basic

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations):
    yield


def _build(data, first: date, days: int, status, coherent, seed=1):
    """Days from ``first``: model per hour varies; production follows it
    (coherent day) or is unrelated to it (broken clouds)."""
    rng = random.Random(seed)
    for i in range(days):
        day = (first + timedelta(days=i)).isoformat()
        _full_day(data, day, kt=0.5)
        model = data.d["model"]["10K"]
        production = data.d["production"]["10K"]
        for k in _day_hours_with_sun(day, L.MIN_ELEVATION_DEG):
            model[k] = rng.uniform(0.2, 4.0)
            production[k] = model[k] * rng.uniform(0.9, 1.1) if coherent(day) else rng.uniform(0.0, 6.0)
        st = status(day)
        if st is not None:
            data.d["days"][day] = {"status": st, "decided": 0, "details": {}}


def _now_after(first: date, days: int) -> datetime:
    last = first + timedelta(days=days)
    return datetime(last.year, last.month, last.day, 2, 30, tzinfo=UTC)


OCT = date(2026, 10, 1)  # about 3 blocks a day
NOV = date(2026, 11, 20)  # about 1-2 blocks a day


def test_rejected_days_do_not_pause_the_learning():
    bad = {(OCT + timedelta(days=i)).isoformat() for i in (9, 11)}
    data = L.LearningData()
    _build(data, OCT, 14, lambda d: "not_representative" if d in bad else L.DAY_OK, lambda d: d not in bad)
    rep = L.representativeness(data, [GROUP], _now_after(OCT, 14), LAT, LON, TZ)
    assert rep["representative"] and rep["r"] > 0.95 and rep["reason"] is None
    assert rep["rejected_days"] == 2 and rep["decided_days"] == 14
    # 0.1.37.2 rule (every day counted): the same data pauses
    for day in bad:
        data.d["days"][day]["status"] = L.DAY_OK
    old = L.representativeness(data, [GROUP], _now_after(OCT, 14), LAT, LON, TZ)
    assert not old["representative"] and old["reason"] == "correlation" and old["r"] < L.REPRESENTATIVE_MIN_R


def test_undecided_days_are_left_out():
    data = L.LearningData()
    _build(data, OCT, 14, lambda d: L.DAY_OK if d < "2026-10-13" else None, lambda d: d < "2026-10-13")
    rep = L.representativeness(data, [GROUP], _now_after(OCT, 14), LAT, LON, TZ)
    assert rep["representative"] and rep["r"] > 0.95 and rep["decided_days"] == 12


def test_a_station_unlike_the_house_still_pauses_through_the_correlation():
    data = L.LearningData()
    _build(data, OCT, 14, lambda d: L.DAY_OK, lambda d: False)  # accepted, but no relation
    rep = L.representativeness(data, [GROUP], _now_after(OCT, 14), LAT, LON, TZ)
    assert not rep["representative"] and rep["reason"] == "correlation"
    assert L.derive_state({"passed": True, "algorithm": L.ALGORITHM_VERSION}, True, False, True,
                          rep["reason"]) == (L.STATE_PAUSED, L.REPRESENTATIVE_REASON_CORRELATION)


@pytest.mark.parametrize(
    ("rejected", "decided", "paused"),
    [(3, 7, True), (2, 7, False), (3, 9, False), (4, 9, True), (3, 6, False), (0, 0, False), (7, 7, True)],
)
def test_share_of_rejected_days(rejected, decided, paused):
    data = L.LearningData()
    first = OCT + timedelta(days=14 - decided)
    _build(data, OCT, 14, lambda d: None, lambda d: True)  # coherent hours, undecided
    for i in range(decided):
        day = (first + timedelta(days=i)).isoformat()
        data.d["days"][day] = {"status": "not_representative" if i < rejected else L.DAY_OK,
                               "decided": 0, "details": {}}
    rep = L.representativeness(data, [GROUP], _now_after(OCT, 14), LAT, LON, TZ)
    assert rep["rejected_days"] == rejected and rep["decided_days"] == decided
    assert rep["representative"] is (not paused)
    if paused and rep["reason"] == "rejected_days":
        assert L.derive_state(None, True, False, False, rep["reason"]) == (
            L.STATE_PAUSED, L.REPRESENTATIVE_REASON_REJECTED)


def test_only_not_representative_counts_as_rejected():
    data = L.LearningData()
    reasons = ["snow", "production_fault", "station_incomplete", "snow", "production_fault", "snow", "snow"]
    _build(data, OCT, 14, lambda d: None, lambda d: True)
    for i, reason in enumerate(reasons):
        data.d["days"][(OCT + timedelta(days=i)).isoformat()] = {"status": reason, "decided": 0, "details": {}}
    rep = L.representativeness(data, [GROUP], _now_after(OCT, 14), LAT, LON, TZ)
    assert rep["decided_days"] == 7 and rep["rejected_days"] == 0 and rep["representative"]


def test_rejected_share_counts_only_the_window():
    data = L.LearningData()
    _build(data, OCT, 30, lambda d: "not_representative" if d < "2026-10-08" else L.DAY_OK, lambda d: True)
    now = _now_after(OCT, 30)  # window 14 days: 17-30 Oct, all accepted
    rep = L.representativeness(data, [GROUP], now, LAT, LON, TZ)
    assert rep["window_days"] == 14 and rep["rejected_days"] == 0 and rep["representative"]


def test_window_grows_in_winter_and_stops_at_28_days():
    data = L.LearningData()
    _build(data, NOV - timedelta(days=40), 40, lambda d: L.DAY_OK, lambda d: True)
    now = _now_after(NOV - timedelta(days=40), 40)
    rep = L.representativeness(data, [GROUP], now, LAT, LON, TZ)
    blocks_14 = L._representative_blocks(data, ["10K"], int(now.timestamp()), 14, LAT, LON, TZ)[0]
    assert len(blocks_14) < L.REPRESENTATIVE_TARGET_BLOCKS
    assert rep["window_days"] > 14 and rep["blocks"] > len(blocks_14)
    assert rep["window_days"] == 14 + 7 * ((rep["window_days"] - 14) // 7)
    # a window stops growing once it has enough blocks
    if rep["window_days"] < L.REPRESENTATIVE_MAX_WINDOW_DAYS:
        assert rep["blocks"] >= L.REPRESENTATIVE_TARGET_BLOCKS
    # never beyond 28 days
    sparse = L.LearningData()
    _build(sparse, NOV - timedelta(days=40), 40, lambda d: L.DAY_OK if d.endswith(("0", "5")) else None,
           lambda d: True)
    rep = L.representativeness(sparse, [GROUP], now, LAT, LON, TZ)
    assert rep["window_days"] == L.REPRESENTATIVE_MAX_WINDOW_DAYS and rep["blocks"] < L.REPRESENTATIVE_TARGET_BLOCKS


def test_summer_window_stays_at_14_days():
    data = L.LearningData()
    _build(data, date(2026, 6, 1), 14, lambda d: L.DAY_OK, lambda d: True)
    rep = L.representativeness(data, [GROUP], _now_after(date(2026, 6, 1), 14), LAT, LON, TZ)
    assert rep["window_days"] == 14 and rep["blocks"] >= L.REPRESENTATIVE_TARGET_BLOCKS


def test_result_fields():
    rep = L.representativeness(L.LearningData(), [GROUP], _now_after(OCT, 14), LAT, LON, TZ)
    assert rep == {"r": None, "hours": 0, "blocks": 0, "window_days": 28, "decided_days": 0, "rejected_days": 0,
                   "rejected_share": 0.0, "representative": True, "reason": None,
                   "basis": "3-hour sums, production vs model on station light, accepted days"}


def test_derive_state_reasons():
    assert L.derive_state(None, True, False, False) == (L.STATE_PAUSED, L.REPRESENTATIVE_REASON_CORRELATION)
    assert L.derive_state(None, True, False, False, "correlation")[1] == L.REPRESENTATIVE_REASON_CORRELATION
    assert L.derive_state(None, True, False, False, "rejected_days")[1] == L.REPRESENTATIVE_REASON_REJECTED


# -- hub ----------------------------------------------------------------------------------------


async def test_startup_re_evaluates_a_result_of_the_old_rule(hass, freezer):
    _, _, _, hub = await basic(hass, freezer)
    now = datetime(2026, 10, 7, 6, tzinfo=UTC)
    hub.data.d["representativeness"] = {"r": 0.79, "hours": 70, "blocks": 26, "representative": False,
                                        "basis": "3-hour sums, production vs model on station light"}
    hub._recompute_state(now)
    assert hub.data.d["state"]["reason"] == L.REPRESENTATIVE_REASON_CORRELATION
    await hub._async_refresh_old_representativeness(now)
    rep = hub.data.d["representativeness"]
    assert "window_days" in rep and rep["representative"]  # no accepted days yet: not paused
    assert hub.data.d["state"]["reason"] != L.REPRESENTATIVE_REASON_CORRELATION
    assert any(e[1] == "station_representative" for e in hub.data.d["events"])


async def test_startup_keeps_a_result_of_the_new_rule(hass, freezer):
    _, _, _, hub = await basic(hass, freezer)
    now = datetime(2026, 10, 7, 6, tzinfo=UTC)
    current = {"r": 0.5, "window_days": 14, "representative": False, "reason": "correlation"}
    hub.data.d["representativeness"] = dict(current)
    await hub._async_refresh_old_representativeness(now)
    assert hub.data.d["representativeness"] == current
    hub.data.d["representativeness"] = None
    await hub._async_refresh_old_representativeness(now)  # nothing stored: the nightly run evaluates
    assert hub.data.d["representativeness"] is None


async def test_nightly_records_the_reason_in_the_event(hass, freezer):
    _, _, _, hub = await basic(hass, freezer)
    now = datetime(2026, 10, 20, 2, 30, tzinfo=UTC)
    for i in range(10):
        day = (date(2026, 10, 6) + timedelta(days=i)).isoformat()
        hub.data.d["days"][day] = {"status": "not_representative" if i < 4 else L.DAY_OK, "decided": 0,
                                   "details": {}}
    hub.data.d["representativeness"] = {"representative": True, "window_days": 14}
    await hub._async_representativeness(now)
    rep = hub.data.d["representativeness"]
    assert not rep["representative"] and rep["reason"] == "rejected_days"
    events = [e for e in hub.data.d["events"] if e[1] == "station_unrepresentative"]
    assert len(events) == 1 and "4 of 10 days rejected" in events[0][2]
    hub._recompute_state(now)
    assert hub.data.d["state"]["reason"] == L.REPRESENTATIVE_REASON_REJECTED
    # the whole check is visible on the station correlation sensor
    hub._notify()
    await hass.async_block_till_done()
    attributes = hass.states.get("sensor.solar_forecast_learning_station_correlation").attributes
    assert attributes["rejected_days"] == 4 and attributes["decided_days"] == 10
    assert attributes["window_days"] == rep["window_days"] and attributes["reason"] == "rejected_days"
    assert attributes["max_rejected_share"] == 0.333 and attributes["required_r"] == L.REPRESENTATIVE_MIN_R
