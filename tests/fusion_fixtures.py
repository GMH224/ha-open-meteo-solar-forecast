"""Builders for SwissWeather Fusion v0.3.3-shaped test data.

The shape follows Fusion's ``ModelABlendCoordinator._compute_solar_forecast``
and ``SolarIrradianceSensor.extra_state_attributes`` (v0.3.3) exactly:

* one entry per hour, starting at the source's current UTC hour,
* ``period_start`` / ``period_end`` as ``datetime.isoformat()`` in UTC,
* ``ghi``/``dni``/``dhi`` rounded to 0.1 W/m², plus ``sources``,
* ``ghi_instant``/``dni_instant``/``dhi_instant`` plus ``instant_sources``,
* either triple may be absent for an hour.

Radiation values come from a simple clear-sky shape (0.75 * E0 * cos Z,
20 % diffuse) scaled by a per-hour cloud factor. That is enough for
integration tests; physical validation against an independent reference is
in ``test_local_physics_validation.py`` (pvlib).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.open_meteo_solar_forecast.local_source import sun_position

LAT = 47.5536  # Frauenfeld
LON = 8.8986
UTC = timezone.utc


def _triple(moment_from: datetime, minutes: int, factor: float) -> tuple[float, float, float]:
    ghi_sum = dni_sum = dhi_sum = 0.0
    for m in range(minutes):
        sun = sun_position(moment_from + timedelta(minutes=m + 0.5), LAT, LON)
        cos_z = max(sun.cos_zenith, 0.0)
        ghi = 0.75 * sun.extraterrestrial_normal * cos_z * factor
        dhi = 0.2 * ghi
        dni = (ghi - dhi) / cos_z if cos_z > 0.02 else 0.0
        ghi_sum, dni_sum, dhi_sum = ghi_sum + ghi, dni_sum + dni, dhi_sum + dhi
    return ghi_sum / minutes, dni_sum / minutes, dhi_sum / minutes


def _instant(moment: datetime, factor: float) -> tuple[float, float, float]:
    sun = sun_position(moment, LAT, LON)
    cos_z = max(sun.cos_zenith, 0.0)
    ghi = 0.75 * sun.extraterrestrial_normal * cos_z * factor
    dhi = 0.2 * ghi
    dni = (ghi - dhi) / cos_z if cos_z > 0.02 else 0.0
    return ghi, dni, dhi


def fusion_series(
    first_hour: datetime,
    hours: int = 120,
    cloud: dict[int, float] | None = None,
    sources: int = 3,
) -> list[dict]:
    """Fusion ``hourly_forecast`` attribute starting at ``first_hour``."""
    cloud = cloud or {}
    series = []
    for offset in range(hours):
        start = first_hour + timedelta(hours=offset)
        factor = cloud.get(offset, 1.0)
        ghi, dni, dhi = _triple(start, 60, factor)
        gi, di, hi = _instant(start, factor)
        series.append(
            {
                "period_start": start.isoformat(),
                "period_end": (start + timedelta(hours=1)).isoformat(),
                "ghi": round(ghi, 1),
                "dni": round(dni, 1),
                "dhi": round(dhi, 1),
                "sources": sources if offset < 33 else 1,
                "ghi_instant": round(gi, 1),
                "dni_instant": round(di, 1),
                "dhi_instant": round(hi, 1),
                "instant_sources": sources if offset < 33 else 1,
            }
        )
    return series


def weather_hourly(first_hour: datetime, hours: int = 168, temp_c: float = 12.0) -> list[dict]:
    """HA ``weather.get_forecasts`` hourly list as Fusion publishes it."""
    return [
        {
            "datetime": (first_hour + timedelta(hours=h)).isoformat(),
            "temperature": temp_c,
            "condition": "sunny",
        }
        for h in range(hours)
    ]
