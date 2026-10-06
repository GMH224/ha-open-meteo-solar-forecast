"""Hybrid source: local days, Open-Meteo for the rest (0.1.33.3).

Pure module: no Home Assistant import, no I/O, no clock read. Exercised by
``tests/test_hybrid.py``. Design record: ``OMSF_v0_1_33_2_Architecture_ICS.md``
§5.9 (hybrid section added in 0.1.33.3).

Rule (owner decision, 6 Oct 2026): sources are joined **by whole local days**.

* A day that the local data covers completely (every daylight hour, see
  ``local_source.complete_local_dates``) comes entirely from the local
  estimate.
* Every other day comes entirely from Open-Meteo, if Open-Meteo covers it
  completely (≥ 23 hourly values in the local day; 23 allows a DST day).
* A day neither source covers completely is absent (sensor "unknown").

Because PV output is zero at local midnight, joining at day boundaries
introduces no visible step in the power curve, and no day ever mixes two
sources.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, tzinfo
from typing import Any

from open_meteo_solar_forecast.models import Estimate

DAY_SOURCE_LOCAL = "local"
DAY_SOURCE_OPEN_METEO = "open_meteo"

MIN_HOURS_FOR_COMPLETE_DAY = 23


def _day(moment: datetime, tz: tzinfo) -> date:
    return moment.astimezone(tz).date()


def _select(
    data: Mapping[datetime, Any], tz: tzinfo, days: set[date]
) -> dict[datetime, Any]:
    """Entries whose local day is in ``days``, re-expressed in ``tz``."""
    return {
        moment.astimezone(tz): value
        for moment, value in data.items()
        if _day(moment, tz) in days
    }


def complete_open_meteo_days(estimate: Estimate, tz: tzinfo) -> set[date]:
    """Local days for which an Open-Meteo estimate has a full set of hours."""
    hours: dict[date, set[int]] = {}
    for moment in estimate.wh_period:
        local = moment.astimezone(tz)
        hours.setdefault(local.date(), set()).add(local.hour)
    return {day for day, seen in hours.items() if len(seen) >= MIN_HOURS_FOR_COMPLETE_DAY}


def merge_hybrid(
    local: Estimate,
    open_meteo: Estimate | None,
    local_days: set[date],
) -> tuple[Estimate, dict[date, str]]:
    """Join a local and an Open-Meteo estimate by whole local days.

    ``local_days`` are the days the local data covers completely. The result
    uses the local estimate's timezone throughout. Returns the merged
    estimate and the source of every day it contains.
    """
    tz = local.api_timezone
    sources: dict[date, str] = {day: DAY_SOURCE_LOCAL for day in local_days}
    om_days: set[date] = set()
    if open_meteo is not None:
        om_days = complete_open_meteo_days(open_meteo, tz) - set(local_days)
        sources.update({day: DAY_SOURCE_OPEN_METEO for day in om_days})

    def joined(local_data: Mapping[datetime, Any], om_data: Mapping[datetime, Any]) -> dict:
        merged = _select(local_data, tz, set(local_days))
        if open_meteo is not None:
            merged.update(_select(om_data, tz, om_days))
        return dict(sorted(merged.items()))

    watts = joined(local.watts, open_meteo.watts if open_meteo else {})
    wh_period = joined(local.wh_period, open_meteo.wh_period if open_meteo else {})
    wh_period_15m = joined(
        local.wh_period_15m, open_meteo.wh_period_15m if open_meteo else {}
    )

    # Daily totals recomputed from the selected hours, exactly as the
    # library computes them (sum of hourly average power = Wh). This stays
    # correct when the two sources use different UTC offsets.
    wh_days: dict[date, float] = {}
    for moment, value in wh_period.items():
        day = moment.date()
        wh_days[day] = wh_days.get(day, 0) + value

    return (
        Estimate(
            watts=watts,
            wh_period=wh_period,
            wh_days=dict(sorted(wh_days.items())),
            wh_period_15m=wh_period_15m,
            api_timezone=tz,
        ),
        dict(sorted(sources.items())),
    )
