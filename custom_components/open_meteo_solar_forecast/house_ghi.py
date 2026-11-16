"""Measured light at the house (0.1.36): the inverters' 15-minute energy,
turned back into global horizontal irradiance.

Design record: ``OMSF_v0_1_36_House_GHI_Spec.md``; interface for consumers:
``docs/house_ghi_interface.md``. Pure like ``learning.py``: no Home Assistant
imports, no I/O, no clock reads.

Quantity
--------
GHI in W/m² on a flat, unobstructed surface, average of one 15-minute window.
The window's measured energy (all inverter groups together) is inverted
through the members' PV model run on clear-sky-shaped light at the grid of
clear-sky indices ``K_GRID`` (same construction as the same-day correction,
but with the station's *measured* air temperature and no learned factor), so
the orientation of the arrays drops out. GHI = k x clear-sky GHI (Haurwitz,
window average).

Independence
------------
Nothing here depends on a weather forecast: the model input is synthetic
clear-sky-shaped light and measured temperature; no learned correction of the
forecast (stage 1), no stage-2 factor and no same-day correction is applied.
A consumer may therefore learn its own forecast errors from this value.

Quality (OPC-UA style)
----------------------
good / uncertain (published with a reason) / bad (not published, with a
reason). Every window from sunrise to sunset is stored with its quality.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

import numpy as np

from .learning import HOUR_S, _clamp, key_to_dt
from .local_source import sun_position
from .same_day import BAND_S, K_GRID, SIGNAL_AFTER_SUNRISE_S, clear_kt, erbs, invert, monotone

# --------------------------------------------------------------------------
# Parameters (specification section in brackets)
# --------------------------------------------------------------------------

VERSION = 1
"""(5) Raised on every change of the clear-sky model, the PV model input or a
quality rule; recorded with its date in the store and the export."""
VERSION_NOTE = (
    "clear sky Haurwitz (window mean of 5 instants); members' PV model on clear-sky-shaped light "
    "(Erbs split), station air temperature, no learned factors; quality rules of 0.1.36.0"
)
WINDOW_S = BAND_S
CLEAR_SAMPLES = 5  # (3) instants per window for the clear-sky mean
LOW_SUN_BAD_DEG = 10.0  # (4) owner data: 08:00 (12°) gets 37 % of the station's clear light
LOW_SUN_UNCERTAIN_DEG = 18.0  # (4) 09:00 (17°): 76 %; from 20° on 0.85-1.0
CLIP_BAD = 0.95  # (4) share of the inverter's AC limit over the window
CLIP_UNCERTAIN = 0.85
MIXED_KD_LOW, MIXED_KD_HIGH = 0.25, 0.85  # (4) diffuse fraction where the split is assumed
RESOLUTION_CLEAR_KWH = 0.3  # (4) as the same-day window: 0.01 kWh steps -> |Δk| <= 0.033 per counter
MODEL_MIN_CLEAR_KWH = 0.01  # (4) below: the model sees no light (sun behind modelled obstacles)
COUNTER_WAIT_S = 1800  # (3) a window waits this long for its counter readings, then is bad
PUBLISH_MAX_AGE_S = 2700  # (5) the sensor shows a window at most this long after its end
SNOW_MIN_ELEVATION_DEG = 18.0  # (4) snow test only outside the low-sun zone
SNOW_STATION_K = 0.5
SNOW_RATIO = 0.4
SNOW_MAX_TEMP_C = 3.0
DEFAULT_TEMPERATURE_C = 10.0  # (3) used, with quality uncertain, when the station has none
TEMPERATURE_MAX_AGE_S = 3 * HOUR_S
RECENT_WINDOWS = 8
RETENTION_DAYS = 760  # two years, as the other measurements

GOOD, UNCERTAIN, BAD = "good", "uncertain", "bad"
QUALITIES = (GOOD, UNCERTAIN, BAD)
BAD_REASONS = ("counter", "startup", "low_sun", "snow", "clipping", "curtailment")
UNCERTAIN_REASONS = ("clipping", "low_sun", "temperature", "saturated", "resolution", "mixed_sky")
REASONS = tuple(dict.fromkeys(BAD_REASONS + UNCERTAIN_REASONS))
"""``curtailment`` is part of the interface; this installation has no export
limit, so it is never detected (spec §4)."""

ROW = ("ghi", "k", "clear_ghi", "quality", "reason", "elevation")
"""Stored window: [ghi | None, k | None, clear_ghi, quality, reason | None, elevation]."""


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


def _instants(start: int) -> list[datetime]:
    step = WINDOW_S / CLEAR_SAMPLES
    return [datetime.fromtimestamp(start + step * (i + 0.5), tz=timezone.utc) for i in range(CLEAR_SAMPLES)]


def window_clear_ghi(start: int, latitude: float, longitude: float) -> float:
    """Haurwitz clear-sky GHI (W/m²), mean over the window."""
    total = 0.0
    for moment in _instants(start):
        cz = sun_position(moment, latitude, longitude).cos_zenith
        if cz > 0.01:
            total += 1098.0 * cz * math.exp(-0.057 / cz)
    return total / CLEAR_SAMPLES


def window_elevation(start: int, latitude: float, longitude: float) -> float:
    """Sun elevation (degrees) at the middle of the window."""
    moment = datetime.fromtimestamp(start + WINDOW_S / 2, tz=timezone.utc)
    cz = sun_position(moment, latitude, longitude).cos_zenith
    return math.degrees(math.asin(_clamp(cz, -1.0, 1.0)))


# --------------------------------------------------------------------------
# One window
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    start: int
    ghi: float | None
    k: float | None
    clear_ghi: float
    quality: str
    reason: str | None
    elevation: float

    @property
    def end(self) -> int:
        return self.start + WINDOW_S

    def row(self) -> list[Any]:
        return [self.ghi, self.k, self.clear_ghi, self.quality, self.reason, self.elevation]

    @classmethod
    def from_row(cls, start: int, row: Sequence[Any]) -> Window:
        return cls(int(start), row[0], row[1], float(row[2]), str(row[3]), row[4], float(row[5]))


def assess(
    start: int,
    *,
    measured: Mapping[str, float | None],
    curve: np.ndarray,
    limits_kwh: Mapping[str, float | None],
    sunrise: datetime | None,
    clear_ghi: float,
    elevation: float,
    kt_clear: float,
    snow: bool,
    temperature_ok: bool,
) -> Window:
    """Quality, k and GHI of one window (spec §4; first matching rule wins).

    ``measured``: group -> window energy (kWh), None = no valid counter
    reading. ``curve``: the summed model response over all groups on
    ``K_GRID`` (kWh). ``limits_kwh``: group -> inverter AC limit over the
    window (kWh), None = unknown. ``kt_clear``: clear-sky clearness index of
    the hour (for the diffuse fraction).
    """
    clear_ghi = round(clear_ghi, 1)
    elevation = round(elevation, 2)

    def bad(reason: str, k: float | None = None) -> Window:
        return Window(start, None, None if k is None else round(k, 4), clear_ghi, BAD, reason, elevation)

    if any(value is None for value in measured.values()) or not measured:
        return bad("counter")
    if sunrise is not None and start < sunrise.timestamp() + SIGNAL_AFTER_SUNRISE_S:
        return bad("startup")
    if elevation < LOW_SUN_BAD_DEG:
        return bad("low_sun")
    model = monotone(curve)
    clear_kwh = float(model[int(np.searchsorted(K_GRID, 1.0))])
    if clear_kwh < MODEL_MIN_CLEAR_KWH:
        return bad("low_sun")
    total = float(sum(v for v in measured.values() if v is not None))
    k, saturated = invert(curve, total)
    if snow:
        return bad("snow", k)
    loads = [
        (measured[g] or 0.0) / limit
        for g, limit in limits_kwh.items()
        if limit is not None and limit > 0 and g in measured
    ]
    load = max(loads, default=0.0)
    if load >= CLIP_BAD:
        return bad("clipping", k)
    reason = None
    if load >= CLIP_UNCERTAIN:
        reason = "clipping"
    elif elevation < LOW_SUN_UNCERTAIN_DEG:
        reason = "low_sun"
    elif not temperature_ok:
        reason = "temperature"
    elif saturated:
        reason = "saturated"
    elif clear_kwh < RESOLUTION_CLEAR_KWH:
        reason = "resolution"
    elif MIXED_KD_LOW <= erbs(k * kt_clear) <= MIXED_KD_HIGH:
        reason = "mixed_sky"
    ghi = round(k * clear_ghi, 1)
    return Window(start, ghi, round(k, 4), clear_ghi, GOOD if reason is None else UNCERTAIN, reason, elevation)


def window_kt_clear(start: int, latitude: float, longitude: float) -> float:
    return clear_kt(start - start % HOUR_S, latitude, longitude)


# --------------------------------------------------------------------------
# Snow
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SnowHour:
    """One complete hour: the house's mean k, the station's k, air
    temperature and sun elevation (None: not known)."""

    house_k: float | None
    station_k: float | None
    temperature: float | None
    elevation: float


def snow_verdict(hours: Sequence[SnowHour]) -> bool | None:
    """True: snow suspected; False: clearly none; None: cannot tell.

    Snow is suspected when, in each of the given (last two complete) hours,
    the station sees a bright sky, it is cold, the sun is out of the low-sun
    zone, and the house gets far less light than the station.
    """
    if not hours:
        return None
    verdicts = []
    for hour in hours:
        if (hour.house_k is None or hour.station_k is None or hour.temperature is None
                or hour.elevation < SNOW_MIN_ELEVATION_DEG or hour.station_k < SNOW_STATION_K):
            return None
        verdicts.append(hour.temperature <= SNOW_MAX_TEMP_C and hour.house_k <= SNOW_RATIO * hour.station_k)
    return all(verdicts)


def station_k(ghi: float | None, start_hour: int, latitude: float, longitude: float) -> float | None:
    """Station clear-sky index of an hour (Haurwitz, same reference)."""
    if ghi is None:
        return None
    clear = sum(window_clear_ghi(start_hour + i * WINDOW_S, latitude, longitude) for i in range(4)) / 4
    if clear <= 1.0:
        return None
    return ghi / clear


# --------------------------------------------------------------------------
# Hours, days, the sensor's attributes
# --------------------------------------------------------------------------


def hour_value(windows: Mapping[int, Window], hour_start: int) -> tuple[float | None, str]:
    """(mean GHI, quality) of a clock hour: bad unless all four windows are
    present and none is bad; uncertain if any is uncertain."""
    items = [windows.get(hour_start + i * WINDOW_S) for i in range(4)]
    if any(w is None or w.quality == BAD or w.ghi is None for w in items):
        return None, BAD
    ghis = [w.ghi for w in items if w is not None and w.ghi is not None]
    quality = UNCERTAIN if any(w is not None and w.quality == UNCERTAIN for w in items) else GOOD
    return round(sum(ghis) / 4.0, 1), quality


def day_counts(windows: Iterable[Window]) -> dict[str, Any]:
    """Windows of one day by quality, and bad/uncertain ones by reason."""
    out: dict[str, Any] = {
        "windows": 0,
        GOOD: 0,
        UNCERTAIN: 0,
        BAD: 0,
        "bad_by_reason": {r: 0 for r in BAD_REASONS},
        "uncertain_by_reason": {r: 0 for r in UNCERTAIN_REASONS},
    }
    for w in windows:
        out["windows"] += 1
        out[w.quality] += 1
        if w.quality == BAD and w.reason in out["bad_by_reason"]:
            out["bad_by_reason"][w.reason] += 1
        elif w.quality == UNCERTAIN and w.reason in out["uncertain_by_reason"]:
            out["uncertain_by_reason"][w.reason] += 1
    return out


def windows_of(section: Mapping[str, Any]) -> dict[int, Window]:
    return {int(k): Window.from_row(int(k), row) for k, row in section["windows"].items()}


def day_of(start: int, tz: tzinfo) -> str:
    return key_to_dt(start).astimezone(tz).date().isoformat()


def latest(windows: Mapping[int, Window]) -> Window | None:
    return windows[max(windows)] if windows else None


def published(window: Window | None, now: datetime) -> bool:
    """The sensor shows a value: a window that is not bad and recent."""
    return (
        window is not None
        and window.quality != BAD
        and window.ghi is not None
        and now.timestamp() - window.end <= PUBLISH_MAX_AGE_S
    )


def attributes(windows: Mapping[int, Window], now: datetime, tz: tzinfo) -> dict[str, Any]:
    """The sensor's attributes (interface: docs/house_ghi_interface.md)."""
    last = latest(windows)
    out: dict[str, Any] = {
        "quality": None,
        "reason": None,
        "window_start": None,
        "window_end": None,
        "clear_sky_index": None,
        "clear_sky_ghi": None,
        "hour_ghi": None,
        "hour_quality": None,
        "recent": [],
        "version": VERSION,
    }
    if last is None:
        return out
    stale = now.timestamp() - last.end > PUBLISH_MAX_AGE_S
    out.update(
        quality=BAD if stale else last.quality,
        reason="counter" if stale and last.quality != BAD else last.reason,
        window_start=key_to_dt(last.start).isoformat(),
        window_end=key_to_dt(last.end).isoformat(),
        clear_sky_index=last.k,
        clear_sky_ghi=last.clear_ghi,
    )
    hour_start = last.end - last.end % HOUR_S - HOUR_S
    hour_ghi, hour_quality = hour_value(windows, hour_start)
    out["hour_ghi"], out["hour_quality"] = hour_ghi, hour_quality
    out["hour_start"] = key_to_dt(hour_start).isoformat()
    out["recent"] = [
        [key_to_dt(w.start).isoformat(), w.ghi, w.quality]
        for w in (windows[s] for s in sorted(windows)[-RECENT_WINDOWS:])
    ]
    today = now.astimezone(tz).date().isoformat()
    counts = day_counts(w for w in windows.values() if day_of(w.start, tz) == today)
    out["windows_today"] = counts["windows"]
    out["bad_counts_today"] = counts["bad_by_reason"]
    out["uncertain_counts_today"] = counts["uncertain_by_reason"]
    return out


# --------------------------------------------------------------------------
# Store section, retention, export
# --------------------------------------------------------------------------


class HouseGhiDataError(ValueError):
    """The stored house-light section is malformed."""


def new_section() -> dict[str, Any]:
    return {"windows": {}, "version_history": []}


def _number(value: Any, allow_none: bool) -> bool:
    if value is None:
        return allow_none
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_section(raw: Any) -> dict[str, Any]:
    """Strict check of the stored section; raises HouseGhiDataError."""
    if raw is None:
        return new_section()
    if not isinstance(raw, dict) or not isinstance(raw.get("windows"), dict):
        raise HouseGhiDataError("house_ghi is not a mapping with windows")
    history = raw.get("version_history", [])
    if not isinstance(history, list):
        raise HouseGhiDataError("house_ghi version history")
    for item in history:
        if (not isinstance(item, list) or len(item) != 3 or not isinstance(item[0], str)
                or not isinstance(item[1], int) or isinstance(item[1], bool) or not isinstance(item[2], str)):
            raise HouseGhiDataError("house_ghi version entry")
    for key, row in raw["windows"].items():
        try:
            int(key)
        except (TypeError, ValueError) as err:
            raise HouseGhiDataError("house_ghi window key") from err
        if not isinstance(row, list) or len(row) != len(ROW):
            raise HouseGhiDataError("house_ghi window row")
        ghi, k, clear, quality, reason, elevation = row
        if quality not in QUALITIES:
            raise HouseGhiDataError("house_ghi quality")
        if reason is not None and reason not in REASONS:
            raise HouseGhiDataError("house_ghi reason")
        if not (_number(ghi, True) and _number(k, True) and _number(clear, False) and _number(elevation, False)):
            raise HouseGhiDataError("house_ghi value")
        if quality == BAD and ghi is not None:
            raise HouseGhiDataError("house_ghi: a bad window carries no value")
        if quality != BAD and ghi is None:
            raise HouseGhiDataError("house_ghi: a published window needs a value")
        if (quality == GOOD) != (reason is None):
            raise HouseGhiDataError("house_ghi: reason only for uncertain and bad windows")
    return {"windows": dict(raw["windows"]), "version_history": [list(i) for i in history]}


def note_version(section: dict[str, Any], now: datetime) -> bool:
    """Record the running version once, with its date (request 4)."""
    history = section["version_history"]
    if history and history[-1][1] == VERSION:
        return False
    history.append([now.isoformat(), VERSION, VERSION_NOTE])
    return True


def store_window(section: dict[str, Any], window: Window) -> None:
    section["windows"][str(window.start)] = window.row()


def purge(section: dict[str, Any], today: str, tz: tzinfo) -> int:
    limit = (date.fromisoformat(today) - timedelta(days=RETENTION_DAYS)).isoformat()
    old = [key for key in section["windows"] if day_of(int(key), tz) < limit]
    for key in old:
        del section["windows"][key]
    return len(old)


WINDOW_HEADER = [
    "window_start_utc", "window_end_utc", "ghi_w_m2", "clear_sky_index", "clear_sky_ghi_w_m2",
    "quality", "reason", "sun_elevation_deg", "version",
]


def version_at(section: Mapping[str, Any], start: int) -> int:
    """The version that produced a window (the latest entry recorded before it)."""
    version = VERSION
    moment = key_to_dt(start)
    for recorded, number, _note in section["version_history"]:
        if datetime.fromisoformat(recorded) <= moment:
            version = number
    return version


def export_rows(section: Mapping[str, Any], tz: tzinfo) -> tuple[list[list[Any]], list[str], list[list[Any]], list[str]]:
    """(window rows, header, day rows, header) for CSV export."""
    windows = windows_of(section)
    rows = []
    for start in sorted(windows):
        w = windows[start]
        rows.append([key_to_dt(w.start).isoformat(), key_to_dt(w.end).isoformat(), w.ghi, w.k, w.clear_ghi,
                     w.quality, w.reason, w.elevation, version_at(section, start)])
    by_day: dict[str, list[Window]] = {}
    for w in windows.values():
        by_day.setdefault(day_of(w.start, tz), []).append(w)
    day_rows = []
    for day in sorted(by_day):
        c = day_counts(by_day[day])
        day_rows.append([day, c["windows"], c[GOOD], c[UNCERTAIN], c[BAD]]
                        + [c["bad_by_reason"][r] for r in BAD_REASONS]
                        + [c["uncertain_by_reason"][r] for r in UNCERTAIN_REASONS])
    day_header = (["date", "windows", "good", "uncertain", "bad"]
                  + [f"bad_{r}" for r in BAD_REASONS] + [f"uncertain_{r}" for r in UNCERTAIN_REASONS])
    return rows, WINDOW_HEADER, day_rows, day_header
