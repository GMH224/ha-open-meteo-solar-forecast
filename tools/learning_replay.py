#!/usr/bin/env python3
"""Independent replay of the learning hub's stage-1 corrections (0.1.34).

Reads the export folder (``/config/open_meteo_solar_forecast/export``) and
recomputes, for every archived forecast row, the correction the
integration stored at that row's issue time, from the exported evidence
only. It also recomputes the irradiance half of the latest skill gate.

This is a second, deliberately separate implementation of
OMSF_v0_1_34_Learning_Spec.md §4–§5 and §9 (it does not import the
integration), so it checks the integration rather than repeating it.
Parameters come from the exported ``settings.json``.

Usage:
    python tools/learning_replay.py EXPORT_DIR [--verbose]

Exit codes: 0 all verifiable rows reproduced; 1 mismatches; 2 bad input.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DAY_S = 86400
BUCKETS = ("0-6h", "6-18h", "18-30h", "30-54h")
TOLERANCE = 2e-6  # stored values carry 6 decimals


def _ts(text: str) -> int:
    return int(datetime.fromisoformat(text).astimezone(timezone.utc).timestamp())


def load(folder: Path) -> dict:
    settings = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
    rows, station, days, issue = [], {}, {}, {}
    for path in sorted(folder.glob("archive_*.csv")):
        with path.open(encoding="utf-8") as handle:
            for item in csv.DictReader(handle):
                rows.append(item)
    for path in sorted(folder.glob("station_hours_*.csv")):
        with path.open(encoding="utf-8") as handle:
            for item in csv.DictReader(handle):
                station[_ts(item["hour_utc"])] = item
    with (folder / "days.csv").open(encoding="utf-8") as handle:
        for item in csv.DictReader(handle):
            days[item["date"]] = item
    issue_path = folder / "forecast_decision.csv"
    if issue_path.exists():
        with issue_path.open(encoding="utf-8") as handle:
            for item in csv.DictReader(handle):
                issue.setdefault(item["date"], []).append(item)
    return {"settings": settings, "rows": rows, "station": station, "days": days, "issue": issue}


def _f(text: str | None) -> float | None:
    return None if text in (None, "") else float(text)


class Replay:
    def __init__(self, data: dict) -> None:
        s = data["settings"]
        p = s["parameters"]
        self.tz = ZoneInfo(s["timezone"])
        self.scales = p["predictor_scales"]
        self.k = p["k_neighbours"]
        self.max_d = p["max_distance"]
        self.min_n = p["min_analogs"]
        self.eps = p["distance_eps"]
        self.n0 = p["shrink_n0"]
        self.half = p["recency_half_life_days"]
        self.season_age = p["season_min_age_days"]
        self.season_win = p["season_window_days"]
        self.season_floor = p["season_floor"]
        self.recency_floor = p["recency_floor"]
        self.max_kt = p["max_shift_kt"]
        self.max_kd = p["max_shift_kd"]
        self.max_age = p["evidence_max_age_days"]
        self.freeze = s.get("freeze") or []
        self.resets = s.get("resets") or []
        self.purged_before = s.get("purged_before")
        self.station = data["station"]
        self.days = data["days"]
        self.rows = []
        for item in data["rows"]:
            hour = _ts(item["hour_utc"])
            self.rows.append({
                "hour": hour,
                "bucket": BUCKETS.index(item["lead_bucket"]),
                "issued": _ts(item["issued_utc"]),
                "x": [float(item["kt"]), float(item["elevation"]), float(item["low"]),
                      max(float(item["mid"]), float(item["high"])), float(item["temp"]) - float(item["dew"]),
                      float(item["total"])],
                "kt": float(item["kt"]),
                "kd": float(item["kd"]),
                "e0h": float(item["e0h"]),
                "shift_kt": float(item["shift_kt"]),
                "shift_kd": float(item["shift_kd"]),
            })

    # -- evidence ---------------------------------------------------------------

    def cutoff(self, ts: int) -> int:
        for start, end in self.freeze:
            if start <= ts and (end is None or ts < end):
                return start
        return ts

    def floor(self, ts: int) -> int:
        return max([r for r, scope in self.resets if scope == "stage1" and r <= ts], default=0)

    def local_day(self, hour: int) -> str:
        return datetime.fromtimestamp(hour, timezone.utc).astimezone(self.tz).date().isoformat()

    def evidence(self, cutoff: int) -> list[dict]:
        out = []
        floor = self.floor(cutoff)
        for row in self.rows:
            if row["hour"] < cutoff - self.max_age * DAY_S or row["issued"] >= cutoff:
                continue
            meas = self.station.get(row["hour"])
            if meas is None or _f(meas["ghi"]) is None or _f(meas["dhi"]) is None:
                continue
            day = self.days.get(self.local_day(row["hour"]))
            if day is None or day["status"] != "ok" or not day["decided_utc"]:
                continue
            decided = _ts(day["decided_utc"])
            if not floor <= decided < cutoff:
                continue
            ghi, dhi = float(meas["ghi"]), float(meas["dhi"])
            if ghi <= 0 or row["e0h"] <= 0:
                kt_m, kd_m = 0.0, 1.0
            else:
                kt_m = min(ghi / row["e0h"], 1.0)
                kd_m = min(max(dhi / ghi, 0.0), 1.0)
            out.append({**row, "err_kt": kt_m - row["kt"], "err_kd": kd_m - row["kd"]})
        return out

    # -- analog ensemble -----------------------------------------------------------

    @staticmethod
    def doy(ts: int) -> int:
        return datetime.fromtimestamp(ts, timezone.utc).timetuple().tm_yday

    def recency(self, age: float, target_doy: int, ev_doy: int) -> float:
        weight = max(0.5 ** (age / self.half), self.recency_floor)
        diff = abs(ev_doy - target_doy)
        diff = min(diff, 365 - diff)
        if age >= self.season_age and diff <= self.season_win:
            weight = max(weight, self.season_floor)
        return weight

    def select(self, ev: list[dict], target: dict, buckets: set[int]) -> list[tuple[float, dict]]:
        found = []
        for item in ev:
            if item["bucket"] not in buckets:
                continue
            d = math.sqrt(sum(((a - b) / s) ** 2 for a, b, s in zip(item["x"], target["x"], self.scales)))
            if d <= self.max_d:
                found.append((d, item))
        found.sort(key=lambda pair: (pair[0], pair[1]["hour"], pair[1]["bucket"]))
        return found[: self.k]

    def correction(self, ev: list[dict], target: dict) -> tuple[float, float]:
        b = target["bucket"]
        chosen = self.select(ev, target, {b})
        if len(chosen) < self.min_n:
            chosen = self.select(ev, target, {b - 1, b, b + 1})
        if len(chosen) < self.min_n:
            return 0.0, 0.0
        tdoy = self.doy(target["hour"])
        weights = [
            (1.0 / (d + self.eps)) * self.recency(abs(target["hour"] - it["hour"]) / DAY_S, tdoy, self.doy(it["hour"]))
            for d, it in chosen
        ]
        total = sum(weights)
        mean_kt = sum(w * it["err_kt"] for w, (_, it) in zip(weights, chosen)) / total
        mean_kd = sum(w * it["err_kd"] for w, (_, it) in zip(weights, chosen)) / total
        n_eff = total * total / sum(w * w for w in weights)
        shrink = n_eff / (n_eff + self.n0)
        clip = lambda v, m: min(max(v, -m), m)  # noqa: E731
        return clip(shrink * mean_kt, self.max_kt), clip(shrink * mean_kd, self.max_kd)

    def verifiable(self, row: dict) -> bool:
        """Evidence the integration may have used is still in the export."""
        if self.purged_before is None:
            return True
        return self.cutoff(row["issued"]) - self.max_age * DAY_S >= self.purged_before

    def run(self, verbose: bool = False) -> dict:
        cache: dict[int, list[dict]] = {}
        checked = skipped = 0
        worst = 0.0
        mismatches = []
        for row in self.rows:
            if not self.verifiable(row):
                skipped += 1
                continue
            cut = self.cutoff(row["issued"])
            if cut not in cache:
                cache[cut] = self.evidence(cut)
            kt, kd = self.correction(cache[cut], row)
            diff = max(abs(kt - row["shift_kt"]), abs(kd - row["shift_kd"]))
            worst = max(worst, diff)
            checked += 1
            if diff > TOLERANCE:
                mismatches.append((row, kt, kd))
                if verbose:
                    hour = datetime.fromtimestamp(row["hour"], timezone.utc).isoformat()
                    print(f"MISMATCH {hour} {BUCKETS[row['bucket']]}: stored "
                          f"({row['shift_kt']}, {row['shift_kd']}) replay ({kt:.6f}, {kd:.6f})")
        return {"checked": checked, "not_verifiable": skipped, "mismatches": len(mismatches),
                "max_difference": worst}


def gate_irradiance(data: dict) -> dict | None:
    """RMSE of the decision forecasts (raw / corrected) over the gate window."""
    s = data["settings"]
    gate = s.get("gate")
    if not gate:
        return None
    tz = ZoneInfo(s["timezone"])
    today = datetime.fromisoformat(gate["evaluated"]).astimezone(tz).date()
    raw, corr, days = [], [], 0
    min_hours = s["parameters"]["gate_min_hours_per_day"]
    for offset in range(1, s["parameters"]["gate_window_days"] + 1):
        day = (today - timedelta(days=offset)).isoformat()
        decision = data["days"].get(day)
        if decision is None or decision["status"] != "ok":
            continue
        r_d, c_d = [], []
        for item in data["issue"].get(day, []):
            meas = data["station"].get(_ts(item["hour_utc"]))
            if meas is None or _f(meas["ghi"]) is None:
                continue
            e0h = float(item["e0h_w_m2"])
            r_d.append(float(item["kt_forecast"]) * e0h - float(meas["ghi"]))
            c_d.append(float(item["kt_corrected"]) * e0h - float(meas["ghi"]))
        if len(r_d) >= min_hours:
            days += 1
            raw += r_d
            corr += c_d
    rmse = lambda e: math.sqrt(sum(v * v for v in e) / len(e)) if e else None  # noqa: E731
    return {"days": days, "rmse_raw_w_m2": rmse(raw), "rmse_corrected_w_m2": rmse(corr),
            "stored": {k: gate.get(k) for k in ("days", "rmse_raw_w_m2", "rmse_corrected_w_m2")}}


# ---------------------------------------------------------------------------
# Same-day correction (0.1.35): the learned persistence table
# ---------------------------------------------------------------------------


def load_same_day(folder: Path) -> tuple[dict, dict] | None:
    """(day -> {"bands": {ts: (k, clear)}, "decisions": {ts: (e, situation, {hour: k})}}, groups)."""
    decisions_path, bands_path = folder / "same_day_decisions.csv", folder / "same_day_bands.csv"
    if not decisions_path.exists() or not bands_path.exists():
        return None
    days: dict = {}
    with bands_path.open(encoding="utf-8") as handle:
        for item in csv.DictReader(handle):
            record = days.setdefault(item["date"], {"bands": {}, "decisions": {}})
            groups = [key for key in item if key.endswith("_measured_kwh")]
            measured = [_f(item[g]) for g in groups]
            record["bands"][_ts(item["band_utc"])] = (_f(item["k_measured"]), float(item["clear_kwh"]), measured)
    with decisions_path.open(encoding="utf-8") as handle:
        for item in csv.DictReader(handle):
            record = days.setdefault(item["date"], {"bands": {}, "decisions": {}})
            kfc = {}
            for part in filter(None, (item.get("forecast_k_by_hour") or "").split(";")):
                moment, value = part.rsplit("=", 1)
                kfc[_ts(moment)] = float(value)
            record["decisions"][_ts(item["decision_utc"])] = (_f(item["e_hat"]), item["situation"], kfc)
    return days, {}


def same_day_table(folder: Path, settings: dict) -> dict | None:
    """Recompute β and σ (least squares through the origin per lead bucket
    and situation, shrunk toward the prior, clamped; residual spread) from
    the exported days and compare with the stored table."""
    section = settings.get("same_day") or {}
    stored, p = section.get("table"), section.get("parameters")
    loaded = load_same_day(folder)
    if not stored or not p or loaded is None:
        return None
    days = loaded[0]
    tz = ZoneInfo(settings["timezone"])
    before = datetime.fromisoformat(stored["computed"]).astimezone(tz).date().isoformat()
    buckets = [tuple(b) for b in p["lead_buckets_h"]]

    def usable(record: dict) -> bool:
        lit = [b for b in record["bands"].values() if b[1] >= p["band_min_clear_kwh"]]
        measured = [b for b in lit if b[0] is not None]
        return bool(lit) and bool(record["decisions"]) and len(measured) >= p["usable_band_share"] * len(lit)

    chosen = sorted(d for d, r in days.items() if d < before and usable(r))[-p["beta_window_days"]:]
    cells: dict = {}
    for day in chosen:
        record = days[day]
        for d_ts, (x, sit, kfc) in record["decisions"].items():
            if x is None:
                continue
            for b_ts, (k, clear, _m) in record["bands"].items():
                if b_ts < d_ts or k is None or clear < p["band_min_clear_kwh"]:
                    continue
                forecast = kfc.get(b_ts // 3600 * 3600)
                if forecast is None:
                    continue
                lead = (b_ts + p["band_s"] / 2 - d_ts) / 3600
                index = next((i for i, (lo, hi) in enumerate(buckets) if lo <= lead < hi), None)
                if index is None:
                    continue
                cells.setdefault((sit, index), []).append((clear, x, k - forecast, day))
    differences = []
    for sit in ("improving", "other"):
        for index in range(len(buckets)):
            pairs = cells.get((sit, index), [])
            n = len({q[3] for q in pairs})
            sxx = sum(w * x * x for w, x, _y, _d in pairs)
            prior = p["prior_beta"][index]
            beta = prior if n == 0 or sxx <= 0 else (
                n * sum(w * x * y for w, x, y, _d in pairs) / sxx + p["prior_days"] * prior) / (n + p["prior_days"])
            beta = min(max(beta, p["beta_min"]), p["beta_max"])
            differences.append(abs(beta - stored["beta"][sit][index]))
            wsum = sum(w for w, *_ in pairs)
            if n and wsum > 0:
                sigma = math.sqrt(sum(w * (y - beta * x) ** 2 for w, x, y, _d in pairs) / wsum)
                differences.append(abs(sigma - (stored["sigma"][sit][index] or 0.0)))
    return {"usable_days": len(chosen), "stored_usable_days": stored.get("usable_days"),
            "max_difference": max(differences) if differences else 0.0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    try:
        data = load(args.folder)
    except (OSError, ValueError, KeyError) as err:
        print(f"cannot read the export: {err}", file=sys.stderr)
        return 2
    result = Replay(data).run(args.verbose)
    same_day = same_day_table(args.folder, data["settings"])
    print(json.dumps({"corrections": result, "gate": gate_irradiance(data), "same_day": same_day},
                     indent=1, default=str))
    gate = gate_irradiance(data)
    gate_ok = True
    if gate is not None and gate["stored"]["rmse_raw_w_m2"] is not None:
        gate_ok = (
            gate["days"] == gate["stored"]["days"]
            and abs(gate["rmse_raw_w_m2"] - gate["stored"]["rmse_raw_w_m2"]) <= 0.01
            and abs(gate["rmse_corrected_w_m2"] - gate["stored"]["rmse_corrected_w_m2"]) <= 0.01
        )
    same_day_ok = same_day is None or (
        same_day["usable_days"] == same_day["stored_usable_days"] and same_day["max_difference"] <= 1e-5
    )
    return 0 if result["mismatches"] == 0 and gate_ok and same_day_ok else 1


if __name__ == "__main__":
    sys.exit(main())
