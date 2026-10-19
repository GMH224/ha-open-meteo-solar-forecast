"""Same-day correction engine (0.1.35, spec OMSF_v0_1_35_Same_Day_Spec.md).

Pure-module tests: every rule by hand, plus a statistical check that the
learned persistence matches the theory of the error model (spec §3.2).
"""

from __future__ import annotations

import ast
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from custom_components.open_meteo_solar_forecast import learning as L
from custom_components.open_meteo_solar_forecast import same_day as SD
from custom_components.open_meteo_solar_forecast.local_source import HourRecord

UTC = timezone.utc
TZ = ZoneInfo("Europe/Zurich")
LAT, LON = 47.3769, 8.5417  # neutral public test location
DAY = "2026-10-07"
SUNRISE = L.sunrise_utc(DAY, TZ, LAT, LON)
NOON = datetime(2026, 10, 7, 11, 0, tzinfo=UTC)
K_NOON = L.hour_key(NOON)


def test_module_is_pure():
    source = Path(SD.__file__).read_text()
    tree = ast.parse(source)
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith("homeassistant") for m in imported)
    for forbidden in ("datetime.now(", "utcnow(", "time.time(", "date.today(", "open("):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_clear_sky_index_by_hand():
    e0h, elev = L.hour_geometry(K_NOON, LAT, LON)
    kt = SD.clear_kt(K_NOON, LAT, LON)
    # Haurwitz at the hour's mean elevation is close to the hour mean
    cz = math.sin(math.radians(elev))
    assert kt == pytest.approx(1098 * cz * math.exp(-0.057 / cz) / e0h, rel=0.01)
    assert 0.65 < kt < 0.85
    assert SD.clear_kt(L.hour_key(datetime(2026, 10, 7, 0, tzinfo=UTC)), LAT, LON) == 0.0


@pytest.mark.parametrize(("kt", "kd"), [
    (0.1, 1 - 0.09 * 0.1),
    (0.5, 0.9511 - 0.1604 * 0.5 + 4.388 * 0.25 - 16.638 * 0.125 + 12.336 * 0.0625),
    (0.9, 0.165),
])
def test_erbs_by_hand(kt, kd):
    assert SD.erbs(kt) == pytest.approx(kd)


def test_forecast_k_and_its_limits():
    e0h, _ = L.hour_geometry(K_NOON, LAT, LON)
    kt_clear = SD.clear_kt(K_NOON, LAT, LON)
    record = HourRecord(start=NOON, ghi=0.3 * e0h, dni=100.0, dhi=0.25 * e0h)
    assert SD.forecast_k(record, LAT, LON) == pytest.approx(0.3 / kt_clear)
    bright = HourRecord(start=NOON, ghi=0.95 * e0h, dni=900.0, dhi=0.1 * e0h)
    assert SD.forecast_k(bright, LAT, LON) == SD.K_MAX
    assert SD.forecast_k(HourRecord(start=NOON), LAT, LON) is None
    night = datetime(2026, 10, 7, 1, tzinfo=UTC)
    assert SD.forecast_k(HourRecord(start=night, ghi=0.0, dni=0.0, dhi=0.0), LAT, LON) is None


def test_curve_records_follow_the_grid():
    records = SD.curve_records([K_NOON], 0.5, LAT, LON)
    record = records[NOON]
    e0h, _ = L.hour_geometry(K_NOON, LAT, LON)
    kt = 0.5 * SD.clear_kt(K_NOON, LAT, LON)
    assert record.ghi == pytest.approx(kt * e0h, abs=1e-3)
    assert record.dhi == pytest.approx(SD.erbs(kt) * kt * e0h, abs=1e-3)
    assert SD.forecast_k(record, LAT, LON) == pytest.approx(0.5, abs=1e-4)


# ---------------------------------------------------------------------------
# Response curves and inversion
# ---------------------------------------------------------------------------


def linear_curve(slope: float) -> np.ndarray:
    return slope * SD.K_GRID


def test_invert_by_hand():
    curve = linear_curve(2.0)  # 2 kWh at k = 1
    assert SD.invert(curve, 0.5) == (pytest.approx(0.25), False)
    assert SD.invert(curve, 0.0) == (0.0, False)
    assert SD.invert(curve, 2.2) == (SD.K_MAX, True)  # brighter than the curve's top
    # a flat top (inverter clipping) is inverted at its first point
    clipped = np.minimum(curve, 1.6)
    assert SD.invert(clipped, 1.6) == (SD.K_MAX, True)
    assert SD.invert(clipped, 1.59)[0] == pytest.approx(0.795)
    # rounding noise in the library cannot make the curve fall
    noisy = curve.copy()
    noisy[5] -= 0.3
    assert list(SD.monotone(noisy)) == list(np.maximum.accumulate(noisy))


def test_curves_add_and_total():
    curves = SD.Curves()
    curves.add("10K", 11, 100, 1.0)
    curves.add("10K", 11, 100, 0.5)
    curves.add("5K", 11, 100, 0.25)
    curves.add("5K", 11, 1000, 9.0)
    assert curves.total([100])[11] == pytest.approx(1.75)
    assert curves.total([100], ["5K"])[11] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


def test_band_energy_rules():
    series = [(0.0, 100.0), (500.0, 100.2), (950.0, 100.5)]
    # step function: the value at 900 is the reading of 500
    assert SD.band_energy(series, 0, 900, fresh_until=950, kwp=10) == pytest.approx(0.2)
    assert SD.band_energy(series, 0, 900, fresh_until=899, kwp=10) is None  # not known to be current
    assert SD.band_energy(series, -10, 900, fresh_until=950, kwp=10) is None  # nothing before the start
    assert SD.band_energy([(0.0, 5.0), (100.0, 4.0)], 0, 900, 950, 10) is None  # negative step
    # step above 1.2 x kWp x 0.25 h (+ one resolution step) is refused
    assert SD.band_energy([(0.0, 0.0), (100.0, 3.0)], 0, 900, 950, 10) == pytest.approx(3.0)
    assert SD.band_energy([(0.0, 0.0), (100.0, 3.02)], 0, 900, 950, 10) is None
    assert SD.counter_value(series, -1) is None


# ---------------------------------------------------------------------------
# The signal
# ---------------------------------------------------------------------------


def _curves_for(bands, slope_by_group):
    curves = SD.Curves()
    for band in bands:
        for group, slope in slope_by_group.items():
            for i, k in enumerate(SD.K_GRID):
                curves.add(group, i, band, slope * float(k))
    return curves


def test_window_starts_thirty_minutes_after_sunrise():
    now = SUNRISE + timedelta(minutes=40)
    bands = SD.window_bands(now, SUNRISE)
    assert all(b >= SUNRISE.timestamp() + 1800 for b in bands)
    later = datetime(2026, 10, 7, 9, 7, tzinfo=UTC)
    assert SD.window_bands(later, SUNRISE) == [int(datetime(2026, 10, 7, 8, m, tzinfo=UTC).timestamp())
                                               for m in (0, 15, 30, 45)]


def test_signal_by_hand():
    now = datetime(2026, 10, 7, 9, 7, tzinfo=UTC)
    bands = SD.window_bands(now, SUNRISE)
    curves = _curves_for(bands, {"10K": 0.3, "5K": 0.1})  # clear sky: 0.4 kWh per band
    hour8, hour9 = L.hour_key(datetime(2026, 10, 7, 8, tzinfo=UTC)), L.hour_key(datetime(2026, 10, 7, 9, tzinfo=UTC))
    kfc = {"10K": {hour8: 0.8, hour9: 0.9}, "5K": {hour8: 0.6, hour9: 0.9}}
    measured = {b: {"10K": 0.3 * 0.2, "5K": 0.1 * 0.2} for b in bands}  # k = 0.2 at the house
    signal = SD.measure_signal(curves, measured, kfc, ["10K", "5K"], now, SUNRISE)
    assert signal.valid and signal.k_meas == pytest.approx(0.2)
    # forecast k weighted by clear-sky energy: (0.3 * 0.8 + 0.1 * 0.6) / 0.4 = 0.75
    assert signal.k_fc == pytest.approx(0.75)
    assert signal.e == pytest.approx(-0.55)
    assert signal.clear_kwh == pytest.approx(1.6) and signal.measured_kwh == pytest.approx(0.32)


def test_signal_refused_with_too_little_light_or_a_missing_counter():
    now = datetime(2026, 10, 7, 9, 7, tzinfo=UTC)
    bands = SD.window_bands(now, SUNRISE)
    kfc = {"g": {b // 3600 * 3600: 0.5 for b in bands}}
    dim = _curves_for(bands, {"g": 0.074})  # 4 x 0.074 = 0.296 kWh < 0.3
    measured = {b: {"g": 0.01} for b in bands}
    signal = SD.measure_signal(dim, measured, kfc, ["g"], now, SUNRISE)
    assert not signal.valid and "too little light" in signal.reason
    enough = _curves_for(bands, {"g": 0.076})
    assert SD.measure_signal(enough, measured, kfc, ["g"], now, SUNRISE).valid
    measured[bands[0]] = {"g": None}
    assert "no counter reading" in SD.measure_signal(enough, measured, kfc, ["g"], now, SUNRISE).reason
    early = SUNRISE + timedelta(minutes=50)
    assert "too early" in SD.measure_signal(enough, measured, kfc, ["g"], early, SUNRISE).reason


def test_situation():
    now = datetime(2026, 10, 7, 8, 30, tzinfo=UTC)
    h = L.hour_key(now)
    assert SD.situation({h: 0.3, h + 3600: 0.4, h + 7200: 0.46}, now) == "improving"
    assert SD.situation({h: 0.3, h + 3600: 0.4, h + 4 * 3600: 0.9}, now) == "other"  # beyond 3 h
    assert SD.situation({h: 0.3, h + 3600: 0.44}, now) == "other"
    assert SD.situation({h + 3600: 0.9}, now) == "other"


# ---------------------------------------------------------------------------
# Applying the correction
# ---------------------------------------------------------------------------


def table(beta=0.5, sigma=0.1, sigma_u=0.2, usable=12):
    n = len(SD.LEAD_BUCKETS_H)
    return SD.Table(beta={s: [beta] * n for s in SD.SITUATIONS}, sigma={s: [sigma] * n for s in SD.SITUATIONS},
                    days={s: [5] * n for s in SD.SITUATIONS}, sigma_u=sigma_u, usable_days=usable)


def test_corrected_and_cautious_index_by_hand():
    t = table(beta=0.5, sigma=0.1)
    assert SD.corrected_k(0.8, -0.4, "other", 1.5, t) == pytest.approx(0.6)
    assert SD.corrected_k(0.8, None, "other", 1.5, t) == 0.8
    assert SD.corrected_k(0.9, 0.8, "other", 0.5, t) == SD.K_MAX
    assert SD.corrected_k(0.1, -0.8, "other", 0.5, t) == 0.0
    assert SD.corrected_k(0.8, -0.4, "other", 17.0, t) == 0.8  # beyond the last bucket
    assert SD.cautious_k(0.6, -0.4, "other", 1.5, t) == pytest.approx(0.6 - SD.CAUTIOUS_Z * 0.1)
    assert SD.cautious_k(0.6, None, "other", 1.5, t) == pytest.approx(0.6 - SD.CAUTIOUS_Z * 0.2)  # unconditional
    assert SD.cautious_k(0.05, -0.4, "other", 1.5, t) == 0.0
    no_spread = table(sigma=None, sigma_u=None)
    assert SD.cautious_k(0.6, -0.4, "other", 1.5, no_spread) == 0.6
    assert SD.CAUTIOUS_Z == pytest.approx(0.8416, abs=1e-4)
    # a table can only come from the store (range-checked) or the learning
    # (clamped); applying it clamps once more (defence in depth)
    assert SD.corrected_k(0.8, -0.4, "other", 0.5, table(beta=1.5)) == pytest.approx(0.4)
    assert SD.corrected_k(0.8, -0.4, "other", 0.5, table(beta=-0.5)) == pytest.approx(0.8)


@pytest.mark.parametrize(("lead", "bucket"), [(0.0, 0), (0.999, 0), (1.0, 1), (3.99, 2), (4.0, 3), (15.9, 4), (16.0, None)])
def test_lead_buckets(lead, bucket):
    assert SD.lead_bucket(lead) == bucket


def test_prior_is_three_hour_half_life():
    assert SD.PRIOR_BETA == pytest.approx(tuple(0.5 ** (m / 3) for m in (0.5, 1.5, 3, 6, 12)), abs=1e-6)


def test_shifts_move_light_to_the_target_index():
    e0h, _ = L.hour_geometry(K_NOON, LAT, LON)
    kt_clear = SD.clear_kt(K_NOON, LAT, LON)
    record = HourRecord(start=NOON, ghi=0.6 * e0h, dni=600.0, dhi=0.18 * e0h)
    shifts = SD.shifts_for({NOON: record}, {K_NOON: 0.3}, LAT, LON)
    shift_kt, shift_kd = shifts[K_NOON]
    assert shift_kt == pytest.approx(0.3 * kt_clear - 0.6, abs=1e-6)
    new_kt = 0.6 + shift_kt
    assert shift_kd == pytest.approx(SD.erbs(new_kt) - SD.erbs(0.6), abs=1e-6)  # greyer -> more diffuse
    assert shift_kd > 0
    # bounded
    bright = HourRecord(start=NOON, ghi=0.8 * e0h, dni=800.0, dhi=0.12 * e0h)
    far = SD.shifts_for({NOON: bright}, {K_NOON: 0.0}, LAT, LON)
    assert far[K_NOON][0] == pytest.approx(-0.6)  # -0.8 bounded
    # no change -> no entry (record stays the same object downstream)
    same = SD.shifts_for({NOON: record}, {K_NOON: 0.6 / kt_clear}, LAT, LON)
    assert same == {}


def _day_hours(day="2026-10-07", factor=0.6):
    hours = {}
    for key in L.local_day_hours(day, TZ):
        e0h, _ = L.hour_geometry(key, LAT, LON)
        start = L.key_to_dt(key)
        hours[start] = HourRecord(start=start, ghi=factor * e0h, dni=300.0, dhi=0.3 * factor * e0h)
    nxt = datetime(2026, 10, 8, 11, tzinfo=UTC)
    e0h, _ = L.hour_geometry(L.hour_key(nxt), LAT, LON)
    hours[nxt] = HourRecord(start=nxt, ghi=factor * e0h, dni=300.0, dhi=0.3 * factor * e0h)
    return hours


def test_plan_touches_only_todays_remaining_hours():
    now = datetime(2026, 10, 7, 9, 10, tzinfo=UTC)
    hours = _day_hours()
    signal = SD.Signal(True, "ok", e=-0.4, situation="other")
    served, cautious = SD.plan(hours, signal, table(), now, TZ, LAT, LON)
    assert served and cautious
    assert all(key + 3600 > now.timestamp() for key in served)  # running hour included, past hours not
    assert L.hour_key(datetime(2026, 10, 7, 9, tzinfo=UTC)) in served
    assert L.hour_key(datetime(2026, 10, 8, 11, tzinfo=UTC)) not in served  # tomorrow untouched
    assert all(shift[0] < 0 for shift in served.values())
    # cautious lower than served everywhere
    for key, shift in cautious.items():
        assert shift[0] <= served.get(key, (0.0, 0.0))[0] + 1e-9
    # without a valid signal: nothing served, cautious from the unconditional spread
    served2, cautious2 = SD.plan(hours, SD.Signal(False, "x"), table(), now, TZ, LAT, LON)
    assert served2 == {} and cautious2
    # not enough days: no cautious bands at all
    assert SD.plan(hours, signal, table(usable=9), now, TZ, LAT, LON)[1] is None
    tomorrow = L.hour_key(datetime(2026, 10, 8, 11, tzinfo=UTC))
    assert tomorrow not in cautious and tomorrow not in cautious2
    # late in the day, tomorrow's morning is within the lead buckets: still untouched
    evening = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)
    early = datetime(2026, 10, 8, 7, tzinfo=UTC)
    e0h, _ = L.hour_geometry(L.hour_key(early), LAT, LON)
    late_hours = {early: HourRecord(start=early, ghi=0.6 * e0h, dni=300.0, dhi=0.18 * e0h)}
    assert SD.plan(late_hours, signal, table(), evening, TZ, LAT, LON) == ({}, {})


def test_plan_decays_with_lead_time():
    now = datetime(2026, 10, 7, 8, 5, tzinfo=UTC)
    hours = _day_hours(factor=0.65)
    t = SD.Table.prior()
    served, _ = SD.plan(hours, SD.Signal(True, "ok", e=-0.5), t, now, TZ, LAT, LON)
    shifts = [served[k][0] for k in sorted(served)]
    near = abs(served[L.hour_key(datetime(2026, 10, 7, 8, tzinfo=UTC))][0])
    far = abs(served[L.hour_key(datetime(2026, 10, 7, 14, tzinfo=UTC))][0])
    assert near > far > 0 and len(shifts) >= 6


# ---------------------------------------------------------------------------
# Decision times
# ---------------------------------------------------------------------------


def test_decision_times():
    times = SD.decision_times(DAY, TZ, SUNRISE)
    assert times[0] == SUNRISE + timedelta(hours=1)
    local = [t.astimezone(TZ) for t in times[1:]]
    assert all(t.minute in (0, 30) for t in local)
    assert local[0] - times[0].astimezone(TZ) <= timedelta(minutes=30)
    assert local[-1].strftime("%H:%M") == "14:00"
    assert SD.decision_times(DAY, TZ, None) == []


def test_eval_decisions():
    times = [int(t.timestamp()) for t in SD.decision_times(DAY, TZ, SUNRISE)]
    picked = SD.eval_decisions(DAY, TZ, times)
    assert picked[0] == times[0]
    assert [L.key_to_dt(p).astimezone(TZ).strftime("%H:%M") for p in picked[1:]] == ["10:00", "12:00"]
    assert SD.eval_decisions(DAY, TZ, []) == []


# ---------------------------------------------------------------------------
# Learning β and σ
# ---------------------------------------------------------------------------


def _record(day, d_ts, x, bands, kfc=0.6, sit="other", rem=None):
    return {"bands": {str(b): {"m": {"g": 0.1}, "c": c, "k": k} for b, (c, k) in bands.items()},
            "decisions": {str(d_ts): {"e": x, "sit": sit, "kfc": {str(b // 3600 * 3600): kfc for b in bands},
                                      "rem": rem or {}}}}


def test_learn_table_by_hand():
    d_ts = int(datetime(2026, 10, 7, 8, 0, tzinfo=UTC).timestamp())
    b1 = d_ts + 3600  # lead 1.125 h (band mid) -> bucket 1-2 h
    rec = _record("2026-10-07", d_ts, -0.4, {b1: (0.5, 0.6 - 0.2)})
    t = SD.learn_table({"2026-10-07": rec}, before="2026-10-08")
    # β_data = Σw·x·y / Σw·x² = (-0.4 · -0.2) / 0.16 = 0.5; one day, prior weight 5
    assert t.beta["other"][1] == pytest.approx((1 * 0.5 + 5 * SD.PRIOR_BETA[1]) / 6)
    assert t.days["other"][1] == 1 and t.days["other"][0] == 0
    assert t.beta["other"][0] == pytest.approx(SD.PRIOR_BETA[0])  # no data: prior
    assert t.beta["improving"] == pytest.approx(list(SD.PRIOR_BETA))
    b = t.beta["other"][1]
    assert t.sigma["other"][1] == pytest.approx(abs(-0.2 - b * -0.4))
    assert t.sigma_u == pytest.approx(0.2)
    assert t.usable_days == 1
    # no look-ahead: the day itself is not used for its own table
    assert SD.learn_table({"2026-10-07": rec}, before="2026-10-07").usable_days == 0


def test_learn_table_clamps_and_separates_situations():
    d_ts = int(datetime(2026, 10, 7, 8, 0, tzinfo=UTC).timestamp())
    b1 = d_ts + 1800  # lead 0.625 h -> bucket 0
    days = {}
    for i in range(30):
        day = (datetime(2026, 9, 1) + timedelta(days=i)).date().isoformat()
        days[day] = _record(day, d_ts, 0.3, {b1: (0.5, 0.6 - 0.3)}, sit="improving")  # anti-persistent
    t = SD.learn_table(days, before="2026-12-01")
    assert t.beta["improving"][0] == 0.0  # clamped at 0
    assert t.beta["other"][0] == pytest.approx(SD.PRIOR_BETA[0])


def test_learned_persistence_matches_the_error_model():
    """Spec §3.2: with AR(1) errors measured with noise, the least-squares β
    equals ρ(Δ)·σ²/(σ²+σn²) (here within sampling error)."""
    rng = random.Random(7)
    tau, sd, noise = 3.0, 0.25, 0.08
    rho15 = 0.5 ** (0.25 / tau)
    days = {}
    start = datetime(2026, 7, 1, 7, 0, tzinfo=UTC)
    for i in range(60):
        day0 = start + timedelta(days=i)
        e = rng.gauss(0, sd)
        path = []
        for _ in range(32):
            path.append(e)
            e = rho15 * e + rng.gauss(0, sd * math.sqrt(1 - rho15**2))
        d_ts = int(day0.timestamp())
        bands = {d_ts + j * 900: (0.5, 0.6 + path[j]) for j in range(1, 32)}
        days[day0.date().isoformat()] = _record(day0.date().isoformat(), d_ts, path[0] + rng.gauss(0, noise), bands)
    t = SD.learn_table(days, before="2026-12-01")
    shrink = sd**2 / (sd**2 + noise**2)
    for bucket, (low, high) in enumerate(SD.LEAD_BUCKETS_H[:4]):
        leads = [(j * 900 + 450) / 3600 for j in range(1, 32) if low <= (j * 900 + 450) / 3600 < high]
        theory = np.mean([0.5 ** (lead / tau) for lead in leads]) * shrink
        expected = (60 * theory + 5 * SD.PRIOR_BETA[bucket]) / 65
        assert t.beta["other"][bucket] == pytest.approx(expected, abs=0.12), bucket


# ---------------------------------------------------------------------------
# Usable days, remaining energy, the check
# ---------------------------------------------------------------------------


def test_usable_day_and_measured_remaining():
    rec = {"bands": {"100": {"m": {"g": 1.0}, "c": 1.0, "k": 0.5}, "1000": {"m": {"g": 2.0}, "c": 1.0, "k": 0.4},
                     "2000": {"m": {"g": None}, "c": 0.01, "k": None}},
           "decisions": {"100": {"e": None, "sit": "other", "kfc": {}, "rem": {}}}}
    assert SD.usable_day(rec)
    assert SD.measured_remaining(rec, "g", 1000) == 2.0
    rec["bands"]["3000"] = {"m": {"g": None}, "c": 0.5, "k": None}
    assert SD.measured_remaining(rec, "g", 1000) is None  # a lit band is missing
    assert not SD.usable_day(rec)  # 2 of 3 lit bands measured: below 80 %
    for i in range(5, 10):
        rec["bands"][str(i * 1000)] = {"m": {"g": 1.0}, "c": 0.5, "k": 0.5}
    assert SD.usable_day(rec)  # 7 of 8 = 87.5 %
    assert not SD.usable_day({"bands": rec["bands"], "decisions": {}})


def _gate_days(errors):
    """errors: list of (base, sdc) remaining-energy errors per day (kWh)."""
    days = {}
    for i, (base, sdc) in enumerate(errors):
        day0 = datetime(2026, 10, 1, 7, 0, tzinfo=UTC) + timedelta(days=i)
        d_ts = int(day0.timestamp())
        actual = 10.0
        days[day0.astimezone(TZ).date().isoformat()] = {
            "bands": {str(d_ts + 900): {"m": {"g": actual}, "c": 1.0, "k": 0.5}},
            "decisions": {str(d_ts): {"e": -0.1, "sit": "other", "kfc": {},
                                      "rem": {"g": [actual + base, actual + sdc, actual - 1.0]}}},
        }
    return days


def test_gate_by_hand():
    now = datetime(2026, 10, 20, 1, 30, tzinfo=UTC)
    days = _gate_days([(2.0, 1.0)] * 7)
    gate = SD.evaluate_gate(days, ["g"], "2026-10-20", TZ, now)
    assert gate["days"] == 7 and gate["improvement"] == pytest.approx(0.5) and gate["passed"]
    assert gate["mae_without_kwh"] == 2.0 and gate["mae_with_kwh"] == 1.0
    assert gate["cautious_coverage"] == 0.0 and gate["cautious_checked"] == 7  # actual never below cautious
    # six days only
    assert not SD.evaluate_gate(_gate_days([(2.0, 1.0)] * 6), ["g"], "2026-10-20", TZ, now)["passed"]
    # better on average, but only 4 of the last 7 days
    mixed = _gate_days([(2.0, 2.5)] * 3 + [(2.0, 0.5)] * 4)
    result = SD.evaluate_gate(mixed, ["g"], "2026-10-20", TZ, now)
    assert result["improvement"] > 0.05 and result["better_recent"] == 4 and not result["passed"]
    # exactly 5 % passes, below does not
    assert SD.evaluate_gate(_gate_days([(2.0, 1.9)] * 7), ["g"], "2026-10-20", TZ, now)["passed"]
    assert not SD.evaluate_gate(_gate_days([(2.0, 1.902)] * 7), ["g"], "2026-10-20", TZ, now)["passed"]
    # today and days older than 28 days are not judged
    assert SD.evaluate_gate(days, ["g"], "2026-10-02", TZ, now)["days"] == 1
    assert SD.evaluate_gate(days, ["g"], "2026-11-30", TZ, now)["days"] == 0


# ---------------------------------------------------------------------------
# Store section
# ---------------------------------------------------------------------------


def test_section_validation():
    good = SD.new_section()
    good["days"]["2026-10-07"] = _record("2026-10-07", 100, -0.2, {1000: (0.5, 0.4)})
    good["table"] = {"computed": "x", **table().to_json()}
    assert SD.validate_section(json.loads(json.dumps(good)))
    for breaker in (
        lambda s: s["days"].update({"2026-13-01": {}}),
        lambda s: s["days"]["2026-10-07"]["bands"]["1000"].update(k=float("nan")),
        lambda s: s["days"]["2026-10-07"]["bands"]["1000"]["m"].update(g="x"),
        lambda s: s["days"]["2026-10-07"]["decisions"]["100"].update(sit="sunny"),
        lambda s: s["days"]["2026-10-07"]["decisions"]["100"].update(e=True),
        lambda s: s["days"]["2026-10-07"]["decisions"]["100"]["kfc"].update({"7200": None}),
        lambda s: s["days"]["2026-10-07"]["decisions"]["100"].update(rem={"g": "x"}),
        lambda s: s["table"]["beta"]["other"].__setitem__(0, 1.5),
        lambda s: s["table"]["sigma"]["other"].__setitem__(0, -0.1),
        lambda s: s["table"]["beta"]["other"].pop(),
        lambda s: s.update(days=[]),
    ):
        raw = json.loads(json.dumps(good))
        breaker(raw)
        with pytest.raises((SD.SameDayDataError, ValueError)):
            SD.validate_section(raw)


def test_malformed_section_rejects_the_store():
    data = L.LearningData()
    raw = json.loads(json.dumps(data.to_json()))
    raw["same_day"]["days"]["2026-10-07"] = {"bands": {"1": {"k": "x", "c": 1}}}
    with pytest.raises(L.LearningDataError):
        L.LearningData.from_json(raw)
    raw = json.loads(json.dumps(data.to_json()))
    raw["settings"]["same_day"] = "yes"
    with pytest.raises(L.LearningDataError):
        L.LearningData.from_json(raw)


def test_store_of_schema_2_is_migrated():
    data = L.LearningData()
    data.d["issue6"]["2026-10-07"] = {"captured": 1, "hours": {K_NOON: [0.5, 0.5, 0.3, 0.3, 500.0]}}
    data.d["entry_days"]["2026-10-07"] = {"e1": {"captured": 1, "raw": 1.0, "corr": 1.0}}
    data.d["station_hours"][K_NOON] = [400.0, 120.0, 60.0, 300.0, 40.0, 12.0, 6.0]
    data.d["settings"]["apply"] = True
    raw = json.loads(json.dumps(data.to_json()))
    raw["schema"] = 2
    raw.pop("same_day")
    raw["settings"].pop("same_day")
    migrated = L.LearningData.from_json(raw)
    assert migrated.migrated_from == 2 and migrated.d["schema"] == 3
    assert migrated.d["issue6"] == {} and migrated.d["entry_days"] == {}
    assert migrated.d["station_hours"][K_NOON][0] == 400.0 and migrated.d["settings"]["apply"] is True
    assert migrated.d["same_day"]["days"] == {} and migrated.same_day_switch is False
    assert L.LearningData.from_json(json.loads(json.dumps(L.LearningData().to_json()))).migrated_from is None


def test_same_day_switch_is_logged():
    data = L.LearningData()
    now = datetime(2026, 10, 7, 9, tzinfo=UTC)
    assert data.set_same_day(True, now) and not data.set_same_day(True, now)
    assert data.same_day_switch and data.d["events"][-1][1] == "same_day_on"


def test_view_copies_the_same_day_days():
    data = L.LearningData()
    data.d["same_day"]["days"]["2026-10-07"] = {"bands": {}, "decisions": {}}
    view = data.view()
    data.d["same_day"]["days"]["2026-10-07"]["bands"]["1"] = {"m": {}, "c": 0.0, "k": None}
    data.d["same_day"]["days"]["2026-10-08"] = {"bands": {}, "decisions": {}}
    assert view.d["same_day"]["days"]["2026-10-07"]["bands"] == {} and "2026-10-08" not in view.d["same_day"]["days"]


def test_purge_keeps_120_days():
    section = SD.new_section()
    section["days"] = {"2026-06-01": {}, "2026-06-20": {}, "2026-10-01": {}}
    assert SD.purge(section, "2026-10-08") == 1 and "2026-06-01" not in section["days"]
    assert "2026-06-20" in section["days"]


def test_export_rows():
    section = SD.new_section()
    d_ts = int(datetime(2026, 10, 7, 8, tzinfo=UTC).timestamp())
    section["days"]["2026-10-07"] = _record("2026-10-07", d_ts, -0.2, {d_ts + 900: (0.5, 0.4)},
                                            rem={"g": [5.0, 4.0, 3.0]})
    d_rows, d_header, b_rows, b_header = SD.export_rows(section, ["g"])
    assert d_header[:4] == ["date", "decision_utc", "e_hat", "situation"]
    assert d_rows[0][4:8] == [5.0, 4.0, 3.0, 0.1]
    assert b_rows[0][2:] == [0.4, 0.5, 0.1] and b_header[-1] == "g_measured_kwh"
