"""Shading table format and its application to irradiance (0.1.33.5)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pvlib
import pytest

from custom_components.open_meteo_solar_forecast import shading as sh
from custom_components.open_meteo_solar_forecast.local_source import sun_position
from custom_components.open_meteo_solar_forecast.shading import (
    ShadingError,
    load_shading_text,
    parse_shading_table,
    shade_payload,
    solar_position,
)

LAT, LON = 47.5548, 8.9036


def grid_text(azs, els, fn, month=None) -> str:
    rows = []
    for az in azs:
        for el in els:
            prefix = f"{month}\t" if month is not None else ""
            rows.append(f"{prefix}{az}\t{el}\t{fn(az, el):.4f}")
    return "\n".join(rows) + "\n"


def k(table, az, el, month=6):
    return float(table.factor(np.array(month), np.array(az), np.array(el)))


# ---------------------------------------------------------------------------
# Table format
# ---------------------------------------------------------------------------
def test_full_circle_table_wraps_between_last_and_first_azimuth():
    t = parse_shading_table(grid_text(range(0, 360, 10), (0, 90), lambda a, e: 1.0 if a == 0 else 0.0))
    assert t.wraps
    assert k(t, 355, 0) == pytest.approx(0.5)
    assert k(t, 5, 0) == pytest.approx(0.5)
    assert k(t, 180, 45) == 0.0


def test_table_with_0_and_360_does_not_double_wrap():
    t = parse_shading_table(grid_text((0, 180, 360), (0, 90), lambda a, e: 0.0 if a == 180 else 1.0))
    assert not t.wraps
    assert k(t, 90, 10) == pytest.approx(0.5)
    assert k(t, 359, 10) == pytest.approx(1.0, abs=0.01)


def test_partial_azimuth_range_means_unshaded_outside():
    t = parse_shading_table(grid_text((60, 100, 140), (0, 30), lambda a, e: 0.2))
    assert not t.wraps
    assert k(t, 100, 10) == pytest.approx(0.2)
    assert k(t, 200, 10) == 1.0
    assert k(t, 30, 10) == 1.0


def test_elevation_clamped_below_and_unshaded_above():
    t = parse_shading_table(grid_text(range(0, 360, 90), (10, 30), lambda a, e: 0.0 if e == 10 else 0.5))
    assert k(t, 90, 2) == 0.0  # below the lowest row: lowest row
    assert k(t, 90, 20) == pytest.approx(0.25)
    assert k(t, 90, 31) == 1.0  # above the table: no information = unshaded


def test_bilinear_interpolation():
    t = parse_shading_table(grid_text((0, 10, 350), (0, 10), lambda a, e: {0: 0.0, 10: 1.0, 350: 0.0}[a] * (e / 10)))
    # wraps? 0,10,350 is not regular -> no wrap
    assert not t.wraps
    assert k(t, 5, 5) == pytest.approx(0.25)


def test_comments_separators_and_blank_lines():
    text = "# my roof\n\n0, 0, 1\n0;90;1 # trailing\n180 0 0.5\n180\t90\t0.5\n"
    t = parse_shading_table(text)
    assert k(t, 180, 45) == pytest.approx(0.5)


def test_monthly_table():
    text = "".join(grid_text((0, 180), (0, 90), lambda a, e, m=m: m / 12, month=m) for m in range(1, 13))
    t = parse_shading_table(text)
    assert t.monthly
    assert k(t, 90, 10, month=1) == pytest.approx(1 / 12, abs=1e-4)
    assert k(t, 90, 10, month=12) == pytest.approx(1.0, abs=1e-4)


BAD_TABLES = [
    ("0\t0\n", "3 columns"),
    ("0 0 1\n0 90 1 5\n", "expected 3 columns"),
    ("0 0 x\n", "not a number"),
    ("0 0 nan\n0 90 1\n180 0 1\n180 90 1\n", "non-finite"),
    ("0 0 1.5\n0 90 1\n180 0 1\n180 90 1\n", "factor"),
    ("0 -5 1\n0 90 1\n180 -5 1\n180 90 1\n", "elevation"),
    ("400 0 1\n0 90 1\n400 90 1\n0 0 1\n", "azimuth"),
    ("0 0 1\n0 0 1\n180 0 1\n180 90 1\n", "duplicate"),
    ("0 0 1\n0 90 1\n180 0 1\n", "complete grid"),
    ("0 0 1\n180 0 1\n", "at least 2"),
    ("# only comments\n", "no data"),
    ("13 0 0 1\n", "month"),
    ("1 0 0 1\n1 0 90 1\n1 180 0 1\n1 180 90 1\n", "12 months"),
]


@pytest.mark.parametrize(("text", "fragment"), BAD_TABLES)
def test_invalid_tables_are_rejected_with_a_clear_message(text, fragment):
    with pytest.raises(ShadingError) as err:
        parse_shading_table(text)
    assert fragment in str(err.value)


def test_table_row_limit(monkeypatch):
    monkeypatch.setattr(sh, "TABLE_MAX_ROWS", 3)
    with pytest.raises(ShadingError, match="rows"):
        parse_shading_table("0 0 1\n0 90 1\n180 0 1\n180 90 1\n")


def test_table_size_limit():
    with pytest.raises(ShadingError, match="larger"):
        parse_shading_table("#" * (sh.SHADING_FILE_MAX_BYTES + 1))


def test_load_dispatches_on_the_file_name():
    table = load_shading_text("0 0 1\n0 90 1\n180 0 1\n180 90 1\n", "/config/roof.txt")
    assert table.source == "table"
    geometry = "version: 1\narray: {azimuth: 180, tilt: 30, origin: [0,0,0], rows: 1, columns: 1, module_width: 1, module_height: 1}\n"
    assert load_shading_text(geometry, "/config/roof.YAML").source == "geometry"
    with pytest.raises(ShadingError):
        load_shading_text(geometry, "/config/roof.txt")  # YAML is not a table


# ---------------------------------------------------------------------------
# Vectorised solar position
# ---------------------------------------------------------------------------
def test_vectorised_sun_matches_the_engine_and_pvlib():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    moments = [start + timedelta(hours=7 * i + 0.3) for i in range(1300)]
    ts = np.array([m.timestamp() for m in moments])
    cz, az, el = solar_position(ts, LAT, LON)
    for i in range(0, 1300, 97):
        ref = sun_position(moments[i], LAT, LON)
        assert cz[i] == pytest.approx(ref.cos_zenith, abs=1e-9)
        if el[i] > 0:
            assert az[i] == pytest.approx(math.degrees(ref.azimuth_rad), abs=1e-6)
    sp = pvlib.solarposition.get_solarposition(pd.DatetimeIndex(moments), LAT, LON, method="nrel_numpy")
    day = sp["elevation"].values > 2
    assert np.max(np.abs(el[day] - sp["elevation"].values[day])) < 0.1
    d = np.abs(((az[day] - sp["azimuth"].values[day]) + 180) % 360 - 180)
    assert np.max(d) < 0.15


# ---------------------------------------------------------------------------
# Application to an Open-Meteo-shaped payload
# ---------------------------------------------------------------------------
TILT, PANEL_AZ = 45.0, 197.0


def clear_payload(day="2026-03-21", n_days=1):
    """Open-Meteo minutely_15 shape from pvlib clear-sky, plane via Hay-Davies."""
    times = pd.date_range(day, periods=96 * n_days, freq="15min", tz="UTC") + pd.Timedelta("15min")
    loc = pvlib.location.Location(LAT, LON, altitude=473)
    mid = times - pd.Timedelta("7.5min")
    sp = loc.get_solarposition(mid)
    cs = loc.get_clearsky(mid, solar_position=sp)
    poa = pvlib.irradiance.get_total_irradiance(TILT, PANEL_AZ, sp["apparent_zenith"], sp["azimuth"],
                                                cs["dni"], cs["ghi"], cs["dhi"], model="isotropic")
    sp_i = loc.get_solarposition(times)
    cs_i = loc.get_clearsky(times, solar_position=sp_i)
    poa_i = pvlib.irradiance.get_total_irradiance(TILT, PANEL_AZ, sp_i["apparent_zenith"], sp_i["azimuth"],
                                                  cs_i["dni"], cs_i["ghi"], cs_i["dhi"], model="isotropic")
    direct = (cs["ghi"] - cs["dhi"]).clip(lower=0)
    direct_i = (cs_i["ghi"] - cs_i["dhi"]).clip(lower=0)
    payload = {"minutely_15": {
        "time": [int(t.timestamp()) for t in times],
        "global_tilted_irradiance": [float(v) for v in poa["poa_global"].fillna(0)],
        "global_tilted_irradiance_instant": [float(v) for v in poa_i["poa_global"].fillna(0)],
        "direct_radiation": [float(v) for v in direct],
        "direct_radiation_instant": [float(v) for v in direct_i],
        "diffuse_radiation": [float(v) for v in cs["dhi"]],
        "diffuse_radiation_instant": [float(v) for v in cs_i["dhi"]],
    }}
    beam = poa["poa_direct"].fillna(0).values
    beam_i = poa_i["poa_direct"].fillna(0).values
    return payload, beam, beam_i, sp_i["elevation"].values


def const_table(value: float):
    return parse_shading_table(grid_text(range(0, 360, 10), (0, 90), lambda a, e: value))


def test_factor_one_leaves_the_payload_unchanged():
    payload, *_ = clear_payload()
    before = [list(payload["minutely_15"][k]) for k in ("global_tilted_irradiance", "global_tilted_irradiance_instant")]
    stats = shade_payload(payload, const_table(1.0), LAT, LON, TILT, PANEL_AZ)
    after = [payload["minutely_15"][k] for k in ("global_tilted_irradiance", "global_tilted_irradiance_instant")]
    for b, a in zip(before, after):
        assert np.allclose(a, b, atol=1e-9)
    assert stats.removed_wh_m2 == 0


def test_factor_zero_removes_exactly_the_direct_light_on_the_plane():
    payload, beam, beam_i, elev = clear_payload()
    gti = np.array(payload["minutely_15"]["global_tilted_irradiance"])
    gti_i = np.array(payload["minutely_15"]["global_tilted_irradiance_instant"])
    shade_payload(payload, const_table(0.0), LAT, LON, TILT, PANEL_AZ)
    new = np.array(payload["minutely_15"]["global_tilted_irradiance"])
    new_i = np.array(payload["minutely_15"]["global_tilted_irradiance_instant"])
    day = elev > 5
    # instants: exact geometry -> what is left is the diffuse part
    assert np.allclose(new_i[day], (gti_i - beam_i)[day], atol=2.0)
    # averages: three sub-moments per quarter; daily energy within 1 %
    removed = (gti - new).sum()
    assert removed == pytest.approx(beam.sum(), rel=0.01)
    assert np.all(new >= 0)


def test_half_factor_removes_half_the_direct_light():
    payload, beam, beam_i, elev = clear_payload()
    gti = np.array(payload["minutely_15"]["global_tilted_irradiance"])
    shade_payload(payload, const_table(0.5), LAT, LON, TILT, PANEL_AZ)
    removed = gti - np.array(payload["minutely_15"]["global_tilted_irradiance"])
    assert removed.sum() == pytest.approx(0.5 * beam.sum(), rel=0.01)


def test_diffuse_and_direct_columns_are_never_changed():
    payload, *_ = clear_payload()
    keep = {k: list(v) for k, v in payload["minutely_15"].items() if not k.startswith("global_tilted")}
    shade_payload(payload, const_table(0.0), LAT, LON, TILT, PANEL_AZ)
    for name, values in keep.items():
        assert payload["minutely_15"][name] == values


def test_none_values_and_night_are_preserved():
    payload, *_ = clear_payload()
    m15 = payload["minutely_15"]
    m15["global_tilted_irradiance"][40] = None
    m15["direct_radiation"][41] = None
    night_before = m15["global_tilted_irradiance"][:10]
    shade_payload(payload, const_table(0.0), LAT, LON, TILT, PANEL_AZ)
    assert m15["global_tilted_irradiance"][40] is None
    assert m15["global_tilted_irradiance"][:10] == night_before


def _sunrise_minutes(lo: float, hi: float):
    """Unix times (1-minute grid, 21 March) with lo < sun elevation < hi."""
    t0 = datetime(2026, 3, 21, 4, 30, tzinfo=timezone.utc).timestamp()
    ts = t0 + 60.0 * np.arange(120)
    _, _, el = solar_position(ts, LAT, LON)
    pick = ts[(el > lo) & (el < hi)]
    assert len(pick) >= 2
    return [int(t) for t in pick]


SUB_MOMENTS = np.array([-12.5, -7.5, -2.5]) * 60.0  # inside the quarter ending at the label


def _sunrise_quarter() -> tuple[int, np.ndarray]:
    """A quarter label whose sub-moments are below 0, between 0 and 1, and
    above 1 deg (only the last one may carry direct light)."""
    t0 = datetime(2026, 3, 21, 4, 30, tzinfo=timezone.utc).timestamp()
    for label in t0 + 30.0 * np.arange(240):
        _, _, el = solar_position(label + SUB_MOMENTS, LAT, LON)
        if el[0] < 0 < el[1] < sh.MIN_SUN_ELEVATION_DEG < el[2]:
            return int(label), el
    raise AssertionError("no suitable label")


def test_low_sun_direct_light_is_not_touched():
    # 0 < elevation < 1 deg: direct light is treated as diffuse (as in
    # local_source), so even a factor of 0 must not remove anything
    times = _sunrise_minutes(0.1, 0.9)
    n = len(times)
    payload = {"minutely_15": {
        "time": times, "global_tilted_irradiance": [50.0] * n, "global_tilted_irradiance_instant": [50.0] * n,
        "direct_radiation": [5.0] * n, "direct_radiation_instant": [5.0] * n}}
    shade_payload(payload, const_table(0.0), LAT, LON, TILT, 90.0)
    assert payload["minutely_15"]["global_tilted_irradiance_instant"] == [50.0] * n


def test_quarter_average_ignores_sub_moments_below_one_degree():
    # only the sub-moment above 1 deg carries direct light:
    # removed = horizontal direct x cos(AOI) / cos(Z) of that moment
    label, _ = _sunrise_quarter()
    payload = {"minutely_15": {"time": [label], "global_tilted_irradiance": [5000.0],
                               "global_tilted_irradiance_instant": [5000.0],
                               "direct_radiation": [5.0], "direct_radiation_instant": [0.0]}}
    shade_payload(payload, const_table(0.0), LAT, LON, TILT, 90.0)
    cz, az, _ = solar_position(np.array([label + SUB_MOMENTS[2]]), LAT, LON)
    expected = 5.0 * float(sh._cos_aoi(cz, az, TILT, 90.0)[0]) / float(cz[0])
    assert 5000.0 - payload["minutely_15"]["global_tilted_irradiance"][0] == pytest.approx(expected, rel=1e-6)


def test_dni_is_capped_physically():
    # sun at 3-4 deg: horizontal direct 800 W/m2 would mean DNI > 10 000
    times = _sunrise_minutes(3.0, 4.0)[:1]
    t = times[0]
    payload = {"minutely_15": {"time": times, "global_tilted_irradiance": [5000.0],
                               "global_tilted_irradiance_instant": [5000.0],
                               "direct_radiation": [800.0], "direct_radiation_instant": [800.0]}}
    shade_payload(payload, const_table(0.0), LAT, LON, TILT, 90.0)
    # instant
    cz, az, el = solar_position(np.array([t], float), LAT, LON)
    caoi = float(sh._cos_aoi(cz, az, TILT, 90.0)[0])
    removed = 5000.0 - payload["minutely_15"]["global_tilted_irradiance_instant"][0]
    assert removed == pytest.approx(sh.MAX_DNI_W_M2 * caoi, rel=1e-6)
    # quarter-hour average: DNI capped as well
    cz3, az3, el3 = solar_position(t + SUB_MOMENTS, LAT, LON)
    caoi3 = np.where(el3 > sh.MIN_SUN_ELEVATION_DEG, np.clip(sh._cos_aoi(cz3, az3, TILT, 90.0), 0, None), 0)
    removed_avg = 5000.0 - payload["minutely_15"]["global_tilted_irradiance"][0]
    assert removed_avg == pytest.approx(sh.MAX_DNI_W_M2 * caoi3.mean(), rel=1e-6)


def test_mismatched_columns_leave_the_payload_alone():
    payload = {"minutely_15": {"time": [1, 2, 3], "global_tilted_irradiance": [1.0, 2.0]}}
    stats = shade_payload(payload, const_table(0.0), LAT, LON, TILT, PANEL_AZ)
    assert payload["minutely_15"]["global_tilted_irradiance"] == [1.0, 2.0]
    assert stats.values == 0
    assert shade_payload({}, const_table(0.0), LAT, LON, TILT, PANEL_AZ).values == 0


def test_month_selects_the_monthly_factor():
    text = "".join(grid_text(range(0, 360, 10), (0, 90), lambda a, e, m=m: 0.0 if m == 6 else 1.0, month=m)
                   for m in range(1, 13))
    t = parse_shading_table(text)
    june, beam, *_ = clear_payload("2026-06-21")
    dec, beam_d, *_ = clear_payload("2026-12-21")
    g_june = np.array(june["minutely_15"]["global_tilted_irradiance"])
    g_dec = np.array(dec["minutely_15"]["global_tilted_irradiance"])
    shade_payload(june, t, LAT, LON, TILT, PANEL_AZ)
    shade_payload(dec, t, LAT, LON, TILT, PANEL_AZ)
    assert (g_june - np.array(june["minutely_15"]["global_tilted_irradiance"])).sum() == pytest.approx(beam.sum(), rel=0.01)
    assert np.allclose(np.array(dec["minutely_15"]["global_tilted_irradiance"]), g_dec)


def test_stats_report_the_removed_share():
    payload, beam, *_ = clear_payload()
    gti = np.array(payload["minutely_15"]["global_tilted_irradiance"])
    stats = shade_payload(payload, const_table(0.0), LAT, LON, TILT, PANEL_AZ)
    assert stats.as_dict()["removed_share"] == pytest.approx(beam.sum() / gti.sum(), rel=0.02)
    assert stats.shaded_values > 20
