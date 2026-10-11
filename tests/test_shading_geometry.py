"""Geometry shading (0.1.33.5): ray tracing against hand-calculable cases.

Every obstacle type is checked against a configuration whose shadow edge can
be computed with a pocket calculator (tan(elevation) = height / distance),
plus the electrical (bypass-diode) model against hand-worked values.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from custom_components.open_meteo_solar_forecast import shading as sh
from custom_components.open_meteo_solar_forecast.shading import (
    ShadingError,
    electrical_factor,
    geometry_table,
    parse_geometry,
    parse_geometry_yaml,
)


def flat_array(size: float = 0.1, **extra) -> dict:
    """A tiny horizontal module at the origin: shading is decided by one point."""
    return {
        "azimuth": 180, "tilt": 0, "origin": [-size / 2, -size / 2, 0.0],
        "rows": 1, "columns": 1, "module_width": size, "module_height": size, "gap": 0,
        **extra,
    }


def doc(obstacles: list, array: dict | None = None, **root) -> dict:
    return {"version": 1, "array": array or flat_array(), "obstacles": obstacles, **root}


def k_at(table, az: float, el: float, month: int = 6) -> float:
    return float(table.factor(np.array(month), np.array(az), np.array(el)))


# ---------------------------------------------------------------------------
# Obstacle types against analytic shadow edges
# ---------------------------------------------------------------------------
def test_wall_polygon_shadow_edge_is_atan_height_over_distance():
    # vertical wall 5 m east of the module, 5 m high, 100 m long: edge at 45 deg
    wall = {"type": "polygon", "vertices": [[5, -50, 0], [5, 50, 0], [5, 50, 5], [5, -50, 5]]}
    t = geometry_table(parse_geometry(doc([wall])))
    assert k_at(t, 90, 42) == 0.0
    assert k_at(t, 90, 48) == 1.0
    assert k_at(t, 270, 10) == 1.0  # sun on the other side
    assert k_at(t, 180, 30) == 1.0


def test_box_shadow_edge():
    box = {"type": "box", "center": [6, 0], "width": 2, "depth": 100, "azimuth": 90, "top": 5}
    t = geometry_table(parse_geometry(doc([box])))
    # near face at x = 5: edge atan(5/5) = 45 deg
    assert k_at(t, 90, 44) == 0.0
    assert k_at(t, 90, 46) == 1.0


def test_box_rotation_is_respected():
    # 2 m wide (east-west) when azimuth = 90, a 2 m deep slab when rotated 90 deg
    slab = {"type": "box", "center": [0, 6], "width": 2, "depth": 0.2, "azimuth": 90, "top": 3}
    t = geometry_table(parse_geometry(doc([slab])))
    assert k_at(t, 0, 20) == 0.0  # straight north: hits the slab (x within +-1)
    assert k_at(t, 20, 20) == 1.0  # 20 deg off: lateral offset 6*tan20 = 2.2 m > 1 m


def test_gable_building_is_shaded_by_the_ridge_not_the_eave():
    # ridge along north at x = 10, eave 2 m at x = 8 and 12, ridge 4 m:
    # eave angle atan(2/8) = 14.0 deg, ridge angle atan(4/10) = 21.8 deg
    house = {"type": "gable_building", "center": [10, 0], "ridge_azimuth": 0, "length": 200,
             "width": 4, "eave_height": 2, "ridge_height": 4, "bottom": -1}
    t = geometry_table(parse_geometry(doc([house])))
    assert k_at(t, 90, 20) == 0.0
    assert k_at(t, 90, 24) == 1.0


def test_gable_building_orientation_matters():
    # ridge along east-west: from the south side the gable end (2 m eave,
    # 4 m ridge, half width 2) is narrow; looking east along y = 0 the ridge
    # line is hit (4 m at 10 m)
    house = {"type": "gable_building", "center": [20, 0], "ridge_azimuth": 90, "length": 20,
             "width": 4, "eave_height": 2, "ridge_height": 4, "bottom": -1}
    t = geometry_table(parse_geometry(doc([house])))
    assert k_at(t, 90, 20) == 0.0  # 4 m at 10 m distance = 21.8 deg
    assert k_at(t, 90, 24) == 1.0


def test_cylinder_tree():
    # radius 1 at x = 10, 0..10 m: entry face at 9 m -> atan(10/9) = 48.0 deg
    tree = {"type": "cylinder", "center": [10, 0], "radius": 1, "bottom": 0, "top": 10}
    t = geometry_table(parse_geometry(doc([tree])))
    assert k_at(t, 90, 46) == 0.0
    assert k_at(t, 90, 50) == 1.0
    # lateral miss: offset 10*sin(dAz) > 1 m for dAz > 5.7 deg
    assert k_at(t, 100, 20) == 1.0
    assert k_at(t, 94, 20) == 0.0


def test_cone_tree_apex():
    # radius 2 at the bottom, apex 10 m at x = 10: limit tan(el) <= 1 -> 45 deg
    cone = {"type": "cone", "center": [10, 0], "radius": 2, "bottom": 0, "top": 10}
    t = geometry_table(parse_geometry(doc([cone])))
    assert k_at(t, 90, 44) == 0.0
    assert k_at(t, 90, 46) == 1.0


def test_cone_with_radius_top_is_a_frustum():
    frustum = {"type": "cone", "center": [10, 0], "radius": 2, "radius_top": 2, "bottom": 0, "top": 10}
    t = geometry_table(parse_geometry(doc([frustum])))
    # behaves like a cylinder of radius 2: entry at 8 m -> atan(10/8) = 51.3 deg
    assert k_at(t, 90, 50) == 0.0
    assert k_at(t, 90, 52) == 1.0


def test_concave_polygon_uses_even_odd_rule():
    # U-shaped wall 5 m east: a notch between y = -1 and 1 above z = 2
    u_wall = {"type": "polygon", "vertices": [
        [5, -3, 0], [5, 3, 0], [5, 3, 6], [5, 1, 6], [5, 1, 2], [5, -1, 2], [5, -1, 6], [5, -3, 6]]}
    t = geometry_table(parse_geometry(doc([u_wall])))
    assert k_at(t, 90, 30) == 1.0  # through the notch (z at 5 m = 2.9 m > 2)
    assert k_at(t, 90, 20) == 0.0  # below the notch floor (1.8 m)


def test_obstacle_behind_the_module_casts_no_shadow():
    wall = {"type": "polygon", "vertices": [[-5, -50, 0], [-5, 50, 0], [-5, 50, 5], [-5, -50, 5]]}
    t = geometry_table(parse_geometry(doc([wall])))
    assert k_at(t, 90, 10) == 1.0


def test_sun_behind_the_panel_plane_is_not_shaded():
    # south-facing vertical module; a wall to the north never matters
    arr = flat_array(azimuth=180)
    arr.update({"tilt": 90})
    wall = {"type": "polygon", "vertices": [[-50, 5, 0], [50, 5, 0], [50, 5, 50], [-50, 5, 50]]}
    t = geometry_table(parse_geometry(doc([wall], array=arr)))
    assert k_at(t, 0, 10) == 1.0


def test_monthly_transparency_of_a_deciduous_tree():
    bare = [0.7, 0.7, 0.6, 0.3, 0, 0, 0, 0, 0, 0.2, 0.6, 0.7]
    tree = {"type": "cylinder", "center": [10, 0], "radius": 3, "top": 12, "transparency": bare}
    t = geometry_table(parse_geometry(doc([tree])))
    assert t.monthly
    assert k_at(t, 90, 30, month=1) == pytest.approx(0.7, abs=1e-6)
    assert k_at(t, 90, 30, month=7) == 0.0
    assert k_at(t, 90, 30, month=4) == pytest.approx(0.3, abs=1e-6)


def test_constant_transparency_is_not_monthly():
    tree = {"type": "cylinder", "center": [10, 0], "radius": 3, "top": 12, "transparency": 0.4}
    t = geometry_table(parse_geometry(doc([tree])))
    assert not t.monthly
    assert k_at(t, 90, 30) == pytest.approx(0.4, abs=1e-6)


def test_two_obstacles_multiply_transparency():
    a = {"type": "cylinder", "center": [10, 0], "radius": 3, "top": 12, "transparency": 0.5}
    b = {"type": "cylinder", "center": [20, 0], "radius": 3, "top": 30, "transparency": 0.5}
    t = geometry_table(parse_geometry(doc([a, b])))
    assert k_at(t, 90, 30) == pytest.approx(0.25, abs=1e-6)


# ---------------------------------------------------------------------------
# Electrical model, hand-worked
# ---------------------------------------------------------------------------
def _spec(cell="full", modules=1, strings=None):
    arr = flat_array(cell_type=cell)
    arr.update({"columns": modules})
    if strings is not None:
        arr["strings"] = strings
    return parse_geometry(doc([], array=arr)).array


def _light(modules=1, diodes=3, samples=1):
    return np.ones((1, modules, diodes, 2, samples))


def test_bypass_one_section_fully_shaded():
    spec = _spec()
    light = _light()
    light[0, 0, 0] = 0.0
    out = electrical_factor(light, spec, "bypass", np.array([800.0]), np.array([120.0]))
    # capabilities 120, 920, 920 -> best = max(3*120, 2*920) = 1840 of 3*920
    assert out[0] == pytest.approx(1840 / 2760)


def test_area_model_one_section_shaded():
    spec = _spec()
    light = _light()
    light[0, 0, 0] = 0.0
    out = electrical_factor(light, spec, "area", np.array([800.0]), np.array([120.0]))
    assert out[0] == pytest.approx((120 + 800 * 2 / 3) / 920)


def test_full_cell_one_shaded_cell_per_section_kills_the_module():
    spec = _spec("full")
    light = _light()
    light[0, 0, :, 0] = 0.0  # left half of every section shaded
    out = electrical_factor(light, spec, "bypass", np.array([800.0]), np.array([120.0]))
    assert out[0] == pytest.approx(360 / 2760)  # all sections at diffuse current


def test_half_cut_keeps_half_the_current():
    spec = _spec("half_cut")
    light = _light()
    light[0, 0, :, 0] = 0.0
    out = electrical_factor(light, spec, "bypass", np.array([800.0]), np.array([120.0]))
    assert out[0] == pytest.approx(3 * 520 / 2760)


def test_bypass_string_bypasses_the_weak_module():
    spec = _spec(modules=3)
    light = _light(modules=3)
    light[0, 2] = 0.0  # one of three modules fully shaded
    out = electrical_factor(light, spec, "bypass", np.array([800.0]), np.array([120.0]))
    # 6 sections at 920 (bypassing 3) beats 9 sections at 120
    assert out[0] == pytest.approx(6 * 920 / (9 * 920))


def test_separate_strings_do_not_drag_each_other():
    spec = _spec(modules=2, strings=[["r1c1"], ["r1c2"]])
    light = _light(modules=2)
    light[0, 1, :, 0] = 0.0  # module 2: every section partly shaded
    out = electrical_factor(light, spec, "bypass", np.array([800.0]), np.array([120.0]))
    one_string = _spec(modules=2)
    out_one = electrical_factor(light, one_string, "bypass", np.array([800.0]), np.array([120.0]))
    assert out[0] == pytest.approx((3 * 920 + 3 * 120) / (6 * 920))
    assert out[0] > out_one[0] or out[0] == pytest.approx(out_one[0])


def test_unshaded_is_exactly_one():
    spec = _spec(modules=3)
    out = electrical_factor(_light(modules=3), spec, "bypass", np.array([800.0]), np.array([120.0]))
    assert out[0] == pytest.approx(1.0)


def test_landscape_sections_are_horizontal_stripes():
    # landscape module (wide): a horizon-level shadow covering the lowest
    # third shades exactly one section -> 2/3 of sections remain
    arr = {"azimuth": 180, "tilt": 90, "origin": [-1, 0, 0], "rows": 1, "columns": 1,
           "module_width": 2.0, "module_height": 1.0, "gap": 0}
    wall = {"type": "polygon", "vertices": [[-50, -10, 0], [50, -10, 0], [50, -10, 3.4], [-50, -10, 3.4]]}
    t = geometry_table(parse_geometry(doc([wall], array=arr, electrical_model="area")))
    # sun due south at 18 deg: edge height at the module = 3.4 - 10*tan18 = 0.15 m
    # -> lowest stripe (0..0.33 m) partly shaded -> area model ~0.15
    k = k_at(t, 180, 18)
    assert 0.0 < 1 - k < 0.34


def test_portrait_sections_are_vertical_stripes():
    arr = {"azimuth": 180, "tilt": 90, "origin": [-0.5, 0, 0], "rows": 1, "columns": 1,
           "module_width": 1.0, "module_height": 2.0, "gap": 0}
    spec = parse_geometry(doc([], array=arr)).array
    pts = spec.sample_points(2, 2)
    # sections differ in x (across the short side), halves differ in z
    xs = pts[0, :, 0, :, 0].mean(axis=1)
    assert xs[0] < xs[1] < xs[2]
    zs = pts[0, 0, :, :, 2].mean(axis=1)
    assert zs[0] < zs[1]


# ---------------------------------------------------------------------------
# Validation: every rule gives a controlled message
# ---------------------------------------------------------------------------
BAD = [
    ({"version": 2, "array": flat_array()}, "version"),
    ({"version": 1}, "array: must be a mapping"),
    ({"version": 1, "array": flat_array(), "extra": 1}, "unknown key"),
    ({"version": 1, "array": {**flat_array(), "tilt": 95}}, "array.tilt"),
    ({"version": 1, "array": {**flat_array(), "rows": 0}}, "array.rows"),
    ({"version": 1, "array": {**flat_array(), "rows": 2.5}}, "whole number"),
    ({"version": 1, "array": {**flat_array(), "rows": 11, "columns": 11}}, "at most"),
    ({"version": 1, "array": {**flat_array(), "origin": [0, 0]}}, "array.origin"),
    ({"version": 1, "array": {**flat_array(), "origin": [0, 0, 5000]}}, "within"),
    ({"version": 1, "array": {**flat_array(), "origin": [0, 0, float("nan")]}}, "numbers"),
    ({"version": 1, "array": {**flat_array(), "cell_type": "thin"}}, "cell_type"),
    ({"version": 1, "array": {**flat_array(), "strings": [["r9c9"]]}}, "unknown module id"),
    ({"version": 1, "array": {**flat_array(), "columns": 2, "strings": [["r1c1"]]}}, "every module"),
    ({"version": 1, "array": {**flat_array(), "strings": [["r1c1"], ["r1c1"]]}}, "twice"),
    ({"version": 1, "array": flat_array(), "electrical_model": "magic"}, "electrical_model"),
    (doc([{"type": "pyramid"}]), "type"),
    (doc([{"type": "box", "center": [0, 0], "width": 1, "depth": 1, "top": -1}]), "top must be above"),
    (doc([{"type": "box", "center": [0, 0], "width": 0, "depth": 1, "top": 1}]), "width"),
    (doc([{"type": "box", "center": [0, 0], "width": 1, "depth": 1, "top": 1, "colour": "red"}]), "unknown key"),
    (doc([{"type": "polygon", "vertices": [[0, 0, 0], [1, 0, 0]]}]), "vertices"),
    (doc([{"type": "polygon", "vertices": [[0, 0, 0], [1, 0, 0], [2, 0, 0]]}]), "no area"),
    (doc([{"type": "polygon", "vertices": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 1]]}]), "one plane"),
    (doc([{"type": "gable_building", "center": [0, 0], "ridge_azimuth": 0, "length": 5, "width": 5,
           "eave_height": 3, "ridge_height": 2}]), "ridge_height"),
    (doc([{"type": "cylinder", "center": [0, 0], "radius": 1, "top": 5, "transparency": 2}]), "transparency"),
    (doc([{"type": "cylinder", "center": [0, 0], "radius": 1, "top": 5, "transparency": [0.5] * 11}]), "transparency"),
    (doc([{"type": "cylinder", "center": [0, 0], "radius": 1, "top": 5, "name": 7}]), "name"),
    (doc([{"type": "box", "center": [0, 0], "width": 1, "depth": 1, "top": 1}] * 65), "at most"),
    (doc("not a list"), "obstacles: must be a list"),
    ([1, 2, 3], "geometry: must be a mapping"),
]


@pytest.mark.parametrize(("data", "fragment"), BAD)
def test_invalid_geometry_is_rejected_with_a_clear_message(data, fragment):
    with pytest.raises(ShadingError) as err:
        parse_geometry(data)
    assert fragment in str(err.value)


def test_bad_yaml_reports_the_line():
    with pytest.raises(ShadingError, match=r"not valid YAML at line \d"):
        parse_geometry_yaml("version: 1\narray: [unclosed\n")


def test_yaml_with_python_tags_is_refused():
    with pytest.raises(ShadingError):
        parse_geometry_yaml("!!python/object/apply:os.system ['true']\n")


def test_oversized_yaml_is_refused():
    with pytest.raises(ShadingError, match="larger"):
        parse_geometry_yaml("#" * (sh.SHADING_FILE_MAX_BYTES + 10))


def test_work_budget_reduces_samples_then_refuses(monkeypatch):
    many = [{"type": "polygon", "vertices": [[5 + i, -50, 0], [5 + i, 50, 0], [5 + i, 50, 5]]} for i in range(10)]
    arr = {**flat_array(), "rows": 10, "columns": 12}
    geom = parse_geometry(doc(many, array=arr))
    monkeypatch.setattr(sh, "WORK_BUDGET", 5.0e8)
    reduced = geometry_table(geom)
    assert reduced.summary["sample_points_per_module"] < 24
    monkeypatch.setattr(sh, "WORK_BUDGET", 1.0e3)
    with pytest.raises(ShadingError, match="too complex"):
        geometry_table(geom)


def test_table_from_geometry_round_trips_through_the_text_format():
    tree = {"type": "cylinder", "center": [10, 0], "radius": 1, "top": 10}
    t = geometry_table(parse_geometry(doc([tree])))
    again = sh.parse_shading_table(t.to_text())
    assert np.allclose(again.factors, t.factors, atol=1e-4)
    assert again.wraps == t.wraps
    for az, el in ((90, 46), (94, 20), (300, 5)):
        assert k_at(again, az, el) == pytest.approx(k_at(t, az, el), abs=1e-3)


def test_geometry_summary_reports_what_was_built():
    tree = {"type": "cylinder", "center": [10, 0], "radius": 1, "top": 10}
    t = geometry_table(parse_geometry(doc([tree])))
    assert t.summary["obstacles"] == 1
    assert t.summary["modules"] == 1
    assert 0 < t.summary["share_of_sun_positions_shaded"] < 0.2
    assert math.isclose(t.summary["array_tilt"], 0.0)


def test_solid_overlapping_the_module_field_is_refused():
    chimney = {"type": "box", "name": "chimney", "center": [0, 0], "width": 1, "depth": 1, "top": 2}
    with pytest.raises(ShadingError, match=r"obstacles\[0\] \(chimney\): overlaps the module field"):
        parse_geometry(doc([chimney]))
    tree = {"type": "cylinder", "center": [0, 0], "radius": 1, "bottom": -1, "top": 2}
    with pytest.raises(ShadingError, match="overlaps"):
        parse_geometry(doc([tree]))


def test_solid_next_to_the_module_field_is_accepted():
    chimney = {"type": "box", "center": [3, 0], "width": 1, "depth": 1, "top": 2}
    parse_geometry(doc([chimney]))
