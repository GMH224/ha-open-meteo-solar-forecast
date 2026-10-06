"""Local weather source: build an Open-Meteo-shaped payload from HA data.

Added in 0.1.33.2. See ``OMSF_v0_1_33_2_Architecture_ICS.md`` for the full design record.

This module is deliberately **pure**: no Home Assistant imports, no I/O, no
clock reads (``now`` is always passed in). Everything here is exercised by
``tests/test_local_source.py`` without a running Home Assistant.

What it does
------------
The upstream ``open_meteo_solar_forecast`` library fetches 15-minute
irradiance and temperature from the Open-Meteo API and turns them into PV
power. In "local" mode the integration substitutes that API response with
one synthesised here from:

* a location-level hourly radiation series (GHI / DNI / DHI hour averages
  over ``[period_start, period_end)`` plus instantaneous values at
  ``period_start``), as published by SwissWeather Fusion >= 0.3.3, and
* an hourly air-temperature forecast from a ``weather.*`` entity.

The library's PV physics (cell temperature, horizon / partial shading, snow,
damping, inverter clamping) then run unchanged, so both sources produce
directly comparable results.

The synthesis has three steps:

1. **Validation** of each hourly record (``parse_irradiance_series``).
2. **Temporal downscaling** hour -> 15 min. Hour averages are spread with a
   constant clearness index ``kt`` and diffuse fraction ``kd`` per hour,
   weighted by extraterrestrial horizontal irradiance. This conserves the
   hourly GHI and DHI energy exactly (unless a value had to be clamped) and
   gives a physically shaped curve around sunrise / sunset instead of
   linear-interpolation artefacts. Instantaneous values interpolate
   ``kt``/``kd`` linearly between anchors at each hour start.
3. **Transposition** of horizontal GHI/DHI onto each array plane with the
   Hay-Davies anisotropic sky model plus isotropic ground reflection.

The output reproduces Open-Meteo's ``minutely_15`` conventions exactly:
an *average* value labelled ``t`` covers ``[t - 15 min, t)``; an *instant*
value labelled ``t`` is the value at ``t``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SOLAR_CONSTANT_W_M2 = 1361.0
"""Total solar irradiance at 1 AU (Kopp & Lean 2011)."""

QUARTER = timedelta(minutes=15)
HOUR = timedelta(hours=1)

IRRADIANCE_MAX_W_M2 = 1500.0
"""Plausibility ceiling for any irradiance component. Ground-level GHI can
briefly exceed the extraterrestrial value under cloud enhancement, but never
1500 W/m²."""

DIFFUSE_TOLERANCE_W_M2 = 5.0
"""Allowed DHI > GHI excess (rounding in the source) before a triple is
rejected as inconsistent."""

CLEARNESS_INDEX_MAX = 1.0
"""Upper clamp for the hourly clearness index kt = GHI / (E0·cosZ).

Values above 1 are physically impossible for hour averages; they occur only
in the sunrise/sunset hour when the model's horizon geometry differs
slightly from ours. Clamping trades exact energy conservation in that one
low-energy hour for robustness. Every clamp is counted in the diagnostics."""

INSTANT_ANCHOR_MIN_ELEVATION_DEG = 5.0
"""Below this sun elevation an instantaneous kt is numerically unstable
(division by a tiny E0·cosZ), so the hour-average kt is used as anchor."""

GROUND_ALBEDO = 0.2
"""Ground reflectance for the reflected-irradiance term (typical grass /
mixed surroundings). Snow-covered ground would be ~0.6-0.8; deliberately not
modelled (see docs, limitation L-4)."""

COS_ZENITH_FLOOR = math.cos(math.radians(89.0))
"""Floor for cos(zenith) in ratios, avoiding division blow-up at the
horizon."""

SUB_SAMPLES_PER_QUARTER = 5
"""Sub-samples (3-minute midpoints) used to integrate the sun geometry over
each 15-minute interval."""

TEMPERATURE_MIN_C = -60.0
TEMPERATURE_MAX_C = 60.0
TEMPERATURE_EXTRAPOLATE_LIMIT = timedelta(minutes=90)

SNOW_DEPTH_MAX_M = 20.0

# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HourRecord:
    """One validated hour of location-level radiation.

    ``start`` is the UTC start of the hour. Average triple covers
    ``[start, start + 1 h)``; instant triple is the value at ``start``.
    Either triple may be ``None`` (absent or rejected).
    """

    start: datetime
    ghi: float | None = None
    dni: float | None = None
    dhi: float | None = None
    ghi_instant: float | None = None
    dni_instant: float | None = None
    dhi_instant: float | None = None
    sources: int | None = None

    @property
    def has_average(self) -> bool:
        return self.ghi is not None and self.dhi is not None

    @property
    def has_instant(self) -> bool:
        return self.ghi_instant is not None and self.dhi_instant is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "ghi": self.ghi,
            "dni": self.dni,
            "dhi": self.dhi,
            "ghi_instant": self.ghi_instant,
            "dni_instant": self.dni_instant,
            "dhi_instant": self.dhi_instant,
            "sources": self.sources,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> HourRecord:
        start = datetime.fromisoformat(data["start"])
        if start.tzinfo is None:
            raise ValueError("naive timestamp in stored hour record")
        return cls(
            start=start.astimezone(timezone.utc),
            ghi=data.get("ghi"),
            dni=data.get("dni"),
            dhi=data.get("dhi"),
            ghi_instant=data.get("ghi_instant"),
            dni_instant=data.get("dni_instant"),
            dhi_instant=data.get("dhi_instant"),
            sources=data.get("sources"),
        )


@dataclass
class ParseReport:
    """Outcome of validating a raw series. Fed into diagnostics."""

    received: int = 0
    accepted_average: int = 0
    accepted_instant: int = 0
    rejected: list[str] = field(default_factory=list)

    def reject(self, index: int, reason: str) -> None:
        # Bounded so a pathological payload cannot bloat diagnostics.
        if len(self.rejected) < 50:
            self.rejected.append(f"[{index}] {reason}")


@dataclass
class SynthesisStats:
    """Counters describing one payload synthesis (per array)."""

    quarters: int = 0
    quarters_without_radiation: int = 0
    kt_clamped_hours: int = 0
    temperature_extrapolated: int = 0
    temperature_fallback: int = 0
    temperature_missing: int = 0


@dataclass(frozen=True)
class LocalSnapshot:
    """Everything needed to synthesise payloads for one refresh.

    Built once per refresh by the HA layer and then used for every array,
    so all arrays see exactly the same input data.
    """

    hours: Mapping[datetime, HourRecord]
    temperatures_c: Mapping[datetime, float]
    utc_offset_seconds: int
    snow_depth_m: float = 0.0
    fallback_temperature_c: float | None = None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def _to_float(value: Any) -> float | None:
    """Strictly convert to a finite float; bool is not a number here."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def parse_utc(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp; only timezone-aware values accepted."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Solar geometry (NOAA / Meeus, accurate to ~0.01° for 1950-2050)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SunPosition:
    zenith_rad: float
    azimuth_rad: float  # compass: 0 = north, pi/2 = east (clockwise)
    cos_zenith: float
    extraterrestrial_normal: float  # W/m², E0 at this day of year

    @property
    def elevation_deg(self) -> float:
        return 90.0 - math.degrees(self.zenith_rad)


def _julian_century(moment: datetime) -> float:
    timestamp = moment.timestamp()
    julian_day = timestamp / 86400.0 + 2440587.5
    return (julian_day - 2451545.0) / 36525.0


def _solar_declination_and_eot(t: float) -> tuple[float, float]:
    """Return (declination [rad], equation of time [minutes])."""
    geom_mean_long = math.radians((280.46646 + t * (36000.76983 + t * 0.0003032)) % 360)
    geom_mean_anom = math.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))
    eccent = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    eq_center = (
        math.sin(geom_mean_anom) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * geom_mean_anom) * (0.019993 - 0.000101 * t)
        + math.sin(3 * geom_mean_anom) * 0.000289
    )
    true_long = math.degrees(geom_mean_long) + eq_center
    omega = math.radians(125.04 - 1934.136 * t)
    apparent_long = math.radians(true_long - 0.00569 - 0.00478 * math.sin(omega))
    mean_obliq = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    obliq = math.radians(mean_obliq + 0.00256 * math.cos(omega))
    declination = math.asin(math.sin(obliq) * math.sin(apparent_long))
    var_y = math.tan(obliq / 2) ** 2
    eot = 4 * math.degrees(
        var_y * math.sin(2 * geom_mean_long)
        - 2 * eccent * math.sin(geom_mean_anom)
        + 4 * eccent * var_y * math.sin(geom_mean_anom) * math.cos(2 * geom_mean_long)
        - 0.5 * var_y * var_y * math.sin(4 * geom_mean_long)
        - 1.25 * eccent * eccent * math.sin(2 * geom_mean_anom)
    )
    return declination, eot


def extraterrestrial_normal(moment: datetime) -> float:
    """Extraterrestrial normal irradiance (Spencer-style eccentricity)."""
    day_of_year = moment.timetuple().tm_yday
    return SOLAR_CONSTANT_W_M2 * (1 + 0.033 * math.cos(2 * math.pi * day_of_year / 365.0))


def sun_position(moment: datetime, latitude: float, longitude: float) -> SunPosition:
    """Geometric solar position (no refraction) at a UTC moment."""
    moment = moment.astimezone(timezone.utc)
    t = _julian_century(moment)
    declination, eot = _solar_declination_and_eot(t)
    minutes = moment.hour * 60 + moment.minute + moment.second / 60 + moment.microsecond / 6e7
    true_solar_time = (minutes + eot + 4 * longitude) % 1440
    hour_angle = math.radians(true_solar_time / 4 - 180)
    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(
        declination
    ) * math.cos(hour_angle)
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    sin_zenith = math.sin(zenith)
    if sin_zenith < 1e-9 or abs(math.cos(lat)) < 1e-9:
        azimuth = 0.0
    else:
        cos_az = (math.sin(lat) * cos_zenith - math.sin(declination)) / (
            math.cos(lat) * sin_zenith
        )
        cos_az = max(-1.0, min(1.0, cos_az))
        az = math.degrees(math.acos(cos_az))
        azimuth = (az + 180) % 360 if hour_angle > 0 else (540 - az) % 360
        azimuth = math.radians(azimuth)
    return SunPosition(
        zenith_rad=zenith,
        azimuth_rad=azimuth,
        cos_zenith=cos_zenith,
        extraterrestrial_normal=extraterrestrial_normal(moment),
    )


def _elevation_deg(moment: datetime, latitude: float, longitude: float) -> float:
    return sun_position(moment, latitude, longitude).elevation_deg


def sunrise_sunset(
    day: date, latitude: float, longitude: float, tz: timezone
) -> tuple[datetime, datetime]:
    """Sunrise and sunset (sun centre at -0.833°) on a local calendar day.

    Found by bisection around the elevation maximum, which is robust at any
    latitude. Polar night returns (noon, noon + 1 s); polar day returns the
    whole day. Both keep ``sunset > sunrise`` so the library's damping
    calculation never divides by zero.
    """
    threshold = -0.833
    day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1) - timedelta(seconds=1)

    # Locate the elevation maximum (solar noon) at 10-minute resolution.
    best = day_start
    best_elev = -91.0
    probe = day_start
    while probe <= day_end:
        elev = _elevation_deg(probe, latitude, longitude)
        if elev > best_elev:
            best, best_elev = probe, elev
        probe += timedelta(minutes=10)

    if best_elev < threshold:
        return best, best + timedelta(seconds=1)

    def crossing(lo: datetime, hi: datetime, rising: bool) -> datetime | None:
        lo_elev = _elevation_deg(lo, latitude, longitude)
        if (lo_elev >= threshold) == rising:
            return None  # no crossing inside the day
        for _ in range(40):
            mid = lo + (hi - lo) / 2
            above = _elevation_deg(mid, latitude, longitude) >= threshold
            if above == rising:
                hi = mid
            else:
                lo = mid
            if hi - lo < timedelta(seconds=1):
                break
        return hi if rising else lo

    sunrise = crossing(day_start, best, rising=True) or day_start
    sunset = crossing(best, day_end, rising=False) or day_end
    if sunset <= sunrise:
        sunset = sunrise + timedelta(seconds=1)
    return sunrise, sunset


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------


def _valid_triple(
    ghi: float | None, dni: float | None, dhi: float | None
) -> tuple[bool, str | None]:
    values = (ghi, dni, dhi)
    if all(v is None for v in values):
        return False, None
    if any(v is None for v in values):
        return False, "incomplete GHI/DNI/DHI triple"
    for name, value in zip(("ghi", "dni", "dhi"), values, strict=True):
        if value < 0 or value > IRRADIANCE_MAX_W_M2:
            return False, f"{name}={value} outside [0, {IRRADIANCE_MAX_W_M2}]"
    if dhi > ghi + DIFFUSE_TOLERANCE_W_M2:
        return False, f"dhi={dhi} exceeds ghi={ghi}"
    return True, None


def parse_irradiance_series(
    raw: Any,
) -> tuple[dict[datetime, HourRecord], ParseReport]:
    """Validate a Fusion ``hourly_forecast`` attribute.

    Never raises on bad content: invalid entries or triples are dropped and
    reported. A non-list input yields an empty result.
    """
    report = ParseReport()
    records: dict[datetime, HourRecord] = {}
    if not isinstance(raw, list):
        report.reject(-1, f"series is {type(raw).__name__}, expected list")
        return records, report

    for index, item in enumerate(raw):
        report.received += 1
        if not isinstance(item, Mapping):
            report.reject(index, "entry is not a mapping")
            continue
        start = parse_utc(item.get("period_start"))
        if start is None:
            report.reject(index, "missing or non-timezone-aware period_start")
            continue
        if start != floor_hour(start):
            report.reject(index, "period_start not on a full hour")
            continue
        end_raw = item.get("period_end")
        if end_raw is not None:
            end = parse_utc(end_raw)
            if end is None or end - start != HOUR:
                report.reject(index, "period_end is not period_start + 1 h")
                continue

        avg = tuple(_to_float(item.get(k)) for k in ("ghi", "dni", "dhi"))
        inst = tuple(
            _to_float(item.get(k)) for k in ("ghi_instant", "dni_instant", "dhi_instant")
        )
        avg_ok, avg_reason = _valid_triple(*avg)
        inst_ok, inst_reason = _valid_triple(*inst)
        if avg_reason:
            report.reject(index, f"average {avg_reason}")
        if inst_reason:
            report.reject(index, f"instant {inst_reason}")
        if not avg_ok and not inst_ok:
            continue

        sources = item.get("sources")
        sources = sources if isinstance(sources, int) and not isinstance(sources, bool) else None
        if start in records:
            report.reject(index, "duplicate period_start (later entry wins)")
        records[start] = HourRecord(
            start=start,
            ghi=avg[0] if avg_ok else None,
            dni=avg[1] if avg_ok else None,
            dhi=avg[2] if avg_ok else None,
            ghi_instant=inst[0] if inst_ok else None,
            dni_instant=inst[1] if inst_ok else None,
            dhi_instant=inst[2] if inst_ok else None,
            sources=sources,
        )
        report.accepted_average += int(avg_ok)
        report.accepted_instant += int(inst_ok)
    return records, report


def parse_temperature_forecast(
    raw: Any, unit: str | None
) -> tuple[dict[datetime, float], int]:
    """Validate an HA ``weather.get_forecasts`` hourly list.

    Returns ``({utc_time: temp_c}, rejected_count)``. Converts °F and K.
    """
    temps: dict[datetime, float] = {}
    rejected = 0
    if not isinstance(raw, list):
        return temps, 0
    unit_norm = (unit or "°C").strip().upper().replace("°", "")
    for item in raw:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        when = parse_utc(item.get("datetime"))
        value = _to_float(item.get("temperature"))
        if when is None or value is None:
            rejected += 1
            continue
        if unit_norm == "F":
            value = (value - 32.0) * 5.0 / 9.0
        elif unit_norm == "K":
            value = value - 273.15
        if not TEMPERATURE_MIN_C <= value <= TEMPERATURE_MAX_C:
            rejected += 1
            continue
        temps[when] = value
    return temps, rejected


def parse_snow_depth(value: Any, unit: str | None) -> float | None:
    """Snow depth in metres from a sensor state, or None if unusable."""
    depth = _to_float(value)
    if depth is None:
        return None
    unit_norm = (unit or "m").strip().lower()
    factor = {"m": 1.0, "cm": 0.01, "mm": 0.001, "in": 0.0254, "ft": 0.3048}.get(unit_norm)
    if factor is None:
        return None
    depth *= factor
    if not 0.0 <= depth <= SNOW_DEPTH_MAX_M:
        return None
    return depth


# --------------------------------------------------------------------------
# History merge (past hours are not published by the source)
# --------------------------------------------------------------------------


def merge_history(
    cached: Mapping[datetime, Any],
    fresh: Mapping[datetime, Any],
    now: datetime,
    keep_from: datetime,
) -> dict[datetime, Any]:
    """Merge a cached and a freshly read hourly mapping.

    * Hours that have **fully elapsed** (end <= ``now``) keep the cached value
      unless the fresh read still contains them (then the fresh one wins:
      it is the latest forecast for that hour).
    * The current and future hours come **only** from the fresh read, so a
      future hour that the source stopped publishing is not resurrected from
      cache.
    * Anything starting before ``keep_from`` is pruned.
    """
    current_hour = floor_hour(now)
    merged: dict[datetime, Any] = {
        start: value
        for start, value in cached.items()
        if keep_from <= start < current_hour
    }
    for start, value in fresh.items():
        if start >= keep_from:
            merged[start] = value
    return dict(sorted(merged.items()))


def series_freshness_problem(
    records: Mapping[datetime, HourRecord],
    now: datetime,
    min_future_hours: int,
) -> str | None:
    """Content-based freshness check of a freshly read series.

    The source publishes a series starting at its current UTC hour. If the
    source stalls, its first entry ages. The check therefore requires:

    * an average for the current hour (the series is not stale), and
    * that the first published hour is no older than one hour before the
      current one (tolerates one source refresh cycle across the hour
      boundary), and
    * at least ``min_future_hours`` hours with averages after now.

    Returns a human-readable problem, or None when the series is usable.
    """
    if not records:
        return "irradiance series is empty or entirely invalid"
    current_hour = floor_hour(now)
    first = min(records)
    if first < current_hour - HOUR:
        return (
            f"irradiance series is stale: first hour {first.isoformat()} is "
            f"older than {(current_hour - HOUR).isoformat()}"
        )
    current = records.get(current_hour)
    if current is None or not current.has_average:
        return f"irradiance series has no average for the current hour {current_hour.isoformat()}"
    future = sum(1 for start, rec in records.items() if start > current_hour and rec.has_average)
    if future < min_future_hours:
        return f"irradiance series covers only {future} future hours (< {min_future_hours})"
    return None


# --------------------------------------------------------------------------
# Plane-of-array geometry and transposition
# --------------------------------------------------------------------------


def _parse_geometry(value: str) -> float | None:
    """The library passes azimuth/tilt as strings; 'nan' marks tracking."""
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"invalid plane geometry value {value!r}") from err
    return None if math.isnan(parsed) else parsed


def plane_orientation(
    sun: SunPosition, tilt_deg: float | None, azimuth_om_deg: float | None
) -> tuple[float, float]:
    """Return (tilt_rad, compass azimuth_rad) of the panel normal.

    ``azimuth_om_deg`` uses Open-Meteo's convention (0 = south, -90 = east,
    90 = west). ``None`` marks a tracked axis, matching the library's use of
    ``nan``:

    * azimuth None, tilt fixed  -> vertical-axis tracker (faces sun azimuth)
    * tilt None, azimuth fixed  -> tilt-axis tracker (tilt follows sun)
    * both None                 -> dual-axis tracker (normal points at sun)
    """
    if azimuth_om_deg is None:
        azimuth = sun.azimuth_rad
    else:
        azimuth = math.radians((azimuth_om_deg + 180.0) % 360.0)

    if tilt_deg is None:
        if azimuth_om_deg is None:
            tilt = min(sun.zenith_rad, math.pi / 2)
        else:
            # Optimal tilt within the plane of the fixed azimuth.
            projected = math.tan(min(sun.zenith_rad, math.radians(89.9))) * math.cos(
                sun.azimuth_rad - azimuth
            )
            tilt = max(0.0, min(math.pi / 2, math.atan(projected)))
    else:
        tilt = math.radians(tilt_deg)
    return tilt, azimuth


def cos_incidence(sun: SunPosition, tilt: float, azimuth: float) -> float:
    return math.cos(tilt) * sun.cos_zenith + math.sin(tilt) * math.sin(
        sun.zenith_rad
    ) * math.cos(sun.azimuth_rad - azimuth)


def transpose_hay_davies(
    sun: SunPosition,
    ghi: float,
    dhi: float,
    tilt_deg: float | None,
    azimuth_om_deg: float | None,
    albedo: float = GROUND_ALBEDO,
) -> float:
    """Global tilted irradiance from horizontal GHI and DHI.

    Hay & Davies (1980): circumsolar diffuse is treated like beam, weighted
    by the anisotropy index A = DNI / E0n; the rest is isotropic.
    Beam normal DNI is derived from the horizontal beam (GHI - DHI) so the
    horizontal energy balance holds by construction.
    """
    if sun.cos_zenith <= 0 or ghi <= 0:
        return 0.0
    tilt, azimuth = plane_orientation(sun, tilt_deg, azimuth_om_deg)
    if sun.cos_zenith < COS_ZENITH_FLOOR:
        # Sun within 1° of the horizon: beam normal = beam_h / cos Z is
        # numerically unstable, and flooring cos Z silently discards beam
        # energy (defect D-02). The horizontal beam there is negligible, so
        # it is treated as diffuse, which conserves GHI exactly.
        dhi = ghi
    cos_z = max(sun.cos_zenith, COS_ZENITH_FLOOR)
    beam_h = max(ghi - dhi, 0.0)
    dni = beam_h / cos_z
    cos_theta = max(cos_incidence(sun, tilt, azimuth), 0.0)
    anisotropy = min(dni / sun.extraterrestrial_normal, 1.0)
    beam = dni * cos_theta
    sky_diffuse = dhi * (
        anisotropy * cos_theta / cos_z + (1 - anisotropy) * (1 + math.cos(tilt)) / 2
    )
    ground = ghi * albedo * (1 - math.cos(tilt)) / 2
    return max(beam + sky_diffuse + ground, 0.0)


# --------------------------------------------------------------------------
# Temporal downscaling
# --------------------------------------------------------------------------


def _quarter_sample_moments(quarter_start: datetime) -> list[datetime]:
    """Midpoints of SUB_SAMPLES_PER_QUARTER equal slices of a quarter."""
    return [
        quarter_start + QUARTER * ((k + 0.5) / SUB_SAMPLES_PER_QUARTER)
        for k in range(SUB_SAMPLES_PER_QUARTER)
    ]


def _hour_sample_moments(hour_start: datetime) -> list[datetime]:
    """The union of the four quarters' sample instants of an hour."""
    return [
        moment
        for q in range(4)
        for moment in _quarter_sample_moments(hour_start + QUARTER * q)
    ]


def _hour_clearness(
    record: HourRecord, latitude: float, longitude: float, stats: SynthesisStats | None
) -> tuple[float, float] | None:
    """Hourly clearness index kt and diffuse fraction kd from the averages."""
    if not record.has_average:
        return None
    # The SAME sample instants as the quarter averages below. Using a finer
    # grid here (it once used 1-minute samples) breaks exact energy
    # conservation in the sunrise/sunset hour, where cos Z is strongly
    # non-linear in time (defect D-01, omsf_v0.1.33.2_release_audit.md).
    moments = _hour_sample_moments(record.start)
    total = 0.0
    for moment in moments:
        sun = sun_position(moment, latitude, longitude)
        total += sun.extraterrestrial_normal * max(sun.cos_zenith, 0.0)
    e0h_mean = total / len(moments)
    ghi, dhi = record.ghi, record.dhi
    if ghi <= 0 or e0h_mean <= 0:
        return 0.0, 1.0
    kt = ghi / e0h_mean
    if kt > CLEARNESS_INDEX_MAX:
        kt = CLEARNESS_INDEX_MAX
        if stats is not None:
            stats.kt_clamped_hours += 1
    kd = min(max(dhi / ghi, 0.0), 1.0)
    return kt, kd


def _instant_anchor(
    record: HourRecord | None,
    hour_kt: tuple[float, float] | None,
    latitude: float,
    longitude: float,
) -> tuple[float, float] | None:
    """kt/kd anchor at an hour start, preferring the published instant."""
    if record is not None and record.has_instant:
        sun = sun_position(record.start, latitude, longitude)
        if sun.elevation_deg >= INSTANT_ANCHOR_MIN_ELEVATION_DEG:
            e0h = sun.extraterrestrial_normal * sun.cos_zenith
            ghi = record.ghi_instant
            if ghi <= 0:
                return 0.0, 1.0
            kt = min(ghi / e0h, CLEARNESS_INDEX_MAX)
            kd = min(max(record.dhi_instant / ghi, 0.0), 1.0)
            return kt, kd
    return hour_kt


def _interpolate_temperature(
    temps: list[tuple[datetime, float]], moment: datetime
) -> tuple[float | None, bool]:
    """Linear interpolation; nearest-value extrapolation within a limit.

    Returns (value, extrapolated?).
    """
    if not temps:
        return None, False
    if moment <= temps[0][0]:
        if temps[0][0] - moment <= TEMPERATURE_EXTRAPOLATE_LIMIT:
            return temps[0][1], moment != temps[0][0]
        return None, False
    if moment >= temps[-1][0]:
        if moment - temps[-1][0] <= TEMPERATURE_EXTRAPOLATE_LIMIT:
            return temps[-1][1], moment != temps[-1][0]
        return None, False
    lo, hi = 0, len(temps) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if temps[mid][0] <= moment:
            lo = mid
        else:
            hi = mid
    (t0, v0), (t1, v1) = temps[lo], temps[hi]
    if t1 - t0 > timedelta(hours=3):
        # Do not bridge a gap larger than three hours.
        return None, False
    weight = (moment - t0) / (t1 - t0)
    return v0 + (v1 - v0) * weight, False


def build_open_meteo_payload(
    snapshot: LocalSnapshot,
    latitude: float,
    longitude: float,
    tilt: str,
    azimuth: str,
    stats: SynthesisStats | None = None,
) -> dict[str, Any]:
    """Synthesise an Open-Meteo ``/v1/forecast`` response for one array.

    ``tilt`` and ``azimuth`` are the strings the library puts in its request
    parameters (``"nan"`` for a tracked axis, azimuth in Open-Meteo
    convention).
    """
    if not snapshot.hours:
        raise ValueError("no irradiance hours available")
    tilt_deg = _parse_geometry(tilt)
    azimuth_deg = _parse_geometry(azimuth)
    if azimuth_deg is not None and not -180.0 <= azimuth_deg <= 180.0:
        raise ValueError(f"azimuth {azimuth_deg} outside [-180, 180]")
    if tilt_deg is not None and not 0.0 <= tilt_deg <= 90.0:
        raise ValueError(f"tilt {tilt_deg} outside [0, 90]")

    stats = stats if stats is not None else SynthesisStats()
    hours = dict(sorted(snapshot.hours.items()))
    first_hour = min(hours)
    last_hour = max(hours)
    tz = timezone(timedelta(seconds=snapshot.utc_offset_seconds))

    hour_kt: dict[datetime, tuple[float, float] | None] = {
        start: _hour_clearness(rec, latitude, longitude, stats) for start, rec in hours.items()
    }
    anchor_cache: dict[datetime, tuple[float, float] | None] = {}

    def anchor(hour_start: datetime) -> tuple[float, float] | None:
        if hour_start not in anchor_cache:
            record = hours.get(hour_start)
            fallback = hour_kt.get(hour_start)
            if fallback is None:
                # No average for this hour: borrow the previous hour's.
                fallback = hour_kt.get(hour_start - HOUR)
            anchor_cache[hour_start] = _instant_anchor(record, fallback, latitude, longitude)
        return anchor_cache[hour_start]

    temps = sorted(snapshot.temperatures_c.items())

    times: list[int] = []
    temperature: list[float | None] = []
    gti_avg: list[float | None] = []
    gti_inst: list[float | None] = []
    dhi_avg: list[float | None] = []
    dhi_inst: list[float | None] = []
    beam_avg: list[float | None] = []
    beam_inst: list[float | None] = []
    snow: list[float] = []

    # Label t: average covers [t - 15 min, t), instant is at t. The first
    # label equals the first hour start (no average; library skips index 0).
    label = first_hour
    end_label = last_hour + HOUR
    while label <= end_label:
        times.append(int(label.timestamp()))
        stats.quarters += 1 if label > first_hour else 0

        temp, extrapolated = _interpolate_temperature(temps, label)
        if temp is None and snapshot.fallback_temperature_c is not None:
            temp = snapshot.fallback_temperature_c
            stats.temperature_fallback += 1
        elif temp is None:
            stats.temperature_missing += 1
        elif extrapolated:
            stats.temperature_extrapolated += 1
        temperature.append(None if temp is None else round(temp, 2))
        snow.append(snapshot.snow_depth_m)

        quarter_start = label - QUARTER
        hour_start = floor_hour(quarter_start)
        kt_kd = hour_kt.get(hour_start) if label > first_hour else None
        if kt_kd is None:
            if label > first_hour:
                stats.quarters_without_radiation += 1
            for series in (gti_avg, gti_inst, dhi_avg, dhi_inst, beam_avg, beam_inst):
                series.append(None)
            label += QUARTER
            continue

        # ---- averages over [quarter_start, label) --------------------
        kt, kd = kt_kd
        sum_g = sum_d = sum_gti = 0.0
        for moment in _quarter_sample_moments(quarter_start):
            sun = sun_position(moment, latitude, longitude)
            g = kt * sun.extraterrestrial_normal * max(sun.cos_zenith, 0.0)
            d = kd * g
            sum_g += g
            sum_d += d
            sum_gti += transpose_hay_davies(sun, g, d, tilt_deg, azimuth_deg)
        n = SUB_SAMPLES_PER_QUARTER
        g_mean, d_mean = sum_g / n, sum_d / n
        gti_avg.append(round(sum_gti / n, 2))
        dhi_avg.append(round(d_mean, 2))
        beam_avg.append(round(max(g_mean - d_mean, 0.0), 2))

        # ---- instant at label ----------------------------------------
        h0 = floor_hour(label)
        a0 = anchor(h0)
        a1 = anchor(h0 + HOUR)
        if a0 is None:
            a0 = kt_kd
        if a1 is None:
            a1 = a0
        weight = (label - h0) / HOUR
        kt_i = a0[0] + (a1[0] - a0[0]) * weight
        kd_i = a0[1] + (a1[1] - a0[1]) * weight
        sun = sun_position(label, latitude, longitude)
        g_i = kt_i * sun.extraterrestrial_normal * max(sun.cos_zenith, 0.0)
        d_i = kd_i * g_i
        gti_inst.append(round(transpose_hay_davies(sun, g_i, d_i, tilt_deg, azimuth_deg), 2))
        dhi_inst.append(round(d_i, 2))
        beam_inst.append(round(max(g_i - d_i, 0.0), 2))
        label += QUARTER

    # Daily sunrise/sunset for every local date touched by the grid.
    local_dates = sorted(
        {datetime.fromtimestamp(ts, timezone.utc).astimezone(tz).date() for ts in times}
    )
    daily_time: list[int] = []
    sunrise: list[int] = []
    sunset: list[int] = []
    for day in local_dates:
        rise, set_ = sunrise_sunset(day, latitude, longitude, tz)
        daily_time.append(int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp()))
        sunrise.append(int(rise.timestamp()))
        sunset.append(int(set_.timestamp()))

    return {
        "latitude": latitude,
        "longitude": longitude,
        "utc_offset_seconds": snapshot.utc_offset_seconds,
        "generationtime_ms": 0.0,
        "source": "local",
        "minutely_15": {
            "time": times,
            "temperature_2m": temperature,
            "global_tilted_irradiance": gti_avg,
            "global_tilted_irradiance_instant": gti_inst,
            "diffuse_radiation": dhi_avg,
            "diffuse_radiation_instant": dhi_inst,
            "direct_radiation": beam_avg,
            "direct_radiation_instant": beam_inst,
            "snow_depth": snow,
        },
        "daily": {"time": daily_time, "sunrise": sunrise, "sunset": sunset},
    }


def complete_local_dates(
    hours: Mapping[datetime, HourRecord],
    latitude: float,
    longitude: float,
    utc_offset_seconds: int,
) -> set[date]:
    """Local dates for which every daylight hour has a valid average.

    The upstream library reports 0 Wh for a day it has no data for, and an
    undercount for a partly covered day. With a ~5-day local horizon that
    would make the d5..d7 sensors read 0 or too low instead of "unknown"
    (defect D-05). A daylight hour is one in which the sun is above the
    horizon (-0.833°) at any of the hour's sample instants.
    """
    if not hours:
        return set()
    tz = timezone(timedelta(seconds=utc_offset_seconds))
    candidates = {start.astimezone(tz).date() for start in hours}
    complete: set[date] = set()
    for day in sorted(candidates):
        midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
        ok = True
        for h in range(24):
            hour_start = (midnight + timedelta(hours=h)).astimezone(timezone.utc)
            daylight = any(
                _elevation_deg(hour_start + QUARTER * q + QUARTER / 2, latitude, longitude) > -0.833
                for q in range(4)
            )
            if not daylight:
                continue
            record = hours.get(hour_start)
            if record is None or not record.has_average:
                ok = False
                break
        if ok:
            complete.add(day)
    return complete


def series_dni_consistency(records: Iterable[HourRecord], latitude: float, longitude: float) -> float | None:
    """Mean absolute deviation (W/m²) between published hourly DNI and the
    DNI implied by (GHI - DHI) / mean cos(zenith) for daytime hours.

    A diagnostic only: the synthesis anchors on GHI and DHI, so a large
    value flags that the source's triple is internally inconsistent.
    """
    deviations: list[float] = []
    for rec in records:
        if not rec.has_average or rec.dni is None:
            continue
        cos_values = [
            max(sun_position(rec.start + timedelta(minutes=m + 0.5), latitude, longitude).cos_zenith, 0.0)
            for m in range(0, 60, 5)
        ]
        mean_cos = sum(cos_values) / len(cos_values)
        if mean_cos < 0.17:  # ignore hours with the sun below ~10°
            continue
        implied = max(rec.ghi - rec.dhi, 0.0) / mean_cos
        deviations.append(abs(implied - rec.dni))
    if not deviations:
        return None
    return round(sum(deviations) / len(deviations), 1)
