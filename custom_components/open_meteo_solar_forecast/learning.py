"""Learning engine (0.1.34): corrects the local weather input from experience.

Design record: ``OMSF_v0_1_34_Learning_Spec.md``.

This module is deliberately **pure**: no Home Assistant imports, no file or
network I/O, no clock reads (every function takes ``now``/``cutoff``
explicitly). The HA glue lives in ``learning_hub.py``.

Overview
--------
Stage 1 (weather, shared by all arrays)
    For every forecast hour the six predictors (forecast clearness index
    kt, sun elevation, low cloud, max(mid, high) cloud, temperature - dew
    point, lead-time bucket) are compared with an archive of past forecast
    hours. The forecast errors of the most similar past hours, measured
    against the MeteoSwiss station (Tänikon), give a shift of kt and of the
    diffuse fraction kd ("analog ensemble", Delle Monache et al. 2013,
    Alessandrini et al. 2015). The shift is shrunk towards zero when the
    evidence is thin, so the learner starts neutral by construction.

Stage 2 (per inverter group)
    The PV model is re-run with *measured* station irradiance; production
    divided by that model, per measured-kt bin (overcast / broken / clear),
    gives a small calibration factor, shrunk towards 1 and bounded.

Every piece of evidence becomes usable only after its day passed quality
control ("decided"), and every correction uses only evidence decided
before the correction's issue time. That makes the archive's stored
corrections reproducible (``tools/learning_replay.py``) and the skill gate
free of look-ahead.

Time keys are UTC epoch seconds of the hour start (``int``); local dates
are ISO strings (``YYYY-MM-DD``).
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

import numpy as np

from .local_source import (
    CLEARNESS_INDEX_MAX,
    IRRADIANCE_MAX_W_M2,
    MAX_SERIES_ENTRIES,
    SERIES_WINDOW_FUTURE,
    SERIES_WINDOW_PAST,
    TEMPERATURE_MAX_C,
    TEMPERATURE_MIN_C,
    HourRecord,
    _hour_sample_moments,
    _to_float,
    parse_utc,
    sun_position,
)

# --------------------------------------------------------------------------
# Parameters (all documented in the specification, section in brackets)
# --------------------------------------------------------------------------

ALGORITHM_VERSION = 2
"""2 (0.1.34.2): total cloud as a predictor; stage 2 by sky type and
temperature band over all stored data; longer memory."""
"""Bumped whenever a change alters corrections; the gate restarts (§9)."""

STORE_SCHEMA_VERSION = 2  # 2: archive rows carry total cloud (1 is migrated)

HOUR_S = 3600
DAY_S = 86400

LEAD_BUCKETS: tuple[tuple[float, float], ...] = ((0.0, 6.0), (6.0, 18.0), (18.0, 30.0), (30.0, 54.0))
BUCKET_NAMES = ("0-6h", "6-18h", "18-30h", "30-54h")
"""Lead time = hour start - issue time, in hours (§4). The running hour has
lead 0. Hours beyond 54 h are not archived and never corrected."""

MIN_ELEVATION_DEG = 5.0  # hours learned from / corrected (§3)
STAGE2_MIN_ELEVATION_DEG = 10.0  # (§6)

PREDICTOR_NAMES = ("kt", "elevation", "low", "mid_high", "spread", "total")
PREDICTOR_SCALES = np.array([0.10, 10.0, 25.0, 30.0, 2.0, 30.0])  # (§4; total cloud 0.1.34.2)

K_NEIGHBOURS = 25
MAX_DISTANCE = 1.5
MIN_ANALOGS = 8
DISTANCE_EPS = 0.2
SHRINK_N0 = 10.0
RECENCY_HALF_LIFE_DAYS = 90.0
RECENCY_FLOOR = 0.1
"""0.1.34.2 (owner: keep learned data long enough for seasons): older
evidence weighs less, halving every 3 months, but never below 10 %."""
SEASON_MIN_AGE_DAYS = 300.0
SEASON_WINDOW_DAYS = 45.0
SEASON_FLOOR = 0.7
"""Evidence from the same season a year (or more) earlier, ±45 days of day
of year, weighs at least 70 %."""
MAX_SHIFT_KT = 0.35
MAX_SHIFT_KD = 0.5
KD_MIN = 0.05
TOP_ANALOGS = 5

STATION_SAMPLES_PER_HOUR = 6
STATION_MIN_VALUES = 5
STATION_SAMPLE_KEEP_S = 72 * HOUR_S
STATION_MAX_AGE_S = 60 * 60
"""A station state older than this is ignored (Fusion marks it unknown)."""
RECENT_MAX_ENTRIES = 100
"""0.1.34.1: Fusion >= 0.3.5 publishes up to 36 records in `recent_values`;
a longer list is rejected as a whole (not truncated)."""
RECENT_WINDOW_S = 7 * 3600
"""Records of `recent_values` older than this (relative to the reference
time) or more than 15 minutes in its future are ignored."""

STATION_RANGES: dict[str, tuple[str, float, float]] = {
    # field: (MeteoSwiss short name, min, max)
    "ghi": ("gre000z0", 0.0, IRRADIANCE_MAX_W_M2),
    "dhi": ("ods000z0", 0.0, IRRADIANCE_MAX_W_M2),
    "sun": ("sre000z0", 0.0, 10.0),
    "lw": ("oli000z0", 50.0, 700.0),
    "refl": ("osr000z0", 0.0, IRRADIANCE_MAX_W_M2),
    "temp": ("tre200s0", TEMPERATURE_MIN_C, TEMPERATURE_MAX_C),
}
STATION_FIELDS = tuple(STATION_RANGES)

STAGE2_BIN_EDGES = (0.3, 0.6)
STAGE2_BIN_NAMES = ("overcast", "broken", "clear")
CELL_HEATING_K_PER_W_M2 = 0.0342
"""Ross coefficient, the same the forecast library uses for its cell
temperature (``Tc = Ta + k * G``). Here applied to horizontal irradiance,
only to choose the calibration band (approximation, spec §6)."""
STAGE2_TEMP_EDGES = (25.0, 45.0)  # °C estimated cell temperature
STAGE2_TEMP_NAMES = ("cool", "warm", "hot")
STAGE2_CELLS = tuple(f"{sky}_{band}" for sky in STAGE2_BIN_NAMES for band in STAGE2_TEMP_NAMES)
"""0.1.34.2: 9 cells per inverter group (sky type x cell-temperature band)."""
STAGE2_SHADE_MAX_LOSS = 0.10
"""Hours in which the shading geometry removes more than this share of the
modelled energy are left out of stage 2 (spec §6)."""
STAGE2_N0 = 40.0
STAGE2_MIN = 0.85
STAGE2_MAX = 1.15

GATE_WINDOW_DAYS = 28
GATE_MIN_DAYS = 14
GATE_MIN_HOURS_PER_DAY = 4
GATE_MIN_IMPROVEMENT = 0.05
GATE_ENERGY_TOLERANCE = 0.01
SUNRISE_ISSUE_HOUR = 6  # local time of the "sunrise view" (owner, 12 Oct 2026)

DAY_RATIO_MIN = 0.6
DAY_RATIO_MAX = 1.4
DAY_RATIO_MIN_MODEL_KWH = 1.0
DAY_STATION_COMPLETENESS = 0.8
MODEL_WAIT_DAYS = 3
REPRESENTATIVE_MIN_R = 0.85
REPRESENTATIVE_WINDOW_DAYS = 14
REPRESENTATIVE_MIN_HOURS = 60
SNOW_ALBEDO = 0.4
SNOW_MIN_GHI = 100.0
COUNTER_STEP_FACTOR = 1.2

RETENTION_DAYS = 760
EVIDENCE_MAX_AGE_DAYS = 740
"""Evidence older than this (relative to the cutoff) is not used. Smaller
than the retention, so a correction never depends on data the retention
has already removed: the export can reproduce recent corrections exactly."""
PRECISION = 6  # decimals of every stored predictor (stored = used)
EVENT_LOG_MAX = 500
HISTORY_MAX = 90
STORE_MAX_BYTES = 16 * 1024 * 1024  # two years ≈ 4–6 MB
MAX_GROUPS = 8

DAY_OK = "ok"
DAY_REASONS = (
    "station_incomplete",
    "snow",
    "production_missing",
    "production_fault",
    "not_representative",
)

STATE_COLLECTING = "collecting"
STATE_SHADOW = "shadow"
STATE_ELIGIBLE = "eligible"
STATE_APPLIED = "applied"
STATE_PAUSED = "paused"
STATES = (STATE_COLLECTING, STATE_SHADOW, STATE_ELIGIBLE, STATE_APPLIED, STATE_PAUSED)

# Archive row layout (lists keep the store compact).
ROW_FIELDS = (
    "issued", "kt", "kd", "low", "mid", "high", "temp", "dew", "elevation", "e0h",
    "shift_kt", "shift_kd", "shrink", "analogs", "pooled", "total",
)
R = {name: i for i, name in enumerate(ROW_FIELDS)}

HOUR_FIELDS = ("ghi", "dhi", "sun", "lw", "refl", "temp", "n")
H = {name: i for i, name in enumerate(HOUR_FIELDS)}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def hour_key(moment: datetime) -> int:
    """UTC epoch seconds of the hour containing ``moment``."""
    ts = int(moment.astimezone(timezone.utc).timestamp())
    return ts - ts % HOUR_S


def key_to_dt(key: int) -> datetime:
    return datetime.fromtimestamp(key, timezone.utc)


def local_date(key: int, tz: tzinfo) -> str:
    return key_to_dt(key).astimezone(tz).date().isoformat()


def local_day_hours(day: str, tz: tzinfo) -> list[int]:
    """UTC hour keys whose start lies in the local date (23/24/25 hours)."""
    d = date.fromisoformat(day)
    start = datetime(d.year, d.month, d.day, tzinfo=tz)
    end = start + timedelta(days=1)
    # Normalise via UTC to handle DST transitions exactly.
    first = hour_key(start)
    keys = []
    k = first
    while k < end.astimezone(timezone.utc).timestamp():
        keys.append(k)
        k += HOUR_S
    return keys


def lead_bucket(lead_hours: float) -> int | None:
    """Index of the lead-time bucket, ``None`` beyond the last one."""
    lead = max(lead_hours, 0.0)
    for index, (low, high) in enumerate(LEAD_BUCKETS):
        if low <= lead < high:
            return index
    return None


def stage2_bin(kt: float) -> int:
    if kt < STAGE2_BIN_EDGES[0]:
        return 0
    if kt < STAGE2_BIN_EDGES[1]:
        return 1
    return 2


def cell_temperature(air_temp: float | None, ghi: float) -> float | None:
    """Estimated module temperature: air temperature plus heating by the
    sunlight (Ross). None when the air temperature is unknown."""
    if air_temp is None:
        return None
    return air_temp + CELL_HEATING_K_PER_W_M2 * max(ghi, 0.0)


def stage2_cell(kt: float, temp: float | None) -> str:
    """Calibration cell: sky type from kt, band from the estimated cell
    temperature (unknown temperature counts as the middle band)."""
    if temp is None:
        band = 1
    elif temp < STAGE2_TEMP_EDGES[0]:
        band = 0
    elif temp < STAGE2_TEMP_EDGES[1]:
        band = 1
    else:
        band = 2
    return f"{STAGE2_BIN_NAMES[stage2_bin(kt)]}_{STAGE2_TEMP_NAMES[band]}"


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


_GEOMETRY_CACHE: dict[tuple[int, float, float], tuple[float, float]] = {}


def hour_geometry(key: int, latitude: float, longitude: float) -> tuple[float, float]:
    """(mean E0·cos Z in W/m², mean sun elevation in °) over the hour.

    Uses exactly the sample instants of ``local_source._hour_clearness``, so
    a kt computed here equals the kt the synthesis derives from the same
    GHI (no second definition of the clearness index).
    """
    cache_key = (key, round(latitude, 4), round(longitude, 4))
    cached = _GEOMETRY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    total = 0.0
    elevation = 0.0
    moments = _hour_sample_moments(key_to_dt(key))
    for moment in moments:
        sun = sun_position(moment, latitude, longitude)
        total += sun.extraterrestrial_normal * max(sun.cos_zenith, 0.0)
        elevation += math.degrees(math.asin(_clamp(sun.cos_zenith, -1.0, 1.0)))
    result = (total / len(moments), elevation / len(moments))
    if len(_GEOMETRY_CACHE) > 20000:
        _GEOMETRY_CACHE.clear()
    _GEOMETRY_CACHE[cache_key] = result
    return result


def clearness(ghi: float, dhi: float, e0h: float) -> tuple[float, float]:
    """kt (clamped to 1) and kd (in [0, 1]) as the synthesis defines them."""
    if ghi <= 0 or e0h <= 0:
        return 0.0, 1.0
    kt = min(ghi / e0h, CLEARNESS_INDEX_MAX)
    kd = _clamp(dhi / ghi, 0.0, 1.0)
    return kt, kd


# --------------------------------------------------------------------------
# Input parsing: weather layers and station values
# --------------------------------------------------------------------------


def _temperature_c(value: Any, unit_norm: str) -> float | None:
    number = _to_float(value)
    if number is None:
        return None
    if unit_norm == "F":
        number = (number - 32.0) * 5.0 / 9.0
    elif unit_norm == "K":
        number -= 273.15
    if not TEMPERATURE_MIN_C <= number <= TEMPERATURE_MAX_C:
        return None
    return number


def parse_weather_layers(
    raw: Any, unit: str | None, now: datetime
) -> tuple[dict[int, tuple[float | None, ...]], int]:
    """Validate ``weather.get_forecasts`` (hourly) for the learner.

    Returns ``({instant_ts: (low, mid, high, temp_c, dew_c, total)}, rejected)``.
    A missing or invalid layer is ``None`` (Fusion: missing = no forecast,
    never 0). Same size and time-window bounds as the irradiance series.
    """
    result: dict[int, tuple[float | None, ...]] = {}
    rejected = 0
    if not isinstance(raw, list):
        return result, 0
    if len(raw) > MAX_SERIES_ENTRIES:
        return result, len(raw)
    unit_norm = (unit or "°C").strip().upper().replace("°", "")
    for item in raw:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        when = parse_utc(item.get("datetime"))
        if when is None or not (now - SERIES_WINDOW_PAST <= when <= now + SERIES_WINDOW_FUTURE):
            rejected += 1
            continue
        layers = []
        for key in ("cloud_coverage_low", "cloud_coverage_mid", "cloud_coverage_high"):
            value = _to_float(item.get(key))
            layers.append(value if value is not None and 0.0 <= value <= 100.0 else None)
        temp = _temperature_c(item.get("temperature"), unit_norm)
        dew = _temperature_c(item.get("dew_point"), unit_norm)
        if dew is not None and temp is not None and dew > temp + 0.5:
            dew = None  # physically impossible; treat as missing
        total = _to_float(item.get("cloud_coverage"))
        total = total if total is not None and 0.0 <= total <= 100.0 else None
        result[int(when.timestamp())] = (*layers, temp, dew, total)
    return result, rejected


def hour_layers(
    instants: Mapping[int, tuple[float | None, ...]], key: int
) -> tuple[float | None, ...]:
    """Weather fields for the hour [key, key+1h): mean of the instants at
    the two ends; one end alone if the other is absent."""
    ends = [instants.get(key), instants.get(key + HOUR_S)]
    out: list[float | None] = []
    for index in range(6):
        values: list[float] = [v for end in ends if end is not None and (v := end[index]) is not None]
        out.append(sum(values) / len(values) if values else None)
    return tuple(out)


@dataclass(frozen=True)
class StationSample:
    """One 10-minute station record (values may be None)."""

    end: int  # epoch s, end of the 10-minute interval
    ghi: float | None
    dhi: float | None
    sun: float | None
    lw: float | None
    refl: float | None
    temp: float | None

    def as_list(self) -> list[float | None]:
        return [self.ghi, self.dhi, self.sun, self.lw, self.refl, self.temp]


def parse_station_values(values: Any, end: datetime | None) -> StationSample | None:
    """Validate the station's ``values`` attribute.

    Out-of-range values become ``None`` (never clamped, never filled).
    Returns ``None`` when there is no usable time or no usable global
    radiation.
    """
    if end is None or not isinstance(values, Mapping):
        return None
    end_ts = int(end.astimezone(timezone.utc).timestamp())
    # 10-minute grid; tolerate a few seconds of jitter.
    rounded = int(round(end_ts / 600.0)) * 600
    if abs(rounded - end_ts) > 120:
        return None
    parsed: dict[str, float | None] = {}
    for name, (short, low, high) in STATION_RANGES.items():
        number = _to_float(values.get(short))
        parsed[name] = number if number is not None and low <= number <= high else None
    if parsed["ghi"] is None:
        return None
    if parsed["dhi"] is not None and parsed["dhi"] > parsed["ghi"] + 5.0:
        parsed["dhi"] = None
    return StationSample(end=rounded, **parsed)


def parse_station_list(raw: Any, reference: datetime) -> tuple[list[StationSample], int]:
    """Validate Fusion's `recent_values` (>= 0.3.5): a list of
    ``{"time": <end of the 10-min interval>, "values": {...}}``.

    Returns (samples, rejected). Each record is validated exactly like the
    latest value; nothing is filled. A non-list or a list longer than
    RECENT_MAX_ENTRIES yields nothing. Records outside
    [reference - 7 h, reference + 15 min] are rejected individually.
    """
    if raw is None:
        return [], 0
    if not isinstance(raw, list) or len(raw) > RECENT_MAX_ENTRIES:
        return [], len(raw) if isinstance(raw, list) else 1
    ref = int(reference.astimezone(timezone.utc).timestamp())
    samples: list[StationSample] = []
    rejected = 0
    for item in raw:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        end = parse_utc(item.get("time"))
        if end is None or not ref - RECENT_WINDOW_S <= end.timestamp() <= ref + 900:
            rejected += 1
            continue
        sample = parse_station_values(item.get("values"), end)
        if sample is None:
            rejected += 1
            continue
        samples.append(sample)
    return samples, rejected


def station_hour(samples: Mapping[int, list[float | None]], key: int) -> list[float | None] | None:
    """Aggregate the six samples ending at key+10 min … key+60 min.

    At least STATION_MIN_VALUES samples with global radiation are required;
    each other field likewise needs that many values or becomes ``None``.
    Sunshine is summed (minutes in the hour, scaled to 6 samples).
    """
    present = [samples.get(key + 600 * i) for i in range(1, STATION_SAMPLES_PER_HOUR + 1)]
    rows: list[list[float | None]] = [row for row in present if row is not None and row[0] is not None]
    if len(rows) < STATION_MIN_VALUES:
        return None
    out: list[float | None] = []
    for index in range(len(STATION_FIELDS)):
        values: list[float] = [v for row in rows if (v := row[index]) is not None]
        if len(values) < STATION_MIN_VALUES:
            out.append(None)
            continue
        mean = sum(values) / len(values)
        out.append(mean * STATION_SAMPLES_PER_HOUR if STATION_FIELDS[index] == "sun" else mean)
    out.append(float(len(rows)))
    return out


# --------------------------------------------------------------------------
# Predictors
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Predictors:
    key: int
    kt: float
    kd: float
    low: float
    mid: float
    high: float
    temp: float
    dew: float
    elevation: float
    e0h: float
    total: float

    def vector(self) -> np.ndarray:
        return np.array(
            [self.kt, self.elevation, self.low, max(self.mid, self.high), self.temp - self.dew, self.total]
        )


def build_predictors(
    records: Mapping[datetime, HourRecord],
    instants: Mapping[int, tuple[float | None, ...]],
    latitude: float,
    longitude: float,
) -> dict[int, Predictors]:
    """Predictors for every hour that has a forecast average, all weather
    fields and a sun elevation of at least MIN_ELEVATION_DEG."""
    result: dict[int, Predictors] = {}
    for start, record in records.items():
        ghi, dhi = record.ghi, record.dhi
        if ghi is None or dhi is None:
            continue
        key = hour_key(start)
        e0h, elevation = hour_geometry(key, latitude, longitude)
        if elevation < MIN_ELEVATION_DEG or e0h <= 0:
            continue
        low, mid, high, temp, dew, total = hour_layers(instants, key)
        if low is None or mid is None or high is None or temp is None or dew is None or total is None:
            continue
        kt, kd = clearness(ghi, dhi, e0h)
        values = [round(v, PRECISION) for v in (kt, kd, low, mid, high, temp, dew, elevation, e0h, total)]
        result[key] = Predictors(key, *values)
    return result


def predictors_from_row(key: int, row: list[Any]) -> Predictors:
    return Predictors(
        key, row[R["kt"]], row[R["kd"]], row[R["low"]], row[R["mid"]], row[R["high"]],
        row[R["temp"]], row[R["dew"]], row[R["elevation"]], row[R["e0h"]], row[R["total"]],
    )


# --------------------------------------------------------------------------
# Store data model
# --------------------------------------------------------------------------


def _new_data() -> dict[str, Any]:
    return {
        "schema": STORE_SCHEMA_VERSION,
        "algorithm": ALGORITHM_VERSION,
        "created": None,
        "settings": {"apply": False, "apply_changed": None},
        "freeze": [],  # [[start_ts, end_ts|None], ...]
        "resets": [],  # [[ts, "stage1"|"stage2"], ...]: evidence decided earlier is ignored
        "station_samples": {},  # end_ts -> [ghi, dhi, sun, lw, refl, temp]
        "station_hours": {},  # hour -> [ghi, dhi, sun, lw, refl, temp, n]
        "archive": {},  # hour -> {bucket(str): row}
        "issue6": {},  # date -> {"captured": ts, "hours": {hour: [kt, kt_c, kd, kd_c, e0h]}}
        "entry_days": {},  # date -> {entry_id: {"captured": ts, "raw": wh, "corr": wh}}
        "production": {},  # group -> {hour: kwh}
        "model": {},  # group -> {hour: kwh}, station light through the shaded model
        "model_free": {},  # group -> {hour: kwh}, same without shading (0.1.34.2)
        "days": {},  # date -> {"decided": ts, "status": str, "details": {...}}
        "stage2": {"computed": None, "algorithm": ALGORITHM_VERSION, "groups": {}},
        "stage2_history": [],
        "gate": None,
        "gate_history": [],
        "state": {"state": STATE_COLLECTING, "reason": "no evidence yet", "since": None},
        "representativeness": None,
        "events": [],
        "last_nightly": None,
        "production_backfilled": [],
        "purged_before": None,  # newest retention limit applied (epoch s)
    }


class LearningDataError(ValueError):
    """The stored learning data is malformed."""


def _migrate_1_to_2(raw: dict[str, Any]) -> dict[str, Any]:
    """0.1.34.2: archive rows gain total cloud and stage 2 gains temperature
    bands. Archived forecasts and the 06:00 records cannot be completed, so
    they are dropped; measurements, production, models, day decisions,
    settings and the event log are kept. The gate restarts (new algorithm)."""
    out = dict(raw)
    out["schema"] = 2
    out["archive"] = {}
    out["issue6"] = {}
    out["entry_days"] = {}
    out["stage2"] = {"computed": None, "algorithm": ALGORITHM_VERSION, "groups": {}}
    out["stage2_history"] = []
    out["gate"] = None
    out["model_free"] = {}  # filled for the last days by the next nightly run
    return out


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LearningDataError(message)


def _int_keys(mapping: Any, name: str) -> dict[int, Any]:
    _require(isinstance(mapping, dict), f"{name} is not a mapping")
    try:
        return {int(k): v for k, v in mapping.items()}
    except (TypeError, ValueError) as err:
        raise LearningDataError(f"{name}: non-numeric key") from err


def _finite_list(value: Any, length: int, name: str, allow_none: bool = True) -> list[Any]:
    _require(isinstance(value, list) and len(value) == length, f"{name}: bad row")
    for item in value:
        if item is None:
            _require(allow_none, f"{name}: missing value")
            continue
        _require(
            isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item),
            f"{name}: non-finite value",
        )
    checked: list[Any] = value
    return checked


class LearningData:
    """In-memory learning state with strict (de)serialisation.

    Keys are converted to ``int`` hour keys in memory and back to strings
    for JSON. Any structural fault in stored data raises
    ``LearningDataError``; the caller quarantines the file and starts
    neutral.
    """

    def __init__(self, raw: dict[str, Any] | None = None) -> None:
        self.d = _new_data() if raw is None else raw
        self.migrated_from: int | None = None

    # -- (de)serialisation ------------------------------------------------

    @classmethod
    def from_json(cls, raw: Any) -> LearningData:
        try:
            return cls._from_json(raw)
        except LearningDataError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, IndexError) as err:
            raise LearningDataError(f"{type(err).__name__}: {err}") from err

    @classmethod
    def _from_json(cls, raw: Any) -> LearningData:
        _require(isinstance(raw, dict), "root is not a mapping")
        migrated = raw.get("schema") == 1
        if migrated:
            raw = _migrate_1_to_2(raw)
        _require(raw.get("schema") == STORE_SCHEMA_VERSION, "unknown schema version")
        data = _new_data()
        for key in data:
            if key in raw:
                data[key] = raw[key]
        settings = data["settings"]
        _require(isinstance(settings, dict) and isinstance(settings.get("apply"), bool), "settings")
        _require(isinstance(data["freeze"], list), "freeze")
        for interval in data["freeze"]:
            _require(isinstance(interval, list) and len(interval) == 2, "freeze interval")
            _require(isinstance(interval[0], int), "freeze start")
            _require(interval[1] is None or isinstance(interval[1], int), "freeze end")
        _require(data["purged_before"] is None or isinstance(data["purged_before"], int), "purged_before")
        _require(isinstance(data["resets"], list), "resets")
        for item in data["resets"]:
            _require(isinstance(item, list) and len(item) == 2 and isinstance(item[0], int)
                     and item[1] in ("stage1", "stage2"), "reset entry")
        data["station_samples"] = {
            k: _finite_list(v, len(STATION_FIELDS), "station sample")
            for k, v in _int_keys(data["station_samples"], "station_samples").items()
        }
        data["station_hours"] = {
            k: _finite_list(v, len(HOUR_FIELDS), "station hour")
            for k, v in _int_keys(data["station_hours"], "station_hours").items()
        }
        archive: dict[int, dict[int, list[Any]]] = {}
        for k, buckets in _int_keys(data["archive"], "archive").items():
            _require(isinstance(buckets, dict), "archive hour")
            archive[k] = {}
            for b, row in buckets.items():
                index = int(b)
                _require(0 <= index < len(LEAD_BUCKETS), "archive bucket")
                archive[k][index] = _finite_list(row, len(ROW_FIELDS), "archive row", allow_none=False)
        data["archive"] = archive
        for group_map in ("production", "model", "model_free"):
            _require(isinstance(data[group_map], dict), group_map)
            converted = {}
            for group, hours in data[group_map].items():
                converted[str(group)] = {
                    k: float(v)
                    for k, v in _int_keys(hours, group_map).items()
                    if _finite_list([v], 1, group_map, allow_none=False)
                }
            data[group_map] = converted
        issue6 = {}
        _require(isinstance(data["issue6"], dict), "issue6")
        for day, item in data["issue6"].items():
            date.fromisoformat(day)
            _require(isinstance(item, dict) and isinstance(item.get("captured"), int), "issue6 day")
            issue6[day] = {
                "captured": item["captured"],
                "hours": {
                    k: _finite_list(v, 5, "issue6 hour", allow_none=False)
                    for k, v in _int_keys(item.get("hours", {}), "issue6 hours").items()
                },
            }
        data["issue6"] = issue6
        _require(isinstance(data["entry_days"], dict), "entry_days")
        for day, entries in data["entry_days"].items():
            date.fromisoformat(day)
            _require(isinstance(entries, dict), "entry_days day")
            for item in entries.values():
                _require(isinstance(item, dict), "entry day")
                _finite_list([item.get("raw"), item.get("corr"), item.get("captured")], 3, "entry day", False)
        _require(isinstance(data["days"], dict), "days")
        for day, item in data["days"].items():
            date.fromisoformat(day)
            _require(isinstance(item, dict) and isinstance(item.get("decided"), int), "day decision")
            _require(item.get("status") == DAY_OK or item.get("status") in DAY_REASONS, "day status")
        _require(isinstance(data["events"], list), "events")
        _require(isinstance(data["stage2"], dict) and isinstance(data["stage2"].get("groups"), dict), "stage2")
        for group, bins in data["stage2"]["groups"].items():
            _require(isinstance(bins, dict), "stage2 group")
            for name in STAGE2_CELLS:
                item = bins.get(name)
                _require(isinstance(item, dict), "stage2 bin")
                factor = item.get("factor")
                _require(
                    isinstance(factor, (int, float)) and STAGE2_MIN <= factor <= STAGE2_MAX,
                    "stage2 factor out of bounds",
                )
        _require(data["state"].get("state") in STATES, "state")
        result = cls(data)
        result.migrated_from = 1 if migrated else None
        return result

    def to_json(self) -> dict[str, Any]:
        d = self.d
        out = dict(d)
        out["station_samples"] = {str(k): v for k, v in sorted(d["station_samples"].items())}
        out["station_hours"] = {str(k): v for k, v in sorted(d["station_hours"].items())}
        out["archive"] = {
            str(k): {str(b): row for b, row in sorted(buckets.items())}
            for k, buckets in sorted(d["archive"].items())
        }
        for group_map in ("production", "model", "model_free"):
            out[group_map] = {
                group: {str(k): v for k, v in sorted(hours.items())}
                for group, hours in d[group_map].items()
            }
        out["issue6"] = {
            day: {"captured": item["captured"], "hours": {str(k): v for k, v in sorted(item["hours"].items())}}
            for day, item in sorted(d["issue6"].items())
        }
        return out

    def view(self) -> LearningData:
        """A consistent read-only copy for computations in a worker thread.

        Made in the event loop. The containers that the event loop changes
        while a run is in progress (station listener, member refreshes, owner
        switches) are copied, so an executor job never iterates a dict that
        changes under it. Computations never write to a view.
        """
        d = dict(self.d)
        for key in ("archive", "station_hours", "days", "issue6", "settings"):
            d[key] = dict(self.d[key])
        d["entry_days"] = {day: dict(entries) for day, entries in self.d["entry_days"].items()}
        for key in ("production", "model", "model_free"):
            d[key] = {group: dict(hours) for group, hours in self.d[key].items()}
        d["freeze"] = [list(interval) for interval in self.d["freeze"]]
        d["resets"] = [list(item) for item in self.d["resets"]]
        d["station_samples"] = {}  # live buffer; not used by any computation
        d["events"] = list(self.d["events"])
        return LearningData(d)

    # -- events ------------------------------------------------------------

    def event(self, now: datetime, kind: str, message: str) -> None:
        self.d["events"].append([now.astimezone(timezone.utc).isoformat(), kind, message])
        if len(self.d["events"]) > EVENT_LOG_MAX:
            del self.d["events"][: len(self.d["events"]) - EVENT_LOG_MAX]

    # -- settings ------------------------------------------------------------

    @property
    def apply_switch(self) -> bool:
        return bool(self.d["settings"]["apply"])

    def set_apply(self, value: bool, now: datetime) -> bool:
        if self.apply_switch == value:
            return False
        self.d["settings"]["apply"] = value
        self.d["settings"]["apply_changed"] = now.astimezone(timezone.utc).isoformat()
        self.event(now, "apply_on" if value else "apply_off",
                   "owner switched learning " + ("on" if value else "off"))
        return True

    @property
    def frozen(self) -> bool:
        freeze = self.d["freeze"]
        return bool(freeze) and freeze[-1][1] is None

    def set_frozen(self, value: bool, now: datetime) -> bool:
        if self.frozen == value:
            return False
        ts = int(now.timestamp())
        if value:
            self.d["freeze"].append([ts, None])
            self.event(now, "frozen", "owner froze learning: the model no longer takes in new evidence")
        else:
            self.d["freeze"][-1][1] = ts
            self.event(now, "unfrozen", "owner unfroze learning")
        return True

    def reset_floor(self, ts: int, scope: str) -> int:
        """Evidence decided before the last owner reset of ``scope`` (at or
        before ``ts``) is ignored."""
        floor = 0
        for reset_ts, reset_scope in self.d["resets"]:
            if reset_scope == scope and reset_ts <= ts:
                floor = max(floor, reset_ts)
        return floor

    def evidence_cutoff(self, ts: int) -> int:
        """Evidence decided before this time may be used at ``ts``."""
        intervals: list[tuple[int, int | None]] = [(int(a), b) for a, b in self.d["freeze"]]
        for start, end in intervals:
            if start <= ts and (end is None or ts < end):
                return start
        return ts

    # -- ingestion ----------------------------------------------------------

    def add_station_sample(self, sample: StationSample) -> bool:
        """Merge one record. An existing record is replaced only by one
        that is at least as complete (0.1.34.1: the same record arrives both
        as the latest value and in `recent_values`)."""
        samples = self.d["station_samples"]
        new = sample.as_list()
        old = samples.get(sample.end)
        if old is not None:
            if old == new:
                return False
            if sum(v is not None for v in new) < sum(v is not None for v in old):
                return False
        samples[sample.end] = new
        return True

    def finalize_station_hours(self, now: datetime) -> int:
        """Aggregate complete hours (ended ≥ 20 min ago) from samples."""
        now_ts = int(now.timestamp())
        samples = self.d["station_samples"]
        hours = {k - k % HOUR_S - (HOUR_S if k % HOUR_S == 0 else 0) for k in samples}
        added = 0
        for key in sorted(hours):
            if key + HOUR_S + 20 * 60 > now_ts:
                continue
            aggregated = station_hour(samples, key)
            if aggregated is not None and self.d["station_hours"].get(key) != aggregated:
                previous = self.d["station_hours"].get(key)
                # Never replace a more complete hour by a less complete one.
                if previous is None or aggregated[H["n"]] >= previous[H["n"]]:
                    self.d["station_hours"][key] = aggregated
                    added += 1
        for key in [k for k in samples if k < now_ts - STATION_SAMPLE_KEEP_S]:
            del samples[key]
        return added

    def archive_forecast(
        self, predictors: Mapping[int, Predictors], corrections: Mapping[int, Correction], now: datetime
    ) -> int:
        """Store each predictor set in its lead bucket (latest issue wins)."""
        now_ts = int(now.timestamp())
        stored = 0
        for key, pred in predictors.items():
            if key + HOUR_S <= now_ts:
                continue  # an elapsed hour is not a forecast any more
            bucket = lead_bucket((key - now_ts) / HOUR_S)
            if bucket is None:
                continue
            corr = corrections.get(key) or NEUTRAL
            row = [
                now_ts, pred.kt, pred.kd, pred.low, pred.mid, pred.high, pred.temp, pred.dew,
                pred.elevation, pred.e0h, corr.shift_kt, corr.shift_kd, corr.shrink,
                corr.analogs, 1 if corr.pooled else 0, pred.total,
            ]
            self.d["archive"].setdefault(key, {})[bucket] = [
                round(v, 6) if isinstance(v, float) else v for v in row
            ]
            stored += 1
        return stored

    def latest_rows(self, keys: Iterable[int]) -> dict[int, list[Any]]:
        """For each hour the most recently issued archived row."""
        out = {}
        for key in keys:
            buckets = self.d["archive"].get(key)
            if buckets:
                out[key] = max(buckets.values(), key=lambda row: row[R["issued"]])
        return out

    # -- retention -----------------------------------------------------------

    def purge(self, now: datetime, tz: tzinfo) -> int:
        limit = int(now.timestamp()) - RETENTION_DAYS * DAY_S
        self.d["purged_before"] = max(self.d["purged_before"] or 0, limit)
        limit_day = key_to_dt(limit).astimezone(tz).date().isoformat()
        removed = 0
        for name in ("station_hours", "archive"):
            for key in [k for k in self.d[name] if k < limit]:
                del self.d[name][key]
                removed += 1
        for name in ("production", "model", "model_free"):
            for hours in self.d[name].values():
                for key in [k for k in hours if k < limit]:
                    del hours[key]
                    removed += 1
        for name in ("issue6", "entry_days", "days"):
            for day in [d for d in self.d[name] if d < limit_day]:
                del self.d[name][day]
                removed += 1
        for name in ("stage2_history", "gate_history"):
            if len(self.d[name]) > HISTORY_MAX:
                del self.d[name][: len(self.d[name]) - HISTORY_MAX]
        return removed

    def trim_oldest_days(self, days: int, tz: tzinfo) -> None:
        """Drop the oldest ``days`` of evidence (store size limit)."""
        keys = sorted(self.d["station_hours"]) + sorted(self.d["archive"])
        if not keys:
            return
        oldest = min(keys)
        fake_now = key_to_dt(oldest + (RETENTION_DAYS + days) * DAY_S)
        self.purge(fake_now, tz)


# --------------------------------------------------------------------------
# Stage 1: analog ensemble
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Correction:
    shift_kt: float = 0.0
    shift_kd: float = 0.0
    mean_error_kt: float = 0.0
    mean_error_kd: float = 0.0
    analogs: int = 0
    n_eff: float = 0.0
    shrink: float = 0.0
    pooled: bool = False
    top: tuple[tuple[int, int, float, float], ...] = ()  # (hour, bucket, distance, error_kt)

    @property
    def is_neutral(self) -> bool:
        return self.shift_kt == 0.0 and self.shift_kd == 0.0


NEUTRAL = Correction()


@dataclass
class Evidence:
    """Usable evidence at one cutoff, as arrays."""

    hours: np.ndarray  # int64 hour keys
    buckets: np.ndarray  # int
    x: np.ndarray  # (n, len(PREDICTOR_NAMES)) predictor vectors
    err_kt: np.ndarray
    err_kd: np.ndarray

    @property
    def size(self) -> int:
        return int(self.hours.size)


def usable_day(data: LearningData, day: str, cutoff: int, scope: str = "stage1") -> bool:
    item = data.d["days"].get(day)
    return (
        item is not None
        and item["status"] == DAY_OK
        and data.reset_floor(cutoff, scope) <= item["decided"] < cutoff
    )


def build_evidence(data: LearningData, cutoff: int, tz: tzinfo) -> Evidence:
    """All archive rows whose hour has a complete station measurement and
    whose day passed quality control before ``cutoff``."""
    hours, buckets, rows, ekt, ekd = [], [], [], [], []
    station = data.d["station_hours"]
    day_ok: dict[str, bool] = {}
    oldest = cutoff - EVIDENCE_MAX_AGE_DAYS * DAY_S
    for key in sorted(data.d["archive"]):
        if key < oldest:
            continue
        meas = station.get(key)
        if meas is None or meas[H["ghi"]] is None or meas[H["dhi"]] is None:
            continue
        day = local_date(key, tz)
        if day not in day_ok:
            day_ok[day] = usable_day(data, day, cutoff)
        if not day_ok[day]:
            continue
        for bucket, row in sorted(data.d["archive"][key].items()):
            if row[R["issued"]] >= cutoff:
                continue
            kt_m, kd_m = clearness(meas[H["ghi"]], meas[H["dhi"]], row[R["e0h"]])
            pred = predictors_from_row(key, row)
            hours.append(key)
            buckets.append(bucket)
            rows.append(pred.vector())
            ekt.append(kt_m - row[R["kt"]])
            ekd.append(kd_m - row[R["kd"]])
    return Evidence(
        hours=np.array(hours, dtype=np.int64),
        buckets=np.array(buckets, dtype=np.int64),
        x=np.array(rows, dtype=float).reshape(-1, len(PREDICTOR_NAMES)),
        err_kt=np.array(ekt, dtype=float),
        err_kd=np.array(ekd, dtype=float),
    )


def recency_weight(age_days: np.ndarray, target_doy: int, evidence_doy: np.ndarray) -> np.ndarray:
    weight = np.maximum(np.power(0.5, age_days / RECENCY_HALF_LIFE_DAYS), RECENCY_FLOOR)
    diff = np.abs(evidence_doy - target_doy)
    diff = np.minimum(diff, 365 - diff)
    season = (age_days >= SEASON_MIN_AGE_DAYS) & (diff <= SEASON_WINDOW_DAYS)
    return np.where(season, np.maximum(weight, SEASON_FLOOR), weight)


def _doy(keys: np.ndarray) -> np.ndarray:
    # Day of year from epoch seconds (UTC), vectorised and leap-year exact.
    days = keys.astype("datetime64[s]").astype("datetime64[D]")
    years = days.astype("datetime64[Y]")
    result: np.ndarray = (days - years).astype(np.int64) + 1
    return result


def _analog(ev: Evidence, mask: np.ndarray, vector: np.ndarray, target: int, pooled: bool) -> Correction:
    idx = np.nonzero(mask)[0]
    if idx.size == 0:
        return Correction(pooled=pooled)
    diff = (ev.x[idx] - vector) / PREDICTOR_SCALES
    dist = np.sqrt(np.sum(diff * diff, axis=1))
    within = dist <= MAX_DISTANCE
    idx, dist = idx[within], dist[within]
    if idx.size == 0:
        return Correction(pooled=pooled)
    # Deterministic order: distance, then hour, then bucket.
    order = np.lexsort((ev.buckets[idx], ev.hours[idx], dist))[:K_NEIGHBOURS]
    idx, dist = idx[order], dist[order]
    n = int(idx.size)
    if n < MIN_ANALOGS:
        return Correction(analogs=n, pooled=pooled)
    age = np.abs(target - ev.hours[idx]) / DAY_S
    target_doy = int(_doy(np.array([target]))[0])
    weight = (1.0 / (dist + DISTANCE_EPS)) * recency_weight(age, target_doy, _doy(ev.hours[idx]))
    total = float(weight.sum())
    mean_kt = float((weight * ev.err_kt[idx]).sum() / total)
    mean_kd = float((weight * ev.err_kd[idx]).sum() / total)
    n_eff = total * total / float((weight * weight).sum())
    shrink = n_eff / (n_eff + SHRINK_N0)
    top = tuple(
        (int(ev.hours[i]), int(ev.buckets[i]), round(float(d), 4), round(float(ev.err_kt[i]), 4))
        for i, d in zip(idx[:TOP_ANALOGS], dist[:TOP_ANALOGS], strict=True)
    )
    return Correction(
        shift_kt=_clamp(shrink * mean_kt, -MAX_SHIFT_KT, MAX_SHIFT_KT),
        shift_kd=_clamp(shrink * mean_kd, -MAX_SHIFT_KD, MAX_SHIFT_KD),
        mean_error_kt=mean_kt,
        mean_error_kd=mean_kd,
        analogs=n,
        n_eff=n_eff,
        shrink=shrink,
        pooled=pooled,
        top=top,
    )


def analog_correction(ev: Evidence, pred: Predictors, bucket: int) -> Correction:
    """Stage-1 correction of one forecast hour (§4, §5)."""
    if ev.size == 0:
        return NEUTRAL
    vector = pred.vector()
    result = _analog(ev, ev.buckets == bucket, vector, pred.key, pooled=False)
    if result.analogs >= MIN_ANALOGS:
        return result
    neighbours = (ev.buckets >= bucket - 1) & (ev.buckets <= bucket + 1)
    return _analog(ev, neighbours, vector, pred.key, pooled=True)


def compute_corrections(
    data: LearningData, predictors: Mapping[int, Predictors], now: datetime, tz: tzinfo
) -> dict[int, Correction]:
    """Corrections for all predictor hours at issue time ``now``."""
    now_ts = int(now.timestamp())
    evidence = build_evidence(data, data.evidence_cutoff(now_ts), tz)
    result = {}
    for key, pred in predictors.items():
        bucket = lead_bucket((key - now_ts) / HOUR_S)
        if bucket is None:
            continue
        result[key] = analog_correction(evidence, pred, bucket)
    return result


def correction_from_row(row: list[Any]) -> tuple[float, float]:
    return float(row[R["shift_kt"]]), float(row[R["shift_kd"]])


# --------------------------------------------------------------------------
# Applying corrections to the synthesis input
# --------------------------------------------------------------------------


def _adjust(kt: float, kd: float, shift_kt: float, shift_kd: float, factor: float) -> tuple[float, float]:
    kt_c = _clamp(kt + shift_kt, 0.0, CLEARNESS_INDEX_MAX)
    kd_c = _clamp(kd + shift_kd, KD_MIN, 1.0) if shift_kd != 0.0 else kd
    return _clamp(kt_c * factor, 0.0, CLEARNESS_INDEX_MAX), kd_c


def stage2_factor_for(stage2: Mapping[str, Any] | None, kt: float, temp: float | None = None) -> float:
    if not stage2:
        return 1.0
    item = stage2.get(stage2_cell(kt, temp))
    if not isinstance(item, Mapping):
        return 1.0
    return float(_clamp(float(item.get("factor", 1.0)), STAGE2_MIN, STAGE2_MAX))


def apply_corrections(
    records: Mapping[datetime, HourRecord],
    shifts: Mapping[int, tuple[float, float]],
    stage2: Mapping[str, Any] | None,
    latitude: float,
    longitude: float,
    temps: Mapping[int, float] | None = None,
) -> tuple[dict[datetime, HourRecord], dict[str, Any]]:
    """Return corrected copies of the hour records.

    Hours without a shift entry, without an average, or with the sun below
    MIN_ELEVATION_DEG are returned **unchanged (same object)**; a shift of
    0 with factor 1 also leaves the record untouched (parity).
    """
    out: dict[datetime, HourRecord] = {}
    changed = 0
    energy_raw = energy_new = 0.0
    for start, record in records.items():
        key = hour_key(start)
        shift = shifts.get(key)
        raw_ghi, raw_dhi = record.ghi, record.dhi
        if shift is None or raw_ghi is None or raw_dhi is None:
            out[start] = record
            continue
        e0h, elevation = hour_geometry(key, latitude, longitude)
        if elevation < MIN_ELEVATION_DEG or e0h <= 0:
            out[start] = record
            continue
        kt, kd = clearness(raw_ghi, raw_dhi, e0h)
        kt_shifted = _clamp(kt + shift[0], 0.0, CLEARNESS_INDEX_MAX)
        factor = stage2_factor_for(
            stage2, kt_shifted, cell_temperature((temps or {}).get(key), kt_shifted * e0h)
        )
        if shift[0] == 0.0 and shift[1] == 0.0 and factor == 1.0:
            out[start] = record
            continue
        kt_c, kd_c = _adjust(kt, kd, shift[0], shift[1], factor)
        ghi = min(kt_c * e0h, IRRADIANCE_MAX_W_M2)
        dhi = kd_c * ghi
        beam_old = max(raw_ghi - raw_dhi, 0.0)
        dni = record.dni if record.dni is not None else 0.0
        if beam_old > 0:
            dni = min(dni * (ghi - dhi) / beam_old, IRRADIANCE_MAX_W_M2)
        else:  # no beam before: derive from the mean geometry (diagnostic only)
            dni = min(max(ghi - dhi, 0.0) / max(e0h / 1361.0, 0.05), IRRADIANCE_MAX_W_M2)
        ghi_i = dni_i = dhi_i = None
        raw_ghi_i, raw_dhi_i = record.ghi_instant, record.dhi_instant
        if raw_ghi_i is not None and raw_dhi_i is not None:
            ghi_i, dni_i, dhi_i = raw_ghi_i, record.dni_instant, raw_dhi_i
            sun = sun_position(start, latitude, longitude)
            if sun.elevation_deg >= MIN_ELEVATION_DEG:
                e0i = sun.extraterrestrial_normal * sun.cos_zenith
                kt_i, kd_i = clearness(raw_ghi_i, raw_dhi_i, e0i)
                kt_ic, kd_ic = _adjust(kt_i, kd_i, shift[0], shift[1], factor)
                beam_i_old = max(raw_ghi_i - raw_dhi_i, 0.0)
                ghi_i = min(kt_ic * e0i, IRRADIANCE_MAX_W_M2)
                dhi_i = kd_ic * ghi_i
                dni_i = (
                    min((record.dni_instant or 0.0) * (ghi_i - dhi_i) / beam_i_old, IRRADIANCE_MAX_W_M2)
                    if beam_i_old > 0 else max(ghi_i - dhi_i, 0.0) / max(sun.cos_zenith, 0.05)
                )
                dni_i = min(dni_i, IRRADIANCE_MAX_W_M2)
        out[start] = HourRecord(
            start=record.start,
            ghi=round(ghi, 3), dni=round(dni, 3), dhi=round(dhi, 3),
            ghi_instant=None if ghi_i is None else round(ghi_i, 3),
            dni_instant=None if dni_i is None else round(dni_i, 3),
            dhi_instant=None if dhi_i is None else round(dhi_i, 3),
            sources=record.sources,
        )
        changed += 1
        energy_raw += raw_ghi
        energy_new += ghi
    return out, {
        "hours_changed": changed,
        "ghi_sum_raw": round(energy_raw, 1),
        "ghi_sum_corrected": round(energy_new, 1),
    }


# --------------------------------------------------------------------------
# Stage 2: per-group calibration
# --------------------------------------------------------------------------


def shaded_hour(modelled: float, unshaded: float | None) -> bool:
    """True when the geometry removes more than STAGE2_SHADE_MAX_LOSS of the
    hour's modelled energy. Unknown (no unshaded value) counts as unshaded."""
    if unshaded is None or unshaded <= 0:
        return False
    return modelled < (1.0 - STAGE2_SHADE_MAX_LOSS) * unshaded


def stage2_factors(
    data: LearningData,
    group: str,
    now: datetime,
    tz: tzinfo,
    latitude: float,
    longitude: float,
) -> dict[str, dict[str, float]]:
    """Calibration factor per cell (sky type × cell-temperature band, §6).

    0.1.34.2: all stored hours are used (retention 2 years), weighted by
    age like stage 1 (half-life 90 days, never below 10 %), so a cell
    learned last summer keeps its factor until new evidence changes it.
    Shrinkage counts the hours of the cell, not their weights. Hours the
    shading geometry darkens by more than STAGE2_SHADE_MAX_LOSS are left
    out, so a geometry error does not spread to unshaded hours.
    """
    now_ts = int(now.timestamp())
    cutoff = data.evidence_cutoff(now_ts)
    production = data.d["production"].get(group, {})
    model = data.d["model"].get(group, {})
    model_free = data.d["model_free"].get(group, {})
    sums = {cell: [0.0, 0.0, 0, 0.0, 0.0] for cell in STAGE2_CELLS}  # w·prod, w·model, n, prod, model
    day_ok: dict[str, bool] = {}
    for key, modelled in model.items():
        if key >= now_ts:
            continue
        produced = production.get(key)
        meas = data.d["station_hours"].get(key)
        if produced is None or meas is None or meas[H["ghi"]] is None:
            continue
        day = local_date(key, tz)
        if day not in day_ok:
            day_ok[day] = usable_day(data, day, cutoff, "stage2")
        if not day_ok[day]:
            continue
        if shaded_hour(modelled, model_free.get(key)):
            continue
        e0h, elevation = hour_geometry(key, latitude, longitude)
        if elevation < STAGE2_MIN_ELEVATION_DEG:
            continue
        kt_m, _ = clearness(meas[H["ghi"]], meas[H["dhi"]] or 0.0, e0h)
        weight = max(0.5 ** ((now_ts - key) / DAY_S / RECENCY_HALF_LIFE_DAYS), RECENCY_FLOOR)
        item = sums[stage2_cell(kt_m, cell_temperature(meas[H["temp"]], meas[H["ghi"]]))]
        item[0] += weight * produced
        item[1] += weight * modelled
        item[2] += 1
        item[3] += produced
        item[4] += modelled
    result = {}
    for cell, (w_produced, w_modelled, n, produced, modelled) in sums.items():
        ratio = w_produced / w_modelled if w_modelled > 0.05 else 1.0
        factor = 1.0 + (ratio - 1.0) * n / (n + STAGE2_N0)
        result[cell] = {
            "factor": round(_clamp(factor, STAGE2_MIN, STAGE2_MAX), 4),
            "ratio": round(ratio, 4),
            "hours": n,
            "production_kwh": round(produced, 3),
            "model_kwh": round(modelled, 3),
        }
    return result


# --------------------------------------------------------------------------
# Quality control
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupInfo:
    name: str
    kwp: float
    members: tuple[str, ...]


def decide_day(
    data: LearningData,
    day: str,
    groups: Iterable[GroupInfo],
    tz: tzinfo,
    latitude: float,
    longitude: float,
    now: datetime,
) -> tuple[str | None, dict[str, Any]]:
    """Quality decision for a finished local day (§7).

    Returns ``(None, details)`` while the decision has to wait (production
    or the station model not yet available, at most MODEL_WAIT_DAYS).
    """
    hours = local_day_hours(day, tz)
    details: dict[str, Any] = {}
    daylight = [k for k in hours if hour_geometry(k, latitude, longitude)[1] >= MIN_ELEVATION_DEG]
    station = data.d["station_hours"]
    complete = [k for k in daylight if station.get(k) is not None and station[k][H["ghi"]] is not None]
    details["station_hours"] = f"{len(complete)}/{len(daylight)}"
    if not daylight or len(complete) < DAY_STATION_COMPLETENESS * len(daylight):
        return "station_incomplete", details
    for key in complete:
        ghi, refl = station[key][H["ghi"]], station[key][H["refl"]]
        if refl is not None and ghi > SNOW_MIN_GHI and refl / ghi > SNOW_ALBEDO:
            details["albedo"] = round(refl / ghi, 3)
            return "snow", details
    age_days = (now.astimezone(tz).date() - date.fromisoformat(day)).days
    may_wait = age_days <= MODEL_WAIT_DAYS
    for group in groups:
        production = data.d["production"].get(group.name, {})
        values = [production.get(k) for k in hours]
        if any(v is None for v in values):
            if may_wait:
                return None, details
            details["group"] = group.name
            return "production_missing", details
        limit = COUNTER_STEP_FACTOR * group.kwp
        if any(v < 0 or v > limit for v in values):
            details["group"] = group.name
            return "production_fault", details
        model = data.d["model"].get(group.name, {})
        modelled = [model.get(k) for k in daylight]
        if any(v is None for v in modelled):
            if may_wait:
                return None, details
            details.setdefault("ratio_unchecked", []).append(group.name)
            continue
        model_sum = sum(modelled)
        if model_sum < DAY_RATIO_MIN_MODEL_KWH:
            continue
        ratio = sum(production[k] for k in daylight) / model_sum
        details.setdefault("ratio", {})[group.name] = round(ratio, 3)
        if not DAY_RATIO_MIN <= ratio <= DAY_RATIO_MAX:
            return "not_representative", details
    return DAY_OK, details


def representativeness(
    data: LearningData,
    groups: Iterable[GroupInfo],
    now: datetime,
    latitude: float,
    longitude: float,
) -> dict[str, Any]:
    """Hourly correlation of total production with station GHI (14 days)."""
    now_ts = int(now.timestamp())
    names = [g.name for g in groups]
    xs, ys = [], []
    for key, meas in data.d["station_hours"].items():
        if key < now_ts - REPRESENTATIVE_WINDOW_DAYS * DAY_S or key >= now_ts or meas[H["ghi"]] is None:
            continue
        if hour_geometry(key, latitude, longitude)[1] < STAGE2_MIN_ELEVATION_DEG:
            continue
        values = [data.d["production"].get(name, {}).get(key) for name in names]
        if not names or any(v is None for v in values):
            continue
        xs.append(meas[H["ghi"]])
        ys.append(sum(values))
    n = len(xs)
    if n < REPRESENTATIVE_MIN_HOURS:
        return {"r": None, "hours": n, "representative": True}
    x, y = np.array(xs), np.array(ys)
    if float(x.std()) == 0.0 or float(y.std()) == 0.0:
        return {"r": None, "hours": n, "representative": True}
    r = float(np.corrcoef(x, y)[0, 1])
    return {"r": round(r, 4), "hours": n, "representative": r >= REPRESENTATIVE_MIN_R}


# --------------------------------------------------------------------------
# Skill gate and state
# --------------------------------------------------------------------------


def capture_issue6(
    data: LearningData,
    predictors: Mapping[int, Predictors],
    corrections: Mapping[int, Correction],
    now: datetime,
    tz: tzinfo,
) -> bool:
    """Keep the forecast of the first run at/after 06:00 local per day."""
    local_now = now.astimezone(tz)
    if local_now.hour < SUNRISE_ISSUE_HOUR:
        return False
    day = local_now.date().isoformat()
    if day in data.d["issue6"]:
        return False
    hours = {}
    for key in local_day_hours(day, tz):
        pred = predictors.get(key)
        if pred is None:
            continue
        corr = corrections.get(key) or NEUTRAL
        kt_c, kd_c = _adjust(pred.kt, pred.kd, corr.shift_kt, corr.shift_kd, 1.0)
        hours[key] = [round(pred.kt, 6), round(kt_c, 6), round(pred.kd, 6), round(kd_c, 6), round(pred.e0h, 3)]
    data.d["issue6"][day] = {"captured": int(now.timestamp()), "hours": hours}
    return True


def record_entry_day(
    data: LearningData, entry_id: str, day: str, raw_wh: float, corr_wh: float, now: datetime, tz: tzinfo
) -> bool:
    """Keep each entry's first forecast of ``day`` made at/after 06:00 local
    on that same day (the energy half of the gate)."""
    local_now = now.astimezone(tz)
    if local_now.hour < SUNRISE_ISSUE_HOUR or local_now.date().isoformat() != day:
        return False
    entries = data.d["entry_days"].setdefault(day, {})
    if entry_id in entries:
        return False
    entries[entry_id] = {"captured": int(now.timestamp()), "raw": float(raw_wh), "corr": float(corr_wh)}
    return True


def evaluate_gate(
    data: LearningData,
    groups: Iterable[GroupInfo],
    now: datetime,
    tz: tzinfo,
) -> dict[str, Any]:
    """Out-of-sample skill of the 06:00 forecasts over the last 28 days."""
    today = now.astimezone(tz).date()
    groups = list(groups)
    err_raw: list[float] = []
    err_corr: list[float] = []
    days_irr = 0
    energy: dict[str, dict[str, list[float]]] = {g.name: {"raw": [], "corr": []} for g in groups}
    station = data.d["station_hours"]
    for offset in range(1, GATE_WINDOW_DAYS + 1):
        day = (today - timedelta(days=offset)).isoformat()
        decision = data.d["days"].get(day)
        if decision is None or decision["status"] != DAY_OK:
            continue
        issue = data.d["issue6"].get(day)
        if issue:
            raw_d, corr_d = [], []
            for key, (kt, kt_c, _kd, _kd_c, e0h) in issue["hours"].items():
                meas = station.get(key)
                if meas is None or meas[H["ghi"]] is None:
                    continue
                raw_d.append((kt * e0h) - meas[H["ghi"]])
                corr_d.append((kt_c * e0h) - meas[H["ghi"]])
            if len(raw_d) >= GATE_MIN_HOURS_PER_DAY:
                days_irr += 1
                err_raw += raw_d
                err_corr += corr_d
        entries = data.d["entry_days"].get(day, {})
        hours = local_day_hours(day, tz)
        for group in groups:
            if not group.members or any(m not in entries for m in group.members):
                continue
            production = data.d["production"].get(group.name, {})
            values = [production.get(k) for k in hours]
            if any(v is None for v in values):
                continue
            actual = sum(values)
            raw = sum(entries[m]["raw"] for m in group.members) / 1000.0
            corr = sum(entries[m]["corr"] for m in group.members) / 1000.0
            energy[group.name]["raw"].append(abs(raw - actual))
            energy[group.name]["corr"].append(abs(corr - actual))
    rmse_raw = math.sqrt(sum(e * e for e in err_raw) / len(err_raw)) if err_raw else None
    rmse_corr = math.sqrt(sum(e * e for e in err_corr) / len(err_corr)) if err_corr else None
    improvement = (
        (rmse_raw - rmse_corr) / rmse_raw if rmse_raw and rmse_corr is not None else None
    )
    group_result = {}
    reasons = []
    energy_ok = True
    for group in groups:
        raw_list, corr_list = energy[group.name]["raw"], energy[group.name]["corr"]
        days = len(raw_list)
        mae_raw = sum(raw_list) / days if days else None
        mae_corr = sum(corr_list) / days if days else None
        ok = (
            days >= GATE_MIN_DAYS
            and mae_raw is not None
            and mae_corr is not None
            and mae_corr <= mae_raw * (1 + GATE_ENERGY_TOLERANCE) + 1e-9
        )
        if days < GATE_MIN_DAYS:
            reasons.append(f"{group.name}: {days}/{GATE_MIN_DAYS} days with energy data")
        elif not ok:
            reasons.append(f"{group.name}: daily energy error would get worse")
        energy_ok = energy_ok and ok
        group_result[group.name] = {
            "days": days,
            "mae_raw_kwh": None if mae_raw is None else round(mae_raw, 3),
            "mae_corrected_kwh": None if mae_corr is None else round(mae_corr, 3),
            "passed": ok,
        }
    if not groups:
        energy_ok = False
        reasons.append("no inverter group configured")
    irr_ok = days_irr >= GATE_MIN_DAYS and improvement is not None and improvement >= GATE_MIN_IMPROVEMENT
    if days_irr < GATE_MIN_DAYS:
        reasons.insert(0, f"{days_irr}/{GATE_MIN_DAYS} days with a complete 06:00 forecast and measurement")
    elif not irr_ok:
        reasons.insert(0, f"irradiance improvement {improvement:.1%} below {GATE_MIN_IMPROVEMENT:.0%}"
                       if improvement is not None else "no irradiance comparison possible")
    return {
        "evaluated": now.astimezone(timezone.utc).isoformat(),
        "algorithm": ALGORITHM_VERSION,
        "days": days_irr,
        "hours": len(err_raw),
        "rmse_raw_w_m2": None if rmse_raw is None else round(rmse_raw, 2),
        "rmse_corrected_w_m2": None if rmse_corr is None else round(rmse_corr, 2),
        "improvement": None if improvement is None else round(improvement, 4),
        "groups": group_result,
        "passed": bool(irr_ok and energy_ok),
        "reasons": reasons,
    }


def derive_state(
    gate: Mapping[str, Any] | None,
    apply_switch: bool,
    representative: bool,
    ever_passed: bool,
) -> tuple[str, str]:
    """State and reason (§9). Only ``applied`` changes the forecast."""
    if not representative:
        return STATE_PAUSED, "station not representative of the house (correlation below limit)"
    if gate is None or gate.get("algorithm") != ALGORITHM_VERSION:
        return STATE_COLLECTING, "no skill evaluation for this algorithm version yet"
    if gate.get("passed"):
        if apply_switch:
            return STATE_APPLIED, "skill gate passed, switch on"
        return STATE_ELIGIBLE, "skill gate passed, switch off"
    reason = "; ".join(gate.get("reasons") or []) or "skill gate not passed"
    if ever_passed:
        return STATE_PAUSED, "skill gate failed: " + reason
    if (gate.get("days") or 0) < GATE_MIN_DAYS:
        return STATE_COLLECTING, reason
    return STATE_SHADOW, reason


# --------------------------------------------------------------------------
# Export (CSV/JSON text; the HA layer writes the files)
# --------------------------------------------------------------------------


def _csv(rows: Iterable[Iterable[Any]], header: Iterable[str]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(header))
    for row in rows:
        writer.writerow(["" if v is None else (repr(v) if isinstance(v, float) else v) for v in row])
    return buffer.getvalue()


def export_archive_csv(data: LearningData, year: int) -> str:
    rows = []
    for key in sorted(data.d["archive"]):
        when = key_to_dt(key)
        if when.year != year:
            continue
        for bucket, row in sorted(data.d["archive"][key].items()):
            rows.append([when.isoformat(), BUCKET_NAMES[bucket], key_to_dt(row[0]).isoformat(), *row[1:]])
    return _csv(rows, ["hour_utc", "lead_bucket", "issued_utc", *ROW_FIELDS[1:]])


def export_station_csv(data: LearningData, year: int) -> str:
    rows = [
        [key_to_dt(k).isoformat(), *v]
        for k, v in sorted(data.d["station_hours"].items())
        if key_to_dt(k).year == year
    ]
    return _csv(rows, ["hour_utc", *HOUR_FIELDS])


def export_days_csv(data: LearningData, groups: Iterable[GroupInfo], tz: tzinfo) -> str:
    groups = list(groups)
    rows = []
    days = sorted(set(data.d["days"]) | set(data.d["entry_days"]) | set(data.d["issue6"]))
    for day in days:
        decision = data.d["days"].get(day) or {}
        hours = local_day_hours(day, tz)
        entries = data.d["entry_days"].get(day, {})
        row: list[Any] = [
            day,
            decision.get("status"),
            key_to_dt(decision["decided"]).isoformat() if decision.get("decided") else None,
            json.dumps(decision.get("details", {}), sort_keys=True) if decision else None,
        ]
        for group in groups:
            production = data.d["production"].get(group.name, {})
            model = data.d["model"].get(group.name, {})
            values = [production.get(k) for k in hours]
            actual = None if any(v is None for v in values) else round(sum(values), 3)
            modelled = [model.get(k) for k in hours if model.get(k) is not None]
            have = all(m in entries for m in group.members) and group.members
            raw = round(sum(entries[m]["raw"] for m in group.members) / 1000, 3) if have else None
            corr = round(sum(entries[m]["corr"] for m in group.members) / 1000, 3) if have else None
            row += [raw, corr, actual, round(sum(modelled), 3) if modelled else None]
        rows.append(row)
    header = ["date", "status", "decided_utc", "details"]
    for group in groups:
        header += [
            f"{group.name}_forecast_raw_kwh", f"{group.name}_forecast_corrected_kwh",
            f"{group.name}_actual_kwh", f"{group.name}_model_from_station_kwh",
        ]
    return _csv(rows, header)


def export_issue6_csv(data: LearningData) -> str:
    rows = []
    for day, item in sorted(data.d["issue6"].items()):
        for key, values in sorted(item["hours"].items()):
            rows.append([day, key_to_dt(item["captured"]).isoformat(), key_to_dt(key).isoformat(), *values])
    return _csv(rows, ["date", "captured_utc", "hour_utc", "kt_forecast", "kt_corrected",
                       "kd_forecast", "kd_corrected", "e0h_w_m2"])


def export_events_csv(data: LearningData) -> str:
    return _csv(data.d["events"], ["time_utc", "kind", "message"])


def export_production_csv(data: LearningData, year: int) -> str:
    groups = sorted(set(data.d["production"]) | set(data.d["model"]))
    keys = sorted({k for g in groups for k in data.d["production"].get(g, {})}
                  | {k for g in groups for k in data.d["model"].get(g, {})})
    rows = []
    for key in keys:
        if key_to_dt(key).year != year:
            continue
        row: list[Any] = [key_to_dt(key).isoformat()]
        for group in groups:
            row += [data.d["production"].get(group, {}).get(key), data.d["model"].get(group, {}).get(key),
                    data.d["model_free"].get(group, {}).get(key)]
        rows.append(row)
    header = ["hour_utc"]
    for group in groups:
        header += [f"{group}_production_kwh", f"{group}_model_from_station_kwh",
                   f"{group}_model_unshaded_kwh"]
    return _csv(rows, header)


def export_settings(data: LearningData, groups: Iterable[GroupInfo], tz: tzinfo, latitude: float,
                    longitude: float) -> dict[str, Any]:
    """Everything the replay tool needs besides the CSV files."""
    return {
        "algorithm": ALGORITHM_VERSION,
        "timezone": str(tz),
        "latitude": latitude,
        "longitude": longitude,
        "freeze": data.d["freeze"],
        "resets": data.d["resets"],
        "purged_before": data.d["purged_before"],
        "created": data.d["created"],
        "settings": data.d["settings"],
        "groups": [{"name": g.name, "kwp": g.kwp, "members": list(g.members)} for g in groups],
        "parameters": {
            "lead_buckets_h": LEAD_BUCKETS,
            "predictor_scales": PREDICTOR_SCALES.tolist(),
            "k_neighbours": K_NEIGHBOURS,
            "max_distance": MAX_DISTANCE,
            "min_analogs": MIN_ANALOGS,
            "distance_eps": DISTANCE_EPS,
            "shrink_n0": SHRINK_N0,
            "recency_half_life_days": RECENCY_HALF_LIFE_DAYS,
            "season_min_age_days": SEASON_MIN_AGE_DAYS,
            "season_window_days": SEASON_WINDOW_DAYS,
            "season_floor": SEASON_FLOOR,
            "recency_floor": RECENCY_FLOOR,
            "max_shift_kt": MAX_SHIFT_KT,
            "max_shift_kd": MAX_SHIFT_KD,
            "evidence_max_age_days": EVIDENCE_MAX_AGE_DAYS,
            "retention_days": RETENTION_DAYS,
            "gate_window_days": GATE_WINDOW_DAYS,
            "gate_min_hours_per_day": GATE_MIN_HOURS_PER_DAY,
        },
        "stage2": data.d["stage2"],
        "gate": data.d["gate"],
        "state": data.d["state"],
        "representativeness": data.d["representativeness"],
    }
