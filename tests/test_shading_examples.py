"""Shipped shading examples and the South geometry of the owner's roof (0.1.33.5)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from custom_components.open_meteo_solar_forecast.shading import load_shading_text, parse_shading_table

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "shading"
COMPONENT = ROOT / "custom_components" / "open_meteo_solar_forecast"

# Direct-light factors of the fitted 3-D model used in the analysis of
# 2026-10-11 (tall wing 11 m, gap 1 m, chimney), which reproduced the
# measured Feb-Oct clear-day losses at the noise level of hourly data.
ANALYSIS = {
    (96, 25): 0.00, (96, 33): 0.52, (96, 41): 0.87,
    (108, 17): 0.00, (108, 25): 0.27, (108, 33): 0.79, (108, 41): 0.91,
    (120, 17): 0.00, (120, 25): 0.61, (120, 33): 0.91, (120, 41): 0.97,
    (132, 17): 0.30, (132, 25): 0.86, (132, 33): 1.00,
    (84, 33): 0.00, (84, 41): 0.61, (72, 41): 0.31,
}


def _k(table, az, el, month=6):
    return float(table.factor(np.array(month), np.array(az), np.array(el)))


# 0.1.34: the shipped South geometry is the 40-degree refit (owner, 12 Oct
# 2026: all roofs 40 deg). Its values at the same sun positions, computed by
# this engine when the refit was accepted (regression pin; the independent
# evidence is the fit against measured production, release audit 0.1.34).
REFIT_40 = {
    (96, 25): 0.00, (96, 33): 0.62, (96, 41): 0.85,
    (108, 17): 0.00, (108, 25): 0.39, (108, 33): 0.85, (108, 41): 0.94,
    (120, 17): 0.06, (120, 25): 0.69, (120, 33): 0.97, (120, 41): 0.97,
    (132, 17): 0.32, (132, 25): 0.89, (132, 33): 1.00,
    (84, 33): 0.25, (84, 41): 0.72, (72, 41): 0.40,
}


@pytest.fixture(scope="module")
def south():
    path = EXAMPLES / "south_geometry.yaml"
    return load_shading_text(path.read_text(), str(path))


@pytest.fixture(scope="module")
def south_45():
    path = EXAMPLES / "south_geometry_45deg.yaml"
    return load_shading_text(path.read_text(), str(path))


def test_south_geometry_45_reproduces_the_fitted_analysis_model(south_45):
    for (az, el), expected in ANALYSIS.items():
        assert _k(south_45, az, el) == pytest.approx(expected, abs=0.07), (az, el)


def test_south_geometry_40_is_the_accepted_refit(south):
    assert south.summary["array_tilt"] == 40.0
    for (az, el), expected in REFIT_40.items():
        assert _k(south, az, el) == pytest.approx(expected, abs=0.01), (az, el)


def test_south_refit_40_stays_close_to_the_45_fit(south, south_45):
    # Both fits explain the same measurements; they may differ at the shadow
    # edges (different roof geometry) but not in substance.
    for az, el in ANALYSIS:
        assert abs(_k(south, az, el) - _k(south_45, az, el)) <= 0.25, (az, el)


def test_south_geometry_is_unshaded_from_midday_on(south):
    # data: no loss for sun azimuth >= 140 deg at the elevations observed
    # (Feb-Oct, >= 24 deg); the model adds low winter sun at 130-160 deg
    for az in range(170, 270, 10):
        for el in (4, 10, 20, 30, 50):
            assert _k(south, az, el) == 1.0, (az, el)
    for az in (140, 150, 160):
        assert _k(south, az, 30) == 1.0
    assert _k(south, 140, 10) < 0.5  # winter morning: still behind the tall wing


def test_south_shipped_table_equals_the_geometry(south):
    table = parse_shading_table((EXAMPLES / "south_shading_table.txt").read_text())
    assert np.allclose(table.factors, south.factors, atol=1e-4)


def test_reference_example_with_all_types_is_valid():
    path = EXAMPLES / "example_all_types.yaml"
    table = load_shading_text(path.read_text(), str(path))
    assert table.monthly  # the birch is see-through in winter
    assert table.summary["obstacles"] == 6
    assert 0.05 < table.summary["share_of_sun_positions_shaded"] < 0.8
    assert _k(table, 120, 10, month=1) >= _k(table, 120, 10, month=7)


def test_command_line_tool_writes_the_same_table(tmp_path):
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "shading_table.py"),
                          str(EXAMPLES / "south_geometry.yaml")], capture_output=True, text=True, check=True)
    table = parse_shading_table(out.stdout)
    shipped = parse_shading_table((EXAMPLES / "south_shading_table.txt").read_text())
    assert np.allclose(table.factors, shipped.factors, atol=1e-4)
    bad = tmp_path / "bad.txt"
    bad.write_text("1 2\n")
    proc = subprocess.run([sys.executable, str(ROOT / "tools" / "shading_table.py"), str(bad), "--check"],
                          capture_output=True, text=True)
    assert proc.returncode == 2 and "invalid" in proc.stderr


@pytest.mark.parametrize("name", ["strings.json", "translations/en.json", "translations/de.json"])
def test_shading_strings_exist_in_every_language(name):
    data = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
    for flow in ("config", "options"):
        step = data[flow]["step"]["array"]
        assert step["data"]["shading_filepath"]
        assert step["data_description"]["shading_filepath"]
        assert "{shading_error}" in data[flow]["error"]["invalid_shading_file"]
        assert data[flow]["error"]["shading_requires_fixed_array"]
    issue = data["issues"]["invalid_shading_file"]
    for placeholder in ("{array}",):
        assert placeholder in issue["title"]
    for placeholder in ("{path}", "{error}"):
        assert placeholder in issue["description"]


def test_shading_module_stays_free_of_home_assistant():
    text = (COMPONENT / "shading.py").read_text()
    assert "homeassistant" not in text
    assert "open(" not in text  # no file I/O in the pure module
