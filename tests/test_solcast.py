"""Solcast as a second light forecast (0.1.37): the pure engine."""

from __future__ import annotations

import ast
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.open_meteo_solar_forecast import learning as L
from custom_components.open_meteo_solar_forecast import same_day as SD
from custom_components.open_meteo_solar_forecast import solcast as SC

from .fusion_fixtures import LAT, LON

UTC = timezone.utc
TZ = ZoneInfo("Europe/Zurich")
DAY = "2026-10-07"
SUNRISE = L.sunrise_utc(DAY, TZ, LAT, LON)
NOON_KEY = int(datetime(2026, 10, 7, 11, tzinfo=UTC).timestamp())


def at(hour, minute=0, day=7):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def answer(start: datetime, hours: int = 60, p=0.4, spread=0.1, fraction=".0000000Z"):
    """A Solcast-style answer with half-hour periods from ``start``."""
    items = []
    t = start.replace(minute=0, second=0) + timedelta(minutes=30)
    for _ in range(hours * 2):
        items.append({"pv_estimate": p, "pv_estimate10": max(p - spread, 0.0), "pv_estimate90": p + spread,
                      "period_end": t.strftime("%Y-%m-%dT%H:%M:%S") + fraction, "period": "PT30M"})
        t += timedelta(minutes=30)
    return {"forecasts": items}


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------


def test_module_is_pure():
    source = Path(SC.__file__).read_text()
    tree = ast.parse(source)
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert not any(name.startswith(("homeassistant", "aiohttp", "random")) for name in imported)
    for forbidden in ("utcnow", "datetime.now", "time.time(", "open(", "random."):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# Calls: schedule and budget
# ---------------------------------------------------------------------------


def test_slots_are_local_times_and_sunrise_plus_40():
    slots = SC.slots_for(DAY, TZ, SUNRISE)
    assert list(slots)[0] == "05:07"
    assert slots["05:07"] == datetime(2026, 10, 7, 3, 7, tzinfo=UTC)  # CEST
    assert slots["sunrise+40"] == SUNRISE + timedelta(minutes=40)
    assert len(slots) == len(SC.SLOT_TIMES_LOCAL) + 1
    winter = SC.slots_for("2026-12-07", TZ, None)
    assert winter["05:07"] == datetime(2026, 12, 7, 4, 7, tzinfo=UTC)  # CET
    assert "sunrise+40" not in winter


def test_a_slot_is_called_once_within_its_window():
    section = SC.new_section()
    slot_time = SUNRISE + timedelta(minutes=40)
    assert SC.due_call(section, slot_time - timedelta(seconds=1), TZ, SUNRISE, 10) is None
    assert SC.due_call(section, slot_time, TZ, SUNRISE, 10) == "sunrise+40"
    late = slot_time + timedelta(seconds=SC.SLOT_WINDOW_S)
    assert SC.due_call(section, late, TZ, SUNRISE, 10) is None  # missed by 45 min: skipped
    SC.note_call(section, slot_time, TZ, "sunrise+40")
    assert SC.due_call(section, slot_time + timedelta(minutes=5), TZ, SUNRISE, 10) is None
    assert SC.calls_today(section, slot_time) == 1


def test_budget_stops_planned_calls_one_below_the_limit_and_keeps_the_reserve():
    section = SC.new_section()
    now = at(7, 40)  # 09:40 local
    section["calls"][SC.utc_day(now)] = 8
    assert SC.due_call(section, now, TZ, SUNRISE, 10) == "09:37"
    section["calls"][SC.utc_day(now)] = 9
    assert SC.due_call(section, now, TZ, SUNRISE, 10) is None  # planned budget used
    section["retry_at"] = int(now.timestamp()) - 1
    assert SC.due_call(section, now, TZ, SUNRISE, 10) == "retry"  # the reserve
    section["calls"][SC.utc_day(now)] = 10
    assert SC.due_call(section, now, TZ, SUNRISE, 10) is None  # never above the limit
    section["calls"][SC.utc_day(now)] = 0
    section["retry_at"] = int(now.timestamp()) + 60
    assert SC.due_call(section, now, TZ, SUNRISE, 10) == "09:37"  # retry not yet due


def test_budget_counts_per_utc_day():
    section = SC.new_section()
    late_local = datetime(2026, 10, 7, 23, 30, tzinfo=UTC)  # 01:30 on 8 Oct local, still 7 Oct UTC
    SC.note_call(section, late_local, TZ, "retry")
    assert section["calls"] == {"2026-10-07": 1}
    assert SC.calls_today(section, datetime(2026, 10, 8, 0, 5, tzinfo=UTC)) == 0


def test_results_failures_and_one_retry():
    section = SC.new_section()
    now = at(9, 40)
    SC.note_result(section, now, "HTTP 429", retry=True, jitter=0.0)
    assert section["failures"] == 1 and section["retry_at"] == int(now.timestamp()) + SC.RETRY_MIN_S
    SC.note_result(section, now, "HTTP 429", retry=True, jitter=0.99)
    assert section["retry_at"] == int(now.timestamp()) + SC.RETRY_MIN_S  # one retry pending at most
    SC.note_call(section, now, TZ, "retry")
    assert section["retry_at"] is None
    other = SC.new_section()
    SC.note_result(other, now, "HTTP 500", retry=True, jitter=1.0)
    assert other["retry_at"] == int(now.timestamp()) + SC.RETRY_MAX_S
    SC.note_result(other, now, "HTTP 401: API key rejected", retry=False, jitter=0.5)
    assert other["failures"] == 2
    SC.note_result(other, now, None, retry=False, jitter=0.0)
    assert other["failures"] == 0 and other["last_error"] is None and other["last_success"] == now.isoformat()


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------


def test_parse_forecast():
    now = at(6, 10)
    periods = SC.parse_forecast(answer(now), now)
    first = str(int(at(6, 30).timestamp()))
    assert periods[first] == [0.4, 0.3, 0.5]
    assert max(int(k) for k in periods) <= now.timestamp() + (SC.FORECAST_HOURS + 1) * 3600
    # offsets and plain seconds are fine
    assert SC.parse_forecast(answer(now, fraction="+00:00"), now)[first] == [0.4, 0.3, 0.5]
    bad = [
        [], {"forecasts": "x"}, {"forecasts": [1]},
        {"forecasts": [{**answer(now)["forecasts"][0], "pv_estimate": "1"}]},
        {"forecasts": [{**answer(now)["forecasts"][0], "pv_estimate": -0.1, "pv_estimate10": -0.2}]},
        {"forecasts": [{**answer(now)["forecasts"][0], "pv_estimate90": 1.6}]},
        {"forecasts": [{**answer(now)["forecasts"][0], "pv_estimate": float("nan")}]},
        {"forecasts": [{**answer(now)["forecasts"][0], "period": "PT15M"}]},
        {"forecasts": [{**answer(now)["forecasts"][0], "pv_estimate10": 0.45}]},
        {"forecasts": [{**answer(now)["forecasts"][0], "period_end": "2026-10-07T06:20:00Z"}]},
        {"forecasts": [{**answer(now)["forecasts"][0], "period_end": "7 Oct"}]},
        {"forecasts": []},
        answer(now - timedelta(days=3), hours=10),  # nothing in the next hours
    ]
    for payload in bad:
        with pytest.raises(SC.SolcastError):
            SC.parse_forecast(payload, now)


def test_latest_issue_and_hour_power():
    section = SC.new_section()
    now = at(6, 10)
    assert SC.latest_issue(section, now) is None
    SC.store_issue(section, now - timedelta(hours=13), {"1": [0.1, 0.1, 0.1]})
    assert SC.latest_issue(section, now) is None  # older than 12 h
    periods = SC.parse_forecast(answer(now, p=0.3), now)
    SC.store_issue(section, now, periods)
    issued, latest = SC.latest_issue(section, now + timedelta(hours=1))
    assert issued == int(now.timestamp()) and latest == periods
    key = int(at(8).timestamp())
    latest[str(key + 1800)] = [0.2, 0.1, 0.3]
    assert SC.hour_power(latest, key) == pytest.approx((0.25, 0.15, 0.35))
    del latest[str(key + 3600)]
    assert SC.hour_power(latest, key) is None  # both halves needed


# ---------------------------------------------------------------------------
# Light, weight, combination
# ---------------------------------------------------------------------------


def test_buckets_and_lead():
    assert [SC.bucket_of(x) for x in (0.0, 2.99, 3.0, 5.9, 6.0, 17.9, 18.0, 29.9, 30.0, 53.9)] == [
        0, 0, 1, 1, 2, 2, 3, 3, 4, 4]
    assert SC.bucket_of(-0.1) is None and SC.bucket_of(54.0) is None
    assert SC.lead_hours(NOON_KEY, NOON_KEY - 3600) == 1.5


def test_solcast_light_by_hand():
    e0h, elevation = L.hour_geometry(NOON_KEY, LAT, LON)
    assert elevation > 30
    assert SC.kt_solcast(0.2, 1150.0, NOON_KEY, LAT, LON) == pytest.approx(1150.0 * 0.2 / e0h)
    clear = SD.clear_kt(NOON_KEY, LAT, LON)
    assert SC.kt_solcast(1.4, 1500.0, NOON_KEY, LAT, LON) == pytest.approx(1.05 * clear)
    night = int(datetime(2026, 10, 7, 1, tzinfo=UTC).timestamp())
    assert SC.kt_solcast(0.1, 1150.0, night, LAT, LON) is None


def test_blend_shift_by_hand():
    assert SC.blend_shift(0.5, 0.6, 0.2, 0.0) == (0.0, 0.0)
    d_kt, d_kd = SC.blend_shift(0.5, 0.6, 0.2, 0.5)
    assert d_kt == pytest.approx(-0.15)  # 0.5 + 0.5 x (0.2 - 0.5) = 0.35
    diffuse = 0.5 * 0.6 * 0.5 + 0.5 * SD.erbs(0.2) * 0.2
    assert 0.6 + d_kd == pytest.approx(diffuse / 0.35)
    d_kt, d_kd = SC.blend_shift(0.5, 0.6, 0.2, 1.0)
    assert 0.5 + d_kt == pytest.approx(0.2) and 0.6 + d_kd == pytest.approx(SD.erbs(0.2))
    assert SC.blend_shift(0.0, 1.0, 0.0, 0.5) == (0.0, 0.0)


def test_blend_weight_by_lead():
    blend = SC.Blend(1000.0, (0.9, 0.5, 0.3, 0.2, 0.1))
    assert [blend.weight(x) for x in (1.0, 4.0, 10.0, 20.0, 40.0, 60.0)] == [0.9, 0.5, 0.3, 0.2, 0.1, 0.0]
    assert SC.Blend().weights == (0.0,) * 5 and SC.Blend().c == SC.C_PRIOR


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------


def test_record_pair_keeps_the_lead_nearest_the_bucket_centre():
    section = SC.new_section()
    key = NOON_KEY
    power = (0.3, 0.2, 0.4)
    assert SC.record_pair(section, key, 0.5, power, 1, key - 2 * 3600) is True  # lead 2.5 h, bucket 0
    assert SC.record_pair(section, key, 0.6, power, 2, key - 1 * 3600) is True  # lead 1.5 h: the centre
    assert SC.record_pair(section, key, 0.7, power, 3, key - 0.5 * 3600) is False  # 1.0 h: farther
    assert section["pairs"][str(key)]["0"] == [1.5, 0.6, 0.3, 0.2, 0.4, 2]
    assert SC.record_pair(section, key, 0.5, power, 4, key - 11.5 * 3600) is True  # lead 12 h, bucket 2
    assert SC.record_pair(section, key, 0.5, None, 5, key - 4 * 3600) is False
    assert SC.record_pair(section, key, 0.5, power, 6, key - 60 * 3600) is False  # beyond 54 h
    assert set(section["pairs"][str(key)]) == {"0", "2"}


def lit_keys(days, start_day=1):
    keys = []
    for d in range(start_day, start_day + days):
        for h in range(9, 15):  # 11:00-16:00 local, sun high in October
            keys.append(int(datetime(2026, 10, d, h, tzinfo=UTC).timestamp()))
    return keys


def synthetic(days, ours_err, solcast_err, seed=1, c_true=1000.0, start_day=1):
    """Pairs and truth: truth kt varies; ours and Solcast carry their own
    errors (std ours_err, solcast_err)."""
    rng = random.Random(seed)
    pairs, truth = {}, {}
    for key in lit_keys(days, start_day):
        e0h, elevation = L.hour_geometry(key, LAT, LON)
        kt_true = rng.uniform(0.15, 0.6)
        truth[key] = SC.Truth(kt_true * e0h, e0h, elevation)
        kt_s = max(kt_true + rng.gauss(0, solcast_err), 0.02)
        power = kt_s * e0h / c_true
        for b, lead in enumerate(SC.BUCKET_CENTRES_H):
            kt_o = min(max(kt_true + rng.gauss(0, ours_err), 0.0), 1.0)
            pairs.setdefault(str(key), {})[str(b)] = [lead, kt_o, power, power * 0.8, power * 1.2, key - 3600]
    return pairs, truth


def test_factor_is_learned_and_shrunk():
    pairs, truth = synthetic(10, 0.1, 0.0, c_true=1000.0)
    first, last = min(truth), max(truth) + 1
    c, n = SC.learn_factor(pairs, truth, first, last)
    assert n == 60
    # least squares through the origin gives 1000 exactly; the prior adds 30 hours at 1150
    sum_pp = sum(pairs[str(k)]["0"][2] ** 2 for k in truth)
    mean_pp = sum_pp / n
    expected = (1000.0 * sum_pp + 30 * mean_pp * 1150.0) / (sum_pp + 30 * mean_pp)
    assert c == pytest.approx(expected)
    assert SC.learn_factor(pairs, truth, first, first + 3 * 86400)[0] == SC.C_PRIOR  # too few hours
    assert SC.learn_factor(pairs, truth, last, last + 86400) == (SC.C_PRIOR, 0)


def test_weights_follow_the_better_forecast_and_stay_in_range():
    pairs, truth = synthetic(20, ours_err=0.15, solcast_err=0.0)  # Solcast perfect
    first, last = min(truth), max(truth) + 1
    weights, counts = SC.learn_weights(pairs, truth, first, last, 1000.0, LAT, LON)
    assert counts == (120,) * 5
    assert all(w == pytest.approx(120 / 180, abs=0.05) for w in weights)  # w_hat ≈ 1, shrunk by 60 hours
    pairs, truth = synthetic(20, ours_err=0.0, solcast_err=0.15)  # ours perfect: Solcast adds nothing
    weights, _ = SC.learn_weights(pairs, truth, first, last, 1000.0, LAT, LON)
    assert all(w < 0.05 for w in weights)
    pairs, truth = synthetic(20, ours_err=0.1, solcast_err=0.1, seed=3)  # equally good: about half
    weights, _ = SC.learn_weights(pairs, truth, first, last, 1000.0, LAT, LON)
    assert all(0.15 < w < 0.55 for w in weights)


def test_weight_by_hand_and_clamps():
    key = NOON_KEY
    e0h, elevation = L.hour_geometry(key, LAT, LON)
    truth = {key: SC.Truth(0.4 * e0h, e0h, elevation)}
    power = 0.2 * e0h / 1000.0  # Solcast kt 0.2
    pairs = {str(key): {"0": [1.5, 0.6, power, power, power, 0]}}
    weights, counts = SC.learn_weights(pairs, truth, key, key + 1, 1000.0, LAT, LON)
    # r = 0.4 - 0.6 = -0.2, d = 0.2 - 0.6 = -0.4: w_hat = 0.5, shrunk 1/(1+60)
    assert weights[0] == pytest.approx(round(0.5 / 61, 4)) and counts[0] == 1
    pairs[str(key)]["0"][1] = 0.3  # truth 0.4 above ours 0.3, Solcast 0.2 below: w_hat < 0 -> 0
    assert SC.learn_weights(pairs, truth, key, key + 1, 1000.0, LAT, LON)[0][0] == 0.0
    low = int(datetime(2026, 10, 7, 5, tzinfo=UTC).timestamp())  # sun below 10°
    e0, el = L.hour_geometry(low, LAT, LON)
    assert el < 10
    assert SC.learn_weights({str(low): {"0": [1.5, 0.6, 0.01, 0.01, 0.01, 0]}},
                            {low: SC.Truth(10.0, e0, el)}, low, low + 1, 1000.0, LAT, LON)[1][0] == 0


def test_learning_never_looks_ahead():
    pairs, truth = synthetic(10, ours_err=0.15, solcast_err=0.0)
    until = int(datetime(2026, 10, 6, tzinfo=UTC).timestamp())
    blend, info = SC.learn_blend(pairs, truth, until, LAT, LON)
    assert info["hours"] == [30] * 5  # 1-5 Oct only (6 hours each)
    later = {k: v for k, v in pairs.items() if int(k) >= until}
    early_only = {k: v for k, v in pairs.items() if int(k) < until}
    assert SC.learn_blend(early_only, truth, until, LAT, LON)[0] == blend
    assert later  # the later days exist and were ignored


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def gate_case(days, ours_err, solcast_err, energy_worse=False, seed=5):
    """28 days: pairs for learning, a decision record per day, energies."""
    pairs, truth = synthetic(days, ours_err, solcast_err, seed=seed)
    section = SC.new_section()
    section["pairs"] = pairs
    production = {"10K": {}}
    for d in range(1, days + 1):
        day = f"2026-10-{d:02d}"
        captured = int(datetime(2026, 10, d, 6, 30, tzinfo=UTC).timestamp())
        hours = {}
        for key in lit_keys(1, d):
            row = pairs[str(key)]["1"]  # 3-6 h lead at 06:30
            hours[key] = (row[1], row[2], truth[key].e0h)
        SC.record_decision_hours(section, day, datetime.fromtimestamp(captured, tz=UTC), hours)
        section["decisions"][day]["captured"] = captured
        SC.record_decision_energy(section, day, "a", 10000.0, 12000.0 if energy_worse else 10500.0)
        production["10K"][day] = 10.6
    return section, truth, production


def test_gate_passes_when_solcast_is_better_out_of_sample():
    section, truth, production = gate_case(28, ours_err=0.15, solcast_err=0.02)
    gate = SC.evaluate_gate(section, truth, {"10K": ["a"]}, production, "2026-10-29", LAT, LON)
    assert gate["passed"], gate["reasons"]
    assert gate["days"] >= 14 and gate["improvement"] > 0.2 and gate["better_recent"] >= 5
    assert gate["groups"]["10K"]["mae_blend_kwh"] < gate["groups"]["10K"]["mae_ours_kwh"]


def test_gate_fails_when_solcast_adds_nothing():
    section, truth, production = gate_case(28, ours_err=0.0, solcast_err=0.2)
    gate = SC.evaluate_gate(section, truth, {"10K": ["a"]}, production, "2026-10-29", LAT, LON)
    assert not gate["passed"] and (gate["improvement"] or 0) < 0.05


def test_gate_needs_days_energy_and_recent_days():
    section, truth, production = gate_case(10, ours_err=0.15, solcast_err=0.02)
    gate = SC.evaluate_gate(section, truth, {"10K": ["a"]}, production, "2026-10-11", LAT, LON)
    assert not gate["passed"] and any("days with a decision forecast" in r for r in gate["reasons"])
    section, truth, production = gate_case(28, ours_err=0.15, solcast_err=0.02, energy_worse=True)
    gate = SC.evaluate_gate(section, truth, {"10K": ["a"]}, production, "2026-10-29", LAT, LON)
    assert not gate["passed"] and "10K: daily energy error would get worse" in gate["reasons"]
    gate = SC.evaluate_gate(section, truth, {}, production, "2026-10-29", LAT, LON)
    assert not gate["passed"]


def test_gate_is_out_of_sample():
    """A day is combined with weights learned only before it: the first
    day has no history, so it is judged with weight 0 (no difference)."""
    section, truth, production = gate_case(28, ours_err=0.15, solcast_err=0.02)
    first = SC.evaluate_gate({**section, "decisions": {"2026-10-01": section["decisions"]["2026-10-01"]}},
                             truth, {"10K": ["a"]}, production, "2026-10-29", LAT, LON)
    assert first["rmse_ours_w_m2"] == first["rmse_blend_w_m2"]


def test_decision_records_once():
    section = SC.new_section()
    assert SC.record_decision_hours(section, DAY, at(6, 30), {NOON_KEY: (0.5, 0.2, 900.0)})
    assert not SC.record_decision_hours(section, DAY, at(6, 40), {NOON_KEY: (0.1, 0.1, 900.0)})
    assert section["decisions"][DAY]["hours"][str(NOON_KEY)] == [0.5, 0.2, 900.0]
    assert SC.record_decision_energy(section, DAY, "a", 1.0, 2.0)
    assert not SC.record_decision_energy(section, DAY, "a", 3.0, 4.0)


# ---------------------------------------------------------------------------
# Store, retention, export, state
# ---------------------------------------------------------------------------


def test_section_validation():
    good = SC.new_section()
    good["calls"] = {"2026-10-07": 3}
    good["issues"] = {"100": {"pe": {"1800": [0.1, 0.05, 0.2]}}}
    good["pairs"] = {str(NOON_KEY): {"0": [1.5, 0.5, 0.2, 0.1, 0.3, 100]}}
    good["decisions"] = {DAY: {"captured": 5, "hours": {str(NOON_KEY): [0.5, None, 900.0]}, "entries": {"a": [1.0, 2.0]}}}
    good["blend"] = {"c": 1100.0, "weights": [0.1, 0.2, 0.0, 0.0, 0.0], "computed": "x"}
    assert SC.validate_section(json.loads(json.dumps(good))) == good
    assert SC.validate_section(None) == SC.new_section()
    cases = [
        ("calls", {"x": -1}), ("calls", {"x": True}), ("slots", {"d": 1}), ("retry_at", "soon"), ("failures", -1),
        ("issues", {"100": {"pe": {"1800": [0.1, 0.05]}}}), ("issues", {"100": {"pe": {"1800": [0.1, 0.05, 2.0]}}}),
        ("issues", {"x": {"pe": {}}}), ("pairs", {str(NOON_KEY): {"7": [1.5, 0.5, 0.2, 0.1, 0.3, 100]}}),
        ("pairs", {str(NOON_KEY): {"0": [1.5, 0.5, 0.2, 0.1, 0.3]}}),
        ("pairs", {str(NOON_KEY): {"0": [1.5, 2.0, 0.2, 0.1, 0.3, 100]}}),
        ("decisions", {"7 Oct": {"captured": 1, "hours": {}, "entries": {}}}),
        ("decisions", {DAY: {"captured": 1, "hours": {"1": [0.5, "x", 9.0]}, "entries": {}}}),
        ("decisions", {DAY: {"captured": 1, "hours": {}, "entries": {"a": [1.0]}}}),
        ("blend", {"c": 5000.0, "weights": [0.0] * 5}), ("blend", {"c": 1100.0, "weights": [1.5, 0, 0, 0, 0]}),
        ("blend", {"c": 1100.0, "weights": [0.1] * 4}), ("gate", {"passed": "yes"}), ("gate_history", {}),
    ]
    for field, value in cases:
        raw = json.loads(json.dumps(good))
        raw[field] = value
        with pytest.raises(SC.SolcastDataError):
            SC.validate_section(raw)
    with pytest.raises(SC.SolcastDataError):
        SC.validate_section([])


def test_store_round_trip_old_store_and_malformed_section():
    data = L.LearningData()
    assert data.d["solcast"] == SC.new_section()
    raw = data.to_json()
    del raw["solcast"]
    assert L.LearningData.from_json(json.loads(json.dumps(raw))).d["solcast"] == SC.new_section()
    raw = data.to_json()
    raw["solcast"] = {**SC.new_section(), "failures": "many"}
    with pytest.raises(L.LearningDataError):
        L.LearningData.from_json(raw)
    raw = data.to_json()
    raw["settings"]["solcast"] = "on"
    with pytest.raises(L.LearningDataError):
        L.LearningData.from_json(raw)


def test_view_copies_the_section():
    data = L.LearningData()
    data.d["solcast"]["pairs"][str(NOON_KEY)] = {"0": [1.5, 0.5, 0.2, 0.1, 0.3, 1]}
    view = data.view()
    data.d["solcast"]["pairs"][str(NOON_KEY)]["1"] = [4.5, 0.5, 0.2, 0.1, 0.3, 1]
    data.d["solcast"]["pairs"]["5"] = {}
    assert set(view.d["solcast"]["pairs"]) == {str(NOON_KEY)}
    assert set(view.d["solcast"]["pairs"][str(NOON_KEY)]) == {"0"}


def test_switch_is_logged():
    data = L.LearningData()
    now = at(10)
    assert data.set_solcast(True, now) and not data.set_solcast(True, now)
    assert data.solcast_switch and data.d["events"][-1][1] == "solcast_on"


def test_retention():
    section = SC.new_section()
    now = datetime(2026, 11, 16, 12, tzinfo=UTC)
    stamp = int(now.timestamp())
    section["issues"] = {str(stamp - 36 * 86400): {"pe": {}}, str(stamp - 34 * 86400): {"pe": {}}}
    section["pairs"] = {str(stamp - 401 * 86400): {}, str(stamp - 399 * 86400): {}}
    section["decisions"] = {"2024-10-01": {"captured": None, "hours": {}, "entries": {}},
                            "2025-11-01": {"captured": None, "hours": {}, "entries": {}}}
    section["slots"] = {"2026-11-01": {}, "2026-11-10": {}}
    section["calls"] = {"2026-11-01": 9, "2026-11-10": 9}
    assert SC.purge(section, now, TZ) == 4
    assert list(section["issues"]) == [str(stamp - 34 * 86400)]
    assert list(section["pairs"]) == [str(stamp - 399 * 86400)]
    assert list(section["decisions"]) == ["2025-11-01"]
    assert list(section["slots"]) == ["2026-11-10"] and list(section["calls"]) == ["2026-11-10"]


def test_export_rows():
    section = SC.new_section()
    section["issues"] = {"100": {"pe": {"1800": [0.1, 0.05, 0.2]}}}
    section["pairs"] = {str(NOON_KEY): {"0": [1.5, 0.5, 0.2, 0.1, 0.3, 100]}}
    SC.record_decision_hours(section, DAY, at(6, 30), {NOON_KEY: (0.5, None, 900.0)})
    SC.record_decision_energy(section, DAY, "a", 1.0, 2.0)
    tables = SC.export_rows(section)
    assert set(tables) == {"solcast_pairs.csv", "solcast_decision_hours.csv", "solcast_decision_energy.csv",
                           "solcast_issues.csv"}
    rows, header = tables["solcast_pairs.csv"]
    assert header[:3] == ["hour_utc", "bucket", "lead_h"] and rows[0][1:7] == [0, 1.5, 0.5, 0.2, 0.1, 0.3]
    assert tables["solcast_decision_energy.csv"][0] == [[DAY, "a", 1.0, 2.0]]
    assert tables["solcast_decision_hours.csv"][0][0][3:] == [0.5, None, 900.0]


def test_state():
    assert SC.state_of(False, None, True) == "not_configured"
    assert SC.state_of(True, None, False) == "collecting"
    assert SC.state_of(True, {"days": 13, "passed": False}, False) == "collecting"
    assert SC.state_of(True, {"days": 14, "passed": False}, True) == "shadow"
    assert SC.state_of(True, {"days": 20, "passed": True}, False) == "eligible"
    assert SC.state_of(True, {"days": 20, "passed": True}, True) == "applied"


def test_settings_export_never_contains_the_key():
    data = L.LearningData()
    settings = L.export_settings(data, [], TZ, LAT, LON)["solcast"]
    assert settings["switch"] is False and settings["parameters"]["reserve_calls"] == 1
    assert "key" not in json.dumps(settings).lower().replace("key_", "")
    assert math.isclose(settings["parameters"]["c_prior_w_m2_per_kw"], SC.C_PRIOR)


def test_gate_needs_the_recent_days():
    """Solcast good for three weeks, then off on the last 7 days: the
    overall improvement may stay, but the recent rule stops it."""
    section, truth, production = gate_case(28, ours_err=0.15, solcast_err=0.02)
    for d in range(22, 29):
        record = section["decisions"][f"2026-10-{d:02d}"]
        for key, row in record["hours"].items():
            t = truth[int(key)]
            row[1] = min(0.95 * t.e0h / SC.C_PRIOR, SC.MAX_KW_PER_KWP)  # Solcast says clear: far too bright
    gate = SC.evaluate_gate(section, truth, {"10K": ["a"]}, production, "2026-10-29", LAT, LON)
    assert gate["better_recent"] < SC.GATE_RECENT_BETTER and not gate["passed"]
    assert any(r.startswith("better on") for r in gate["reasons"])
