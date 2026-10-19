"""Same-day correction engine (0.1.35): today's measured production steers
the rest of today's forecast.

Design record: ``OMSF_v0_1_35_Same_Day_Spec.md``. Pure like ``learning.py``:
no Home Assistant imports, no I/O, no clock reads.

Quantity
--------
k = GHI / GHI_clear, the clear-sky index of horizontal light (Haurwitz clear
sky). Forecast k_fc(h) per hour comes from the served forecast light. The
house's measured k comes from the inverters' energy counters, through the
members' own PV model: the model is run on clear-sky-shaped light scaled to
a grid of k values (diffuse split by Erbs), giving per 15-minute band and
inverter group a monotone response curve E(k). Inverting the summed curves
at the measured energy gives the k at the house.

Error model
-----------
e(t) = k(t) - k_fc(t). The error measured over the last hour, ê, predicts the
error Δ hours later as β(Δ)·ê (least-squares through the origin, per lead
bucket and situation, shrunk toward the prior 0.5^(Δ/3 h)). The corrected
index is k̂ = clamp(k_fc + β·ê, 0, K_MAX). The cautious index subtracts
z·σ(Δ) (z = 0.8416, one-sided 80 %), σ from the regression residuals.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

import numpy as np

from .learning import (
    CLEARNESS_INDEX_MAX,
    HOUR_S,
    KD_MIN,
    MIN_ELEVATION_DEG,
    _clamp,
    clearness,
    hour_geometry,
    hour_key,
    key_to_dt,
)
from .local_source import HourRecord, _hour_sample_moments, sun_position

# --------------------------------------------------------------------------
# Parameters (specification section in brackets)
# --------------------------------------------------------------------------

BAND_S = 900
K_MAX = 1.05  # (3.1) cloud enhancement over an hour stays small
K_GRID = np.array([0.0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, K_MAX])
"""Response-curve grid; denser at low k, where grey days live."""
WINDOW_BANDS = 4  # (3.1) the last hour
MIN_WINDOW_BANDS = 2
SIGNAL_AFTER_SUNRISE_S = 1800  # East (5K) waits 30 min after sunrise (owner data)
MIN_WINDOW_CLEAR_KWH = 0.3  # (3.1) counter resolution 0.01 kWh -> |Δk| <= 0.033 per counter
BAND_MIN_CLEAR_KWH = 0.05  # bands below this carry no usable k
COUNTER_STALE_S = 1200
COUNTER_STEP_FACTOR = 1.2  # kWh per kWp and hour (as the production check)
MAX_SHIFT_KT = 0.6  # (3.1)
LEAD_BUCKETS_H = ((0.0, 1.0), (1.0, 2.0), (2.0, 4.0), (4.0, 8.0), (8.0, 16.0))
BUCKET_LABELS = ("0-1h", "1-2h", "2-4h", "4-8h", "8-16h")
PRIOR_HALF_LIFE_H = 3.0
PRIOR_DAYS = 5.0
BETA_MIN, BETA_MAX = 0.0, 1.0
BETA_WINDOW_DAYS = 60
SITUATIONS = ("improving", "other")
IMPROVING_DELTA_K = 0.15
IMPROVING_HORIZON_H = 3.0
CAUTIOUS_Z = 0.8416  # one-sided 80 % normal quantile -> 20th percentile
CAUTIOUS_MIN_DAYS = 10
DECISION_AFTER_SUNRISE_S = 3600  # the owner's automation runs at sunrise + 1 h
DECISION_LAST_LOCAL = time(14, 0)
EVAL_TIMES_LOCAL = (time(10, 0), time(12, 0))  # plus the first decision (sunrise + 1 h)
USABLE_BAND_SHARE = 0.8
GATE_WINDOW_DAYS = 28
GATE_MIN_DAYS = 7
GATE_MIN_IMPROVEMENT = 0.05
GATE_RECENT_DAYS = 7
GATE_RECENT_BETTER = 5
RETENTION_DAYS = 120


def _prior_beta(bucket: int) -> float:
    low, high = LEAD_BUCKETS_H[bucket]
    return 0.5 ** (((low + high) / 2.0) / PRIOR_HALF_LIFE_H)


PRIOR_BETA = tuple(round(_prior_beta(i), 6) for i in range(len(LEAD_BUCKETS_H)))


# --------------------------------------------------------------------------
# Geometry: clear sky, diffuse split, sunrise
# --------------------------------------------------------------------------

_CLEAR_CACHE: dict[tuple[int, float, float], float] = {}


def clear_kt(key: int, latitude: float, longitude: float) -> float:
    """Clear-sky clearness index of the hour (Haurwitz GHI over the same
    sample instants as ``hour_geometry``); 0 when the sun is down."""
    cache_key = (key, round(latitude, 4), round(longitude, 4))
    cached = _CLEAR_CACHE.get(cache_key)
    if cached is not None:
        return cached
    e0h, _ = hour_geometry(key, latitude, longitude)
    total = 0.0
    moments = _hour_sample_moments(key_to_dt(key))
    for moment in moments:
        cz = sun_position(moment, latitude, longitude).cos_zenith
        if cz > 0.01:
            total += 1098.0 * cz * math.exp(-0.057 / cz)
    ghi_clear = total / len(moments)
    result = min(ghi_clear / e0h, CLEARNESS_INDEX_MAX) if e0h > 1.0 else 0.0
    if len(_CLEAR_CACHE) > 20000:
        _CLEAR_CACHE.clear()
    _CLEAR_CACHE[cache_key] = result
    return result


def erbs(kt: float) -> float:
    """Diffuse fraction (Erbs, Klein & Duffie 1982)."""
    if kt <= 0.22:
        kd = 1.0 - 0.09 * kt
    elif kt <= 0.80:
        kd = 0.9511 - 0.1604 * kt + 4.388 * kt**2 - 16.638 * kt**3 + 12.336 * kt**4
    else:
        kd = 0.165
    return _clamp(kd, KD_MIN, 1.0)


def forecast_k(record: HourRecord, latitude: float, longitude: float) -> float | None:
    """Clear-sky index of a forecast hour (None: no light data or sun down)."""
    if record.ghi is None or record.dhi is None:
        return None
    key = hour_key(record.start)
    e0h, elevation = hour_geometry(key, latitude, longitude)
    kt_clear = clear_kt(key, latitude, longitude)
    if elevation < MIN_ELEVATION_DEG or kt_clear <= 0 or e0h <= 0:
        return None
    kt, _ = clearness(record.ghi, record.dhi, e0h)
    return _clamp(kt / kt_clear, 0.0, K_MAX)


# --------------------------------------------------------------------------
# Response curves (light at grid k -> model energy per band)
# --------------------------------------------------------------------------


def curve_records(keys: Iterable[int], k: float, latitude: float, longitude: float) -> dict[datetime, HourRecord]:
    """Hour records with clear-sky-shaped light at clear-sky index ``k``
    (diffuse by Erbs; direct normal derived as for the station model)."""
    out: dict[datetime, HourRecord] = {}
    for key in keys:
        e0h, _ = hour_geometry(key, latitude, longitude)
        kt = _clamp(k * clear_kt(key, latitude, longitude), 0.0, CLEARNESS_INDEX_MAX)
        ghi = kt * e0h
        dhi = erbs(kt) * ghi
        dni = max(ghi - dhi, 0.0) / max(e0h / 1361.0, 0.05)
        start = key_to_dt(key)
        out[start] = HourRecord(start=start, ghi=round(ghi, 3), dni=round(min(dni, 1500.0), 3), dhi=round(dhi, 3))
    return out


@dataclass
class Curves:
    """Per band (UTC start, s) and group: model energy (kWh) on ``K_GRID``."""

    bands: dict[int, dict[str, np.ndarray]] = field(default_factory=dict)

    def add(self, group: str, k_index: int, band: int, kwh: float) -> None:
        per_group = self.bands.setdefault(band, {})
        curve = per_group.setdefault(group, np.zeros(len(K_GRID)))
        curve[k_index] += kwh

    def total(self, bands: Iterable[int], groups: Iterable[str] | None = None) -> np.ndarray:
        """Summed curve over bands (and groups)."""
        out = np.zeros(len(K_GRID))
        wanted = None if groups is None else set(groups)
        for band in bands:
            for group, curve in self.bands.get(band, {}).items():
                if wanted is None or group in wanted:
                    out += curve
        return out


def monotone(curve: np.ndarray) -> np.ndarray:
    """Running maximum: the model output can only rise with more light
    (guards against rounding in the library)."""
    return np.maximum.accumulate(curve)


def invert(curve: np.ndarray, energy: float) -> tuple[float, bool]:
    """k at which the (summed) response curve reaches ``energy``.

    Returns (k, saturated): saturated when the energy is at or above the
    curve's top (k = K_MAX) - the measurement says "at least this bright".
    """
    curve = monotone(curve)
    if energy <= curve[0]:
        return float(K_GRID[0]), False
    if energy >= curve[-1]:
        return float(K_GRID[-1]), True
    # first grid index whose value reaches the energy; interpolate linearly
    index = int(np.searchsorted(curve, energy, side="left"))
    low, high = curve[index - 1], curve[index]
    if high <= low:
        return float(K_GRID[index]), False
    fraction = (energy - low) / (high - low)
    return float(K_GRID[index - 1] + fraction * (K_GRID[index] - K_GRID[index - 1])), False


# --------------------------------------------------------------------------
# Counters: energy per band
# --------------------------------------------------------------------------


def counter_value(series: Sequence[tuple[float, float]], moment: float) -> float | None:
    """Counter reading at ``moment`` (step function: last value at or
    before it). ``series`` sorted by time."""
    value = None
    for ts, reading in series:
        if ts > moment:
            break
        value = reading
    return value


def band_energy(
    series: Sequence[tuple[float, float]], start: float, end: float, fresh_until: float, kwp: float
) -> float | None:
    """Energy (kWh) of [start, end) from a counter series, or None.

    Valid only when a reading exists at or before ``start`` and the counter
    is known to be current at ``end`` (``fresh_until``: last time the
    integration reported the state, even unchanged). A negative step or one
    above COUNTER_STEP_FACTOR x kWp x duration is refused.
    """
    if fresh_until < end:
        return None
    first, last = counter_value(series, start), counter_value(series, end)
    if first is None or last is None:
        return None
    step = last - first
    if step < 0 or step > COUNTER_STEP_FACTOR * max(kwp, 0.1) * (end - start) / HOUR_S + 0.011:
        return None
    return step


# --------------------------------------------------------------------------
# The measured error ê
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    """The same-day signal at one moment."""

    valid: bool
    reason: str
    e: float | None = None
    k_meas: float | None = None
    k_fc: float | None = None
    clear_kwh: float = 0.0
    measured_kwh: float = 0.0
    bands: tuple[int, ...] = ()
    saturated: bool = False
    situation: str = "other"

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "reason": self.reason,
            "e": None if self.e is None else round(self.e, 4),
            "k_measured": None if self.k_meas is None else round(self.k_meas, 4),
            "k_forecast": None if self.k_fc is None else round(self.k_fc, 4),
            "clear_kwh": round(self.clear_kwh, 3),
            "measured_kwh": round(self.measured_kwh, 3),
            "window": [key_to_dt(b).isoformat() for b in self.bands],
            "saturated": self.saturated,
            "situation": self.situation,
        }


def window_bands(now: datetime, sunrise: datetime | None) -> list[int]:
    """The last WINDOW_BANDS completed bands, not before sunrise + 30 min."""
    end = int(now.timestamp()) // BAND_S * BAND_S
    earliest = None if sunrise is None else int(sunrise.timestamp()) + SIGNAL_AFTER_SUNRISE_S
    bands = []
    for i in range(1, WINDOW_BANDS + 1):
        start = end - i * BAND_S
        if earliest is not None and start < earliest:
            break
        bands.append(start)
    return sorted(bands)


def situation(kfc: Mapping[int, float], now: datetime) -> str:
    """'improving' when the forecast expects clearly more sun within 3 h
    than in the current hour (the 'deck will clear' case)."""
    now_key = hour_key(now)
    current = kfc.get(now_key)
    if current is None:
        return "other"
    ahead = [v for k, v in kfc.items() if now_key < k <= now_key + IMPROVING_HORIZON_H * HOUR_S]
    if ahead and max(ahead) - current >= IMPROVING_DELTA_K:
        return "improving"
    return "other"


def measure_signal(
    curves: Curves,
    measured: Mapping[int, Mapping[str, float | None]],
    kfc_groups: Mapping[str, Mapping[int, float]],
    groups: Sequence[str],
    now: datetime,
    sunrise: datetime | None,
) -> Signal:
    """ê over the last hour, all groups together (the weather is one).

    ``measured``: band -> group -> kWh (None: unknown). ``kfc_groups``:
    group -> hour -> forecast k (each group's served light; stage 2 makes
    them differ slightly).
    """
    bands = window_bands(now, sunrise)
    joint = joint_kfc(curves, kfc_groups, groups)
    sit = situation(joint, now)
    if len(bands) < MIN_WINDOW_BANDS:
        return Signal(False, "too early (sunrise + 30 min, then two bands)", situation=sit)
    total_measured = 0.0
    for band in bands:
        for group in groups:
            value = (measured.get(band) or {}).get(group)
            if value is None:
                return Signal(False, f"no counter reading for {group}", bands=tuple(bands), situation=sit)
            total_measured += value
    curve = curves.total(bands, groups)
    clear = float(monotone(curve)[np.searchsorted(K_GRID, 1.0)])
    if not curve.any():
        return Signal(False, "no model response yet", bands=tuple(bands), situation=sit)
    if clear < MIN_WINDOW_CLEAR_KWH:
        return Signal(False, f"too little light in the window ({clear:.2f} kWh clear sky < "
                      f"{MIN_WINDOW_CLEAR_KWH})", clear_kwh=clear, measured_kwh=total_measured,
                      bands=tuple(bands), situation=sit)
    k_meas, saturated = invert(curve, total_measured)
    # forecast index of the window, weighted like the measurement (clear-sky energy)
    weight = value_sum = 0.0
    for band in bands:
        for group in groups:
            k_group = kfc_groups.get(group, {}).get(band // HOUR_S * HOUR_S)
            g_curve = curves.bands.get(band, {}).get(group)
            if k_group is None or g_curve is None:
                continue
            w = float(monotone(g_curve)[np.searchsorted(K_GRID, 1.0)])
            weight += w
            value_sum += w * k_group
    if weight <= 0:
        return Signal(False, "no forecast for the window", clear_kwh=clear, measured_kwh=total_measured,
                      bands=tuple(bands), situation=sit)
    k_fc = value_sum / weight
    return Signal(True, "ok", e=k_meas - k_fc, k_meas=k_meas, k_fc=k_fc, clear_kwh=clear,
                  measured_kwh=total_measured, bands=tuple(bands), saturated=saturated, situation=sit)


def joint_kfc(curves: Curves, kfc_groups: Mapping[str, Mapping[int, float]], groups: Sequence[str]) -> dict[int, float]:
    """Forecast k per hour, groups weighted by their clear-sky energy."""
    hours = sorted({h for g in groups for h in kfc_groups.get(g, {})})
    out: dict[int, float] = {}
    for hour in hours:
        weight = value = 0.0
        for group in groups:
            k = kfc_groups.get(group, {}).get(hour)
            if k is None:
                continue
            w = 0.0
            for band in range(hour, hour + HOUR_S, BAND_S):
                g_curve = curves.bands.get(band, {}).get(group)
                if g_curve is not None:
                    w += float(monotone(g_curve)[np.searchsorted(K_GRID, 1.0)])
            w = w if w > 0 else 1e-6
            weight += w
            value += w * k
        if weight > 0:
            out[hour] = value / weight
    return out


# --------------------------------------------------------------------------
# Applying the correction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Table:
    """β and σ per situation and lead bucket."""

    beta: Mapping[str, Sequence[float]]
    sigma: Mapping[str, Sequence[float | None]]
    days: Mapping[str, Sequence[int]]
    sigma_u: float | None
    usable_days: int

    @classmethod
    def prior(cls) -> Table:
        return cls(
            beta={s: list(PRIOR_BETA) for s in SITUATIONS},
            sigma={s: [None] * len(LEAD_BUCKETS_H) for s in SITUATIONS},
            days={s: [0] * len(LEAD_BUCKETS_H) for s in SITUATIONS},
            sigma_u=None,
            usable_days=0,
        )

    @classmethod
    def from_json(cls, raw: Mapping[str, Any] | None) -> Table:
        if not raw:
            return cls.prior()
        return cls(beta=raw["beta"], sigma=raw["sigma"], days=raw["days"], sigma_u=raw.get("sigma_u"),
                   usable_days=int(raw.get("usable_days", 0)))

    def to_json(self) -> dict[str, Any]:
        return {"beta": {s: [round(float(v), 6) for v in self.beta[s]] for s in SITUATIONS},
                "sigma": {s: [None if v is None else round(float(v), 6) for v in self.sigma[s]] for s in SITUATIONS},
                "days": {s: [int(v) for v in self.days[s]] for s in SITUATIONS},
                "sigma_u": None if self.sigma_u is None else round(float(self.sigma_u), 6),
                "usable_days": self.usable_days}

    @property
    def cautious_ready(self) -> bool:
        return self.usable_days >= CAUTIOUS_MIN_DAYS


def lead_bucket(lead_h: float) -> int | None:
    for index, (low, high) in enumerate(LEAD_BUCKETS_H):
        if low <= lead_h < high:
            return index
    return None


def corrected_k(k_fc: float, e: float | None, sit: str, lead_h: float, table: Table) -> float:
    if e is None:
        return k_fc
    bucket = lead_bucket(max(lead_h, 0.0))
    beta = 0.0 if bucket is None else float(table.beta[sit][bucket])
    return _clamp(k_fc + _clamp(beta, BETA_MIN, BETA_MAX) * e, 0.0, K_MAX)


def cautious_k(k_hat: float, e: float | None, sit: str, lead_h: float, table: Table) -> float:
    """k̂ - z·σ, never above k̂. σ: conditional residual spread when a valid
    signal exists, otherwise the unconditional spread."""
    bucket = lead_bucket(max(lead_h, 0.0))
    sigma: float | None
    if e is None or bucket is None:
        sigma = table.sigma_u
    else:
        sigma = table.sigma[sit][bucket]
        if sigma is None:
            sigma = table.sigma_u
    if sigma is None:
        return k_hat
    return _clamp(k_hat - CAUTIOUS_Z * float(sigma), 0.0, k_hat)


def shifts_for(
    hours: Mapping[datetime, HourRecord],
    target_k: Mapping[int, float],
    latitude: float,
    longitude: float,
) -> dict[int, tuple[float, float]]:
    """Light shifts (Δkt, Δkd) that move each hour to its target k; the
    diffuse fraction follows Erbs (grey -> more diffuse)."""
    out: dict[int, tuple[float, float]] = {}
    for start, record in hours.items():
        key = hour_key(start)
        target = target_k.get(key)
        if target is None or record.ghi is None or record.dhi is None:
            continue
        e0h, elevation = hour_geometry(key, latitude, longitude)
        kt_clear = clear_kt(key, latitude, longitude)
        if elevation < MIN_ELEVATION_DEG or e0h <= 0 or kt_clear <= 0:
            continue
        kt, _ = clearness(record.ghi, record.dhi, e0h)
        shift_kt = _clamp(target * kt_clear - kt, -MAX_SHIFT_KT, MAX_SHIFT_KT)
        new_kt = _clamp(kt + shift_kt, 0.0, CLEARNESS_INDEX_MAX)
        shift_kd = erbs(new_kt) - erbs(kt)
        if abs(shift_kt) < 1e-6:
            continue
        out[key] = (round(shift_kt, 6), round(shift_kd, 6))
    return out


def plan(
    hours: Mapping[datetime, HourRecord],
    signal: Signal | None,
    table: Table,
    now: datetime,
    tz: tzinfo,
    latitude: float,
    longitude: float,
) -> tuple[dict[int, tuple[float, float]], dict[int, tuple[float, float]] | None]:
    """(served shifts, cautious shifts or None) for today's remaining hours.

    Past hours and other days are never touched. Without a valid signal the
    served shifts are empty; cautious bands still use the unconditional
    spread once enough days exist.
    """
    today = now.astimezone(tz).date()
    now_ts = now.timestamp()
    e = signal.e if signal is not None and signal.valid else None
    sit = signal.situation if signal is not None else "other"
    served: dict[int, float] = {}
    cautious: dict[int, float] = {}
    for start, record in hours.items():
        if start.astimezone(tz).date() != today or start.timestamp() + HOUR_S <= now_ts:
            continue
        k_fc = forecast_k(record, latitude, longitude)
        if k_fc is None:
            continue
        lead_h = (start.timestamp() + HOUR_S / 2 - now_ts) / HOUR_S
        k_hat = corrected_k(k_fc, e, sit, lead_h, table)
        if e is not None:
            served[hour_key(start)] = k_hat
        if table.cautious_ready:
            cautious[hour_key(start)] = cautious_k(k_hat, e, sit, lead_h, table)
    served_shifts = shifts_for(hours, served, latitude, longitude)
    cautious_shifts = shifts_for(hours, cautious, latitude, longitude) if table.cautious_ready else None
    return served_shifts, cautious_shifts


# --------------------------------------------------------------------------
# Decision times
# --------------------------------------------------------------------------


def decision_times(day: str, tz: tzinfo, sunrise: datetime | None) -> list[datetime]:
    """Sunrise + 1 h, then every full and half hour up to 14:00 local."""
    if sunrise is None:
        return []
    first = sunrise + timedelta(seconds=DECISION_AFTER_SUNRISE_S)
    last = datetime.combine(date.fromisoformat(day), DECISION_LAST_LOCAL, tz)
    if first > last:
        return []
    out = [first]
    slot = first.astimezone(tz).replace(second=0, microsecond=0)
    slot = slot.replace(minute=30 if slot.minute < 30 else 0)
    if slot.minute == 0:
        slot += timedelta(hours=1)
    while slot <= last:
        out.append(slot.astimezone(timezone.utc))
        slot += timedelta(minutes=30)
    return out


def eval_decisions(day: str, tz: tzinfo, decisions: Iterable[int]) -> list[int]:
    """The decision records the gate judges: the first one (sunrise + 1 h)
    and the first at/after 10:00 and 12:00."""
    ordered = sorted(decisions)
    if not ordered:
        return []
    picked = [ordered[0]]
    for moment in EVAL_TIMES_LOCAL:
        target = datetime.combine(date.fromisoformat(day), moment, tz).timestamp()
        later = [d for d in ordered if d >= target]
        if later and later[0] not in picked:
            picked.append(later[0])
    return picked


# --------------------------------------------------------------------------
# Day records, learning and the check (store section ``same_day``)
# --------------------------------------------------------------------------


def new_section() -> dict[str, Any]:
    return {"days": {}, "table": None, "gate": None, "gate_history": []}


def usable_day(record: Mapping[str, Any]) -> bool:
    """Enough measured daylight bands and at least one decision."""
    bands = record.get("bands", {})
    lit = [b for b in bands.values() if (b.get("c") or 0.0) >= BAND_MIN_CLEAR_KWH]
    if not lit or not record.get("decisions"):
        return False
    measured = [b for b in lit if b.get("k") is not None]
    return len(measured) >= USABLE_BAND_SHARE * len(lit)


def measured_remaining(record: Mapping[str, Any], group: str, from_ts: int) -> float | None:
    """Measured energy of a group from ``from_ts`` to the end of the day;
    None if any lit band is missing."""
    total = 0.0
    for key, band in record.get("bands", {}).items():
        if int(key) < from_ts:
            continue
        value = (band.get("m") or {}).get(group)
        if value is None:
            if (band.get("c") or 0.0) >= BAND_MIN_CLEAR_KWH:
                return None
            continue
        total += value
    return total


def learn_table(days: Mapping[str, Mapping[str, Any]], before: str) -> Table:
    """β and σ per situation and lead bucket from usable days before
    ``before`` (no look-ahead), the last BETA_WINDOW_DAYS of them."""
    usable = sorted(d for d, rec in days.items() if d < before and usable_day(rec))[-BETA_WINDOW_DAYS:]
    sxy = {s: [0.0] * len(LEAD_BUCKETS_H) for s in SITUATIONS}  # Σw·x·y
    sxx = {s: [0.0] * len(LEAD_BUCKETS_H) for s in SITUATIONS}  # Σw·x²
    used: dict[str, list[set[str]]] = {s: [set() for _ in LEAD_BUCKETS_H] for s in SITUATIONS}
    pairs: dict[str, list[list[tuple[float, float, float]]]] = {s: [[] for _ in LEAD_BUCKETS_H] for s in SITUATIONS}
    unconditional: list[tuple[float, float]] = []
    for day in usable:
        record = days[day]
        bands = record["bands"]
        for d_key, decision in record["decisions"].items():
            d_ts = int(d_key)
            kfc = {int(h): v for h, v in (decision.get("kfc") or {}).items()}
            x = decision.get("e")
            sit = decision.get("sit") if decision.get("sit") in SITUATIONS else "other"
            for b_key, band in bands.items():
                b_ts = int(b_key)
                if b_ts < d_ts or band.get("k") is None:
                    continue
                w = band.get("c") or 0.0
                k_fc = kfc.get(b_ts // HOUR_S * HOUR_S)
                if w < BAND_MIN_CLEAR_KWH or k_fc is None:
                    continue
                y = float(band["k"]) - float(k_fc)
                unconditional.append((w, y))
                if x is None:
                    continue
                bucket = lead_bucket((b_ts + BAND_S / 2 - d_ts) / HOUR_S)
                if bucket is None:
                    continue
                sxy[sit][bucket] += w * x * y
                sxx[sit][bucket] += w * x * x
                used[sit][bucket].add(day)
                pairs[sit][bucket].append((w, float(x), y))
    beta: dict[str, list[float]] = {}
    sigma: dict[str, list[float | None]] = {}
    count: dict[str, list[int]] = {}
    for sit in SITUATIONS:
        beta[sit], sigma[sit], count[sit] = [], [], []
        for bucket in range(len(LEAD_BUCKETS_H)):
            n = len(used[sit][bucket])
            prior = PRIOR_BETA[bucket]
            if n == 0 or sxx[sit][bucket] <= 0:
                b = prior
            else:
                b = (n * (sxy[sit][bucket] / sxx[sit][bucket]) + PRIOR_DAYS * prior) / (n + PRIOR_DAYS)
            b = _clamp(b, BETA_MIN, BETA_MAX)
            beta[sit].append(b)
            count[sit].append(n)
            cell_pairs = pairs[sit][bucket]
            wsum = sum(p[0] for p in cell_pairs)
            if n == 0 or wsum <= 0:
                sigma[sit].append(None)
            else:
                sigma[sit].append(math.sqrt(sum(w * (y - b * x) ** 2 for w, x, y in cell_pairs) / wsum))
    wsum = sum(w for w, _ in unconditional)
    sigma_u = math.sqrt(sum(w * y * y for w, y in unconditional) / wsum) if wsum > 0 else None
    return Table(beta=beta, sigma=sigma, days=count, sigma_u=sigma_u, usable_days=len(usable))


def evaluate_gate(days: Mapping[str, Mapping[str, Any]], groups: Sequence[str], today: str, tz: tzinfo,
                  now: datetime) -> dict[str, Any]:
    """Remaining-day energy error with vs without the same-day correction
    at the decision times the owner's automation uses (last 28 days)."""
    start = (date.fromisoformat(today) - timedelta(days=GATE_WINDOW_DAYS)).isoformat()
    per_day: list[tuple[str, float, float]] = []  # day, mean error without, with
    covered = below = 0
    for day in sorted(d for d in days if start <= d < today):
        record = days[day]
        if not usable_day(record):
            continue
        err_base = err_sdc = 0.0
        n = 0
        decisions = record["decisions"]
        for d_key in eval_decisions(day, tz, [int(k) for k in decisions]):
            decision = decisions[str(d_key)]
            for group in groups:
                rem = (decision.get("rem") or {}).get(group)
                actual = measured_remaining(record, group, d_key)
                if rem is None or actual is None or rem[0] is None or rem[1] is None:
                    continue
                err_base += abs(rem[0] - actual)
                err_sdc += abs(rem[1] - actual)
                n += 1
                if len(rem) > 2 and rem[2] is not None:
                    covered += 1
                    below += int(actual < rem[2])
        if n:
            per_day.append((day, err_base / n, err_sdc / n))
    days_n = len(per_day)
    mae_base = sum(p[1] for p in per_day) / days_n if days_n else None
    mae_sdc = sum(p[2] for p in per_day) / days_n if days_n else None
    improvement = (mae_base - mae_sdc) / mae_base if mae_base and mae_sdc is not None else None
    recent = per_day[-GATE_RECENT_DAYS:]
    better_recent = sum(1 for p in recent if p[2] < p[1] - 1e-9)
    reasons = []
    if days_n < GATE_MIN_DAYS:
        reasons.append(f"{days_n}/{GATE_MIN_DAYS} usable days")
    elif improvement is None or improvement < GATE_MIN_IMPROVEMENT - 1e-9:
        reasons.append("remaining-day error improvement "
                       + ("n/a" if improvement is None else f"{improvement:.1%}") + f" below {GATE_MIN_IMPROVEMENT:.0%}")
    if days_n >= GATE_MIN_DAYS and better_recent < GATE_RECENT_BETTER:
        reasons.append(f"better on {better_recent} of the last {len(recent)} days (needs {GATE_RECENT_BETTER})")
    passed = (
        days_n >= GATE_MIN_DAYS
        and improvement is not None
        and improvement >= GATE_MIN_IMPROVEMENT - 1e-9  # exactly 5 % passes (float rounding)
        and better_recent >= GATE_RECENT_BETTER
    )
    return {
        "evaluated": now.astimezone(timezone.utc).isoformat(),
        "days": days_n,
        "mae_without_kwh": None if mae_base is None else round(mae_base, 3),
        "mae_with_kwh": None if mae_sdc is None else round(mae_sdc, 3),
        "improvement": None if improvement is None else round(improvement, 4),
        "better_recent": better_recent,
        "recent_days": len(recent),
        "cautious_checked": covered,
        "cautious_below": below,
        "cautious_coverage": None if covered == 0 else round(below / covered, 4),
        "passed": passed,
        "reasons": reasons,
    }


def purge(section: dict[str, Any], today: str) -> int:
    limit = (date.fromisoformat(today) - timedelta(days=RETENTION_DAYS)).isoformat()
    old = [d for d in section["days"] if d < limit]
    for day in old:
        del section["days"][day]
    if len(section["gate_history"]) > 90:
        del section["gate_history"][: len(section["gate_history"]) - 90]
    return len(old)


class SameDayDataError(ValueError):
    """The stored same-day section is malformed."""


def _finite(value: Any, allow_none: bool = True) -> bool:
    if value is None:
        return allow_none
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_section(raw: Any) -> dict[str, Any]:
    """Strict check of the stored section; raises SameDayDataError."""
    if raw is None:
        return new_section()
    if not isinstance(raw, dict) or not isinstance(raw.get("days"), dict):
        raise SameDayDataError("same_day is not a mapping with days")
    for day, record in raw["days"].items():
        date.fromisoformat(day)
        if not isinstance(record, dict):
            raise SameDayDataError("same_day day")
        for key, band in (record.get("bands") or {}).items():
            int(key)
            if not isinstance(band, dict) or not _finite(band.get("k")) or not _finite(band.get("c")):
                raise SameDayDataError("same_day band")
            for value in (band.get("m") or {}).values():
                if not _finite(value):
                    raise SameDayDataError("same_day band energy")
        for key, decision in (record.get("decisions") or {}).items():
            int(key)
            if not isinstance(decision, dict) or not _finite(decision.get("e")):
                raise SameDayDataError("same_day decision")
            if decision.get("sit") not in SITUATIONS:
                raise SameDayDataError("same_day situation")
            for h, v in (decision.get("kfc") or {}).items():
                int(h)
                if not _finite(v, allow_none=False):
                    raise SameDayDataError("same_day forecast k")
            for rem in (decision.get("rem") or {}).values():
                if not isinstance(rem, list) or not all(_finite(v) for v in rem):
                    raise SameDayDataError("same_day remaining energy")
        record.setdefault("bands", {})
        record.setdefault("decisions", {})
    table = raw.get("table")
    if table is not None:
        if not isinstance(table, dict):
            raise SameDayDataError("same_day table")
        try:
            loaded = Table.from_json(table)
            for sit in SITUATIONS:
                if (len(loaded.beta[sit]) != len(LEAD_BUCKETS_H) or len(loaded.sigma[sit]) != len(LEAD_BUCKETS_H)
                        or len(loaded.days[sit]) != len(LEAD_BUCKETS_H)):
                    raise SameDayDataError("same_day table size")
        except (KeyError, TypeError, ValueError) as err:
            raise SameDayDataError(f"same_day table: {err}") from err
        if not _finite(table.get("sigma_u")):
            raise SameDayDataError("same_day sigma_u")
        for sit in SITUATIONS:
            for v in table["beta"][sit]:
                if not _finite(v, allow_none=False) or not BETA_MIN <= v <= BETA_MAX:
                    raise SameDayDataError("same_day beta out of bounds")
            for v in table["sigma"][sit]:
                if not _finite(v) or (v is not None and v < 0):
                    raise SameDayDataError("same_day sigma")
    raw.setdefault("gate", None)
    raw.setdefault("gate_history", [])
    return raw


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def export_rows(section: Mapping[str, Any], groups: Sequence[str]) -> tuple[list[list[Any]], list[str], list[list[Any]], list[str]]:
    """(decision rows, header, band rows, header) for CSV export."""
    d_rows, b_rows = [], []
    for day in sorted(section["days"]):
        record = section["days"][day]
        for key in sorted(record["decisions"], key=int):
            decision = record["decisions"][key]
            row: list[Any] = [day, key_to_dt(int(key)).isoformat(), decision.get("e"), decision.get("sit")]
            for group in groups:
                rem = (decision.get("rem") or {}).get(group) or [None, None, None]
                row += list(rem) + [measured_remaining(record, group, int(key))]
            row.append(";".join(f"{key_to_dt(int(h)).isoformat()}={v}" for h, v in sorted(
                (decision.get("kfc") or {}).items(), key=lambda item: int(item[0]))))
            d_rows.append(row)
        for key in sorted(record["bands"], key=int):
            band = record["bands"][key]
            row = [day, key_to_dt(int(key)).isoformat(), band.get("k"), band.get("c")]
            row += [(band.get("m") or {}).get(group) for group in groups]
            b_rows.append(row)
    d_header = ["date", "decision_utc", "e_hat", "situation"]
    for group in groups:
        d_header += [f"{group}_remaining_without_kwh", f"{group}_remaining_with_kwh",
                     f"{group}_remaining_cautious_kwh", f"{group}_remaining_measured_kwh"]
    d_header.append("forecast_k_by_hour")
    b_header = ["date", "band_utc", "k_measured", "clear_kwh"] + [f"{g}_measured_kwh" for g in groups]
    return d_rows, d_header, b_rows, b_header


def day_of(ts: int, tz: tzinfo) -> str:
    return key_to_dt(ts).astimezone(tz).date().isoformat()

