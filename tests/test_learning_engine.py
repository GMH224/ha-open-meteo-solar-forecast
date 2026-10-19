"""Unit tests of the pure learning engine (0.1.34).

Every algorithmic rule of OMSF_v0_1_34_Learning_Spec.md §3–§9 is checked
against a value that can be calculated by hand. No Home Assistant.
"""

from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from custom_components.open_meteo_solar_forecast import learning as L
from custom_components.open_meteo_solar_forecast.local_source import (
    HourRecord,
    _hour_clearness,
)

UTC = timezone.utc
TZ = ZoneInfo("Europe/Zurich")
LAT, LON = 47.3769, 8.5417  # neutral public test location
NOON = datetime(2026, 10, 7, 11, 0, tzinfo=UTC)  # 13:00 local, sun ~ 33°
K_NOON = L.hour_key(NOON)


def pred(key=K_NOON, kt=0.6, kd=0.3, low=50.0, mid=10.0, high=0.0, temp=12.0, dew=8.0, total=55.0):
    e0h, elev = L.hour_geometry(key, LAT, LON)
    return L.Predictors(key, kt, kd, low, mid, high, temp, dew, elev, e0h, total)


def evidence(rows):
    """rows: (hour, bucket, vector(6), err_kt, err_kd)."""
    return L.Evidence(
        hours=np.array([r[0] for r in rows], dtype=np.int64),
        buckets=np.array([r[1] for r in rows], dtype=np.int64),
        x=np.array([r[2] for r in rows], dtype=float).reshape(-1, len(L.PREDICTOR_NAMES)),
        err_kt=np.array([r[3] for r in rows], dtype=float),
        err_kd=np.array([r[4] for r in rows], dtype=float),
    )


# ---------------------------------------------------------------------------
# Time handling
# ---------------------------------------------------------------------------


def test_local_day_hours_handles_dst():
    assert len(L.local_day_hours("2026-10-07", TZ)) == 24
    assert len(L.local_day_hours("2026-10-25", TZ)) == 25  # CEST -> CET
    assert len(L.local_day_hours("2026-03-29", TZ)) == 23  # CET -> CEST
    hours = L.local_day_hours("2026-10-07", TZ)
    assert L.key_to_dt(hours[0]) == datetime(2026, 10, 6, 22, tzinfo=UTC)


@pytest.mark.parametrize(
    ("lead", "bucket"),
    [(-0.5, 0), (0, 0), (5.99, 0), (6, 1), (17.99, 1), (18, 2), (29.99, 2), (30, 3), (53.99, 3), (54, None), (100, None)],
)
def test_lead_buckets(lead, bucket):
    assert L.lead_bucket(lead) == bucket


def test_clearness_index_is_the_synthesis_definition():
    record = HourRecord(start=NOON, ghi=400.0, dni=500.0, dhi=150.0)
    e0h, _ = L.hour_geometry(K_NOON, LAT, LON)
    assert L.clearness(400.0, 150.0, e0h) == pytest.approx(_hour_clearness(record, LAT, LON, None), abs=1e-12)
    assert L.clearness(5000.0, 10.0, e0h)[0] == 1.0  # clamped like the synthesis
    assert L.clearness(0.0, 0.0, e0h) == (0.0, 1.0)


def test_stage2_bins():
    assert [L.stage2_bin(v) for v in (0.0, 0.299, 0.3, 0.599, 0.6, 1.0)] == [0, 0, 1, 1, 2, 2]


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def test_weather_layers_validated_and_missing_stays_missing():
    now = NOON
    raw = [
        {"datetime": "2026-10-07T11:00:00+00:00", "cloud_coverage_low": 40, "cloud_coverage_mid": 10,
         "cloud_coverage_high": 0, "cloud_coverage": 45, "temperature": 12.0, "dew_point": 8.0},
        {"datetime": "2026-10-07T12:00:00+00:00", "cloud_coverage_low": 120, "temperature": 13.0,
         "dew_point": 15.0, "cloud_coverage": -3},  # low/total out of range; mid/high missing; dew > temp
        {"datetime": "2026-10-07T13:00:00+00:00"},
        "junk",
        {"datetime": "2026-11-30T00:00:00+00:00", "cloud_coverage_low": 1},  # outside window
        {"datetime": "no date"},
    ]
    instants, rejected = L.parse_weather_layers(raw, "°C", now)
    assert rejected == 3
    assert instants[int(datetime(2026, 10, 7, 11, tzinfo=UTC).timestamp())] == (40.0, 10.0, 0.0, 12.0, 8.0, 45.0)
    assert instants[int(datetime(2026, 10, 7, 12, tzinfo=UTC).timestamp())] == (None, None, None, 13.0, None, None)
    assert instants[int(datetime(2026, 10, 7, 13, tzinfo=UTC).timestamp())] == (None,) * 6


def test_weather_layers_fahrenheit_and_size_bound():
    raw = [{"datetime": NOON.isoformat(), "cloud_coverage_low": 0, "cloud_coverage_mid": 0,
            "cloud_coverage_high": 0, "temperature": 50.0, "dew_point": 41.0}]
    instants, _ = L.parse_weather_layers(raw, "°F", NOON)
    assert instants[K_NOON][3] == pytest.approx(10.0)
    assert instants[K_NOON][4] == pytest.approx(5.0)
    assert L.parse_weather_layers([{}] * 501, "°C", NOON) == ({}, 501)
    assert L.parse_weather_layers("x", "°C", NOON) == ({}, 0)


def test_hour_layers_mean_of_both_ends_or_one():
    a = K_NOON
    instants = {a: (10.0, 20.0, None, 10.0, 6.0, 40.0), a + 3600: (30.0, None, None, 12.0, 8.0, None)}
    assert L.hour_layers(instants, a) == (20.0, 20.0, None, 11.0, 7.0, 40.0)
    assert L.hour_layers(instants, a + 3600) == (30.0, None, None, 12.0, 8.0, None)


def _values(ghi=300.0, dhi=100.0, sun=5.0, lw=320.0, refl=50.0, temp=10.0):
    return {"gre000z0": ghi, "ods000z0": dhi, "sre000z0": sun, "oli000z0": lw, "osr000z0": refl, "tre200s0": temp}


def test_station_values_parsed_and_ranges_enforced():
    end = datetime(2026, 10, 7, 11, 10, 30, tzinfo=UTC)  # 30 s jitter
    sample = L.parse_station_values(_values(), end)
    assert sample.end == int(datetime(2026, 10, 7, 11, 10, tzinfo=UTC).timestamp())
    assert sample.as_list() == [300.0, 100.0, 5.0, 320.0, 50.0, 10.0]
    bad = L.parse_station_values(_values(dhi=400.0, sun=11, lw=10, temp=99), end)
    assert bad.as_list() == [300.0, None, None, None, 50.0, None]
    assert L.parse_station_values(_values(ghi=None), end) is None
    assert L.parse_station_values(_values(ghi=-1), end) is None
    assert L.parse_station_values(_values(), end + timedelta(minutes=4)) is None  # off the grid
    assert L.parse_station_values("x", end) is None
    assert L.parse_station_values(_values(), None) is None


def test_station_hour_needs_five_of_six_values():
    samples = {K_NOON + 600 * i: [100.0 * i, 50.0, 10.0, 300.0, 20.0, 10.0] for i in range(1, 7)}
    hour = L.station_hour(samples, K_NOON)
    assert hour[0] == pytest.approx(350.0)  # mean of 100..600
    assert hour[2] == pytest.approx(60.0)  # sunshine: minutes in the hour
    assert hour[-1] == 6.0
    del samples[K_NOON + 600]
    assert L.station_hour(samples, K_NOON)[0] == pytest.approx(400.0)
    del samples[K_NOON + 1200]
    assert L.station_hour(samples, K_NOON) is None
    # a field with fewer than 5 values becomes None, the hour stays
    samples = {K_NOON + 600 * i: [100.0, None if i < 3 else 50.0, 1.0, 300.0, 20.0, 10.0] for i in range(1, 7)}
    assert L.station_hour(samples, K_NOON)[1] is None


def test_finalize_assigns_the_full_hour_sample_to_the_previous_hour():
    data = L.LearningData()
    for i in range(1, 7):
        data.add_station_sample(L.StationSample(K_NOON + 600 * i, 100.0, 50.0, 10.0, 300.0, 20.0, 10.0))
    # 12:15: the hour ended 15 min ago -> not yet final
    assert data.finalize_station_hours(NOON + timedelta(minutes=75)) == 0
    assert data.finalize_station_hours(NOON + timedelta(minutes=80)) == 1
    assert list(data.d["station_hours"]) == [K_NOON]
    # a less complete re-aggregation never replaces a complete hour
    del data.d["station_samples"][K_NOON + 600]
    data.finalize_station_hours(NOON + timedelta(minutes=90))
    assert data.d["station_hours"][K_NOON][-1] == 6.0
    # samples older than 72 h are pruned
    data.finalize_station_hours(NOON + timedelta(hours=80))
    assert data.d["station_samples"] == {}


# ---------------------------------------------------------------------------
# Predictors and archive
# ---------------------------------------------------------------------------


def _record(key, kt=0.6, kd=0.3):
    e0h, _ = L.hour_geometry(key, LAT, LON)
    ghi = kt * e0h
    return HourRecord(start=L.key_to_dt(key), ghi=ghi, dni=300.0, dhi=kd * ghi)


def test_predictors_require_sun_and_all_weather_fields():
    night = L.hour_key(datetime(2026, 10, 7, 1, tzinfo=UTC))
    records = {L.key_to_dt(k): _record(k) for k in (K_NOON, K_NOON + 3600, night)}
    records[L.key_to_dt(night)] = HourRecord(start=L.key_to_dt(night), ghi=0.0, dni=0.0, dhi=0.0)
    full = (40.0, 10.0, 5.0, 12.0, 8.0, 45.0)
    instants = {K_NOON: full, K_NOON + 3600: full, K_NOON + 7200: (None, 10.0, 5.0, 12.0, 8.0, 45.0),
                night: full, night + 3600: full}
    preds = L.build_predictors(records, instants, LAT, LON)
    assert set(preds) == {K_NOON, K_NOON + 3600}  # night: sun < 5°
    p = preds[K_NOON]
    assert p.kt == pytest.approx(0.6) and p.kd == pytest.approx(0.3)
    assert list(p.vector()) == pytest.approx([0.6, p.elevation, 40.0, 10.0, 4.0, 45.0])
    del instants[K_NOON + 3600]
    del instants[K_NOON + 7200]
    assert K_NOON + 3600 not in L.build_predictors(records, instants, LAT, LON)


def test_archive_keeps_latest_issue_per_bucket_and_skips_elapsed_or_far_hours():
    data = L.LearningData()
    p = pred()
    issue1 = NOON - timedelta(hours=20)  # lead 20 h -> bucket 2
    data.archive_forecast({K_NOON: p}, {}, issue1)
    issue2 = NOON - timedelta(hours=19)
    data.archive_forecast({K_NOON: replace_kt(p, 0.5)}, {}, issue2)
    assert set(data.d["archive"][K_NOON]) == {2}
    assert data.d["archive"][K_NOON][2][L.R["kt"]] == 0.5
    data.archive_forecast({K_NOON: p}, {}, NOON + timedelta(minutes=30))  # running hour: bucket 0
    assert set(data.d["archive"][K_NOON]) == {0, 2}
    data.archive_forecast({K_NOON: p}, {}, NOON + timedelta(hours=1))  # elapsed
    data.archive_forecast({K_NOON + 54 * 3600: pred(K_NOON + 54 * 3600)}, {}, NOON)  # too far
    assert K_NOON + 54 * 3600 not in data.d["archive"]
    assert data.latest_rows([K_NOON])[K_NOON][L.R["issued"]] == int((NOON + timedelta(minutes=30)).timestamp())


def replace_kt(p, kt):
    return L.Predictors(p.key, kt, p.kd, p.low, p.mid, p.high, p.temp, p.dew, p.elevation, p.e0h, p.total)


# ---------------------------------------------------------------------------
# Analog ensemble
# ---------------------------------------------------------------------------


def test_empty_evidence_is_neutral():
    assert L.analog_correction(evidence([]), pred(), 1) == L.NEUTRAL


def test_shrinkage_formula_by_hand():
    p = pred()
    v = list(p.vector())
    rows = [(K_NOON, 1, v, -0.2, 0.1)] * 10  # distance 0, age 0 -> weight 5 each
    c = L.analog_correction(evidence(rows), p, 1)
    assert c.analogs == 10
    assert c.n_eff == pytest.approx(10.0)  # (10*5)^2 / (10*25)
    assert c.shrink == pytest.approx(0.5)
    assert c.mean_error_kt == pytest.approx(-0.2)
    assert c.shift_kt == pytest.approx(-0.1)
    assert c.shift_kd == pytest.approx(0.05)
    assert not c.pooled


def test_fewer_than_eight_analogs_is_neutral_and_reports_the_count():
    p = pred()
    rows = [(K_NOON, 1, list(p.vector()), -0.2, 0.0)] * 7
    c = L.analog_correction(evidence(rows), p, 1)
    assert c.shift_kt == 0.0 and c.analogs == 7 and c.pooled


def test_neighbouring_buckets_are_pooled_when_the_own_bucket_is_thin():
    p = pred()
    v = list(p.vector())
    rows = [(K_NOON, 2, v, -0.2, 0.0)] * 8 + [(K_NOON, 3, v, +0.5, 0.0)] * 50
    c = L.analog_correction(evidence(rows), p, 1)
    assert c.pooled and c.analogs == 8 and c.mean_error_kt == pytest.approx(-0.2)


def test_distance_limit_and_k_limit():
    p = pred()
    v = np.array(p.vector())
    far = list(v + np.array([0.151, 0, 0, 0, 0, 0]))  # 1.51 scale units
    near = list(v + np.array([0.149, 0, 0, 0, 0, 0]))
    c = L.analog_correction(evidence([(K_NOON, 1, far, -0.2, 0.0)] * 30), p, 1)
    assert c.analogs == 0 and c.shift_kt == 0.0
    c = L.analog_correction(evidence([(K_NOON, 1, near, -0.2, 0.0)] * 30), p, 1)
    assert c.analogs == L.K_NEIGHBOURS


def test_ties_are_broken_deterministically_by_hour():
    p = pred()
    v = list(p.vector())
    rows = [(K_NOON - 3600 * i, 1, v, -0.01 * i, 0.0) for i in range(40)]
    c1 = L.analog_correction(evidence(rows), p, 1)
    c2 = L.analog_correction(evidence(list(reversed(rows))), p, 1)
    assert c1 == c2
    assert [t[0] for t in c1.top] == sorted(t[0] for t in c1.top)  # oldest hour first among equals


def test_shift_bounds():
    p = pred()
    rows = [(K_NOON, 1, list(p.vector()), -0.9, 0.9)] * 25
    c = L.analog_correction(evidence(rows), p, 1)
    assert c.shift_kt == -L.MAX_SHIFT_KT and c.shift_kd == L.MAX_SHIFT_KD


def test_recency_and_season_weights():
    w = L.recency_weight(np.array([0.0, 90.0, 365.0, 365.0, 200.0]), 280, np.array([280, 280, 280, 330, 100]))
    # half-life 90 d; same season a year earlier (±45 d) >= 0.7; never below 0.1
    assert list(w) == pytest.approx([1.0, 0.5, 0.7, 0.1, 0.5 ** (200 / 90)])


# ---------------------------------------------------------------------------
# No look-ahead, freeze, reset
# ---------------------------------------------------------------------------


def _decide(data, day, ts, status=L.DAY_OK):
    data.d["days"][day] = {"decided": ts, "status": status, "details": {}}


def _station(data, key, kt, kd=0.3):
    e0h, _ = L.hour_geometry(key, LAT, LON)
    data.d["station_hours"][key] = [kt * e0h, kd * kt * e0h, 30.0, 300.0, 20.0, 10.0, 6.0]


def test_evidence_only_from_days_decided_before_the_cutoff():
    data = L.LearningData()
    p = pred()
    data.archive_forecast({K_NOON: p}, {}, NOON - timedelta(hours=10))
    _station(data, K_NOON, 0.4)
    decided = int((NOON + timedelta(hours=14)).timestamp())
    _decide(data, "2026-10-07", decided)
    assert L.build_evidence(data, decided, TZ).size == 0  # decided == cutoff: not yet
    ev = L.build_evidence(data, decided + 1, TZ)
    assert ev.size == 1
    assert ev.err_kt[0] == pytest.approx(0.4 - 0.6)
    assert ev.err_kd[0] == pytest.approx(0.0)
    _decide(data, "2026-10-07", decided, "snow")
    assert L.build_evidence(data, decided + 1, TZ).size == 0


def test_evidence_older_than_740_days_is_not_used():
    data = L.LearningData()
    p = pred()
    data.archive_forecast({K_NOON: p}, {}, NOON - timedelta(hours=10))
    _station(data, K_NOON, 0.4)
    _decide(data, "2026-10-07", int((NOON + timedelta(hours=14)).timestamp()))
    assert L.build_evidence(data, int((NOON + timedelta(days=739)).timestamp()), TZ).size == 1
    assert L.build_evidence(data, int((NOON + timedelta(days=741)).timestamp()), TZ).size == 0


def test_predictors_are_rounded_once_so_stored_equals_used():
    rec = HourRecord(start=NOON, ghi=412.123456789, dni=300.0, dhi=133.987654321)
    full = (40.1234567, 10.7654321, 5.0, 12.3456789, 8.7654321, 44.4444444)
    p = L.build_predictors({NOON: rec}, {K_NOON: full, K_NOON + 3600: full}, LAT, LON)[K_NOON]
    for value in (p.kt, p.kd, p.low, p.mid, p.temp, p.dew, p.elevation, p.e0h, p.total):
        assert round(value, 6) == value
    data = L.LearningData()
    data.archive_forecast({K_NOON: p}, {}, NOON - timedelta(hours=1))
    assert L.predictors_from_row(K_NOON, data.d["archive"][K_NOON][0]) == p


def test_freeze_moves_the_cutoff_and_reset_sets_a_floor():
    data = L.LearningData()
    t0 = datetime(2026, 10, 8, tzinfo=UTC)
    data.set_frozen(True, t0)
    assert data.evidence_cutoff(int(t0.timestamp()) + 9999) == int(t0.timestamp())
    data.set_frozen(False, t0 + timedelta(days=2))
    assert data.evidence_cutoff(int((t0 + timedelta(days=3)).timestamp())) == int((t0 + timedelta(days=3)).timestamp())
    assert data.evidence_cutoff(int((t0 + timedelta(days=1)).timestamp())) == int(t0.timestamp())
    # reset: evidence decided before it is ignored afterwards, kept before
    p = pred()
    data.archive_forecast({K_NOON: p}, {}, NOON - timedelta(hours=10))
    _station(data, K_NOON, 0.4)
    decided = int((NOON + timedelta(hours=14)).timestamp())
    _decide(data, "2026-10-07", decided)
    reset = decided + 100
    data.d["resets"].append([reset, "stage1"])
    assert L.build_evidence(data, reset - 1, TZ).size == 1
    assert L.build_evidence(data, reset + 1, TZ).size == 0
    assert L.usable_day(data, "2026-10-07", reset + 1, "stage2")  # other scope unaffected


def test_corrections_at_issue_time_are_stored_with_the_row():
    data = L.LearningData()
    p = pred()
    c = L.Correction(shift_kt=-0.1, shift_kd=0.05, shrink=0.5, analogs=10, pooled=True)
    data.archive_forecast({K_NOON: p}, {K_NOON: c}, NOON - timedelta(hours=3))
    row = data.d["archive"][K_NOON][0]
    assert L.correction_from_row(row) == (-0.1, 0.05)
    assert row[L.R["analogs"]] == 10 and row[L.R["pooled"]] == 1


# ---------------------------------------------------------------------------
# Applying corrections
# ---------------------------------------------------------------------------


def test_apply_without_shift_returns_the_same_objects():
    rec = _record(K_NOON)
    out, summary = L.apply_corrections({rec.start: rec}, {}, None, LAT, LON)
    assert out[rec.start] is rec and summary["hours_changed"] == 0
    out, _ = L.apply_corrections({rec.start: rec}, {K_NOON: (0.0, 0.0)}, None, LAT, LON)
    assert out[rec.start] is rec


def test_apply_shift_exactly():
    rec = _record(K_NOON, kt=0.6, kd=0.3)
    e0h, _ = L.hour_geometry(K_NOON, LAT, LON)
    out, summary = L.apply_corrections({rec.start: rec}, {K_NOON: (-0.1, 0.2)}, None, LAT, LON)
    new = out[rec.start]
    assert new.ghi == pytest.approx(0.5 * e0h, abs=2e-3)
    assert new.dhi == pytest.approx(0.25 * e0h, abs=2e-3)
    assert new.dhi <= new.ghi
    assert summary["hours_changed"] == 1


def test_apply_stage2_factor_by_bin_of_the_shifted_kt():
    rec = _record(K_NOON, kt=0.6, kd=0.3)
    e0h, _ = L.hour_geometry(K_NOON, LAT, LON)
    stage2 = {"overcast_warm": {"factor": 0.9}, "broken_warm": {"factor": 1.1}, "clear_warm": {"factor": 1.0},
              "broken_hot": {"factor": 0.95}, "broken_cool": {"factor": 1.05}}
    out, _ = L.apply_corrections({rec.start: rec}, {K_NOON: (-0.1, 0.0)}, stage2, LAT, LON)
    assert out[rec.start].ghi == pytest.approx(0.55 * e0h, abs=2e-3)  # 0.5 -> broken, no temperature -> warm
    # cell temperature = forecast air temperature + 0.0342 x corrected light (0.5 * e0h)
    hot_air = L.STAGE2_TEMP_EDGES[1] - L.CELL_HEATING_K_PER_W_M2 * 0.5 * e0h + 0.01
    out, _ = L.apply_corrections({rec.start: rec}, {K_NOON: (-0.1, 0.0)}, stage2, LAT, LON, {K_NOON: hot_air})
    assert out[rec.start].ghi == pytest.approx(0.475 * e0h, abs=2e-3)  # hot band -> x0.95
    out, _ = L.apply_corrections({rec.start: rec}, {K_NOON: (-0.1, 0.0)}, stage2, LAT, LON, {K_NOON: hot_air - 0.02})
    assert out[rec.start].ghi == pytest.approx(0.55 * e0h, abs=2e-3)  # just below: warm
    out, _ = L.apply_corrections({rec.start: rec}, {K_NOON: (0.0, 0.0)}, stage2, LAT, LON)
    assert out[rec.start] is rec  # clear bin factor 1, no shift
    stage2["clear_warm"]["factor"] = 99  # out-of-bounds factor is clamped
    out, _ = L.apply_corrections({rec.start: rec}, {K_NOON: (0.0, 0.0)}, stage2, LAT, LON)
    assert out[rec.start].ghi == pytest.approx(min(0.6 * L.STAGE2_MAX, 1.0) * e0h, abs=2e-3)


def test_apply_leaves_low_sun_and_records_without_average_alone():
    low = L.hour_key(datetime(2026, 10, 7, 5, tzinfo=UTC))  # 07:00 local, sun < 5°
    rec = HourRecord(start=L.key_to_dt(low), ghi=10.0, dni=0.0, dhi=10.0)
    inst = HourRecord(start=NOON, ghi_instant=400.0, dni_instant=500.0, dhi_instant=100.0)
    out, _ = L.apply_corrections({rec.start: rec, inst.start: inst}, {low: (-0.1, 0.0), K_NOON: (-0.1, 0.0)}, None, LAT, LON)
    assert out[rec.start] is rec and out[inst.start] is inst


def test_apply_shifts_the_instant_in_kt_space():
    e0h_i = None
    rec = _record(K_NOON, kt=0.6, kd=0.3)
    from custom_components.open_meteo_solar_forecast.local_source import sun_position

    sun = sun_position(NOON, LAT, LON)
    e0h_i = sun.extraterrestrial_normal * sun.cos_zenith
    rec = HourRecord(start=rec.start, ghi=rec.ghi, dni=rec.dni, dhi=rec.dhi,
                     ghi_instant=0.7 * e0h_i, dni_instant=600.0, dhi_instant=0.2 * 0.7 * e0h_i)
    out, _ = L.apply_corrections({rec.start: rec}, {K_NOON: (-0.2, 0.1)}, None, LAT, LON)
    new = out[rec.start]
    assert new.ghi_instant == pytest.approx(0.5 * e0h_i, abs=2e-3)
    assert new.dhi_instant == pytest.approx(0.3 * 0.5 * e0h_i, abs=2e-3)
    assert 0 <= new.dni_instant <= 1500


# ---------------------------------------------------------------------------
# Stage 2
# ---------------------------------------------------------------------------


def _day_hours_with_sun(day, min_elev):
    return [k for k in L.local_day_hours(day, TZ) if L.hour_geometry(k, LAT, LON)[1] >= min_elev]


def test_stage2_factor_by_hand():
    data = L.LearningData()
    day = "2026-10-07"
    hours = _day_hours_with_sun(day, 10.0)
    assert len(hours) >= 8
    for k in hours:
        _station(data, k, 0.2)  # overcast bin
        data.d["production"].setdefault("10K", {})[k] = 1.0
        data.d["model"].setdefault("10K", {})[k] = 2.0
    now = datetime(2026, 10, 8, 2, tzinfo=UTC)
    _decide(data, day, int(now.timestamp()) - 10)
    result = L.stage2_factors(data, "10K", now, TZ, LAT, LON)
    n = len(hours)
    assert set(result) == set(L.STAGE2_CELLS) and len(result) == 9
    cell = result["overcast_cool"]  # 10 °C air + little light -> cool band
    assert cell["hours"] == n
    assert cell["ratio"] == pytest.approx(0.5)
    assert cell["factor"] == pytest.approx(max(1 - 0.5 * n / (n + 40), L.STAGE2_MIN), abs=1e-4)
    assert result["clear_warm"] == {"factor": 1.0, "ratio": 1.0, "hours": 0, "production_kwh": 0.0, "model_kwh": 0.0}
    assert result["overcast_warm"]["hours"] == 0 and result["overcast_hot"]["hours"] == 0
    # hours the geometry darkens by more than 10 % are left out; exactly 10 % stays in
    data.d["model_free"]["10K"] = {hours[0]: 2.0 / 0.89, hours[1]: 2.0 / 0.9, hours[2]: 2.0}
    assert L.stage2_factors(data, "10K", now, TZ, LAT, LON)["overcast_cool"]["hours"] == n - 1
    # an excluded day contributes nothing
    _decide(data, day, int(now.timestamp()) - 10, "snow")
    assert L.stage2_factors(data, "10K", now, TZ, LAT, LON)["overcast_cool"]["hours"] == 0


def test_stage2_bounds():
    data = L.LearningData()
    day = "2026-10-07"
    for k in _day_hours_with_sun(day, 10.0):
        _station(data, k, 0.7)
        data.d["production"].setdefault("g", {})[k] = 10.0
        data.d["model"].setdefault("g", {})[k] = 1.0
    # many hours: replicate the day 30 times by shifting keys one week each would leave the window;
    # instead give a huge ratio: the bound applies even with few hours.
    now = datetime(2026, 10, 8, 2, tzinfo=UTC)
    _decide(data, day, int(now.timestamp()) - 10)
    result = L.stage2_factors(data, "g", now, TZ, LAT, LON)
    clear = [result[c] for c in L.STAGE2_CELLS if c.startswith("clear_") and result[c]["hours"]]
    assert clear and all(item["factor"] == L.STAGE2_MAX for item in clear)


# ---------------------------------------------------------------------------
# Quality control
# ---------------------------------------------------------------------------

GROUP = L.GroupInfo("10K", 10.0, ("e1",))


def _full_day(data, day, kt=0.5, prod_per_hour=1.0, model_per_hour=1.0, albedo=0.05):
    for k in L.local_day_hours(day, TZ):
        e0h, elev = L.hour_geometry(k, LAT, LON)
        data.d["production"].setdefault("10K", {})[k] = prod_per_hour if elev >= L.MIN_ELEVATION_DEG else 0.0
        if elev >= L.MIN_ELEVATION_DEG:
            ghi = kt * e0h
            data.d["station_hours"][k] = [ghi, 0.3 * ghi, 30.0, 300.0, albedo * ghi, 10.0, 6.0]
            data.d["model"].setdefault("10K", {})[k] = model_per_hour


DAY = "2026-10-07"
NIGHT_AFTER = datetime(2026, 10, 8, 1, 30, tzinfo=UTC)


def test_day_ok():
    data = L.LearningData()
    _full_day(data, DAY)
    status, details = L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, NIGHT_AFTER)
    assert status == L.DAY_OK and details["ratio"]["10K"] == pytest.approx(1.0)


def test_day_station_incomplete():
    data = L.LearningData()
    _full_day(data, DAY)
    daylight = _day_hours_with_sun(DAY, L.MIN_ELEVATION_DEG)
    for k in daylight[: math.ceil(len(daylight) * 0.25)]:
        del data.d["station_hours"][k]
    assert L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, NIGHT_AFTER)[0] == "station_incomplete"


def test_day_snow():
    data = L.LearningData()
    _full_day(data, DAY, albedo=0.6)
    status, details = L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, NIGHT_AFTER)
    assert status == "snow" and details["albedo"] == pytest.approx(0.6)


def test_day_production_missing_waits_then_decides():
    data = L.LearningData()
    _full_day(data, DAY)
    del data.d["production"]["10K"][L.local_day_hours(DAY, TZ)[0]]
    assert L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, NIGHT_AFTER)[0] is None
    late = NIGHT_AFTER + timedelta(days=L.MODEL_WAIT_DAYS + 1)
    assert L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, late)[0] == "production_missing"


def test_day_production_fault():
    data = L.LearningData()
    _full_day(data, DAY)
    data.d["production"]["10K"][L.local_day_hours(DAY, TZ)[12]] = 12.5  # > 1.2 x 10 kWp
    assert L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, NIGHT_AFTER)[0] == "production_fault"
    data.d["production"]["10K"][L.local_day_hours(DAY, TZ)[12]] = -0.1
    assert L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, NIGHT_AFTER)[0] == "production_fault"


@pytest.mark.parametrize(("ratio", "status"), [(0.59, "not_representative"), (0.61, L.DAY_OK),
                                               (1.39, L.DAY_OK), (1.41, "not_representative")])
def test_day_ratio_band(ratio, status):
    data = L.LearningData()
    _full_day(data, DAY, prod_per_hour=ratio, model_per_hour=1.0)
    assert L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, NIGHT_AFTER)[0] == status


def test_day_without_station_model_waits_then_is_unchecked():
    data = L.LearningData()
    _full_day(data, DAY)
    data.d["model"] = {}
    assert L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, NIGHT_AFTER)[0] is None
    status, details = L.decide_day(data, DAY, [GROUP], TZ, LAT, LON, NIGHT_AFTER + timedelta(days=4))
    assert status == L.DAY_OK and details["ratio_unchecked"] == ["10K"]


def test_representativeness():
    data = L.LearningData()
    rng = random.Random(3)
    for d in range(1, 8):
        day = (datetime(2026, 10, 1) + timedelta(days=d)).date().isoformat()
        _full_day(data, day, kt=rng.uniform(0.1, 0.7))
        for k in _day_hours_with_sun(day, 10):
            data.d["production"]["10K"][k] = data.d["station_hours"][k][0] / 100.0
    now = datetime(2026, 10, 9, 2, tzinfo=UTC)
    rep = L.representativeness(data, [GROUP], now, LAT, LON)
    assert rep["r"] == pytest.approx(1.0) and rep["representative"]
    for k in data.d["production"]["10K"]:
        data.d["production"]["10K"][k] = rng.uniform(0, 5)
    assert not L.representativeness(data, [GROUP], now, LAT, LON)["representative"]
    assert L.representativeness(L.LearningData(), [GROUP], now, LAT, LON) == {"r": None, "hours": 0, "representative": True}


# ---------------------------------------------------------------------------
# Skill gate and state
# ---------------------------------------------------------------------------


def _gate_data(days, raw_err=50.0, corr_err=0.0, energy_raw=20.0, energy_corr=18.0, actual=18.0):
    data = L.LearningData()
    today = datetime(2026, 10, 30, 2, tzinfo=UTC)
    for i in range(1, days + 1):
        day = (today.astimezone(TZ).date() - timedelta(days=i)).isoformat()
        hours = _day_hours_with_sun(day, 15)[:5]
        issue = {}
        for k in hours:
            e0h = 500.0
            data.d["station_hours"][k] = [250.0, 75.0, 0.0, 300.0, 10.0, 10.0, 6.0]
            issue[k] = [(250.0 + raw_err) / e0h, (250.0 + corr_err) / e0h, 0.3, 0.3, e0h]
        data.d["issue6"][day] = {"captured": 0, "hours": issue}
        _decide(data, day, 1)
        all_hours = L.local_day_hours(day, TZ)
        for k in all_hours:
            data.d["production"].setdefault("10K", {})[k] = actual / len(all_hours)
        data.d["entry_days"][day] = {"e1": {"captured": 0, "raw": energy_raw * 1000, "corr": energy_corr * 1000}}
    return data, today


def test_gate_passes_with_14_days_of_better_forecasts():
    data, now = _gate_data(14)
    gate = L.evaluate_gate(data, [GROUP], now, TZ)
    assert gate["passed"] and gate["days"] == 14
    assert gate["rmse_raw_w_m2"] == pytest.approx(50.0) and gate["rmse_corrected_w_m2"] == pytest.approx(0.0)
    assert gate["improvement"] == pytest.approx(1.0)
    assert gate["groups"]["10K"] == {"days": 14, "mae_raw_kwh": 2.0, "mae_corrected_kwh": 0.0, "passed": True}


def test_gate_fails_with_13_days():
    data, now = _gate_data(13)
    gate = L.evaluate_gate(data, [GROUP], now, TZ)
    assert not gate["passed"] and "13/14" in gate["reasons"][0]


def test_gate_fails_below_five_percent():
    data, now = _gate_data(20, raw_err=50.0, corr_err=48.0)  # 4 % better
    gate = L.evaluate_gate(data, [GROUP], now, TZ)
    assert not gate["passed"] and gate["improvement"] == pytest.approx(0.04)


def test_gate_fails_when_energy_gets_worse():
    data, now = _gate_data(20, energy_raw=18.5, energy_corr=17.0)
    gate = L.evaluate_gate(data, [GROUP], now, TZ)
    assert not gate["passed"] and not gate["groups"]["10K"]["passed"]


def test_gate_ignores_days_not_ok_and_needs_groups():
    data, now = _gate_data(20)
    for day in list(data.d["days"])[:10]:
        data.d["days"][day]["status"] = "snow"
    assert L.evaluate_gate(data, [GROUP], now, TZ)["days"] == 10
    assert not L.evaluate_gate(data, [], now, TZ)["passed"]


@pytest.mark.parametrize(
    ("gate", "switch", "rep", "ever", "state"),
    [
        (None, True, True, False, L.STATE_COLLECTING),
        ({"algorithm": L.ALGORITHM_VERSION, "passed": False, "days": 3, "reasons": ["x"]}, True, True, False, L.STATE_COLLECTING),
        ({"algorithm": L.ALGORITHM_VERSION, "passed": False, "days": 20, "reasons": ["x"]}, True, True, False, L.STATE_SHADOW),
        ({"algorithm": L.ALGORITHM_VERSION, "passed": True, "days": 20}, False, True, False, L.STATE_ELIGIBLE),
        ({"algorithm": L.ALGORITHM_VERSION, "passed": True, "days": 20}, True, True, False, L.STATE_APPLIED),
        ({"algorithm": L.ALGORITHM_VERSION, "passed": False, "days": 20, "reasons": []}, True, True, True, L.STATE_PAUSED),
        ({"algorithm": L.ALGORITHM_VERSION, "passed": True, "days": 20}, True, False, False, L.STATE_PAUSED),
        ({"algorithm": L.ALGORITHM_VERSION + 1, "passed": True, "days": 20}, True, True, True, L.STATE_COLLECTING),
    ],
)
def test_state_matrix(gate, switch, rep, ever, state):
    assert L.derive_state(gate, switch, rep, ever)[0] == state


SUNRISE = L.sunrise_utc("2026-10-07", TZ, LAT, LON)
DECISION = SUNRISE + timedelta(hours=1)


def test_sunrise_by_hand():
    # Zurich, 7 Oct 2026: sunrise 07:33 local (05:33 UTC) within the 2-min step
    assert datetime(2026, 10, 7, 5, 30, tzinfo=UTC) <= SUNRISE <= datetime(2026, 10, 7, 5, 36, tzinfo=UTC)
    before = SUNRISE - timedelta(minutes=2)
    sun = L.sun_position(before, LAT, LON)
    assert math.degrees(math.asin(sun.cos_zenith)) < L.SUNRISE_ELEVATION_DEG
    assert L.sunrise_utc("2026-06-21", TZ, LAT, LON) < datetime(2026, 6, 21, 3, 40, tzinfo=UTC)


def test_decision_forecast_captured_once_from_sunrise_plus_one_hour():
    data = L.LearningData()
    p = pred()
    assert not L.capture_issue6(data, {K_NOON: p}, {}, DECISION - timedelta(minutes=1), TZ, LAT, LON)
    c = L.Correction(shift_kt=-0.1)
    assert L.capture_issue6(data, {K_NOON: p}, {K_NOON: c}, DECISION, TZ, LAT, LON)
    assert data.d["issue6"]["2026-10-07"]["hours"][K_NOON][:2] == [0.6, 0.5]
    assert not L.capture_issue6(data, {K_NOON: p}, {}, DECISION + timedelta(hours=1), TZ, LAT, LON)


def test_entry_day_only_same_day_from_sunrise_plus_one_hour():
    data = L.LearningData()
    early = DECISION - timedelta(minutes=1)
    assert not L.record_entry_day(data, "e1", "2026-10-07", 1, 2, early, TZ, LAT, LON)
    assert not L.record_entry_day(data, "e1", "2026-10-08", 1, 2, DECISION, TZ, LAT, LON)
    assert L.record_entry_day(data, "e1", "2026-10-07", 1, 2, DECISION, TZ, LAT, LON)
    assert not L.record_entry_day(data, "e1", "2026-10-07", 9, 9, DECISION + timedelta(hours=1), TZ, LAT, LON)
    assert data.d["entry_days"]["2026-10-07"]["e1"]["raw"] == 1.0


# ---------------------------------------------------------------------------
# Store (de)serialisation and retention
# ---------------------------------------------------------------------------


def _populated():
    data = L.LearningData()
    data.archive_forecast({K_NOON: pred()}, {}, NOON - timedelta(hours=3))
    _station(data, K_NOON, 0.4)
    data.add_station_sample(L.StationSample(K_NOON + 600, 1.0, 0.5, 0.0, 300.0, None, 10.0))
    data.d["production"]["10K"] = {K_NOON: 1.5}
    data.d["model"]["10K"] = {K_NOON: 1.4}
    _decide(data, "2026-10-07", 5)
    data.d["issue6"]["2026-10-07"] = {"captured": 1, "hours": {K_NOON: [0.6, 0.5, 0.3, 0.3, 500.0]}}
    data.d["entry_days"]["2026-10-07"] = {"e1": {"captured": 1, "raw": 1.0, "corr": 2.0}}
    data.event(NOON, "x", "y")
    return data


def test_round_trip_through_json():
    data = _populated()
    text = json.dumps(data.to_json())
    again = L.LearningData.from_json(json.loads(text))
    assert again.to_json() == data.to_json()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema=99),
        lambda d: d.update(settings="x"),
        lambda d: d["settings"].update(apply="yes"),
        lambda d: d.update(freeze=[[1]]),
        lambda d: d.update(resets=[[1, "everything"]]),
        lambda d: d["archive"][next(iter(d["archive"]))].update({"7": [0] * 15}),
        lambda d: d["archive"][next(iter(d["archive"]))]["0"].__setitem__(1, float("nan")),
        lambda d: d["archive"][next(iter(d["archive"]))]["0"].append(1),
        lambda d: d["station_hours"].update({"abc": [0] * 7}),
        lambda d: d["days"].update({"2026-13-01": {"decided": 1, "status": "ok"}}),
        lambda d: d["days"]["2026-10-07"].update(status="great"),
        lambda d: d["stage2"]["groups"].update({"g": {"overcast": {"factor": 1}, "broken": {"factor": 1},
                                                     "clear": {"factor": 1}}}),  # old 3-cell shape
        lambda d: d["stage2"]["groups"].update(
            {"g": {c: {"factor": 3 if c == "broken_hot" else 1} for c in L.STAGE2_CELLS}}),  # out of bounds
        lambda d: d.update(state={"state": "dancing"}),
        lambda d: d.update(state="x"),
        lambda d: d["production"].update({"g": {"1": True}}),
        lambda d: d["entry_days"]["2026-10-07"]["e1"].update(raw=None),
        lambda d: d.update(events="x"),
    ],
)
def test_malformed_store_is_rejected(mutate):
    raw = json.loads(json.dumps(_populated().to_json()))
    mutate(raw)
    with pytest.raises(L.LearningDataError):
        L.LearningData.from_json(raw)


def test_non_mapping_store_is_rejected():
    with pytest.raises(L.LearningDataError):
        L.LearningData.from_json([1, 2])


def test_purge_keeps_760_days():
    data = _populated()
    later = NOON + timedelta(days=761)
    data.purge(later, TZ)
    assert data.d["archive"] == {} and data.d["station_hours"] == {} and data.d["days"] == {}
    assert data.d["production"]["10K"] == {} and data.d["issue6"] == {}
    data = _populated()
    data.purge(NOON + timedelta(days=759), TZ)
    assert K_NOON in data.d["archive"]


def test_event_log_is_bounded():
    data = L.LearningData()
    for i in range(L.EVENT_LOG_MAX + 20):
        data.event(NOON, "k", str(i))
    assert len(data.d["events"]) == L.EVENT_LOG_MAX and data.d["events"][-1][2] == str(L.EVENT_LOG_MAX + 19)


def test_switch_and_freeze_are_logged():
    data = L.LearningData()
    assert data.set_apply(True, NOON) and not data.set_apply(True, NOON)
    assert data.set_frozen(True, NOON) and data.frozen
    assert data.set_frozen(False, NOON) and not data.frozen
    assert [e[1] for e in data.d["events"]] == ["apply_on", "frozen", "unfrozen"]


# ---------------------------------------------------------------------------
# Synthetic learning: the learner must recover a known bias (spec §14)
# ---------------------------------------------------------------------------


def _simulate(days, bias=-0.2, seed=1):
    rng = random.Random(seed)
    data = L.LearningData()
    start = datetime(2026, 9, 1, tzinfo=UTC)
    for d in range(days):
        day0 = start + timedelta(days=d)
        issue = day0 - timedelta(hours=2)
        preds = {}
        for h in range(24):
            k = L.hour_key(day0 + timedelta(hours=h))
            e0h, elev = L.hour_geometry(k, LAT, LON)
            if elev < L.MIN_ELEVATION_DEG:
                continue
            kt = rng.uniform(0.2, 0.75)
            low = rng.choice([0.0, 10.0, 80.0, 95.0])
            preds[k] = L.Predictors(k, kt, 0.3, low, rng.uniform(0, 30), 0.0, 12.0, 8.0, elev, e0h, min(low + 10, 100))
            kt_m = max(kt + (bias if low > 50 else 0.0) + rng.gauss(0, 0.02), 0.02)
            data.d["station_hours"][k] = [kt_m * e0h, 0.3 * kt_m * e0h, 0.0, 300.0, 20.0, 12.0, 6.0]
        corr = L.compute_corrections(data, preds, issue, TZ)
        data.archive_forecast(preds, corr, issue)
        day = day0.astimezone(TZ).date().isoformat()
        _decide(data, day, int((day0 + timedelta(days=1, hours=2)).timestamp()))
    return data, start + timedelta(days=days)


def test_learner_recovers_a_low_cloud_bias_and_leaves_clear_hours_alone():
    data, day0 = _simulate(40)
    issue = day0 - timedelta(hours=2)
    key = L.hour_key(day0 + timedelta(hours=10))
    e0h, elev = L.hour_geometry(key, LAT, LON)
    ev = L.build_evidence(data, int(issue.timestamp()), TZ)
    foggy = L.analog_correction(ev, L.Predictors(key, 0.6, 0.3, 90.0, 10.0, 0.0, 12.0, 8.0, elev, e0h, 100.0), 1)
    clear = L.analog_correction(ev, L.Predictors(key, 0.6, 0.3, 5.0, 10.0, 0.0, 12.0, 8.0, elev, e0h, 15.0), 1)
    assert foggy.mean_error_kt == pytest.approx(-0.2, abs=0.03)
    assert foggy.shift_kt == pytest.approx(foggy.shrink * foggy.mean_error_kt)
    assert foggy.shift_kt < -0.1
    assert abs(clear.shift_kt) < 0.02


def test_learner_starts_neutral():
    data, day0 = _simulate(1)
    rows = [row for buckets in data.d["archive"].values() for row in buckets.values()]
    assert rows and all(row[L.R["shift_kt"]] == 0.0 for row in rows)


def test_learning_is_deterministic():
    a, _ = _simulate(20, seed=5)
    b, _ = _simulate(20, seed=5)
    assert json.dumps(a.to_json()) == json.dumps(b.to_json())


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_exports_are_parseable_and_complete():
    import csv
    import io

    data = _populated()
    archive = list(csv.DictReader(io.StringIO(L.export_archive_csv(data, 2026))))
    assert len(archive) == 1 and archive[0]["lead_bucket"] == "0-6h"
    assert float(archive[0]["kt"]) == pytest.approx(0.6)
    station = list(csv.DictReader(io.StringIO(L.export_station_csv(data, 2026))))
    assert float(station[0]["ghi"]) == pytest.approx(data.d["station_hours"][K_NOON][0])
    days = list(csv.DictReader(io.StringIO(L.export_days_csv(data, [GROUP], TZ))))
    assert days[0]["status"] == "ok" and days[0]["10K_forecast_corrected_kwh"] == "0.002"
    assert "kt_corrected" in L.export_issue6_csv(data)
    assert L.export_events_csv(data).count("\n") == 2
    settings = L.export_settings(data, [GROUP], TZ, LAT, LON)
    assert settings["parameters"]["k_neighbours"] == 25 and settings["timezone"] == "Europe/Zurich"
    assert L.export_archive_csv(data, 2025).count("\n") == 1  # header only


def test_view_isolates_computations_from_live_changes():
    data = _populated()
    view = data.view()
    data.d["entry_days"]["2026-10-07"]["e2"] = {"captured": 2, "raw": 1.0, "corr": 1.0}
    data.d["entry_days"]["2026-10-08"] = {}
    data.d["freeze"].append([1, None])
    data.d["settings"]["apply"] = True
    data.d["production"]["10K"][K_NOON + 3600] = 9.0
    assert "e2" not in view.d["entry_days"]["2026-10-07"] and "2026-10-08" not in view.d["entry_days"]
    assert view.d["freeze"] == [] and view.d["settings"]["apply"] is False
    assert K_NOON + 3600 not in view.d["production"]["10K"]
    assert view.d["archive"] == data.d["archive"]  # same evidence


def test_hours_with_the_sun_below_five_degrees_get_no_predictors():
    day = "2026-10-07"
    low_sun = [k for k in L.local_day_hours(day, TZ)
               if L.hour_geometry(k, LAT, LON)[0] > 0 and 0 < L.hour_geometry(k, LAT, LON)[1] < L.MIN_ELEVATION_DEG]
    assert low_sun  # the sunrise hour
    key = low_sun[0]
    rec = HourRecord(start=L.key_to_dt(key), ghi=20.0, dni=50.0, dhi=15.0)
    full = (40.0, 10.0, 5.0, 12.0, 8.0, 45.0)
    assert L.build_predictors({rec.start: rec}, {key: full, key + 3600: full}, LAT, LON) == {}


# ---------------------------------------------------------------------------
# 0.1.34.1: Fusion >= 0.3.5 `recent_values`
# ---------------------------------------------------------------------------


def _rec(minutes, ghi=100.0, **extra):
    end = NOON + timedelta(minutes=minutes)
    return {"time": end.isoformat(), "values": {**_values(ghi=ghi), **extra}}


def test_recent_values_are_parsed_record_by_record():
    raw = [_rec(10), _rec(20, ghi=200.0), "junk", {"time": "no"}, _rec(30, ghi=None),
           _rec(-8 * 60), _rec(60 + 30)]  # too old / too far ahead of the reference
    samples, rejected = L.parse_station_list(raw, NOON + timedelta(minutes=40))
    assert [s.end for s in samples] == [K_NOON + 600, K_NOON + 1200]
    assert samples[1].ghi == 200.0
    assert rejected == 5


def test_recent_values_list_is_bounded_and_typed():
    assert L.parse_station_list(None, NOON) == ([], 0)
    assert L.parse_station_list({"a": 1}, NOON) == ([], 1)
    too_long = [_rec(10)] * (L.RECENT_MAX_ENTRIES + 1)
    assert L.parse_station_list(too_long, NOON) == ([], L.RECENT_MAX_ENTRIES + 1)
    assert len(L.parse_station_list([_rec(10)] * L.RECENT_MAX_ENTRIES, NOON + timedelta(hours=1))[0]) == 100


def test_a_record_is_replaced_only_by_one_at_least_as_complete():
    data = L.LearningData()
    full = L.StationSample(K_NOON + 600, 100.0, 50.0, 10.0, 300.0, 20.0, 10.0)
    partial = L.StationSample(K_NOON + 600, 120.0, None, None, 300.0, 20.0, 10.0)
    assert data.add_station_sample(full)
    assert not data.add_station_sample(partial)  # less complete: kept
    assert data.d["station_samples"][K_NOON + 600][0] == 100.0
    corrected = L.StationSample(K_NOON + 600, 101.0, 50.0, 10.0, 300.0, 20.0, 10.0)
    assert data.add_station_sample(corrected)  # same completeness: newer wins
    assert data.d["station_samples"][K_NOON + 600][0] == 101.0
    assert not data.add_station_sample(corrected)



# ---------------------------------------------------------------------------
# 0.1.34.2: total cloud, temperature bands, long memory, migration
# ---------------------------------------------------------------------------


def test_hours_without_total_cloud_get_no_predictors():
    rec = _record(K_NOON)
    no_total = (40.0, 10.0, 5.0, 12.0, 8.0, None)
    assert L.build_predictors({rec.start: rec}, {K_NOON: no_total, K_NOON + 3600: no_total}, LAT, LON) == {}


def test_low_cloud_separates_analogs_at_equal_total_cloud():
    """Fog (low cloud) and high veil can have the same total cover."""
    p = pred(low=80.0, total=80.0)
    v = list(p.vector())
    far = [v[0], v[1], 80.0 - 38.0, v[3], v[4], v[5]]  # 38 % less low cloud: 1.52 scale units
    near = [v[0], v[1], 80.0 - 37.0, v[3], v[4], v[5]]
    assert L.analog_correction(evidence([(K_NOON, 1, far, -0.2, 0.0)] * 30), p, 1).analogs == 0
    assert L.analog_correction(evidence([(K_NOON, 1, near, -0.2, 0.0)] * 30), p, 1).analogs == 25


def test_total_cloud_separates_analogs():
    p = pred(total=90.0)
    v = list(p.vector())
    far = v[:5] + [90.0 - 46.0]  # 46 % less total cloud: 1.53 scale units
    near = v[:5] + [90.0 - 44.0]
    assert L.analog_correction(evidence([(K_NOON, 1, far, -0.2, 0.0)] * 30), p, 1).analogs == 0
    assert L.analog_correction(evidence([(K_NOON, 1, near, -0.2, 0.0)] * 30), p, 1).analogs == 25


@pytest.mark.parametrize(("temp", "cell"), [(None, "broken_warm"), (-3.0, "broken_cool"), (24.99, "broken_cool"),
                                            (25.0, "broken_warm"), (44.99, "broken_warm"), (45.0, "broken_hot")])
def test_stage2_cells_by_cell_temperature(temp, cell):
    assert L.stage2_cell(0.45, temp) == cell


def test_cell_temperature_follows_sunlight_not_only_air():
    assert L.cell_temperature(20.0, 900.0) == pytest.approx(20.0 + 0.0342 * 900.0)  # clear: ~ +31 K
    assert L.cell_temperature(20.0, 150.0) == pytest.approx(20.0 + 0.0342 * 150.0)  # overcast: ~ +5 K
    assert L.cell_temperature(20.0, -5.0) == 20.0
    assert L.cell_temperature(None, 900.0) is None
    # same air temperature, different sky -> different band
    assert L.stage2_cell(0.75, L.cell_temperature(20.0, 900.0)) == "clear_hot"
    assert L.stage2_cell(0.2, L.cell_temperature(20.0, 140.0)) == "overcast_cool"


@pytest.mark.parametrize(("modelled", "unshaded", "shaded"), [
    (0.89, 1.0, True), (0.9, 1.0, False), (1.0, 1.0, False), (0.5, None, False), (0.0, 0.0, False)])
def test_shaded_hour(modelled, unshaded, shaded):
    assert L.shaded_hour(modelled, unshaded) is shaded


def test_stage2_keeps_a_season_learned_long_ago():
    """A hot-day factor learned ten months ago still counts (weight >= 0.1)."""
    data = L.LearningData()
    summer = "2026-07-15"
    for k in _day_hours_with_sun(summer, 10.0):
        e0h, _ = L.hour_geometry(k, LAT, LON)
        data.d["station_hours"][k] = [0.7 * e0h, 0.2 * 0.7 * e0h, 60.0, 300.0, 30.0, 28.0, 6.0]
        data.d["production"].setdefault("g", {})[k] = 0.9
        data.d["model"].setdefault("g", {})[k] = 1.0
    _decide(data, summer, int(datetime(2026, 7, 16, 2, tzinfo=UTC).timestamp()))
    later = datetime(2027, 5, 15, 2, tzinfo=UTC)  # ten months later
    result = L.stage2_factors(data, "g", later, TZ, LAT, LON)
    cells = [result[c] for c in L.STAGE2_CELLS if result[c]["hours"]]
    assert sum(c["hours"] for c in cells) >= 8 and result["clear_hot"]["hours"] >= 4
    for cell in cells:
        n = cell["hours"]
        assert cell["ratio"] == pytest.approx(0.9)
        assert cell["factor"] == pytest.approx(1 - 0.1 * n / (n + 40), abs=1e-4)


def test_store_of_schema_1_is_migrated_keeping_measurements():
    raw = json.loads(json.dumps(_populated().to_json()))
    raw["schema"] = 1
    for buckets in raw["archive"].values():
        for row in buckets.values():
            row.pop()  # schema-1 rows had no total cloud
    raw["stage2"]["groups"] = {"10K": {"overcast": {"factor": 1.0}, "broken": {"factor": 1.0}, "clear": {"factor": 1.0}}}
    migrated = L.LearningData.from_json(raw)
    assert migrated.migrated_from == 1 and migrated.d["schema"] == L.STORE_SCHEMA_VERSION
    assert migrated.d["archive"] == {} and migrated.d["issue6"] == {} and migrated.d["gate"] is None
    assert migrated.d["station_hours"] and migrated.d["production"]["10K"] and migrated.d["days"]
    assert migrated.d["events"]  # kept
    assert migrated.d["model"]["10K"] and migrated.d["model_free"] == {}
    assert L.LearningData.from_json(json.loads(json.dumps(_populated().to_json()))).migrated_from is None


def test_retention_is_two_years():
    data = _populated()
    data.d["model_free"]["10K"] = {K_NOON: 1.5}
    data.purge(NOON + timedelta(days=700), TZ)
    assert K_NOON in data.d["archive"] and data.d["production"]["10K"] and data.d["model_free"]["10K"]
    data.purge(NOON + timedelta(days=761), TZ)
    assert data.d["archive"] == {} and data.d["model_free"]["10K"] == {}


def test_stage2_weights_old_hours_with_the_floor_by_hand():
    """Ratio = Σw·production / Σw·model; w = max(0.5^(age/90 d), 0.1)."""
    data = L.LearningData()
    now = datetime(2026, 10, 8, 2, tzinfo=UTC)
    expected = [0.0, 0.0]
    hours = 0
    for day, produced in (("2025-09-01", 0.8), ("2026-10-07", 1.0)):  # 400 days old (floor) and recent
        for k in _day_hours_with_sun(day, 10.0):
            e0h, _ = L.hour_geometry(k, LAT, LON)
            data.d["station_hours"][k] = [0.2 * e0h, 0.06 * e0h, 0.0, 300.0, 5.0, 0.0, 6.0]  # overcast, cold
            data.d["production"].setdefault("g", {})[k] = produced
            data.d["model"].setdefault("g", {})[k] = 1.0
            w = max(0.5 ** ((now.timestamp() - k) / 86400 / 90), 0.1)
            expected[0] += w * produced
            expected[1] += w
            hours += 1
        _decide(data, day, int(now.timestamp()) - 10)
    cell = L.stage2_factors(data, "g", now, TZ, LAT, LON)["overcast_cool"]
    assert cell["hours"] == hours
    assert cell["ratio"] == pytest.approx(expected[0] / expected[1], abs=1e-4)
    assert cell["ratio"] < 0.99  # the old day still counts (without the floor it would be ~0.991)
