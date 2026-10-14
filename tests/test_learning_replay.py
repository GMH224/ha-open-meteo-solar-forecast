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
LAT, LON = 47.48, 8.90
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
        for issue in (day0 - timedelta(hours=26), day0 - timedelta(hours=8), day0 + timedelta(hours=4, minutes=25)):
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
            L.capture_issue6(data, preds, corr, issue, TZ)
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
    (folder / "forecast_0600.csv").write_text(L.export_issue6_csv(data))
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
