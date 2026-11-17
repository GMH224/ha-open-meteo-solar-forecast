"""Solcast as a second light forecast, combined by a learned weight (0.1.37).

Design record: ``OMSF_v0_1_37_Solcast_Blend_Spec.md``. Pure like
``learning.py``: no Home Assistant imports, no I/O, no clock reads, no
randomness (the hub passes jitter in).

Idea
----
A free Solcast hobbyist "rooftop site" that lies flat (tilt 0°, 1 kW DC and
AC, efficiency 100 %) forecasts the power of 1 kWp of horizontal panels, i.e.
the light on the flat (GHI) through Solcast's own weather data (satellite
cloud tracking + weather models). Its power is turned into light with a factor
``c`` (W/m² per kW) learned against the reference station. Per hour the light
the members would get without Solcast (``kt_ours``: the forecast after the
weather learning when that is applied) is combined with Solcast's::

    kt_blend = kt_ours + w_b x (kt_solcast - kt_ours),   w_b in [0, 1]

with one weight per lead-time bucket ``b``, learned by least squares against
the station over the last 28 days and pulled towards 0 ("ours only") with few
hours. The members' PV model (geometry, shading, inverter) then makes the
production from it, as from any light. Served only after its own out-of-sample
check passed and the owner switched it on; otherwise shadow.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

from .learning import (
    GATE_ENERGY_TOLERANCE,
    GATE_MIN_IMPROVEMENT,
    GATE_WINDOW_DAYS,
    HOUR_S,
    _clamp,
    hour_geometry,
    key_to_dt,
)
from .same_day import clear_kt, erbs

DAY_S = 24 * HOUR_S

# --------------------------------------------------------------------------
# Parameters (specification section in brackets)
# --------------------------------------------------------------------------

API_URL = "https://api.solcast.com.au/rooftop_sites/{site}/forecasts"  # (3) hobbyist endpoint
API_HOURS = 72
API_TIMEOUT_S = 30
DEFAULT_API_LIMIT = 10  # (3) requests per UTC day for new hobbyist accounts
RESERVE_CALLS = 1  # (3) planned calls stop one below the limit; the reserve is for one retry
SLOT_TIMES_LOCAL = ("05:07", "09:37", "11:07", "12:37", "14:07", "15:37", "20:37")
"""(3) fixed local times; odd minutes keep clear of the full hour, when the
Solcast servers are busiest."""
SUNRISE_SLOT_MIN = 40  # (3) just before the battery decision at sunrise + 1 h
SLOT_WINDOW_S = 45 * 60  # (3) a slot missed by more than this is skipped
RETRY_MIN_S, RETRY_MAX_S = 5 * 60, 15 * 60  # (3) one retry after a busy or failed call
FAILURE_WARN = 3  # (3) consecutive failures before the hub reports a problem
PERIOD_S = 1800
MAX_KW_PER_KWP = 1.5  # (3) plausibility bound of the flat 1 kW site's power
FORECAST_HOURS = 54  # (3) periods kept per issue (the forecast horizon used)
ISSUE_MAX_AGE_S = 12 * HOUR_S  # (5) an older issue is not used
RAW_RETENTION_DAYS = 35
PAIR_RETENTION_DAYS = 400
DECISION_RETENTION_DAYS = 760
CALL_RETENTION_DAYS = 10

C_PRIOR = 1150.0  # (4) W/m² per kW of the flat 1 kWp site: about 87 % system efficiency
C_PRIOR_HOURS = 30.0
C_MIN, C_MAX = 800.0, 1600.0
CALIBRATION_MIN_HOURS = 20

BUCKETS_H = (0, 3, 6, 18, 30, 54)  # (5) lead-time buckets of the weight
BUCKET_CENTRES_H = (1.5, 4.5, 12.0, 24.0, 42.0)
W_PRIOR_HOURS = 60.0  # (5) pull towards 0 ("ours only")
MIN_ELEVATION_LEARN_DEG = 10.0
MIN_ELEVATION_APPLY_DEG = 5.0
KT_S_CLEAR_MARGIN = 1.05  # (5) Solcast light at most 5 % above clear sky

GATE_MIN_DAYS = 14  # (6)
GATE_RECENT = 7
GATE_RECENT_BETTER = 5
GATE_MIN_ENERGY_DAYS = 7
DECISION_MIN_SOLCAST_SHARE = 0.5  # (6) a decision day counts if Solcast covered half its hours


class SolcastError(ValueError):
    """A Solcast answer that cannot be used."""


class SolcastDataError(ValueError):
    """The stored Solcast section is malformed."""


# --------------------------------------------------------------------------
# Calls: schedule and budget
# --------------------------------------------------------------------------


def utc_day(now: datetime) -> str:
    return now.astimezone(timezone.utc).date().isoformat()


def calls_today(section: Mapping[str, Any], now: datetime) -> int:
    return int(section["calls"].get(utc_day(now), 0))


def planned_budget(limit: int) -> int:
    return max(limit - RESERVE_CALLS, 0)


def slots_for(day: str, tz: tzinfo, sunrise: datetime | None) -> dict[str, datetime]:
    """The day's call slots (name -> UTC time)."""
    local = date.fromisoformat(day)
    out: dict[str, datetime] = {}
    for text in SLOT_TIMES_LOCAL:
        hh, mm = (int(x) for x in text.split(":"))
        out[text] = datetime.combine(local, time(hh, mm), tzinfo=tz).astimezone(timezone.utc)
    if sunrise is not None:
        out["sunrise+40"] = sunrise + timedelta(minutes=SUNRISE_SLOT_MIN)
    return dict(sorted(out.items(), key=lambda item: item[1]))


def due_call(
    section: Mapping[str, Any], now: datetime, tz: tzinfo, sunrise: datetime | None, limit: int
) -> str | None:
    """The slot (or "retry") to call now, or None. Planned slots stop one
    below the daily limit; a retry may use the reserve."""
    used = calls_today(section, now)
    retry_at = section.get("retry_at")
    if retry_at is not None and now.timestamp() >= retry_at and used < limit:
        return "retry"
    if used >= planned_budget(limit):
        return None
    day = now.astimezone(tz).date().isoformat()
    done = section["slots"].get(day, {})
    for name, moment in slots_for(day, tz, sunrise).items():
        if name in done:
            continue
        if moment <= now < moment + timedelta(seconds=SLOT_WINDOW_S):
            return name
    return None


def note_call(section: dict[str, Any], now: datetime, tz: tzinfo, slot: str) -> None:
    """Count a call before it is sent (a failed call counts too)."""
    day = utc_day(now)
    section["calls"][day] = int(section["calls"].get(day, 0)) + 1
    if slot == "retry":
        section["retry_at"] = None
    else:
        local_day = now.astimezone(tz).date().isoformat()
        section["slots"].setdefault(local_day, {})[slot] = "sent"
    section["last_call"] = now.isoformat()


def note_result(section: dict[str, Any], now: datetime, error: str | None, retry: bool, jitter: float) -> None:
    """Record the outcome; a failed call may schedule one retry
    (``jitter`` in [0, 1) chooses its delay within 5-15 min)."""
    if error is None:
        section["failures"] = 0
        section["last_error"] = None
        section["last_success"] = now.isoformat()
        return
    section["failures"] = int(section.get("failures", 0)) + 1
    section["last_error"] = error[:200]
    if retry and section.get("retry_at") is None:
        delay = RETRY_MIN_S + _clamp(jitter, 0.0, 1.0) * (RETRY_MAX_S - RETRY_MIN_S)
        section["retry_at"] = int(now.timestamp() + delay)


# --------------------------------------------------------------------------
# The answer
# --------------------------------------------------------------------------

_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def _parse_time(text: Any) -> int:
    if not isinstance(text, str):
        raise SolcastError("period_end is not a string")
    match = _ISO.match(text.strip())
    if match is None:
        raise SolcastError("period_end format")
    zone = "+00:00" if match.group(3) == "Z" else match.group(3)
    moment = datetime.fromisoformat(match.group(1) + zone)
    return int(moment.timestamp())


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SolcastError("value is not a number")
    if value < 0 or value > MAX_KW_PER_KWP:
        raise SolcastError("value out of range")
    return float(value)


def parse_forecast(payload: Any, now: datetime) -> dict[str, list[float]]:
    """Half-hour periods (period end -> [p, p10, p90] in kW of the flat
    1 kWp site) from now to 54 h ahead. Raises SolcastError."""
    if not isinstance(payload, dict) or not isinstance(payload.get("forecasts"), list):
        raise SolcastError("no forecasts list")
    start = now.timestamp()
    end = start + (FORECAST_HOURS + 1) * HOUR_S
    out: dict[str, list[float]] = {}
    for item in payload["forecasts"]:
        if not isinstance(item, dict):
            raise SolcastError("forecast item")
        if item.get("period", "PT30M") != "PT30M":
            raise SolcastError("period is not 30 minutes")
        period_end = _parse_time(item.get("period_end"))
        if period_end % PERIOD_S:
            raise SolcastError("period_end not on a half hour")
        p, p10, p90 = (_number(item.get(k)) for k in ("pv_estimate", "pv_estimate10", "pv_estimate90"))
        if not p10 <= p + 1e-9 or not p <= p90 + 1e-9:
            raise SolcastError("estimates not ordered (p10 <= p <= p90)")
        if start - PERIOD_S < period_end <= end:
            out[str(period_end)] = [round(p, 5), round(p10, 5), round(p90, 5)]
    if not out:
        raise SolcastError("no period in the next hours")
    return out


def store_issue(section: dict[str, Any], issued: datetime, periods: Mapping[str, list[float]]) -> None:
    section["issues"][str(int(issued.timestamp()))] = {"pe": dict(periods)}


def latest_issue(section: Mapping[str, Any], now: datetime) -> tuple[int, dict[str, list[float]]] | None:
    """The newest stored answer, if not older than 12 h."""
    issues = section["issues"]
    if not issues:
        return None
    issued = max(int(k) for k in issues)
    if issued > now.timestamp() or now.timestamp() - issued > ISSUE_MAX_AGE_S:
        return None
    return issued, issues[str(issued)]["pe"]


def hour_power(periods: Mapping[str, Sequence[float]], key: int) -> tuple[float, float, float] | None:
    """Mean power over the clock hour starting at ``key`` (both half hours
    required): (p, p10, p90) in kW of the flat 1 kWp site."""
    first, second = periods.get(str(key + PERIOD_S)), periods.get(str(key + 2 * PERIOD_S))
    if first is None or second is None:
        return None
    return (
        (first[0] + second[0]) / 2.0,
        (first[1] + second[1]) / 2.0,
        (first[2] + second[2]) / 2.0,
    )


# --------------------------------------------------------------------------
# Light from Solcast, the weight, the combination
# --------------------------------------------------------------------------


def bucket_of(lead_h: float) -> int | None:
    for index in range(len(BUCKETS_H) - 1):
        if BUCKETS_H[index] <= lead_h < BUCKETS_H[index + 1]:
            return index
    return None


def lead_hours(key: int, moment: float) -> float:
    """Lead time to the middle of the hour."""
    return (key + HOUR_S / 2 - moment) / HOUR_S


def kt_solcast(power_kw: float, c: float, key: int, latitude: float, longitude: float) -> float | None:
    """Clearness index of Solcast's light for an hour (None: sun too low)."""
    e0h, elevation = hour_geometry(key, latitude, longitude)
    if e0h <= 0 or elevation < MIN_ELEVATION_APPLY_DEG:
        return None
    kt = c * power_kw / e0h
    return _clamp(kt, 0.0, KT_S_CLEAR_MARGIN * clear_kt(key, latitude, longitude))


@dataclass(frozen=True)
class Blend:
    """The learned state used to combine: factor c and one weight per bucket."""

    c: float = C_PRIOR
    weights: tuple[float, ...] = (0.0,) * (len(BUCKETS_H) - 1)

    def weight(self, lead_h: float) -> float:
        b = bucket_of(lead_h)
        return 0.0 if b is None else self.weights[b]


def blend_shift(
    kt: float, kd: float, kt_s: float, w: float
) -> tuple[float, float]:
    """(shift of kt, shift of kd) that turns ours into the combination: the
    light is combined linearly; the diffuse part of Solcast's light is taken
    from Erbs (Solcast's free answer has no split)."""
    if w <= 0.0:
        return 0.0, 0.0
    kt_b = kt + w * (kt_s - kt)
    if kt_b <= 0.0:
        return -kt, 0.0
    diffuse = (1.0 - w) * kd * kt + w * erbs(kt_s) * kt_s
    kd_b = _clamp(diffuse / kt_b, 0.0, 1.0)
    return kt_b - kt, kd_b - kd


# --------------------------------------------------------------------------
# Learning: pairs, factor, weights
# --------------------------------------------------------------------------


def record_pair(
    section: dict[str, Any], key: int, kt_ours: float, power: tuple[float, float, float] | None,
    issued: int | None, moment: float,
) -> bool:
    """Keep, per hour and lead bucket, the pair (ours, Solcast) whose lead
    is nearest the bucket's centre."""
    if power is None or issued is None:
        return False
    lead = lead_hours(key, moment)
    b = bucket_of(lead)
    if b is None:
        return False
    row = [round(lead, 3), round(kt_ours, 5), round(power[0], 5), round(power[1], 5), round(power[2], 5), issued]
    slot = section["pairs"].setdefault(str(key), {})
    old = slot.get(str(b))
    if old is not None and abs(old[0] - BUCKET_CENTRES_H[b]) <= abs(lead - BUCKET_CENTRES_H[b]):
        return False
    slot[str(b)] = row
    return True


@dataclass(frozen=True)
class Truth:
    """Measured light of an hour (station GHI) and its geometry."""

    ghi: float
    e0h: float
    elevation: float


def _window_keys(pairs: Mapping[str, Any], first: int, last: int) -> list[int]:
    return [int(k) for k in pairs if first <= int(k) < last]


def learn_factor(
    pairs: Mapping[str, Any], truth: Mapping[int, Truth], first: int, last: int
) -> tuple[float, int]:
    """c (W/m² per kW) by least squares through the origin on the shortest
    lead bucket, shrunk towards the physical prior."""
    sum_pg = sum_pp = 0.0
    n = 0
    for key in _window_keys(pairs, first, last):
        row = pairs[str(key)].get("0")
        t = truth.get(key)
        if row is None or t is None or t.elevation < MIN_ELEVATION_LEARN_DEG or row[2] <= 0:
            continue
        sum_pg += row[2] * t.ghi
        sum_pp += row[2] * row[2]
        n += 1
    if n < CALIBRATION_MIN_HOURS or sum_pp <= 0:
        return C_PRIOR, n
    mean_pp = sum_pp / n
    c = (sum_pg + C_PRIOR_HOURS * mean_pp * C_PRIOR) / (sum_pp + C_PRIOR_HOURS * mean_pp)
    return _clamp(c, C_MIN, C_MAX), n


def learn_weights(
    pairs: Mapping[str, Any], truth: Mapping[int, Truth], first: int, last: int, c: float,
    latitude: float, longitude: float,
) -> tuple[tuple[float, ...], tuple[int, ...]]:
    """One weight per bucket: least squares of (truth - ours) on
    (Solcast - ours), shrunk towards 0 by 60 prior hours, clamped to [0, 1]."""
    n_buckets = len(BUCKETS_H) - 1
    num = [0.0] * n_buckets
    den = [0.0] * n_buckets
    count = [0] * n_buckets
    for key in _window_keys(pairs, first, last):
        t = truth.get(key)
        if t is None or t.elevation < MIN_ELEVATION_LEARN_DEG or t.e0h <= 0:
            continue
        kt_true = t.ghi / t.e0h
        for b_text, row in pairs[str(key)].items():
            kt_s = kt_solcast(row[2], c, key, latitude, longitude)
            if kt_s is None:
                continue
            b = int(b_text)
            r = kt_true - row[1]
            d = kt_s - row[1]
            num[b] += r * d
            den[b] += d * d
            count[b] += 1
    weights = []
    for b in range(n_buckets):
        if den[b] <= 1e-9 or count[b] == 0:
            weights.append(0.0)
            continue
        w_hat = num[b] / den[b]
        weights.append(round(_clamp(w_hat * count[b] / (count[b] + W_PRIOR_HOURS), 0.0, 1.0), 4))
    return tuple(weights), tuple(count)


def learn_blend(
    pairs: Mapping[str, Any], truth: Mapping[int, Truth], until: int, latitude: float, longitude: float,
) -> tuple[Blend, dict[str, Any]]:
    """Factor and weights from the 28 days before ``until`` (no look-ahead)."""
    first = until - GATE_WINDOW_DAYS * DAY_S
    c, n_c = learn_factor(pairs, truth, first, until)
    weights, counts = learn_weights(pairs, truth, first, until, c, latitude, longitude)
    return Blend(round(c, 2), weights), {"factor_hours": n_c, "hours": list(counts)}


# --------------------------------------------------------------------------
# Decision record and the check
# --------------------------------------------------------------------------


def record_decision_hours(
    section: dict[str, Any], day: str, captured: datetime, rows: Mapping[int, tuple[float, float | None, float]],
) -> bool:
    """Once per day, at the decision forecast (first member refresh at or
    after sunrise + 1 h): per hour [kt_ours, Solcast power or None, e0h]."""
    record = section["decisions"].setdefault(day, {"captured": None, "hours": {}, "entries": {}})
    if record["captured"] is not None:
        return False
    record["captured"] = int(captured.timestamp())
    record["hours"] = {
        str(k): [round(v[0], 5), None if v[1] is None else round(v[1], 5), round(v[2], 3)] for k, v in rows.items()
    }
    return True


def record_decision_energy(
    section: dict[str, Any], day: str, entry_id: str, ours_wh: float, blend_wh: float
) -> bool:
    record = section["decisions"].setdefault(day, {"captured": None, "hours": {}, "entries": {}})
    if entry_id in record["entries"]:
        return False
    record["entries"][entry_id] = [round(float(ours_wh), 2), round(float(blend_wh), 2)]
    return True


def evaluate_gate(
    section: Mapping[str, Any],
    truth: Mapping[int, Truth],
    groups: Mapping[str, Sequence[str]],
    production: Mapping[str, Mapping[str, float | None]],
    today: str,
    latitude: float,
    longitude: float,
) -> dict[str, Any]:
    """Out-of-sample check over the last 28 days: each day's decision hours
    are combined with the factor and weights learned only from the 28 days
    before that day. ``production``: group -> day -> measured kWh."""
    today_d = date.fromisoformat(today)
    err_ours: list[float] = []
    err_blend: list[float] = []
    per_day: list[tuple[str, float, float]] = []
    energy: dict[str, dict[str, list[float]]] = {g: {"ours": [], "blend": []} for g in groups}
    for offset in range(GATE_WINDOW_DAYS, 0, -1):
        day = (today_d - timedelta(days=offset)).isoformat()
        record = section["decisions"].get(day)
        if record is None or record["captured"] is None:
            continue
        captured = record["captured"]
        day_start = int(datetime.combine(date.fromisoformat(day), time(0), tzinfo=timezone.utc).timestamp())
        blend, _info = learn_blend(section["pairs"], truth, day_start, latitude, longitude)
        e_o: list[float] = []
        e_b: list[float] = []
        lit = covered = 0
        for key_text, (kt_o, power, e0h) in record["hours"].items():
            key = int(key_text)
            t = truth.get(key)
            if t is None or t.elevation < MIN_ELEVATION_LEARN_DEG:
                continue
            lit += 1
            kt_b = kt_o
            if power is not None:
                kt_s = kt_solcast(power, blend.c, key, latitude, longitude)
                if kt_s is not None:
                    covered += 1
                    kt_b = kt_o + blend.weight(lead_hours(key, captured)) * (kt_s - kt_o)
            e_o.append(kt_o * e0h - t.ghi)
            e_b.append(kt_b * e0h - t.ghi)
        if lit < 4 or covered < DECISION_MIN_SOLCAST_SHARE * lit:
            continue
        err_ours += e_o
        err_blend += e_b
        per_day.append((day, _rmse(e_o), _rmse(e_b)))
        for group, members in groups.items():
            entries = record["entries"]
            actual = production.get(group, {}).get(day)
            if actual is None or not members or any(m not in entries for m in members):
                continue
            ours = sum(entries[m][0] for m in members) / 1000.0
            blended = sum(entries[m][1] for m in members) / 1000.0
            energy[group]["ours"].append(abs(ours - actual))
            energy[group]["blend"].append(abs(blended - actual))
    rmse_o, rmse_b = _rmse(err_ours), _rmse(err_blend)
    improvement = (rmse_o - rmse_b) / rmse_o if err_ours and rmse_o > 0 else None
    recent = per_day[-GATE_RECENT:]
    better_recent = sum(1 for _d, o, b in recent if b < o)
    reasons = []
    days = len(per_day)
    if days < GATE_MIN_DAYS:
        reasons.append(f"{days}/{GATE_MIN_DAYS} days with a decision forecast, Solcast and measurement")
    elif improvement is None or improvement < GATE_MIN_IMPROVEMENT - 1e-9:
        reasons.append("light improvement below 5 %" if improvement is not None else "no comparison possible")
    if days >= GATE_MIN_DAYS and better_recent < GATE_RECENT_BETTER:
        reasons.append(f"better on {better_recent} of the last {len(recent)} days (needs {GATE_RECENT_BETTER})")
    group_result = {}
    for group in groups:
        o_list, b_list = energy[group]["ours"], energy[group]["blend"]
        n = len(o_list)
        mae_o = sum(o_list) / n if n else None
        mae_b = sum(b_list) / n if n else None
        ok = n >= GATE_MIN_ENERGY_DAYS and mae_o is not None and mae_b is not None and (
            mae_b <= mae_o * (1 + GATE_ENERGY_TOLERANCE) + 1e-9)
        if n < GATE_MIN_ENERGY_DAYS:
            reasons.append(f"{group}: {n}/{GATE_MIN_ENERGY_DAYS} days with energy data")
        elif not ok:
            reasons.append(f"{group}: daily energy error would get worse")
        group_result[group] = {
            "days": n,
            "mae_ours_kwh": None if mae_o is None else round(mae_o, 3),
            "mae_blend_kwh": None if mae_b is None else round(mae_b, 3),
            "passed": ok,
        }
    return {
        "days": days,
        "hours": len(err_ours),
        "rmse_ours_w_m2": round(rmse_o, 2) if err_ours else None,
        "rmse_blend_w_m2": round(rmse_b, 2) if err_blend else None,
        "improvement": None if improvement is None else round(improvement, 4),
        "better_recent": better_recent,
        "recent_days": len(recent),
        "groups": group_result,
        "passed": not reasons and bool(groups),
        "reasons": reasons if groups else reasons + ["no inverter group configured"],
    }


def _rmse(errors: Sequence[float]) -> float:
    return math.sqrt(sum(e * e for e in errors) / len(errors)) if errors else 0.0


# --------------------------------------------------------------------------
# Store section, retention, export
# --------------------------------------------------------------------------


def new_section() -> dict[str, Any]:
    return {
        "calls": {},
        "slots": {},
        "retry_at": None,
        "failures": 0,
        "last_error": None,
        "last_call": None,
        "last_success": None,
        "issues": {},
        "pairs": {},
        "decisions": {},
        "blend": None,
        "gate": None,
        "gate_history": [],
    }


def _num(value: Any, allow_none: bool = False) -> bool:
    if value is None:
        return allow_none
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_section(raw: Any) -> dict[str, Any]:
    """Strict check; raises SolcastDataError. A missing section is new."""
    try:
        return _validate(raw)
    except SolcastDataError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as err:
        raise SolcastDataError(f"solcast: {err}") from err


def _validate(raw: Any) -> dict[str, Any]:
    if raw is None:
        return new_section()
    if not isinstance(raw, dict):
        raise SolcastDataError("solcast is not a mapping")
    out = new_section()
    for key in out:
        if key in raw:
            out[key] = raw[key]
    if not isinstance(out["calls"], dict) or not all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0
        for k, v in out["calls"].items()
    ):
        raise SolcastDataError("solcast calls")
    if not isinstance(out["slots"], dict) or not all(isinstance(v, dict) for v in out["slots"].values()):
        raise SolcastDataError("solcast slots")
    if out["retry_at"] is not None and not isinstance(out["retry_at"], int):
        raise SolcastDataError("solcast retry")
    if not isinstance(out["failures"], int) or out["failures"] < 0:
        raise SolcastDataError("solcast failures")
    if not isinstance(out["issues"], dict):
        raise SolcastDataError("solcast issues")
    for issued, issue in out["issues"].items():
        if not isinstance(issue, dict) or not isinstance(issue.get("pe"), dict):
            raise SolcastDataError("solcast issue")
        int(issued)
        for period_end, values in issue["pe"].items():
            int(period_end)
            if (not isinstance(values, list) or len(values) != 3 or not all(_num(v) for v in values)
                    or not all(0 <= v <= MAX_KW_PER_KWP for v in values)):
                raise SolcastDataError("solcast period")
    if not isinstance(out["pairs"], dict):
        raise SolcastDataError("solcast pairs")
    for key, slot in out["pairs"].items():
        int(key)
        if not isinstance(slot, dict):
            raise SolcastDataError("solcast pair slot")
        for b, row in slot.items():
            if b not in {str(i) for i in range(len(BUCKETS_H) - 1)}:
                raise SolcastDataError("solcast pair bucket")
            if not isinstance(row, list) or len(row) != 6 or not all(_num(v) for v in row):
                raise SolcastDataError("solcast pair row")
            if not 0.0 <= row[1] <= 1.5 or not all(0.0 <= v <= MAX_KW_PER_KWP for v in row[2:5]):
                raise SolcastDataError("solcast pair value")
    if not isinstance(out["decisions"], dict):
        raise SolcastDataError("solcast decisions")
    for day, record in out["decisions"].items():
        date.fromisoformat(day)
        if (not isinstance(record, dict) or not isinstance(record.get("hours"), dict)
                or not isinstance(record.get("entries"), dict)):
            raise SolcastDataError("solcast decision")
        if record.get("captured") is not None and not isinstance(record["captured"], int):
            raise SolcastDataError("solcast decision time")
        for key, row in record["hours"].items():
            int(key)
            if (not isinstance(row, list) or len(row) != 3 or not _num(row[0]) or not _num(row[1], True)
                    or not _num(row[2])):
                raise SolcastDataError("solcast decision hour")
        for entry, row in record["entries"].items():
            if not isinstance(entry, str) or not isinstance(row, list) or len(row) != 2 or not all(_num(v) for v in row):
                raise SolcastDataError("solcast decision energy")
    blend = out["blend"]
    if blend is not None:
        if not isinstance(blend, dict) or not _num(blend.get("c")) or not isinstance(blend.get("weights"), list):
            raise SolcastDataError("solcast blend")
        if not C_MIN <= blend["c"] <= C_MAX or len(blend["weights"]) != len(BUCKETS_H) - 1 or not all(
                _num(w) and 0.0 <= w <= 1.0 for w in blend["weights"]):
            raise SolcastDataError("solcast weights")
    # The check result ("gate") is validated with the other checks in
    # learning.LearningData (ICS-04, 0.1.37.1): a malformed one is dropped.
    if not isinstance(out["gate_history"], list):
        raise SolcastDataError("solcast gate history")
    return out


def blend_of(section: Mapping[str, Any]) -> Blend:
    raw = section.get("blend")
    if not raw:
        return Blend()
    return Blend(float(raw["c"]), tuple(float(w) for w in raw["weights"]))


def copy_section(section: Mapping[str, Any]) -> dict[str, Any]:
    """Deep enough copy for the executor (lists inside are not mutated)."""
    out = dict(section)
    for key in ("calls", "slots", "issues", "pairs", "decisions"):
        out[key] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in section[key].items()}
    out["gate_history"] = list(section["gate_history"])
    return out


def purge(section: dict[str, Any], now: datetime, tz: tzinfo) -> int:
    removed = 0
    stamp = now.timestamp()
    for name, days in (("issues", RAW_RETENTION_DAYS), ("pairs", PAIR_RETENTION_DAYS)):
        for key in [k for k in section[name] if int(k) < stamp - days * DAY_S]:
            del section[name][key]
            removed += 1
    today = now.astimezone(tz).date()
    for name, days in (("decisions", DECISION_RETENTION_DAYS), ("slots", CALL_RETENTION_DAYS)):
        limit = (today - timedelta(days=days)).isoformat()
        for key in [k for k in section[name] if k < limit]:
            del section[name][key]
            removed += 1
    limit = (now.astimezone(timezone.utc).date() - timedelta(days=CALL_RETENTION_DAYS)).isoformat()
    for key in [k for k in section["calls"] if k < limit]:
        del section["calls"][key]
    return removed


def export_rows(section: Mapping[str, Any]) -> dict[str, tuple[list[list[Any]], list[str]]]:
    """CSV tables: pairs, decisions (hours), decision energies, issues."""
    pairs = []
    for key in sorted(section["pairs"], key=int):
        for b, row in sorted(section["pairs"][key].items()):
            pairs.append([key_to_dt(int(key)).isoformat(), int(b), *row[:5], key_to_dt(int(row[5])).isoformat()])
    decisions = []
    energies = []
    for day in sorted(section["decisions"]):
        record = section["decisions"][day]
        captured = None if record["captured"] is None else key_to_dt(record["captured"]).isoformat()
        for key in sorted(record["hours"], key=int):
            decisions.append([day, captured, key_to_dt(int(key)).isoformat(), *record["hours"][key]])
        for entry, (ours, blended) in sorted(record["entries"].items()):
            energies.append([day, entry, ours, blended])
    issues = []
    for issued in sorted(section["issues"], key=int):
        for period_end in sorted(section["issues"][issued]["pe"], key=int):
            issues.append([key_to_dt(int(issued)).isoformat(), key_to_dt(int(period_end)).isoformat(),
                           *section["issues"][issued]["pe"][period_end]])
    return {
        "solcast_pairs.csv": (pairs, ["hour_utc", "bucket", "lead_h", "kt_ours", "p_kw", "p10_kw", "p90_kw",
                                      "issued_utc"]),
        "solcast_decision_hours.csv": (decisions, ["date", "captured_utc", "hour_utc", "kt_ours", "p_kw", "e0h_w_m2"]),
        "solcast_decision_energy.csv": (energies, ["date", "entry", "ours_wh", "blend_wh"]),
        "solcast_issues.csv": (issues, ["issued_utc", "period_end_utc", "p_kw", "p10_kw", "p90_kw"]),
    }


def state_of(configured: bool, gate: Mapping[str, Any] | None, switch: bool) -> str:
    if not configured:
        return "not_configured"
    if gate is None or gate.get("days", 0) < GATE_MIN_DAYS:
        return "collecting"
    if not gate.get("passed"):
        return "shadow"
    return "applied" if switch else "eligible"


def truth_from(station_hours: Mapping[int, Sequence[Any]], ghi_index: int, keys: Iterable[int],
               latitude: float, longitude: float) -> dict[int, Truth]:
    out: dict[int, Truth] = {}
    for key in keys:
        row = station_hours.get(key)
        if row is None or row[ghi_index] is None:
            continue
        e0h, elevation = hour_geometry(key, latitude, longitude)
        out[key] = Truth(float(row[ghi_index]), e0h, elevation)
    return out
