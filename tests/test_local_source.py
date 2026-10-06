"""Unit tests for ``local_source`` (0.1.33.2) — pure logic, no Home Assistant.

Standard applied: *if I break the code, does a test fail?* Every rule in the
module has a test that fails when the rule is removed; the mutation record
in ``omsf_v0_1_33_2_ICS_quality_bug_testing_report.md`` lists which test catches what.
"""

from __future__ import annotations

import ast
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_components.open_meteo_solar_forecast import local_source as ls

from .fusion_fixtures import LAT, LON, fusion_series

UTC = timezone.utc
CEST = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 6, 9, 20, tzinfo=UTC)  # 11:20 local, sun up
HOUR0 = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
MODULE = Path(ls.__file__)


def _entry(start: datetime, **values) -> dict:
    base = {
        "period_start": start.isoformat(),
        "period_end": (start + timedelta(hours=1)).isoformat(),
        "ghi": 400.0,
        "dni": 500.0,
        "dhi": 120.0,
        "ghi_instant": 380.0,
        "dni_instant": 480.0,
        "dhi_instant": 115.0,
        "sources": 3,
    }
    base.update(values)
    return base


# ---------------------------------------------------------------------------
# Purity: the module must stay free of Home Assistant and I/O.
# ---------------------------------------------------------------------------
def test_the_engine_imports_nothing_from_home_assistant_or_io():
    tree = ast.parse(MODULE.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "math", "collections", "dataclasses", "datetime", "typing"}


def test_the_engine_never_reads_the_clock():
    source = MODULE.read_text()
    for forbidden in ("datetime.now(", "utcnow(", "time.time(", "date.today("):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# Solar geometry
# ---------------------------------------------------------------------------
def test_solar_noon_in_frauenfeld_is_south_and_at_the_expected_height():
    # 2026-10-06, declination about -5.2°: noon elevation = 90 - 47.55 - 5.2.
    noon = datetime(2026, 10, 6, 11, 14, tzinfo=UTC)
    sun = ls.sun_position(noon, LAT, LON)
    assert abs(math.degrees(sun.azimuth_rad) - 180) < 1.0
    assert abs(sun.elevation_deg - 37.2) < 0.4


def test_morning_sun_is_in_the_east_and_evening_sun_in_the_west():
    morning = ls.sun_position(datetime(2026, 6, 21, 5, 0, tzinfo=UTC), LAT, LON)
    evening = ls.sun_position(datetime(2026, 6, 21, 17, 0, tzinfo=UTC), LAT, LON)
    assert 45 < math.degrees(morning.azimuth_rad) < 120
    assert 240 < math.degrees(evening.azimuth_rad) < 315


def test_extraterrestrial_irradiance_peaks_in_january_and_dips_in_july():
    jan = ls.extraterrestrial_normal(datetime(2026, 1, 3, tzinfo=UTC))
    jul = ls.extraterrestrial_normal(datetime(2026, 7, 4, tzinfo=UTC))
    assert 1400 < jan < 1410
    assert 1315 < jul < 1325


def test_sunrise_and_sunset_bracket_noon_and_match_day_length():
    rise, set_ = ls.sunrise_sunset(date(2026, 10, 6), LAT, LON, CEST)
    assert rise.tzinfo is not None and set_ > rise
    assert timedelta(hours=11, minutes=15) < set_ - rise < timedelta(hours=11, minutes=35)
    assert rise.astimezone(CEST).hour == 7


def test_polar_night_and_polar_day_never_produce_a_zero_length_day():
    # The library divides by (sunset - sunrise); zero would crash damping.
    tz = timezone.utc
    rise, set_ = ls.sunrise_sunset(date(2026, 12, 21), 80.0, 15.0, tz)
    assert set_ > rise
    rise, set_ = ls.sunrise_sunset(date(2026, 6, 21), 80.0, 15.0, tz)
    assert set_ > rise
    assert set_ - rise > timedelta(hours=23)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
def test_a_valid_entry_is_accepted_with_both_triples():
    records, report = ls.parse_irradiance_series([_entry(HOUR0)])
    rec = records[HOUR0]
    assert rec.has_average and rec.has_instant
    assert (rec.ghi, rec.dni, rec.dhi, rec.sources) == (400.0, 500.0, 120.0, 3)
    assert report.accepted_average == 1 and report.rejected == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"period_start": "2026-10-06T09:00:00"}, "non-timezone-aware"),
        ({"period_start": "garbage"}, "non-timezone-aware"),
        ({"period_start": "2026-10-06T09:30:00+00:00"}, "not on a full hour"),
        ({"period_end": "2026-10-06T11:00:00+00:00"}, "period_start + 1 h"),
    ],
)
def test_entries_with_bad_timestamps_are_rejected_and_reported(change, reason):
    records, report = ls.parse_irradiance_series([_entry(HOUR0, **change)])
    assert records == {}
    assert any(reason in item for item in report.rejected)


@pytest.mark.parametrize(
    "change",
    [
        {"ghi": -1.0},
        {"dni": 1500.1},
        {"dhi": 500.0},  # dhi > ghi + tolerance
        {"ghi": None},  # incomplete
        {"dni": None},  # incomplete (mutation M11 escaped without these)
        {"dhi": None},
        {"ghi": "nan"},
        {"ghi": float("inf")},
        {"dni": True},  # bool is not a number (M12: ghi=True was rejected
                        # for dhi > ghi instead, so the test proved nothing)
    ],
)
def test_an_invalid_average_triple_is_dropped_but_the_instant_survives(change):
    records, report = ls.parse_irradiance_series([_entry(HOUR0, **change)])
    rec = records[HOUR0]
    assert not rec.has_average and rec.ghi is None and rec.dni is None and rec.dhi is None
    assert rec.has_instant
    assert report.accepted_average == 0 and report.accepted_instant == 1
    assert report.rejected


def test_diffuse_slightly_above_global_within_rounding_tolerance_is_accepted():
    records, _ = ls.parse_irradiance_series([_entry(HOUR0, ghi=100.0, dhi=104.9)])
    assert records[HOUR0].has_average


def test_a_timestamp_with_offset_is_normalised_to_utc():
    local = HOUR0.astimezone(CEST)
    records, _ = ls.parse_irradiance_series(
        [_entry(HOUR0, period_start=local.isoformat(), period_end=(local + timedelta(hours=1)).isoformat())]
    )
    assert list(records) == [HOUR0]


def test_a_duplicate_hour_is_reported_and_the_later_entry_wins():
    records, report = ls.parse_irradiance_series([_entry(HOUR0, ghi=300.0), _entry(HOUR0, ghi=310.0)])
    assert records[HOUR0].ghi == 310.0
    assert any("duplicate" in item for item in report.rejected)


@pytest.mark.parametrize("raw", [None, {}, "x", 5])
def test_a_series_that_is_not_a_list_yields_nothing(raw):
    records, report = ls.parse_irradiance_series(raw)
    assert records == {} and report.rejected


def test_rejection_reporting_is_bounded():
    bad = [_entry(HOUR0 + timedelta(hours=h), ghi=-1, ghi_instant=-1) for h in range(200)]
    _, report = ls.parse_irradiance_series(bad)
    assert len(report.rejected) == 50


def test_temperature_forecast_converts_fahrenheit_and_kelvin():
    raw = [{"datetime": HOUR0.isoformat(), "temperature": 50.0}]
    temps, _ = ls.parse_temperature_forecast(raw, "°F")
    assert temps[HOUR0] == pytest.approx(10.0)
    temps, _ = ls.parse_temperature_forecast(raw, "K")
    assert temps == {}  # 50 K is -223 °C: rejected as implausible
    temps, _ = ls.parse_temperature_forecast([{"datetime": HOUR0.isoformat(), "temperature": 283.15}], "K")
    assert temps[HOUR0] == pytest.approx(10.0)


def test_temperature_entries_that_are_implausible_or_malformed_are_counted():
    raw = [
        {"datetime": HOUR0.isoformat(), "temperature": 75.0},
        {"datetime": "nonsense", "temperature": 10.0},
        {"datetime": HOUR0.isoformat()},
        "not a dict",
        {"datetime": (HOUR0 + timedelta(hours=1)).isoformat(), "temperature": 11.0},
    ]
    temps, rejected = ls.parse_temperature_forecast(raw, "°C")
    assert rejected == 4 and list(temps.values()) == [11.0]


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [("0.25", "m", 0.25), ("25", "cm", 0.25), ("250", "mm", 0.25), ("-1", "m", None),
     ("unknown", "m", None), ("1", "furlong", None), ("30", "m", None)],
)
def test_snow_depth_is_converted_to_metres_or_refused(value, unit, expected):
    result = ls.parse_snow_depth(value, unit)
    assert result == (pytest.approx(expected) if expected is not None else None)


# ---------------------------------------------------------------------------
# Freshness and history
# ---------------------------------------------------------------------------
def _records(first: datetime, hours: int) -> dict:
    records, _ = ls.parse_irradiance_series(
        [_entry(first + timedelta(hours=h)) for h in range(hours)]
    )
    return records


def test_a_fresh_series_passes():
    assert ls.series_freshness_problem(_records(HOUR0, 48), NOW, 6) is None


def test_one_source_cycle_of_lag_across_the_hour_boundary_is_tolerated():
    # Source still publishes from the previous hour shortly after rollover.
    assert ls.series_freshness_problem(_records(HOUR0 - timedelta(hours=1), 48), NOW, 6) is None


def test_a_series_that_stopped_updating_is_stale_even_though_it_covers_now():
    stalled = _records(HOUR0 - timedelta(hours=3), 100)
    problem = ls.series_freshness_problem(stalled, NOW, 6)
    assert problem and "stale" in problem


def test_a_series_without_the_current_hour_average_is_refused():
    records = _records(HOUR0, 48)
    records[HOUR0] = ls.HourRecord(start=HOUR0, ghi_instant=1.0, dni_instant=1.0, dhi_instant=1.0)
    assert "current hour" in ls.series_freshness_problem(records, NOW, 6)


def test_a_series_too_short_for_a_forecast_is_refused():
    problem = ls.series_freshness_problem(_records(HOUR0, 4), NOW, 6)
    assert problem and "future hours" in problem


def test_an_empty_series_is_refused():
    assert "empty" in ls.series_freshness_problem({}, NOW, 6)


def test_history_keeps_elapsed_hours_the_source_no_longer_publishes():
    keep_from = HOUR0 - timedelta(hours=24)
    cached = {HOUR0 - timedelta(hours=2): "old-2", HOUR0 - timedelta(hours=1): "old-1"}
    fresh = {HOUR0: "new0", HOUR0 + timedelta(hours=1): "new1"}
    merged = ls.merge_history(cached, fresh, NOW, keep_from)
    assert merged == {
        HOUR0 - timedelta(hours=2): "old-2",
        HOUR0 - timedelta(hours=1): "old-1",
        HOUR0: "new0",
        HOUR0 + timedelta(hours=1): "new1",
    }


def test_history_never_resurrects_a_future_hour_the_source_dropped():
    cached = {HOUR0 + timedelta(hours=5): "stale-future", HOUR0: "stale-current"}
    fresh = {HOUR0: "fresh-current"}
    merged = ls.merge_history(cached, fresh, NOW, HOUR0 - timedelta(days=1))
    assert merged == {HOUR0: "fresh-current"}


def test_history_prefers_the_fresh_value_for_a_past_hour_still_published():
    hour = HOUR0 - timedelta(hours=1)
    merged = ls.merge_history({hour: "cached"}, {hour: "fresh"}, NOW, HOUR0 - timedelta(days=1))
    assert merged[hour] == "fresh"


def test_history_is_pruned_before_keep_from():
    keep_from = HOUR0 - timedelta(hours=3)
    cached = {HOUR0 - timedelta(hours=4): "x", HOUR0 - timedelta(hours=3): "y"}
    assert list(ls.merge_history(cached, {}, NOW, keep_from)) == [HOUR0 - timedelta(hours=3)]
    # Fresh entries older than keep_from are pruned too (mutation M17).
    fresh = {HOUR0 - timedelta(hours=6): "old", HOUR0: "now"}
    assert list(ls.merge_history({}, fresh, NOW, keep_from)) == [HOUR0]


def test_hour_records_round_trip_through_storage_json():
    rec = ls.HourRecord(start=HOUR0, ghi=1.0, dni=2.0, dhi=0.5, sources=2)
    assert ls.HourRecord.from_json(rec.to_json()) == rec
    with pytest.raises(ValueError):
        ls.HourRecord.from_json({**rec.to_json(), "start": "2026-10-06T09:00:00"})


# ---------------------------------------------------------------------------
# Transposition
# ---------------------------------------------------------------------------
NOON = ls.sun_position(datetime(2026, 10, 6, 11, 14, tzinfo=UTC), LAT, LON)


def test_a_horizontal_plane_receives_exactly_ghi():
    assert ls.transpose_hay_davies(NOON, 500.0, 150.0, 0.0, 0.0) == pytest.approx(500.0)


def test_pure_isotropic_diffuse_on_a_vertical_plane():
    # GHI == DHI -> no beam, anisotropy 0: half the sky plus half the ground.
    expected = 200.0 * 0.5 + 200.0 * ls.GROUND_ALBEDO * 0.5
    assert ls.transpose_hay_davies(NOON, 200.0, 200.0, 90.0, 0.0) == pytest.approx(expected)


def test_a_south_facing_panel_beats_a_north_facing_one_at_noon():
    south = ls.transpose_hay_davies(NOON, 500.0, 100.0, 30.0, 0.0)
    north = ls.transpose_hay_davies(NOON, 500.0, 100.0, 30.0, 180.0)
    assert south > 500.0 > north


def test_open_meteo_azimuth_convention_minus_ninety_is_east():
    morning = ls.sun_position(datetime(2026, 10, 6, 7, 0, tzinfo=UTC), LAT, LON)
    east = ls.transpose_hay_davies(morning, 300.0, 80.0, 30.0, -90.0)
    west = ls.transpose_hay_davies(morning, 300.0, 80.0, 30.0, 90.0)
    assert east > west


def test_night_and_zero_irradiance_give_zero():
    night = ls.sun_position(datetime(2026, 10, 6, 0, 0, tzinfo=UTC), LAT, LON)
    assert ls.transpose_hay_davies(night, 100.0, 50.0, 30.0, 0.0) == 0.0
    assert ls.transpose_hay_davies(NOON, 0.0, 0.0, 30.0, 0.0) == 0.0


@pytest.mark.parametrize("hour", [7, 9, 11, 13, 15])
def test_trackers_never_receive_less_than_the_fixed_plane_they_generalise(hour):
    sun = ls.sun_position(datetime(2026, 10, 6, hour, 0, tzinfo=UTC), LAT, LON)
    fixed = ls.transpose_hay_davies(sun, 500.0, 100.0, 30.0, 0.0)
    dual = ls.transpose_hay_davies(sun, 500.0, 100.0, None, None)
    azimuth_tracker = ls.transpose_hay_davies(sun, 500.0, 100.0, 30.0, None)
    tilt_tracker = ls.transpose_hay_davies(sun, 500.0, 100.0, None, 0.0)
    assert dual >= fixed - 1e-6
    assert azimuth_tracker >= fixed - 1e-6
    assert tilt_tracker >= fixed - 1e-6


def test_dual_axis_plane_faces_the_sun():
    tilt, azimuth = ls.plane_orientation(NOON, None, None)
    assert ls.cos_incidence(NOON, tilt, azimuth) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Payload synthesis
# ---------------------------------------------------------------------------
def _snapshot(series, temps=None, **kwargs) -> ls.LocalSnapshot:
    records, _ = ls.parse_irradiance_series(series)
    if temps is None:
        first = min(records)
        temps = {first + timedelta(hours=h): 12.0 for h in range(len(records) + 1)}
    return ls.LocalSnapshot(hours=records, temperatures_c=temps, utc_offset_seconds=7200, **kwargs)


DAY0 = datetime(2026, 10, 6, 0, 0, tzinfo=UTC)


def _payload(series=None, tilt="0", azimuth="0", **kwargs):
    snap = _snapshot(series or fusion_series(DAY0, 48), **kwargs)
    stats = ls.SynthesisStats()
    return ls.build_open_meteo_payload(snap, LAT, LON, tilt, azimuth, stats), snap, stats


def test_payload_has_exactly_the_fields_the_library_reads():
    payload, _, _ = _payload()
    assert set(payload["minutely_15"]) == {
        "time", "temperature_2m", "global_tilted_irradiance",
        "global_tilted_irradiance_instant", "diffuse_radiation",
        "diffuse_radiation_instant", "direct_radiation",
        "direct_radiation_instant", "snow_depth",
    }
    assert set(payload["daily"]) == {"time", "sunrise", "sunset"}
    assert payload["utc_offset_seconds"] == 7200
    lengths = {len(v) for v in payload["minutely_15"].values()}
    assert lengths == {len(payload["minutely_15"]["time"])}


def test_grid_is_quarter_hourly_and_starts_at_the_first_hour():
    payload, snap, _ = _payload()
    times = payload["minutely_15"]["time"]
    assert times[0] == int(min(snap.hours).timestamp())
    assert {b - a for a, b in zip(times, times[1:])} == {900}
    assert times[-1] == int((max(snap.hours) + timedelta(hours=1)).timestamp())


def test_the_first_label_carries_no_average_because_it_would_cover_the_previous_hour():
    payload, _, _ = _payload()
    assert payload["minutely_15"]["global_tilted_irradiance"][0] is None
    assert payload["minutely_15"]["temperature_2m"][0] is not None


def test_horizontal_plane_quarter_averages_conserve_hourly_ghi_and_dhi():
    payload, snap, stats = _payload(tilt="0", azimuth="0")
    m = payload["minutely_15"]
    assert stats.kt_clamped_hours == 0
    for index, start in enumerate(sorted(snap.hours)):
        rec = snap.hours[start]
        quarters = range(1 + 4 * index, 5 + 4 * index)  # labels start+15 .. start+60
        gti = sum(m["global_tilted_irradiance"][q] for q in quarters) / 4
        dhi = sum(m["diffuse_radiation"][q] for q in quarters) / 4
        beam = sum(m["direct_radiation"][q] for q in quarters) / 4
        # Exact by construction; the only error is the 0.01 W/m2 output
        # rounding. (A looser tolerance let mutation M04 — the D-01
        # regression — escape.)
        assert gti == pytest.approx(rec.ghi, abs=0.01)
        assert dhi == pytest.approx(rec.dhi, abs=0.01)
        assert beam + dhi == pytest.approx(gti, abs=0.01)


def test_an_average_is_labelled_at_the_end_of_its_quarter():
    # Shift test: a cloudy hour must appear in labels start+15..start+60,
    # not start..start+45 (Open-Meteo's end-of-interval convention).
    cloudy = 11  # 11:00-12:00 UTC is cloudy
    payload, snap, _ = _payload(series=fusion_series(DAY0, 48, cloud={cloudy: 0.1}))
    m = payload["minutely_15"]
    times = m["time"]
    start = DAY0 + timedelta(hours=cloudy)
    by_label = dict(zip(times, m["global_tilted_irradiance"]))
    inside = [by_label[int((start + timedelta(minutes=15 * k)).timestamp())] for k in (1, 2, 3, 4)]
    before = by_label[int(start.timestamp())]
    assert max(inside) < 0.2 * before


def test_instant_values_at_hour_starts_reproduce_the_published_instants():
    payload, snap, _ = _payload()
    m = payload["minutely_15"]
    by_label = {t: i for i, t in enumerate(m["time"])}
    checked = 0
    for start, rec in snap.hours.items():
        sun = ls.sun_position(start, LAT, LON)
        if sun.elevation_deg < ls.INSTANT_ANCHOR_MIN_ELEVATION_DEG or start == min(snap.hours):
            continue
        i = by_label[int(start.timestamp())]
        ghi = m["direct_radiation_instant"][i] + m["diffuse_radiation_instant"][i]
        assert ghi == pytest.approx(rec.ghi_instant, rel=0.002, abs=0.1)
        assert m["diffuse_radiation_instant"][i] == pytest.approx(rec.dhi_instant, rel=0.002, abs=0.1)
        checked += 1
    assert checked >= 10


def test_night_quarters_are_zero_not_missing():
    payload, _, _ = _payload()
    m = payload["minutely_15"]
    midnight = m["time"].index(int(datetime(2026, 10, 6, 22, 0, tzinfo=UTC).timestamp()))
    assert m["global_tilted_irradiance"][midnight] == 0.0
    assert m["global_tilted_irradiance_instant"][midnight] == 0.0


def test_a_missing_hour_yields_missing_values_for_every_radiation_field_together():
    series = fusion_series(DAY0, 24)
    del series[10]
    payload, _, stats = _payload(series=series)
    m = payload["minutely_15"]
    gap = [int((DAY0 + timedelta(hours=10, minutes=15 * k)).timestamp()) for k in (1, 2, 3, 4)]
    fields = ("global_tilted_irradiance", "global_tilted_irradiance_instant", "diffuse_radiation",
              "diffuse_radiation_instant", "direct_radiation", "direct_radiation_instant")
    for label in gap:
        i = m["time"].index(label)
        assert all(m[f][i] is None for f in fields)
    assert stats.quarters_without_radiation == 4
    # All-or-none invariant over the whole payload (the library does
    # arithmetic on diffuse/direct whenever GTI is present).
    for i in range(len(m["time"])):
        present = {m[f][i] is not None for f in fields}
        assert len(present) == 1


def test_daily_sun_times_cover_every_local_date_in_the_grid():
    payload, _, _ = _payload()
    days = {datetime.fromtimestamp(t, CEST).date() for t in payload["minutely_15"]["time"]}
    daily = [datetime.fromtimestamp(t, CEST).date() for t in payload["daily"]["time"]]
    assert set(daily) == days
    assert all(s > r for r, s in zip(payload["daily"]["sunrise"], payload["daily"]["sunset"]))


def test_temperature_is_interpolated_and_falls_back_when_absent():
    series = fusion_series(DAY0, 12)
    temps = {DAY0: 10.0, DAY0 + timedelta(hours=1): 14.0}
    payload, _, stats = _payload(series=series, temps=temps, fallback_temperature_c=7.0)
    m = payload["minutely_15"]
    assert m["temperature_2m"][2] == pytest.approx(12.0)  # 00:30 between 10 and 14
    assert m["temperature_2m"][-1] == 7.0  # far from any forecast point
    assert stats.temperature_extrapolated > 0 and stats.temperature_fallback > 0


def test_without_any_temperature_the_quarter_is_left_for_the_library_to_skip():
    payload, _, stats = _payload(series=fusion_series(DAY0, 6), temps={})
    assert set(payload["minutely_15"]["temperature_2m"]) == {None}
    assert stats.temperature_missing == len(payload["minutely_15"]["time"])


def test_temperature_gaps_longer_than_three_hours_are_not_bridged():
    temps = {DAY0: 10.0, DAY0 + timedelta(hours=5): 20.0}
    value, _ = ls._interpolate_temperature(sorted(temps.items()), DAY0 + timedelta(hours=2, minutes=30))
    assert value is None


def test_snow_depth_is_applied_to_every_quarter():
    payload, _, _ = _payload(snow_depth_m=0.12)
    assert set(payload["minutely_15"]["snow_depth"]) == {0.12}


def test_an_implausible_hour_average_is_clamped_and_counted():
    series = fusion_series(DAY0, 12)
    series[7]["ghi"] = 1400.0  # 07 UTC: low sun, kt would be far above 1
    series[7]["dhi"] = 100.0
    _, _, stats = _payload(series=series)
    assert stats.kt_clamped_hours >= 1


@pytest.mark.parametrize(("tilt", "azimuth"), [("95", "0"), ("30", "200"), ("x", "0")])
def test_invalid_plane_geometry_is_refused(tilt, azimuth):
    with pytest.raises(ValueError):
        _payload(tilt=tilt, azimuth=azimuth)


def test_tracking_axes_marked_nan_are_accepted():
    payload, _, _ = _payload(tilt="nan", azimuth="nan")
    assert any(v for v in payload["minutely_15"]["global_tilted_irradiance"] if v)


def test_an_empty_snapshot_is_refused():
    with pytest.raises(ValueError):
        ls.build_open_meteo_payload(
            ls.LocalSnapshot(hours={}, temperatures_c={}, utc_offset_seconds=0), LAT, LON, "30", "0"
        )


def test_dni_consistency_is_small_for_a_consistent_series_and_large_otherwise():
    records, _ = ls.parse_irradiance_series(fusion_series(DAY0, 24))
    consistent = ls.series_dni_consistency(records.values(), LAT, LON)
    assert consistent is not None and consistent < 25
    broken = [
        ls.HourRecord(start=r.start, ghi=r.ghi, dni=(r.dni or 0) + 300, dhi=r.dhi) for r in records.values()
    ]
    assert ls.series_dni_consistency(broken, LAT, LON) > 250


# ---------------------------------------------------------------------------
# Day coverage (D-05)
# ---------------------------------------------------------------------------
def _hours_from(first: datetime, count: int) -> dict:
    records, _ = ls.parse_irradiance_series(fusion_series(first, count))
    return records


def test_a_fully_covered_day_is_complete_and_a_partial_last_day_is_not():
    # 2026-10-06 00:00 local (+02:00) = 2026-10-05 22:00 UTC, 60 hours.
    first = datetime(2026, 10, 5, 22, 0, tzinfo=UTC)
    complete = ls.complete_local_dates(_hours_from(first, 60), LAT, LON, 7200)
    assert complete == {date(2026, 10, 6), date(2026, 10, 7)}  # 10-08 ends 10:00 local


def test_a_missing_night_hour_does_not_make_a_day_incomplete():
    first = datetime(2026, 10, 5, 22, 0, tzinfo=UTC)
    hours = _hours_from(first, 24)
    del hours[datetime(2026, 10, 6, 0, 0, tzinfo=UTC)]  # 02:00 local
    assert date(2026, 10, 6) in ls.complete_local_dates(hours, LAT, LON, 7200)


def test_a_missing_daylight_hour_makes_the_day_incomplete():
    first = datetime(2026, 10, 5, 22, 0, tzinfo=UTC)
    hours = _hours_from(first, 24)
    hours[datetime(2026, 10, 6, 10, 0, tzinfo=UTC)] = ls.HourRecord(
        start=datetime(2026, 10, 6, 10, 0, tzinfo=UTC), ghi_instant=1.0, dni_instant=1.0, dhi_instant=1.0
    )
    assert ls.complete_local_dates(hours, LAT, LON, 7200) == set()


def test_the_sunrise_hour_counts_as_daylight():
    first = datetime(2026, 10, 5, 22, 0, tzinfo=UTC)
    hours = _hours_from(first, 24)
    del hours[datetime(2026, 10, 6, 5, 0, tzinfo=UTC)]  # 07:00-08:00 local, sunrise 07:30
    assert ls.complete_local_dates(hours, LAT, LON, 7200) == set()


def test_no_hours_means_no_complete_dates():
    assert ls.complete_local_dates({}, LAT, LON, 0) == set()
