"""Measured light at the house (0.1.36): the pure engine."""

from __future__ import annotations

import ast
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from custom_components.open_meteo_solar_forecast import house_ghi as HG
from custom_components.open_meteo_solar_forecast import learning as L
from custom_components.open_meteo_solar_forecast import same_day as SD
from custom_components.open_meteo_solar_forecast.local_source import sun_position

from .fusion_fixtures import LAT, LON

UTC = timezone.utc
TZ = ZoneInfo("Europe/Zurich")
NOON = int(datetime(2026, 10, 7, 11, 0, tzinfo=UTC).timestamp())  # 13:00 local
SUNRISE = L.sunrise_utc("2026-10-07", TZ, LAT, LON)


def linear_curve(clear_kwh: float) -> np.ndarray:
    """Energy proportional to k (clear_kwh at k = 1)."""
    return SD.K_GRID * clear_kwh


def window(**overrides):
    args = dict(
        measured={"10K": 0.6, "5K": 0.2},
        curve=linear_curve(2.0),
        limits_kwh={"10K": 2.5, "5K": 1.25},
        sunrise=SUNRISE,
        clear_ghi=500.0,
        elevation=30.0,
        kt_clear=0.75,
        snow=False,
        temperature_ok=True,
    )
    args.update(overrides)
    return HG.assess(NOON, **args)


# ---------------------------------------------------------------------------
# Module and geometry
# ---------------------------------------------------------------------------


def test_module_is_pure():
    source = Path(HG.__file__).read_text()
    tree = ast.parse(source)
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert not any(name.startswith("homeassistant") for name in imported)
    for forbidden in ("utcnow", "datetime.now", "time.time", "open(", "Path("):
        assert forbidden not in source, forbidden


def test_clear_sky_ghi_is_the_window_mean_of_haurwitz():
    expected = 0.0
    for i in range(5):
        moment = datetime.fromtimestamp(NOON + 90 + 180 * i, tz=UTC)
        cz = sun_position(moment, LAT, LON).cos_zenith
        expected += 1098.0 * cz * math.exp(-0.057 / cz) / 5
    assert HG.window_clear_ghi(NOON, LAT, LON) == pytest.approx(expected, rel=1e-12)
    night = int(datetime(2026, 10, 7, 1, 0, tzinfo=UTC).timestamp())
    assert HG.window_clear_ghi(night, LAT, LON) == 0.0


def test_clear_sky_and_elevation_against_pvlib():
    pvlib = pytest.importorskip("pvlib")
    import pandas as pd

    times = pd.date_range(datetime.fromtimestamp(NOON, tz=UTC), periods=5, freq="180s") + pd.Timedelta("90s")
    location = pvlib.location.Location(LAT, LON, altitude=408)
    solar = location.get_solarposition(times)
    reference = pvlib.clearsky.haurwitz(solar["apparent_zenith"]).ghi.mean()
    assert HG.window_clear_ghi(NOON, LAT, LON) == pytest.approx(reference, rel=0.01)
    middle = location.get_solarposition(pd.DatetimeIndex([datetime.fromtimestamp(NOON + 450, tz=UTC)]))
    assert HG.window_elevation(NOON, LAT, LON) == pytest.approx(float(middle["elevation"].iloc[0]), abs=0.1)


# ---------------------------------------------------------------------------
# Quality rules
# ---------------------------------------------------------------------------


def test_good_window_by_hand():
    w = window(measured={"10K": 0.3, "5K": 0.1}, kt_clear=0.75)  # 0.4 of 2.0 kWh -> k = 0.2
    assert w.k == pytest.approx(0.2)
    assert w.ghi == pytest.approx(100.0)  # 0.2 x 500
    assert (w.quality, w.reason) == (HG.GOOD, None)  # kt 0.15: diffuse 0.99, not mixed
    assert w.end == NOON + 900 and w.clear_ghi == 500.0


def test_bad_rules_and_their_order():
    assert (window(measured={"10K": None, "5K": 0.2}).reason, window(measured={}).reason) == ("counter", "counter")
    startup = HG.assess(int(SUNRISE.timestamp()) + 1799 - 1799 % 900, **{
        k: v for k, v in dict(measured={"10K": 0.1}, curve=linear_curve(2.0), limits_kwh={}, sunrise=SUNRISE,
                              clear_ghi=50.0, elevation=12.0, kt_clear=0.5, snow=False,
                              temperature_ok=True).items()})
    assert (startup.quality, startup.reason) == (HG.BAD, "startup")
    late = int(SUNRISE.timestamp()) + 1800
    ok = HG.assess(late, measured={"10K": 0.1}, curve=linear_curve(2.0), limits_kwh={}, sunrise=SUNRISE,
                   clear_ghi=50.0, elevation=12.0, kt_clear=0.5, snow=False, temperature_ok=True)
    assert ok.reason != "startup"
    assert window(elevation=9.99).reason == "low_sun" and window(elevation=9.99).quality == HG.BAD
    assert window(elevation=10.0).quality != HG.BAD
    assert window(curve=linear_curve(0.009)).reason == "low_sun"  # model sees no light
    assert window(snow=True).reason == "snow"
    # a missing counter wins over everything else
    assert window(measured={"10K": None}, elevation=5.0, snow=True).reason == "counter"


def test_clipping_edges():
    limit = 1.25  # 5 kW over 15 min
    bad = window(measured={"10K": 0.5, "5K": 0.95 * limit})
    assert (bad.quality, bad.reason, bad.ghi) == (HG.BAD, "clipping", None) and bad.k is not None
    below = window(measured={"10K": 0.5, "5K": 0.9499 * limit})
    assert (below.quality, below.reason) == (HG.UNCERTAIN, "clipping")
    assert window(measured={"10K": 0.5, "5K": 0.85 * limit}).reason == "clipping"
    assert window(measured={"10K": 0.5, "5K": 0.8499 * limit}).reason != "clipping"
    assert window(measured={"10K": 0.5, "5K": 1.2}, limits_kwh={"10K": 2.5, "5K": None}).reason != "clipping"


def test_uncertain_rules_and_their_order():
    assert window(elevation=17.99).reason == "low_sun" and window(elevation=17.99).quality == HG.UNCERTAIN
    assert window(elevation=18.0).reason is None
    assert window(temperature_ok=False).reason == "temperature"
    sat = window(measured={"10K": 2.0, "5K": 0.2}, limits_kwh={})
    assert (sat.reason, sat.k) == ("saturated", SD.K_MAX)
    assert window(curve=linear_curve(0.29), measured={"10K": 0.02, "5K": 0.0}).reason == "resolution"
    assert window(curve=linear_curve(0.3), measured={"10K": 0.02, "5K": 0.0}).reason is None
    # order: clipping before low sun before temperature
    assert window(measured={"10K": 0.5, "5K": 1.1}, elevation=12.0, temperature_ok=False).reason == "clipping"
    assert window(elevation=12.0, temperature_ok=False).reason == "low_sun"


def test_mixed_sky_by_the_diffuse_fraction():
    kt_clear = 0.75
    for k, mixed in ((0.2, False), (0.4, False), (0.6, True), (0.95, False)):
        w = window(measured={"10K": k * 2.0, "5K": 0.0}, kt_clear=kt_clear, limits_kwh={})
        assert (w.reason == "mixed_sky") is mixed, (k, SD.erbs(k * kt_clear))
    # the edges are on the diffuse fraction
    kd = SD.erbs(0.6 * kt_clear)
    assert HG.MIXED_KD_LOW <= kd <= HG.MIXED_KD_HIGH


# ---------------------------------------------------------------------------
# Snow
# ---------------------------------------------------------------------------


def test_snow_verdict():
    snowy = HG.SnowHour(house_k=0.2, station_k=0.8, temperature=0.0, elevation=20.0)
    assert HG.snow_verdict([snowy, snowy]) is True
    warm = HG.SnowHour(house_k=0.2, station_k=0.8, temperature=3.1, elevation=20.0)
    assert HG.snow_verdict([snowy, warm]) is False
    fine = HG.SnowHour(house_k=0.33, station_k=0.8, temperature=0.0, elevation=20.0)  # 0.33 > 0.4 x 0.8
    assert HG.snow_verdict([snowy, fine]) is False
    edge = HG.SnowHour(house_k=0.32, station_k=0.8, temperature=3.0, elevation=20.0)
    assert HG.snow_verdict([edge, edge]) is True
    for unknown in (
        HG.SnowHour(None, 0.8, 0.0, 20.0), HG.SnowHour(0.2, None, 0.0, 20.0), HG.SnowHour(0.2, 0.8, None, 20.0),
        HG.SnowHour(0.2, 0.8, 0.0, 17.9), HG.SnowHour(0.2, 0.49, 0.0, 20.0),
    ):
        assert HG.snow_verdict([snowy, unknown]) is None
    assert HG.snow_verdict([]) is None


def test_station_k_uses_the_same_clear_sky():
    hour = NOON - NOON % 3600
    clear = sum(HG.window_clear_ghi(hour + i * 900, LAT, LON) for i in range(4)) / 4
    assert HG.station_k(clear / 2, hour, LAT, LON) == pytest.approx(0.5)
    assert HG.station_k(None, hour, LAT, LON) is None
    assert HG.station_k(10.0, int(datetime(2026, 10, 7, 1, tzinfo=UTC).timestamp()), LAT, LON) is None


# ---------------------------------------------------------------------------
# Hours, days, attributes
# ---------------------------------------------------------------------------


def w_at(start, ghi=100.0, quality=HG.GOOD, reason=None):
    return HG.Window(start, None if quality == HG.BAD else ghi, 0.3, 400.0, quality, reason, 25.0)


def test_hour_value():
    hour = NOON
    full = {hour + i * 900: w_at(hour + i * 900, 100.0 + 10 * i) for i in range(4)}
    assert HG.hour_value(full, hour) == (115.0, HG.GOOD)
    one_uncertain = dict(full)
    one_uncertain[hour + 900] = w_at(hour + 900, 110.0, HG.UNCERTAIN, "mixed_sky")
    assert HG.hour_value(one_uncertain, hour) == (115.0, HG.UNCERTAIN)
    one_bad = dict(full)
    one_bad[hour + 1800] = w_at(hour + 1800, quality=HG.BAD, reason="clipping")
    assert HG.hour_value(one_bad, hour) == (None, HG.BAD)
    missing = {k: v for k, v in full.items() if k != hour + 2700}
    assert HG.hour_value(missing, hour) == (None, HG.BAD)


def test_day_counts():
    items = [w_at(NOON), w_at(NOON + 900, quality=HG.UNCERTAIN, reason="low_sun"),
             w_at(NOON + 1800, quality=HG.BAD, reason="clipping"), w_at(NOON + 2700, quality=HG.BAD, reason="snow")]
    c = HG.day_counts(items)
    assert (c["windows"], c[HG.GOOD], c[HG.UNCERTAIN], c[HG.BAD]) == (4, 1, 1, 2)
    assert c["bad_by_reason"] == {"counter": 0, "startup": 0, "low_sun": 0, "snow": 1, "clipping": 1, "curtailment": 0}
    assert c["uncertain_by_reason"]["low_sun"] == 1 and sum(c["uncertain_by_reason"].values()) == 1


def test_attributes_and_publication():
    now = datetime.fromtimestamp(NOON + 3600 + 120, tz=UTC)
    windows = {NOON + i * 900: w_at(NOON + i * 900, 100.0 + i) for i in range(4)}
    windows[NOON - 7200] = w_at(NOON - 7200, quality=HG.BAD, reason="low_sun")
    for i in range(1, 6):
        windows[NOON - 7200 + i * 900] = w_at(NOON - 7200 + i * 900)
    a = HG.attributes(windows, now, TZ)
    required = ("quality", "reason", "window_start", "window_end", "clear_sky_index", "clear_sky_ghi",
                "hour_ghi", "hour_quality", "recent", "version")
    assert all(key in a for key in required)
    assert a["window_start"] == datetime.fromtimestamp(NOON + 2700, tz=UTC).isoformat()
    assert a["window_end"] == datetime.fromtimestamp(NOON + 3600, tz=UTC).isoformat()
    assert (a["quality"], a["reason"], a["clear_sky_index"], a["clear_sky_ghi"]) == (HG.GOOD, None, 0.3, 400.0)
    assert (a["hour_ghi"], a["hour_quality"]) == (101.5, HG.GOOD)
    assert len(a["recent"]) == HG.RECENT_WINDOWS and a["recent"][-1][1:] == [103.0, HG.GOOD]
    assert a["version"] == HG.VERSION and a["windows_today"] == 10
    assert a["bad_counts_today"]["low_sun"] == 1
    assert HG.published(HG.latest(windows), now)
    # stale: shown as bad, reason counter
    end = datetime.fromtimestamp(NOON + 3600, tz=UTC)
    later = end + timedelta(seconds=HG.PUBLISH_MAX_AGE_S + 1)
    assert not HG.published(HG.latest(windows), later)
    stale = HG.attributes(windows, later, TZ)
    assert (stale["quality"], stale["reason"]) == (HG.BAD, "counter")
    assert HG.published(HG.latest(windows), end + timedelta(seconds=HG.PUBLISH_MAX_AGE_S))
    assert not HG.published(w_at(NOON, quality=HG.BAD, reason="snow"), now)
    assert not HG.published(HG.Window(NOON + 2700, 50.0, 0.2, 400.0, HG.BAD, "snow", 25.0), now)  # quality decides
    assert HG.attributes({}, now, TZ)["window_start"] is None


# ---------------------------------------------------------------------------
# Store section, version, retention, export
# ---------------------------------------------------------------------------


def test_section_validation():
    good = {"windows": {str(NOON): [100.0, 0.25, 400.0, "good", None, 25.0],
                        str(NOON + 900): [None, None, 400.0, "bad", "counter", 25.0]},
            "version_history": [["2026-11-16T00:00:00+00:00", 1, "note"]]}
    assert HG.validate_section(good)["windows"] == good["windows"]
    assert HG.validate_section(None) == HG.new_section()
    bad_cases = [
        [],
        {"windows": []},
        {"windows": {"x": [100.0, 0.2, 400.0, "good", None, 25.0]}},
        {"windows": {str(NOON): [100.0, 0.2, 400.0, "good", None]}},
        {"windows": {str(NOON): [100.0, 0.2, 400.0, "fine", None, 25.0]}},
        {"windows": {str(NOON): [100.0, 0.2, 400.0, "uncertain", "fog", 25.0]}},
        {"windows": {str(NOON): [float("nan"), 0.2, 400.0, "good", None, 25.0]}},
        {"windows": {str(NOON): [100.0, 0.2, None, "good", None, 25.0]}},
        {"windows": {str(NOON): [100.0, 0.2, 400.0, "bad", "snow", 25.0]}},
        {"windows": {str(NOON): [None, 0.2, 400.0, "uncertain", "low_sun", 25.0]}},
        {"windows": {str(NOON): [100.0, 0.2, 400.0, "good", "low_sun", 25.0]}},
        {"windows": {str(NOON): [100.0, 0.2, 400.0, "uncertain", None, 25.0]}},
        {"windows": {}, "version_history": [["2026-11-16", "1", "note"]]},
        {"windows": {}, "version_history": "1"},
    ]
    for case in bad_cases:
        with pytest.raises(HG.HouseGhiDataError):
            HG.validate_section(case)


def test_malformed_section_rejects_the_store():
    raw = L.LearningData().to_json()
    raw["house_ghi"] = {"windows": {str(NOON): [100.0, 0.2, 400.0, "great", None, 25.0]}, "version_history": []}
    with pytest.raises(L.LearningDataError):
        L.LearningData.from_json(raw)


def test_store_without_the_section_gets_an_empty_one_and_round_trips():
    raw = L.LearningData().to_json()
    del raw["house_ghi"]
    data = L.LearningData.from_json(raw)
    assert data.d["house_ghi"] == HG.new_section()
    HG.store_window(data.d["house_ghi"], w_at(NOON))
    again = L.LearningData.from_json(__import__("json").loads(__import__("json").dumps(data.to_json())))
    assert again.d["house_ghi"]["windows"][str(NOON)] == w_at(NOON).row()


def test_view_copies_the_windows():
    data = L.LearningData()
    HG.store_window(data.d["house_ghi"], w_at(NOON))
    view = data.view()
    HG.store_window(data.d["house_ghi"], w_at(NOON + 900))
    assert str(NOON + 900) not in view.d["house_ghi"]["windows"]


def test_version_is_noted_once():
    section = HG.new_section()
    now = datetime(2026, 11, 16, 8, tzinfo=UTC)
    assert HG.note_version(section, now) is True
    assert HG.note_version(section, now + timedelta(days=1)) is False
    assert section["version_history"] == [[now.isoformat(), HG.VERSION, HG.VERSION_NOTE]]
    section["version_history"] = [["2026-01-01T00:00:00+00:00", 0, "older"]]
    assert HG.note_version(section, now) is True and len(section["version_history"]) == 2
    assert HG.version_at(section, int(now.timestamp()) - 1) == 0
    assert HG.version_at(section, int(now.timestamp())) == HG.VERSION


def test_retention_is_two_years():
    section = HG.new_section()
    today = "2026-11-16"
    old = int(datetime(2024, 10, 16, 12, tzinfo=UTC).timestamp())  # 761 days before
    kept = int(datetime(2024, 10, 18, 12, tzinfo=UTC).timestamp())  # 759 days before
    for start in (old, kept):
        HG.store_window(section, w_at(start))
    assert HG.purge(section, today, TZ) == 1
    assert list(section["windows"]) == [str(kept)]
    data = L.LearningData()
    HG.store_window(data.d["house_ghi"], w_at(old))
    HG.store_window(data.d["house_ghi"], w_at(kept))
    data.purge(datetime(2026, 11, 16, 12, tzinfo=UTC), TZ)
    assert list(data.d["house_ghi"]["windows"]) == [str(kept)]


def test_export_rows():
    section = HG.new_section()
    HG.note_version(section, datetime(2026, 10, 1, tzinfo=UTC))
    HG.store_window(section, w_at(NOON + 900, quality=HG.BAD, reason="clipping"))
    HG.store_window(section, w_at(NOON))
    rows, header, day_rows, day_header = HG.export_rows(section, TZ)
    assert header == ["window_start_utc", "window_end_utc", "ghi_w_m2", "clear_sky_index", "clear_sky_ghi_w_m2",
                      "quality", "reason", "sun_elevation_deg", "version"]
    assert rows[0] == [datetime.fromtimestamp(NOON, tz=UTC).isoformat(),
                       datetime.fromtimestamp(NOON + 900, tz=UTC).isoformat(), 100.0, 0.3, 400.0, "good", None,
                       25.0, HG.VERSION]
    assert rows[1][5:7] == ["bad", "clipping"] and rows[1][2] is None
    assert day_header[:5] == ["date", "windows", "good", "uncertain", "bad"]
    assert "bad_curtailment" in day_header and "uncertain_mixed_sky" in day_header
    assert day_rows == [["2026-10-07", 2, 1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0]]


def test_settings_export_names_the_version_and_rules():
    data = L.LearningData()
    HG.note_version(data.d["house_ghi"], datetime(2026, 11, 16, tzinfo=UTC))
    settings = L.export_settings(data, [], TZ, LAT, LON)["house_ghi"]
    assert settings["version"] == HG.VERSION and len(settings["version_history"]) == 1
    assert settings["parameters"]["low_sun_bad_deg"] == 10.0 and settings["parameters"]["clip_bad"] == 0.95
