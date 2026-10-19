"""The independent replay tool reproduces the integration's corrections
from the export alone (0.1.34, spec §10/§14)."""

from __future__ import annotations

import csv
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.open_meteo_solar_forecast import learning as L

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import learning_replay  # noqa: E402

UTC = timezone.utc
TZ = ZoneInfo("Europe/Zurich")
LAT, LON = 47.3769, 8.5417  # neutral public test location
GROUP = L.GroupInfo("10K", 10.0, ("e1",))


def simulate(days: int, seed: int = 2, freeze_day: int | None = None, reset_day: int | None = None):
    rng = random.Random(seed)
    data = L.LearningData()
    start = datetime(2026, 9, 1, tzinfo=UTC)
    for d in range(days):
        day0 = start + timedelta(days=d)
        if freeze_day is not None and d == freeze_day:
            data.set_frozen(True, day0 - timedelta(hours=5))
        if freeze_day is not None and d == freeze_day + 5:
            data.set_frozen(False, day0 - timedelta(hours=5))
        if reset_day is not None and d == reset_day:
            data.d["resets"].append([int((day0 - timedelta(hours=5)).timestamp()), "stage1"])
        # three issues a day (different lead buckets), like the hourly run
        truth = {}
        # the third issue is the decision forecast (after sunrise + 1 h, 09:25 local)
        for issue in (day0 - timedelta(hours=26), day0 - timedelta(hours=8), day0 + timedelta(hours=7, minutes=25)):
            preds = {}
            for h in range(24):
                k = L.hour_key(day0 + timedelta(hours=h))
                if k + 3600 <= issue.timestamp():
                    continue
                e0h, elev = L.hour_geometry(k, LAT, LON)
                if elev < L.MIN_ELEVATION_DEG:
                    continue
                low = rng.choice([0.0, 15.0, 70.0, 95.0])
                values = [rng.uniform(0.2, 0.75), 0.3, low, rng.uniform(0, 40), 0.0, 12.0, rng.uniform(4, 11),
                          elev, e0h, min(low + rng.uniform(0, 30), 100.0)]
                preds[k] = L.Predictors(k, *[round(v, 6) for v in values])
                truth.setdefault(k, (preds[k].kt, low))
            corr = L.compute_corrections(data, preds, issue, TZ)
            data.archive_forecast(preds, corr, issue)
            L.capture_issue6(data, preds, corr, issue, TZ, LAT, LON)
        for k, (kt, low) in truth.items():
            e0h, _ = L.hour_geometry(k, LAT, LON)
            kt_m = max(kt + (-0.2 if low > 50 else 0.03) + rng.gauss(0, 0.03), 0.02)
            data.d["station_hours"][k] = [round(kt_m * e0h, 3), round(0.35 * kt_m * e0h, 3), 0.0, 300.0, 9.0, 10.0, 6.0]
        day = day0.astimezone(TZ).date().isoformat()
        data.d["days"][day] = {"decided": int((day0 + timedelta(days=1, hours=1, minutes=30)).timestamp()),
                               "status": "snow" if d % 9 == 4 else L.DAY_OK, "details": {}}
    end = start + timedelta(days=days, hours=1, minutes=31)
    data.d["gate"] = L.evaluate_gate(data, [GROUP], end, TZ)
    return data


def export(data: L.LearningData, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for year in (2026,):
        (folder / f"archive_{year}.csv").write_text(L.export_archive_csv(data, year))
        (folder / f"station_hours_{year}.csv").write_text(L.export_station_csv(data, year))
    (folder / "days.csv").write_text(L.export_days_csv(data, [GROUP], TZ))
    (folder / "forecast_decision.csv").write_text(L.export_issue6_csv(data))
    (folder / "settings.json").write_text(json.dumps(L.export_settings(data, [GROUP], TZ, LAT, LON), default=str))
    return folder


@pytest.mark.parametrize(("freeze", "reset"), [(None, None), (20, None), (None, 25)])
def test_replay_reproduces_every_stored_correction(tmp_path, freeze, reset):
    data = simulate(45, freeze_day=freeze, reset_day=reset)
    shifts = [row[L.R["shift_kt"]] for b in data.d["archive"].values() for row in b.values()]
    assert sum(1 for s in shifts if abs(s) > 0.05) > 50  # the learner did act
    folder = export(data, tmp_path / "export")
    result = learning_replay.Replay(learning_replay.load(folder)).run()
    assert result["mismatches"] == 0 and result["not_verifiable"] == 0
    assert result["checked"] == len(shifts)
    assert result["max_difference"] <= learning_replay.TOLERANCE
    assert learning_replay.main([str(folder)]) == 0


def test_replay_recomputes_the_gate(tmp_path):
    data = simulate(40)
    folder = export(data, tmp_path / "export")
    gate = learning_replay.gate_irradiance(learning_replay.load(folder))
    assert gate["days"] == data.d["gate"]["days"] > 0
    assert gate["rmse_raw_w_m2"] == pytest.approx(data.d["gate"]["rmse_raw_w_m2"], abs=0.01)
    assert gate["rmse_corrected_w_m2"] == pytest.approx(data.d["gate"]["rmse_corrected_w_m2"], abs=0.01)


def test_replay_detects_a_tampered_correction(tmp_path, capsys):
    data = simulate(30)
    folder = export(data, tmp_path / "export")
    path = folder / "archive_2026.csv"
    rows = list(csv.DictReader(path.open()))
    target = next(r for r in rows if abs(float(r["shift_kt"])) > 0.05)
    target["shift_kt"] = repr(float(target["shift_kt"]) + 0.01)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    assert learning_replay.main([str(folder), "--verbose"]) == 1
    assert "MISMATCH" in capsys.readouterr().out


def test_replay_marks_rows_whose_evidence_was_purged(tmp_path):
    data = simulate(10)
    data.d["purged_before"] = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
    folder = export(data, tmp_path / "export")
    result = learning_replay.Replay(learning_replay.load(folder)).run()
    assert result["not_verifiable"] == result["checked"] + result["not_verifiable"] > 0


def test_replay_rejects_a_missing_export(tmp_path):
    assert learning_replay.main([str(tmp_path / "nothing")]) == 2


# ---------------------------------------------------------------------------
# 0.1.35: the same-day persistence table is reproduced from the export
# ---------------------------------------------------------------------------


def same_day_export(tmp_path, tamper: bool = False) -> Path:
    from custom_components.open_meteo_solar_forecast import same_day as SD

    rng = random.Random(3)
    data = simulate(5)
    section = data.d["same_day"]
    for i in range(25):
        day0 = datetime(2026, 9, 1, 7, 0, tzinfo=UTC) + timedelta(days=i)
        bands, decisions = {}, {}
        e = rng.gauss(0, 0.25)
        for j in range(36):
            b = int(day0.timestamp()) + j * 900
            e = 0.9 * e + rng.gauss(0, 0.1)
            bands[str(b)] = {"m": {"10K": round(rng.uniform(0, 1), 4)}, "c": round(rng.uniform(0.02, 1.0), 4),
                             "k": None if j % 11 == 5 else round(0.6 + e, 4)}
        for j in (0, 6, 12):
            d_ts = int(day0.timestamp()) + j * 900
            decisions[str(d_ts)] = {
                "e": None if (i + j) % 7 == 0 else round(rng.gauss(0, 0.3), 4),
                "sit": "improving" if (i + j) % 3 == 0 else "other",
                "kfc": {str(int(day0.timestamp()) // 3600 * 3600 + h * 3600): round(rng.uniform(0.3, 0.9), 4)
                        for h in range(10)},
                "rem": {"10K": [5.0, 4.0, None]},
            }
        section["days"][day0.astimezone(TZ).date().isoformat()] = {"bands": bands, "decisions": decisions}
    computed = datetime(2026, 9, 20, 1, 30, tzinfo=UTC)
    table = SD.learn_table(section["days"], computed.astimezone(TZ).date().isoformat())
    assert table.usable_days > 10
    section["table"] = {"computed": computed.isoformat(), **table.to_json()}
    folder = export(data, tmp_path / "export")
    d_rows, d_header, b_rows, b_header = SD.export_rows(section, ["10K"])
    if tamper:
        b_rows[40][2] = b_rows[40][2] + 0.05 if b_rows[40][2] is not None else 0.5
    (folder / "same_day_decisions.csv").write_text(L._csv(d_rows, d_header))
    (folder / "same_day_bands.csv").write_text(L._csv(b_rows, b_header))
    return folder


def test_replay_reproduces_the_same_day_table(tmp_path):
    folder = same_day_export(tmp_path)
    data = learning_replay.load(folder)
    result = learning_replay.same_day_table(folder, data["settings"])
    assert result["usable_days"] == result["stored_usable_days"] > 10
    assert result["max_difference"] <= 1e-5
    assert learning_replay.main([str(folder)]) == 0


def test_replay_detects_a_tampered_same_day_band(tmp_path):
    folder = same_day_export(tmp_path, tamper=True)
    assert learning_replay.main([str(folder)]) == 1
