"""Unit tests for ``hybrid.py`` (0.1.33.3) — the day-level join rule.

Pure: no Home Assistant. Local and Open-Meteo estimates carry different
constant values (111 vs 222) so every assertion can tell the sources apart.
"""

from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from open_meteo_solar_forecast.models import Estimate

from custom_components.open_meteo_solar_forecast import hybrid
from custom_components.open_meteo_solar_forecast.hybrid import (
    DAY_SOURCE_LOCAL,
    DAY_SOURCE_OPEN_METEO,
    complete_open_meteo_days,
    merge_hybrid,
)

CEST = timezone(timedelta(hours=2))
UTC = timezone.utc
D0 = date(2026, 10, 6)


def _estimate(tz, first_day: date, days: int, value: int, hours=range(24)) -> Estimate:
    watts, wh_period, wh_15m, wh_days = {}, {}, {}, {}
    for d in range(days):
        day = first_day + timedelta(days=d)
        for h in hours:
            start = datetime(day.year, day.month, day.day, h, tzinfo=tz)
            wh_period[start] = value
            wh_days[day] = wh_days.get(day, 0) + value
            for q in range(4):
                watts[start + timedelta(minutes=15 * q)] = value
                wh_15m[start + timedelta(minutes=15 * q)] = value / 4
    return Estimate(watts=watts, wh_period=wh_period, wh_days=wh_days,
                    wh_period_15m=wh_15m, api_timezone=tz)


def _by_day(data, tz):
    out = {}
    for moment, value in data.items():
        out.setdefault(moment.astimezone(tz).date(), set()).add(value)
    return out


def test_the_module_stays_pure():
    tree = ast.parse(Path(hybrid.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "collections", "datetime", "typing", "open_meteo_solar_forecast"}


def test_complete_local_days_come_from_local_and_all_other_days_from_open_meteo():
    local = _estimate(CEST, D0, 3, 111)
    om = _estimate(CEST, D0, 8, 222)
    local_days = {D0 + timedelta(days=1)}  # only tomorrow fully local
    merged, sources = merge_hybrid(local, om, local_days)
    assert sources[D0 + timedelta(days=1)] == DAY_SOURCE_LOCAL
    for d in (0, 2, 3, 4, 5, 6, 7):
        assert sources[D0 + timedelta(days=d)] == DAY_SOURCE_OPEN_METEO
    assert merged.wh_days[D0 + timedelta(days=1)] == 24 * 111
    assert merged.wh_days[D0 + timedelta(days=5)] == 24 * 222


def test_no_day_ever_mixes_two_sources():
    local = _estimate(CEST, D0, 3, 111)
    om = _estimate(CEST, D0, 8, 222)
    merged, sources = merge_hybrid(local, om, {D0, D0 + timedelta(days=1)})
    for data, scale in ((merged.watts, 1), (merged.wh_period, 1), (merged.wh_period_15m, 4)):
        for day, values in _by_day(data, CEST).items():
            expected = 111 if sources[day] == DAY_SOURCE_LOCAL else 222
            assert values == {expected / scale}, day


def test_a_local_day_wins_even_where_open_meteo_has_it():
    local = _estimate(CEST, D0, 2, 111)
    om = _estimate(CEST, D0, 2, 222)
    merged, sources = merge_hybrid(local, om, {D0})
    assert sources[D0] == DAY_SOURCE_LOCAL and merged.wh_days[D0] == 24 * 111


def test_partial_local_data_outside_complete_days_is_dropped():
    local = _estimate(CEST, D0, 3, 111)  # local has data for 3 days ...
    merged, sources = merge_hybrid(local, None, {D0 + timedelta(days=1)})  # ... one complete
    assert set(sources) == {D0 + timedelta(days=1)}
    assert set(merged.wh_days) == {D0 + timedelta(days=1)}
    assert {m.astimezone(CEST).date() for m in merged.watts} == {D0 + timedelta(days=1)}


def test_without_open_meteo_only_the_local_days_remain():
    local = _estimate(CEST, D0, 2, 111)
    merged, sources = merge_hybrid(local, None, {D0})
    assert sources == {D0: DAY_SOURCE_LOCAL}
    assert set(merged.wh_days) == {D0}


def test_an_incomplete_open_meteo_day_is_not_used():
    local = _estimate(CEST, D0, 1, 111)
    om = _estimate(CEST, D0 + timedelta(days=1), 1, 222, hours=range(6, 24))  # 18 hours
    merged, sources = merge_hybrid(local, om, {D0})
    assert D0 + timedelta(days=1) not in sources
    assert set(merged.wh_days) == {D0}


def test_a_23_hour_dst_day_counts_as_complete():
    om = _estimate(CEST, D0, 1, 222, hours=[h for h in range(24) if h != 2])
    assert complete_open_meteo_days(om, CEST) == {D0}


def test_open_meteo_in_another_offset_is_regrouped_into_local_days():
    """Open-Meteo's timezone=auto may differ from Home Assistant's. Days and
    daily totals must follow the local estimate's timezone."""
    local = _estimate(CEST, D0, 1, 111)
    om = _estimate(UTC, D0 - timedelta(days=1), 10, 222)
    merged, sources = merge_hybrid(local, om, {D0})
    assert merged.api_timezone == CEST
    day = D0 + timedelta(days=3)
    assert sources[day] == DAY_SOURCE_OPEN_METEO
    assert merged.wh_days[day] == 24 * 222
    assert all(m.utcoffset() == timedelta(hours=2) for m in merged.wh_period)
    # The first UTC day is incomplete in CEST and must not appear.
    assert D0 - timedelta(days=1) not in sources
    # Every Open-Meteo day, including the one right after the local day
    # (where grouping in the source's own offset would lose two hours),
    # has exactly 24 Open-Meteo hours; the local day has none of them
    # (mutation M40 escaped while only an interior day was checked).
    for day, src in sources.items():
        hours = [v for m, v in merged.wh_period.items() if m.date() == day]
        expected = 111 if src == DAY_SOURCE_LOCAL else 222
        assert hours == [expected] * 24, day


def test_daily_totals_equal_the_sum_of_the_selected_hours():
    local = _estimate(CEST, D0, 2, 111)
    om = _estimate(CEST, D0, 5, 222)
    merged, _ = merge_hybrid(local, om, {D0})
    for day, total in merged.wh_days.items():
        assert total == sum(v for m, v in merged.wh_period.items() if m.date() == day)


def test_merged_series_are_sorted_in_time():
    local = _estimate(CEST, D0 + timedelta(days=1), 1, 111)
    om = _estimate(CEST, D0, 4, 222)
    merged, sources = merge_hybrid(local, om, {D0 + timedelta(days=1)})
    for data in (merged.watts, merged.wh_period, merged.wh_period_15m):
        keys = list(data)
        assert keys == sorted(keys)
    assert list(sources) == sorted(sources)


def test_nothing_from_either_source_is_an_empty_forecast():
    merged, sources = merge_hybrid(_estimate(CEST, D0, 1, 111), None, set())
    assert sources == {} and merged.wh_days == {} and merged.watts == {}
