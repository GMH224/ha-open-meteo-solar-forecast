"""Validation of the local engine against an independent reference (pvlib).

pvlib is the de-facto reference implementation for PV modelling (Sandia /
NREL lineage). It is a TEST-ONLY dependency; these tests are skipped when it
is not installed. The thresholds below are the ones recorded in the release
audit, so loosening one is an audit-visible change.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

pvlib = pytest.importorskip("pvlib")
pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

from custom_components.open_meteo_solar_forecast import local_source as ls  # noqa: E402
from custom_components.open_meteo_solar_forecast.local_provider import (  # noqa: E402
    LocalOpenMeteoSolarForecast,
)

LAT, LON = 47.5536, 8.8986  # Frauenfeld
UTC = timezone.utc


def test_solar_position_matches_nrel_spa_within_two_hundredths_of_a_degree_all_year():
    worst_zenith = worst_azimuth = 0.0
    for step in range(0, 365 * 24, 7):
        moment = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=step, minutes=13)
        mine = ls.sun_position(moment, LAT, LON)
        ref = pvlib.solarposition.get_solarposition(
            pd.DatetimeIndex([moment]), LAT, LON, method="nrel_numpy"
        )
        zenith = float(ref["zenith"].iloc[0])
        worst_zenith = max(worst_zenith, abs(math.degrees(mine.zenith_rad) - zenith))
        if zenith < 85:
            delta = (math.degrees(mine.azimuth_rad) - float(ref["azimuth"].iloc[0]) + 180) % 360 - 180
            worst_azimuth = max(worst_azimuth, abs(delta))
    assert worst_zenith < 0.02
    assert worst_azimuth < 0.03


@pytest.mark.parametrize("day", [date(2026, 3, 20), date(2026, 6, 21), date(2026, 10, 6), date(2026, 12, 21)])
def test_sunrise_and_sunset_match_spa_within_one_minute(day):
    tz = timezone(timedelta(hours=1))
    rise, set_ = ls.sunrise_sunset(day, LAT, LON, tz)
    ref = pvlib.location.Location(LAT, LON).get_sun_rise_set_transit(
        pd.DatetimeIndex([pd.Timestamp(day.isoformat() + " 12:00", tz="Etc/GMT-1")]), method="spa"
    )
    assert abs((rise - ref["sunrise"].iloc[0].to_pydatetime()).total_seconds()) < 60
    assert abs((set_ - ref["sunset"].iloc[0].to_pydatetime()).total_seconds()) < 60


@pytest.mark.parametrize(
    ("tilt", "azimuth_om"),
    [(0, 0), (30, 0), (30, -90), (30, 90), (45, 180), (60, 45), (90, 0)],
)
@pytest.mark.parametrize("hour", [7, 9, 11, 13, 15])
def test_hay_davies_transposition_matches_pvlib(tilt, azimuth_om, hour):
    moment = datetime(2026, 10, 6, hour, 0, tzinfo=UTC)
    sun = ls.sun_position(moment, LAT, LON)
    # Physically consistent inputs (DNI below E0 at every sun height), so
    # the engine's anisotropy clamp A <= 1 is not exercised here.
    ghi = 0.7 * sun.extraterrestrial_normal * sun.cos_zenith
    dhi = 0.25 * ghi
    mine = ls.transpose_hay_davies(sun, ghi, dhi, tilt, azimuth_om)
    dni = (ghi - dhi) / sun.cos_zenith
    ref = pvlib.irradiance.get_total_irradiance(
        tilt, (azimuth_om + 180) % 360,
        math.degrees(sun.zenith_rad), math.degrees(sun.azimuth_rad),
        dni, ghi, dhi, dni_extra=sun.extraterrestrial_normal,
        model="haydavies", albedo=ls.GROUND_ALBEDO,
    )
    assert mine == pytest.approx(float(ref["poa_global"]), rel=1e-6, abs=1e-6)


def test_anisotropy_is_clamped_where_the_input_implies_dni_above_e0():
    """Deliberate deviation from pvlib (which does not clamp): A = DNI/E0
    above 1 is unphysical and would weight circumsolar diffuse above the
    whole diffuse component."""
    sun = ls.sun_position(datetime(2026, 10, 6, 7, 0, tzinfo=UTC), LAT, LON)
    ghi, dhi = 480.0, 140.0  # implies DNI ~ 2000 W/m2 at ~10° elevation
    assert (ghi - dhi) / sun.cos_zenith > sun.extraterrestrial_normal
    mine = ls.transpose_hay_davies(sun, ghi, dhi, 30, 0)
    tilt = math.radians(30)
    beam = (ghi - dhi) / sun.cos_zenith * ls.cos_incidence(sun, tilt, math.radians(180))
    circumsolar_max = dhi * ls.cos_incidence(sun, tilt, math.radians(180)) / sun.cos_zenith
    ground = ghi * ls.GROUND_ALBEDO * (1 - math.cos(tilt)) / 2
    assert mine == pytest.approx(beam + circumsolar_max + ground)


class _ExecutorHass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _clear_sky_minutes(days: int, cloud_day: int | None):
    loc = pvlib.location.Location(LAT, LON, altitude=420)
    start = pd.Timestamp("2026-10-06 00:00", tz="UTC")
    idx = pd.date_range(start, start + pd.Timedelta(days=days), freq="1min", inclusive="left")
    clear = loc.get_clearsky(idx, model="ineichen")
    pos = loc.get_solarposition(idx)
    factor = np.ones(len(idx))
    if cloud_day is not None:
        mask = (idx >= start + pd.Timedelta(days=cloud_day)) & (idx < start + pd.Timedelta(days=cloud_day + 1))
        factor[mask] = 0.5
    ghi = clear["ghi"] * factor
    dni = clear["dni"] * factor**2
    cos_z = np.cos(np.radians(pos["zenith"])).clip(lower=0)
    dhi = ghi - dni * cos_z
    return start, idx, pos, ghi, dni, dhi


@pytest.mark.parametrize(
    ("tilt", "azimuth_compass", "tolerance"),
    [(30, 180, 0.01), (30, 90, 0.015), (45, 270, 0.025), (10, 135, 0.01)],
)
async def test_daily_energy_through_the_library_matches_a_one_minute_reference(tilt, azimuth_compass, tolerance):
    """End-to-end: Fusion-shaped hourly data -> local engine -> upstream
    library, against a 1-minute pvlib computation with the library's own
    power model. Covers hour labelling, downscaling, transposition and the
    adapter together. Tolerances are the audited downscaling error bounds.
    """
    start, idx, pos, ghi, dni, dhi = _clear_sky_minutes(3, cloud_day=1)
    series = []
    for hour in pd.date_range(start, idx[-1], freq="1h"):
        window = slice(hour, hour + pd.Timedelta(minutes=59))
        series.append({
            "period_start": hour.isoformat(), "period_end": (hour + pd.Timedelta(hours=1)).isoformat(),
            "ghi": float(ghi[window].mean()), "dni": float(dni[window].mean()), "dhi": float(dhi[window].mean()),
            "ghi_instant": float(ghi[hour]), "dni_instant": float(dni[hour]), "dhi_instant": float(dhi[hour]),
        })
    records, report = ls.parse_irradiance_series(series)
    assert report.rejected == []
    temps = {t.to_pydatetime(): 12.0 for t in pd.date_range(start, start + pd.Timedelta(days=3), freq="1h")}
    snapshot = ls.LocalSnapshot(hours=records, temperatures_c=temps, utc_offset_seconds=7200)

    forecast = LocalOpenMeteoSolarForecast(
        latitude=LAT, longitude=LON, azimuth=azimuth_compass - 180, declination=tilt, dc_kwp=10.0
    )
    forecast.prepare(_ExecutorHass(), snapshot)
    estimate = await forecast.estimate()

    poa = pvlib.irradiance.get_total_irradiance(
        tilt, azimuth_compass, pos["zenith"], pos["azimuth"],
        ((ghi - dhi) / np.cos(np.radians(pos["zenith"])).clip(lower=math.cos(math.radians(89)))),
        ghi, dhi, dni_extra=pvlib.irradiance.get_extra_radiation(idx),
        model="haydavies", albedo=ls.GROUND_ALBEDO,
    )["poa_global"].fillna(0)
    cell = 12.0 + poa * 0.0342
    power = (10000 * poa / 1000 * (1 - 0.004 * (cell - 25))).clip(lower=0)
    local_dates = idx.tz_convert("Etc/GMT-2").date
    compared = 0
    for day, wh in estimate.wh_days.items():
        reference = float(power[local_dates == day].sum() / 60)
        if reference < 1000:
            continue
        assert wh == pytest.approx(reference, rel=tolerance), day
        compared += 1
    assert compared == 3
